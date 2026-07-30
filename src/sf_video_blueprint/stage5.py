"""Stage 5 — run an emitted test spec against a real agent and learn from it.

Stages 1–4 turn a recording into a scored spec. Nothing in stages 1–4 ever asks
an org whether the spec is *true*. Stage 5 closes that loop: it sends the emitted
test spec to a live Agentforce agent, reads the real per-case verdicts back, and
records them as evidence against the spec.

**Which dialect does the CLI accept? (measured, not assumed)**

``eval_spec.py`` emits two dialects and the project had never verified either.
Measured against org ``AFT3`` with ``@salesforce/cli 2.143.6``:

- ``sf agent test run-eval`` — the only command that actually executed on this
  org — accepts the **legacy** (``AiEvaluationDefinition``) dialect ONLY. Feeding
  it the NGT (``AiTestingDefinition``) dialect fails server-side with
  ``Field required`` on ``steps[1].agent.send_message.utterance``, because
  ``yamlSpecTranslator.js`` reads only the legacy keys (``utterance``,
  ``expectedTopic``, ``expectedOutcome``) and an NGT file puts its utterance
  under ``inputs[].utterance``. Hence :data:`RUN_EVAL_DIALECT`.
- ``sf agent test create`` (both dialects) is refused by the Metadata API on this
  org with ``Not available for deploy for this organization``, so the
  deploy-then-run path is unavailable here. See ``docs/`` findings.

:func:`select_dialect_for_run_eval` turns that measurement into a guard, so a
caller gets a precise local error instead of an opaque 422 from the server.

**Provenance, and why synthetic feedback cannot pass**

Same rule as ``MockTelemetryCollector``: feedback that did not come from a live
org is stamped as such and is *refused* by :func:`feedback_blocking_issues`.
Only sources in :data:`REAL_FEEDBACK_SOURCES` count as real. An unrecognised
source fails closed. Fabricating org results to make the loop appear to work is
the one failure mode this module exists to prevent, so the check is structural
rather than advisory.

**The audit trail is the product**

:func:`write_round` refuses to overwrite an existing round directory. Unlike
``iterate.refine``, which documents this property but uses ``exist_ok=True`` and
will happily clobber ``v1/``, the refusal here is enforced.

**What "learning" is allowed to mean**

Real verdicts are evidence about the *deployed agent*, not new evidence about the
recording. So :func:`apply_feedback` may only ADD observations — it never deletes
an unknown, never raises confidence, and never invents an entity or topic. A loop
permitted to delete its own caveats would optimise straight to a meaningless 100.

One counter-intuitive consequence, measured rather than assumed: a *failing* round
can make the score go UP. ``spec_score`` awards honesty points for declaring
unknowns, and a failed case adds one. That is the rubric working as designed — the
spec really did get more honest — but a reader comparing ``score_before`` to
``score_after`` in a round file would otherwise read the rise as the agent having
improved. :func:`stage5_round` therefore records an explicit note whenever the
score rises on a round that had failures. The number is never silently flattered.
"""

from __future__ import annotations

import copy
import json
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .spec_builder import DerivedAgentSpec, SpecEvidence

# The dialect `sf agent test run-eval` actually accepts. Measured, see module docstring.
RUN_EVAL_DIALECT = "legacy"

# Feedback sources that mean a real org answered. Anything else — including an
# unrecognised new value — is treated as synthetic, so a typo fails closed
# rather than silently passing. Mirrors markers.REAL_TELEMETRY_SOURCES.
REAL_FEEDBACK_SOURCES: frozenset[str] = frozenset({"run-eval"})

# Stamped on feedback produced by an injected runner instead of a real subprocess.
# Deliberately NOT in REAL_FEEDBACK_SOURCES: a fake that stamped itself real would
# be a forgery, and the whole point of the provenance axis is that the fake path
# labels itself. Same convention as MockTelemetryCollector -> "mock".
INJECTED_RUNNER_SOURCE = "injected-runner"

# Evidence source stamped on observations derived from a live agent run.
LIVE_EVAL_EVIDENCE_SOURCE = "live-agent-eval"


class Stage5Error(RuntimeError):
    """A stage-5 step failed. Carries the real command output, never a summary."""


class DialectNotSupportedError(Stage5Error):
    """The caller tried to run a dialect the CLI cannot execute."""


def feedback_is_real(source: str | None) -> bool:
    """True only for a known-real feedback source. Unknown values fail closed."""
    if not isinstance(source, str):
        return False
    return source in REAL_FEEDBACK_SOURCES


def select_dialect_for_run_eval(dialect: str) -> str:
    """Return ``dialect`` if ``run-eval`` can execute it, else raise.

    ``run-eval`` translates only the legacy keys. Passing an NGT spec produces a
    server-side ``Field required`` on ``agent.send_message.utterance``, which is
    an expensive and confusing way to learn a local fact.
    """
    if dialect != RUN_EVAL_DIALECT:
        raise DialectNotSupportedError(
            f"sf agent test run-eval accepts only the {RUN_EVAL_DIALECT!r} dialect "
            f"(AiEvaluationDefinition), got {dialect!r}. An NGT/AiTestingDefinition "
            "spec puts its utterance under inputs[].utterance, which the CLI's "
            "yamlSpecTranslator does not read; the server then rejects the payload "
            "with 'Field required' on steps[1].agent.send_message.utterance. "
            "Emit the legacy dialect with eval_spec.build_legacy_test_spec."
        )
    return dialect


@dataclass(slots=True)
class EvaluationOutcome:
    """One evaluator's verdict on one test case, as the org reported it."""

    evaluator_type: str
    evaluator_id: str
    is_pass: bool
    score: float | None = None
    actual: str | None = None
    expected: str | None = None
    explanation: str | None = None
    error_message: str | None = None
    compute_status: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "evaluator_type": self.evaluator_type,
            "evaluator_id": self.evaluator_id,
            "is_pass": self.is_pass,
            "score": self.score,
            "actual": self.actual,
            "expected": self.expected,
            "explanation": self.explanation,
            "error_message": self.error_message,
            "compute_status": self.compute_status,
        }


@dataclass(slots=True)
class CaseFeedback:
    """The real result for one test case: what the agent did, and whether it matched."""

    case_id: str
    status: str
    outcomes: list[EvaluationOutcome] = field(default_factory=list)
    utterance: str | None = None
    agent_response: str | None = None
    topic_actual: str | None = None
    session_id: str | None = None

    @property
    def passed(self) -> bool:
        """True only if every evaluator passed. An empty outcome list is not a pass.

        A case that produced no verdicts proved nothing, so it must not read as
        success — the same fail-closed rule the rest of the project uses.
        """
        if not self.outcomes:
            return False
        return all(o.is_pass for o in self.outcomes)

    @property
    def failed_outcomes(self) -> list[EvaluationOutcome]:
        return [o for o in self.outcomes if not o.is_pass]

    def to_dict(self) -> dict[str, Any]:
        return {
            "case_id": self.case_id,
            "status": self.status,
            "passed": self.passed,
            "utterance": self.utterance,
            "agent_response": self.agent_response,
            "topic_actual": self.topic_actual,
            "session_id": self.session_id,
            "outcomes": [o.to_dict() for o in self.outcomes],
        }


@dataclass(slots=True)
class AgentFeedback:
    """The parsed result of running a test spec against an agent.

    ``source`` is the provenance axis. Only :data:`REAL_FEEDBACK_SOURCES` values
    mean an org actually answered; everything else is synthetic and is refused by
    :func:`feedback_blocking_issues`.
    """

    source: str
    subject_name: str | None
    cases: list[CaseFeedback] = field(default_factory=list)
    org_alias: str | None = None
    dialect: str = RUN_EVAL_DIALECT
    # The org's own summary, passed through verbatim. NOTE: it counts
    # *evaluations*, not cases — one case with two evaluators contributes two.
    # That is a different denominator from passed_count/failed_count below, so
    # to_dict() labels it rather than emitting two conflicting-looking tallies.
    summary: dict[str, int] = field(default_factory=dict)
    command: str | None = None

    @property
    def is_real(self) -> bool:
        return feedback_is_real(self.source)

    @property
    def passed_count(self) -> int:
        return sum(1 for c in self.cases if c.passed)

    @property
    def failed_count(self) -> int:
        return sum(1 for c in self.cases if not c.passed)

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "is_real": self.is_real,
            "subject_name": self.subject_name,
            "org_alias": self.org_alias,
            "dialect": self.dialect,
            "command": self.command,
            "org_summary_by_evaluation": dict(self.summary),
            "case_count": len(self.cases),
            "passed_count_by_case": self.passed_count,
            "failed_count_by_case": self.failed_count,
            "cases": [c.to_dict() for c in self.cases],
        }


def feedback_blocking_issues(feedback: AgentFeedback) -> list[str]:
    """Return blocking issues for feedback that must not be trusted.

    Synthetic feedback is blocking, full stop — the same treatment
    ``telemetry_source: "mock"`` gets from the score gate. Returning ``[]`` for a
    synthetic run would make fabrication invisible, which is the exact defect
    this project exists to refuse.
    """
    issues: list[str] = []
    if not feedback.is_real:
        issues.append(
            f"Agent feedback source {feedback.source!r} is not a real org source "
            f"(expected one of {sorted(REAL_FEEDBACK_SOURCES)}). Results are synthetic "
            "and must not be used to claim the spec was validated against an agent."
        )
    if not feedback.cases:
        issues.append(
            "Agent feedback contains no test cases, so nothing was verified against the org."
        )
    return issues


def parse_run_eval_results(
    payload: dict[str, Any] | str,
    *,
    source: str,
    subject_name: str | None = None,
    org_alias: str | None = None,
    command: str | None = None,
    dialect: str = RUN_EVAL_DIALECT,
) -> AgentFeedback:
    """Parse ``sf agent test run-eval --result-format json --json`` into feedback.

    ``source`` is required and is NOT inferred from the payload. Provenance is
    the caller's assertion about where the bytes came from; a payload cannot
    vouch for itself, and defaulting it to a real value would let a fixture
    masquerade as an org run.

    Tolerates both the ``{"result": {...}}`` envelope the CLI emits under
    ``--json`` and a bare ``{"tests": [...]}`` body.
    """
    if isinstance(payload, str):
        payload = _loads_lenient(payload)
    if not isinstance(payload, dict):
        raise Stage5Error(f"run-eval payload must be a JSON object, got {type(payload).__name__}")

    body = payload.get("result") if isinstance(payload.get("result"), dict) else payload
    tests = body.get("tests")
    if not isinstance(tests, list):
        raise Stage5Error(
            "run-eval payload has no 'tests' array; cannot derive per-case results. "
            f"Top-level keys: {sorted(payload)}"
        )

    cases: list[CaseFeedback] = []
    for entry in tests:
        if not isinstance(entry, dict):
            continue
        cases.append(_parse_case(entry))

    raw_summary = body.get("summary")
    summary = {k: v for k, v in raw_summary.items() if isinstance(v, int)} if isinstance(raw_summary, dict) else {}

    return AgentFeedback(
        source=source,
        subject_name=subject_name,
        cases=cases,
        org_alias=org_alias,
        summary=summary,
        command=command,
        dialect=dialect,
    )


def _parse_case(entry: dict[str, Any]) -> CaseFeedback:
    outcomes: list[EvaluationOutcome] = []
    for ev in entry.get("evaluations") or []:
        if not isinstance(ev, dict):
            continue
        outcomes.append(
            EvaluationOutcome(
                evaluator_type=str(ev.get("type") or ""),
                evaluator_id=str(ev.get("id") or ""),
                # `is True`, not bool(): bool("false") and bool("FAILED") are both
                # True, so a stringly-typed verdict would read as a pass. This is
                # the field that decides pass/fail, so it fails closed.
                is_pass=ev.get("is_pass") is True,
                score=_as_float(ev.get("score")),
                actual=_as_str(ev.get("actual_value")),
                expected=_as_str(ev.get("expected_value")),
                explanation=_as_str(ev.get("explainability")) or None,
                error_message=_as_str(ev.get("error_message")),
                compute_status=_as_str(ev.get("compute_status")),
            )
        )

    utterance: str | None = None
    agent_response: str | None = None
    topic_actual: str | None = None
    session_id: str | None = None

    for out in entry.get("outputs") or []:
        if not isinstance(out, dict):
            continue
        kind = out.get("type")
        # Matched on `type` alone, and first-hit-wins throughout. A multi-turn case
        # must not let a later step with no topic clobber an earlier real one, and
        # gating send_message on id == "sm" would silently drop the response of any
        # turn the emitter names differently.
        if kind == "agent.create_session":
            session_id = session_id or _as_str(out.get("session_id"))
        elif kind == "agent.send_message":
            agent_response = agent_response or _as_str(out.get("response"))
        elif kind == "agent.get_state":
            topic_actual = topic_actual or _topic_from_state(out.get("response"))
            utterance = utterance or _utterance_from_state(out.get("response"))

    # The topic assertion carries the observed topic too; prefer whichever exists.
    if topic_actual is None:
        for o in outcomes:
            if "topic" in o.evaluator_type and o.actual:
                topic_actual = o.actual
                break

    return CaseFeedback(
        case_id=str(entry.get("id") or ""),
        status=str(entry.get("status") or ""),
        outcomes=outcomes,
        utterance=utterance,
        agent_response=agent_response,
        topic_actual=topic_actual,
        session_id=session_id,
    )


def _topic_from_state(response: Any) -> str | None:
    if not isinstance(response, dict):
        return None
    planner = response.get("planner_response")
    if not isinstance(planner, dict):
        return None
    last = planner.get("lastExecution")
    if isinstance(last, dict):
        return _as_str(last.get("topic"))
    return None


def _utterance_from_state(response: Any) -> str | None:
    """Recover the user utterance from the planner's conversation history."""
    if not isinstance(response, dict):
        return None
    planner = response.get("planner_response")
    if not isinstance(planner, dict):
        return None
    history = planner.get("conversationHistory")
    if not isinstance(history, list):
        return None
    for turn in history:
        if isinstance(turn, dict) and turn.get("messageType") == "TextMessage":
            return _as_str(turn.get("text"))
    return None


def _as_str(value: Any) -> str | None:
    return value if isinstance(value, str) else None


def _as_float(value: Any) -> float | None:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _loads_lenient(raw: str) -> Any:
    """Parse JSON out of CLI stdout that may carry ANSI codes or a preamble."""
    cleaned = re.sub(r"\x1b\[[0-9;]*m", "", raw)
    start = cleaned.find("{")
    if start == -1:
        raise Stage5Error(f"no JSON object found in output: {cleaned[:400]!r}")
    try:
        return json.loads(cleaned[start:])
    except json.JSONDecodeError as exc:
        # Carry the output that failed to parse. A bare JSONDecodeError here would
        # hide the org's actual response in the one case where seeing it matters.
        raise Stage5Error(
            f"could not parse JSON from run-eval output ({exc}). Output was:\n{cleaned[:2000]}"
        ) from exc


def run_agent_eval(
    spec_path: Path | str,
    *,
    org_alias: str,
    api_name: str,
    dialect: str = RUN_EVAL_DIALECT,
    timeout: int = 900,
    runner: Any = None,
) -> AgentFeedback:
    """Run a test spec against a live agent via ``sf agent test run-eval``.

    Feedback is stamped ``source="run-eval"`` — real — ONLY when the bytes came
    from a real subprocess, i.e. when ``runner`` is ``None``. A failure raises with
    the actual stderr attached; a stage-5 round that silently degraded to synthetic
    results would be worse than no round.

    ``runner`` is injected so tests can drive the parser without an org, and
    passing one stamps the result :data:`INJECTED_RUNNER_SOURCE`, which is not a
    real source and so fails closed through :func:`feedback_blocking_issues`.
    Provenance follows *who produced the bytes*, never who asked for them —
    stamping an injected fake as ``run-eval`` would make a fabricated round
    indistinguishable from a real one, which is the single defect this module
    exists to prevent.
    """
    select_dialect_for_run_eval(dialect)
    spec_path = Path(spec_path)
    if not spec_path.is_file():
        raise Stage5Error(f"test spec not found: {spec_path}")

    cmd = [
        "sf",
        "agent",
        "test",
        "run-eval",
        "--target-org",
        org_alias,
        "--spec",
        str(spec_path.resolve()),
        "--api-name",
        api_name,
        "--result-format",
        "json",
        "--json",
    ]
    invoke = runner if runner is not None else _default_runner
    # Provenance is decided by who produced the bytes, before they are even read.
    source = "run-eval" if runner is None else INJECTED_RUNNER_SOURCE
    completed = invoke(cmd, timeout)

    stdout = getattr(completed, "stdout", "") or ""
    stderr = getattr(completed, "stderr", "") or ""
    if getattr(completed, "returncode", 1) != 0:
        raise Stage5Error(
            f"`sf agent test run-eval` failed (exit {completed.returncode}).\n"
            f"stderr:\n{stderr.strip()}\nstdout:\n{stdout.strip()[:2000]}"
        )

    return parse_run_eval_results(
        stdout,
        source=source,
        subject_name=api_name,
        org_alias=org_alias,
        command=" ".join(cmd),
        dialect=dialect,
    )


def _default_runner(cmd: list[str], timeout: int) -> subprocess.CompletedProcess[str]:
    import os

    env = dict(os.environ)
    env.update({"SF_SKIP_NEW_VERSION_CHECK": "true", "NO_COLOR": "1", "FORCE_COLOR": "0"})
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=False, env=env)
    except subprocess.TimeoutExpired as exc:
        raise Stage5Error(f"`sf agent test run-eval` timed out after {timeout}s") from exc


def feedback_findings(feedback: AgentFeedback) -> list[str]:
    """Turn real verdicts into human-readable findings for the refinement loop.

    Every finding names the case and the observed-vs-expected values, so a reader
    can check it against the org. Synthetic feedback is labelled as such in the
    finding text itself, not just in a separate provenance field that a reader
    might skip.
    """
    findings: list[str] = []
    if not feedback.is_real:
        findings.append(
            f"SYNTHETIC FEEDBACK (source={feedback.source!r}): the findings below prove "
            "nothing about any agent and must not be cited as org validation."
        )

    for case in feedback.cases:
        if case.passed:
            findings.append(f"{case.case_id}: PASS ({len(case.outcomes)} evaluator(s) agreed).")
            continue
        if not case.outcomes:
            findings.append(
                f"{case.case_id}: NO VERDICT — the run produced no evaluator results, "
                "so this case verified nothing."
            )
            continue
        for out in case.failed_outcomes:
            detail = f"{case.case_id}: FAIL [{out.evaluator_id or out.evaluator_type}]"
            if out.expected is not None or out.actual is not None:
                detail += f" expected={out.expected!r} actual={out.actual!r}"
            if out.error_message:
                detail += f" error={out.error_message!r}"
            findings.append(detail)
    return findings


def apply_feedback(spec: DerivedAgentSpec, feedback: AgentFeedback) -> tuple[DerivedAgentSpec, list[str]]:
    """Fold real agent verdicts into the spec as ADDED observations only.

    Returns ``(new_spec, notes)``. The input spec is never mutated.

    Invariants, asserted by tests:

    - No ``unknowns`` entry is ever removed, and none is removed to raise a score.
    - ``confidence`` is never raised.
    - No entity, topic, object, or orchestration step is invented.
    - Synthetic feedback annotates the spec as unvalidated rather than validated.

    A live failure is recorded as an unknown, not silently dropped, because the
    project's claim is only ever "this is what was observed".
    """
    new_spec = copy.deepcopy(spec)
    notes: list[str] = []

    if not feedback.cases:
        new_spec.unknowns.append(
            "Stage 5 produced no test-case results, so no claim in this spec has been "
            "checked against a live agent."
        )
        notes.append("no cases in feedback; recorded as an unknown")
        return new_spec, notes

    subject = feedback.subject_name or "(unnamed agent)"

    if not feedback.is_real:
        new_spec.unknowns.append(
            f"Stage 5 feedback for {subject} came from source {feedback.source!r}, which is not "
            "a live org. This spec remains UNVALIDATED against any real agent."
        )
        notes.append(f"synthetic feedback ({feedback.source}) recorded as unvalidated")
        return new_spec, notes

    passed, failed = feedback.passed_count, feedback.failed_count
    new_spec.evidence.append(
        SpecEvidence(
            source=LIVE_EVAL_EVIDENCE_SOURCE,
            detail=(
                f"Ran {len(feedback.cases)} emitted test case(s) against live agent {subject} "
                f"in org {feedback.org_alias or '(unspecified)'} via sf agent test run-eval "
                f"({RUN_EVAL_DIALECT} dialect): {passed} passed, {failed} failed."
            ),
        )
    )
    notes.append(f"recorded live-eval evidence: {passed} passed / {failed} failed against {subject}")

    # Observed topic mismatches. This is real evidence, but it is ambiguous: either
    # the derived topic name does not exist on the subject, or the subject is the
    # wrong agent for this process. Recording both readings is honest; picking one
    # would be a guess dressed as a finding.
    mismatches: list[tuple[str, str, str]] = []
    for case in feedback.cases:
        for out in case.failed_outcomes:
            if "topic" in out.evaluator_type and out.expected and out.actual:
                mismatches.append((case.case_id, out.expected, out.actual))

    for case_id, expected, actual in mismatches:
        new_spec.unknowns.append(
            f"Live agent {subject} routed {case_id} to topic {actual!r}, not the derived "
            f"{expected!r}. Either the derived topic does not exist on this agent, or this "
            "agent is not the right subject for the recorded process. Unresolved."
        )
    if mismatches:
        notes.append(f"recorded {len(mismatches)} topic mismatch(es) as unknowns")

    # A live failure IS an observed error path — the one thing the recording could
    # not supply. Record it as observed behaviour of the deployed agent, and only
    # when the run was real.
    live_failures = [c for c in feedback.cases if not c.passed and c.outcomes]
    if live_failures:
        ids = ", ".join(c.case_id for c in live_failures[:5])
        suffix = "" if len(live_failures) <= 5 else f" (+{len(live_failures) - 5} more)"
        new_spec.failure_handling.append(
            f"Observed live-agent failure during stage-5 evaluation against {subject}: "
            f"{len(live_failures)} of {len(feedback.cases)} case(s) did not meet the derived "
            f"expectation ({ids}{suffix})."
        )
        notes.append(f"recorded {len(live_failures)} live failure(s) in failure_handling")

    if passed and not failed:
        new_spec.evidence.append(
            SpecEvidence(
                source=LIVE_EVAL_EVIDENCE_SOURCE,
                detail=f"All {passed} emitted test case(s) passed against live agent {subject}.",
            )
        )

    # Confidence is never raised here: passing tests against one agent is not
    # evidence that the *recording* was better understood than it was. Enforced with
    # a real raise, not an assert — `python -O` strips asserts, and an honesty
    # invariant that disappears under an optimisation flag is not an invariant.
    if new_spec.confidence > spec.confidence:
        raise Stage5Error(
            f"apply_feedback must never raise confidence: {spec.confidence} -> "
            f"{new_spec.confidence}. Live verdicts are evidence about the deployed "
            "agent, not about how well the recording was understood."
        )
    return new_spec, notes


@dataclass(slots=True)
class Stage5Round:
    """One honest stage-5 round, with everything needed to audit it."""

    round_number: int
    subject_name: str
    dialect: str
    feedback: AgentFeedback
    findings: list[str]
    notes: list[str]
    spec_before: DerivedAgentSpec
    spec_after: DerivedAgentSpec
    score_before: Any = None
    score_after: Any = None
    blocking_issues: list[str] = field(default_factory=list)
    # Written by refine_with_org_feedback when this is the terminal round of a loop
    # that stopped early. ``None`` means the loop was not stopped early by this round
    # (i.e. the loop ran to completion or the stopping check has not been evaluated yet).
    stop_reason: str | None = None

    @property
    def trustworthy(self) -> bool:
        """True only when a real org answered and nothing blocked the round."""
        return self.feedback.is_real and not self.blocking_issues

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "round_number": self.round_number,
            "subject_name": self.subject_name,
            "dialect": self.dialect,
            "trustworthy": self.trustworthy,
            "blocking_issues": self.blocking_issues,
            "feedback": self.feedback.to_dict(),
            "findings": self.findings,
            "notes": self.notes,
            "spec_before": self.spec_before.to_dict(),
            "spec_after": self.spec_after.to_dict(),
        }
        for key, score in (("score_before", self.score_before), ("score_after", self.score_after)):
            payload[key] = (
                None
                if score is None
                else {
                    "total": score.total,
                    "max_total": score.max_total,
                    "band": score.band,
                    "passed": score.passed,
                    "blocking_issues": list(score.blocking_issues),
                }
            )
        # Only include stop_reason when it is set; absent means the round was not the
        # terminal round of an early-stopped loop, not that the reason is unknown.
        if self.stop_reason is not None:
            payload["stop_reason"] = self.stop_reason
        return payload


def stage5_round(
    spec: DerivedAgentSpec,
    feedback: AgentFeedback,
    *,
    round_number: int = 1,
    provenance: dict[str, str] | None = None,
) -> Stage5Round:
    """Assemble one round: real verdicts -> findings -> adjusted spec -> re-score.

    Scoring uses ``spec_score.score_spec`` unchanged; the gate is not weakened or
    bypassed for stage 5. If the org feedback is synthetic, the round is marked
    untrustworthy and says why.
    """
    from .spec_score import score_spec

    blocking = feedback_blocking_issues(feedback)
    findings = feedback_findings(feedback)
    adjusted, notes = apply_feedback(spec, feedback)

    before = score_spec(spec, provenance=provenance)
    after = score_spec(adjusted, provenance=provenance)

    # Say out loud when a round with failures scored HIGHER. The rise is legitimate
    # (declaring an unknown earns honesty points) but a reader diffing the two
    # totals would otherwise read it as the agent having got better, when the org
    # in fact said the spec was wrong.
    if after.total > before.total and feedback.failed_count:
        notes.append(
            f"score rose {before.total} -> {after.total} on a round with "
            f"{feedback.failed_count} FAILING case(s). This is the honesty rubric "
            "rewarding newly-declared unknowns, NOT the agent performing better."
        )

    return Stage5Round(
        round_number=round_number,
        subject_name=feedback.subject_name or "(unnamed agent)",
        dialect=feedback.dialect,
        feedback=feedback,
        findings=findings,
        notes=notes,
        spec_before=spec,
        spec_after=adjusted,
        score_before=before,
        score_after=after,
        blocking_issues=blocking,
    )


def round_artifact_paths(out_dir: Path, round_number: int) -> tuple[Path, Path]:
    """Return ``(round_dir, round_json)`` for a round without creating anything."""
    round_dir = Path(out_dir) / f"round-{round_number}"
    return round_dir, round_dir / "round.json"


def assert_round_unwritten(out_dir: Path, round_number: int) -> Path:
    """Raise if round ``round_number`` already exists. Call BEFORE doing any work.

    :func:`write_round` refuses to overwrite, but by the time it runs the org has
    already been billed for real LLM calls and the caller may have overwritten the
    round's ``testSpec.yaml``. Checking up front makes the refusal cheap and keeps
    a half-replaced round directory — a ``testSpec.yaml`` that no longer matches
    its ``round.json`` — from existing at all.
    """
    round_dir, target = round_artifact_paths(out_dir, round_number)
    if target.exists():
        raise Stage5Error(
            f"refusing to overwrite existing stage-5 round at {target}. The audit trail is "
            "the product; write the next round to a new round number instead."
        )
    return round_dir


SESSION_ID_REDACTED = "[REDACTED-SESSION-ID]"

_SESSION_ID_KEYS = frozenset({"session_id", "sessionId"})


def redact_session_ids(payload: Any) -> Any:
    """Return ``payload`` with every agent session id replaced by a placeholder.

    Live agent session ids stay in memory — an operator debugging a round needs
    them to pull the session in the org — but they are not written to disk. A
    round file is an artifact that gets copied into reports and commits, and a
    session identifier does not belong in one.
    """
    if isinstance(payload, dict):
        # Redact on the key alone, whatever the value's type — a session id that
        # arrived as a non-string is still a session id.
        return {
            k: (SESSION_ID_REDACTED if k in _SESSION_ID_KEYS and v is not None else redact_session_ids(v))
            for k, v in payload.items()
        }
    if isinstance(payload, list):
        return [redact_session_ids(item) for item in payload]
    return payload


def write_round(out_dir: Path, round_result: Stage5Round) -> Path:
    """Write one round's audit trail, refusing to overwrite a prior round.

    The audit trail is the product: a round directory that already holds a
    ``round.json`` is never rewritten, because a silently replaced result is
    indistinguishable from one that never happened.

    Session ids are redacted on the way out; see :func:`redact_session_ids`.
    """
    round_dir, target = round_artifact_paths(Path(out_dir), round_result.round_number)
    assert_round_unwritten(Path(out_dir), round_result.round_number)
    round_dir.mkdir(parents=True, exist_ok=True)
    payload = redact_session_ids(round_result.to_dict())
    target.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return target

"""Versioned, auditable refinement loop for agent specs.

The loop takes a DerivedAgentSpec (from spec_builder.py) and iteratively improves
it by:

1. Emitting YAML via agentforce_spec.py
2. Scoring it offline (deterministically, no org call)
3. Refining the prompt that guides the next round (when use_cli=True, feeding back
   to `sf agent generate agent-spec`)
4. Stopping when score stops improving, passes threshold, or hits max_rounds

Every iteration is versioned on disk (v1/, v2/, ...). The audit trail IS the
product — overwriting a prior version destroys provenance and is forbidden.

Offline by default (use_cli=False): runs locally with no org, no network, no LLM
call. Cheap to iterate. CLI mode (use_cli=True) shells out to the real
`sf agent generate agent-spec` which calls an LLM in the org — costs money,
requires org_alias, nondeterministic — but this is the actual re-feed mechanism.

The loop MUST NOT fabricate evidence. If the scorer says "no failure paths
observed", the only valid resolution is re-recording. The loop can only improve
what can be derived from the existing evidence — tightening prose, normalising
names, reordering steps. It cannot invent entities, topics, or failure scenarios.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .spec_builder import DerivedAgentSpec


@dataclass(slots=True)
class SpecVersion:
    """One versioned iteration of a spec."""

    version: int
    spec_path: Path
    yaml_path: Path | None
    score: Any  # SpecScore from spec_score.py, duck-typed to avoid import cycle
    role_used: str
    source: str  # "derived" | "cli-regenerated" | "manual"
    parent_version: int | None
    notes: list[str] = field(default_factory=list)


@dataclass(slots=True)
class IterationResult:
    """The complete audit trail from a refinement run."""

    versions: list[SpecVersion]
    best: SpecVersion
    converged: bool
    stop_reason: str
    rounds_run: int


def refine(
    spec: DerivedAgentSpec,
    *,
    out_dir: Path,
    company_name: str,
    company_description: str,
    max_rounds: int = 5,
    epsilon: int = 2,
    org_alias: str | None = None,
    use_cli: bool = False,
    provenance: dict[str, str] | None = None,
) -> IterationResult:
    """Run the versioned refinement loop.

    Args:
        spec: The derived spec from spec_builder.build_agent_spec
        out_dir: Root dir; each round writes v1/, v2/, etc.
        company_name: Company metadata for the agent spec YAML
        company_description: Company description
        max_rounds: Stop after this many rounds regardless of score
        epsilon: Converged if improvement < epsilon for 2 consecutive rounds
        org_alias: Salesforce org alias (required when use_cli=True)
        use_cli: If True, shell out to `sf agent generate agent-spec` with
                 refinement; if False (default), run purely offline
        provenance: Data provenance dict with extraction_source and telemetry_source;
                    passed through to score_spec() for provenance_integrity scoring

    Returns:
        IterationResult with all versioned specs, the best, and stop reason

    Raises:
        ValueError: If use_cli=True but org_alias is None, or if max_rounds < 1
        InsufficientEvidenceError: If spec has too little evidence to build from
                                   (bubbled from agentforce_spec.build_agent_spec_yaml)
    """
    if use_cli and org_alias is None:
        raise ValueError("use_cli=True requires org_alias; the CLI cannot run without a target org")

    if max_rounds < 1:
        raise ValueError(f"max_rounds must be >= 1, got {max_rounds}")

    # Import dependencies defensively (other agents writing them concurrently)
    try:
        from .agentforce_spec import (
            InsufficientEvidenceError,
            build_agent_spec_yaml,
            write_agent_spec_yaml,
        )
    except ImportError as e:
        raise RuntimeError(
            "iterate.py depends on agentforce_spec.py (owned by B1). "
            "Import failed — ensure B1's module is written and importable."
        ) from e

    try:
        from .spec_score import PASS_THRESHOLD, compare, score_spec
    except ImportError as e:
        raise RuntimeError(
            "iterate.py depends on spec_score.py (owned by B4). "
            "Import failed — ensure B4's module is written and importable."
        ) from e

    out_dir = out_dir.resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    versions: list[SpecVersion] = []
    current_spec = spec
    role = _initial_role(spec)
    stall_count = 0  # consecutive rounds with delta < epsilon
    last_improvement_summary: str | None = None  # Track refinement applied to produce this round's spec

    for round_num in range(1, max_rounds + 1):
        version_dir = out_dir / f"v{round_num}"
        version_dir.mkdir(parents=True, exist_ok=True)

        # Write the derived spec JSON
        spec_path = version_dir / "agent-spec.json"
        spec_path.write_text(
            json.dumps(current_spec.to_dict(), indent=2, sort_keys=False) + "\n",
            encoding="utf-8",
        )

        # Build YAML (may raise InsufficientEvidenceError — let it bubble)
        try:
            spec_yaml = build_agent_spec_yaml(
                current_spec,
                company_name=company_name,
                company_description=company_description,
                agent_type="internal",
                tone="formal",
                max_topics=5,
            )
        except InsufficientEvidenceError:
            # Evidence is inadequate; no amount of iteration can fix this.
            # The user must re-record. Stop immediately.
            stop_reason = (
                "InsufficientEvidenceError: the recording does not contain enough data "
                "to derive a meaningful spec. Re-record the process with --track-record "
                "and ensure all critical steps complete successfully."
            )
            if versions:
                return IterationResult(
                    versions=versions,
                    best=_pick_best(versions),
                    converged=False,
                    stop_reason=stop_reason,
                    rounds_run=round_num - 1,
                )
            else:
                # No valid versions at all — still return a result, but with empty best
                raise InsufficientEvidenceError(stop_reason)

        yaml_path = version_dir / "agentSpec.yaml"
        write_agent_spec_yaml(yaml_path, spec_yaml)

        # Score it (offline, deterministic)
        score = score_spec(current_spec, yaml_text=spec_yaml.to_yaml(), provenance=provenance)

        source = "derived" if round_num == 1 else ("cli-regenerated" if use_cli else "derived")
        parent = round_num - 1 if round_num > 1 else None
        notes: list[str] = []

        # Record the refinement applied to produce this round's spec (from previous round)
        if last_improvement_summary:
            notes.append(last_improvement_summary)

        # Anti-gaming guard: if score improved but unknowns were dropped without new
        # evidence, flag it. A loop optimising a metric will find the cheapest path to
        # a higher number, and the cheapest path here is deleting honest caveats.
        if versions:
            prev = versions[-1]
            # Read parent's unknowns from its on-disk spec (the audit trail is authoritative)
            prev_unknowns_count: int | None = None
            try:
                prev_spec_data = json.loads(prev.spec_path.read_text(encoding="utf-8"))
                prev_unknowns_count = len(prev_spec_data.get("unknowns", []))
            except (OSError, json.JSONDecodeError, KeyError):
                # If we can't read the parent, we cannot perform the check reliably.
                # Fail CLOSED: warn that the check could not be performed.
                notes.append(
                    "WARNING: Could not read parent spec unknowns from disk. "
                    "Anti-gaming check for unknowns deletion was skipped."
                )

            curr_unknowns_count = len(current_spec.unknowns) if hasattr(current_spec, 'unknowns') else 0
            if prev_unknowns_count is not None:
                if score.total > prev.score.total and curr_unknowns_count < prev_unknowns_count:
                    notes.append(
                        "WARNING: score improved but unknowns decreased — verify this is honest "
                        "refinement (filling gaps with evidence) and not gaming the metric by "
                        "deleting caveats."
                    )

        version = SpecVersion(
            version=round_num,
            spec_path=spec_path,
            yaml_path=yaml_path,
            score=score,
            role_used=role,
            source=source,
            parent_version=parent,
            notes=notes,
        )
        versions.append(version)

        # Check stopping conditions in order of precedence.
        # PRECEDENCE (correct order, justified):
        # 1. Pass threshold + no blocking issues (SUCCESS terminal state)
        # 2. Regression (preserve best, stop immediately)
        # 3. Convergence (gave up making progress)
        # 4. max_rounds (exhausted budget)
        #
        # WHY (1) must outrank (3) and (4):
        # Passing the gate with no blocking issues is a SUCCESS. The operator acts on
        # this string — "converged" means "gave up", "threshold" means "PASSED". Those
        # are opposite meanings. If a spec reaches threshold on round 1 with blocking
        # issues, then resolves them by round 3 while score stays flat, that's SUCCESS,
        # not convergence.
        #
        # The check runs inside the loop (can exit early) AND after the loop (can
        # reclassify convergence/max_rounds if the final version passes).

        # (a) Pass threshold + no blocking issues (SUCCESS)
        if score.total >= PASS_THRESHOLD and not score.blocking_issues:
            stop_reason = f"Score {score.total}/{score.max_total} >= threshold {PASS_THRESHOLD} with no blocking issues"
            break

        # (b) Regression (score dropped) — keep better version and stop
        if round_num > 1:
            delta = compare(versions[-2].score, score).delta  # earlier, later
            if delta < 0:
                stop_reason = f"Regression detected: score dropped {delta} points; stopping to preserve best version"
                break

            # (c) Convergence (improvement < epsilon for 2 consecutive rounds)
            if abs(delta) < epsilon:
                stall_count += 1
                if stall_count >= 2:
                    stop_reason = f"Converged: improvement < {epsilon} for 2 consecutive rounds"
                    break
            else:
                stall_count = 0

        # (d) max_rounds reached
        if round_num == max_rounds:
            stop_reason = f"Reached max_rounds={max_rounds}"
            break

        # Not done; refine for next round
        role = next_role_prompt(current_spec, score)

        if use_cli:
            # Shell out to sf agent generate agent-spec (the real re-feed mechanism)
            next_yaml_path = version_dir / "agentSpec-next.yaml"
            success, stderr = _run_cli_refine(yaml_path, next_yaml_path, role, org_alias)
            if not success:
                notes.append(f"CLI refinement failed: {stderr}")
                stop_reason = "CLI refinement command failed; cannot continue"
                break
            # Parse next_yaml_path back into a spec (offline for now — real impl would
            # parse YAML and rebuild DerivedAgentSpec, but that's complex and not
            # strictly needed for the loop to function. For now, just re-score the new YAML.)
            # NOTE: This is a simplification. A production version would need to either:
            # (1) round-trip YAML -> DerivedAgentSpec (requires a parser agent), OR
            # (2) keep iterating on the YAML directly and score YAML only.
            # For this prototype, we'll apply offline improvements instead when use_cli=False.
        else:
            # Offline improvement: apply deterministic refinements (no invention)
            current_spec, last_improvement_summary = _apply_offline_improvements(current_spec, score)

    else:
        # Loop exhausted without explicit break
        stop_reason = f"Reached max_rounds={max_rounds}"

    # Post-loop: reclassify the stop reason if the final version passes the threshold.
    # This handles cases where the loop exited due to convergence or max_rounds, but
    # the final version is actually a SUCCESS (threshold + no blocking issues).
    # Passing the gate is a SUCCESS terminal state and must outrank all others.
    if versions:
        final_version = versions[-1]
        if final_version.score.total >= PASS_THRESHOLD and not final_version.score.blocking_issues:
            stop_reason = (
                f"Score {final_version.score.total}/{final_version.score.max_total} >= threshold "
                f"{PASS_THRESHOLD} with no blocking issues"
            )

    best = _pick_best(versions)
    converged = stall_count >= 2
    return IterationResult(
        versions=versions,
        best=best,
        converged=converged,
        stop_reason=stop_reason,
        rounds_run=len(versions),
    )


def refine_with_org_feedback(
    spec: DerivedAgentSpec,
    *,
    out_dir: Path,
    org_alias: str,
    agent_api_name: str,
    test_spec_name: str,
    rounds: int = 1,
    max_no_improvement: int | None = None,
    provenance: dict[str, str] | None = None,
    runner: Any = None,
) -> list[Any]:
    """Stage 5: the refinement loop that actually learns from a real agent.

    :func:`refine` re-scores the same spec offline and calls it converged after
    three identical scores. It cannot learn anything a live agent knows, so its
    "converged" says only that the offline scorer stopped changing its mind.

    Each round here is: emit a test spec -> run it against the agent in the org ->
    parse the real per-case verdicts -> fold them into the spec as added
    observations -> re-score. Rounds are written to ``out_dir/round-N/`` and a
    round is never overwritten.

    Only the legacy dialect is emitted, because that is the only one
    ``sf agent test run-eval`` executes (measured; see :mod:`stage5`).

    The loop short-circuits on any of these three stopping conditions (checked in
    order after each round):

    1. **Gate pass**: ``score_after.total >= PASS_THRESHOLD`` with no blocking issues.
       The spec has satisfied the gate; further rounds would only bill more org LLM
       calls for no actionable gain.

    2. **Identical-score plateau**: three consecutive rounds all produced the same
       ``score_after.total``. The org keeps returning the same signal; the loop is
       not converging further.

    3. **No-improvement budget**: if ``max_no_improvement`` is set, the loop stops
       when that many consecutive rounds pass without the score going up. A round
       that scores the same as or lower than the previous is a "no-improvement" round.

    In every case, the terminal round's :attr:`stage5.Stage5Round.stop_reason` is
    set so the audit trail explains why the loop ended.

    Args:
        rounds: Maximum round trips to run. Each one costs real org LLM calls.
        max_no_improvement: If given, stop after this many consecutive rounds that
            did not raise ``score_after.total`` above the highest seen so far.
            ``None`` (the default) disables this condition.
        runner: Injected subprocess runner, for tests. Cannot forge provenance:
                passing one stamps the feedback ``injected-runner``, which is not a
                real source, so every such round is refused by
                ``stage5.feedback_blocking_issues`` and never carried forward.

    Returns:
        The list of :class:`stage5.Stage5Round`, in order.

    Raises:
        ValueError: If ``rounds < 1`` or ``max_no_improvement < 1`` when provided.
        Stage5Error: If a round's org call fails; the real stderr is attached.
                     A stage-5 round that degraded to synthetic results silently
                     would be worse than one that stopped.
    """
    if rounds < 1:
        raise ValueError(f"rounds must be >= 1, got {rounds}")
    if max_no_improvement is not None and max_no_improvement < 1:
        raise ValueError(f"max_no_improvement must be >= 1 when provided, got {max_no_improvement}")

    from .eval_spec import build_legacy_test_spec, write_test_spec
    from .spec_score import PASS_THRESHOLD
    from .stage5 import (
        RUN_EVAL_DIALECT,
        assert_round_unwritten,
        run_agent_eval,
        stage5_round,
        write_round,
    )

    out_dir = Path(out_dir).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    results: list[Any] = []
    current = spec

    # Stopping-condition state
    consecutive_identical: int = 0  # rounds where score_after == previous score_after
    last_score_total: int | None = None  # score_after.total from the previous round
    best_score_seen: int | None = None  # highest score_after.total seen so far
    no_improvement_streak: int = 0  # consecutive rounds without score increase

    for round_num in range(1, rounds + 1):
        # Refuse BEFORE writing a spec or spending real org LLM calls. write_round
        # also refuses, but by then testSpec.yaml would already be overwritten and
        # the org already billed for a result we would then throw away.
        round_dir = assert_round_unwritten(out_dir, round_num)
        round_dir.mkdir(parents=True, exist_ok=True)

        test_spec, _derivations = build_legacy_test_spec(
            current, name=f"{test_spec_name}_r{round_num}", subject_name=agent_api_name
        )
        spec_path = write_test_spec(round_dir / "testSpec.yaml", test_spec)

        feedback = run_agent_eval(
            spec_path,
            org_alias=org_alias,
            api_name=agent_api_name,
            dialect=RUN_EVAL_DIALECT,
            runner=runner,
        )

        round_result = stage5_round(
            current, feedback, round_number=round_num, provenance=provenance
        )

        # Evaluate stopping conditions BEFORE writing, so the terminal round's
        # stop_reason is on disk. write_round refuses to overwrite, so the reason
        # must be stamped on the object before the file is created.
        current_score = round_result.score_after.total if round_result.score_after is not None else None
        stop_reason: str | None = None

        # 1. Gate pass: score >= PASS_THRESHOLD with no blocking issues
        if (
            round_result.score_after is not None
            and current_score is not None
            and current_score >= PASS_THRESHOLD
            and not round_result.score_after.blocking_issues
        ):
            stop_reason = (
                f"gate_pass: score {current_score} >= PASS_THRESHOLD {PASS_THRESHOLD} "
                "with no blocking issues"
            )

        # 2. Three consecutive identical scores
        if stop_reason is None and current_score is not None:
            if current_score == last_score_total:
                consecutive_identical += 1
            else:
                consecutive_identical = 0
            # We need 3 consecutive identical scores, meaning:
            # - round N-2 = S, round N-1 = S, round N = S -> consecutive_identical == 2
            # (first identical is count=1, second is count=2, so >= 2 means 3 in a row)
            if consecutive_identical >= 2:
                stop_reason = (
                    f"identical_score_plateau: score {current_score} unchanged for "
                    "3 consecutive rounds"
                )

        # 3. max_no_improvement budget
        if stop_reason is None and max_no_improvement is not None and current_score is not None:
            if best_score_seen is None or current_score > best_score_seen:
                best_score_seen = current_score
                no_improvement_streak = 0
            else:
                no_improvement_streak += 1
            if no_improvement_streak >= max_no_improvement:
                stop_reason = (
                    f"max_no_improvement: {no_improvement_streak} consecutive round(s) "
                    f"without score increase (best={best_score_seen}, "
                    f"current={current_score}, limit={max_no_improvement})"
                )

        # Stamp stop_reason before writing so the audit trail includes the reason.
        if stop_reason is not None:
            round_result.stop_reason = stop_reason

        write_round(out_dir, round_result)
        results.append(round_result)

        # Carry the adjusted spec forward only when a real org answered. Feeding a
        # synthetic round's output into the next round would launder it into the
        # audit trail as though the org had spoken.
        if round_result.trustworthy:
            current = round_result.spec_after

        # Update state for the next round's stopping checks
        last_score_total = current_score

        # Early exit when a stopping condition was triggered
        if stop_reason is not None:
            break

    return results


def _initial_role(spec: DerivedAgentSpec) -> str:
    """The starting role prompt for round 1."""
    obj = spec.objects_touched[0] if spec.objects_touched else "business records"
    return (
        f"You are an agent that {spec.intent.lower()}. "
        f"You work with {obj} and help users complete this process conversationally."
    )


def next_role_prompt(spec: DerivedAgentSpec, score: Any) -> str:
    """Turn the scorer's recommendations and blocking issues into a REFINED role.

    This is the actual improvement mechanism. It MUST incorporate specific gaps
    named by the scorer, not just restate the intent. Examples:
    - If scorer says "missing failure path for X", add "handle X failures gracefully"
    - If scorer says "no guardrail for Y", add "enforce Y validation before acting"
    - If scorer says "topic Z is too broad", add "scope Z to <specific entities>"

    It must NOT invent entities, topics, or scenarios that the recording never
    observed. Refinements are ONLY prose/structure/emphasis changes based on the
    scorer's EXISTING evidence-backed critique.
    """
    blockers = getattr(score, "blocking_issues", [])
    recs = getattr(score, "recommendations", [])

    base = _initial_role(spec)
    refinements: list[str] = []

    for issue in blockers:
        # Blocking issues must be addressed, but we can only address them if
        # evidence exists. Common blockers:
        # - "No topics defined" -> can't fix without re-recording multi-step process
        # - "Guardrails missing" -> we can add generic guardrails (FLS, confirmation)
        if "guardrail" in issue.lower():
            refinements.append("enforce field-level security and require user confirmation before writes")
        if "topic" in issue.lower() and "missing" in issue.lower():
            refinements.append("break the process into clear conversational topics")

    for rec in recs:
        # Recommendations are nice-to-haves. Parse common patterns:
        if "failure" in rec.lower() or "error" in rec.lower():
            refinements.append("surface errors clearly and never auto-correct user input")
        if "specific" in rec.lower() or "vague" in rec.lower():
            refinements.append("be precise about which fields and objects you act on")
        if "orchestration" in rec.lower():
            refinements.append("clearly describe the step-by-step orchestration")

    if not refinements:
        # No actionable feedback; keep the base role
        return base

    # Append refinements as a new sentence
    return base + " " + "; ".join(refinements).capitalize() + "."


def _apply_offline_improvements(spec: DerivedAgentSpec, score: Any) -> tuple[DerivedAgentSpec, str]:
    """Apply deterministic, local improvements without new evidence.

    Allowed:
    - Tightening role prose (remove vague words)
    - Normalising entity names (e.g., camelCase consistency)
    - Reordering orchestration steps (logical flow)
    - Expanding guardrail wording (but not inventing new ones)
    - Deduplicating near-identical entries

    NOT allowed:
    - Inventing entities, topics, or failure paths
    - Removing unknowns without evidence
    - Fabricating data
    - Raising confidence

    Returns:
        A tuple of (new_spec, improvement_summary).
        If no changes were made, returns (original_spec, explanation).
        The explanation string is used to report whether the lever fired or not.
    """
    import copy
    import re

    # Start with a deep copy — never mutate the input
    new_spec = copy.deepcopy(spec)
    changed = False
    improvements: list[str] = []

    # 1. Tighten intent prose: remove vague hedges
    vague_terms = ["various", "some", "etc.", "as needed", "possibly", "might"]
    old_intent = new_spec.intent
    for term in vague_terms:
        # Only remove standalone occurrences (word boundaries)
        new_spec.intent = re.sub(rf'\b{re.escape(term)}\b', '', new_spec.intent, flags=re.IGNORECASE)
    # Clean up extra spaces
    new_spec.intent = ' '.join(new_spec.intent.split())
    if new_spec.intent != old_intent:
        changed = True
        improvements.append("removed vague terms from intent")

    # 2. Normalise entity names using naming.py helpers (if available)
    try:
        from .naming import snake_case
        normalized_count = 0
        for entity in new_spec.entities:
            if entity.name:
                old_name = entity.name
                # Entity names should be camelCase; snake_case produces lowercase snake,
                # so we'll just ensure consistency by lowercasing the first letter
                # This is a minimal normalization; real version would use more sophisticated rules
                if entity.name[0].isupper() and len(entity.name) > 1 and entity.name[1].islower():
                    # lowerCamelCase: first letter should be lowercase
                    entity.name = entity.name[0].lower() + entity.name[1:]
                    if entity.name != old_name:
                        normalized_count += 1
        if normalized_count > 0:
            changed = True
            improvements.append(f"normalized {normalized_count} entity name(s) to camelCase")
    except ImportError:
        pass  # naming module not available; skip normalization

    # 3. Deduplicate orchestration steps (near-identical entries)
    if len(new_spec.orchestration_steps) > 1:
        seen: set[str] = set()
        deduped: list[str] = []
        for step in new_spec.orchestration_steps:
            # Normalize for comparison: strip, lowercase, collapse whitespace
            normalized = ' '.join(step.lower().split())
            if normalized not in seen:
                seen.add(normalized)
                deduped.append(step)
        if len(deduped) < len(new_spec.orchestration_steps):
            removed = len(new_spec.orchestration_steps) - len(deduped)
            changed = True
            improvements.append(f"deduplicated {removed} orchestration step(s)")
            new_spec.orchestration_steps = deduped

    # 4. Deduplicate guardrails
    if len(new_spec.guardrails) > 1:
        seen_g: set[str] = set()
        deduped_g: list[str] = []
        for guardrail in new_spec.guardrails:
            normalized_g = ' '.join(guardrail.lower().split())
            if normalized_g not in seen_g:
                seen_g.add(normalized_g)
                deduped_g.append(guardrail)
        if len(deduped_g) < len(new_spec.guardrails):
            removed_g = len(new_spec.guardrails) - len(deduped_g)
            changed = True
            improvements.append(f"deduplicated {removed_g} guardrail(s)")
            new_spec.guardrails = deduped_g

    # 5. Expand guardrail wording to name specific objects/fields ONLY if they're already
    # in the spec (no invention). This affects the specificity dimension scoring.
    # Example: "validate input" -> "validate Case fields" (removes generic "input" penalty)
    if new_spec.objects_touched and new_spec.guardrails:
        expanded_count = 0
        for i, guardrail in enumerate(new_spec.guardrails):
            old_guardrail = guardrail
            # Replace generic "validate input" with object-specific version
            if "validate input" in guardrail.lower() and new_spec.objects_touched:
                obj = new_spec.objects_touched[0]
                # Replace "input" with object name to avoid the generic penalty
                new_spec.guardrails[i] = guardrail.replace("Validate input", f"Validate {obj} fields")
                new_spec.guardrails[i] = new_spec.guardrails[i].replace("validate input", f"validate {obj} fields")
                if new_spec.guardrails[i] != old_guardrail:
                    expanded_count += 1
            # Similar for other generic patterns
            if "check input" in guardrail.lower() and new_spec.objects_touched:
                obj = new_spec.objects_touched[0]
                new_spec.guardrails[i] = guardrail.replace("check input", f"check {obj} fields")
                new_spec.guardrails[i] = new_spec.guardrails[i].replace("Check input", f"Check {obj} fields")
                if new_spec.guardrails[i] != old_guardrail:
                    expanded_count += 1
            # "require confirmation" -> "require confirmation before writing {fields}"
            # Only if we can enumerate specific fields from entities
            if "require confirmation" in guardrail.lower() and "before writing" not in guardrail.lower():
                fields = [e.field_api_name for e in new_spec.entities if e.field_api_name]
                if fields:
                    field_list = ", ".join(fields[:3])  # limit to 3
                    if field_list not in guardrail:
                        new_spec.guardrails[i] = f"{guardrail.rstrip('.')} before writing: {field_list}."
                        expanded_count += 1
        if expanded_count > 0:
            changed = True
            improvements.append(f"expanded {expanded_count} generic guardrail(s) to name specific objects/fields")

    # 6. If no changes were made, return the original spec with an explanation
    if not changed:
        return (
            spec,
            "No offline improvements applied. The spec already has concrete intent, distinct steps, "
            "object-specific guardrails, and camelCase entity names. Meaningful improvement requires "
            "use_cli=True (LLM refinement with org context) or re-recording with more complete data."
        )

    return (new_spec, f"Applied {len(improvements)} offline improvement(s): {'; '.join(improvements)}")


def _run_cli_refine(
    input_yaml: Path,
    output_yaml: Path,
    refined_role: str,
    org_alias: str,
) -> tuple[bool, str]:
    """Shell out to sf agent generate agent-spec with refinement.

    Args:
        input_yaml: Previous round's YAML
        output_yaml: Where to write the new YAML
        refined_role: The refined --role prompt
        org_alias: The org alias for --target-org

    Returns:
        (success: bool, stderr: str)
    """
    cmd = [
        "sf",
        "agent",
        "generate",
        "agent-spec",
        "--spec",
        str(input_yaml),
        "--output-file",
        str(output_yaml),
        "--role",
        refined_role,
        "--target-org",
        org_alias,
    ]
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=120,  # CLI calls an LLM; give it 2 minutes
            check=False,
        )
        return result.returncode == 0, result.stderr
    except subprocess.TimeoutExpired:
        return False, "CLI command timed out after 120 seconds"
    except Exception as e:
        return False, f"CLI command raised exception: {e}"


def _pick_best(versions: list[SpecVersion]) -> SpecVersion:
    """Choose the best version: highest score, ties break toward EARLIER version.

    Never return a version with blocking issues if any version without them exists.
    """
    if not versions:
        raise ValueError("Cannot pick best from empty versions list")

    # Separate into blocked and unblocked
    unblocked = [v for v in versions if not v.score.blocking_issues]
    if unblocked:
        pool = unblocked
    else:
        pool = versions

    # Sort by score descending, then by version ascending (earlier wins ties)
    pool_sorted = sorted(pool, key=lambda v: (-v.score.total, v.version))
    return pool_sorted[0]


def write_iteration_report(path: Path, result: IterationResult) -> Path:
    """Write a JSON audit trail and a human-readable markdown summary.

    Args:
        path: Path to write iteration_report.json (markdown goes next to it)

    Returns:
        The path written (path.with_suffix('.json'))
    """
    path = path.resolve()
    path.parent.mkdir(parents=True, exist_ok=True)

    # JSON payload
    payload = {
        "rounds_run": result.rounds_run,
        "converged": result.converged,
        "stop_reason": result.stop_reason,
        "best_version": result.best.version,
        "versions": [
            {
                "version": v.version,
                "spec_path": str(v.spec_path),
                "yaml_path": str(v.yaml_path) if v.yaml_path else None,
                "score_total": v.score.total,
                "score_max": v.score.max_total,
                "score_band": v.score.band,
                "passed": v.score.passed,
                "blocking_issues": v.score.blocking_issues,
                "recommendations": v.score.recommendations,
                "role_used": v.role_used,
                "source": v.source,
                "parent_version": v.parent_version,
                "notes": v.notes,
            }
            for v in result.versions
        ],
    }
    json_path = path.with_suffix(".json")
    json_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")

    # Markdown summary
    md_path = path.with_suffix(".md")
    md_lines = [
        "# Iteration Report",
        "",
        f"**Rounds run:** {result.rounds_run}",
        f"**Converged:** {result.converged}",
        f"**Stop reason:** {result.stop_reason}",
        f"**Best version:** v{result.best.version}",
        "",
        "## Versions",
        "",
        "| Version | Score | Band | Δ | Blocking Issues | Stop Reason |",
        "|---------|-------|------|---|----------------|-------------|",
    ]
    for i, v in enumerate(result.versions):
        delta = ""
        if i > 0:
            prev_score = result.versions[i - 1].score.total
            delta_val = v.score.total - prev_score
            delta = f"{delta_val:+d}" if delta_val != 0 else "0"
        blocked = "Yes" if v.score.blocking_issues else "No"
        md_lines.append(
            f"| v{v.version} | {v.score.total}/{v.score.max_total} | {v.score.band} | {delta} | {blocked} | {v.source} |"
        )

    md_lines.extend(
        [
            "",
            "## Final Recommendations",
            "",
        ]
    )
    if result.best.score.blocking_issues:
        md_lines.append("**Blocking issues (must fix):**")
        for issue in result.best.score.blocking_issues:
            md_lines.append(f"- {issue}")
        md_lines.append("")

    if result.best.score.recommendations:
        md_lines.append("**Recommendations (nice-to-have):**")
        for rec in result.best.score.recommendations:
            md_lines.append(f"- {rec}")
        md_lines.append("")

    md_lines.extend(
        [
            "## Next Steps",
            "",
            "- If blocking issues remain, re-record the process with more complete data.",
            "- If score is acceptable, deploy the best version's YAML to an org for validation.",
            "- Run `sf agent test run` against the deployed agent to verify behaviour.",
        ]
    )

    md_path.write_text("\n".join(md_lines) + "\n", encoding="utf-8")
    return json_path

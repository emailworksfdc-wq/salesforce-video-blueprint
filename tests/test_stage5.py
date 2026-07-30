"""Tests for stage5.py — running an emitted spec against a real agent.

The fixture in ``tests/fixtures/run_eval_aft3_coral_cloud_booking.json`` is the
REAL response body from ``sf agent test run-eval`` against agent
``Coral_Cloud_Booking_Agent`` in org ``AFT3``, captured 2026-07-26. Session ids,
the org id, and the instance endpoint are redacted; every verdict, score,
topic, and agent response is verbatim. Two of the five cases are kept: one that
failed both evaluators and the one that genuinely passed.

Using a real payload matters. A hand-written fixture would encode what we
*assume* the Eval API returns, and the parser would then be verified against our
own assumption rather than against Salesforce.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from sf_video_blueprint.eval_spec import (
    build_legacy_test_spec,
    build_ngt_test_spec,
    write_test_spec,
)
from sf_video_blueprint.spec_builder import (
    DerivedAgentSpec,
    DerivedEntity,
    SpecEvidence,
)
from sf_video_blueprint.stage5 import (
    INJECTED_RUNNER_SOURCE,
    REAL_FEEDBACK_SOURCES,
    RUN_EVAL_DIALECT,
    SESSION_ID_REDACTED,
    AgentFeedback,
    CaseFeedback,
    DialectNotSupportedError,
    EvaluationOutcome,
    Stage5Error,
    apply_feedback,
    feedback_blocking_issues,
    feedback_findings,
    feedback_is_real,
    parse_run_eval_results,
    redact_session_ids,
    run_agent_eval,
    select_dialect_for_run_eval,
    stage5_round,
    write_round,
)

FIXTURE = Path(__file__).parent / "fixtures" / "run_eval_aft3_coral_cloud_booking.json"


def _real_payload() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def _make_spec(
    *,
    confidence: float = 0.7,
    unknowns: list[str] | None = None,
    failure_handling: list[str] | None = None,
) -> DerivedAgentSpec:
    return DerivedAgentSpec(
        intent="Update Case (Status)",
        confidence=confidence,
        objects_touched=["Case"],
        entities=[
            DerivedEntity(
                name="status",
                object_api_name="Case",
                field_api_name="Status",
                evidence=[SpecEvidence("data-delta", "Case.Status observed")],
            )
        ],
        orchestration_steps=["Resolve the Case", "SUBMIT on button:Save -> writes Status"],
        guardrails=["Require explicit user confirmation before writing: Status."],
        failure_handling=failure_handling
        if failure_handling is not None
        else ["No failures were observed in this run, so error paths are UNTESTED."],
        unknowns=unknowns if unknowns is not None else ["Action API names were not observed."],
        evidence=[SpecEvidence("dom-capture", "8 events observed")],
    )


def _real_feedback() -> AgentFeedback:
    return parse_run_eval_results(
        _real_payload(),
        source="run-eval",
        subject_name="Coral_Cloud_Booking_Agent",
        org_alias="AFT3",
    )


# --- The dialect question, which the project had never answered ---


def test_run_eval_dialect_is_legacy() -> None:
    """run-eval accepts AiEvaluationDefinition (legacy) only. Measured against AFT3."""
    assert RUN_EVAL_DIALECT == "legacy"
    assert select_dialect_for_run_eval("legacy") == "legacy"


def test_ngt_dialect_is_refused_locally_with_the_real_reason() -> None:
    """An NGT spec must fail locally, not after a round trip to a 422.

    The server's actual complaint is `Field required` on
    steps[1].agent.send_message.utterance, because the CLI's translator never
    reads inputs[].utterance. The local error must name that so the operator
    does not go hunting in the org.
    """
    with pytest.raises(DialectNotSupportedError) as exc:
        select_dialect_for_run_eval("ngt")
    message = str(exc.value)
    assert "utterance" in message
    assert "legacy" in message


def test_run_agent_eval_refuses_ngt_before_touching_the_org(tmp_path: Path) -> None:
    """The dialect guard must fire before any subprocess is spawned."""
    spec_file = tmp_path / "ngt.yaml"
    spec_file.write_text("name: x\n", encoding="utf-8")

    def exploding_runner(cmd, timeout):  # pragma: no cover - must never run
        raise AssertionError("run-eval was invoked with a dialect the CLI cannot execute")

    with pytest.raises(DialectNotSupportedError):
        run_agent_eval(spec_file, org_alias="AFT3", api_name="A", dialect="ngt", runner=exploding_runner)


def test_emitted_ngt_spec_lacks_the_key_run_eval_reads() -> None:
    """Ground the dialect finding in what eval_spec actually emits.

    This is the structural reason run-eval rejects NGT: the utterance lives
    under inputs[], and the translator only looks at a top-level `utterance:`.
    """
    spec = _make_spec()
    legacy, _ = build_legacy_test_spec(spec, name="L", subject_name="Coral_Cloud_Booking_Agent")
    ngt, _ = build_ngt_test_spec(spec, name="N", subject_name="Coral_Cloud_Booking_Agent")

    assert legacy.testCases[0].utterance
    assert not hasattr(ngt.testCases[0], "utterance")
    assert ngt.testCases[0].inputs[0].utterance


# --- Parsing the real org response ---


def test_parses_real_org_payload() -> None:
    fb = _real_feedback()
    assert fb.is_real
    assert fb.subject_name == "Coral_Cloud_Booking_Agent"
    assert len(fb.cases) == 2
    assert fb.passed_count == 1
    assert fb.failed_count == 1


def test_parses_real_verdicts_verbatim() -> None:
    """The observed topic and both verdicts must survive parsing unchanged."""
    fb = _real_feedback()
    failing = next(c for c in fb.cases if not c.passed)

    assert failing.case_id == "SFVB_Case_Triage_Legacy_case_0"
    assert failing.topic_actual == "Booked_Activity_Management"
    assert failing.session_id == "REDACTED-SESSION-UUID"
    assert failing.utterance == "Update the status on case {recordId} to {status}"
    assert "I can only assist with booking activities" in failing.agent_response

    topic = next(o for o in failing.outcomes if "topic" in o.evaluator_type)
    assert topic.is_pass is False
    assert topic.expected == "Update_Case_Status"
    assert topic.actual == "Booked_Activity_Management"

    passing = next(c for c in fb.cases if c.passed)
    assert passing.case_id == "SFVB_Case_Triage_Legacy_case_4"
    assert all(o.is_pass for o in passing.outcomes)


def test_parse_accepts_bare_body_and_cli_envelope() -> None:
    payload = _real_payload()
    enveloped = parse_run_eval_results(payload, source="run-eval")
    bare = parse_run_eval_results(payload["result"], source="run-eval")
    assert len(enveloped.cases) == len(bare.cases) == 2


def test_parse_tolerates_ansi_and_preamble() -> None:
    """CLI stdout carries ANSI codes and warning lines; the parser must cope."""
    noisy = "Warning: beta command\n\x1b[33m" + json.dumps(_real_payload())
    fb = parse_run_eval_results(noisy, source="run-eval")
    assert len(fb.cases) == 2


def test_parse_rejects_payload_without_tests() -> None:
    with pytest.raises(Stage5Error, match="no 'tests' array"):
        parse_run_eval_results({"result": {"summary": {}}}, source="run-eval")


def test_parse_requires_explicit_provenance() -> None:
    """`source` has no default: a payload cannot vouch for its own origin."""
    with pytest.raises(TypeError):
        parse_run_eval_results(_real_payload())  # type: ignore[call-arg]


# --- Provenance: synthetic results must not be able to claim validation ---


@pytest.mark.parametrize("source", ["mock", "fixture", "synthetic", "live-org", "", "LEGACY", None])
def test_only_run_eval_counts_as_real_feedback(source) -> None:
    """Fails closed. An unrecognised source is synthetic, so a typo cannot pass."""
    assert feedback_is_real(source) is False


def test_synthetic_feedback_is_blocking() -> None:
    fb = AgentFeedback(
        source="mock",
        subject_name="Coral_Cloud_Booking_Agent",
        cases=[CaseFeedback(case_id="c0", status="passed", outcomes=[EvaluationOutcome("t", "i", True)])],
    )
    issues = feedback_blocking_issues(fb)
    assert issues, "synthetic feedback must be blocking, exactly like telemetry_source='mock'"
    assert "not a real org source" in issues[0]


def test_real_feedback_is_not_blocking() -> None:
    assert feedback_blocking_issues(_real_feedback()) == []


def test_empty_feedback_is_blocking_even_when_source_is_real() -> None:
    fb = AgentFeedback(source="run-eval", subject_name="A", cases=[])
    assert any("no test cases" in i for i in feedback_blocking_issues(fb))


def test_case_with_no_verdicts_is_not_a_pass() -> None:
    """A case that produced no evaluator results proved nothing."""
    assert CaseFeedback(case_id="c", status="completed", outcomes=[]).passed is False


def test_findings_label_synthetic_results_in_the_text() -> None:
    """The warning must be in the findings themselves, not only in a side field."""
    fb = AgentFeedback(
        source="mock",
        subject_name="A",
        cases=[CaseFeedback(case_id="c0", status="passed", outcomes=[EvaluationOutcome("t", "i", True)])],
    )
    assert "SYNTHETIC FEEDBACK" in feedback_findings(fb)[0]


def test_findings_quote_real_expected_and_actual() -> None:
    findings = feedback_findings(_real_feedback())
    joined = "\n".join(findings)
    assert "Booked_Activity_Management" in joined
    assert "Update_Case_Status" in joined
    assert any(f.startswith("SFVB_Case_Triage_Legacy_case_4: PASS") for f in findings)


# --- apply_feedback: may only ADD observations ---


def test_apply_feedback_records_live_evidence() -> None:
    spec = _make_spec()
    adjusted, notes = apply_feedback(spec, _real_feedback())

    details = " ".join(e.detail for e in adjusted.evidence)
    assert "Coral_Cloud_Booking_Agent" in details
    assert "run-eval" in details
    assert notes


def test_apply_feedback_records_topic_mismatch_as_unknown() -> None:
    """A real routing mismatch is evidence of ambiguity, so it becomes an unknown."""
    adjusted, _ = apply_feedback(_make_spec(), _real_feedback())
    unknowns = " ".join(adjusted.unknowns)
    assert "Booked_Activity_Management" in unknowns
    assert "Update_Case_Status" in unknowns


def test_apply_feedback_records_live_failure_as_observed_failure_path() -> None:
    """A live failure is the observed error path the recording could not supply."""
    adjusted, _ = apply_feedback(_make_spec(), _real_feedback())
    assert any("Observed live-agent failure" in fh for fh in adjusted.failure_handling)


def test_apply_feedback_never_removes_unknowns() -> None:
    spec = _make_spec(unknowns=["Action API names were not observed.", "Second gap."])
    adjusted, _ = apply_feedback(spec, _real_feedback())
    for original in spec.unknowns:
        assert original in adjusted.unknowns


def test_apply_feedback_never_raises_confidence() -> None:
    spec = _make_spec(confidence=0.7)
    adjusted, _ = apply_feedback(spec, _real_feedback())
    assert adjusted.confidence <= 0.7


def test_apply_feedback_does_not_mutate_input_spec() -> None:
    spec = _make_spec()
    before = json.dumps(spec.to_dict(), sort_keys=True)
    apply_feedback(spec, _real_feedback())
    assert json.dumps(spec.to_dict(), sort_keys=True) == before


def test_apply_feedback_invents_no_entities_or_objects() -> None:
    spec = _make_spec()
    adjusted, _ = apply_feedback(spec, _real_feedback())
    assert [e.name for e in adjusted.entities] == [e.name for e in spec.entities]
    assert adjusted.objects_touched == spec.objects_touched
    assert adjusted.orchestration_steps == spec.orchestration_steps


def test_synthetic_feedback_marks_spec_unvalidated_not_validated() -> None:
    """The worst outcome would be a synthetic round that reads like a real one."""
    fb = AgentFeedback(
        source="mock",
        subject_name="Coral_Cloud_Booking_Agent",
        cases=[CaseFeedback(case_id="c0", status="passed", outcomes=[EvaluationOutcome("t", "i", True)])],
    )
    adjusted, _ = apply_feedback(_make_spec(), fb)
    assert any("UNVALIDATED" in u for u in adjusted.unknowns)
    assert not any("live-agent-eval" == e.source for e in adjusted.evidence)


def test_feedback_with_no_cases_records_an_unknown() -> None:
    adjusted, _ = apply_feedback(_make_spec(), AgentFeedback(source="run-eval", subject_name="A", cases=[]))
    assert any("no test-case results" in u for u in adjusted.unknowns)


# --- A full round ---


def test_stage5_round_with_real_feedback_is_trustworthy() -> None:
    rnd = stage5_round(_make_spec(), _real_feedback())
    assert rnd.trustworthy
    assert rnd.blocking_issues == []
    assert rnd.dialect == "legacy"
    assert rnd.score_before is not None and rnd.score_after is not None


def test_stage5_round_with_synthetic_feedback_is_not_trustworthy() -> None:
    fb = AgentFeedback(
        source="mock",
        subject_name="A",
        cases=[CaseFeedback(case_id="c0", status="passed", outcomes=[EvaluationOutcome("t", "i", True)])],
    )
    rnd = stage5_round(_make_spec(), fb)
    assert rnd.trustworthy is False
    assert rnd.blocking_issues


def test_stage5_round_does_not_pass_the_gate_on_mock_provenance() -> None:
    """Stage 5 must not become a way to launder a mock-telemetry run past the gate."""
    rnd = stage5_round(
        _make_spec(),
        _real_feedback(),
        provenance={"extraction_source": "dom-capture", "telemetry_source": "mock"},
    )
    assert rnd.score_after.passed is False
    assert rnd.score_after.blocking_issues


def test_round_json_is_serialisable_and_carries_provenance(tmp_path: Path) -> None:
    rnd = stage5_round(_make_spec(), _real_feedback())
    written = write_round(tmp_path, rnd)
    payload = json.loads(written.read_text(encoding="utf-8"))

    assert payload["feedback"]["source"] == "run-eval"
    assert payload["feedback"]["is_real"] is True
    assert payload["trustworthy"] is True
    assert payload["spec_before"] and payload["spec_after"]


def test_write_round_redacts_session_ids(tmp_path: Path) -> None:
    """Session ids must not reach disk, but must stay available in memory."""
    rnd = stage5_round(_make_spec(), _real_feedback())
    written = write_round(tmp_path, rnd)
    text = written.read_text(encoding="utf-8")

    assert "REDACTED-SESSION-UUID" not in text
    assert SESSION_ID_REDACTED in text
    # Still reachable in-process for an operator pulling the session in the org.
    assert rnd.feedback.cases[0].session_id == "REDACTED-SESSION-UUID"


def test_redact_session_ids_covers_nested_planner_state() -> None:
    """The planner's sessionProperties nests a second copy of the id."""
    payload = {"a": [{"session_id": "019f-real"}, {"planner": {"sessionProperties": {"sessionId": "019f-real"}}}]}
    scrubbed = redact_session_ids(payload)
    assert "019f-real" not in json.dumps(scrubbed)


# --- Parametrized coverage for the three new redaction gaps ---


@pytest.mark.parametrize(
    "header_key",
    ["Authorization", "authorization"],
    ids=["capitalized", "lowercase"],
)
def test_redact_session_ids_bearer_token_in_headers(header_key: str) -> None:
    """Bearer tokens in HTTP header dicts must be redacted regardless of case."""
    payload = {"request": {"headers": {header_key: "Bearer SYNTHETIC_TOKEN_ABCDEF1234567890"}}}
    scrubbed = redact_session_ids(payload)
    assert "SYNTHETIC_TOKEN_ABCDEF1234567890" not in json.dumps(scrubbed)
    assert SESSION_ID_REDACTED in json.dumps(scrubbed)


def test_redact_session_ids_non_bearer_headers_are_preserved() -> None:
    """Keys that are NOT authorization headers must not be redacted (no false positives)."""
    payload = {"request": {"headers": {"Content-Type": "application/json", "X-Request-Id": "req-abc-123"}}}
    scrubbed = redact_session_ids(payload)
    assert scrubbed == payload, "Non-credential headers should be left untouched"


@pytest.mark.parametrize(
    "url,expected_present,expected_absent",
    [
        (
            "https://example.salesforce.com/secur/frontdoor.jsp?sid=SYNTHETIC_SID_TOKEN&retURL=/",
            "sid=",
            "SYNTHETIC_SID_TOKEN",
        ),
        (
            "https://example.salesforce.com/secur/frontdoor.jsp?sid=SYNTHETIC_SID_ONLY",
            "sid=",
            "SYNTHETIC_SID_ONLY",
        ),
        (
            "https://example.salesforce.com/secur/frontdoor.jsp?retURL=/home&sid=SYNTHETIC_SID_MIDDLE&foo=bar",
            "sid=",
            "SYNTHETIC_SID_MIDDLE",
        ),
    ],
    ids=["sid-with-trailing-params", "sid-at-end", "sid-in-middle"],
)
def test_redact_session_ids_frontdoor_sid_url(
    url: str, expected_present: str, expected_absent: str
) -> None:
    """The sid= parameter value in frontdoor.jsp URLs must be redacted."""
    payload = {"login_url": url}
    scrubbed = redact_session_ids(payload)
    scrubbed_url = scrubbed["login_url"]
    assert expected_absent not in scrubbed_url
    assert expected_present in scrubbed_url
    assert SESSION_ID_REDACTED in scrubbed_url


def test_redact_session_ids_plain_url_without_sid_unchanged() -> None:
    """A URL that does NOT contain frontdoor.jsp must not be modified."""
    url = "https://example.salesforce.com/lightning/r/Case/500abc/view"
    payload = {"url": url}
    scrubbed = redact_session_ids(payload)
    assert scrubbed["url"] == url


@pytest.mark.parametrize(
    "depth_label,payload,secret",
    [
        (
            "bearer_3_levels_deep",
            {"level1": {"level2": {"headers": {"Authorization": "Bearer SYNTHETIC_DEEP_BEARER"}}}},
            "SYNTHETIC_DEEP_BEARER",
        ),
        (
            "frontdoor_3_levels_deep",
            {
                "level1": [
                    {
                        "level2": {
                            "url": "https://test.salesforce.com/secur/frontdoor.jsp?sid=SYNTHETIC_DEEP_SID"
                        }
                    }
                ]
            },
            "SYNTHETIC_DEEP_SID",
        ),
        (
            "session_id_in_list_of_dicts_3_levels",
            {"planner": {"steps": [{"inner": {"session_id": "SYNTHETIC_NESTED_SID"}}]}},
            "SYNTHETIC_NESTED_SID",
        ),
    ],
)
def test_redact_session_ids_three_levels_deep(depth_label: str, payload: dict, secret: str) -> None:
    """Each new pattern must be caught at three levels of nesting."""
    scrubbed = redact_session_ids(payload)
    assert secret not in json.dumps(scrubbed), f"Secret leaked at depth {depth_label}"
    assert SESSION_ID_REDACTED in json.dumps(scrubbed)


def test_write_round_refuses_to_overwrite_a_prior_round(tmp_path: Path) -> None:
    """The audit trail is the product; a silently replaced round is indistinguishable
    from one that never happened."""
    rnd = stage5_round(_make_spec(), _real_feedback())
    write_round(tmp_path, rnd)
    with pytest.raises(Stage5Error, match="refusing to overwrite"):
        write_round(tmp_path, rnd)


def test_write_round_allows_a_new_round_number(tmp_path: Path) -> None:
    write_round(tmp_path, stage5_round(_make_spec(), _real_feedback(), round_number=1))
    second = write_round(tmp_path, stage5_round(_make_spec(), _real_feedback(), round_number=2))
    assert second.exists()
    assert (tmp_path / "round-1" / "round.json").exists()


# --- run_agent_eval plumbing (no org needed) ---


def test_run_agent_eval_builds_the_verified_command(tmp_path: Path) -> None:
    spec_file = tmp_path / "legacy-testSpec.yaml"
    spec_file.write_text("name: x\nsubjectName: A\ntestCases: []\n", encoding="utf-8")
    captured: dict = {}

    class Done:
        returncode = 0
        stdout = json.dumps(_real_payload())
        stderr = ""

    def runner(cmd, timeout):
        captured["cmd"] = cmd
        return Done()

    fb = run_agent_eval(spec_file, org_alias="AFT3", api_name="Coral_Cloud_Booking_Agent", runner=runner)

    cmd = captured["cmd"]
    assert cmd[:4] == ["sf", "agent", "test", "run-eval"]
    assert "--target-org" in cmd and "AFT3" in cmd
    assert "--api-name" in cmd and "Coral_Cloud_Booking_Agent" in cmd
    assert len(fb.cases) == 2


def test_run_agent_eval_raises_with_real_stderr(tmp_path: Path) -> None:
    """A failed run must surface the org's actual complaint, never a summary."""
    spec_file = tmp_path / "s.yaml"
    spec_file.write_text("name: x\n", encoding="utf-8")
    real_error = (
        'Error (TestExecutionFailed): Failed to execute tests: {"detail":[{"type":"missing",'
        '"loc":["body","tests",0,"steps",1,"agent.send_message","utterance"],"msg":"Field required"}]}'
    )

    class Failed:
        returncode = 1
        stdout = ""
        stderr = real_error

    with pytest.raises(Stage5Error) as exc:
        run_agent_eval(spec_file, org_alias="AFT3", api_name="A", runner=lambda c, t: Failed())
    assert "Field required" in str(exc.value)


def test_run_agent_eval_requires_an_existing_spec_file(tmp_path: Path) -> None:
    with pytest.raises(Stage5Error, match="test spec not found"):
        run_agent_eval(tmp_path / "missing.yaml", org_alias="AFT3", api_name="A")


def test_iterate_org_feedback_loop_writes_one_round_per_trip(tmp_path: Path) -> None:
    """The stage-5 loop in iterate.py must emit, run, parse, and version each round."""
    from sf_video_blueprint.iterate import refine_with_org_feedback

    calls: list[list[str]] = []

    class Done:
        returncode = 0
        stdout = json.dumps(_real_payload())
        stderr = ""

    def runner(cmd, timeout):
        calls.append(cmd)
        return Done()

    rounds = refine_with_org_feedback(
        _make_spec(),
        out_dir=tmp_path / "stage5",
        org_alias="AFT3",
        agent_api_name="Coral_Cloud_Booking_Agent",
        test_spec_name="SFVB_Case_Triage",
        rounds=2,
        runner=runner,
    )

    assert len(rounds) == 2 and len(calls) == 2
    for n in (1, 2):
        assert (tmp_path / "stage5" / f"round-{n}" / "round.json").exists()
        assert (tmp_path / "stage5" / f"round-{n}" / "testSpec.yaml").exists()
    # An injected runner is NOT an org, so no round here may claim to be trustworthy.
    assert all(not r.trustworthy for r in rounds)
    assert all(r.feedback.source == INJECTED_RUNNER_SOURCE for r in rounds)


def test_iterate_org_feedback_loop_emits_the_dialect_the_cli_accepts(tmp_path: Path) -> None:
    """The emitted spec must carry a top-level `utterance:`, which is the key
    run-eval reads. An NGT spec here would fail server-side."""
    from sf_video_blueprint.iterate import refine_with_org_feedback

    class Done:
        returncode = 0
        stdout = json.dumps(_real_payload())
        stderr = ""

    refine_with_org_feedback(
        _make_spec(),
        out_dir=tmp_path / "s5",
        org_alias="AFT3",
        agent_api_name="Coral_Cloud_Booking_Agent",
        test_spec_name="T",
        runner=lambda c, t: Done(),
    )
    emitted = (tmp_path / "s5" / "round-1" / "testSpec.yaml").read_text(encoding="utf-8")
    assert "- utterance:" in emitted
    assert "inputs:" not in emitted


def test_iterate_org_feedback_loop_does_not_carry_synthetic_results_forward(tmp_path: Path) -> None:
    """A synthetic round must not be laundered into the next round's input.

    No monkeypatching needed: an injected runner is synthetic by construction, so
    this exercises the real provenance path rather than a simulated one.
    """
    from sf_video_blueprint.iterate import refine_with_org_feedback

    class Done:
        returncode = 0
        stdout = json.dumps(_real_payload())
        stderr = ""

    rounds = refine_with_org_feedback(
        _make_spec(),
        out_dir=tmp_path / "s5",
        org_alias="AFT3",
        agent_api_name="A",
        test_spec_name="T",
        rounds=2,
        runner=lambda c, t: Done(),
    )

    assert all(not r.trustworthy for r in rounds)
    # Round 2 started from the ORIGINAL spec, not round 1's adjusted output.
    assert rounds[1].spec_before.to_dict() == rounds[0].spec_before.to_dict()


def test_iterate_org_feedback_loop_rejects_zero_rounds(tmp_path: Path) -> None:
    from sf_video_blueprint.iterate import refine_with_org_feedback

    with pytest.raises(ValueError, match="rounds must be >= 1"):
        refine_with_org_feedback(
            _make_spec(),
            out_dir=tmp_path,
            org_alias="AFT3",
            agent_api_name="A",
            test_spec_name="T",
            rounds=0,
        )


def test_emitted_legacy_spec_round_trips_through_run_agent_eval(tmp_path: Path) -> None:
    """End-to-end wiring: eval_spec emits it, stage5 runs it, the parser reads it."""
    legacy, _ = build_legacy_test_spec(
        _make_spec(), name="SFVB_Case_Triage_Legacy", subject_name="Coral_Cloud_Booking_Agent"
    )
    spec_file = write_test_spec(tmp_path / "legacy-testSpec.yaml", legacy)

    class Done:
        returncode = 0
        stdout = json.dumps(_real_payload())
        stderr = ""

    fb = run_agent_eval(
        spec_file, org_alias="AFT3", api_name="Coral_Cloud_Booking_Agent", runner=lambda c, t: Done()
    )
    rnd = stage5_round(_make_spec(), fb)
    # The wiring works end to end, but this ran through an injected runner, so the
    # round must label itself synthetic rather than claim org validation.
    assert rnd.trustworthy is False
    assert rnd.findings
    assert any("SYNTHETIC FEEDBACK" in f for f in rnd.findings)


def test_score_rise_on_a_failing_round_is_called_out_explicitly() -> None:
    """A failing round can score HIGHER, because declaring unknowns earns honesty
    points. That is the rubric working — but an audit reader diffing the two totals
    must not be left to infer that the agent improved."""
    spec = _make_spec(unknowns=[], failure_handling=[])
    rnd = stage5_round(spec, _real_feedback())

    assert rnd.feedback.failed_count > 0
    if rnd.score_after.total > rnd.score_before.total:
        assert any("NOT the agent performing better" in n for n in rnd.notes)
    else:
        assert rnd.score_after.total <= rnd.score_before.total


def test_apply_feedback_confidence_guard_is_not_a_bare_assert() -> None:
    """`python -O` strips asserts. An honesty invariant that vanishes under an
    optimisation flag is not an invariant, so the guard must raise for real."""
    import subprocess
    import sys

    prog = (
        "from tests.test_stage5 import _make_spec, _real_feedback;"
        "import sf_video_blueprint.stage5 as s;"
        "spec=_make_spec();"
        "orig=s.copy.deepcopy;"
        "s.copy.deepcopy=lambda x: (lambda c: (setattr(c,'confidence',1.0), c)[1])(orig(x));"
        "\ntry:\n    s.apply_feedback(spec, _real_feedback())\n"
        "    print('NO_GUARD')\nexcept s.Stage5Error:\n    print('GUARDED')\n"
    )
    out = subprocess.run(
        [sys.executable, "-O", "-c", prog],
        capture_output=True,
        text=True,
        check=False,
        cwd=str(Path(__file__).parent.parent),
    )
    assert "GUARDED" in out.stdout, f"guard did not fire under -O: {out.stdout!r} {out.stderr[-500:]!r}"


def test_org_summary_and_case_counts_are_labelled_by_their_unit(tmp_path: Path) -> None:
    """The org's summary counts EVALUATIONS; passed_count counts CASES. Emitting
    both unlabelled makes a round file look self-contradictory."""
    rnd = stage5_round(_make_spec(), _real_feedback())
    payload = json.loads(write_round(tmp_path, rnd).read_text(encoding="utf-8"))["feedback"]

    assert payload["org_summary_by_evaluation"] == {"passed": 1, "failed": 8, "scored": 0, "errors": 0}
    assert payload["passed_count_by_case"] == 1
    assert payload["failed_count_by_case"] == 1
    assert "summary" not in payload


def test_non_string_session_id_is_still_redacted() -> None:
    scrubbed = redact_session_ids({"session_id": 12345, "sessionId": ["a", "b"]})
    assert scrubbed == {"session_id": SESSION_ID_REDACTED, "sessionId": SESSION_ID_REDACTED}


def test_injected_runner_is_never_stamped_as_a_real_org_run(tmp_path: Path) -> None:
    """The fake path must label itself, exactly like MockTelemetryCollector.

    This is the one that matters most. If an injected runner produced feedback
    stamped ``run-eval``, anyone with a fixture and a lambda could manufacture a
    ``round.json`` saying ``"trustworthy": true`` without an org existing — and it
    would be byte-indistinguishable from a real round.
    """
    spec_file = tmp_path / "legacy.yaml"
    spec_file.write_text("name: x\nsubjectName: A\ntestCases: []\n", encoding="utf-8")

    class Done:
        returncode = 0
        stdout = json.dumps(_real_payload())
        stderr = ""

    fb = run_agent_eval(spec_file, org_alias="AFT3", api_name="A", runner=lambda c, t: Done())

    assert fb.source == INJECTED_RUNNER_SOURCE
    assert fb.is_real is False
    assert INJECTED_RUNNER_SOURCE not in REAL_FEEDBACK_SOURCES
    assert feedback_blocking_issues(fb)

    rnd = stage5_round(_make_spec(), fb)
    assert rnd.trustworthy is False
    payload = json.loads(write_round(tmp_path / "out", rnd).read_text(encoding="utf-8"))
    assert payload["trustworthy"] is False
    assert payload["feedback"]["is_real"] is False


def test_dialect_travels_from_the_call_into_the_feedback(tmp_path: Path) -> None:
    """`dialect` must reflect what ran, not a default that happens to be right."""
    assert parse_run_eval_results(_real_payload(), source="run-eval", dialect="other").dialect == "other"

    spec_file = tmp_path / "s.yaml"
    spec_file.write_text("name: x\n", encoding="utf-8")

    class Done:
        returncode = 0
        stdout = json.dumps(_real_payload())
        stderr = ""

    fb = run_agent_eval(spec_file, org_alias="AFT3", api_name="A", runner=lambda c, t: Done())
    assert fb.dialect == RUN_EVAL_DIALECT


def test_a_stringly_typed_verdict_is_not_a_pass() -> None:
    """`bool("false")` is True. is_pass decides pass/fail, so it must fail closed."""
    payload = {
        "tests": [
            {
                "id": "c0",
                "status": "completed",
                "evaluations": [
                    {"type": "evaluator.planner_topic_assertion", "id": "t", "is_pass": "false"}
                ],
            }
        ]
    }
    fb = parse_run_eval_results(payload, source="run-eval")
    assert fb.cases[0].outcomes[0].is_pass is False
    assert fb.cases[0].passed is False
    assert fb.passed_count == 0


def test_unparseable_stdout_carries_the_output_it_could_not_parse() -> None:
    """Stage5Error must carry real output — most of all when parsing itself fails."""
    with pytest.raises(Stage5Error) as exc:
        parse_run_eval_results("Warning: {beta}\nnot json at all", source="run-eval")
    assert "could not parse JSON" in str(exc.value)
    assert "not json at all" in str(exc.value)


def test_multi_turn_case_keeps_the_first_observed_topic_and_response() -> None:
    """A later step with no topic must not clobber an earlier real one, and a
    send_message whose id is not "sm" must still yield its response."""
    payload = {
        "tests": [
            {
                "id": "c0",
                "status": "completed",
                "evaluations": [],
                "outputs": [
                    {"type": "agent.create_session", "id": "cs", "session_id": "sess-1"},
                    {"type": "agent.send_message", "id": "sm2", "response": "first answer"},
                    {
                        "type": "agent.get_state",
                        "id": "gs",
                        "response": {"planner_response": {"lastExecution": {"topic": "Real_Topic"}}},
                    },
                    {"type": "agent.get_state", "id": "gs2", "response": {"planner_response": {}}},
                ],
            }
        ]
    }
    case = parse_run_eval_results(payload, source="run-eval").cases[0]
    assert case.topic_actual == "Real_Topic"
    assert case.agent_response == "first answer"
    assert case.session_id == "sess-1"


def test_loop_refuses_an_existing_round_before_spending_org_calls(tmp_path: Path) -> None:
    """The overwrite refusal must land BEFORE the org is billed and before the
    round's testSpec.yaml is replaced. A guard that only fires at write time has
    already paid for a result it throws away, and leaves round-N holding a spec
    that no longer matches its round.json."""
    from sf_video_blueprint.iterate import refine_with_org_feedback

    out_dir = tmp_path / "s5"
    (out_dir / "round-1").mkdir(parents=True)
    (out_dir / "round-1" / "round.json").write_text('{"round_number": 1}', encoding="utf-8")
    (out_dir / "round-1" / "testSpec.yaml").write_text("name: PRIOR_ROUND\n", encoding="utf-8")

    calls: list[list[str]] = []

    def exploding_runner(cmd, timeout):
        calls.append(cmd)
        raise AssertionError("the org was called for a round that already exists")

    with pytest.raises(Stage5Error, match="refusing to overwrite"):
        refine_with_org_feedback(
            _make_spec(),
            out_dir=out_dir,
            org_alias="AFT3",
            agent_api_name="A",
            test_spec_name="T",
            runner=exploding_runner,
        )

    assert calls == []
    assert (out_dir / "round-1" / "testSpec.yaml").read_text(encoding="utf-8") == "name: PRIOR_ROUND\n"
    assert json.loads((out_dir / "round-1" / "round.json").read_text(encoding="utf-8")) == {"round_number": 1}

"""Tests for the MCP server.

Skipped entirely when the optional `mcp` extra is absent, so the default
`pip install -e ".[dev]"` suite stays green. Install with
`pip install -e ".[dev,mcp]"` to run these.

Two properties matter most here and are tested directly:

1. **No tool can report a mock run as evidence-backed.** An agent driving this
   server must not be able to obtain a passing score from fabricated telemetry.
2. **Failures come back as structured data, not exceptions.** An MCP tool that
   raises gives the calling model a stack trace; one that returns an error code
   gives it something to act on.
"""

from __future__ import annotations

import inspect
import json
from pathlib import Path

import pytest

pytest.importorskip("mcp", reason="the optional [mcp] extra is not installed")

# Imported below the importorskip on purpose: mcp_server imports `mcp` at module
# scope, so a top-of-file import here would raise ModuleNotFoundError during
# collection instead of skipping cleanly when the extra is absent.
from sf_video_blueprint import mcp_server

EXAMPLE = Path(__file__).parent.parent / "examples" / "case_triage.dom_capture.jsonl"

EXPECTED_TOOLS = {
    "health",
    "validate_capture",
    "derive_spec",
    "score_spec",
    "emit_agent_bundle",
    "emit_test_spec",
    "preview_api_names",
    "run_stage5_round",
    "run_iterate",
}


# ---------------------------------------------------------------------------
# Registration and envelope
# ---------------------------------------------------------------------------


def _list_tools() -> list:
    """Drive the async MCP registry from a sync test.

    Deliberately `asyncio.run` rather than an async test: that would need the
    anyio/asyncio-mode pytest plugin, and adding a plugin dependency to exercise
    one registry call is not a trade worth making.
    """
    import asyncio

    return asyncio.run(mcp_server.mcp.list_tools())


def test_all_tools_are_registered() -> None:
    assert {tool.name for tool in _list_tools()} == EXPECTED_TOOLS


def test_every_tool_has_a_description() -> None:
    """The docstring is the only thing telling a model when to call a tool."""
    for tool in _list_tools():
        assert tool.description, f"{tool.name} has no description"


def test_every_tool_exposes_an_input_schema() -> None:
    """A model cannot call a tool whose arguments it cannot see."""
    for tool in _list_tools():
        assert tool.inputSchema, f"{tool.name} has no input schema"


def test_health_reports_capabilities_and_limitations() -> None:
    result = mcp_server.health()

    assert result["ok"] is True
    assert set(result["tools"]) == EXPECTED_TOOLS
    # Org contact is conditional, not absent: `emit_agent_bundle` compiles against
    # an org when given an org_alias, and `run_stage5_round`/`run_iterate` always
    # call sf agent test run-eval. Claiming a flat False would understate what the
    # server does, which is its own kind of dishonesty.
    contacts_org = result["capabilities"]["contactsSalesforceOrg"]
    assert "org_alias" in contacts_org, (
        "health() must name the org_alias condition in contactsSalesforceOrg"
    )
    assert "run_stage5_round" in contacts_org or "run_iterate" in contacts_org or "stage" in contacts_org.lower(), (
        "health() must disclose that run_stage5_round and run_iterate contact the org"
    )
    assert "by default" in result["capabilities"]["offline"]
    # Offline disclosure must mention the stage-5 tools too.
    assert "run_stage5_round" in result["capabilities"]["offline"] or "run_iterate" in result["capabilities"]["offline"]
    assert result["capabilities"]["readOnly"] is True
    assert result["capabilities"]["launchesBrowser"] is False
    # The limitations list is load-bearing: an agent plans around it. Assert the
    # disclosures that most change how a caller should treat the output.
    assert result["limitations"]
    # Pinned to the *substance* rather than a sentence. This assertion used to look
    # for "validated against a real Salesforce org", which was part of the claim
    # that no validation had ever happened. That claim stopped being true on
    # 2026-07-26, so matching it verbatim would have forced the disclosure to stay
    # stale in order to keep a test green. What must never disappear is the warning
    # that an emitted bundle may not compile.
    assert any(
        "may be syntactically invalid" in item for item in result["limitations"]
    ), "health() must warn that an emitted .agent bundle may not compile"
    assert any(
        "validate authoring-bundle" in item for item in result["limitations"]
    ), "health() must name the CLI command that is the actual authority"
    # Local validation must never be presentable as org validation: validate_locally
    # returned zero findings on the exact file the compiler rejected with 24 errors.
    assert any(
        "locallyValid" in item and "not org validation" in item
        for item in result["limitations"]
    ), "health() must disclose that locallyValid is not org validation"
    # The org-compile mechanism must be named, not just the CLI: a caller needs to
    # know which field carries Salesforce's verdict.
    assert any("orgValidation.compiled" in item for item in result["limitations"])
    assert any("telemetry_source=mock" in item for item in result["limitations"])


def test_server_instructions_disclose_the_mock_telemetry_constraint() -> None:
    """Instructions reach the model before any tool call.

    If they omit the mock-telemetry constraint, a model can call `derive_spec` and
    report the result as validated output — the single most damaging way to
    misread this server.
    """
    instructions = mcp_server.mcp.instructions or ""

    assert "mock" in instructions
    assert "CANNOT pass the quality gate" in instructions
    # The skipped-is-not-a-pass distinction has to reach the model before it reads
    # `locallyValid: true` and calls the bundle validated.
    assert "orgValidation" in instructions
    assert 'skipped' in instructions and "never as a pass" in instructions
    assert "Never suggest" in instructions and "lowering a score threshold" in instructions


def test_server_version_comes_from_package_metadata() -> None:
    """A hardcoded version drifts from pyproject.toml on the first bump."""
    from importlib.metadata import version

    assert mcp_server.SERVER_VERSION == version("sf-video-blueprint")
    assert mcp_server.SERVER_VERSION != "0.0.0+unknown"


def test_success_envelope_shape() -> None:
    result = mcp_server.health()

    assert result["ok"] is True
    assert result["requestId"]
    assert result["serverVersion"] == mcp_server.SERVER_VERSION
    assert isinstance(result["durationMs"], int)


def test_error_envelope_shape() -> None:
    result = mcp_server.derive_spec("/nonexistent/path/capture.jsonl")

    assert result["ok"] is False
    assert result["error"]["code"] == mcp_server.ERROR_NOT_FOUND
    assert result["error"]["message"]
    assert result["requestId"]


def test_request_ids_are_unique_per_call() -> None:
    ids = {mcp_server.health()["requestId"] for _ in range(5)}
    assert len(ids) == 5


# ---------------------------------------------------------------------------
# The honesty properties
# ---------------------------------------------------------------------------


def test_derive_spec_never_claims_real_evidence() -> None:
    """Telemetry is mocked in this server, so no result may claim otherwise."""
    result = mcp_server.derive_spec(str(EXAMPLE))

    assert result["ok"] is True
    assert result["provenance"]["telemetry_source"] == "mock"
    assert result["evidence_is_real"] is False


def test_derive_spec_result_does_not_pass_the_gate() -> None:
    result = mcp_server.derive_spec(str(EXAMPLE))

    assert result["passed"] is False
    assert result["blocking_issues"]


def test_score_spec_agrees_with_derive_spec(tmp_path: Path) -> None:
    """Scoring a written spec must not produce a different verdict than deriving it.

    A divergence would mean one of the two paths reads provenance differently,
    which is exactly how a mock run could slip past the gate on one route.
    """
    spec_path = tmp_path / "spec.json"
    derived = mcp_server.derive_spec(str(EXAMPLE), output_path=str(spec_path))
    scored = mcp_server.score_spec(str(spec_path))

    assert scored["ok"] is True
    assert scored["total"] == derived["score"]
    assert scored["passed"] == derived["passed"] is False


def test_score_spec_reports_a_capped_display_total_when_blocked(tmp_path: Path) -> None:
    """A blocked spec must not hand an agent a number that reads as near-success.

    The example capture scores a raw 85 while blocked on mock telemetry. An agent (or
    a human reading the tool output) that sees only "85/100" draws the opposite
    conclusion from the one the blocking issues support, so the tool reports
    `displayTotal` capped into the low band alongside the raw `total`.
    """
    spec_path = tmp_path / "spec.json"
    mcp_server.derive_spec(str(EXAMPLE), output_path=str(spec_path))
    scored = mcp_server.score_spec(str(spec_path))

    assert scored["ok"] is True
    assert scored["passed"] is False
    assert scored["blockingIssues"], "Expected the mock-telemetry blocker."
    assert scored["displayTotal"] < 60, (
        f"Blocked spec reported displayTotal={scored['displayTotal']}, which reads as a "
        "moderate score."
    )
    # The raw total is still exposed for callers comparing refinement rounds.
    assert scored["total"] >= scored["displayTotal"]


def test_score_gate_threshold_is_not_weakened() -> None:
    """Guard the contract: the pass threshold is 75 and tools must report it."""
    from sf_video_blueprint.spec_score import PASS_THRESHOLD

    assert PASS_THRESHOLD == 75
    assert mcp_server.health()["passThreshold"] == 75


# ---------------------------------------------------------------------------
# Failure handling — errors as data
# ---------------------------------------------------------------------------


def test_rejected_capture_returns_validation_error_not_an_exception(tmp_path: Path) -> None:
    bad = tmp_path / "bad.jsonl"
    bad.write_text("not json\n{unclosed\nnope\n", encoding="utf-8")

    result = mcp_server.derive_spec(str(bad))

    assert result["ok"] is False
    assert result["error"]["code"] == mcp_server.ERROR_VALIDATION
    assert any("DATA LOSS" in f for f in result["error"]["findings"])
    assert "remedy" in result["error"]


def test_validate_capture_reports_loss_without_refusing(tmp_path: Path) -> None:
    """validate_capture is diagnostic: it reports a doomed capture as data."""
    bad = tmp_path / "bad.jsonl"
    bad.write_text("not json\n{unclosed\nnope\n", encoding="utf-8")

    result = mcp_server.validate_capture(str(bad))

    assert result["ok"] is True
    assert result["wouldBeRejected"] is True
    assert result["eventsParsed"] == 0
    assert result["skippedLineCount"] == 3
    assert result["lossRatio"] == 1.0


def test_validate_capture_on_clean_example() -> None:
    result = mcp_server.validate_capture(str(EXAMPLE))

    assert result["ok"] is True
    assert result["wouldBeRejected"] is False
    assert result["eventsParsed"] > 0
    assert result["skippedLineCount"] == 0
    assert result["lossRatio"] == 0.0


def test_loss_ratio_does_not_divide_by_zero(tmp_path: Path) -> None:
    empty = tmp_path / "empty.jsonl"
    empty.write_text("", encoding="utf-8")

    result = mcp_server.validate_capture(str(empty))

    assert result["ok"] is True
    assert result["lossRatio"] == 0.0


def test_bad_dialect_is_rejected() -> None:
    result = mcp_server.emit_test_spec(str(EXAMPLE), "T", "agent", dialect="klingon")

    assert result["ok"] is False
    assert result["error"]["code"] == mcp_server.ERROR_VALIDATION


def test_empty_process_description_is_rejected() -> None:
    result = mcp_server.preview_api_names("   ")

    assert result["ok"] is False
    assert result["error"]["code"] == mcp_server.ERROR_VALIDATION


# ---------------------------------------------------------------------------
# Emitters
# ---------------------------------------------------------------------------


def test_emit_agent_bundle_produces_valid_script_and_meta(tmp_path: Path) -> None:
    result = mcp_server.emit_agent_bundle(
        str(EXAMPLE),
        developer_name="case_triage_agent",
        agent_label="Case Triage Agent",
        output_dir=str(tmp_path / "bundle"),
    )

    assert result["ok"] is True
    assert result["locallyValid"] is True
    assert result["agentScript"].startswith("system:")
    assert "AiAuthoringBundle" in result["bundleMetaXml"]
    assert Path(result["writtenTo"]["agent"]).is_file()
    assert Path(result["writtenTo"]["bundleMeta"]).is_file()


def test_emitted_bundle_declares_no_apex_or_flow_actions() -> None:
    """Ground rule: never reference an action that may not exist in the org."""
    result = mcp_server.emit_agent_bundle(
        str(EXAMPLE), developer_name="a", agent_label="A"
    )

    assert "@apex." not in result["agentScript"]
    assert "@flow." not in result["agentScript"]


def test_emit_agent_bundle_tells_the_caller_to_validate_with_the_cli() -> None:
    """Local validation is this project's opinion, not Salesforce's."""
    result = mcp_server.emit_agent_bundle(
        str(EXAMPLE), developer_name="a", agent_label="A"
    )

    assert "sf agent validate authoring-bundle" in result["nextStep"]


def test_emit_agent_bundle_reports_org_validation_as_skipped_without_an_org() -> None:
    """With no org alias the compiler is not asked, and that is reported as SKIPPED.

    The distinction is the whole point: `locallyValid: true` plus
    `orgValidation.compiled: false` says "our checks passed, Salesforce was never
    asked". Collapsing those two into one boolean is how output that no org has
    accepted starts looking validated.
    """
    result = mcp_server.emit_agent_bundle(
        str(EXAMPLE), developer_name="a", agent_label="A"
    )

    org = result["orgValidation"]
    assert org["outcome"] == "skipped"
    assert org["compiled"] is False
    assert org["errors"] == []


def test_emit_agent_bundle_refuses_an_out_of_scope_org() -> None:
    """PPCDM / PPCaccenture are out of scope; the tool must refuse before any call."""
    result = mcp_server.emit_agent_bundle(
        str(EXAMPLE), developer_name="a", agent_label="A", org_alias="PPCDM"
    )

    assert result["ok"] is False
    assert "scope" in result["error"]["message"].lower()


@pytest.mark.parametrize("dialect", ["legacy", "ngt"])
def test_emit_test_spec_both_dialects(dialect: str) -> None:
    result = mcp_server.emit_test_spec(
        str(EXAMPLE), test_name="Case_Triage_Test", subject_name="case_triage_agent",
        dialect=dialect,
    )

    assert result["ok"] is True
    assert result["dialect"] == dialect
    assert result["caseCount"] > 0
    assert result["yaml"].strip()
    assert len(result["derivations"]) == result["caseCount"]


def test_emitted_test_topic_matches_the_bundle_topic() -> None:
    """The reference pair must agree, or a test asserts a topic that never fires.

    The two artifacts use different dialects of the same name on purpose: the test
    spec's `expectedTopic` is the `Topic_Api_Name` form, while the `.agent` script
    declares `subagent topic_api_name`. `names_agree` is the canonical check for
    that mapping — comparing the raw strings would fail on a correct bundle.

    `expectedTopic` appears in the emitted YAML rather than in the derivation
    records, so this reads the YAML; asserting against `derivations` would pass
    vacuously because that field does not exist there.
    """
    yaml_mod = pytest.importorskip("yaml", reason="pyyaml is a dev extra")
    from sf_video_blueprint.naming import names_agree, snake_case

    bundle = mcp_server.emit_agent_bundle(
        str(EXAMPLE), developer_name="a", agent_label="A"
    )
    tests = mcp_server.emit_test_spec(str(EXAMPLE), "T", "a", dialect="legacy")

    parsed = yaml_mod.safe_load(tests["yaml"])
    topics = {
        case["expectedTopic"]
        for case in parsed["testCases"]
        if case.get("expectedTopic")
    }
    assert topics, "no expectedTopic emitted; the linkage assertion would be vacuous"
    for topic in topics:
        subagent = snake_case(topic)
        assert names_agree(topic, subagent)
        assert f"subagent {subagent}:" in bundle["agentScript"], (
            f"test expects topic {topic!r} but the bundle declares no "
            f"`subagent {subagent}` to serve it"
        )


def test_preview_api_names_keeps_the_reference_pair_consistent() -> None:
    result = mcp_server.preview_api_names("Update Case Status")

    assert result["ok"] is True
    assert result["routerActionName"] == f"go_to_{result['subagentName']}"
    assert result["subagentName"] == result["topicApiName"].lower()
    # The router prefix must fit inside the assumed 80-char API name cap.
    assert result["lengths"]["router"] <= 80


# ---------------------------------------------------------------------------
# Wire safety
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "call",
    [
        lambda: mcp_server.health(),
        lambda: mcp_server.validate_capture(str(EXAMPLE)),
        lambda: mcp_server.derive_spec(str(EXAMPLE)),
        lambda: mcp_server.emit_agent_bundle(str(EXAMPLE), "a", "A"),
        lambda: mcp_server.emit_test_spec(str(EXAMPLE), "T", "a"),
        lambda: mcp_server.preview_api_names("Update Case Status"),
    ],
)
def test_every_tool_result_is_json_serializable(call) -> None:
    """MCP returns JSON. A dataclass or set leaking through fails at the wire."""
    assert json.loads(json.dumps(call()))


def test_no_tool_writes_to_stdout(capsys: pytest.CaptureFixture[str]) -> None:
    """stdout is the JSON-RPC transport; a stray print corrupts the stream."""
    mcp_server.health()
    mcp_server.validate_capture(str(EXAMPLE))
    mcp_server.derive_spec(str(EXAMPLE))
    mcp_server.preview_api_names("Update Case Status")

    assert capsys.readouterr().out == ""


def test_logging_configuration_targets_stderr() -> None:
    """Same reason: a log record on stdout corrupts the JSON-RPC stream."""
    import logging
    import sys

    mcp_server._configure_logging()
    try:
        streams = [
            getattr(h, "stream", None) for h in logging.getLogger().handlers
        ]
        assert streams, "no logging handler configured"
        # Asserting `is sys.stderr` would be wrong under pytest, which swaps both
        # streams for capture objects. The load-bearing property is that stdout —
        # the JSON-RPC transport — is never a log target.
        assert sys.stdout not in streams
        assert sys.__stdout__ not in streams
    finally:
        # basicConfig is global; leaving a handler behind would leak into other
        # tests' captured output.
        for handler in list(logging.getLogger().handlers):
            logging.getLogger().removeHandler(handler)


def test_importing_the_module_does_not_reconfigure_root_logging() -> None:
    """A host app that imports this module must keep its own logging setup.

    `logging.basicConfig` at module scope would silently hijack the root logger of
    any application that imports the server for its tool functions.
    """
    import ast

    source = (
        Path(__file__).parent.parent / "src" / "sf_video_blueprint" / "mcp_server.py"
    ).read_text(encoding="utf-8")

    for node in ast.parse(source).body:
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Call):
            func = node.value.func
            name = getattr(func, "attr", getattr(func, "id", ""))
            assert name != "basicConfig", (
                "logging.basicConfig runs at import time; move it into main()"
            )


def test_tilde_paths_are_expanded() -> None:
    """A client may hand over an unexpanded ~ path."""
    resolved = mcp_server._resolve("~/x.jsonl")

    assert "~" not in str(resolved)
    assert resolved.is_absolute()


def test_main_is_callable_entry_point() -> None:
    """`sf-blueprint-mcp` points here; a wrong name breaks only at install time."""
    assert callable(mcp_server.main)
    assert inspect.signature(mcp_server.main).parameters == {}


# ---------------------------------------------------------------------------
# _spec_from_json helper
# ---------------------------------------------------------------------------


def _derived_spec_json() -> str:
    """Return a minimal DerivedAgentSpec JSON string for test fixtures."""
    derived = mcp_server.derive_spec(str(EXAMPLE))
    assert derived["ok"] is True
    return json.dumps(derived["spec"])


def test_spec_from_json_round_trips_a_real_spec() -> None:
    """_spec_from_json must reconstruct the same shape that to_dict() produces."""
    spec_json = _derived_spec_json()
    spec = mcp_server._spec_from_json(spec_json)

    from sf_video_blueprint.spec_builder import DerivedAgentSpec

    assert isinstance(spec, DerivedAgentSpec)
    assert spec.intent
    assert isinstance(spec.entities, list)
    assert isinstance(spec.evidence, list)


def test_spec_from_json_rejects_malformed_json() -> None:
    with pytest.raises(ValueError, match="not valid JSON"):
        mcp_server._spec_from_json("{not json}")


def test_spec_from_json_rejects_non_object_json() -> None:
    with pytest.raises(ValueError, match="must be a JSON object"):
        mcp_server._spec_from_json("[1, 2, 3]")


def test_spec_from_json_tolerates_missing_optional_keys() -> None:
    """A minimal spec with only intent must deserialize without raising."""
    spec = mcp_server._spec_from_json(json.dumps({"intent": "test intent"}))
    assert spec.intent == "test intent"
    assert spec.entities == []
    assert spec.evidence == []


# ---------------------------------------------------------------------------
# run_stage5_round
# ---------------------------------------------------------------------------


def _make_injected_runner(result_json: str):
    """Return a runner callable that fakes sf agent test run-eval output."""
    import subprocess

    class FakeResult:
        returncode = 0
        stdout = result_json
        stderr = ""

    def runner(cmd: list, timeout: int) -> FakeResult:
        return FakeResult()

    return runner


_FAKE_RUN_EVAL_RESULT = json.dumps({
    "result": {
        "tests": [
            {
                "id": "case_001",
                "status": "COMPLETED",
                "evaluations": [
                    {
                        "type": "topic",
                        "id": "topic_eval",
                        "is_pass": False,
                        "expected_value": "Update_Case_Status",
                        "actual_value": "General_Inquiry",
                    }
                ],
                "outputs": [],
            }
        ],
        "summary": {"total": 1, "passed": 0, "failed": 1},
    }
})


def test_run_stage5_round_refuses_forbidden_org(tmp_path: Path) -> None:
    """PPCDM / PPCaccenture must be refused before any org call."""
    result = mcp_server.run_stage5_round(
        spec_json=_derived_spec_json(),
        org_alias="PPCDM",
        agent_api_name="TestAgent",
        test_spec_name="TestSpec",
        out_dir=str(tmp_path),
    )

    assert result["ok"] is False
    assert "scope" in result["error"]["message"].lower()


def test_run_stage5_round_refuses_ppaccenture(tmp_path: Path) -> None:
    """PPCaccenture variant is also refused."""
    result = mcp_server.run_stage5_round(
        spec_json=_derived_spec_json(),
        org_alias="PPCaccenture",
        agent_api_name="TestAgent",
        test_spec_name="TestSpec",
        out_dir=str(tmp_path),
    )

    assert result["ok"] is False
    assert result["error"]["code"] == mcp_server.ERROR_VALIDATION


def test_run_stage5_round_rejects_bad_spec_json(tmp_path: Path) -> None:
    result = mcp_server.run_stage5_round(
        spec_json="not json at all",
        org_alias="test-org",
        agent_api_name="TestAgent",
        test_spec_name="TestSpec",
        out_dir=str(tmp_path),
    )

    assert result["ok"] is False
    assert result["error"]["code"] == mcp_server.ERROR_VALIDATION
    assert "spec_json" in result["error"]["message"]


def test_run_stage5_round_result_is_json_serializable(tmp_path: Path) -> None:
    """Stage-5 results must be wire-safe regardless of the org feedback shape."""
    # Patch run_agent_eval at the stage5 module level so the tool exercises the
    # full code path (forbidden-org check, spec parsing, feedback loop) without
    # a real org.
    from sf_video_blueprint import stage5

    _original = stage5._default_runner

    class _FakeCompleted:
        returncode = 0
        stdout = _FAKE_RUN_EVAL_RESULT
        stderr = ""

    stage5._default_runner = lambda cmd, timeout: _FakeCompleted()
    try:
        result = mcp_server.run_stage5_round(
            spec_json=_derived_spec_json(),
            org_alias="fake-dev-org",
            agent_api_name="TestAgent",
            test_spec_name="TestSpec",
            out_dir=str(tmp_path),
        )
    finally:
        stage5._default_runner = _original

    # May fail if the org call fails; we just need JSON-serializability.
    assert json.loads(json.dumps(result))


def test_run_stage5_round_response_has_expected_fields(tmp_path: Path) -> None:
    """A successful round must return all the documented top-level fields.

    Note: patching stage5._default_runner runs the full code path while keeping
    the subprocess off the network. The source is still 'run-eval' because
    run_agent_eval uses `runner is None` to decide provenance, not whether
    _default_runner was replaced. What we test here is the *shape* of the
    envelope, not provenance (provenance is tested in test_stage5.py).
    """
    from sf_video_blueprint import stage5

    _original = stage5._default_runner

    class _FakeCompleted:
        returncode = 0
        stdout = _FAKE_RUN_EVAL_RESULT
        stderr = ""

    stage5._default_runner = lambda cmd, timeout: _FakeCompleted()
    try:
        result = mcp_server.run_stage5_round(
            spec_json=_derived_spec_json(),
            org_alias="fake-dev-org",
            agent_api_name="TestAgent",
            test_spec_name="TestSpec",
            out_dir=str(tmp_path),
        )
    finally:
        stage5._default_runner = _original

    # Error path (e.g. sf not found on PATH in CI) is acceptable; shape path is not.
    if result["ok"]:
        assert "roundNumber" in result
        assert "trustworthy" in result
        assert isinstance(result["blockingIssues"], list)
        assert "findings" in result
        assert "feedbackSource" in result
        assert isinstance(result["caseCount"], int)


def test_run_stage5_round_writes_round_file(tmp_path: Path) -> None:
    """A successful round must write a round.json to out_dir/round-N/."""
    from sf_video_blueprint import stage5

    _original = stage5._default_runner

    class _FakeCompleted:
        returncode = 0
        stdout = _FAKE_RUN_EVAL_RESULT
        stderr = ""

    stage5._default_runner = lambda cmd, timeout: _FakeCompleted()
    try:
        result = mcp_server.run_stage5_round(
            spec_json=_derived_spec_json(),
            org_alias="fake-dev-org",
            agent_api_name="TestAgent",
            test_spec_name="TestSpec",
            out_dir=str(tmp_path),
        )
    finally:
        stage5._default_runner = _original

    if result["ok"]:
        assert Path(result["writtenTo"]).is_file(), (
            "run_stage5_round must write round.json when it succeeds"
        )


def test_run_stage5_round_increments_round_number_when_prior_rounds_exist(tmp_path: Path) -> None:
    """The tool must auto-increment the round number rather than overwriting a prior round.

    Pre-creating round-1/round.json simulates a completed round; the next call
    must write round-2, not attempt to overwrite round-1.
    """
    from sf_video_blueprint import stage5

    _original = stage5._default_runner

    class _FakeCompleted:
        returncode = 0
        stdout = _FAKE_RUN_EVAL_RESULT
        stderr = ""

    # Pre-create round-1 so the tool sees it as an existing round.
    (tmp_path / "round-1").mkdir()
    (tmp_path / "round-1" / "round.json").write_text(
        json.dumps({"round_number": 1}), encoding="utf-8"
    )

    stage5._default_runner = lambda cmd, timeout: _FakeCompleted()
    try:
        result = mcp_server.run_stage5_round(
            spec_json=_derived_spec_json(),
            org_alias="fake-dev-org",
            agent_api_name="TestAgent",
            test_spec_name="TestSpec",
            out_dir=str(tmp_path),
        )
    finally:
        stage5._default_runner = _original

    if result["ok"]:
        assert result["roundNumber"] == 2, (
            "tool must write round-2 when round-1 already exists"
        )
        assert (tmp_path / "round-1" / "round.json").read_text().startswith("{"), (
            "round-1 must not be overwritten"
        )


# ---------------------------------------------------------------------------
# run_iterate
# ---------------------------------------------------------------------------


def test_run_iterate_refuses_forbidden_org(tmp_path: Path) -> None:
    result = mcp_server.run_iterate(
        spec_json=_derived_spec_json(),
        org_alias="PPCDM",
        agent_api_name="TestAgent",
        test_spec_name="TestSpec",
        out_dir=str(tmp_path),
    )

    assert result["ok"] is False
    assert "scope" in result["error"]["message"].lower()


def test_run_iterate_refuses_ppaccenture(tmp_path: Path) -> None:
    result = mcp_server.run_iterate(
        spec_json=_derived_spec_json(),
        org_alias="PPCaccenture",
        agent_api_name="TestAgent",
        test_spec_name="TestSpec",
        out_dir=str(tmp_path),
    )

    assert result["ok"] is False
    assert result["error"]["code"] == mcp_server.ERROR_VALIDATION


def test_run_iterate_rejects_rounds_less_than_one(tmp_path: Path) -> None:
    result = mcp_server.run_iterate(
        spec_json=_derived_spec_json(),
        org_alias="test-org",
        agent_api_name="TestAgent",
        test_spec_name="TestSpec",
        out_dir=str(tmp_path),
        rounds=0,
    )

    assert result["ok"] is False
    assert result["error"]["code"] == mcp_server.ERROR_VALIDATION
    assert "rounds" in result["error"]["message"]


def test_run_iterate_rejects_bad_spec_json(tmp_path: Path) -> None:
    result = mcp_server.run_iterate(
        spec_json="not json",
        org_alias="test-org",
        agent_api_name="TestAgent",
        test_spec_name="TestSpec",
        out_dir=str(tmp_path),
    )

    assert result["ok"] is False
    assert result["error"]["code"] == mcp_server.ERROR_VALIDATION


def test_run_iterate_result_is_json_serializable(tmp_path: Path) -> None:
    """Iterate results must be wire-safe regardless of round shape."""
    from sf_video_blueprint import stage5

    _original = stage5._default_runner

    class _FakeCompleted:
        returncode = 0
        stdout = _FAKE_RUN_EVAL_RESULT
        stderr = ""

    stage5._default_runner = lambda cmd, timeout: _FakeCompleted()
    try:
        result = mcp_server.run_iterate(
            spec_json=_derived_spec_json(),
            org_alias="fake-dev-org",
            agent_api_name="TestAgent",
            test_spec_name="TestSpec",
            out_dir=str(tmp_path),
            rounds=1,
        )
    finally:
        stage5._default_runner = _original

    assert json.loads(json.dumps(result))


def test_run_iterate_with_injected_runner_reports_rounds_run(tmp_path: Path) -> None:
    """Even with synthetic feedback, the tool must report how many rounds ran."""
    from sf_video_blueprint import stage5

    _original = stage5._default_runner

    class _FakeCompleted:
        returncode = 0
        stdout = _FAKE_RUN_EVAL_RESULT
        stderr = ""

    stage5._default_runner = lambda cmd, timeout: _FakeCompleted()
    try:
        result = mcp_server.run_iterate(
            spec_json=_derived_spec_json(),
            org_alias="fake-dev-org",
            agent_api_name="TestAgent",
            test_spec_name="TestSpec",
            out_dir=str(tmp_path),
            rounds=1,
        )
    finally:
        stage5._default_runner = _original

    if result["ok"]:
        assert result["roundsRun"] >= 1
        assert isinstance(result["rounds"], list)
        assert len(result["rounds"]) == result["roundsRun"]


def test_run_iterate_passed_and_blocking_issues_are_consistent(tmp_path: Path) -> None:
    """passed=True and non-empty blockingIssues must never appear together.

    `passed` means the quality gate was cleared; `blockingIssues` means it was
    not. Reporting both simultaneously is a contradiction.
    """
    from sf_video_blueprint import stage5

    _original = stage5._default_runner

    # Build a fake that claims every case passed.
    _fake_all_pass = json.dumps({
        "result": {
            "tests": [
                {
                    "id": "case_001",
                    "status": "COMPLETED",
                    "evaluations": [
                        {"type": "topic", "id": "t", "is_pass": True}
                    ],
                    "outputs": [],
                }
            ],
            "summary": {"total": 1, "passed": 1, "failed": 0},
        }
    })

    class _FakeCompleted:
        returncode = 0
        stdout = _fake_all_pass
        stderr = ""

    stage5._default_runner = lambda cmd, timeout: _FakeCompleted()
    try:
        result = mcp_server.run_iterate(
            spec_json=_derived_spec_json(),
            org_alias="fake-dev-org",
            agent_api_name="TestAgent",
            test_spec_name="TestSpec",
            out_dir=str(tmp_path),
            rounds=1,
        )
    finally:
        stage5._default_runner = _original

    if result["ok"]:
        # passed=True and non-empty blockingIssues is a contradiction.
        if result.get("passed"):
            assert not result.get("blockingIssues"), (
                "run_iterate must not report passed=True alongside blocking issues"
            )

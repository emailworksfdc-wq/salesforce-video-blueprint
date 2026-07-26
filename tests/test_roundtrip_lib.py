from __future__ import annotations

"""Tests for the round-trip name derivation.

`scripts/agentforce_roundtrip.sh` used to hardcode three different agent names in
a single run — `test_agent` in the `.agent` config, `RoundtripTestAgent` in the
CLI flags, `TestAgent` in both test specs — so the emitted test suite pointed at
an agent that did not exist and the round trip could not ever have completed.

These tests pin the property that made that bug possible: **every name comes from
`naming.py`, and the artifacts are checked against each other on the emitted bytes
rather than on the variables we passed in.**

The `test_old_hardcoded_*` cases below are the regression guards. They assert that
the exact triple the script used to carry is rejected, so re-introducing any hand-
written name fails here rather than after someone spends an org run on it.
"""

import json
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from roundtrip_lib import (
    AgentIdentity,
    RoundtripError,
    derive_identity,
    emit_artifacts,
    load_derived_spec,
    verify_emitted_artifacts,
)

from sf_video_blueprint.naming import (
    names_agree,
    router_action_name,
    subagent_name,
    topic_api_name,
)
from sf_video_blueprint.spec_builder import (
    DerivedAgentSpec,
    DerivedEntity,
    SpecEvidence,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "agentforce_roundtrip.sh"
RT_LIB = REPO_ROOT / "scripts" / "roundtrip_lib.py"
EXAMPLE_CAPTURE = REPO_ROOT / "examples" / "case_triage.dom_capture.jsonl"


def _spec(intent: str = "Update Case (Status)", confidence: float = 0.7) -> DerivedAgentSpec:
    evidence = [SpecEvidence(source="dom-capture", detail="observed at step 3")]
    return DerivedAgentSpec(
        intent=intent,
        confidence=confidence,
        objects_touched=["Case"],
        entities=[
            DerivedEntity(
                name="status",
                object_api_name="Case",
                field_api_name="Status",
                evidence=list(evidence),
            )
        ],
        orchestration_steps=["Resolve the Case record", "Update Status", "Confirm"],
        guardrails=["Require explicit user confirmation before writing: Status."],
        failure_handling=["No failures were observed in this run."],
        unknowns=[],
        evidence=list(evidence),
    )


def _write_spec_json(path: Path, spec: DerivedAgentSpec, *, telemetry: str = "mock") -> Path:
    payload = spec.to_dict()
    payload["provenance"] = {
        "extraction_source": "dom-capture",
        "telemetry_source": telemetry,
        "replay_source": "noop",
    }
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# Name derivation
# ---------------------------------------------------------------------------


def test_all_three_artifacts_agree_on_names() -> None:
    """The canonical cross-artifact check, on the derived identity."""
    identity = derive_identity("Update Case (Status)")

    # Topic identity: spec YAML topic == subagent block == router target == expectedTopic.
    assert names_agree(identity.topic_name, identity.subagent)
    assert identity.router_action == f"go_to_{identity.subagent}"
    assert identity.expected_topic == identity.topic_name

    # Agent identity: bundle API name == config developer_name == test subjectName.
    assert names_agree(identity.agent_api_name, identity.developer_name)
    assert identity.test_subject_name == identity.agent_api_name


def test_names_come_from_naming_module_not_from_this_script() -> None:
    """Derivation must delegate, so a naming.py change propagates automatically."""
    intent = "Escalate Case (Priority)"
    identity = derive_identity(intent)

    assert identity.topic_name == topic_api_name(intent)
    assert identity.subagent == subagent_name(intent)
    assert identity.router_action == router_action_name(intent)


@pytest.mark.parametrize(
    "intent",
    [
        "Update Case (Status)",
        "Create Contact",
        "escalation",  # reserved: naming.py must escape it
        "2024 Renewal Review",  # must not start with a digit
        "Update Case " + ("Extremely Verbose Business Process Name " * 4),  # over the cap
    ],
)
def test_identity_is_coherent_for_awkward_intents(intent: str) -> None:
    """The linkages must hold through escaping, prefixing and truncation.

    Truncation is the subtle one: the agent name carries the `SFVB TEST` prefix,
    so it truncates at a different token than the topic name does. Both dialects
    of each name must still agree with each other.
    """
    identity = derive_identity(intent)
    identity.assert_coherent()  # raises on any broken linkage

    assert names_agree(identity.agent_api_name, identity.developer_name)
    assert names_agree(identity.topic_name, identity.subagent)


def test_empty_intent_is_refused_rather_than_named() -> None:
    with pytest.raises(RoundtripError, match="no intent"):
        derive_identity("   ")


# ---------------------------------------------------------------------------
# Regression guards: the exact bug that was fixed
# ---------------------------------------------------------------------------


def test_old_hardcoded_name_triple_is_rejected() -> None:
    """The three names the script used to carry must not validate as coherent.

    Before the fix the round trip ran with developer_name="test_agent",
    --api-name "RoundtripTestAgent", and subjectName "TestAgent". This asserts
    that combination is now caught, so it cannot be reintroduced silently.
    """
    broken = AgentIdentity(
        intent="Update Case (Status)",
        agent_api_name="RoundtripTestAgent",  # what the CLI flags said
        developer_name="test_agent",  # what the .agent config said
        agent_label="Roundtrip Test Agent",
        test_subject_name="TestAgent",  # what both test specs said
        topic_name="Update_Case_Status",
        subagent="update_case_status",
        router_action="go_to_update_case_status",
        expected_topic="Update_Case_Status",
    )

    with pytest.raises(RoundtripError) as excinfo:
        broken.assert_coherent()

    message = str(excinfo.value)
    assert "agent identity diverged" in message
    assert "does not name the agent under test" in message


def test_divergent_topic_dialect_is_rejected() -> None:
    """`eval_spec` once emitted `UpdateCase` as expectedTopic — a dangling reference."""
    broken = AgentIdentity(
        intent="Update Case (Status)",
        agent_api_name="SFVB_TEST_Update_Case_Status",
        developer_name="sfvb_test_update_case_status",
        agent_label="SFVB TEST Update Case Status",
        test_subject_name="SFVB_TEST_Update_Case_Status",
        topic_name="Update_Case_Status",
        subagent="update_case_status",
        router_action="go_to_update_case_status",
        expected_topic="UpdateCase",  # points at a topic that does not exist
    )
    with pytest.raises(RoundtripError, match="expectedTopic"):
        broken.assert_coherent()


def test_router_pointing_at_the_wrong_subagent_is_rejected() -> None:
    broken = AgentIdentity(
        intent="Update Case (Status)",
        agent_api_name="SFVB_TEST_Update_Case_Status",
        developer_name="sfvb_test_update_case_status",
        agent_label="SFVB TEST Update Case Status",
        test_subject_name="SFVB_TEST_Update_Case_Status",
        topic_name="Update_Case_Status",
        subagent="update_case_status",
        router_action="go_to_something_else",
        expected_topic="Update_Case_Status",
    )
    with pytest.raises(RoundtripError, match="router action"):
        broken.assert_coherent()


def test_the_shell_script_contains_no_hardcoded_agent_names() -> None:
    """The script must ask for names, never spell them.

    A literal name in the shell script is how the original divergence happened:
    three stages were edited at different times and nothing tied them together.

    Comments are excluded on purpose: the file's header quotes the three old names
    to explain the bug, and deleting that explanation to satisfy a lint would lose
    the only record of why this design exists. What matters is that no *executed*
    line spells a name.
    """
    code_lines = [
        line
        for line in SCRIPT.read_text(encoding="utf-8").splitlines()
        if not line.lstrip().startswith("#")
    ]
    code = "\n".join(code_lines)

    for forbidden in ("RoundtripTestAgent", "test_agent", "TestAgent", "Roundtrip Test Agent"):
        assert forbidden not in code, (
            f"{SCRIPT.name} hardcodes the agent name {forbidden!r} in executable code; "
            "derive it from naming.py via roundtrip_lib.py instead"
        )

    # And it must actually be asking roundtrip_lib for them.
    assert "roundtrip_lib.py" in code
    assert "RT_AGENT_API_NAME" in code


# ---------------------------------------------------------------------------
# Emission: the artifacts on disk must carry the derived names
# ---------------------------------------------------------------------------


def test_emit_writes_every_name_into_the_artifacts(tmp_path: Path) -> None:
    spec_path = _write_spec_json(tmp_path / "spec.json", _spec())
    manifest = emit_artifacts(spec_path, tmp_path / "out")
    identity = manifest["identity"]
    paths = manifest["paths"]

    script_text = Path(paths["agent_script"]).read_text(encoding="utf-8")
    assert f'developer_name: "{identity["developer_name"]}"' in script_text
    assert f"subagent {identity['subagent']}:" in script_text
    assert (
        f"{identity['router_action']}: @utils.transition to @subagent.{identity['subagent']}"
        in script_text
    )

    spec_yaml = Path(paths["agent_spec_yaml"]).read_text(encoding="utf-8")
    assert f"name: {identity['topic_name']}" in spec_yaml

    for key in ("test_spec_legacy", "test_spec_ngt"):
        text = Path(paths[key]).read_text(encoding="utf-8")
        assert f"subjectName: {identity['test_subject_name']}" in text
        assert identity["expected_topic"] in text


def test_emitted_bundle_is_laid_out_where_the_cli_can_find_it(tmp_path: Path) -> None:
    """`sf agent validate authoring-bundle` is requiresProject=true.

    It resolves the bundle from a local SFDX package directory and throws
    AABNotFound if the layout is wrong, so the layout is part of the contract:
    <project>/force-app/main/default/aiAuthoringBundles/<ApiName>/<ApiName>.agent
    """
    spec_path = _write_spec_json(tmp_path / "spec.json", _spec())
    manifest = emit_artifacts(spec_path, tmp_path / "out")
    api_name = manifest["identity"]["agent_api_name"]

    project = Path(manifest["paths"]["sfdx_project_dir"])
    assert (project / "sfdx-project.json").is_file()

    bundle = project / "force-app" / "main" / "default" / "aiAuthoringBundles" / api_name
    assert (bundle / f"{api_name}.agent").is_file()
    assert (bundle / f"{api_name}.bundle-meta.xml").is_file()

    package_dirs = json.loads((project / "sfdx-project.json").read_text(encoding="utf-8"))
    assert package_dirs["packageDirectories"][0]["path"] == "force-app"


def test_emitted_artifacts_are_verified_against_each_other(tmp_path: Path) -> None:
    """`verify_emitted_artifacts` must catch a tampered file, not just trust us."""
    spec_path = _write_spec_json(tmp_path / "spec.json", _spec())
    manifest = emit_artifacts(spec_path, tmp_path / "out")
    identity = AgentIdentity(**manifest["identity"])
    legacy = Path(manifest["paths"]["test_spec_legacy"])

    # Simulate the original bug: the test spec names a different agent.
    tampered = legacy.read_text(encoding="utf-8").replace(
        f"subjectName: {identity.test_subject_name}", "subjectName: TestAgent"
    )
    legacy.write_text(tampered, encoding="utf-8")

    with pytest.raises(RoundtripError, match="subjectName"):
        verify_emitted_artifacts(
            identity,
            agent_script=Path(manifest["paths"]["agent_script"]),
            agent_spec_yaml=Path(manifest["paths"]["agent_spec_yaml"]),
            test_specs=[legacy],
        )


def test_low_confidence_spec_is_refused(tmp_path: Path) -> None:
    """An inadequate recording must stop the round trip, not produce a plausible file."""
    spec_path = _write_spec_json(tmp_path / "spec.json", _spec(confidence=0.1))
    with pytest.raises(Exception) as excinfo:  # InsufficientEvidenceError
        emit_artifacts(spec_path, tmp_path / "out")
    assert "confidence" in str(excinfo.value).lower() or "evidence" in str(excinfo.value).lower()


def test_load_derived_spec_rejects_a_spec_with_no_intent(tmp_path: Path) -> None:
    path = tmp_path / "bad.json"
    path.write_text(json.dumps({"confidence": 0.9}), encoding="utf-8")
    with pytest.raises(RoundtripError, match="no 'intent'"):
        load_derived_spec(path)


def test_load_derived_spec_rejects_invalid_json(tmp_path: Path) -> None:
    path = tmp_path / "bad.json"
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(RoundtripError, match="not valid JSON"):
        load_derived_spec(path)


# ---------------------------------------------------------------------------
# The script itself: honest reporting of what it did and did not do
# ---------------------------------------------------------------------------


def test_script_is_syntactically_valid() -> None:
    result = subprocess.run(
        ["bash", "-n", str(SCRIPT)], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.skipif(not EXAMPLE_CAPTURE.is_file(), reason="example capture missing")
def test_offline_run_completes_and_refuses_to_claim_org_validation(tmp_path: Path) -> None:
    """The whole point: offline the script works, and says nothing was validated.

    This is the end-to-end guard on the honesty property. The previous script
    printed "All executed stages PASSED" with a summary of {"pass": true} while
    both org stages were skipped.
    """
    out_dir = tmp_path / "rt"
    result = subprocess.run(
        [
            "bash",
            str(SCRIPT),
            "--capture",
            str(EXAMPLE_CAPTURE),
            "--out",
            str(out_dir),
        ],
        capture_output=True,
        text=True,
        check=False,
        cwd=REPO_ROOT,
        env={**__import__("os").environ, "PY_BIN": sys.executable},
    )

    assert result.returncode == 0, f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"

    # It must NOT claim Salesforce validation it did not perform.
    assert "NOTHING WAS VALIDATED BY SALESFORCE" in result.stdout
    assert "s5_org_validate" in result.stdout

    summary = json.loads((out_dir / "roundtrip_summary.json").read_text(encoding="utf-8"))
    assert summary["salesforce_validated"] is False
    assert summary["org_alias"] is None
    assert summary["all_executed_stages_passed"] is True

    statuses = {stage["stage"]: stage["status"] for stage in summary["stages"]}
    assert statuses["s5_org_validate"] == "skipped"
    assert statuses["s4_emit_artifacts"] == "pass"

    # And the names it used must be the derived ones, recorded for the reader.
    names = summary["derived_names"]
    assert names_agree(names["agent_api_name"], names["developer_name"])
    assert names["test_subject_name"] == names["agent_api_name"]


def test_summary_has_no_single_pass_boolean(tmp_path: Path) -> None:
    """`{"pass": true}` with skipped org stages was the original overclaim.

    The key is deliberately absent so a downstream reader cannot resurrect the
    ambiguity by checking `summary["pass"]`.
    """
    out_dir = tmp_path / "rt"
    subprocess.run(
        ["bash", str(SCRIPT), "--capture", str(EXAMPLE_CAPTURE), "--out", str(out_dir)],
        capture_output=True,
        text=True,
        check=False,
        cwd=REPO_ROOT,
        env={**__import__("os").environ, "PY_BIN": sys.executable},
    )
    summary = json.loads((out_dir / "roundtrip_summary.json").read_text(encoding="utf-8"))
    assert "pass" not in summary
    assert "salesforce_validated" in summary


def test_script_rejects_unknown_arguments() -> None:
    result = subprocess.run(
        ["bash", str(SCRIPT), "--nonsense"],
        capture_output=True,
        text=True,
        check=False,
        cwd=REPO_ROOT,
    )
    assert result.returncode == 2
    assert "unknown argument" in result.stderr


def test_script_refuses_the_out_of_scope_orgs() -> None:
    """PPCDM/PPCaccenture are hard-blocked with no override."""
    for alias in ("PPCDM", "PPCaccenture"):
        result = subprocess.run(
            ["bash", str(SCRIPT), "--org", alias],
            capture_output=True,
            text=True,
            check=False,
            cwd=REPO_ROOT,
            env={**__import__("os").environ, "PY_BIN": sys.executable},
        )
        assert result.returncode == 3, result.stdout
        assert "out of scope" in result.stderr


def test_capture_and_spec_are_mutually_exclusive(tmp_path: Path) -> None:
    spec_path = _write_spec_json(tmp_path / "spec.json", _spec())
    result = subprocess.run(
        [
            "bash",
            str(SCRIPT),
            "--capture",
            str(EXAMPLE_CAPTURE),
            "--spec",
            str(spec_path),
        ],
        capture_output=True,
        text=True,
        check=False,
        cwd=REPO_ROOT,
    )
    assert result.returncode == 2
    assert "mutually exclusive" in result.stderr


def test_score_subcommand_reports_without_weakening_the_gate(tmp_path: Path) -> None:
    """The gate must stay at 75 and must refuse mock telemetry.

    Wiring the round trip must not become a route to a passing score: a spec
    built from mock telemetry has to keep failing.
    """
    spec_path = _write_spec_json(tmp_path / "spec.json", _spec(), telemetry="mock")
    out = tmp_path / "score.json"
    result = subprocess.run(
        [sys.executable, str(RT_LIB), "score", str(spec_path), "--out", str(out)],
        capture_output=True,
        text=True,
        check=False,
        cwd=REPO_ROOT,
    )
    assert result.returncode == 0, result.stderr

    payload = json.loads(out.read_text(encoding="utf-8"))
    assert payload["threshold"] == 75
    assert payload["passed"] is False, "mock telemetry must not pass the gate"
    assert any("mock" in issue.lower() for issue in payload["blocking_issues"])


# ---------------------------------------------------------------------------
# The failure path: a rejected bundle must be reported as a failure
# ---------------------------------------------------------------------------
#
# Everything above proves the script is honest when the org stage is SKIPPED.
# That is only half the property. The stage that actually matters is the one that
# RUNS AND FAILS: `sf agent validate authoring-bundle` exits 1 when Salesforce
# rejects the .agent grammar, and the original script's summary logic
# (`all(status in ("pass", "skipped"))`) would have been just as happy to call
# that a pass as it was to call a skip one.
#
# Measured against AFT3 on 2026-07-26: a bundle whose `instructions: ->` key was
# stripped to a bare `->` — lane 01's original emitter defect — is rejected with
# exit 1, `code: CompileAgentScriptError`, 135 CompilationErrors. These tests
# replay that shape through a stub so the reporting is verified with no org and no
# credentials, which is what lets them run in CI.


def _stub_sf(tmp_path: Path, *, validate_exit: int, validate_stdout: str) -> Path:
    """A fake `sf` that answers the two subcommands the script actually calls.

    Stubbing the CLI rather than mocking Python is deliberate: the behaviour under
    test is in the shell script's exit-code handling, so the seam has to be the
    process boundary the script really uses.
    """
    bin_dir = tmp_path / "stubbin"
    bin_dir.mkdir()
    sf = bin_dir / "sf"
    sf.write_text(
        "#!/usr/bin/env bash\n"
        'case "$*" in\n'
        "  *--version*) echo '@salesforce/cli/2.143.6 stub' ;;\n"
        "  *'agent --help'*|*agent*--help*) exit 0 ;;\n"
        # instanceUrl must look like a dev org or the safety guard (exit 3) trips
        # before validation is ever reached.
        "  *'org display'*)\n"
        '    echo \'{"status":0,"result":{"instanceUrl":"https://stub-dev.develop.my.salesforce.com"}}\' ;;\n'
        "  *'agent validate'*)\n"
        f"    cat <<'JSON'\n{validate_stdout}\nJSON\n"
        f"    exit {validate_exit} ;;\n"
        "  *) echo \"stub: unhandled: $*\" >&2; exit 127 ;;\n"
        "esac\n",
        encoding="utf-8",
    )
    sf.chmod(0o755)
    return bin_dir


# The verbatim envelope AFT3 returned for the rejected bundle, trimmed to the
# fields the script parses.
_REJECTION_JSON = json.dumps(
    {
        "status": 1,
        "code": "CompileAgentScriptError",
        "message": "Syntax error: unexpected `->`",
        "data": {
            "errors": [
                {
                    "errorType": "CompilationError",
                    "description": "Syntax error: unexpected `->`",
                    "lineStart": 38,
                    "colStart": 8,
                },
                {
                    "errorType": "CompilationError",
                    "description": "Missing required field 'target'",
                    "lineStart": 41,
                    "colStart": 12,
                },
            ]
        },
    },
    indent=2,
)


def _run_with_stub(tmp_path: Path, bin_dir: Path, out_dir: Path):
    import os

    env = {
        **os.environ,
        "PY_BIN": sys.executable,
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
    }
    return subprocess.run(
        [
            "bash",
            str(SCRIPT),
            "--capture",
            str(EXAMPLE_CAPTURE),
            "--org",
            "STUB_DEV",
            "--out",
            str(out_dir),
        ],
        capture_output=True,
        text=True,
        check=False,
        cwd=REPO_ROOT,
        env=env,
    )


@pytest.mark.skipif(not EXAMPLE_CAPTURE.is_file(), reason="example capture missing")
def test_a_rejected_bundle_is_reported_as_a_failure(tmp_path: Path) -> None:
    """Exit 1 from the compiler must not be reportable as a completed round trip."""
    out_dir = tmp_path / "rt"
    result = _run_with_stub(
        tmp_path, _stub_sf(tmp_path, validate_exit=1, validate_stdout=_REJECTION_JSON), out_dir
    )

    assert result.returncode == 1, f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    # Without --keep-going the run aborts at the failed stage, so the abort notice
    # is the verdict line. What matters either way is that no success is claimed.
    assert "ABORTED after a failed stage" in result.stdout
    assert "ROUND TRIP COMPLETE" not in result.stdout
    assert "Salesforce validated" not in result.stdout

    summary = json.loads((out_dir / "roundtrip_summary.json").read_text(encoding="utf-8"))
    statuses = {stage["stage"]: stage["status"] for stage in summary["stages"]}
    assert statuses["s5_org_validate"] == "fail"

    # The critical assertion: a stage that ran and FAILED must not be folded into
    # a success, and `salesforce_validated` must not become true just because an
    # org was configured and the stage was attempted.
    assert summary["all_executed_stages_passed"] is False
    assert summary["salesforce_validated"] is False
    assert summary["org_alias"] == "STUB_DEV"


@pytest.mark.skipif(not EXAMPLE_CAPTURE.is_file(), reason="example capture missing")
def test_the_compilers_own_errors_are_printed_not_paraphrased(tmp_path: Path) -> None:
    """A paraphrased compiler error is how a project ends up guessing at a grammar.

    The line and column matter most: they are what turns "invalid syntax
    somewhere" into a fix.
    """
    out_dir = tmp_path / "rt"
    result = _run_with_stub(
        tmp_path, _stub_sf(tmp_path, validate_exit=1, validate_stdout=_REJECTION_JSON), out_dir
    )

    assert "CompilationError" in result.stdout
    assert "unexpected `->`" in result.stdout
    assert "Ln 38" in result.stdout
    assert "2 compilation error(s)" in result.stdout


@pytest.mark.skipif(not EXAMPLE_CAPTURE.is_file(), reason="example capture missing")
def test_keep_going_still_ends_on_a_failure_verdict(tmp_path: Path) -> None:
    """`--keep-going` must not turn a failed stage into a completed round trip.

    The flag exists to see every stage's result in one run. A reader who passes it
    is the most likely to skim to the last line, so that line has to say FAILED.
    """
    import os

    out_dir = tmp_path / "rt"
    bin_dir = _stub_sf(tmp_path, validate_exit=1, validate_stdout=_REJECTION_JSON)
    result = subprocess.run(
        [
            "bash",
            str(SCRIPT),
            "--capture",
            str(EXAMPLE_CAPTURE),
            "--org",
            "STUB_DEV",
            "--out",
            str(out_dir),
            "--keep-going",
        ],
        capture_output=True,
        text=True,
        check=False,
        cwd=REPO_ROOT,
        env={
            **os.environ,
            "PY_BIN": sys.executable,
            "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        },
    )

    assert result.returncode == 1
    assert "ROUND TRIP FAILED" in result.stdout
    assert "ROUND TRIP COMPLETE" not in result.stdout


@pytest.mark.skipif(not EXAMPLE_CAPTURE.is_file(), reason="example capture missing")
def test_a_successful_validation_is_recorded_as_validated(tmp_path: Path) -> None:
    """The positive control for the two tests above.

    Without it, a script that reported every org run as a failure would pass them
    both. Mirrors the real AFT3 response: `{"status":0,"result":{"success":true}}`.
    """
    out_dir = tmp_path / "rt"
    success = json.dumps({"status": 0, "result": {"success": True}, "warnings": []})
    result = _run_with_stub(
        tmp_path, _stub_sf(tmp_path, validate_exit=0, validate_stdout=success), out_dir
    )

    assert result.returncode == 0, f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    assert "ROUND TRIP COMPLETE" in result.stdout
    assert "NOTHING WAS VALIDATED" not in result.stdout

    summary = json.loads((out_dir / "roundtrip_summary.json").read_text(encoding="utf-8"))
    assert summary["salesforce_validated"] is True
    assert summary["stages_skipped"] == 0

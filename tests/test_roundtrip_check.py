"""Tests for scripts/roundtrip_check.py — the CI contract check.

These tests cover:

1. **Offline behaviour is unchanged.** Omitting --org-alias leaves the exit code
   unaffected.  The existing CI gate that runs without an org must not be broken.

2. **New flags are wired up.** Providing --org-alias + --agent-api-name reaches
   ``run_stage5_round`` and actually invokes ``stage5.run_agent_eval`` (verified
   via the injected runner).

3. **--strict exits non-zero on a failing round.** A round with blocking issues
   or failing test cases returns 1 when --strict is passed.

4. **--strict does not affect exit code when omitted.** A failing round without
   --strict still returns 0 (the caller's choice, not ours).

5. **Missing --org-alias-only or --agent-api-name-only is refused.** Both must be
   supplied together or both omitted.

All tests run without a live org.  The ``runner`` injection in
``run_stage5_round`` is used where stage-5 behaviour is under test.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

# Make scripts/ importable.
REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = REPO_ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))
sys.path.insert(0, str(REPO_ROOT / "src"))

from roundtrip_check import (  # type: ignore[import]
    _check_name_contracts,
    _check_offline_contracts,
    main,
    run_stage5_round,
)

from sf_video_blueprint.spec_builder import DerivedAgentSpec, DerivedEntity, SpecEvidence

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_AGENT_API_NAME = "SFVB_TEST_Update_Case_Status"
_DEVELOPER_NAME = "sfvb_test_update_case_status"
_TOPIC_NAME = "Update_Case_Status"
_SUBAGENT = "update_case_status"
_ROUTER_ACTION = "go_to_update_case_status"


def _derived_names() -> dict[str, str]:
    return {
        "agent_api_name": _AGENT_API_NAME,
        "developer_name": _DEVELOPER_NAME,
        "agent_label": "SFVB TEST Update Case Status",
        "test_subject_name": _AGENT_API_NAME,
        "topic_name": _TOPIC_NAME,
        "subagent": _SUBAGENT,
        "router_action": _ROUTER_ACTION,
        "expected_topic": _TOPIC_NAME,
        "intent": "Update Case Status",
    }


def _offline_summary() -> dict[str, Any]:
    """A valid offline summary — no org, s5_org_validate skipped."""
    return {
        "all_executed_stages_passed": True,
        "stages_run": 4,
        "stages_skipped": 1,
        "salesforce_validated": False,
        "org_alias": None,
        "derived_names": _derived_names(),
        "stages": [
            {"stage": "s1_derive_spec", "status": "pass", "detail": ""},
            {"stage": "s2_derive_names", "status": "pass", "detail": ""},
            {"stage": "s3_score_gate", "status": "pass", "detail": ""},
            {"stage": "s4_emit_artifacts", "status": "pass", "detail": ""},
            {"stage": "s5_org_validate", "status": "skipped", "detail": "no org"},
        ],
    }


def _write_offline_summary(tmp_path: Path) -> Path:
    summary_path = tmp_path / "roundtrip_summary.json"
    summary_path.write_text(json.dumps(_offline_summary()), encoding="utf-8")
    return summary_path


def _write_derived_spec(out_dir: Path) -> Path:
    """Write a minimal derived spec JSON that load_derived_spec can parse."""
    spec = DerivedAgentSpec(
        intent="Update Case Status",
        confidence=0.75,
        objects_touched=["Case"],
        entities=[
            DerivedEntity(
                name="status",
                object_api_name="Case",
                field_api_name="Status",
                evidence=[SpecEvidence(source="dom-capture", detail="observed at step 3")],
            )
        ],
        orchestration_steps=["Resolve the Case record", "Update Status field", "Confirm"],
        guardrails=["Require explicit user confirmation before writing: Status."],
        failure_handling=["No failures were observed in this run."],
        unknowns=[],
        evidence=[SpecEvidence(source="dom-capture", detail="observed at step 3")],
    )
    payload = spec.to_dict()
    payload["provenance"] = {
        "extraction_source": "dom-capture",
        "telemetry_source": "live-org",
        "replay_source": "noop",
    }
    path = out_dir / "roundtrip.agent-spec.json"
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return path


def _write_minimal_test_spec(out_dir: Path) -> Path:
    """Write a stub legacy test spec YAML so file-existence checks pass."""
    path = out_dir / "testSpec-legacy.yaml"
    path.write_text(
        "apiVersion: aiEvaluationDefinition\n"
        f"name: {_AGENT_API_NAME} Tests\n"
        f"subjectName: {_AGENT_API_NAME}\n"
        "testCases: []\n",
        encoding="utf-8",
    )
    return path


# A minimal run-eval JSON response that parse_run_eval_results accepts.
_FAKE_EVAL_RESPONSE = json.dumps(
    {
        "status": 0,
        "result": {
            "tests": [
                {
                    "id": "case_0",
                    "status": "passed",
                    "evaluations": [
                        {
                            "type": "evaluator.planner_topic_assertion",
                            "id": "check_topic",
                            "compute_status": "COMPLETED",
                            "score": 1,
                            "is_pass": True,
                            "label": "",
                            "explainability": "",
                            "error_message": None,
                            "actual_value": _TOPIC_NAME,
                            "expected_value": _TOPIC_NAME,
                        }
                    ],
                    "outputs": [],
                }
            ],
            "summary": {"total": 1, "passed": 1, "failed": 0},
        },
    }
)

_FAKE_EVAL_RESPONSE_FAILING = json.dumps(
    {
        "status": 0,
        "result": {
            "tests": [
                {
                    "id": "case_0",
                    "status": "failed",
                    "evaluations": [
                        {
                            "type": "evaluator.planner_topic_assertion",
                            "id": "check_topic",
                            "compute_status": "COMPLETED",
                            "score": 0,
                            "is_pass": False,
                            "label": "",
                            "explainability": "Wrong topic",
                            "error_message": None,
                            "actual_value": "Other_Topic",
                            "expected_value": _TOPIC_NAME,
                        }
                    ],
                    "outputs": [],
                }
            ],
            "summary": {"total": 1, "passed": 0, "failed": 1},
        },
    }
)


def _injected_runner(response_json: str):
    """Return a fake subprocess runner that always succeeds with response_json."""

    def runner(cmd: list[str], timeout: int):  # noqa: ARG001
        return SimpleNamespace(returncode=0, stdout=response_json, stderr="")

    return runner


# ---------------------------------------------------------------------------
# 1. Offline behaviour is unchanged
# ---------------------------------------------------------------------------


def test_offline_check_passes_with_valid_summary(tmp_path: Path) -> None:
    """Omitting --org-alias with a valid summary exits 0 — same as before."""
    summary_path = _write_offline_summary(tmp_path)
    rc = main([str(summary_path)])
    assert rc == 0


def test_offline_check_fails_on_contract_violation(tmp_path: Path) -> None:
    """Contract violations still cause exit 1 when --org-alias is omitted."""
    summary = _offline_summary()
    summary["salesforce_validated"] = True  # lie
    summary_path = tmp_path / "roundtrip_summary.json"
    summary_path.write_text(json.dumps(summary), encoding="utf-8")

    rc = main([str(summary_path)])
    assert rc == 1


def test_offline_check_missing_summary_exits_2(tmp_path: Path) -> None:
    """A missing summary file exits 2 regardless of org flags."""
    rc = main([str(tmp_path / "nonexistent.json")])
    assert rc == 2


def test_offline_flag_pair_mutual_exclusion(tmp_path: Path) -> None:
    """--org-alias without --agent-api-name (and vice versa) is refused."""
    summary_path = _write_offline_summary(tmp_path)
    # Only org-alias, no agent-api-name
    with pytest.raises(SystemExit) as exc:
        main([str(summary_path), "--org-alias", "some-org"])
    assert exc.value.code == 2

    # Only agent-api-name, no org-alias
    with pytest.raises(SystemExit) as exc:
        main([str(summary_path), "--agent-api-name", _AGENT_API_NAME])
    assert exc.value.code == 2


# ---------------------------------------------------------------------------
# 2. New flags are wired up: run_stage5_round is reached
# ---------------------------------------------------------------------------


def test_run_stage5_round_reaches_run_agent_eval(tmp_path: Path) -> None:
    """Providing --org-alias + --agent-api-name calls run_agent_eval via the runner."""
    _write_derived_spec(tmp_path)
    _write_minimal_test_spec(tmp_path)
    summary_path = _write_offline_summary(tmp_path)

    calls: list[tuple[list[str], int]] = []

    def capturing_runner(cmd: list[str], timeout: int):
        calls.append((cmd, timeout))
        return SimpleNamespace(returncode=0, stdout=_FAKE_EVAL_RESPONSE, stderr="")

    rc, round_result = run_stage5_round(
        summary_path,
        org_alias="test-dev-org",
        agent_api_name=_AGENT_API_NAME,
        runner=capturing_runner,
    )

    # The runner was called — stage5.run_agent_eval reached the subprocess layer.
    assert len(calls) == 1, f"expected exactly 1 runner call, got {len(calls)}"
    assert "run-eval" in calls[0][0]
    assert rc == 0
    assert round_result is not None


def test_run_stage5_round_returns_round_result_object(tmp_path: Path) -> None:
    """run_stage5_round returns a Stage5Round with round_number=1."""
    _write_derived_spec(tmp_path)
    _write_minimal_test_spec(tmp_path)
    summary_path = _write_offline_summary(tmp_path)

    rc, round_result = run_stage5_round(
        summary_path,
        org_alias="test-dev-org",
        agent_api_name=_AGENT_API_NAME,
        runner=_injected_runner(_FAKE_EVAL_RESPONSE),
    )

    assert rc == 0
    assert round_result is not None
    assert round_result.round_number == 1
    assert round_result.score_after is not None


def test_run_stage5_round_missing_spec_returns_1(tmp_path: Path) -> None:
    """Missing derived spec returns (1, None) — no crash."""
    summary_path = _write_offline_summary(tmp_path)
    # Intentionally do NOT write roundtrip.agent-spec.json
    _write_minimal_test_spec(tmp_path)

    rc, round_result = run_stage5_round(
        summary_path,
        org_alias="test-dev-org",
        agent_api_name=_AGENT_API_NAME,
        runner=_injected_runner(_FAKE_EVAL_RESPONSE),
    )
    assert rc == 1
    assert round_result is None


def test_run_stage5_round_missing_test_spec_returns_1(tmp_path: Path) -> None:
    """Missing test spec returns (1, None) — no crash."""
    _write_derived_spec(tmp_path)
    summary_path = _write_offline_summary(tmp_path)
    # Intentionally do NOT write testSpec-legacy.yaml

    rc, round_result = run_stage5_round(
        summary_path,
        org_alias="test-dev-org",
        agent_api_name=_AGENT_API_NAME,
        runner=_injected_runner(_FAKE_EVAL_RESPONSE),
    )
    assert rc == 1
    assert round_result is None


def test_test_spec_resolved_from_emit_manifest(tmp_path: Path) -> None:
    """When emit_manifest.json names a test spec path, that path is used."""
    _write_derived_spec(tmp_path)
    # Write the test spec in a non-default location.
    custom_spec = tmp_path / "custom" / "testSpec.yaml"
    custom_spec.parent.mkdir(parents=True)
    custom_spec.write_text(
        "apiVersion: aiEvaluationDefinition\n"
        f"subjectName: {_AGENT_API_NAME}\n"
        "testCases: []\n",
        encoding="utf-8",
    )
    manifest = {"paths": {"test_spec_legacy": str(custom_spec)}}
    (tmp_path / "emit_manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    summary_path = _write_offline_summary(tmp_path)

    seen_paths: list[str] = []

    def path_capturing_runner(cmd: list[str], timeout: int):
        # The --spec argument is the test spec path.
        for i, tok in enumerate(cmd):
            if tok == "--spec" and i + 1 < len(cmd):
                seen_paths.append(cmd[i + 1])
        return SimpleNamespace(returncode=0, stdout=_FAKE_EVAL_RESPONSE, stderr="")

    rc, _ = run_stage5_round(
        summary_path,
        org_alias="test-dev-org",
        agent_api_name=_AGENT_API_NAME,
        runner=path_capturing_runner,
    )
    assert rc == 0
    assert seen_paths, "runner was never called"
    assert str(custom_spec) in seen_paths[0]


# ---------------------------------------------------------------------------
# 3. --strict exits non-zero when the round fails
# ---------------------------------------------------------------------------


def test_strict_exits_nonzero_on_failing_round(tmp_path: Path) -> None:
    """With --strict, a failing test case causes exit 1."""
    _write_derived_spec(tmp_path)
    _write_minimal_test_spec(tmp_path)
    summary_path = _write_offline_summary(tmp_path)

    rc = main(
        [
            str(summary_path),
            "--org-alias",
            "test-dev-org",
            "--agent-api-name",
            _AGENT_API_NAME,
            "--strict",
        ],
        _runner=_injected_runner(_FAKE_EVAL_RESPONSE_FAILING),
    )
    assert rc == 1


def test_strict_exits_nonzero_on_blocking_issues(tmp_path: Path) -> None:
    """With --strict, synthetic feedback (blocking) causes exit 1.

    The injected runner stamps the feedback ``injected-runner``, which is not
    in REAL_FEEDBACK_SOURCES.  This means feedback_blocking_issues() returns a
    non-empty list, and the round is not trustworthy.  --strict treats this as a
    failure, which is the correct behaviour: an untrustworthy round should not
    silently pass.
    """
    _write_derived_spec(tmp_path)
    _write_minimal_test_spec(tmp_path)
    summary_path = _write_offline_summary(tmp_path)

    # The injected runner always produces blocking issues (source="injected-runner").
    rc = main(
        [
            str(summary_path),
            "--org-alias",
            "test-dev-org",
            "--agent-api-name",
            _AGENT_API_NAME,
            "--strict",
        ],
        _runner=_injected_runner(_FAKE_EVAL_RESPONSE),
    )
    assert rc == 1


# ---------------------------------------------------------------------------
# 4. Without --strict, a failing round does NOT change the exit code
# ---------------------------------------------------------------------------


def test_without_strict_failing_round_exits_0(tmp_path: Path) -> None:
    """Absent --strict, a failing round does not alter the exit code.

    The caller decides whether a failing stage-5 round is fatal. This preserves
    the original CI gate behaviour: the offline checks exit 0, and the org round
    is advisory unless --strict is passed.
    """
    _write_derived_spec(tmp_path)
    _write_minimal_test_spec(tmp_path)
    summary_path = _write_offline_summary(tmp_path)

    rc = main(
        [
            str(summary_path),
            "--org-alias",
            "test-dev-org",
            "--agent-api-name",
            _AGENT_API_NAME,
            # NO --strict
        ],
        _runner=_injected_runner(_FAKE_EVAL_RESPONSE_FAILING),
    )
    assert rc == 0


# ---------------------------------------------------------------------------
# 5. Contract-check helpers: direct unit tests
# ---------------------------------------------------------------------------


def test_check_offline_contracts_clean_summary_returns_no_failures() -> None:
    summary = _offline_summary()
    failures = _check_offline_contracts(summary)
    assert failures == []


def test_check_offline_contracts_detects_salesforce_validated_true() -> None:
    summary = _offline_summary()
    summary["salesforce_validated"] = True
    failures = _check_offline_contracts(summary)
    assert any("salesforce_validated" in f for f in failures)


def test_check_offline_contracts_detects_missing_pass_stages() -> None:
    summary = _offline_summary()
    # Remove s2_derive_names stage entirely
    summary["stages"] = [s for s in summary["stages"] if s["stage"] != "s2_derive_names"]
    failures = _check_offline_contracts(summary)
    assert any("s2_derive_names" in f for f in failures)


def test_check_name_contracts_clean_names_returns_no_failures() -> None:
    summary = _offline_summary()
    failures, names = _check_name_contracts(summary)
    assert failures == []
    assert names["agent_api_name"] == _AGENT_API_NAME


def test_check_name_contracts_detects_mismatched_subject_name() -> None:
    summary = _offline_summary()
    summary["derived_names"]["test_subject_name"] = "WrongAgent"
    failures, _ = _check_name_contracts(summary)
    assert any("subjectName" in f or "test spec" in f.lower() for f in failures)


def test_check_name_contracts_detects_wrong_router_action() -> None:
    summary = _offline_summary()
    summary["derived_names"]["router_action"] = "go_to_something_else"
    failures, _ = _check_name_contracts(summary)
    assert any("router action" in f for f in failures)

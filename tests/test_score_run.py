from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "score_run.py"

ALL_GATES_PASSING = {
    "preflight_ok": True,
    "execution_ok": True,
    "telemetry_ok": True,
    "artifacts_ok": True,
    "negative_tests_ok": True,
    "critical_issue": False,
}

GOOD_SPEC = {
    "intent": "Update Opportunity (StageName)",
    "confidence": 0.7,
    "objects_touched": ["Opportunity"],
    "entities": [{"name": "stageName", "field_api_name": "StageName"}],
    "unknowns": [],
    "provenance": {"telemetry_source": "live-org"},
}


def _run(tmp_path: Path, summary: dict, spec: dict | None, html: str = "clean report") -> dict:
    out_dir = tmp_path
    (out_dir / "mock_blueprint.html").write_text(html, encoding="utf-8")
    (out_dir / "live_blueprint.html").write_text(html, encoding="utf-8")
    if spec is not None:
        (out_dir / "live_blueprint.agent-spec.json").write_text(json.dumps(spec), encoding="utf-8")
    summary_path = out_dir / "run_summary.json"
    summary_path.write_text(json.dumps(summary), encoding="utf-8")

    proc = subprocess.run(
        [sys.executable, str(SCRIPT), str(summary_path), str(out_dir)],
        capture_output=True,
        text=True,
        check=False,
    )
    payload = json.loads(proc.stdout)
    payload["_returncode"] = proc.returncode
    return payload


def test_clean_derived_run_passes(tmp_path: Path) -> None:
    result = _run(tmp_path, ALL_GATES_PASSING, GOOD_SPEC)
    assert result["pass"] is True
    assert result["_returncode"] == 0
    assert result["blocking_issues"] == []


def test_placeholder_content_blocks_pass(tmp_path: Path) -> None:
    """The core regression: a run full of stub content must not score as passing."""
    result = _run(
        tmp_path,
        ALL_GATES_PASSING,
        GOOD_SPEC,
        html="<p>Update case status from UI workflow</p><p>Sample_Flow</p>",
    )
    assert result["pass"] is False
    assert result["_returncode"] != 0
    assert any("placeholder" in issue for issue in result["blocking_issues"])


def test_mock_telemetry_spec_blocks_pass(tmp_path: Path) -> None:
    spec = dict(GOOD_SPEC, provenance={"telemetry_source": "mock"})
    result = _run(tmp_path, ALL_GATES_PASSING, spec)
    assert result["pass"] is False
    assert any("mock telemetry" in issue for issue in result["blocking_issues"])


def test_missing_spec_blocks_pass(tmp_path: Path) -> None:
    result = _run(tmp_path, ALL_GATES_PASSING, None)
    assert result["pass"] is False
    assert result["gates"]["spec_derived_ok"] is False


def test_unresolved_intent_blocks_pass(tmp_path: Path) -> None:
    spec = dict(GOOD_SPEC, intent="UNRESOLVED: nothing observed", confidence=0.05)
    result = _run(tmp_path, ALL_GATES_PASSING, spec)
    assert result["pass"] is False


def test_spec_with_no_objects_blocks_pass(tmp_path: Path) -> None:
    spec = dict(GOOD_SPEC, objects_touched=[])
    result = _run(tmp_path, ALL_GATES_PASSING, spec)
    assert result["pass"] is False
    assert any("no Salesforce object" in issue for issue in result["blocking_issues"])


def test_critical_issue_actually_blocks(tmp_path: Path) -> None:
    """Previously unfailable: critical_issue was a hardcoded False literal."""
    summary = dict(ALL_GATES_PASSING, critical_issue=True)
    result = _run(tmp_path, summary, GOOD_SPEC)
    assert result["pass"] is False
    assert any("critical_issue" in issue for issue in result["blocking_issues"])


def test_failed_preflight_lowers_score(tmp_path: Path) -> None:
    summary = dict(ALL_GATES_PASSING, preflight_ok=False)
    result = _run(tmp_path, summary, GOOD_SPEC)
    assert result["gates"]["preflight_ok"] is False
    assert result["score"] < result["max_score"]

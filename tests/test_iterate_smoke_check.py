"""Tests for scripts/iterate_smoke_check.py.

These tests cover all three contracts the checker enforces:

1. At least one versioned spec must exist (v1/agent-spec.json).
2. Every versioned spec with a 'provenance' key must carry an honest stamp;
   missing provenance is warned, not failed.
3. The iteration report must be present, valid JSON, and structurally sound
   (rounds_run > 0, versions non-empty, stop_reason present).

All tests run without a live org, LLM, or network.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

# Make scripts/ importable
REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = REPO_ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))
sys.path.insert(0, str(REPO_ROOT / "src"))

from iterate_smoke_check import check_iterate_output, main  # type: ignore[import]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _write_versioned_spec(
    out_dir: Path,
    version: int,
    intent: str = "Update Case Status",
    provenance: dict | None | str = "include",
) -> Path:
    """Write a minimal v<N>/agent-spec.json for testing.

    provenance="include" -> include a real provenance block
    provenance=None       -> omit the 'provenance' key
    provenance=dict(...)  -> use that dict
    """
    vdir = out_dir / f"v{version}"
    vdir.mkdir(parents=True, exist_ok=True)
    spec: dict = {
        "intent": intent,
        "confidence": 0.7,
        "objects_touched": ["Case"],
        "entities": [],
        "orchestration_steps": ["Load Case", "Update Status"],
        "guardrails": ["Require confirmation"],
        "failure_handling": ["Log failure"],
        "unknowns": [],
        "evidence": [],
    }
    if provenance == "include":
        spec["provenance"] = {
            "extraction_source": "dom-capture",
            "telemetry_source": "mock",
        }
    elif isinstance(provenance, dict):
        spec["provenance"] = provenance
    # else: provenance is None -> don't add the key

    spec_path = vdir / "agent-spec.json"
    spec_path.write_text(json.dumps(spec, indent=2) + "\n", encoding="utf-8")
    return spec_path


def _write_report(
    out_dir: Path,
    rounds_run: int = 1,
    stop_reason: str = "Reached max_rounds=3",
    versions: list[dict] | None = None,
) -> Path:
    """Write a minimal iteration_report.json for testing."""
    if versions is None:
        versions = [
            {
                "version": i + 1,
                "score_total": 60,
                "score_max": 100,
                "score_band": "medium",
                "passed": False,
                "blocking_issues": ["mock telemetry"],
                "recommendations": [],
                "stop_reason": None,
            }
            for i in range(rounds_run)
        ]
    report = {
        "rounds_run": rounds_run,
        "converged": False,
        "stop_reason": stop_reason,
        "best_version": 1,
        "versions": versions,
    }
    report_path = out_dir / "iteration_report.json"
    report_path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    return report_path


# ---------------------------------------------------------------------------
# Contract 1: at least one versioned spec
# ---------------------------------------------------------------------------

class TestContract1VersionedSpec:
    def test_fails_when_no_v1_spec(self, tmp_path: Path) -> None:
        """No v1/agent-spec.json -> contract violation."""
        failures = check_iterate_output(tmp_path)
        assert any("v1/agent-spec.json" in f for f in failures), failures

    def test_passes_when_v1_spec_exists(self, tmp_path: Path) -> None:
        """v1/agent-spec.json present -> contract 1 satisfied."""
        _write_versioned_spec(tmp_path, 1)
        _write_report(tmp_path)
        failures = check_iterate_output(tmp_path)
        assert not any("versioned spec found at" in f for f in failures), failures

    def test_returns_early_when_v1_missing(self, tmp_path: Path) -> None:
        """When v1 is absent, early return — only one failure."""
        failures = check_iterate_output(tmp_path)
        assert len(failures) == 1, f"expected 1 failure, got: {failures}"


# ---------------------------------------------------------------------------
# Contract 2: provenance is present and propagated
# ---------------------------------------------------------------------------

class TestContract2Provenance:
    def test_passes_with_provenance(self, tmp_path: Path) -> None:
        """Spec with provenance block -> no provenance violation."""
        _write_versioned_spec(tmp_path, 1, provenance="include")
        _write_report(tmp_path)
        failures = check_iterate_output(tmp_path)
        assert not any("provenance" in f.lower() for f in failures), failures

    def test_no_failure_on_missing_provenance_key(self, tmp_path: Path) -> None:
        """Missing provenance key is a WARNING, not a contract failure."""
        _write_versioned_spec(tmp_path, 1, provenance=None)
        _write_report(tmp_path)
        failures = check_iterate_output(tmp_path)
        # Missing provenance should not appear as a hard failure
        assert not any("provenance" in f for f in failures), (
            "missing provenance key should warn, not fail"
        )

    def test_multiple_versions_all_checked(self, tmp_path: Path) -> None:
        """All versioned specs are inspected."""
        _write_versioned_spec(tmp_path, 1, provenance="include")
        _write_versioned_spec(tmp_path, 2, provenance="include")
        _write_versioned_spec(tmp_path, 3, provenance="include")
        _write_report(tmp_path, rounds_run=3)
        failures = check_iterate_output(tmp_path)
        assert not failures, failures

    def test_invalid_json_in_versioned_spec(self, tmp_path: Path) -> None:
        """Invalid JSON in a versioned spec is a contract violation."""
        vdir = tmp_path / "v1"
        vdir.mkdir(parents=True)
        (vdir / "agent-spec.json").write_text("not json!!!", encoding="utf-8")
        _write_report(tmp_path)
        failures = check_iterate_output(tmp_path)
        assert any("not valid JSON" in f for f in failures), failures

    def test_vdir_exists_but_spec_missing(self, tmp_path: Path) -> None:
        """v<N>/ directory with no agent-spec.json inside is a violation.

        The checker tests for the file directly; the message is the same
        whether or not the directory itself exists.
        """
        (tmp_path / "v1").mkdir()  # dir exists but no agent-spec.json
        _write_report(tmp_path)
        failures = check_iterate_output(tmp_path)
        # The checker reports that no v1/agent-spec.json exists — whether the
        # directory is there or not does not change the contract violation.
        assert len(failures) >= 1, "expected at least one violation"
        assert any(
            "v1" in f and "agent-spec.json" in f for f in failures
        ), failures


# ---------------------------------------------------------------------------
# Contract 3: iteration report
# ---------------------------------------------------------------------------

class TestContract3IterationReport:
    def test_fails_when_report_missing(self, tmp_path: Path) -> None:
        """No iteration_report.json -> contract violation."""
        _write_versioned_spec(tmp_path, 1)
        # Don't write a report
        failures = check_iterate_output(tmp_path)
        assert any("iteration_report.json" in f for f in failures), failures

    def test_passes_with_valid_report(self, tmp_path: Path) -> None:
        """Valid report -> no contract violations."""
        _write_versioned_spec(tmp_path, 1)
        _write_report(tmp_path, rounds_run=1)
        failures = check_iterate_output(tmp_path)
        assert not failures, failures

    def test_fails_when_report_rounds_zero(self, tmp_path: Path) -> None:
        """rounds_run=0 in report but specs exist -> inconsistency violation."""
        _write_versioned_spec(tmp_path, 1)
        _write_report(tmp_path, rounds_run=0, versions=[])
        failures = check_iterate_output(tmp_path)
        assert any("rounds_run=0" in f or "inconsistent" in f.lower() for f in failures), failures

    def test_fails_when_report_missing_stop_reason(self, tmp_path: Path) -> None:
        """Report without stop_reason -> contract violation."""
        _write_versioned_spec(tmp_path, 1)
        report = {
            "rounds_run": 1,
            "converged": False,
            "best_version": 1,
            "versions": [{"version": 1, "score_total": 60}],
            # no 'stop_reason'
        }
        (tmp_path / "iteration_report.json").write_text(
            json.dumps(report, indent=2), encoding="utf-8"
        )
        failures = check_iterate_output(tmp_path)
        assert any("stop_reason" in f for f in failures), failures

    def test_fails_when_report_versions_empty(self, tmp_path: Path) -> None:
        """versions=[] in report -> contract violation."""
        _write_versioned_spec(tmp_path, 1)
        report = {
            "rounds_run": 1,
            "converged": False,
            "stop_reason": "Reached max_rounds=3",
            "best_version": 1,
            "versions": [],  # empty
        }
        (tmp_path / "iteration_report.json").write_text(
            json.dumps(report, indent=2), encoding="utf-8"
        )
        failures = check_iterate_output(tmp_path)
        assert any("versions" in f for f in failures), failures

    def test_fails_when_report_invalid_json(self, tmp_path: Path) -> None:
        """Corrupt JSON in iteration report -> violation."""
        _write_versioned_spec(tmp_path, 1)
        (tmp_path / "iteration_report.json").write_text(
            "{bad json!", encoding="utf-8"
        )
        failures = check_iterate_output(tmp_path)
        assert any("not valid JSON" in f for f in failures), failures

    def test_fails_when_report_missing_rounds_run(self, tmp_path: Path) -> None:
        """Report missing rounds_run key -> violation."""
        _write_versioned_spec(tmp_path, 1)
        report = {
            # no 'rounds_run'
            "converged": False,
            "stop_reason": "Reached max_rounds=3",
            "best_version": 1,
            "versions": [{"version": 1, "score_total": 60}],
        }
        (tmp_path / "iteration_report.json").write_text(
            json.dumps(report, indent=2), encoding="utf-8"
        )
        failures = check_iterate_output(tmp_path)
        assert any("rounds_run" in f for f in failures), failures


# ---------------------------------------------------------------------------
# main() CLI tests
# ---------------------------------------------------------------------------

class TestMain:
    def test_main_exits_2_when_dir_missing(self, tmp_path: Path) -> None:
        """Passing a non-existent directory returns exit code 2."""
        rc = main([str(tmp_path / "no_such_dir")])
        assert rc == 2

    def test_main_exits_1_on_violations(self, tmp_path: Path) -> None:
        """Violations -> exit 1."""
        # empty dir -> no v1/agent-spec.json
        rc = main([str(tmp_path)])
        assert rc == 1

    def test_main_exits_0_on_clean_output(self, tmp_path: Path) -> None:
        """All contracts satisfied -> exit 0."""
        _write_versioned_spec(tmp_path, 1)
        _write_report(tmp_path, rounds_run=1)
        rc = main([str(tmp_path)])
        assert rc == 0

    def test_main_writes_result_json(self, tmp_path: Path) -> None:
        """--out writes a JSON result file."""
        _write_versioned_spec(tmp_path, 1)
        _write_report(tmp_path, rounds_run=1)
        result_file = tmp_path / "result.json"
        rc = main([str(tmp_path), "--out", str(result_file)])
        assert rc == 0
        assert result_file.is_file()
        result = json.loads(result_file.read_text(encoding="utf-8"))
        assert result["passed"] is True
        assert result["violations"] == []

    def test_main_result_json_captures_violations(self, tmp_path: Path) -> None:
        """--out captures violation list when contracts fail."""
        result_file = tmp_path / "result.json"
        rc = main([str(tmp_path), "--out", str(result_file)])
        assert rc == 1
        assert result_file.is_file()
        result = json.loads(result_file.read_text(encoding="utf-8"))
        assert result["passed"] is False
        assert len(result["violations"]) >= 1

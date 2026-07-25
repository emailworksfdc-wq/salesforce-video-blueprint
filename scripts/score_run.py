#!/usr/bin/env python3
"""Score a validation run.

Design rule: this score must be able to FAIL. The previous version summed five
booleans, two of which (``preflight_ok``, ``critical_issue``) were hardcoded
literals in the caller, while the rest only asserted that files existed and that
the org answered an API probe. A run emitting entirely placeholder content
therefore scored 100/100 "pass", and the process always exited 0.

Presence is not correctness. This version adds two gates that inspect the actual
output, treats placeholder/simulated content as blocking, and exits non-zero when
the run does not pass.
"""
from __future__ import annotations

import json
from pathlib import Path
import sys

# Strings that indicate stub/sample content rather than observed org data.
PLACEHOLDER_MARKERS = (
    "Sample_Flow",
    "button:Save",
    "500xx0000012345AAA",
    "Update case status from UI workflow",
    "Heuristic extraction in use",
    "UNRESOLVED:",
)

WEIGHTS = {
    "preflight_ok": 10,
    "execution_ok": 15,
    "telemetry_ok": 15,
    "artifacts_ok": 15,
    "negative_tests_ok": 10,
    "evidence_is_real_ok": 20,
    "spec_derived_ok": 15,
}

PASS_THRESHOLD = 85


def _scan_placeholders(paths: list[Path]) -> list[str]:
    hits: list[str] = []
    for path in paths:
        if not path.exists():
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for marker in PLACEHOLDER_MARKERS:
            if marker in text:
                hits.append(f"{path.name}: {marker!r}")
    return hits


def _spec_quality(spec_path: Path) -> tuple[bool, list[str]]:
    """A spec is only usable if it was derived and names concrete targets."""
    if not spec_path.exists():
        return False, [f"no agent spec emitted at {spec_path}"]
    try:
        spec = json.loads(spec_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return False, [f"agent spec unreadable: {exc}"]

    problems: list[str] = []
    if spec.get("provenance", {}).get("telemetry_source") != "live-org":
        problems.append("spec was built from mock telemetry, not a live org")
    if not spec.get("objects_touched"):
        problems.append("spec names no Salesforce object (no data delta observed)")
    if not spec.get("entities"):
        problems.append("spec derived no input entities")
    if str(spec.get("intent", "")).startswith("UNRESOLVED"):
        problems.append("spec intent is unresolved")
    if float(spec.get("confidence", 0.0)) < 0.5:
        problems.append(f"spec confidence too low: {spec.get('confidence')}")
    if spec.get("unknowns"):
        problems.append(f"{len(spec['unknowns'])} unknown(s) recorded in spec")
    return not problems, problems


def main() -> int:
    if len(sys.argv) < 2:
        print("Usage: score_run.py <run_summary.json> [out_dir]")
        return 2

    path = Path(sys.argv[1])
    if not path.exists():
        print(f"Missing input file: {path}")
        return 2

    data = json.loads(path.read_text(encoding="utf-8"))
    out_dir = Path(sys.argv[2]) if len(sys.argv) > 2 else path.parent

    blueprints = [out_dir / "mock_blueprint.html", out_dir / "live_blueprint.html"]
    spec_path = out_dir / "live_blueprint.agent-spec.json"

    placeholder_hits = _scan_placeholders(blueprints + [spec_path])
    evidence_is_real = not placeholder_hits
    spec_ok, spec_problems = _spec_quality(spec_path)

    gates = {
        "preflight_ok": bool(data.get("preflight_ok", False)),
        "execution_ok": bool(data.get("execution_ok", False)),
        "telemetry_ok": bool(data.get("telemetry_ok", False)),
        "artifacts_ok": bool(data.get("artifacts_ok", False)),
        "negative_tests_ok": bool(data.get("negative_tests_ok", False)),
        "evidence_is_real_ok": evidence_is_real,
        "spec_derived_ok": spec_ok,
    }

    score = sum(WEIGHTS[name] for name, ok in gates.items() if ok)

    blocking: list[str] = []
    if data.get("critical_issue", False):
        blocking.append("critical_issue flagged by the run")
    if not evidence_is_real:
        blocking.append(
            "output contains placeholder/simulated content: " + "; ".join(placeholder_hits[:6])
        )
    blocking.extend(spec_problems)

    result = {
        "score": score,
        "max_score": sum(WEIGHTS.values()),
        "band": "high" if score >= PASS_THRESHOLD else "medium" if score >= 60 else "low",
        # A run that emits placeholder content can never pass, whatever the score.
        "pass": score >= PASS_THRESHOLD and not blocking,
        "gates": gates,
        "blocking_issues": blocking,
    }
    print(json.dumps(result, indent=2))
    # Exit non-zero on failure so the calling script's `set -e` actually trips.
    return 0 if result["pass"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

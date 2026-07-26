#!/usr/bin/env python3
"""CI contract check: the pipeline must emit a derived spec, and the score gate
must REFUSE it when telemetry is mock.

This is not a unit test; it verifies the end-to-end binary on a real capture
file. It exists because two properties are easy to break invisibly:

1. **Provenance stamping.** CI has no Salesforce org, so a run here MUST label
   itself `telemetry_source: "mock"`. If it ever claims `live-org`, provenance
   is being set from a flag rather than from evidence, and every downstream
   honesty guarantee is void.

2. **Gate strength.** A spec built from mock telemetry must not pass, no matter
   how complete it looks. If this script starts reporting `passed=True`, the
   gate has been weakened — which this project treats as a defect, not a fix.

Usage:
    python scripts/ci_smoke_check.py outputs/ci_smoke.agent-spec.json

Exits non-zero on any contract violation so `set -e` in the caller trips.
"""

from __future__ import annotations

import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from sf_video_blueprint.spec_score import PASS_THRESHOLD, score_spec_file  # noqa: E402


def main() -> int:
    if len(sys.argv) < 2:
        print("usage: ci_smoke_check.py <agent-spec.json>", file=sys.stderr)
        return 2

    spec_path = Path(sys.argv[1])
    if not spec_path.is_file():
        print(f"FAIL: no agent spec emitted at {spec_path}", file=sys.stderr)
        return 1

    failures: list[str] = []
    spec = json.loads(spec_path.read_text(encoding="utf-8"))

    # --- Contract 1: provenance reflects reality -----------------------------
    provenance = spec.get("provenance") or {}
    print(f"provenance: {json.dumps(provenance, sort_keys=True)}")

    extraction = provenance.get("extraction_source")
    telemetry = provenance.get("telemetry_source")

    if extraction != "dom-capture":
        failures.append(
            f"extraction_source is {extraction!r}, expected 'dom-capture' "
            "(the smoke run feeds a real JSONL capture)"
        )
    if telemetry != "mock":
        failures.append(
            f"telemetry_source is {telemetry!r}, expected 'mock'. CI has no org, "
            "so any other value means provenance is stamped from a flag rather "
            "than from evidence."
        )

    if not spec.get("intent"):
        failures.append("no intent was derived from the capture")
    else:
        print(f"intent: {spec['intent']}")

    # --- Contract 2: the gate refuses mock telemetry -------------------------
    result = score_spec_file(spec_path)
    print(
        f"score: {result.total}/{result.max_total}  band={result.band}  "
        f"passed={result.passed}  (threshold {PASS_THRESHOLD})"
    )
    for name, dimension in result.dimensions.items():
        print(f"  {name:24} {dimension.score:>3}/{dimension.max_score}")
    for issue in result.blocking_issues:
        print(f"  BLOCKED: {issue}")

    if result.passed:
        failures.append(
            "a spec built from MOCK telemetry passed the quality gate. The gate "
            "has been weakened; that is a defect, not a fix. See CONTRIBUTING.md."
        )

    if failures:
        print("\nCONTRACT VIOLATIONS:", file=sys.stderr)
        for failure in failures:
            print(f"  - {failure}", file=sys.stderr)
        return 1

    print("\nOK: spec derived, provenance honest, gate correctly refused mock telemetry.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

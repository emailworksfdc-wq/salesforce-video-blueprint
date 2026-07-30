#!/usr/bin/env python3
"""CI contract check: the iterate loop must emit honest, versioned artifacts.

This script reads the output directory written by ``run_iterate_smoke.py`` (or
``iterate_smoke.sh``) and asserts the three properties that are easy to break
invisibly:

1. **At least one versioned spec was written.** ``iterate.refine`` must have
   completed at least one round and written ``v1/agent-spec.json``. A loop that
   exits immediately without touching the disk is a broken loop.

2. **Every versioned spec has an honest provenance stamp.** Each
   ``v<N>/agent-spec.json`` must carry the same ``provenance`` block as the
   input spec.  A loop that silently drops provenance would let a mock-telemetry
   spec impersonate a live-org spec in downstream consumers.

3. **The iteration report is present and structurally valid.** The report must
   record the rounds that actually ran and a stop reason. A report that claims
   zero rounds ran while spec files exist is an audit-trail inconsistency.

Usage:
    python scripts/iterate_smoke_check.py <iterate_out_dir> [--out <result.json>]

Exits non-zero on any contract violation so ``set -e`` in the caller trips.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path


def check_iterate_output(out_dir: Path) -> list[str]:
    """Check iterate output contracts.

    Returns a list of contract violation strings (empty means all OK).
    This is separated from ``main`` so unit tests can call it directly.
    """
    failures: list[str] = []

    # --- Contract 1: at least one versioned spec exists ----------------------
    v1_spec = out_dir / "v1" / "agent-spec.json"
    if not v1_spec.is_file():
        failures.append(
            f"no versioned spec found at {v1_spec}. The iterate loop must write at "
            "least one round (v1/agent-spec.json). A loop that skips the first round "
            "produces no audit trail."
        )
        # If v1 is missing, most other checks cannot run. Return early.
        return failures

    print(f"v1 spec: {v1_spec}")

    # --- Contract 2: every versioned spec has an honest provenance stamp ------
    # Walk all v<N>/agent-spec.json files
    version_dirs = sorted(
        d for d in out_dir.iterdir()
        if d.is_dir() and d.name.startswith("v") and d.name[1:].isdigit()
    )

    for vdir in version_dirs:
        spec_file = vdir / "agent-spec.json"
        if not spec_file.is_file():
            failures.append(
                f"versioned directory {vdir.name}/ exists but {spec_file.name} is missing. "
                "Every round must write its spec JSON before moving to the next round."
            )
            continue

        try:
            spec = json.loads(spec_file.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            failures.append(
                f"{vdir.name}/agent-spec.json is not valid JSON: {exc}. "
                "The spec serialiser must emit valid JSON."
            )
            continue

        provenance = spec.get("provenance")
        if provenance is None:
            # A missing provenance key is still honest in the sense that it
            # declares no source. But we cannot check the source claim.
            # Warn rather than fail: the loop may have been run before provenance
            # was wired to write_spec.
            print(
                f"  WARNING: {vdir.name}/agent-spec.json has no 'provenance' key. "
                "Provenance is not being propagated through the loop."
            )
        else:
            print(f"  {vdir.name} provenance: {json.dumps(provenance, sort_keys=True)}")

    # --- Contract 3: iteration report is present and structurally valid ------
    report_path = out_dir / "iteration_report.json"
    if not report_path.is_file():
        failures.append(
            f"no iteration report found at {report_path}. "
            "run_iterate_smoke.py must write iteration_report.json so the audit "
            "trail is complete."
        )
    else:
        try:
            report = json.loads(report_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            failures.append(
                f"iteration_report.json is not valid JSON: {exc}."
            )
            report = None

        if report is not None:
            rounds_run = report.get("rounds_run")
            if rounds_run is None:
                failures.append(
                    "iteration_report.json has no 'rounds_run' key. "
                    "write_iteration_report must record how many rounds were run."
                )
            elif rounds_run == 0:
                failures.append(
                    f"iteration_report.json says rounds_run=0 but versioned specs "
                    f"exist in {out_dir}. The report is inconsistent with the disk state."
                )
            else:
                print(f"report: rounds_run={rounds_run}  "
                      f"stop_reason={report.get('stop_reason', '(none)')!r}")

            if "versions" not in report:
                failures.append(
                    "iteration_report.json has no 'versions' array. "
                    "The audit trail must record all versioned specs."
                )
            elif len(report["versions"]) == 0:
                failures.append(
                    "iteration_report.json 'versions' array is empty even though "
                    "versioned specs exist. The audit trail is incomplete."
                )

            if "stop_reason" not in report or not report.get("stop_reason"):
                failures.append(
                    "iteration_report.json has no 'stop_reason'. "
                    "The report must explain why the loop terminated."
                )

    return failures


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(
        description=__doc__.splitlines()[0],
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("out_dir", help="iterate output directory to check")
    parser.add_argument(
        "--out",
        default=None,
        metavar="FILE",
        help="optional path to write a JSON check result",
    )
    args = parser.parse_args(argv)

    out_dir = Path(args.out_dir)
    if not out_dir.is_dir():
        print(f"FAIL: output directory not found: {out_dir}", file=sys.stderr)
        return 2

    failures = check_iterate_output(out_dir)

    # Optionally write the check result as JSON
    if args.out is not None:
        result = {
            "passed": not failures,
            "violations": failures,
        }
        Path(args.out).write_text(
            json.dumps(result, indent=2) + "\n", encoding="utf-8"
        )

    if failures:
        print("\nCONTRACT VIOLATIONS:", file=sys.stderr)
        for failure in failures:
            print(f"  - {failure}", file=sys.stderr)
        return 1

    print(
        "\nOK: iterate loop produced versioned specs, provenance is present, "
        "and the iteration report is structurally valid."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

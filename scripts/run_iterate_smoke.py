#!/usr/bin/env python3
"""Run the iterate refinement loop offline from a derived agent spec.

This wrapper drives the offline :func:`iterate.refine` loop from a spec file
produced by the CLI pipeline (``sf-blueprint run --spec-output ...``). It is
the Python entry-point used by ``iterate_smoke.sh`` so the shell script stays
short and the Python path is set correctly.

It does NOT use the CLI back-end (``use_cli=False``): no org, no LLM, no
network. It is the cheapest way to verify the loop is wired up.

Usage:
    python scripts/run_iterate_smoke.py <spec.json> --out <dir> [--max-rounds N]

The script writes:
  <out>/v1/agent-spec.json   — first round spec
  <out>/v1/agentSpec.yaml    — first round YAML
  <out>/iteration_report.json — machine-readable summary
  <out>/iteration_report.md   — human-readable summary

Exits non-zero if the loop fails to produce at least one versioned spec, or if
any fatal error occurs. It does NOT exit non-zero when the score gate refuses
the spec: the gate refusing a mock-telemetry run is the gate working correctly.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# Make src/ importable without installing the package
_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT / "src"))


def _load_spec(spec_path: Path) -> "DerivedAgentSpec":  # type: ignore[name-defined]
    """Load a DerivedAgentSpec from a JSON spec file emitted by the CLI."""
    from sf_video_blueprint.spec_builder import (
        DerivedAgentSpec,
        DerivedEntity,
        SpecEvidence,
    )

    raw = json.loads(spec_path.read_text(encoding="utf-8"))

    # Reconstruct entities
    entities = []
    for e in raw.get("entities", []):
        evidence = [
            SpecEvidence(
                source=ev.get("source", "inference"),
                detail=ev.get("detail", ev.get("description", "")),
            )
            for ev in (e.get("evidence") or [])
        ]
        entities.append(
            DerivedEntity(
                name=e.get("name", ""),
                object_api_name=e.get("object_api_name", ""),
                field_api_name=e.get("field_api_name", ""),
                evidence=evidence,
            )
        )

    # Reconstruct top-level evidence
    top_evidence = [
        SpecEvidence(
            source=ev.get("source", "inference"),
            detail=ev.get("detail", ev.get("description", "")),
        )
        for ev in (raw.get("evidence") or [])
    ]

    spec = DerivedAgentSpec(
        intent=raw.get("intent", ""),
        confidence=raw.get("confidence", 0.5),
        objects_touched=raw.get("objects_touched", []),
        entities=entities,
        orchestration_steps=raw.get("orchestration_steps", []),
        guardrails=raw.get("guardrails", []),
        failure_handling=raw.get("failure_handling", []),
        unknowns=raw.get("unknowns", []),
        evidence=top_evidence,
    )
    return spec


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__.splitlines()[0],
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("spec", help="path to the derived agent-spec.json")
    parser.add_argument(
        "--out",
        default="./outputs/iterate_smoke",
        metavar="DIR",
        help="output directory for versioned specs and reports (default: %(default)s)",
    )
    parser.add_argument(
        "--max-rounds",
        type=int,
        default=3,
        metavar="N",
        help="maximum offline refinement rounds (default: %(default)s)",
    )
    parser.add_argument(
        "--company-name",
        default="Acme Corp",
        metavar="NAME",
        help="company name for the emitted spec YAML (default: %(default)s)",
    )
    parser.add_argument(
        "--company-description",
        default="A sample company for smoke testing.",
        metavar="DESC",
        help="company description for the emitted spec YAML",
    )

    args = parser.parse_args(argv)

    spec_path = Path(args.spec)
    if not spec_path.is_file():
        print(f"FAIL: spec file not found: {spec_path}", file=sys.stderr)
        return 2

    out_dir = Path(args.out).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    # Load the spec
    try:
        spec = _load_spec(spec_path)
    except Exception as exc:  # noqa: BLE001
        print(f"FAIL: could not load spec from {spec_path}: {exc}", file=sys.stderr)
        return 1

    print(f"spec loaded: intent={spec.intent!r}")

    # Load provenance from the spec file
    provenance: dict[str, str] = {}
    try:
        raw = json.loads(spec_path.read_text(encoding="utf-8"))
        provenance = raw.get("provenance") or {}
    except Exception:  # noqa: BLE001
        pass
    if provenance:
        print(f"provenance: {json.dumps(provenance, sort_keys=True)}")

    # Import iterate
    try:
        from sf_video_blueprint.iterate import refine, write_iteration_report
    except ImportError as exc:
        print(f"FAIL: cannot import iterate: {exc}", file=sys.stderr)
        return 1

    # Run the offline loop
    print(f"running iterate.refine (max_rounds={args.max_rounds}, use_cli=False) ...")
    try:
        result = refine(
            spec,
            out_dir=out_dir,
            company_name=args.company_name,
            company_description=args.company_description,
            max_rounds=args.max_rounds,
            use_cli=False,
            provenance=provenance,
        )
    except Exception as exc:  # noqa: BLE001
        print(f"FAIL: iterate.refine raised {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1

    print(f"rounds_run={result.rounds_run}  stop_reason={result.stop_reason!r}")
    print(f"best_version=v{result.best.version}  "
          f"score={result.best.score.total}/{result.best.score.max_total}  "
          f"band={result.best.score.band}  "
          f"passed={result.best.score.passed}")
    if result.best.score.blocking_issues:
        print("blocking issues on best version:")
        for issue in result.best.score.blocking_issues:
            print(f"  - {issue}")

    if not result.versions:
        print("FAIL: no versioned specs were produced", file=sys.stderr)
        return 1

    # Write the iteration report
    report_path = out_dir / "iteration_report"
    write_iteration_report(report_path, result)
    print(f"report: {report_path}.json")

    print(
        f"\nOK: iterate loop completed — {result.rounds_run} round(s), "
        f"stop_reason={result.stop_reason!r}."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

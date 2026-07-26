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

CRITICAL FIX: The CI gate must be NO WEAKER than the in-process scorer. When the
spec JSON exists, this script invokes spec_score.score_spec_file() and treats its
blocking_issues as authoritative. A spec that fails in-process must NOT pass CI.
"""
from __future__ import annotations

import json
from pathlib import Path
import sys
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

# Single source of truth, shared with spec_score.py. These lists had drifted, so
# a spec containing "TODO" failed one scorer and passed the other; a gate that
# disagrees with itself is not a gate.
#
# Note what is NOT here any more: "button:Save". It was a proxy for the stub
# extractor, but a real DOM capture of an operator clicking Save produces exactly
# that string as observed evidence. Keeping it would have failed every genuine run
# the moment Step 5 started working. It is replaced by STUB_FINGERPRINTS (strings
# only the stub can emit) plus the structural extraction_source check below —
# catching the stub by identity rather than by a string it shares with real data.
from sf_video_blueprint.markers import (  # noqa: E402
    PLACEHOLDER_MARKERS,
    STUB_FINGERPRINTS,
    extraction_is_real,
    telemetry_is_real,
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


def _normalize_provenance_source(source: Any) -> str | None:
    """Normalize provenance source to str, failing closed on non-string types.

    CRASH FIX: extraction_is_real/telemetry_is_real do `source in FROZENSET`,
    which raises TypeError if source is a list. Handle non-string defensively:
    a non-string source is treated as "not real" (fail closed).

    Args:
        source: The provenance source value (may be str, list, dict, None, etc.)

    Returns:
        The source as a string if it's a string, or None otherwise.
    """
    if isinstance(source, str):
        return source
    # Non-string source (list, dict, None, etc.) fails closed
    return None


def _scan_placeholders(paths: list[Path]) -> list[str]:
    hits: list[str] = []
    for path in paths:
        if not path.exists():
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for marker in (*PLACEHOLDER_MARKERS, *STUB_FINGERPRINTS):
            if marker in text:
                hits.append(f"{path.name}: {marker!r}")
    return hits


def _spec_quality(spec_path: Path) -> tuple[bool, list[str], dict[str, Any]]:
    """Invoke the in-process scorer to assess spec quality.

    Returns:
        Tuple of (passed, blocking_issues, full_score_result).

    CRITICAL FIX: This CI gate must be NO WEAKER than the in-process scorer.
    We invoke spec_score.score_spec_file() and treat its blocking_issues as
    authoritative. A spec that fails in-process MUST NOT pass CI.
    """
    if not spec_path.exists():
        return False, [f"no agent spec emitted at {spec_path}"], {}

    # Import the in-process scorer
    from sf_video_blueprint.spec_score import score_spec_file

    try:
        score_result = score_spec_file(spec_path)
    except Exception as exc:
        return False, [f"spec scoring failed: {exc}"], {}

    # The in-process scorer is the authoritative gate. If it says the spec fails,
    # CI must fail too.
    return score_result.passed, score_result.blocking_issues, score_result.to_dict()


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
    spec_ok, spec_problems, spec_score_detail = _spec_quality(spec_path)

    # CRITICAL FIX: Label self-reported gates so readers know they were not
    # independently verified. These are caller-asserted booleans from run_summary.json.
    gates = {
        "preflight_ok": bool(data.get("preflight_ok", False)),  # self-reported
        "execution_ok": bool(data.get("execution_ok", False)),  # self-reported
        "telemetry_ok": bool(data.get("telemetry_ok", False)),  # self-reported
        "artifacts_ok": bool(data.get("artifacts_ok", False)),  # self-reported
        "negative_tests_ok": bool(data.get("negative_tests_ok", False)),  # self-reported
        "evidence_is_real_ok": evidence_is_real,  # independently verified
        "spec_derived_ok": spec_ok,  # independently verified via spec_score.score_spec_file
    }

    score = sum(WEIGHTS[name] for name, ok in gates.items() if ok)

    blocking: list[str] = []
    if data.get("critical_issue", False):
        blocking.append("critical_issue flagged by the run (self-reported)")
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
        "spec_score_detail": spec_score_detail,  # full in-process scorer result
        "self_reported_gates": [
            "preflight_ok",
            "execution_ok",
            "telemetry_ok",
            "artifacts_ok",
            "negative_tests_ok",
            "critical_issue",
        ],
    }
    print(json.dumps(result, indent=2))
    # Exit non-zero on failure so the calling script's `set -e` actually trips.
    return 0 if result["pass"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

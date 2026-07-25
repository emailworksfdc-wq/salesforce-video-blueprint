#!/usr/bin/env python3
from __future__ import annotations

import json
from pathlib import Path
import sys


def bool_score(value: bool, points: int) -> int:
    return points if value else 0


def main() -> int:
    if len(sys.argv) != 2:
        print("Usage: score_run.py <run_summary.json>")
        return 2

    path = Path(sys.argv[1])
    if not path.exists():
        print(f"Missing input file: {path}")
        return 2

    data = json.loads(path.read_text(encoding="utf-8"))
    score = 0
    score += bool_score(data.get("preflight_ok", False), 20)
    score += bool_score(data.get("execution_ok", False), 20)
    score += bool_score(data.get("telemetry_ok", False), 20)
    score += bool_score(data.get("artifacts_ok", False), 20)
    score += bool_score(data.get("negative_tests_ok", False), 20)

    result = {
        "score": score,
        "band": "high" if score >= 85 else "medium" if score >= 70 else "low",
        "pass": score >= 85 and not data.get("critical_issue", False),
    }
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

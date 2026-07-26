#!/usr/bin/env python3
"""CI contract check: an offline round trip must not claim what it did not do.

`scripts/agentforce_roundtrip.sh` shipped in a state where it printed
"All executed stages PASSED" and wrote `{"pass": true}` while both of its
org-dependent stages were silently skipped, and where it used three different
agent names in one run so the emitted test suite targeted an agent no stage had
produced. Both defects were invisible from the exit code — which was 0.

So this script reads the *summary*, not the exit code, and asserts the two
properties whose loss would restore those bugs:

1. **No overclaiming.** With no `--org`, `s5_org_validate` must be recorded as
   `skipped`, `salesforce_validated` must be `false`, and the single ambiguous
   `pass` key must stay absent. A reader has to be able to tell "Salesforce
   agreed" apart from "we never asked".

2. **One name.** The bundle API name, the `.agent` config `developer_name`, and
   the test spec `subjectName` must still name the same agent, and the topic name
   must still agree with the subagent the router transitions to.

This complements `tests/test_roundtrip_lib.py`, which pins the same properties in
process. This one runs the real script end to end, the way an operator would.

Usage:
    python scripts/roundtrip_check.py outputs/roundtrip/roundtrip_summary.json

Exits non-zero on any contract violation so `set -e` in the caller trips.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from sf_video_blueprint.naming import names_agree


def main() -> int:
    if len(sys.argv) < 2:
        print("usage: roundtrip_check.py <roundtrip_summary.json>", file=sys.stderr)
        return 2

    summary_path = Path(sys.argv[1])
    if not summary_path.is_file():
        print(f"FAIL: no summary written at {summary_path}", file=sys.stderr)
        return 1

    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    failures: list[str] = []

    statuses = {stage["stage"]: stage["status"] for stage in summary.get("stages", [])}
    print(f"stages: {json.dumps(statuses, sort_keys=True)}")

    # --- Contract 1: the run reports what it skipped --------------------------
    if "pass" in summary:
        failures.append(
            'the summary carries a single "pass" key again. It was removed because '
            '{"pass": true} with skipped org stages is indistinguishable from a '
            "validated run. Report per-stage status instead."
        )
    if summary.get("salesforce_validated") is not False:
        failures.append(
            f"salesforce_validated is {summary.get('salesforce_validated')!r} in a run "
            "with no org configured; nothing was sent to Salesforce"
        )
    if summary.get("org_alias") is not None:
        failures.append(f"org_alias is {summary.get('org_alias')!r} in an offline run")
    if statuses.get("s5_org_validate") != "skipped":
        failures.append(
            f"s5_org_validate is {statuses.get('s5_org_validate')!r}, expected 'skipped' "
            "— CI has no org, so it cannot have run"
        )

    # Everything that does not need an org must actually have run, otherwise the
    # check above passes trivially on a script that does nothing at all.
    for stage in ("s2_derive_names", "s3_score_gate", "s4_emit_artifacts"):
        if statuses.get(stage) != "pass":
            failures.append(
                f"{stage} is {statuses.get(stage)!r}, expected 'pass' — it needs no org"
            )

    # --- Contract 2: one agent, one topic, in every artifact ------------------
    names = summary.get("derived_names") or {}
    if not names:
        failures.append("the summary records no derived names")
    else:
        print(f"derived names: {json.dumps(names, sort_keys=True)}")

        if not names_agree(names["agent_api_name"], names["developer_name"]):
            failures.append(
                f"bundle api_name {names['agent_api_name']!r} and config developer_name "
                f"{names['developer_name']!r} are not the same agent"
            )
        if names["test_subject_name"] != names["agent_api_name"]:
            failures.append(
                f"test spec subjectName {names['test_subject_name']!r} does not name the "
                f"agent that was emitted ({names['agent_api_name']!r}); the suite would "
                "target an agent that does not exist — this was the original defect"
            )
        if not names_agree(names["topic_name"], names["subagent"]):
            failures.append(
                f"topic {names['topic_name']!r} and subagent {names['subagent']!r} are "
                "not the same topic"
            )
        if names["router_action"] != f"go_to_{names['subagent']}":
            failures.append(
                f"router action {names['router_action']!r} does not transition to "
                f"subagent {names['subagent']!r}"
            )
        if names["expected_topic"] != names["topic_name"]:
            failures.append(
                f"test spec expectedTopic {names['expected_topic']!r} does not match the "
                f"emitted topic {names['topic_name']!r}"
            )

    if failures:
        print("\nCONTRACT VIOLATIONS:", file=sys.stderr)
        for failure in failures:
            print(f"  - {failure}", file=sys.stderr)
        return 1

    print(
        "\nOK: offline round trip ran, the org stage is reported as skipped rather than "
        f"claimed, and every artifact names {names['agent_api_name']}."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

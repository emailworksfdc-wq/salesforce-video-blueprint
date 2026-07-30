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

Usage (offline CI gate — no --org-alias, behaviour unchanged):
    python scripts/roundtrip_check.py outputs/roundtrip/roundtrip_summary.json

Usage (with org — one stage-5 round):
    python scripts/roundtrip_check.py outputs/roundtrip/roundtrip_summary.json \\
        --org-alias my-dev-org --agent-api-name SFVB_TEST_Update_Case_Status

    Add --strict to exit non-zero when the stage-5 round fails.

Exits non-zero on any contract violation so `set -e` in the caller trips.
Omitting --org-alias leaves the script behaviour identical to before this change.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from sf_video_blueprint.naming import names_agree


def _check_offline_contracts(summary: dict[str, Any]) -> list[str]:
    """Return all contract violations found in an offline (no-org) summary.

    Separated from ``main`` so unit tests can call it without argument parsing.
    """
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

    return failures


def _check_name_contracts(summary: dict[str, Any]) -> tuple[list[str], dict[str, Any]]:
    """Return (failures, names) for the one-name contract check.

    Both the offline and org-connected paths run this check: the names must
    agree in every artifact regardless of whether an org was involved.
    """
    failures: list[str] = []
    names: dict[str, Any] = summary.get("derived_names") or {}

    if not names:
        failures.append("the summary records no derived names")
        return failures, names

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

    return failures, names


def run_stage5_round(
    summary_path: Path,
    *,
    org_alias: str,
    agent_api_name: str,
    runner: Any = None,
) -> tuple[int, Any]:
    """Run one stage-5 round and return (exit_code, Stage5Round | None).

    Returns ``(0, round_result)`` when the round completes (regardless of
    pass/fail verdict — the caller decides via ``--strict`` whether failure is
    fatal).  Returns ``(1, None)`` on a hard error (missing files, import
    failure, Stage5Error from the org call).

    ``runner`` is injected by tests to drive the parser without a live org.
    A non-None runner stamps the feedback ``injected-runner``, which is NOT a
    real source — provenance guards in :mod:`stage5` refuse it as synthetic.
    This is the provenance system working as designed: it is not a test limitation.
    """
    # Lazy-import so the offline path stays importable in environments where
    # the full src/ tree is not installed.
    try:
        from sf_video_blueprint.stage5 import Stage5Error, run_agent_eval, stage5_round
    except ImportError as exc:
        print(f"FAIL: cannot import stage5: {exc}", file=sys.stderr)
        return 1, None

    out_dir = summary_path.parent

    # --- locate the derived spec ---------------------------------------------
    # The roundtrip script writes it to {OUT_DIR}/roundtrip.agent-spec.json.
    spec_path = out_dir / "roundtrip.agent-spec.json"
    if not spec_path.is_file():
        print(
            f"FAIL: derived spec not found at {spec_path}. "
            "Re-run agentforce_roundtrip.sh to produce it.",
            file=sys.stderr,
        )
        return 1, None

    # --- locate the legacy test spec ----------------------------------------
    # Prefer the emit manifest's path (authoritative); fall back to the known
    # default layout when the manifest is absent.
    manifest_path = out_dir / "emit_manifest.json"
    test_spec_path: Path | None = None
    if manifest_path.is_file():
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            raw = (manifest.get("paths") or {}).get("test_spec_legacy")
            if raw:
                test_spec_path = Path(raw)
        except (json.JSONDecodeError, KeyError):
            pass
    if test_spec_path is None:
        test_spec_path = out_dir / "testSpec-legacy.yaml"

    if not test_spec_path.is_file():
        print(
            f"FAIL: legacy test spec not found at {test_spec_path}. "
            "Re-run agentforce_roundtrip.sh (S4 emit) to produce it.",
            file=sys.stderr,
        )
        return 1, None

    # --- load the derived spec ----------------------------------------------
    _scripts_dir = str(Path(__file__).resolve().parent)
    if _scripts_dir not in sys.path:
        sys.path.insert(0, _scripts_dir)
    try:
        from roundtrip_lib import load_derived_spec, load_provenance  # type: ignore[import]
    except ImportError as exc:
        print(f"FAIL: cannot import roundtrip_lib: {exc}", file=sys.stderr)
        return 1, None

    try:
        spec = load_derived_spec(spec_path)
        provenance = load_provenance(spec_path)
    except Exception as exc:  # noqa: BLE001
        print(f"FAIL: loading derived spec: {exc}", file=sys.stderr)
        return 1, None

    # --- run the round ------------------------------------------------------
    print(f"\nStage 5 round against org={org_alias!r} agent={agent_api_name!r}")
    print(f"  test spec: {test_spec_path}")

    try:
        feedback = run_agent_eval(
            test_spec_path,
            org_alias=org_alias,
            api_name=agent_api_name,
            runner=runner,
        )
    except Stage5Error as exc:
        print(f"FAIL: stage-5 org call failed: {exc}", file=sys.stderr)
        return 1, None

    round_result = stage5_round(spec, feedback, round_number=1, provenance=provenance)

    # --- print the round summary --------------------------------------------
    score_after = round_result.score_after
    score_label = (
        f"{score_after.total}/{score_after.max_total} band={score_after.band}"
        if score_after is not None
        else "n/a"
    )
    print(
        f"  round={round_result.round_number}  score={score_label}  "
        f"passed={score_after.passed if score_after else False}  "
        f"trustworthy={round_result.trustworthy}"
    )
    if round_result.blocking_issues:
        print("  blocking issues:")
        for issue in round_result.blocking_issues:
            print(f"    - {issue}")
    if round_result.findings:
        print("  findings:")
        for finding in round_result.findings:
            print(f"    {finding}")

    if round_result.trustworthy and score_after is not None and score_after.passed:
        stop_reason = "passed"
    elif round_result.blocking_issues:
        stop_reason = f"blocked ({len(round_result.blocking_issues)} issue(s))"
    else:
        stop_reason = "failed" if feedback.failed_count > 0 else "completed"
    print(f"  stop_reason: {stop_reason}")

    return 0, round_result


def main(argv: list[str] | None = None, *, _runner: Any = None) -> int:
    """Entry point. ``_runner`` is injected by tests; pass ``None`` for production.

    ``_runner`` is threaded through to :func:`run_stage5_round` so tests can
    exercise the org-connected path without a live org. It is never documented
    in the CLI help because it is a test seam, not a user-facing option.
    """
    parser = argparse.ArgumentParser(
        description=__doc__.splitlines()[0],
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("summary", help="path to roundtrip_summary.json")
    parser.add_argument(
        "--org-alias",
        default=None,
        metavar="ALIAS",
        help=(
            "Salesforce org alias. When supplied together with --agent-api-name, "
            "runs one stage-5 round against the live agent after the offline checks. "
            "Omit to run offline-only (the original CI gate behaviour)."
        ),
    )
    parser.add_argument(
        "--agent-api-name",
        default=None,
        metavar="API_NAME",
        help=(
            "API name of the deployed Agentforce agent to test. "
            "Required when --org-alias is given."
        ),
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        default=False,
        help=(
            "Exit non-zero when the stage-5 round fails (blocking issues or "
            "failing test cases). Has no effect when --org-alias is omitted."
        ),
    )

    args = parser.parse_args(argv)

    # --org-alias and --agent-api-name must both be supplied or both omitted.
    if (args.org_alias is None) != (args.agent_api_name is None):
        parser.error("--org-alias and --agent-api-name must be supplied together")

    summary_path = Path(args.summary)
    if not summary_path.is_file():
        print(f"FAIL: no summary written at {summary_path}", file=sys.stderr)
        return 2

    summary = json.loads(summary_path.read_text(encoding="utf-8"))

    # --- Offline contract checks (always run) --------------------------------
    failures: list[str] = []

    if args.org_alias is None:
        # No org supplied: assert the run did NOT claim org validation.
        failures.extend(_check_offline_contracts(summary))

    name_failures, names = _check_name_contracts(summary)
    failures.extend(name_failures)

    if failures:
        print("\nCONTRACT VIOLATIONS:", file=sys.stderr)
        for failure in failures:
            print(f"  - {failure}", file=sys.stderr)
        return 1

    if args.org_alias is None:
        # Offline path: success.  No behaviour change from the original.
        print(
            "\nOK: offline round trip ran, the org stage is reported as skipped rather than "
            f"claimed, and every artifact names {names.get('agent_api_name', '(unknown)')}."
        )
        return 0

    # --- Stage-5 round (only when --org-alias is given) ----------------------
    print(
        "\nOK: offline contracts satisfied. "
        f"Every artifact names {names.get('agent_api_name', '(unknown)')}."
    )

    rc, round_result = run_stage5_round(
        summary_path,
        org_alias=args.org_alias,
        agent_api_name=args.agent_api_name,
        runner=_runner,
    )
    if rc != 0:
        return rc

    if args.strict and round_result is not None:
        round_failed = (
            bool(round_result.blocking_issues) or round_result.feedback.failed_count > 0
        )
        if round_failed:
            print(
                f"\nSTRICT FAIL: stage-5 round failed "
                f"(blocking_issues={len(round_result.blocking_issues)}, "
                f"failed_cases={round_result.feedback.failed_count})",
                file=sys.stderr,
            )
            return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

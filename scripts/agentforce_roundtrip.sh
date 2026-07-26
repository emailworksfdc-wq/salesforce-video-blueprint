#!/usr/bin/env bash
set -euo pipefail

# ============================================================================
# agentforce_roundtrip.sh — the whole round trip, end to end, honestly reported
# ============================================================================
# capture -> derive -> score -> emit bundle -> emit test specs -> validate -> report
#
# Runs offline by default. Pass `--org <alias>` to add the one org-dependent
# stage (`sf agent validate authoring-bundle`, which compiles the .agent file
# against Salesforce's real compiler).
#
# WHY THIS SCRIPT WAS REWRITTEN
#
# The previous version referred to three different agent names in a single run:
# the .agent config said `test_agent`, the CLI flags said `RoundtripTestAgent`,
# and both test specs said `TestAgent`. So it could never have completed a real
# round trip — the emitted test suite targeted an agent that did not exist.
# Every name is now derived from `naming.py` via `scripts/roundtrip_lib.py`; this
# script does not spell a single API name itself.
#
# It also printed "All executed stages PASSED" while the org stages were skipped.
# A skipped stage is now reported as SKIPPED, in the summary line and in the JSON,
# and the final line names what did not run.
#
# House style: `scripts/ci_smoke_check.py` and `scripts/mcp_stdio_check.py` —
# fail loudly, explain why, never assert a success that was not measured.
# ============================================================================

usage() {
  cat <<'EOF'
Usage: agentforce_roundtrip.sh [--capture <file>] [--spec <file>] [--org <alias>]
                               [--out <dir>] [--keep-going]

Runs the full round trip. Offline by default; every org call is opt-in.

Options:
  --capture <file>  DOM capture JSONL to derive a spec from.
                    Default: examples/case_triage.dom_capture.jsonl
  --spec <file>     Skip capture and start from an existing agent-spec JSON.
                    Mutually exclusive with --capture.
  --org <alias>     Run the org-dependent validate stage against this alias.
                    Omitted => S6 reports SKIPPED and claims nothing.
  --out <dir>       Output directory. Default: ./outputs/roundtrip
  --keep-going      Do not stop at the first failed stage (still exits non-zero).
  -h, --help        This text.

Environment:
  PY_BIN            Python interpreter (>=3.11). Auto-detected if unset.

Exit codes:
  0  every stage that RAN passed (skipped org stages are reported, not claimed)
  1  at least one stage failed
  2  bad arguments / preflight failure
  3  org safety guard tripped
  5  InsufficientEvidenceError — the recording is inadequate (a real finding)

Notes:
  * `sf agent validate authoring-bundle` is `requiresProject = true`: it resolves
    the bundle from a local SFDX project and POSTs the file content to the
    compile API, using the org for auth only. No deploy is required, so this
    script does not deploy and creates no metadata in the org.
  * The quality gate is REPORTED, never enforced down. A mock-telemetry run is
    supposed to fail the gate; that is the gate working.
EOF
}

# ----------------------------------------------------------------------------
# Argument parsing
# ----------------------------------------------------------------------------
CAPTURE=""
SPEC_IN=""
ORG_ALIAS=""
OUT_DIR="./outputs/roundtrip"
KEEP_GOING=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --capture) CAPTURE="${2:-}"; shift 2 ;;
    --spec)    SPEC_IN="${2:-}"; shift 2 ;;
    --org)     ORG_ALIAS="${2:-}"; shift 2 ;;
    --out)     OUT_DIR="${2:-}"; shift 2 ;;
    --keep-going) KEEP_GOING=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "ERROR: unknown argument: $1" >&2; echo >&2; usage >&2; exit 2 ;;
  esac
done

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DEFAULT_CAPTURE="${REPO_ROOT}/examples/case_triage.dom_capture.jsonl"

if [[ -n "${CAPTURE}" && -n "${SPEC_IN}" ]]; then
  echo "ERROR: --capture and --spec are mutually exclusive" >&2
  exit 2
fi
if [[ -z "${SPEC_IN}" && -z "${CAPTURE}" ]]; then
  CAPTURE="${DEFAULT_CAPTURE}"
fi
if [[ -n "${CAPTURE}" && ! -f "${CAPTURE}" ]]; then
  echo "ERROR: capture file not found: ${CAPTURE}" >&2
  exit 2
fi
if [[ -n "${SPEC_IN}" && ! -f "${SPEC_IN}" ]]; then
  echo "ERROR: spec file not found: ${SPEC_IN}" >&2
  exit 2
fi

LOG_DIR="${OUT_DIR}/logs"
mkdir -p "${LOG_DIR}"

# `sf` prints an update notice and ANSI colour into stdout, which corrupts
# --json parsing downstream. Silence both before any sf call.
export SF_SKIP_NEW_VERSION_CHECK=true NO_COLOR=1 FORCE_COLOR=0
export SF_DISABLE_LOG_FILE=true

die() {
  echo "ERROR: $*" >&2
  exit "${2:-1}"
}

banner() {
  echo ""
  echo "=============================================================="
  echo "  $1"
  echo "=============================================================="
}

# --- Stage bookkeeping ------------------------------------------------------
# Names, statuses and details are kept in parallel arrays (bash 3.2 on macOS has
# no associative-array ordering guarantees worth relying on). Every stage MUST
# land in exactly one of: pass / fail / skipped / insufficient_evidence.
STAGE_NAMES=()
STAGE_STATUSES=()
STAGE_DETAILS=()
FINAL_EXIT=0

record_stage() {
  # record_stage <name> <status> <detail>
  STAGE_NAMES+=("$1")
  STAGE_STATUSES+=("$2")
  STAGE_DETAILS+=("$3")
  case "$2" in
    pass)    echo "  [pass]    $1" ;;
    skipped) echo "  [SKIPPED] $1 — $3" ;;
    insufficient_evidence)
             echo "  [EVIDENCE] $1 — $3"
             FINAL_EXIT=5 ;;
    fail)    echo "  [FAIL]    $1 — $3"
             [[ ${FINAL_EXIT} -eq 5 ]] || FINAL_EXIT=1 ;;
    *) die "internal error: unknown stage status '$2'" ;;
  esac
}

# Defined before its first caller (`halt_unless_keep_going`, which can fire from
# S1 onwards): bash resolves function names at call time, so a definition further
# down the file would abort an early failure with "command not found" instead of
# writing the summary that explains what went wrong.
write_summary() {
  local summary="${OUT_DIR}/roundtrip_summary.json"
  RT_STAGE_NAMES="$(printf '%s\n' "${STAGE_NAMES[@]:-}")" \
  RT_STAGE_STATUSES="$(printf '%s\n' "${STAGE_STATUSES[@]:-}")" \
  RT_STAGE_DETAILS="$(printf '%s\n' "${STAGE_DETAILS[@]:-}")" \
  RT_ORG_ALIAS="${ORG_ALIAS}" \
  RT_SUMMARY_PATH="${summary}" \
  RT_IDENTITY_PATH="${LOG_DIR}/s2_identity.json" \
  "${PY_BIN}" - <<'PY'
import json, os
from pathlib import Path

def lines(key):
    raw = os.environ.get(key, "")
    return [line for line in raw.splitlines() if line]

names, statuses, details = lines("RT_STAGE_NAMES"), lines("RT_STAGE_STATUSES"), lines("RT_STAGE_DETAILS")
stages = [
    {"stage": n, "status": s, "detail": d}
    for n, s, d in zip(names, statuses, details)
]

ran = [s for s in stages if s["status"] != "skipped"]
skipped = [s for s in stages if s["status"] == "skipped"]
failed = [s for s in stages if s["status"] in ("fail", "insufficient_evidence")]
org_alias = os.environ.get("RT_ORG_ALIAS", "")
validated = any(s["stage"] == "s5_org_validate" and s["status"] == "pass" for s in stages)

identity = {}
identity_path = Path(os.environ["RT_IDENTITY_PATH"])
if identity_path.is_file():
    identity = json.loads(identity_path.read_text(encoding="utf-8"))

payload = {
    # Deliberately NOT called "pass": a run where every org stage was skipped
    # has not passed a round trip, and a single boolean invited exactly the
    # overclaim this script used to make.
    "all_executed_stages_passed": not failed,
    "stages_run": len(ran),
    "stages_skipped": len(skipped),
    "salesforce_validated": validated,
    "org_alias": org_alias or None,
    "derived_names": identity,
    "stages": stages,
}
Path(os.environ["RT_SUMMARY_PATH"]).write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
PY
}

# Stop at the first failure unless --keep-going: continuing past a broken stage
# is how the previous version ended up reporting success for work it never did.
halt_unless_keep_going() {
  if [[ ${KEEP_GOING} -eq 0 ]]; then
    write_summary
    echo ""
    echo "ABORTED after a failed stage. Re-run with --keep-going to see the rest."
    exit "${FINAL_EXIT}"
  fi
}

# ============================================================================
# Preflight — interpreter and imports
# ============================================================================
banner "Preflight"

PY_BIN="${PY_BIN:-}"
if [[ -z "${PY_BIN}" ]]; then
  for cand in "${REPO_ROOT}/.lanevenv/bin/python" "${REPO_ROOT}/.venv/bin/python"; do
    if [[ -x "${cand}" ]]; then PY_BIN="${cand}"; break; fi
  done
fi
if [[ -z "${PY_BIN}" ]]; then
  for cand in python3.13 python3.12 python3.11 python3; do
    if command -v "${cand}" >/dev/null 2>&1; then PY_BIN="$(command -v "${cand}")"; break; fi
  done
fi
[[ -n "${PY_BIN}" ]] || die "no python3 interpreter found; set PY_BIN" 2

"${PY_BIN}" - <<'PY' || die "Python >=3.11 required (PEP 604 unions are evaluated at runtime); set PY_BIN" 2
import sys
raise SystemExit(0 if sys.version_info >= (3, 11) else 1)
PY
echo "  interpreter: ${PY_BIN} ($("${PY_BIN}" -V 2>&1))"

RT_LIB="${REPO_ROOT}/scripts/roundtrip_lib.py"
[[ -f "${RT_LIB}" ]] || die "missing ${RT_LIB}" 2
"${PY_BIN}" "${RT_LIB}" --help >/dev/null 2>&1 \
  || die "roundtrip_lib.py is not importable with ${PY_BIN}. Run: ${PY_BIN} -m pip install -e '.[dev]'" 2
echo "  round-trip library: OK"

# ============================================================================
# Preflight — org, only when --org was passed
# ============================================================================
ORG_INSTANCE=""
if [[ -n "${ORG_ALIAS}" ]]; then
  banner "Preflight: org ${ORG_ALIAS}"

  # Hard block, no override. These two orgs are out of scope for this project
  # even read-only.
  if [[ "${ORG_ALIAS}" == "PPCDM" || "${ORG_ALIAS}" == "PPCaccenture" ]]; then
    die "${ORG_ALIAS} is permanently out of scope for this project" 3
  fi

  command -v sf >/dev/null 2>&1 || die "sf CLI not found in PATH" 2
  echo "  $(sf --version 2>&1 | head -1)"
  sf agent --help >/dev/null 2>&1 \
    || die "sf agent topic unavailable. Install: sf plugins install @salesforce/plugin-agent" 2

  set +e
  ORG_JSON="$(sf org display --target-org "${ORG_ALIAS}" --json 2>&1)"
  ORG_RC=$?
  set -e
  if [[ ${ORG_RC} -ne 0 ]]; then
    echo "${ORG_JSON}" >&2
    die "sf org display failed for ${ORG_ALIAS}" 3
  fi

  # Strip ANSI and slice from the first `{`: sf can still prepend warnings.
  ORG_INSTANCE="$(
    ORG_JSON="${ORG_JSON}" "${PY_BIN}" - <<'PY'
import json, os, re, sys
raw = re.sub(r"\x1b\[[0-9;]*m", "", os.environ["ORG_JSON"])
raw = raw[raw.index("{"):]
print(json.loads(raw).get("result", {}).get("instanceUrl", ""))
PY
  )"
  [[ -n "${ORG_INSTANCE}" ]] || die "could not resolve instanceUrl for ${ORG_ALIAS}" 3

  # Refuse anything that is not demonstrably a sandbox, scratch or dev org.
  # The instance URL is host-only and carries no credential, so it is safe to
  # print; tokens and frontdoor URLs are never echoed anywhere in this script.
  case "${ORG_INSTANCE}" in
    *.sandbox.my.salesforce.com|*.scratch.my.salesforce.com|*.develop.my.salesforce.com)
      echo "  instance: ${ORG_INSTANCE} (sandbox/scratch/dev — safe)" ;;
    *)
      die "org safety guard: ${ORG_INSTANCE} is not a sandbox/scratch/dev org" 3 ;;
  esac
fi

# ============================================================================
# S1: capture -> derived spec
# ============================================================================
banner "S1: derive a spec from the capture"

DERIVED_SPEC="${SPEC_IN}"
if [[ -n "${SPEC_IN}" ]]; then
  record_stage "s1_derive_spec" "skipped" "--spec given; using ${SPEC_IN} as-is"
else
  DERIVED_SPEC="${OUT_DIR}/roundtrip.agent-spec.json"
  set +e
  "${PY_BIN}" -m sf_video_blueprint.cli \
    --capture "${CAPTURE}" \
    --org-url "${ORG_INSTANCE:-https://example-dev.develop.my.salesforce.com}" \
    --output-path "${OUT_DIR}/roundtrip.html" \
    --spec-output "${DERIVED_SPEC}" \
    >"${LOG_DIR}/s1_derive.out" 2>"${LOG_DIR}/s1_derive.err"
  S1_RC=$?
  set -e
  if [[ ${S1_RC} -eq 0 && -f "${DERIVED_SPEC}" ]]; then
    record_stage "s1_derive_spec" "pass" "derived from $(basename "${CAPTURE}")"
    grep -E '^(Derived intent|WARNING)' "${LOG_DIR}/s1_derive.out" | sed 's/^/    /' || true
  else
    record_stage "s1_derive_spec" "fail" "pipeline exit ${S1_RC}; see ${LOG_DIR}/s1_derive.err"
    tail -20 "${LOG_DIR}/s1_derive.err" >&2 || true
    halt_unless_keep_going
  fi
fi

# ============================================================================
# S2: name derivation — the three artifacts must agree BEFORE anything is emitted
# ============================================================================
banner "S2: derive names from naming.py"

IDENTITY_OK=0
if [[ -f "${DERIVED_SPEC}" ]]; then
  set +e
  IDENTITY_SHELL="$("${PY_BIN}" "${RT_LIB}" identity "${DERIVED_SPEC}" --shell 2>"${LOG_DIR}/s2_identity.err")"
  S2_RC=$?
  set -e
  if [[ ${S2_RC} -eq 0 ]]; then
    eval "${IDENTITY_SHELL}"
    "${PY_BIN}" "${RT_LIB}" identity "${DERIVED_SPEC}" >"${LOG_DIR}/s2_identity.json"
    IDENTITY_OK=1
    record_stage "s2_derive_names" "pass" "all cross-artifact linkages agree"
    echo "    agent (bundle api name) : ${RT_AGENT_API_NAME}"
    echo "    agent (config dev name) : ${RT_DEVELOPER_NAME}"
    echo "    agent (test subjectName): ${RT_TEST_SUBJECT_NAME}"
    echo "    topic (spec yaml)       : ${RT_TOPIC_NAME}"
    echo "    topic (subagent block)  : ${RT_SUBAGENT}"
    echo "    topic (router action)   : ${RT_ROUTER_ACTION}"
    echo "    topic (expectedTopic)   : ${RT_EXPECTED_TOPIC}"
  else
    record_stage "s2_derive_names" "fail" "name derivation refused; see ${LOG_DIR}/s2_identity.err"
    cat "${LOG_DIR}/s2_identity.err" >&2 || true
    halt_unless_keep_going
  fi
else
  record_stage "s2_derive_names" "skipped" "no derived spec to name"
fi

# ============================================================================
# S3: quality gate — reported, never enforced downward
# ============================================================================
banner "S3: score the derived spec"

if [[ -f "${DERIVED_SPEC}" ]]; then
  set +e
  "${PY_BIN}" "${RT_LIB}" score "${DERIVED_SPEC}" --out "${OUT_DIR}/score.json" \
    >"${LOG_DIR}/s3_score.out" 2>"${LOG_DIR}/s3_score.err"
  S3_RC=$?
  set -e
  if [[ ${S3_RC} -eq 0 ]]; then
    sed 's/^/    /' "${LOG_DIR}/s3_score.out"
    # A failing gate on a mock-telemetry run is the gate doing its job, so the
    # stage passes on "the gate returned a verdict", not on "the verdict was
    # good". The verdict itself is in score.json and printed above.
    record_stage "s3_score_gate" "pass" "gate returned a verdict (see score.json)"
  else
    record_stage "s3_score_gate" "fail" "scorer errored; see ${LOG_DIR}/s3_score.err"
    tail -20 "${LOG_DIR}/s3_score.err" >&2 || true
    halt_unless_keep_going
  fi
else
  record_stage "s3_score_gate" "skipped" "no derived spec to score"
fi

# ============================================================================
# S4: emit the bundle and both test spec dialects
# ============================================================================
banner "S4: emit bundle + test specs"

BUNDLE_READY=0
SFDX_DIR=""
if [[ ${IDENTITY_OK} -eq 1 ]]; then
  set +e
  "${PY_BIN}" "${RT_LIB}" emit "${DERIVED_SPEC}" "${OUT_DIR}" \
    --manifest "${OUT_DIR}/emit_manifest.json" \
    >"${LOG_DIR}/s4_emit.out" 2>"${LOG_DIR}/s4_emit.err"
  S4_RC=$?
  set -e
  if [[ ${S4_RC} -eq 0 ]]; then
    SFDX_DIR="$(
      "${PY_BIN}" -c 'import json,sys;print(json.load(open(sys.argv[1]))["paths"]["sfdx_project_dir"])' \
        "${OUT_DIR}/emit_manifest.json"
    )"
    BUNDLE_READY=1
    record_stage "s4_emit_artifacts" "pass" "bundle + both test spec dialects written"
    echo "    bundle project: ${SFDX_DIR}"
    LOCAL_FINDINGS="$(
      "${PY_BIN}" -c 'import json,sys;print(len(json.load(open(sys.argv[1]))["local_findings"]))' \
        "${OUT_DIR}/emit_manifest.json"
    )"
    # Lane 01 measured validate_locally returning zero findings on a file the
    # real compiler rejected with 24 errors. Report the count, claim nothing.
    echo "    validate_locally findings: ${LOCAL_FINDINGS} (NOT a grammar verdict — see S5)"
  elif [[ ${S4_RC} -eq 5 ]]; then
    record_stage "s4_emit_artifacts" "insufficient_evidence" "recording inadequate; see ${LOG_DIR}/s4_emit.err"
    tail -20 "${LOG_DIR}/s4_emit.err" >&2 || true
    halt_unless_keep_going
  else
    record_stage "s4_emit_artifacts" "fail" "emit exit ${S4_RC}; see ${LOG_DIR}/s4_emit.err"
    tail -30 "${LOG_DIR}/s4_emit.err" >&2 || true
    halt_unless_keep_going
  fi
else
  record_stage "s4_emit_artifacts" "skipped" "names were not derived"
fi

# ============================================================================
# S5: sf agent validate authoring-bundle — the ONLY authoritative grammar verdict
# ============================================================================
banner "S5: sf agent validate authoring-bundle"

if [[ -z "${ORG_ALIAS}" ]]; then
  record_stage "s5_org_validate" "skipped" "no --org given; nothing was validated by Salesforce"
elif [[ ${BUNDLE_READY} -eq 0 ]]; then
  record_stage "s5_org_validate" "skipped" "no bundle was emitted to validate"
else
  # `requiresProject = true`: run from inside the emitted SFDX project so the
  # CLI can resolve the bundle off local disk. No deploy, no org mutation.
  set +e
  ( cd "${SFDX_DIR}" && sf agent validate authoring-bundle \
      --target-org "${ORG_ALIAS}" \
      --api-name "${RT_AGENT_API_NAME}" \
      --json ) >"${LOG_DIR}/s5_validate.json" 2>"${LOG_DIR}/s5_validate.err"
  S5_RC=$?
  set -e

  case ${S5_RC} in
    0) record_stage "s5_org_validate" "pass" "Salesforce compiled ${RT_AGENT_API_NAME} without errors" ;;
    1) record_stage "s5_org_validate" "fail" "compilation errors from the real compiler" ;;
    2) record_stage "s5_org_validate" "fail" "compile API returned HTTP 404 (unavailable in this org/region)" ;;
    3) record_stage "s5_org_validate" "fail" "compile API returned HTTP 500 (server error)" ;;
    *) record_stage "s5_org_validate" "fail" "unexpected exit ${S5_RC}" ;;
  esac

  # Print the compiler's own words. Paraphrasing a compiler error is how a
  # project ends up guessing at a grammar it could have simply read.
  if [[ ${S5_RC} -ne 0 ]]; then
    "${PY_BIN}" - "${LOG_DIR}/s5_validate.json" <<'PY' || true
import json, re, sys
from pathlib import Path
raw = re.sub(r"\x1b\[[0-9;]*m", "", Path(sys.argv[1]).read_text(encoding="utf-8"))
try:
    payload = json.loads(raw[raw.index("{"):])
except (ValueError, json.JSONDecodeError):
    print("    (unparseable CLI output)")
    print("\n".join(f"    {line}" for line in raw.splitlines()[:20]))
    raise SystemExit(0)
errors = (payload.get("data") or {}).get("errors") or []
for err in errors:
    print(f"    {err.get('errorType')}: {err.get('description')} "
          f"[Ln {err.get('lineStart')}, Col {err.get('colStart')}]")
# Only fall back to `message` when there is no structured error list: `message`
# is the concatenation of every error, so printing both repeats the first one.
if not errors and payload.get("message"):
    print(f"    {payload['message'].splitlines()[0]}")
print(f"    ({len(errors)} compilation error(s); full JSON in {sys.argv[1]})")
PY
    halt_unless_keep_going
  fi
fi

# ============================================================================
# S6: summary — says SKIPPED where it skipped
# ============================================================================
banner "S6: summary"
write_summary

echo ""
for i in "${!STAGE_NAMES[@]}"; do
  printf '  %-22s %s\n' "${STAGE_NAMES[$i]}" "${STAGE_STATUSES[$i]}"
done

echo ""
echo "  outputs: ${OUT_DIR}"
echo "  summary: ${OUT_DIR}/roundtrip_summary.json"
echo ""

# The verdict line. It names what was skipped rather than implying completeness.
SKIPPED_LIST=""
for i in "${!STAGE_NAMES[@]}"; do
  if [[ "${STAGE_STATUSES[$i]}" == "skipped" ]]; then
    SKIPPED_LIST="${SKIPPED_LIST}${SKIPPED_LIST:+, }${STAGE_NAMES[$i]}"
  fi
done

if [[ ${FINAL_EXIT} -eq 0 ]]; then
  if [[ -n "${ORG_ALIAS}" ]]; then
    echo "ROUND TRIP COMPLETE — Salesforce validated ${RT_AGENT_API_NAME:-the bundle} in org ${ORG_ALIAS}."
  else
    echo "LOCAL ROUND TRIP COMPLETE — NOTHING WAS VALIDATED BY SALESFORCE."
    echo "  No --org was given, so the compile step did not run. Re-run with"
    echo "  --org <alias> for a grammar verdict from Salesforce."
  fi
  [[ -n "${SKIPPED_LIST}" ]] && echo "  Skipped (not attempted, not claimed): ${SKIPPED_LIST}"
elif [[ ${FINAL_EXIT} -eq 5 ]]; then
  echo "RECORDING INADEQUATE — InsufficientEvidenceError. This is a real, informative"
  echo "  failure: the capture does not carry enough evidence to emit an honest spec."
else
  echo "ROUND TRIP FAILED — at least one stage failed. See the stage table above."
  [[ -n "${SKIPPED_LIST}" ]] && echo "  Skipped: ${SKIPPED_LIST}"
fi

exit "${FINAL_EXIT}"

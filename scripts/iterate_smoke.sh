#!/usr/bin/env bash
set -euo pipefail

# ============================================================================
# iterate_smoke.sh — offline smoke test for the iterate refinement loop
# ============================================================================
# Runs the CLI pipeline on the example capture, then drives the iterate loop
# offline (no org, no LLM, no network). Checks:
#
#   1. The CLI emits a derived spec (s1)
#   2. The iterate loop runs without error (s2)
#   3. The iteration report is written (s3)
#   4. At least one versioned spec is produced (s4)
#   5. Every versioned spec has an honest provenance stamp (s5)
#
# This is an OFFLINE smoke test. It does not exercise use_cli=True (the real
# LLM re-feed mechanism) — that requires an org. It proves the offline
# refinement loop is wired up correctly.
#
# Exit codes:
#   0  every stage that RAN passed
#   1  at least one stage failed
#   2  bad arguments / preflight failure
# ============================================================================

usage() {
  cat <<'EOF'
Usage: iterate_smoke.sh [--capture <file>] [--out <dir>] [--max-rounds <n>]

Offline smoke test for the iterate refinement loop.

Options:
  --capture <file>   DOM capture JSONL.
                     Default: examples/case_triage.dom_capture.jsonl
  --out <dir>        Output directory. Default: ./outputs/iterate_smoke
  --max-rounds <n>   Max refinement rounds (default: 3)
  -h, --help         This text.

Environment:
  PY_BIN             Python interpreter (>=3.11). Auto-detected if unset.

Exit codes:
  0  all stages passed
  1  at least one stage failed
  2  bad arguments / preflight failure
EOF
}

# --- Argument parsing --------------------------------------------------------
CAPTURE=""
OUT_DIR="./outputs/iterate_smoke"
MAX_ROUNDS=3

while [[ $# -gt 0 ]]; do
  case "$1" in
    --capture)   CAPTURE="${2:-}"; shift 2 ;;
    --out)       OUT_DIR="${2:-}"; shift 2 ;;
    --max-rounds) MAX_ROUNDS="${2:-3}"; shift 2 ;;
    -h|--help)   usage; exit 0 ;;
    *) echo "ERROR: unknown argument: $1" >&2; echo >&2; usage >&2; exit 2 ;;
  esac
done

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DEFAULT_CAPTURE="${REPO_ROOT}/examples/case_triage.dom_capture.jsonl"

if [[ -z "${CAPTURE}" ]]; then
  CAPTURE="${DEFAULT_CAPTURE}"
fi
if [[ ! -f "${CAPTURE}" ]]; then
  echo "ERROR: capture file not found: ${CAPTURE}" >&2
  exit 2
fi

LOG_DIR="${OUT_DIR}/logs"
mkdir -p "${LOG_DIR}"

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

STAGE_NAMES=()
STAGE_STATUSES=()
STAGE_DETAILS=()
FINAL_EXIT=0

record_stage() {
  STAGE_NAMES+=("$1")
  STAGE_STATUSES+=("$2")
  STAGE_DETAILS+=("$3")
  case "$2" in
    pass)    echo "  [pass]    $1" ;;
    skipped) echo "  [SKIPPED] $1 — $3" ;;
    fail)    echo "  [FAIL]    $1 — $3"
             FINAL_EXIT=1 ;;
    *) die "internal error: unknown stage status '$2'" ;;
  esac
}

write_summary() {
  local summary="${OUT_DIR}/iterate_smoke_summary.json"
  RT_STAGE_NAMES="$(printf '%s\n' "${STAGE_NAMES[@]:-}")" \
  RT_STAGE_STATUSES="$(printf '%s\n' "${STAGE_STATUSES[@]:-}")" \
  RT_STAGE_DETAILS="$(printf '%s\n' "${STAGE_DETAILS[@]:-}")" \
  RT_SUMMARY_PATH="${summary}" \
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
failed = [s for s in stages if s["status"] == "fail"]

payload = {
    "all_executed_stages_passed": not failed,
    "stages_run": len(ran),
    "stages": stages,
}
Path(os.environ["RT_SUMMARY_PATH"]).write_text(
    json.dumps(payload, indent=2) + "\n", encoding="utf-8"
)
PY
}

# ============================================================================
# Preflight
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

"${PY_BIN}" - <<'PY' || die "Python >=3.11 required; set PY_BIN" 2
import sys
raise SystemExit(0 if sys.version_info >= (3, 11) else 1)
PY
echo "  interpreter: ${PY_BIN} ($("${PY_BIN}" -V 2>&1))"

# ============================================================================
# S1: derive a spec from the capture
# ============================================================================
banner "S1: derive spec from capture"

DERIVED_SPEC="${OUT_DIR}/iterate_smoke.agent-spec.json"
set +e
"${PY_BIN}" -m sf_video_blueprint.cli run \
  --capture "${CAPTURE}" \
  --org-url "https://example-dev.develop.my.salesforce.com" \
  --output-path "${OUT_DIR}/iterate_smoke.html" \
  --spec-output "${DERIVED_SPEC}" \
  >"${LOG_DIR}/s1_derive.out" 2>"${LOG_DIR}/s1_derive.err"
S1_RC=$?
set -e

if [[ ${S1_RC} -eq 0 && -f "${DERIVED_SPEC}" ]]; then
  record_stage "s1_derive_spec" "pass" "spec derived from $(basename "${CAPTURE}")"
  grep -E '^(Derived intent|WARNING)' "${LOG_DIR}/s1_derive.out" | sed 's/^/    /' || true
else
  record_stage "s1_derive_spec" "fail" "pipeline exit ${S1_RC}; see ${LOG_DIR}/s1_derive.err"
  tail -20 "${LOG_DIR}/s1_derive.err" >&2 || true
  write_summary
  exit "${FINAL_EXIT}"
fi

# ============================================================================
# S2: run the offline iterate loop
# ============================================================================
banner "S2: run iterate loop (offline, max_rounds=${MAX_ROUNDS})"

ITERATE_OUT="${OUT_DIR}/iterate_out"
mkdir -p "${ITERATE_OUT}"

set +e
"${PY_BIN}" "${REPO_ROOT}/scripts/run_iterate_smoke.py" \
  "${DERIVED_SPEC}" \
  --out "${ITERATE_OUT}" \
  --max-rounds "${MAX_ROUNDS}" \
  >"${LOG_DIR}/s2_iterate.out" 2>"${LOG_DIR}/s2_iterate.err"
S2_RC=$?
set -e

if [[ ${S2_RC} -eq 0 ]]; then
  record_stage "s2_iterate_loop" "pass" "iterate loop completed (max_rounds=${MAX_ROUNDS})"
  cat "${LOG_DIR}/s2_iterate.out" | sed 's/^/    /' || true
else
  record_stage "s2_iterate_loop" "fail" "iterate loop exit ${S2_RC}; see ${LOG_DIR}/s2_iterate.err"
  tail -20 "${LOG_DIR}/s2_iterate.err" >&2 || true
  write_summary
  exit "${FINAL_EXIT}"
fi

# ============================================================================
# S3: check the iterate contracts
# ============================================================================
banner "S3: check iterate smoke contracts"

ITERATE_SUMMARY="${OUT_DIR}/iterate_smoke_check_result.json"
set +e
"${PY_BIN}" "${REPO_ROOT}/scripts/iterate_smoke_check.py" \
  "${ITERATE_OUT}" \
  --out "${ITERATE_SUMMARY}" \
  >"${LOG_DIR}/s3_check.out" 2>"${LOG_DIR}/s3_check.err"
S3_RC=$?
set -e

if [[ ${S3_RC} -eq 0 ]]; then
  record_stage "s3_iterate_contracts" "pass" "all iterate contracts satisfied"
  cat "${LOG_DIR}/s3_check.out" | sed 's/^/    /' || true
else
  record_stage "s3_iterate_contracts" "fail" "contract violations; see ${LOG_DIR}/s3_check.err"
  cat "${LOG_DIR}/s3_check.err" >&2 || true
fi

# ============================================================================
# Summary
# ============================================================================
banner "Summary"
write_summary

echo ""
for i in "${!STAGE_NAMES[@]}"; do
  printf '  %-26s %s\n' "${STAGE_NAMES[$i]}" "${STAGE_STATUSES[$i]}"
done

echo ""
echo "  outputs: ${OUT_DIR}"
echo "  summary: ${OUT_DIR}/iterate_smoke_summary.json"
echo ""

if [[ ${FINAL_EXIT} -eq 0 ]]; then
  echo "ITERATE SMOKE PASSED — offline refinement loop is wired up correctly."
else
  echo "ITERATE SMOKE FAILED — at least one stage failed. See the table above."
fi

exit "${FINAL_EXIT}"

#!/usr/bin/env bash
set -euo pipefail

if [[ $# -lt 2 ]]; then
  echo "Usage: $0 <org-alias> <video-path> [track-record Object:Id]"
  exit 2
fi

ORG_ALIAS="$1"
VIDEO_PATH="$2"
TRACK_RECORD="${3:-}"
OUT_DIR="./outputs/validation"
SUMMARY="${OUT_DIR}/run_summary.json"
ARTIFACT_DIR="./outputs/replay_artifacts"
STRICT_ARTIFACTS="${STRICT_ARTIFACTS:-0}"

mkdir -p "${OUT_DIR}" "${ARTIFACT_DIR}"

die() {
  echo "ERROR: $*" >&2
  exit 1
}

retry_cmd() {
  local attempts="$1"
  local base_sleep="$2"
  local label="$3"
  shift 3
  local i=1
  local last_rc=1
  local log_file="${OUT_DIR}/${label}.log"
  : > "${log_file}"
  while [[ $i -le $attempts ]]; do
    if "$@" >>"${log_file}" 2>&1; then
      return 0
    fi
    last_rc=$?
    local sleep_s
    sleep_s=$(python3 - <<PY
import random
base=${base_sleep}
i=${i}
print(round((base * (2 ** (i-1))) + random.uniform(0, 0.35), 2))
PY
)
    echo "WARN: ${label} attempt ${i}/${attempts} failed (rc=${last_rc}), retrying in ${sleep_s}s" >&2
    sleep "${sleep_s}"
    i=$((i + 1))
  done
  echo "ERROR: ${label} failed after ${attempts} attempts. See ${log_file}" >&2
  return "${last_rc}"
}

json_check() {
  python3 - <<'PY' "$1"
import json, pathlib, sys
path = pathlib.Path(sys.argv[1])
json.loads(path.read_text(encoding="utf-8"))
PY
}

echo "[1/7] Preflight"
# The package requires Python >=3.11 (PEP 604 unions are evaluated at runtime by
# dataclasses). The system python3 on macOS is 3.9, so resolve an interpreter
# explicitly and fail loudly rather than dying later with a confusing error.
PY_BIN="${PY_BIN:-}"
if [[ -z "${PY_BIN}" ]]; then
  if [[ -x "./.venv/bin/python" ]]; then
    PY_BIN="./.venv/bin/python"
  else
    for cand in python3.13 python3.12 python3.11 python3; do
      if command -v "${cand}" >/dev/null 2>&1; then PY_BIN="$(command -v "${cand}")"; break; fi
    done
  fi
fi
[[ -n "${PY_BIN}" ]] || die "no python3 interpreter found"
"${PY_BIN}" - <<'PY' || die "Python >=3.11 required; set PY_BIN or create ./.venv (uv venv --python 3.13 .venv)"
import sys
raise SystemExit(0 if sys.version_info >= (3, 11) else 1)
PY
"${PY_BIN}" -c 'import typer, pydantic, jinja2, requests' >/dev/null 2>&1 \
  || die "missing deps for ${PY_BIN}. Run: uv pip install --python ${PY_BIN} -e ."
echo "  interpreter: ${PY_BIN} ($(${PY_BIN} -V 2>&1))"
PREFLIGHT_OK=true
retry_cmd 3 0.5 "preflight_org_display" env SF_DISABLE_LOG_FILE=true sf org display --target-org "${ORG_ALIAS}" --json || die "org display failed"
retry_cmd 3 0.5 "preflight_user_query" env SF_DISABLE_LOG_FILE=true sf data query --target-org "${ORG_ALIAS}" --query "SELECT Id FROM User LIMIT 1" --json || die "user query failed"

ORG_URL="$(SF_DISABLE_LOG_FILE=true sf org display --target-org "${ORG_ALIAS}" --json | python3 -c 'import sys,json;print(json.load(sys.stdin)["result"]["instanceUrl"])')"
SF_ACCESS_TOKEN="$(SF_DISABLE_LOG_FILE=true sf org display --target-org "${ORG_ALIAS}" --json | python3 -c 'import sys,json;print(json.load(sys.stdin)["result"]["accessToken"])')"
export SF_BLUEPRINT_PLAYWRIGHT=1
export SF_BLUEPRINT_HEADLESS=1
export SF_BLUEPRINT_ARTIFACTS_DIR="${ARTIFACT_DIR}"

echo "[2/7] Mock run"
PYTHONPATH=src "${PY_BIN}" -m sf_video_blueprint.cli "${VIDEO_PATH}" --org-url "${ORG_URL}" --mode mock --output-path "${OUT_DIR}/mock_blueprint.html" >"${OUT_DIR}/mock_run.log" 2>&1 || die "mock run failed, see ${OUT_DIR}/mock_run.log"

echo "[3/7] Live run"
# Token is passed via the exported SF_ACCESS_TOKEN env var, never as a CLI
# argument: argv is world-readable via `ps` and lands in shell history.
export SF_ACCESS_TOKEN
LIVE_CMD=("${PY_BIN}" -m sf_video_blueprint.cli "${VIDEO_PATH}" --org-url "${ORG_URL}" --mode live --output-path "${OUT_DIR}/live_blueprint.html")
if [[ -n "${TRACK_RECORD}" ]]; then
  LIVE_CMD+=(--track-record "${TRACK_RECORD}")
fi
PYTHONPATH=src "${LIVE_CMD[@]}" >"${OUT_DIR}/live_run.log" 2>&1 || die "live run failed, see ${OUT_DIR}/live_run.log"

# A run can exit 0 while individual replay steps failed. Surface that as a
# critical issue rather than assuming success.
LIVE_STEP_FAILED=false
if grep -qE 'Replay status:[[:space:]]*(failed|retried)' "${OUT_DIR}/live_blueprint.html" 2>/dev/null; then
  LIVE_STEP_FAILED=true
  echo "WARN: live run contains failed/retried replay steps" >&2
fi

echo "[4/7] Telemetry checks"
set +e
retry_cmd 3 0.4 "telemetry_apexlog" curl -fsS -H "Authorization: Bearer ${SF_ACCESS_TOKEN}" "${ORG_URL}/services/data/v61.0/tooling/query?q=SELECT+Id+FROM+ApexLog+LIMIT+1"
Q1=$?
retry_cmd 3 0.4 "telemetry_async" curl -fsS -H "Authorization: Bearer ${SF_ACCESS_TOKEN}" "${ORG_URL}/services/data/v61.0/query?q=SELECT+Id+FROM+AsyncApexJob+LIMIT+1"
Q2=$?
retry_cmd 3 0.4 "telemetry_flow" curl -fsS -H "Authorization: Bearer ${SF_ACCESS_TOKEN}" "${ORG_URL}/services/data/v61.0/query?q=SELECT+Id+FROM+FlowInterview+LIMIT+1"
Q3=$?
retry_cmd 3 0.4 "telemetry_validation" curl -fsS -H "Authorization: Bearer ${SF_ACCESS_TOKEN}" "${ORG_URL}/services/data/v61.0/tooling/query?q=SELECT+Id+FROM+ValidationRule+LIMIT+1"
Q4=$?
set -e

echo "[5/7] Artifact checks"
ARTIFACTS_OK=false
PNG_COUNT=$(ls "${ARTIFACT_DIR}"/*.png 2>/dev/null | wc -l | tr -d ' ')
TRACE_COUNT=$(ls "${ARTIFACT_DIR}"/*.network.json 2>/dev/null | wc -l | tr -d ' ')
VALID_JSON_COUNT=0
for f in "${ARTIFACT_DIR}"/*.network.json; do
  [[ -e "${f}" ]] || continue
  if [[ -s "${f}" ]] && json_check "${f}" >/dev/null 2>&1; then
    VALID_JSON_COUNT=$((VALID_JSON_COUNT + 1))
  fi
done
if [[ "${PNG_COUNT}" -ge 1 && "${TRACE_COUNT}" -ge 1 && "${VALID_JSON_COUNT}" -ge 1 ]]; then
  ARTIFACTS_OK=true
fi

echo "[6/7] Negative test (track-record format)"
NEG_OK=false
set +e
BAD_OUT="$(PYTHONPATH=src "${PY_BIN}" -m sf_video_blueprint.cli "${VIDEO_PATH}" --org-url "${ORG_URL}" --mode live --track-record "BadFormat" 2>&1)"
NEG_RC=$?
echo "${BAD_OUT}" > "${OUT_DIR}/negative_track_record.log"
if [[ ${NEG_RC} -ne 0 && "${BAD_OUT}" =~ Invalid[[:space:]].*track-record ]]; then
  NEG_OK=true
fi
set -e

echo "[7/7] Score"
"${PY_BIN}" - <<'PY' "${SUMMARY}" "${Q1}" "${Q2}" "${Q3}" "${Q4}" "${ARTIFACTS_OK}" "${NEG_OK}" "${OUT_DIR}" "${PREFLIGHT_OK}" "${LIVE_STEP_FAILED}"
import json, sys, pathlib
def flag(value: str) -> bool:
    return value.lower() == "true"
summary_path = pathlib.Path(sys.argv[1])
q = [int(sys.argv[2]), int(sys.argv[3]), int(sys.argv[4]), int(sys.argv[5])]
artifacts_ok = flag(sys.argv[6])
neg_ok = flag(sys.argv[7])
out_dir = pathlib.Path(sys.argv[8])
preflight_ok = flag(sys.argv[9])
live_step_failed = flag(sys.argv[10])
data = {
  # Previously hardcoded True; now reflects whether preflight actually ran clean.
  "preflight_ok": preflight_ok,
  "execution_ok": (out_dir / "mock_blueprint.html").exists() and (out_dir / "live_blueprint.html").exists(),
  "telemetry_ok": all(item == 0 for item in q),
  "artifacts_ok": artifacts_ok,
  "negative_tests_ok": neg_ok,
  # Previously hardcoded False, which made the `not critical_issue` pass
  # condition unfailable. Now set when the live run reported a step failure.
  "critical_issue": live_step_failed,
  "details": {
    "telemetry_exit_codes": {"apexlog": q[0], "async": q[1], "flow": q[2], "validation": q[3]},
    "artifact_counts": {
      "screenshots": int(len(list(pathlib.Path("./outputs/replay_artifacts").glob("*.png")))),
      "network_traces": int(len(list(pathlib.Path("./outputs/replay_artifacts").glob("*.network.json"))))
    }
  }
}
summary_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
print(json.dumps(data, indent=2))
PY

set +e
"${PY_BIN}" ./scripts/score_run.py "${SUMMARY}" "${OUT_DIR}"
SCORE_RC=$?
set -e

if [[ "${STRICT_ARTIFACTS}" == "1" ]]; then
  # NOTE: nothing in the pipeline emits these two artifacts yet, so
  # STRICT_ARTIFACTS=1 is expected to fail until an emitter exists.
  [[ -s "${ARTIFACT_DIR}/replay_manifest.json" ]] || die "STRICT_ARTIFACTS=1 requires replay_manifest.json (no emitter implemented yet)"
  [[ -s "${ARTIFACT_DIR}/step_ledger.json" ]] || die "STRICT_ARTIFACTS=1 requires step_ledger.json (no emitter implemented yet)"
fi

echo "Validation complete. See ${OUT_DIR}"
if [[ ${SCORE_RC} -ne 0 ]]; then
  die "quality gate FAILED (see blocking_issues above). Artifacts retained in ${OUT_DIR}"
fi
echo "Quality gate passed."

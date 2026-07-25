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
python3 --version >/dev/null || die "python3 unavailable"
retry_cmd 3 0.5 "preflight_org_display" env SF_DISABLE_LOG_FILE=true sf org display --target-org "${ORG_ALIAS}" --json || die "org display failed"
retry_cmd 3 0.5 "preflight_user_query" env SF_DISABLE_LOG_FILE=true sf data query --target-org "${ORG_ALIAS}" --query "SELECT Id FROM User LIMIT 1" --json || die "user query failed"

ORG_URL="$(SF_DISABLE_LOG_FILE=true sf org display --target-org "${ORG_ALIAS}" --json | python3 -c 'import sys,json;print(json.load(sys.stdin)["result"]["instanceUrl"])')"
SF_ACCESS_TOKEN="$(SF_DISABLE_LOG_FILE=true sf org display --target-org "${ORG_ALIAS}" --json | python3 -c 'import sys,json;print(json.load(sys.stdin)["result"]["accessToken"])')"
export SF_BLUEPRINT_PLAYWRIGHT=1
export SF_BLUEPRINT_HEADLESS=1
export SF_BLUEPRINT_ARTIFACTS_DIR="${ARTIFACT_DIR}"

echo "[2/7] Mock run"
PYTHONPATH=src python3 -m sf_video_blueprint.cli run "${VIDEO_PATH}" --org-url "${ORG_URL}" --mode mock --output-path "${OUT_DIR}/mock_blueprint.html" >"${OUT_DIR}/mock_run.log" 2>&1 || die "mock run failed, see ${OUT_DIR}/mock_run.log"

echo "[3/7] Live run"
LIVE_CMD=(python3 -m sf_video_blueprint.cli run "${VIDEO_PATH}" --org-url "${ORG_URL}" --mode live --access-token "${SF_ACCESS_TOKEN}" --output-path "${OUT_DIR}/live_blueprint.html")
if [[ -n "${TRACK_RECORD}" ]]; then
  LIVE_CMD+=(--track-record "${TRACK_RECORD}")
fi
PYTHONPATH=src "${LIVE_CMD[@]}" >"${OUT_DIR}/live_run.log" 2>&1 || die "live run failed, see ${OUT_DIR}/live_run.log"

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
BAD_OUT="$(PYTHONPATH=src python3 -m sf_video_blueprint.cli run "${VIDEO_PATH}" --org-url "${ORG_URL}" --mode live --access-token "${SF_ACCESS_TOKEN}" --track-record "BadFormat" 2>&1)"
NEG_RC=$?
echo "${BAD_OUT}" > "${OUT_DIR}/negative_track_record.log"
if [[ ${NEG_RC} -ne 0 && "${BAD_OUT}" =~ Invalid[[:space:]].*track-record ]]; then
  NEG_OK=true
fi
set -e

echo "[7/7] Score"
python3 - <<'PY' "${SUMMARY}" "${Q1}" "${Q2}" "${Q3}" "${Q4}" "${ARTIFACTS_OK}" "${NEG_OK}" "${OUT_DIR}"
import json, sys, pathlib
summary_path = pathlib.Path(sys.argv[1])
q = [int(sys.argv[2]), int(sys.argv[3]), int(sys.argv[4]), int(sys.argv[5])]
artifacts_ok = sys.argv[6] == "true" or sys.argv[6] == "True"
neg_ok = sys.argv[7] == "true" or sys.argv[7] == "True"
out_dir = pathlib.Path(sys.argv[8])
data = {
  "preflight_ok": True,
  "execution_ok": (out_dir / "mock_blueprint.html").exists() and (out_dir / "live_blueprint.html").exists(),
  "telemetry_ok": all(item == 0 for item in q),
  "artifacts_ok": artifacts_ok,
  "negative_tests_ok": neg_ok,
  "critical_issue": False,
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

python3 ./scripts/score_run.py "${SUMMARY}"

if [[ "${STRICT_ARTIFACTS}" == "1" ]]; then
  [[ -s "${ARTIFACT_DIR}/replay_manifest.json" ]] || die "STRICT_ARTIFACTS=1 requires replay_manifest.json"
  [[ -s "${ARTIFACT_DIR}/step_ledger.json" ]] || die "STRICT_ARTIFACTS=1 requires step_ledger.json"
fi

echo "Validation complete. See ${OUT_DIR}"

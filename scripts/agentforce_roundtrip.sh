#!/usr/bin/env bash
set -euo pipefail

# ============================================================================
# agentforce_roundtrip.sh — REAL verification for Step 6 Agentforce spec output
# ============================================================================
# Drives a derived agent spec through the actual Salesforce CLI and reports
# honestly at every stage. This is the ONLY authority on whether our generated
# YAML, .agent, and testSpec files are accepted by the CLI.
#
# Owner: B9
# Contract: INTERFACE_CONTRACT.md § 3
# Pattern source: validate_dev_org.sh (PY_BIN preflight, env-var token passing,
#                 honest exit codes, real failure detection)
#
# Requirements met:
# 1. PY_BIN preflight identical in spirit to validate_dev_org.sh
# 2. Org safety guard (sandbox/scratch/dev only, PPCDM/PPCaccenture hard-blocked)
# 3. CLI preflight (sf exists, agent topic available)
# 4. Six stages: local emit (S1/S2), CLI generate/validate (S3/S4), test-spec (S5), summary (S6)
# 5. All CLI stdout/stderr captured to files, last 20 lines printed on failure
# 6. No hardcoded success literals — every status from real exit codes
# 7. Secrets passed via env vars, never argv
# 8. DRY_RUN=1 support (S1/S2 only, no org)
# 9. Exit non-zero if any executed stage failed
# 10. bash -n clean
# ============================================================================

usage() {
  cat <<EOF
Usage: $0 <org-alias> <derived-spec.json> [out-dir]

Arguments:
  org-alias          Sandbox, scratch, or .develop.my.salesforce.com dev org alias.
                     PPCDM and PPCaccenture are permanently blocked.
  derived-spec.json  Path to DerivedAgentSpec JSON (from spec_builder.py).
  out-dir            Optional output directory (default: ./outputs/roundtrip).

Environment:
  DRY_RUN=1          Run only local stages (S1/S2), skip org-dependent CLI stages.
  PY_BIN             Python interpreter to use (auto-detected if unset).

Exit codes:
  0   All executed stages passed
  1   At least one stage failed
  2   Invalid arguments
  3   Org safety guard triggered
  4   CLI preflight failed
  5   InsufficientEvidenceError (recording inadequate — legitimate failure)
EOF
  exit 2
}

if [[ $# -lt 2 ]]; then
  usage
fi

ORG_ALIAS="$1"
DERIVED_SPEC="$2"
OUT_DIR="${3:-./outputs/roundtrip}"
DRY_RUN="${DRY_RUN:-0}"

[[ -f "${DERIVED_SPEC}" ]] || { echo "ERROR: derived spec not found: ${DERIVED_SPEC}" >&2; exit 2; }
mkdir -p "${OUT_DIR}/logs"

die() {
  echo "ERROR: $*" >&2
  exit "${2:-1}"
}

banner() {
  echo ""
  echo "========================================"
  echo "  $1"
  echo "========================================"
}

# ============================================================================
# Preflight 1: Python interpreter (identical to validate_dev_org.sh pattern)
# ============================================================================
banner "Preflight: Python interpreter"

PY_BIN="${PY_BIN:-}"
if [[ -z "${PY_BIN}" ]]; then
  if [[ -x "./.venv/bin/python" ]]; then
    PY_BIN="./.venv/bin/python"
  else
    for cand in python3.13 python3.12 python3.11 python3; do
      if command -v "${cand}" >/dev/null 2>&1; then
        PY_BIN="$(command -v "${cand}")"
        break
      fi
    done
  fi
fi

[[ -n "${PY_BIN}" ]] || die "no python3 interpreter found" 2

# Assert Python >= 3.11 (PEP 604 unions)
"${PY_BIN}" - <<'PY' || die "Python >=3.11 required; set PY_BIN or create ./.venv (uv venv --python 3.13 .venv)" 2
import sys
raise SystemExit(0 if sys.version_info >= (3, 11) else 1)
PY

echo "  interpreter: ${PY_BIN} ($(${PY_BIN} -V 2>&1))"

# Assert project imports work
"${PY_BIN}" -c 'import sf_video_blueprint.agentforce_spec' >/dev/null 2>&1 || \
  die "sf_video_blueprint.agentforce_spec not importable. Run: uv pip install --python ${PY_BIN} -e ." 2

"${PY_BIN}" -c 'import sf_video_blueprint.agent_script' >/dev/null 2>&1 || \
  die "sf_video_blueprint.agent_script not importable. Run: uv pip install --python ${PY_BIN} -e ." 2

"${PY_BIN}" -c 'import sf_video_blueprint.eval_spec' >/dev/null 2>&1 || \
  die "sf_video_blueprint.eval_spec not importable. Run: uv pip install --python ${PY_BIN} -e ." 2

echo "  ✓ Project imports verified"

# ============================================================================
# Preflight 2: CLI itself
# ============================================================================
banner "Preflight: Salesforce CLI"

command -v sf >/dev/null 2>&1 || die "sf CLI not found in PATH" 4
SF_VERSION="$(sf --version 2>&1 | head -1)"
echo "  ${SF_VERSION}"

sf agent --help >/dev/null 2>&1 || die "agent topic not available. Install: sf plugins install @salesforce/plugin-agent" 4
echo "  ✓ agent topic available"

# ============================================================================
# Preflight 3: Org safety guard (HARD BLOCK — no override)
# ============================================================================
if [[ "${DRY_RUN}" == "0" ]]; then
  banner "Preflight: Org safety guard"

  # HARD BLOCK by alias name
  if [[ "${ORG_ALIAS}" == "PPCDM" || "${ORG_ALIAS}" == "PPCaccenture" ]]; then
    die "PPCDM and PPCaccenture are permanently out of scope for this project" 3
  fi

  set +e
  ORG_DISPLAY_JSON="$(SF_DISABLE_LOG_FILE=true sf org display --target-org "${ORG_ALIAS}" --json 2>&1)"
  ORG_DISPLAY_RC=$?
  set -e

  if [[ ${ORG_DISPLAY_RC} -ne 0 ]]; then
    echo "ERROR: sf org display failed for ${ORG_ALIAS}" >&2
    echo "${ORG_DISPLAY_JSON}" >&2
    exit 3
  fi

  INSTANCE_URL="$("${PY_BIN}" - <<PY
import sys, json
data = json.loads('''${ORG_DISPLAY_JSON}''')
print(data.get("result", {}).get("instanceUrl", ""))
PY
)"

  [[ -n "${INSTANCE_URL}" ]] || die "could not resolve instanceUrl for ${ORG_ALIAS}" 3

  echo "  org: ${ORG_ALIAS}"
  echo "  instance: ${INSTANCE_URL}"

  # Refuse production: only allow sandbox, scratch, or .develop.my.salesforce.com dev orgs
  if [[ "${INSTANCE_URL}" =~ \.sandbox\.my\.salesforce\.com$ ]] || \
     [[ "${INSTANCE_URL}" =~ \.scratch\.my\.salesforce\.com$ ]] || \
     [[ "${INSTANCE_URL}" =~ \.develop\.my\.salesforce\.com$ ]] || \
     [[ "${INSTANCE_URL}" =~ ^https://[^.]+--[^.]+\.sandbox\.my\.salesforce\.com$ ]]; then
    echo "  ✓ Org is sandbox/scratch/dev (safe)"
  else
    die "Org safety guard: ${INSTANCE_URL} is NOT a sandbox/scratch/dev org. Refusing to proceed." 3
  fi
fi

# ============================================================================
# STAGE 1: Emit agentSpec.yaml from derived JSON
# ============================================================================
banner "S1: agentSpec.yaml (local)"

S1_STATUS="unknown"
S1_EXIT=999
S1_START="$(date +%s)"
AGENT_SPEC_YAML="${OUT_DIR}/agentSpec.yaml"

set +e
"${PY_BIN}" - <<PY "${DERIVED_SPEC}" "${AGENT_SPEC_YAML}" >"${OUT_DIR}/logs/s1_agentspec.out" 2>"${OUT_DIR}/logs/s1_agentspec.err"
import sys, json, pathlib
from sf_video_blueprint.agentforce_spec import build_agent_spec_yaml, write_agent_spec_yaml, InsufficientEvidenceError
from sf_video_blueprint.spec_builder import DerivedAgentSpec, DerivedEntity, SpecEvidence

def parse_derived_spec(data: dict) -> DerivedAgentSpec:
    """Parse a JSON dict into a DerivedAgentSpec with proper nested dataclasses."""
    entities = [
        DerivedEntity(
            name=e["name"],
            object_api_name=e.get("object_api_name"),
            field_api_name=e.get("field_api_name"),
            evidence=[SpecEvidence(source=ev["source"], detail=ev["detail"]) for ev in e.get("evidence", [])]
        )
        for e in data.get("entities", [])
    ]
    evidence = [SpecEvidence(source=ev["source"], detail=ev["detail"]) for ev in data.get("evidence", [])]

    return DerivedAgentSpec(
        intent=data["intent"],
        confidence=data["confidence"],
        objects_touched=data.get("objects_touched", []),
        entities=entities,
        orchestration_steps=data.get("orchestration_steps", []),
        guardrails=data.get("guardrails", []),
        failure_handling=data.get("failure_handling", []),
        unknowns=data.get("unknowns", []),
        evidence=evidence,
    )

try:
    derived_path = pathlib.Path(sys.argv[1])
    out_path = pathlib.Path(sys.argv[2])
    derived_json = json.loads(derived_path.read_text(encoding="utf-8"))

    # Parse JSON to DerivedAgentSpec
    derived_spec = parse_derived_spec(derived_json)

    # Build the YAML structure
    spec_yaml = build_agent_spec_yaml(
        derived_spec,
        company_name=derived_json.get("company_name", "Unknown Company"),
        company_description=derived_json.get("company_description", "Unknown description"),
        allow_incomplete=False  # Fail loudly if evidence is insufficient
    )

    # Write to disk
    write_agent_spec_yaml(out_path, spec_yaml)
    print(f"✓ Wrote {out_path}")
except InsufficientEvidenceError as e:
    print(f"INSUFFICIENT_EVIDENCE: {e}", file=sys.stderr)
    sys.exit(5)
except Exception as e:
    print(f"ERROR: {e}", file=sys.stderr)
    import traceback
    traceback.print_exc(file=sys.stderr)
    sys.exit(1)
PY
S1_EXIT=$?
set -e

S1_END="$(date +%s)"
S1_DURATION=$((S1_END - S1_START))

if [[ ${S1_EXIT} -eq 0 ]]; then
  S1_STATUS="pass"
  echo "  ✓ agentSpec.yaml emitted: ${AGENT_SPEC_YAML}"
elif [[ ${S1_EXIT} -eq 5 ]]; then
  S1_STATUS="insufficient_evidence"
  echo "  ✗ Recording is inadequate (InsufficientEvidenceError)"
  tail -20 "${OUT_DIR}/logs/s1_agentspec.err"
else
  S1_STATUS="fail"
  echo "  ✗ agentSpec.yaml emission failed"
  tail -20 "${OUT_DIR}/logs/s1_agentspec.err"
fi

# ============================================================================
# STAGE 2: Emit local .agent and run its structural checks
# ============================================================================
banner "S2: Agent Script (local checks)"

S2_STATUS="unknown"
S2_EXIT=999
S2_START="$(date +%s)"
AGENT_SCRIPT_FILE="${OUT_DIR}/AgentScript.agent"

set +e
"${PY_BIN}" - <<PY "${DERIVED_SPEC}" "${AGENT_SCRIPT_FILE}" >"${OUT_DIR}/logs/s2_agentscript.out" 2>"${OUT_DIR}/logs/s2_agentscript.err"
import sys, json, pathlib
from sf_video_blueprint.agent_script import build_agent_script, write_agent_script, validate_locally, InsufficientEvidenceError
from sf_video_blueprint.spec_builder import DerivedAgentSpec, DerivedEntity, SpecEvidence

def parse_derived_spec(data: dict) -> DerivedAgentSpec:
    """Parse a JSON dict into a DerivedAgentSpec with proper nested dataclasses."""
    entities = [
        DerivedEntity(
            name=e["name"],
            object_api_name=e.get("object_api_name"),
            field_api_name=e.get("field_api_name"),
            evidence=[SpecEvidence(source=ev["source"], detail=ev["detail"]) for ev in e.get("evidence", [])]
        )
        for e in data.get("entities", [])
    ]
    evidence = [SpecEvidence(source=ev["source"], detail=ev["detail"]) for ev in data.get("evidence", [])]

    return DerivedAgentSpec(
        intent=data["intent"],
        confidence=data["confidence"],
        objects_touched=data.get("objects_touched", []),
        entities=entities,
        orchestration_steps=data.get("orchestration_steps", []),
        guardrails=data.get("guardrails", []),
        failure_handling=data.get("failure_handling", []),
        unknowns=data.get("unknowns", []),
        evidence=evidence,
    )

try:
    derived_path = pathlib.Path(sys.argv[1])
    out_path = pathlib.Path(sys.argv[2])
    derived_json = json.loads(derived_path.read_text(encoding="utf-8"))

    # Parse JSON to DerivedAgentSpec
    derived_spec = parse_derived_spec(derived_json)

    # Build Agent Script content
    script_content = build_agent_script(
        derived_spec,
        developer_name="test_agent",
        agent_label="Test Agent",
        description=derived_json.get("intent", "Test agent description"),
        allow_incomplete=False
    )

    # Run local structural checks
    errors = validate_locally(script_content)
    if errors:
        print("Local structural checks found issues:", file=sys.stderr)
        for err in errors:
            print(f"  - {err}", file=sys.stderr)
        print("NOTE: These are LOCAL checks only. CLI validation (S4) is authoritative.", file=sys.stderr)

    # Write to disk
    write_agent_script(out_path, script_content)
    print(f"✓ Wrote {out_path}")
    if errors:
        print(f"  (with {len(errors)} local warnings)")
except InsufficientEvidenceError as e:
    print(f"INSUFFICIENT_EVIDENCE: {e}", file=sys.stderr)
    sys.exit(5)
except Exception as e:
    print(f"ERROR: {e}", file=sys.stderr)
    import traceback
    traceback.print_exc(file=sys.stderr)
    sys.exit(1)
PY
S2_EXIT=$?
set -e

S2_END="$(date +%s)"
S2_DURATION=$((S2_END - S2_START))

if [[ ${S2_EXIT} -eq 0 ]]; then
  S2_STATUS="pass"
  echo "  ✓ Agent Script emitted (local structural checks ONLY): ${AGENT_SCRIPT_FILE}"
  echo "  NOTE: Local checks are NOT CLI validation — see S4 for real verdict"
elif [[ ${S2_EXIT} -eq 5 ]]; then
  S2_STATUS="insufficient_evidence"
  echo "  ✗ Recording is inadequate (InsufficientEvidenceError)"
  tail -20 "${OUT_DIR}/logs/s2_agentscript.err"
else
  S2_STATUS="fail"
  echo "  ✗ Agent Script emission failed"
  tail -20 "${OUT_DIR}/logs/s2_agentscript.err"
fi

# ============================================================================
# STAGE 3: sf agent generate authoring-bundle (REAL CLI)
# ============================================================================
S3_STATUS="skipped"
S3_EXIT=0
S3_DURATION=0
AUTHORING_BUNDLE_DIR="${OUT_DIR}/authoring_bundle"

if [[ "${DRY_RUN}" == "0" && "${S1_STATUS}" == "pass" ]]; then
  banner "S3: sf agent generate authoring-bundle"

  S3_START="$(date +%s)"
  mkdir -p "${AUTHORING_BUNDLE_DIR}"

  # Flags verified from --help:
  # -o/--target-org (required)
  # -f/--spec (path to spec YAML)
  # -n/--name (label)
  # --api-name (API name)
  # -d/--output-dir (where to write bundle)
  # --force-overwrite (don't prompt)

  set +e
  SF_DISABLE_LOG_FILE=true sf agent generate authoring-bundle \
    --target-org "${ORG_ALIAS}" \
    --spec "${AGENT_SPEC_YAML}" \
    --name "Roundtrip Test Agent" \
    --api-name "RoundtripTestAgent" \
    --output-dir "${AUTHORING_BUNDLE_DIR}" \
    --force-overwrite \
    >"${OUT_DIR}/logs/s3_authoring_bundle.out" 2>"${OUT_DIR}/logs/s3_authoring_bundle.err"
  S3_EXIT=$?
  set -e

  S3_END="$(date +%s)"
  S3_DURATION=$((S3_END - S3_START))

  if [[ ${S3_EXIT} -eq 0 ]]; then
    S3_STATUS="pass"
    echo "  ✓ Authoring bundle generated by CLI"
  else
    S3_STATUS="fail"
    echo "  ✗ sf agent generate authoring-bundle failed (exit ${S3_EXIT})"
    echo "  Last 20 lines of stderr:"
    tail -20 "${OUT_DIR}/logs/s3_authoring_bundle.err"
  fi
elif [[ "${DRY_RUN}" == "1" ]]; then
  echo "  S3: skipped (DRY_RUN=1)"
else
  echo "  S3: skipped (S1 failed)"
fi

# ============================================================================
# STAGE 4: sf agent validate authoring-bundle (AUTHORITATIVE grammar verdict)
# ============================================================================
S4_STATUS="skipped"
S4_EXIT=0
S4_DURATION=0

if [[ "${DRY_RUN}" == "0" && "${S3_STATUS}" == "pass" ]]; then
  banner "S4: sf agent validate authoring-bundle"

  S4_START="$(date +%s)"

  # Flags verified from --help:
  # -o/--target-org (required)
  # -n/--api-name (API name of bundle to validate)

  set +e
  SF_DISABLE_LOG_FILE=true sf agent validate authoring-bundle \
    --target-org "${ORG_ALIAS}" \
    --api-name "RoundtripTestAgent" \
    >"${OUT_DIR}/logs/s4_validate.out" 2>"${OUT_DIR}/logs/s4_validate.err"
  S4_EXIT=$?
  set -e

  S4_END="$(date +%s)"
  S4_DURATION=$((S4_END - S4_START))

  # Exit codes per --help:
  # 0 = success, 1 = compilation errors, 2 = 404 (API not available), 3 = 500 server error
  if [[ ${S4_EXIT} -eq 0 ]]; then
    S4_STATUS="pass"
    echo "  ✓ Agent Script validated by CLI — grammar is CORRECT"
  elif [[ ${S4_EXIT} -eq 1 ]]; then
    S4_STATUS="fail"
    echo "  ✗ Compilation errors in Agent Script"
    echo "  Last 20 lines of stderr:"
    tail -20 "${OUT_DIR}/logs/s4_validate.err"
  elif [[ ${S4_EXIT} -eq 2 ]]; then
    S4_STATUS="fail"
    echo "  ✗ Validation API returned 404 (not available in org/region)"
  elif [[ ${S4_EXIT} -eq 3 ]]; then
    S4_STATUS="fail"
    echo "  ✗ Validation API returned 500 (server error)"
  else
    S4_STATUS="fail"
    echo "  ✗ Validation failed with unexpected exit code ${S4_EXIT}"
  fi
elif [[ "${DRY_RUN}" == "1" ]]; then
  echo "  S4: skipped (DRY_RUN=1)"
else
  echo "  S4: skipped (S3 not executed or failed)"
fi

# ============================================================================
# STAGE 5: Generate test spec (both dialects)
# ============================================================================
banner "S5: Test spec (both dialects)"

S5_STATUS="unknown"
S5_EXIT=999
S5_START="$(date +%s)"
LEGACY_TEST_SPEC="${OUT_DIR}/testSpec-legacy.yaml"
NGT_TEST_SPEC="${OUT_DIR}/testSpec-ngt.yaml"

set +e
"${PY_BIN}" - <<PY "${DERIVED_SPEC}" "${LEGACY_TEST_SPEC}" "${NGT_TEST_SPEC}" >"${OUT_DIR}/logs/s5_testspec.out" 2>"${OUT_DIR}/logs/s5_testspec.err"
import sys, json, pathlib
from sf_video_blueprint.eval_spec import build_legacy_test_spec, build_ngt_test_spec, write_test_spec
from sf_video_blueprint.spec_builder import DerivedAgentSpec, DerivedEntity, SpecEvidence

def parse_derived_spec(data: dict) -> DerivedAgentSpec:
    """Parse a JSON dict into a DerivedAgentSpec with proper nested dataclasses."""
    entities = [
        DerivedEntity(
            name=e["name"],
            object_api_name=e.get("object_api_name"),
            field_api_name=e.get("field_api_name"),
            evidence=[SpecEvidence(source=ev["source"], detail=ev["detail"]) for ev in e.get("evidence", [])]
        )
        for e in data.get("entities", [])
    ]
    evidence = [SpecEvidence(source=ev["source"], detail=ev["detail"]) for ev in data.get("evidence", [])]

    return DerivedAgentSpec(
        intent=data["intent"],
        confidence=data["confidence"],
        objects_touched=data.get("objects_touched", []),
        entities=entities,
        orchestration_steps=data.get("orchestration_steps", []),
        guardrails=data.get("guardrails", []),
        failure_handling=data.get("failure_handling", []),
        unknowns=data.get("unknowns", []),
        evidence=evidence,
    )

try:
    derived_path = pathlib.Path(sys.argv[1])
    legacy_out = pathlib.Path(sys.argv[2])
    ngt_out = pathlib.Path(sys.argv[3])
    derived_json = json.loads(derived_path.read_text(encoding="utf-8"))

    # Parse JSON to DerivedAgentSpec
    derived_spec = parse_derived_spec(derived_json)

    # Build legacy test spec
    legacy_spec, legacy_derivations = build_legacy_test_spec(
        derived_spec,
        name="Test Agent Tests (Legacy)",
        subject_name="TestAgent",
        subject_type="AGENT"
    )
    write_test_spec(legacy_out, legacy_spec)
    print(f"✓ Legacy test spec: {legacy_out} ({len(legacy_spec.testCases)} test cases)")

    # Build NGT test spec
    ngt_spec, ngt_derivations = build_ngt_test_spec(
        derived_spec,
        name="Test Agent Tests (NGT)",
        subject_name="TestAgent"
    )
    write_test_spec(ngt_out, ngt_spec)
    print(f"✓ NGT test spec: {ngt_out} ({len(ngt_spec.testCases)} test cases)")
except Exception as e:
    print(f"ERROR: {e}", file=sys.stderr)
    import traceback
    traceback.print_exc(file=sys.stderr)
    sys.exit(1)
PY
S5_EXIT=$?
set -e

S5_END="$(date +%s)"
S5_DURATION=$((S5_END - S5_START))

if [[ ${S5_EXIT} -eq 0 ]]; then
  S5_STATUS="pass"
  echo "  ✓ Both test spec dialects emitted"
else
  S5_STATUS="fail"
  echo "  ✗ Test spec emission failed"
  tail -20 "${OUT_DIR}/logs/s5_testspec.err"
fi

# ============================================================================
# STAGE 6: Machine-readable summary
# ============================================================================
banner "S6: Summary"

SUMMARY_JSON="${OUT_DIR}/roundtrip_summary.json"

"${PY_BIN}" - <<PY "${SUMMARY_JSON}" "${S1_STATUS}" "${S1_EXIT}" "${S1_DURATION}" \
  "${S2_STATUS}" "${S2_EXIT}" "${S2_DURATION}" \
  "${S3_STATUS}" "${S3_EXIT}" "${S3_DURATION}" \
  "${S4_STATUS}" "${S4_EXIT}" "${S4_DURATION}" \
  "${S5_STATUS}" "${S5_EXIT}" "${S5_DURATION}"
import sys, json, pathlib

summary_path = pathlib.Path(sys.argv[1])
stages = {
  "s1_agentspec_yaml": {
    "status": sys.argv[2],
    "exit_code": int(sys.argv[3]),
    "duration_s": int(sys.argv[4]),
    "stdout_path": "logs/s1_agentspec.out",
    "stderr_path": "logs/s1_agentspec.err"
  },
  "s2_agent_script_local": {
    "status": sys.argv[5],
    "exit_code": int(sys.argv[6]),
    "duration_s": int(sys.argv[7]),
    "stdout_path": "logs/s2_agentscript.out",
    "stderr_path": "logs/s2_agentscript.err"
  },
  "s3_authoring_bundle_cli": {
    "status": sys.argv[8],
    "exit_code": int(sys.argv[9]),
    "duration_s": int(sys.argv[10]),
    "stdout_path": "logs/s3_authoring_bundle.out",
    "stderr_path": "logs/s3_authoring_bundle.err"
  },
  "s4_validate_cli": {
    "status": sys.argv[11],
    "exit_code": int(sys.argv[12]),
    "duration_s": int(sys.argv[13]),
    "stdout_path": "logs/s4_validate.out",
    "stderr_path": "logs/s4_validate.err"
  },
  "s5_test_spec": {
    "status": sys.argv[14],
    "exit_code": int(sys.argv[15]),
    "duration_s": int(sys.argv[16]),
    "stdout_path": "logs/s5_testspec.out",
    "stderr_path": "logs/s5_testspec.err"
  }
}

overall_pass = all(
  stage["status"] in ("pass", "skipped")
  for stage in stages.values()
)

summary = {
  "pass": overall_pass,
  "stages": stages
}

summary_path.write_text(json.dumps(summary, indent=2), encoding="utf-8")
print(json.dumps(summary, indent=2))
PY

echo ""
echo "========================================"
echo "  Roundtrip complete"
echo "========================================"
echo "Summary: ${SUMMARY_JSON}"
echo "Outputs: ${OUT_DIR}"
echo ""

# ============================================================================
# Exit code: non-zero if ANY executed stage failed
# ============================================================================
FINAL_EXIT=0

if [[ "${S1_STATUS}" == "fail" ]]; then FINAL_EXIT=1; fi
if [[ "${S1_STATUS}" == "insufficient_evidence" ]]; then FINAL_EXIT=5; fi
if [[ "${S2_STATUS}" == "fail" ]]; then FINAL_EXIT=1; fi
if [[ "${S3_STATUS}" == "fail" ]]; then FINAL_EXIT=1; fi
if [[ "${S4_STATUS}" == "fail" ]]; then FINAL_EXIT=1; fi
if [[ "${S5_STATUS}" == "fail" ]]; then FINAL_EXIT=1; fi

if [[ ${FINAL_EXIT} -eq 0 ]]; then
  echo "✓ All executed stages PASSED"
elif [[ ${FINAL_EXIT} -eq 5 ]]; then
  echo "✗ RECORDING INADEQUATE (InsufficientEvidenceError) — this is a legitimate, informative failure"
else
  echo "✗ AT LEAST ONE STAGE FAILED"
fi

exit ${FINAL_EXIT}

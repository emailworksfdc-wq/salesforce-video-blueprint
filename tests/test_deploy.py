"""Tests for deploy.py — validate-then-deploy workflow.

The subprocess boundary is injected, so the suite stays offline-clean in CI.
No real org is needed: the runner doubles simulate every CLI outcome.

Security properties tested explicitly:
- Forbidden org aliases (PPCDM, PPCaccenture) are refused before any CLI call.
- A deploy that Salesforce rejects returns REJECTED and does not silently pass.
- validate_only stops after validation; deploy is never attempted.
- dry_run is forwarded to the CLI argv.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from sf_video_blueprint.deploy import (
    DeployOutcome,
    DeployResult,
    deploy_bundle,
    _parse_deploy_output,
)

# Minimal valid agent source — just needs to be a non-empty string.
AGENT_SOURCE = """system:
    role: ->
        | A test agent.
"""

DEVELOPER_NAME = "SFVB_TEST_Deploy_Agent"


# ---------------------------------------------------------------------------
# Runner doubles
# ---------------------------------------------------------------------------


def _fake_runner(returncode: int = 0, stdout: str = "", stderr: str = ""):
    """Build a runner that never spawns a process."""
    calls: list[list[str]] = []

    def runner(cmd: list[str], *, cwd: Path, timeout: int):
        calls.append(list(cmd))

        class _Completed:
            pass

        done = _Completed()
        done.returncode = returncode
        done.stdout = stdout
        done.stderr = stderr
        return done

    runner.calls = calls  # type: ignore[attr-defined]
    return runner


def _compile_success_payload() -> str:
    return json.dumps({
        "status": 0,
        "result": {"success": True},
    })


def _compile_failure_payload() -> str:
    return json.dumps({
        "status": 1,
        "data": {
            "success": False,
            "errors": [
                {"description": "Syntax error: unexpected `->` [Ln 12, Col 8]"},
                {"description": "Another compiler error"},
            ],
        },
    })


def _deploy_success_payload() -> str:
    return json.dumps({
        "status": 0,
        "result": {"success": True},
    })


def _deploy_failure_payload() -> str:
    return json.dumps({
        "status": 1,
        "result": {
            "success": False,
            "details": {
                "failures": [
                    {"message": "Deploy failed: metadata mismatch"},
                ]
            },
        },
    })


def _two_step_runner(validate_stdout: str, deploy_stdout: str, returncode: int = 0):
    """Runner that returns different stdout on first and second calls."""
    calls: list[list[str]] = []
    responses = [validate_stdout, deploy_stdout]
    idx = [0]

    def runner(cmd: list[str], *, cwd: Path, timeout: int):
        calls.append(list(cmd))
        stdout = responses[min(idx[0], len(responses) - 1)]
        idx[0] += 1

        class _Completed:
            pass

        done = _Completed()
        done.returncode = returncode
        done.stdout = stdout
        done.stderr = ""
        return done

    runner.calls = calls  # type: ignore[attr-defined]
    return runner


# ---------------------------------------------------------------------------
# DeployResult dataclass
# ---------------------------------------------------------------------------


class TestDeployResult:
    def test_succeeded_is_true_for_deployed(self) -> None:
        r = DeployResult(outcome=DeployOutcome.DEPLOYED, deployed=True)
        assert r.succeeded is True

    def test_succeeded_is_true_for_dry_run(self) -> None:
        r = DeployResult(outcome=DeployOutcome.DRY_RUN, dry_run=True)
        assert r.succeeded is True

    def test_succeeded_is_false_for_rejected(self) -> None:
        r = DeployResult(outcome=DeployOutcome.REJECTED)
        assert r.succeeded is False

    def test_succeeded_is_false_for_error(self) -> None:
        r = DeployResult(outcome=DeployOutcome.ERROR)
        assert r.succeeded is False

    def test_succeeded_is_false_for_blocked(self) -> None:
        r = DeployResult(outcome=DeployOutcome.BLOCKED)
        assert r.succeeded is False

    def test_succeeded_is_false_for_skipped(self) -> None:
        r = DeployResult(outcome=DeployOutcome.SKIPPED)
        assert r.succeeded is False

    def test_default_lists_are_empty(self) -> None:
        r = DeployResult(outcome=DeployOutcome.SKIPPED)
        assert r.validation_errors == []
        assert r.deploy_errors == []

    def test_deployed_defaults_to_false(self) -> None:
        r = DeployResult(outcome=DeployOutcome.DEPLOYED)
        assert r.deployed is False  # caller must set it explicitly

    def test_compiled_defaults_to_false(self) -> None:
        r = DeployResult(outcome=DeployOutcome.VALIDATED)
        assert r.compiled is False


# ---------------------------------------------------------------------------
# _parse_deploy_output
# ---------------------------------------------------------------------------


class TestParseDeployOutput:
    def test_success_result(self) -> None:
        succeeded, errors, detail = _parse_deploy_output(
            _deploy_success_payload(), ""
        )
        assert succeeded is True
        assert errors == []

    def test_failure_with_messages(self) -> None:
        succeeded, errors, detail = _parse_deploy_output(
            _deploy_failure_payload(), ""
        )
        assert succeeded is False
        assert any("Deploy failed" in e for e in errors)

    def test_no_json_returns_false(self) -> None:
        succeeded, errors, detail = _parse_deploy_output("no json here", "err msg")
        assert succeeded is False
        assert errors

    def test_non_object_json_returns_false(self) -> None:
        succeeded, errors, detail = _parse_deploy_output("[1,2,3]", "")
        assert succeeded is False

    def test_empty_stdout_returns_false(self) -> None:
        succeeded, errors, detail = _parse_deploy_output("", "cli failed")
        assert succeeded is False

    def test_status_zero_without_success_key(self) -> None:
        """result.status=0 is accepted as success even without success key."""
        payload = json.dumps({"status": 0, "result": {"status": 0}})
        succeeded, errors, detail = _parse_deploy_output(payload, "")
        assert succeeded is True

    def test_ansi_escapes_are_stripped(self) -> None:
        """CLI may colourise its output even when --json is passed."""
        ansi_prefix = "\x1b[32m"
        payload = ansi_prefix + _deploy_success_payload()
        succeeded, errors, detail = _parse_deploy_output(payload, "")
        assert succeeded is True


# ---------------------------------------------------------------------------
# deploy_bundle — no org
# ---------------------------------------------------------------------------


class TestDeployBundleNoOrg:
    def test_no_org_alias_returns_skipped(self, tmp_path: Path) -> None:
        runner = _fake_runner()
        result = deploy_bundle(
            AGENT_SOURCE,
            developer_name=DEVELOPER_NAME,
            org_alias=None,
            project_dir=tmp_path,
            runner=runner,
        )

        assert result.outcome is DeployOutcome.SKIPPED
        assert runner.calls == [], "No CLI call must be made when org_alias is None"
        assert result.succeeded is False

    def test_skipped_result_includes_helpful_detail(self, tmp_path: Path) -> None:
        result = deploy_bundle(
            AGENT_SOURCE,
            developer_name=DEVELOPER_NAME,
            org_alias=None,
            project_dir=tmp_path,
        )
        assert result.detail  # must explain why it was skipped


# ---------------------------------------------------------------------------
# deploy_bundle — forbidden orgs
# ---------------------------------------------------------------------------


class TestDeployBundleForbiddenOrg:
    @pytest.mark.parametrize("alias", ["PPCDM", "PPCaccenture", "ppcdm", "ppcaccenture"])
    def test_forbidden_org_is_blocked_before_any_cli_call(
        self, alias: str, tmp_path: Path
    ) -> None:
        runner = _fake_runner()
        result = deploy_bundle(
            AGENT_SOURCE,
            developer_name=DEVELOPER_NAME,
            org_alias=alias,
            project_dir=tmp_path,
            runner=runner,
        )

        assert result.outcome is DeployOutcome.BLOCKED
        assert runner.calls == [], "A blocked org must not reach the CLI"
        assert "out of scope" in result.detail.lower()

    def test_blocked_result_is_not_a_success(self, tmp_path: Path) -> None:
        result = deploy_bundle(
            AGENT_SOURCE,
            developer_name=DEVELOPER_NAME,
            org_alias="PPCDM",
            project_dir=tmp_path,
        )
        assert result.succeeded is False


# ---------------------------------------------------------------------------
# deploy_bundle — validate-then-deploy (happy path)
# ---------------------------------------------------------------------------


class TestDeployBundleHappyPath:
    def test_full_deploy_runs_two_cli_calls(self, tmp_path: Path) -> None:
        runner = _two_step_runner(
            _compile_success_payload(),
            _deploy_success_payload(),
        )
        result = deploy_bundle(
            AGENT_SOURCE,
            developer_name=DEVELOPER_NAME,
            org_alias="test-sandbox",
            project_dir=tmp_path,
            runner=runner,
        )

        assert result.outcome is DeployOutcome.DEPLOYED
        assert result.compiled is True
        assert result.deployed is True
        assert result.succeeded is True
        # Two calls: sf agent validate authoring-bundle AND sf project deploy start
        assert len(runner.calls) == 2

    def test_validate_call_uses_sf_agent_validate(self, tmp_path: Path) -> None:
        runner = _two_step_runner(
            _compile_success_payload(),
            _deploy_success_payload(),
        )
        deploy_bundle(
            AGENT_SOURCE,
            developer_name=DEVELOPER_NAME,
            org_alias="test-sandbox",
            project_dir=tmp_path,
            runner=runner,
        )
        validate_cmd = runner.calls[0]
        assert "sf" in validate_cmd
        assert "agent" in validate_cmd
        assert "validate" in validate_cmd
        assert "authoring-bundle" in validate_cmd

    def test_deploy_call_uses_sf_project_deploy_start(self, tmp_path: Path) -> None:
        runner = _two_step_runner(
            _compile_success_payload(),
            _deploy_success_payload(),
        )
        deploy_bundle(
            AGENT_SOURCE,
            developer_name=DEVELOPER_NAME,
            org_alias="test-sandbox",
            project_dir=tmp_path,
            runner=runner,
        )
        deploy_cmd = runner.calls[1]
        assert "sf" in deploy_cmd
        assert "project" in deploy_cmd
        assert "deploy" in deploy_cmd
        assert "start" in deploy_cmd

    def test_no_credential_on_argv(self, tmp_path: Path) -> None:
        """Tokens must never appear on the command line (world-readable via ps)."""
        runner = _two_step_runner(
            _compile_success_payload(),
            _deploy_success_payload(),
        )
        deploy_bundle(
            AGENT_SOURCE,
            developer_name=DEVELOPER_NAME,
            org_alias="test-sandbox",
            project_dir=tmp_path,
            runner=runner,
        )
        for call in runner.calls:
            combined = " ".join(call).lower()
            assert "password" not in combined
            assert "token" not in combined
            assert "sid=" not in combined
            assert "frontdoor" not in combined


# ---------------------------------------------------------------------------
# deploy_bundle — validate-only
# ---------------------------------------------------------------------------


class TestDeployBundleValidateOnly:
    def test_validate_only_runs_one_cli_call(self, tmp_path: Path) -> None:
        runner = _fake_runner(
            returncode=0,
            stdout=_compile_success_payload(),
        )
        result = deploy_bundle(
            AGENT_SOURCE,
            developer_name=DEVELOPER_NAME,
            org_alias="test-sandbox",
            project_dir=tmp_path,
            validate_only=True,
            runner=runner,
        )

        assert result.outcome is DeployOutcome.VALIDATED
        assert result.compiled is True
        assert result.deployed is False
        assert len(runner.calls) == 1, "validate-only must not run sf project deploy start"

    def test_validate_only_detail_mentions_no_deploy(self, tmp_path: Path) -> None:
        runner = _fake_runner(
            returncode=0,
            stdout=_compile_success_payload(),
        )
        result = deploy_bundle(
            AGENT_SOURCE,
            developer_name=DEVELOPER_NAME,
            org_alias="test-sandbox",
            project_dir=tmp_path,
            validate_only=True,
            runner=runner,
        )
        assert "deploy not attempted" in result.detail.lower() or "validate-only" in result.detail.lower()

    def test_validate_only_rejected_returns_rejected(self, tmp_path: Path) -> None:
        runner = _fake_runner(
            returncode=1,
            stdout=_compile_failure_payload(),
        )
        result = deploy_bundle(
            AGENT_SOURCE,
            developer_name=DEVELOPER_NAME,
            org_alias="test-sandbox",
            project_dir=tmp_path,
            validate_only=True,
            runner=runner,
        )
        assert result.outcome is DeployOutcome.REJECTED
        assert result.validation_errors
        assert result.compiled is False
        assert result.deployed is False


# ---------------------------------------------------------------------------
# deploy_bundle — rejected by compiler
# ---------------------------------------------------------------------------


class TestDeployBundleRejected:
    def test_compiler_rejection_stops_before_deploy(self, tmp_path: Path) -> None:
        """When validation fails, sf project deploy start must NOT run."""
        runner = _two_step_runner(
            _compile_failure_payload(),
            _deploy_success_payload(),
        )
        result = deploy_bundle(
            AGENT_SOURCE,
            developer_name=DEVELOPER_NAME,
            org_alias="test-sandbox",
            project_dir=tmp_path,
            runner=runner,
        )

        assert result.outcome is DeployOutcome.REJECTED
        assert result.compiled is False
        assert result.deployed is False
        assert result.validation_errors
        assert len(runner.calls) == 1, "Deploy must not run after a compiler rejection"

    def test_rejected_result_carries_verbatim_errors(self, tmp_path: Path) -> None:
        runner = _fake_runner(returncode=1, stdout=_compile_failure_payload())
        result = deploy_bundle(
            AGENT_SOURCE,
            developer_name=DEVELOPER_NAME,
            org_alias="test-sandbox",
            project_dir=tmp_path,
            runner=runner,
        )
        assert any("Syntax error" in e for e in result.validation_errors)

    def test_rejected_result_is_not_succeeded(self, tmp_path: Path) -> None:
        runner = _fake_runner(returncode=1, stdout=_compile_failure_payload())
        result = deploy_bundle(
            AGENT_SOURCE,
            developer_name=DEVELOPER_NAME,
            org_alias="test-sandbox",
            project_dir=tmp_path,
            runner=runner,
        )
        assert result.succeeded is False


# ---------------------------------------------------------------------------
# deploy_bundle — dry run
# ---------------------------------------------------------------------------


class TestDeployBundleDryRun:
    def test_dry_run_passes_flag_to_deploy_command(self, tmp_path: Path) -> None:
        runner = _two_step_runner(
            _compile_success_payload(),
            _deploy_success_payload(),
        )
        result = deploy_bundle(
            AGENT_SOURCE,
            developer_name=DEVELOPER_NAME,
            org_alias="test-sandbox",
            project_dir=tmp_path,
            dry_run=True,
            runner=runner,
        )

        assert result.outcome is DeployOutcome.DRY_RUN
        assert result.dry_run is True
        assert result.deployed is False
        # The --dry-run flag must appear on the deploy call's argv
        deploy_cmd = runner.calls[1]
        assert "--dry-run" in deploy_cmd

    def test_dry_run_still_validates_first(self, tmp_path: Path) -> None:
        runner = _two_step_runner(
            _compile_success_payload(),
            _deploy_success_payload(),
        )
        deploy_bundle(
            AGENT_SOURCE,
            developer_name=DEVELOPER_NAME,
            org_alias="test-sandbox",
            project_dir=tmp_path,
            dry_run=True,
            runner=runner,
        )
        # Both calls must run
        assert len(runner.calls) == 2

    def test_dry_run_rejected_compile_stops_before_deploy(self, tmp_path: Path) -> None:
        runner = _two_step_runner(
            _compile_failure_payload(),
            _deploy_success_payload(),
        )
        result = deploy_bundle(
            AGENT_SOURCE,
            developer_name=DEVELOPER_NAME,
            org_alias="test-sandbox",
            project_dir=tmp_path,
            dry_run=True,
            runner=runner,
        )
        assert result.outcome is DeployOutcome.REJECTED
        assert len(runner.calls) == 1


# ---------------------------------------------------------------------------
# deploy_bundle — CLI not found
# ---------------------------------------------------------------------------


class TestDeployBundleCliNotFound:
    def test_cli_not_found_returns_error(self, tmp_path: Path) -> None:
        def raising_runner(cmd, *, cwd, timeout):
            raise FileNotFoundError("sf: not found")

        result = deploy_bundle(
            AGENT_SOURCE,
            developer_name=DEVELOPER_NAME,
            org_alias="test-sandbox",
            project_dir=tmp_path,
            runner=raising_runner,
        )
        # CompileResult.ERROR from validate_bundle_with_org propagates as our ERROR
        assert result.outcome is DeployOutcome.ERROR
        assert result.succeeded is False

    def test_timeout_returns_error(self, tmp_path: Path) -> None:
        def timeout_runner(cmd, *, cwd, timeout):
            raise subprocess.TimeoutExpired(cmd, timeout)

        result = deploy_bundle(
            AGENT_SOURCE,
            developer_name=DEVELOPER_NAME,
            org_alias="test-sandbox",
            project_dir=tmp_path,
            runner=timeout_runner,
        )
        assert result.outcome is DeployOutcome.ERROR


# ---------------------------------------------------------------------------
# deploy_bundle — org alias handling
# ---------------------------------------------------------------------------


class TestDeployBundleOrgAliasHandling:
    def test_org_alias_appears_in_validate_command(self, tmp_path: Path) -> None:
        runner = _two_step_runner(
            _compile_success_payload(),
            _deploy_success_payload(),
        )
        deploy_bundle(
            AGENT_SOURCE,
            developer_name=DEVELOPER_NAME,
            org_alias="my-sandbox",
            project_dir=tmp_path,
            runner=runner,
        )
        validate_cmd = runner.calls[0]
        assert "my-sandbox" in validate_cmd

    def test_org_alias_appears_in_deploy_command(self, tmp_path: Path) -> None:
        runner = _two_step_runner(
            _compile_success_payload(),
            _deploy_success_payload(),
        )
        deploy_bundle(
            AGENT_SOURCE,
            developer_name=DEVELOPER_NAME,
            org_alias="my-sandbox",
            project_dir=tmp_path,
            runner=runner,
        )
        deploy_cmd = runner.calls[1]
        assert "my-sandbox" in deploy_cmd

    def test_developer_name_in_result(self, tmp_path: Path) -> None:
        runner = _two_step_runner(
            _compile_success_payload(),
            _deploy_success_payload(),
        )
        result = deploy_bundle(
            AGENT_SOURCE,
            developer_name=DEVELOPER_NAME,
            org_alias="my-sandbox",
            project_dir=tmp_path,
            runner=runner,
        )
        assert result.developer_name == DEVELOPER_NAME
        assert result.org_alias == "my-sandbox"


# ---------------------------------------------------------------------------
# MCP run_deploy
# ---------------------------------------------------------------------------


class TestMcpRunDeploy:
    """Integration tests for the run_deploy MCP tool.

    These test the MCP layer without spawning a subprocess: they check the
    guard logic (forbidden org, missing file, etc.) and the result envelope.
    """

    @pytest.fixture(autouse=True)
    def _require_mcp(self) -> None:
        pytest.importorskip("mcp", reason="the optional [mcp] extra is not installed")

    def test_run_deploy_refuses_forbidden_org(self) -> None:
        from sf_video_blueprint import mcp_server
        from pathlib import Path

        example = Path(__file__).parent.parent / "examples" / "case_triage.dom_capture.jsonl"
        result = mcp_server.run_deploy(
            str(example),
            developer_name="A",
            agent_label="A",
            org_alias="PPCDM",
        )
        assert result["ok"] is False
        assert "scope" in result["error"]["message"].lower()

    def test_run_deploy_refuses_ppaccenture(self) -> None:
        from sf_video_blueprint import mcp_server
        from pathlib import Path

        example = Path(__file__).parent.parent / "examples" / "case_triage.dom_capture.jsonl"
        result = mcp_server.run_deploy(
            str(example),
            developer_name="A",
            agent_label="A",
            org_alias="PPCaccenture",
        )
        assert result["ok"] is False
        assert result["error"]["code"] == mcp_server.ERROR_VALIDATION

    def test_run_deploy_missing_capture_returns_not_found(self) -> None:
        from sf_video_blueprint import mcp_server

        result = mcp_server.run_deploy(
            "/nonexistent/capture.jsonl",
            developer_name="A",
            agent_label="A",
            org_alias="my-sandbox",
        )
        assert result["ok"] is False
        assert result["error"]["code"] == mcp_server.ERROR_NOT_FOUND

    def test_run_deploy_skipped_without_org_alias_forbidden(self) -> None:
        """A valid org that just doesn't have CLI should fail gracefully."""
        from sf_video_blueprint import mcp_server
        from pathlib import Path

        example = Path(__file__).parent.parent / "examples" / "case_triage.dom_capture.jsonl"
        # Pass a non-forbidden alias; sf CLI won't be available in CI so we
        # expect an error result but not a security violation.
        result = mcp_server.run_deploy(
            str(example),
            developer_name="Case_Triage_Agent",
            agent_label="Case Triage Agent",
            org_alias="ci-sandbox",
            validate_only=True,
        )
        # Either ok with outcome=skipped/error, or error — but not a crash
        assert "ok" in result

    def test_run_deploy_result_is_json_serializable(self) -> None:
        from sf_video_blueprint import mcp_server
        from pathlib import Path
        import json

        example = Path(__file__).parent.parent / "examples" / "case_triage.dom_capture.jsonl"
        result = mcp_server.run_deploy(
            str(example),
            developer_name="A",
            agent_label="A",
            org_alias="PPCDM",  # will be blocked — still JSON-safe
        )
        # Must be JSON-serializable regardless of outcome
        serialized = json.dumps(result)
        assert json.loads(serialized) == result

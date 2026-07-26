"""Tests for the compiler-validation call site.

The gap this closes: every org validation in this project was performed by hand
at a shell prompt. No code path invoked the compiler, so nothing in the repo
could tell you whether the bundle it just emitted compiles. `validate_locally`
is the project's own opinion; `sf agent validate authoring-bundle` is
Salesforce's.

These tests never touch a real org. The subprocess boundary is injected, so the
suite stays offline-clean in CI: the only test that would need an org is the one
asserting we SKIP when no org is configured.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from sf_video_blueprint.org_validation import (
    CompileOutcome,
    CompileResult,
    org_is_forbidden,
    validate_bundle_with_org,
    write_validation_project,
)

AGENT_SOURCE = """system:
    role: ->
        | A test agent.
"""


def _fake_runner(returncode: int, stdout: str = "", stderr: str = ""):
    """Build a runner double that records the argv it was handed."""
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


# ---------------------------------------------------------------------------
# Skipping when there is no org — the CI-safety property
# ---------------------------------------------------------------------------


def test_no_org_configured_skips_cleanly_without_invoking_the_cli(tmp_path):
    """With no org alias, the compiler is never invoked and this is not a failure.

    CI has no Salesforce org. A validation step that errors there would be
    removed within a week; a step that silently reports "valid" would be worse.
    SKIPPED is the only honest third state.
    """
    runner = _fake_runner(0)
    result = validate_bundle_with_org(
        AGENT_SOURCE,
        developer_name="SFVB_TEST_Agent",
        org_alias=None,
        project_dir=tmp_path,
        runner=runner,
    )

    assert result.outcome is CompileOutcome.SKIPPED
    assert result.compiled is False, "A skipped check must never read as a pass"
    assert runner.calls == [], "No org means no CLI invocation at all"
    assert "no org" in result.detail.lower()


def test_skipped_result_is_not_a_failure():
    """SKIPPED must be distinguishable from REJECTED by callers."""
    skipped = CompileResult(outcome=CompileOutcome.SKIPPED, detail="no org")
    rejected = CompileResult(outcome=CompileOutcome.REJECTED, detail="bad", errors=["x"])

    assert skipped.is_failure is False
    assert rejected.is_failure is True


# ---------------------------------------------------------------------------
# The two real verdicts
# ---------------------------------------------------------------------------


def test_exit_zero_with_success_true_is_a_compile_pass(tmp_path):
    runner = _fake_runner(0, stdout=json.dumps({"status": 0, "data": {"success": True, "errors": []}}))
    result = validate_bundle_with_org(
        AGENT_SOURCE,
        developer_name="SFVB_TEST_Agent",
        org_alias="AFT3",
        project_dir=tmp_path,
        runner=runner,
    )

    assert result.outcome is CompileOutcome.COMPILED
    assert result.compiled is True
    assert result.errors == []


def test_compiler_errors_are_surfaced_verbatim(tmp_path):
    """The compiler's own words, not a paraphrase.

    Verbatim text is the whole value of this call: `Too big: expected string to
    have <=80 characters` tells an operator exactly what to change, and a
    reworded version does not.
    """
    payload = {
        "status": 1,
        "data": {
            "success": False,
            "errors": [
                {"description": "Too big: expected string to have <=80 characters"},
                {"description": "Cannot invoke '@apex.Foo' — 'apex' is not a valid invocation target."},
            ],
        },
    }
    runner = _fake_runner(1, stdout=json.dumps(payload))
    result = validate_bundle_with_org(
        AGENT_SOURCE,
        developer_name="SFVB_TEST_Agent",
        org_alias="AFT3",
        project_dir=tmp_path,
        runner=runner,
    )

    assert result.outcome is CompileOutcome.REJECTED
    assert result.compiled is False
    assert "Too big: expected string to have <=80 characters" in result.errors
    assert any("not a valid invocation target" in e for e in result.errors)


def test_ansi_escapes_are_stripped_before_json_parsing(tmp_path):
    """The CLI colourises even with --json; a naive json.loads() dies on it."""
    raw = "\x1b[33mwarning\x1b[0m\n" + json.dumps({"status": 0, "data": {"success": True, "errors": []}})
    runner = _fake_runner(0, stdout=raw)
    result = validate_bundle_with_org(
        AGENT_SOURCE,
        developer_name="SFVB_TEST_Agent",
        org_alias="AFT3",
        project_dir=tmp_path,
        runner=runner,
    )

    assert result.outcome is CompileOutcome.COMPILED


def test_unparseable_output_is_an_error_not_a_pass(tmp_path):
    """Fail closed. Garbage from the CLI must never be read as success."""
    runner = _fake_runner(0, stdout="not json at all")
    result = validate_bundle_with_org(
        AGENT_SOURCE,
        developer_name="SFVB_TEST_Agent",
        org_alias="AFT3",
        project_dir=tmp_path,
        runner=runner,
    )

    assert result.outcome is CompileOutcome.ERROR
    assert result.compiled is False


def test_missing_cli_is_reported_as_error_not_rejection(tmp_path):
    """A missing `sf` binary is an environment problem, not a bad bundle."""

    def runner(cmd, *, cwd, timeout):
        raise FileNotFoundError("sf")

    result = validate_bundle_with_org(
        AGENT_SOURCE,
        developer_name="SFVB_TEST_Agent",
        org_alias="AFT3",
        project_dir=tmp_path,
        runner=runner,
    )

    assert result.outcome is CompileOutcome.ERROR
    assert result.compiled is False
    assert "sf" in result.detail


# ---------------------------------------------------------------------------
# Safety rules that apply to any org call in this repo
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("alias", ["PPCDM", "PPCaccenture", "ppcdm", "ppaccenture"])
def test_forbidden_orgs_are_blocked_before_any_cli_call(alias, tmp_path):
    """PPCDM / PPCaccenture are out of scope, not even read-only.

    Blocked BEFORE the subprocess, so a typo'd alias cannot reach the network.
    """
    runner = _fake_runner(0)
    result = validate_bundle_with_org(
        AGENT_SOURCE,
        developer_name="SFVB_TEST_Agent",
        org_alias=alias,
        project_dir=tmp_path,
        runner=runner,
    )

    assert result.outcome is CompileOutcome.BLOCKED
    assert result.compiled is False
    assert runner.calls == [], "A forbidden org must not reach the CLI"


def test_invocation_passes_the_org_alias_and_api_name(tmp_path):
    runner = _fake_runner(0, stdout=json.dumps({"status": 0, "data": {"success": True, "errors": []}}))
    validate_bundle_with_org(
        AGENT_SOURCE,
        developer_name="SFVB_TEST_Agent",
        org_alias="AFT3",
        project_dir=tmp_path,
        runner=runner,
    )

    assert len(runner.calls) == 1
    cmd = runner.calls[0]
    assert cmd[:4] == ["sf", "agent", "validate", "authoring-bundle"]
    assert "--json" in cmd
    assert cmd[cmd.index("--target-org") + 1] == "AFT3"
    # The flag is `--api-name`, not `--name`. `--name` is what the command looks
    # like it should take, and passing it produced `Nonexistent flag: --name`
    # against a real org — which the parser then reported as a compiler rejection
    # of the bundle. Pin the exact spelling.
    assert "--name" not in cmd
    assert cmd[cmd.index("--api-name") + 1] == "SFVB_TEST_Agent"


def test_a_cli_usage_error_is_not_reported_as_a_rejected_bundle(tmp_path):
    """`Nonexistent flag: --x` says the invocation is wrong, not the bundle.

    Misfiling this as REJECTED is the worst available failure: it sends the
    operator to fix an emitter that was never broken, and a later green run would
    look like the emitter fix worked.
    """
    payload = {
        "status": 1,
        "name": "Error",
        "message": "Nonexistent flag: --name\nSee more help with --help",
    }
    runner = _fake_runner(1, stdout=json.dumps(payload))
    result = validate_bundle_with_org(
        AGENT_SOURCE,
        developer_name="SFVB_TEST_Agent",
        org_alias="AFT3",
        project_dir=tmp_path,
        runner=runner,
    )

    assert result.outcome is CompileOutcome.ERROR
    assert result.compiled is False
    assert "Nonexistent flag" in result.detail


def test_a_bundle_the_cli_cannot_find_is_an_error_not_a_rejection(tmp_path):
    """"No authoring bundle found" is a wiring problem, not a grammar verdict."""
    payload = {
        "status": 1,
        "name": "NoAuthoringBundleFoundError",
        "message": "No authoring bundle found with the name SFVB_TEST_Agent",
    }
    runner = _fake_runner(1, stdout=json.dumps(payload))
    result = validate_bundle_with_org(
        AGENT_SOURCE,
        developer_name="SFVB_TEST_Agent",
        org_alias="AFT3",
        project_dir=tmp_path,
        runner=runner,
    )

    assert result.outcome is CompileOutcome.ERROR
    assert result.compiled is False


def test_the_suite_never_scaffolds_into_the_repo_root():
    """No test may leave a force-app/ tree beside the source.

    An earlier draft of these tests passed `project_dir=Path(".")`, which wrote a
    real `sfdx-project.json` and a bundle into the working tree. Harmless-looking,
    but it makes `git status` dirty and would eventually get committed.
    """
    repo_root = Path(__file__).parent.parent

    assert not (repo_root / "force-app").exists(), (
        "A test scaffolded a validation project into the repo root. "
        "Pass a tmp_path as project_dir."
    )
    assert not (repo_root / "sfdx-project.json").exists()


def test_no_token_is_ever_placed_on_the_argv(tmp_path):
    """Never pass a token as argv: it is world-readable via `ps`.

    The org connection is by alias only; the CLI resolves the credential itself.
    """
    runner = _fake_runner(0, stdout=json.dumps({"status": 0, "data": {"success": True, "errors": []}}))
    validate_bundle_with_org(
        AGENT_SOURCE,
        developer_name="SFVB_TEST_Agent",
        org_alias="AFT3",
        project_dir=tmp_path,
        runner=runner,
    )

    flat = " ".join(runner.calls[0]).lower()
    for leak in ("sid=", "access_token", "sessionid", "frontdoor", "--token"):
        assert leak not in flat, f"Secret-shaped argument on argv: {leak}"


# ---------------------------------------------------------------------------
# The project scaffold the CLI requires
# ---------------------------------------------------------------------------


def test_validation_project_is_scaffolded_for_the_cli(tmp_path):
    """`sf agent validate authoring-bundle` sets requiresProject = true.

    It needs a project root and finds the bundle by API name under a package
    directory, so the caller cannot just hand it a loose .agent file.
    """
    bundle_dir = write_validation_project(
        tmp_path, AGENT_SOURCE, developer_name="SFVB_TEST_Agent"
    )

    project_json = tmp_path / "sfdx-project.json"
    assert project_json.is_file(), "The CLI requires a project root"
    config = json.loads(project_json.read_text(encoding="utf-8"))
    assert config["packageDirectories"][0]["path"] == "force-app"

    assert (bundle_dir / "SFVB_TEST_Agent.agent").read_text(encoding="utf-8") == AGENT_SOURCE
    assert (bundle_dir / "SFVB_TEST_Agent.bundle-meta.xml").is_file()
    assert bundle_dir.parent.name == "aiAuthoringBundles"


def test_scaffold_writes_the_agent_source_byte_for_byte(tmp_path):
    """What gets compiled must be exactly what was emitted.

    Any normalisation here would mean the verdict describes a different file
    than the one the caller holds.
    """
    source = "system:\n    role: ->\n        | Trailing space kept.   \n\n\n"
    bundle_dir = write_validation_project(tmp_path, source, developer_name="SFVB_TEST_Bytes")
    written = (bundle_dir / "SFVB_TEST_Bytes.agent").read_text(encoding="utf-8")

    assert written == source


# --- the deny-list must not be reimplemented here -----------------------------


@pytest.mark.parametrize(
    "alias",
    [
        "PPCDM",
        "ppcdm",
        "PpCdM",
        " PPCDM ",
        "ppc-dm",
        "PPC_DM",
        "ppc.dm",
        "PPCaccenture",
        "ppcaccenture",
        "PPCACCENTURE",
        " PPCaccenture ",
        "ppc-accenture",
        "PPC_Accenture",
        "ppc accenture",
    ],
)
def test_every_spelling_of_a_blocked_org_is_refused(alias):
    """This module is a code path that CONTACTS an org, so its guard must not be weaker
    than the canonical one.

    It originally carried a third private copy of the deny-list matched with
    `.strip().lower()`. Measured: that accepted `ppc-dm`, `PPC_DM`, `ppc.dm`,
    `ppc-accenture`, `PPC_Accenture` and `ppc accenture` — six spellings the
    canonical `org_denylist` refuses — and it inherited the misspelled
    `ppaccenture` entry whose missing `c` was the original bypass.
    """
    assert org_is_forbidden(alias), f"{alias!r} reached an org-contacting path"


@pytest.mark.parametrize("alias", ["AFT3", "AFTDX5", "na-dev", "TD2", "TDProj"])
def test_permitted_dev_orgs_are_not_swept_up(alias):
    """A guard that refuses everything is an outage, not a control."""
    assert not org_is_forbidden(alias)


def test_this_module_does_not_define_its_own_denylist():
    """One normalizing implementation, not four.

    Asserted on the source rather than on behaviour, because a reintroduced private
    set would pass the parametrized tests above on the day it was written and only
    diverge later — which is exactly how the first three copies rotted apart.
    """
    source = (
        Path(__file__).resolve().parent.parent
        / "src"
        / "sf_video_blueprint"
        / "org_validation.py"
    ).read_text(encoding="utf-8")

    assert "ppcdm" not in source.lower().replace("ppcdm`", ""), (
        "org_validation.py names a blocked alias directly; it must delegate to "
        "org_denylist instead of carrying its own copy."
    )
    assert "org_denylist import" in source, "org_validation.py must import the canonical guard"

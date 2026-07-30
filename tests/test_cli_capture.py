from __future__ import annotations
import sys
from pathlib import Path
from unittest import mock
import pytest
from typer.testing import CliRunner
import re
from sf_video_blueprint.cli import app


def _strip_ansi(text: str) -> str:
    """Remove ANSI escape codes from terminal output."""
    return re.sub(r"\[[0-9;]*m", "", text)


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


# 1. capture --help shows all options

def test_capture_help_shows_org_alias_option(runner: CliRunner) -> None:
    result = runner.invoke(app, ["capture", "--help"])
    assert result.exit_code == 0, f"exit={result.exit_code} stdout={result.stdout}"
    assert "--org-alias" in _strip_ansi(result.stdout).lower()


def test_capture_help_shows_out_dir_option(runner: CliRunner) -> None:
    result = runner.invoke(app, ["capture", "--help"])
    assert result.exit_code == 0
    assert "--out-dir" in _strip_ansi(result.stdout).lower()


def test_capture_help_shows_start_url_option(runner: CliRunner) -> None:
    result = runner.invoke(app, ["capture", "--help"])
    assert result.exit_code == 0
    assert "--start-url" in _strip_ansi(result.stdout).lower()


def test_capture_help_shows_note_option(runner: CliRunner) -> None:
    result = runner.invoke(app, ["capture", "--help"])
    assert result.exit_code == 0
    assert "--note" in _strip_ansi(result.stdout).lower()


def test_capture_help_mentions_playwright(runner: CliRunner) -> None:
    result = runner.invoke(app, ["capture", "--help"])
    assert result.exit_code == 0
    assert "playwright" in result.stdout.lower()


def test_top_level_help_lists_capture(runner: CliRunner) -> None:
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "capture" in result.stdout.lower()


# 2. Missing playwright prints install hint, not a stack trace

def test_missing_playwright_prints_install_hint(runner: CliRunner) -> None:
    with mock.patch.dict(sys.modules, {"playwright": None, "playwright.sync_api": None}):
        result = runner.invoke(app, ["capture", "--org-alias", "my-sandbox"])
    assert result.exit_code != 0
    assert "Traceback" not in result.output
    assert "playwright" in result.output.lower()
    assert "pip install" in result.output.lower() or "install" in result.output.lower()


def test_missing_playwright_no_raw_exception(runner: CliRunner) -> None:
    with mock.patch.dict(sys.modules, {"playwright": None, "playwright.sync_api": None}):
        result = runner.invoke(app, ["capture", "--org-alias", "my-sandbox"])
    assert "ModuleNotFoundError" not in result.output
    assert "File \"" not in result.output


# 3. cli.py stays importable without playwright

def test_cli_importable_without_playwright() -> None:
    import builtins, importlib
    real_import = builtins.__import__
    def mock_import(name, *args, **kwargs):
        if name.startswith("playwright"):
            raise ModuleNotFoundError(f"No module named {name!r}")
        return real_import(name, *args, **kwargs)
    saved = {k: v for k, v in sys.modules.items() if "sf_video_blueprint" in k}
    for k in saved:
        sys.modules.pop(k)
    try:
        with mock.patch("builtins.__import__", side_effect=mock_import):
            importlib.import_module("sf_video_blueprint.cli")
    finally:
        sys.modules.update(saved)


# 4. python -m capture.inject still works

def test_capture_inject_has_callable_main() -> None:
    import capture.inject as inject_mod
    assert callable(getattr(inject_mod, "main", None))


def test_capture_inject_main_guard_present() -> None:
    import inspect, capture.inject as inject_mod
    source = inspect.getsource(inject_mod)
    assert "__main__" in source
    assert "typer.run(main)" in source or "app()" in source


# 5. capture delegates to inject.main

def test_capture_delegates_to_inject_main(runner: CliRunner) -> None:
    called = {}
    def fake_main(org_alias, out_dir, start_url, note):
        called["org_alias"] = org_alias
        called["out_dir"] = out_dir
        called["start_url"] = start_url
        called["note"] = note
    with mock.patch("capture.inject.main", side_effect=fake_main):
        result = runner.invoke(app, [
            "capture", "--org-alias", "my-test-sandbox",
            "--out-dir", "/tmp/test-capture",
            "--start-url", "https://test.my.salesforce.com/lightning",
            "--note", "test run",
        ])
    assert result.exit_code == 0, f"exit={result.exit_code} output={result.output}"
    assert called.get("org_alias") == "my-test-sandbox"
    assert str(called.get("out_dir")) == "/tmp/test-capture"
    assert called.get("start_url") == "https://test.my.salesforce.com/lightning"
    assert called.get("note") == "test run"


def test_capture_optional_args_default_to_none(runner: CliRunner) -> None:
    called = {}
    def fake_main(org_alias, out_dir, start_url, note):
        called["start_url"] = start_url
        called["note"] = note
    with mock.patch("capture.inject.main", side_effect=fake_main):
        result = runner.invoke(app, ["capture", "--org-alias", "my-sandbox"])
    assert result.exit_code == 0, f"exit={result.exit_code} output={result.output}"
    assert called.get("start_url") is None
    assert called.get("note") is None


def test_capture_org_alias_required(runner: CliRunner) -> None:
    result = runner.invoke(app, ["capture"])
    assert result.exit_code != 0

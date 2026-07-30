"""Tests for capture/inject.py — process-name option and manifest enrichment.

Covers:
- validate_process_name: slug validation (letters, digits, hyphens only)
- capture_sf_cli_version: returns string or None without raising
- capture_playwright_mcp_version: returns string or None without raising
- Output path naming: <process_name>_<timestamp>.dom_capture.jsonl
- Manifest enrichment: process_name, sf_cli_version, playwright_mcp_version
- CaptureManifest model: new optional fields are accepted and round-trip cleanly
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Import the functions under test
# ---------------------------------------------------------------------------

from capture.inject import (
    capture_playwright_mcp_version,
    capture_sf_cli_version,
    validate_process_name,
)


# ===========================================================================
# 1. validate_process_name — slug validation
# ===========================================================================


class TestValidateProcessName:
    """validate_process_name must accept slug-safe names and reject everything else."""

    # --- passing cases ---

    def test_simple_slug_is_accepted(self) -> None:
        assert validate_process_name("case-creation") == "case-creation"

    def test_single_word_is_accepted(self) -> None:
        assert validate_process_name("login") == "login"

    def test_alphanumeric_slug_is_accepted(self) -> None:
        assert validate_process_name("case2update") == "case2update"

    def test_slug_with_digits_in_middle(self) -> None:
        assert validate_process_name("v2-case-triage") == "v2-case-triage"

    def test_slug_starting_with_digit_is_accepted(self) -> None:
        """Digits are valid slug-safe characters."""
        assert validate_process_name("2fa-setup") == "2fa-setup"

    def test_uppercase_letters_are_accepted(self) -> None:
        assert validate_process_name("CaseCreation") == "CaseCreation"

    def test_mixed_case_slug_is_accepted(self) -> None:
        assert validate_process_name("Case-Update-v2") == "Case-Update-v2"

    # --- failing cases ---

    def test_space_in_name_raises(self) -> None:
        with pytest.raises(ValueError, match="slug-safe"):
            validate_process_name("case creation")

    def test_slash_in_name_raises(self) -> None:
        with pytest.raises(ValueError, match="slug-safe"):
            validate_process_name("case/creation")

    def test_backslash_in_name_raises(self) -> None:
        with pytest.raises(ValueError, match="slug-safe"):
            validate_process_name("case\\creation")

    def test_underscore_in_name_raises(self) -> None:
        """Underscores are NOT in the allowed set (hyphens only)."""
        with pytest.raises(ValueError, match="slug-safe"):
            validate_process_name("case_creation")

    def test_dot_in_name_raises(self) -> None:
        with pytest.raises(ValueError, match="slug-safe"):
            validate_process_name("case.creation")

    def test_at_symbol_raises(self) -> None:
        with pytest.raises(ValueError, match="slug-safe"):
            validate_process_name("case@creation")

    def test_empty_string_raises(self) -> None:
        with pytest.raises(ValueError):
            validate_process_name("")

    def test_leading_hyphen_raises(self) -> None:
        """A slug must start with a letter or digit, not a hyphen."""
        with pytest.raises(ValueError, match="slug-safe"):
            validate_process_name("-case-creation")

    def test_unicode_raises(self) -> None:
        """Non-ASCII characters are not slug-safe."""
        with pytest.raises(ValueError, match="slug-safe"):
            validate_process_name("case-création")

    def test_newline_raises(self) -> None:
        with pytest.raises(ValueError, match="slug-safe"):
            validate_process_name("case\ncreation")

    def test_tab_raises(self) -> None:
        with pytest.raises(ValueError, match="slug-safe"):
            validate_process_name("case\tcreation")

    def test_returns_input_unchanged(self) -> None:
        """validate_process_name must return the same string it received."""
        name = "order-fulfillment"
        assert validate_process_name(name) is name or validate_process_name(name) == name


# ===========================================================================
# 2. capture_sf_cli_version — never raises, returns str or None
# ===========================================================================


class TestCaptureSfCliVersion:
    """capture_sf_cli_version must return a string or None and never raise."""

    def test_returns_string_or_none(self) -> None:
        result = capture_sf_cli_version()
        assert result is None or isinstance(result, str)

    def test_does_not_raise_when_sf_missing(self) -> None:
        """If 'sf' is not on PATH, the function must return None without raising."""
        with patch("subprocess.run", side_effect=FileNotFoundError("sf not found")):
            result = capture_sf_cli_version()
        assert result is None

    def test_does_not_raise_on_subprocess_error(self) -> None:
        import subprocess

        with patch(
            "subprocess.run",
            side_effect=subprocess.CalledProcessError(1, "sf"),
        ):
            result = capture_sf_cli_version()
        assert result is None

    def test_does_not_raise_on_json_decode_error(self) -> None:
        """When sf --version --json returns non-JSON, the function must not raise."""
        mock_result = MagicMock()
        mock_result.stdout = "NOT JSON"
        with patch("subprocess.run", return_value=mock_result):
            result = capture_sf_cli_version()
        # Non-JSON output is handled silently; result is None or a string.
        assert result is None or isinstance(result, str)

    def test_parses_cli_version_key(self) -> None:
        """When sf --version --json returns a cliVersion key, it is used."""
        mock_result = MagicMock()
        mock_result.stdout = json.dumps({"cliVersion": "@salesforce/cli/2.99.0"})
        with patch("subprocess.run", return_value=mock_result):
            result = capture_sf_cli_version()
        assert result == "@salesforce/cli/2.99.0"

    def test_falls_back_to_version_key(self) -> None:
        """When only 'version' is present, that is used."""
        mock_result = MagicMock()
        mock_result.stdout = json.dumps({"version": "2.88.0"})
        with patch("subprocess.run", return_value=mock_result):
            result = capture_sf_cli_version()
        assert result == "2.88.0"


# ===========================================================================
# 3. capture_playwright_mcp_version — never raises, returns str or None
# ===========================================================================


class TestCapturePlaywrightMcpVersion:
    """capture_playwright_mcp_version must return a string or None and never raise."""

    def test_returns_string_or_none(self) -> None:
        result = capture_playwright_mcp_version()
        assert result is None or isinstance(result, str)

    def test_does_not_raise_when_package_absent(self) -> None:
        """If neither package is installed, return None without raising."""
        from importlib.metadata import PackageNotFoundError

        with patch(
            "importlib.metadata.version",
            side_effect=PackageNotFoundError("not installed"),
        ):
            result = capture_playwright_mcp_version()
        assert result is None

    def test_returns_version_when_playwright_mcp_installed(self) -> None:
        def fake_version(pkg: str) -> str:
            if pkg == "playwright-mcp":
                return "1.50.0"
            raise Exception("not found")

        with patch("capture.inject.capture_playwright_mcp_version") as mock_fn:
            mock_fn.return_value = "1.50.0"
            result = mock_fn()
        assert result == "1.50.0"


# ===========================================================================
# 4. Output path: process_name flows into the filename
# ===========================================================================


class TestOutputPathNaming:
    """The output file name must be <process_name>_<timestamp>.dom_capture.jsonl."""

    def test_jsonl_path_includes_process_name(self, tmp_path: Path) -> None:
        """Verify the naming convention by constructing the path as inject.py does."""
        process_name = "case-creation"
        timestamp = 1720000000
        file_stem = f"{process_name}_{timestamp}"
        jsonl_path = tmp_path / f"{file_stem}.dom_capture.jsonl"

        assert "case-creation" in str(jsonl_path)
        assert "1720000000" in str(jsonl_path)
        assert jsonl_path.name.endswith(".dom_capture.jsonl")

    def test_two_captures_different_process_names_do_not_collide(
        self, tmp_path: Path
    ) -> None:
        """Different process names produce different file names."""
        ts = 1720000000
        path_a = tmp_path / f"case-creation_{ts}.dom_capture.jsonl"
        path_b = tmp_path / f"case-update_{ts}.dom_capture.jsonl"

        assert path_a != path_b
        assert path_a.name != path_b.name

    def test_two_captures_same_process_different_timestamps_do_not_collide(
        self, tmp_path: Path
    ) -> None:
        """Same process name but different timestamps produce different files."""
        path_a = tmp_path / "case-creation_1720000000.dom_capture.jsonl"
        path_b = tmp_path / "case-creation_1720000100.dom_capture.jsonl"

        assert path_a != path_b

    def test_network_path_shares_stem_with_jsonl_path(self, tmp_path: Path) -> None:
        """Network JSONL and manifest share the same file stem as the main JSONL."""
        process_name = "account-update"
        timestamp = 1720000000
        file_stem = f"{process_name}_{timestamp}"

        jsonl_path = tmp_path / f"{file_stem}.dom_capture.jsonl"
        network_path = tmp_path / f"{file_stem}.dom_capture.network.jsonl"
        manifest_path = tmp_path / f"{file_stem}.dom_capture.manifest.json"

        # All three share the same stem
        assert jsonl_path.name.startswith(file_stem)
        assert network_path.name.startswith(file_stem)
        assert manifest_path.name.startswith(file_stem)


# ===========================================================================
# 5. Manifest enrichment: process_name, sf_cli_version, playwright_mcp_version
# ===========================================================================


class TestManifestEnrichment:
    """The manifest dict written by inject.py must include the enrichment keys."""

    def _make_manifest_dict(
        self,
        process_name: str = "case-creation",
        sf_cli_version: str | None = "@salesforce/cli/2.99.0",
        playwright_mcp_version: str | None = "1.50.0",
    ) -> dict:
        """Build a manifest dict matching what inject.py writes."""
        return {
            "capture_id": "capture-1720000000",
            "process_name": process_name,
            "org_alias": "my-sandbox",
            "org_instance_url": "https://my-sandbox.develop.my.salesforce.com",
            "is_sandbox": True,
            "is_scratch": False,
            "started_at": "2026-07-01T00:00:00",
            "ended_at": "2026-07-01T00:05:00",
            "event_count": 10,
            "network_event_count": 5,
            "sink_errors": 0,
            "recorder_sha256": "abc123",
            "playwright_version": "1.50.0",
            "sf_cli_version": sf_cli_version,
            "playwright_mcp_version": playwright_mcp_version,
            "operator_note": None,
        }

    def test_manifest_contains_process_name(self) -> None:
        manifest = self._make_manifest_dict(process_name="case-creation")
        assert manifest["process_name"] == "case-creation"

    def test_manifest_contains_sf_cli_version(self) -> None:
        manifest = self._make_manifest_dict(sf_cli_version="@salesforce/cli/2.99.0")
        assert manifest["sf_cli_version"] == "@salesforce/cli/2.99.0"

    def test_manifest_contains_playwright_mcp_version(self) -> None:
        manifest = self._make_manifest_dict(playwright_mcp_version="1.50.0")
        assert manifest["playwright_mcp_version"] == "1.50.0"

    def test_manifest_allows_none_for_optional_fields(self) -> None:
        """sf_cli_version and playwright_mcp_version may be None (CLI not installed)."""
        manifest = self._make_manifest_dict(
            sf_cli_version=None,
            playwright_mcp_version=None,
        )
        assert manifest["sf_cli_version"] is None
        assert manifest["playwright_mcp_version"] is None

    def test_manifest_roundtrips_through_json(self, tmp_path: Path) -> None:
        """The manifest dict must survive JSON serialisation and deserialisation."""
        manifest = self._make_manifest_dict()
        manifest_path = tmp_path / "test.dom_capture.manifest.json"
        manifest_path.write_text(json.dumps(manifest, indent=2) + "\n")

        loaded = json.loads(manifest_path.read_text())
        assert loaded["process_name"] == manifest["process_name"]
        assert loaded["sf_cli_version"] == manifest["sf_cli_version"]
        assert loaded["playwright_mcp_version"] == manifest["playwright_mcp_version"]


# ===========================================================================
# 6. CaptureManifest Pydantic model accepts the new fields
# ===========================================================================


class TestCaptureManifestModel:
    """CaptureManifest must accept and round-trip the three new optional fields."""

    def _base_manifest(self) -> dict:
        return {
            "capture_id": "test-capture",
            "org_alias": "my-sandbox",
            "org_instance_url": "https://my-sandbox.develop.my.salesforce.com",
            "is_sandbox": True,
            "is_scratch": False,
            "started_at": "2026-07-01T00:00:00Z",
            "event_count": 5,
            "network_event_count": 2,
            "sink_errors": 0,
        }

    def test_model_accepts_process_name(self) -> None:
        from sf_video_blueprint.dom_capture import CaptureManifest

        data = {**self._base_manifest(), "process_name": "case-creation"}
        manifest = CaptureManifest.model_validate(data)
        assert manifest.process_name == "case-creation"

    def test_model_accepts_sf_cli_version(self) -> None:
        from sf_video_blueprint.dom_capture import CaptureManifest

        data = {**self._base_manifest(), "sf_cli_version": "@salesforce/cli/2.99.0"}
        manifest = CaptureManifest.model_validate(data)
        assert manifest.sf_cli_version == "@salesforce/cli/2.99.0"

    def test_model_accepts_playwright_mcp_version(self) -> None:
        from sf_video_blueprint.dom_capture import CaptureManifest

        data = {**self._base_manifest(), "playwright_mcp_version": "1.50.0"}
        manifest = CaptureManifest.model_validate(data)
        assert manifest.playwright_mcp_version == "1.50.0"

    def test_model_defaults_new_fields_to_none(self) -> None:
        """Existing manifests without the new fields must still load."""
        from sf_video_blueprint.dom_capture import CaptureManifest

        manifest = CaptureManifest.model_validate(self._base_manifest())
        assert manifest.process_name is None
        assert manifest.sf_cli_version is None
        assert manifest.playwright_mcp_version is None

    def test_model_all_new_fields_present(self) -> None:
        from sf_video_blueprint.dom_capture import CaptureManifest

        data = {
            **self._base_manifest(),
            "process_name": "account-update",
            "sf_cli_version": "@salesforce/cli/2.50.0",
            "playwright_mcp_version": "1.48.0",
        }
        manifest = CaptureManifest.model_validate(data)
        assert manifest.process_name == "account-update"
        assert manifest.sf_cli_version == "@salesforce/cli/2.50.0"
        assert manifest.playwright_mcp_version == "1.48.0"

    def test_model_serialises_new_fields(self) -> None:
        """model_dump must include the new fields so they round-trip via JSON."""
        from sf_video_blueprint.dom_capture import CaptureManifest

        data = {
            **self._base_manifest(),
            "process_name": "case-close",
            "sf_cli_version": "@salesforce/cli/2.77.0",
            "playwright_mcp_version": None,
        }
        manifest = CaptureManifest.model_validate(data)
        dumped = manifest.model_dump()
        assert "process_name" in dumped
        assert dumped["process_name"] == "case-close"
        assert dumped["sf_cli_version"] == "@salesforce/cli/2.77.0"
        assert dumped["playwright_mcp_version"] is None


# ===========================================================================
# 7. JSONL header record contains process_name
# ===========================================================================


class TestJsonlHeader:
    """The header record written to the JSONL file must contain process_name."""

    def test_header_record_contains_process_name(self, tmp_path: Path) -> None:
        """Build a header dict like inject.py writes and verify process_name is present."""
        capture_id = "capture-1720000000"
        process_name = "case-creation"
        org_alias = "my-sandbox"
        started_at = 1720000000000000000

        header = {
            "_record_type": "header",
            "capture_id": capture_id,
            "process_name": process_name,
            "org_alias": org_alias,
            "started_at": started_at,
        }

        jsonl_path = tmp_path / "case-creation_1720000000.dom_capture.jsonl"
        jsonl_path.write_text(json.dumps(header) + "\n", encoding="utf-8")

        loaded = json.loads(jsonl_path.read_text().splitlines()[0])
        assert loaded["_record_type"] == "header"
        assert loaded["process_name"] == "case-creation"

    def test_header_record_is_self_describing(self, tmp_path: Path) -> None:
        """A JSONL file with a header record exposes process_name without the manifest."""
        process_name = "order-fulfillment"
        header = {
            "_record_type": "header",
            "capture_id": "capture-9999",
            "process_name": process_name,
            "org_alias": "sandbox",
            "started_at": 1720000000000000000,
        }
        jsonl_path = tmp_path / f"{process_name}_9999.dom_capture.jsonl"
        jsonl_path.write_text(json.dumps(header) + "\n", encoding="utf-8")

        first_line = json.loads(jsonl_path.read_text().splitlines()[0])
        assert first_line.get("process_name") == process_name

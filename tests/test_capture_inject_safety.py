"""Tests for capture/inject.py assert_org_is_safe production safety guard.

Gap 1 fix: deny-list check delegates to org_denylist.is_org_blocked.
Gap 2 fix: URL fallback when isSandbox is absent from CLI 2.143.6+ output.
"""

from __future__ import annotations
import sys
from pathlib import Path
from unittest.mock import patch
import pytest

_repo_root = Path(__file__).resolve().parent
_capture_dir = _repo_root.parent / "capture"
if str(_capture_dir) not in sys.path:
    sys.path.insert(0, str(_capture_dir))
_src_dir = _repo_root.parent / "src"
if str(_src_dir) not in sys.path:
    sys.path.insert(0, str(_src_dir))
from inject import BLOCKED_ORG_ALIASES, _SAFE_URL_MARKERS, assert_org_is_safe

def _org(alias=None, username="admin@safe.develop.my.salesforce.com",
         instance_url="https://safe.develop.my.salesforce.com",
         is_sandbox=None, is_scratch=None) -> dict:
    info: dict = {"username": username, "instanceUrl": instance_url}
    if alias is not None: info["alias"] = alias
    if is_sandbox is not None: info["isSandbox"] = is_sandbox
    if is_scratch is not None: info["isScratch"] = is_scratch
    return info

class TestDenyListIntegration:
    @pytest.mark.parametrize("alias", [
        "PPCDM", "PPCaccenture",
        "ppcdm", "ppcaccenture",
        "PpCdM", "PPCACCENTURE",
        "PPC-DM", "PPC_accenture",
        "ppaccenture",
    ])
    def test_blocked_alias_raises(self, alias):
        info = _org(alias=alias, username="admin@example.com",
                    instance_url="https://example.my.salesforce.com")
        with pytest.raises(ValueError, match="permanently out of scope"):
            assert_org_is_safe(info)

    def test_blocked_username_raises_even_if_alias_is_clean(self):
        info = _org(alias="innocent", username="admin@ppcdm.com",
                    instance_url="https://safe.develop.my.salesforce.com")
        with pytest.raises(ValueError, match="permanently out of scope"):
            assert_org_is_safe(info)

    def test_blocked_instance_url_ppcdm_raises(self):
        info = _org(alias="some-alias", username="admin@example.com",
                    instance_url="https://ppcdm.sandbox.my.salesforce.com")
        with pytest.raises(ValueError, match="permanently out of scope"):
            assert_org_is_safe(info)

    def test_blocked_instance_url_ppcaccenture_raises(self):
        info = _org(alias="some-alias", username="admin@example.com",
                    instance_url="https://ppcaccenture.sandbox.my.salesforce.com")
        with pytest.raises(ValueError, match="permanently out of scope"):
            assert_org_is_safe(info)

    def test_no_subprocess_called_for_blocked_org(self):
        info = _org(alias="ppcdm", username="admin@ppcdm.com",
                    instance_url="https://ppcdm.my.salesforce.com")
        with patch("inject.subprocess.run") as mock_run:
            with pytest.raises(ValueError):
                assert_org_is_safe(info)
        assert mock_run.call_count == 0

class TestSandboxDetectionWithAbsentFlag:
    def test_dev_org_url_passes_when_is_sandbox_absent(self):
        info = {"username": "admin@testdev.develop.my.salesforce.com",
                "instanceUrl": "https://testdev.develop.my.salesforce.com"}
        assert_org_is_safe(info)

    def test_dev_org_with_is_sandbox_false_and_dev_url_passes(self):
        info = _org(username="admin@aft3.develop.my.salesforce.com",
                    instance_url="https://aft3.develop.my.salesforce.com",
                    is_sandbox=False, is_scratch=False)
        assert_org_is_safe(info)

    def test_sandbox_url_passes_when_is_sandbox_absent(self):
        info = {"username": "admin@myorg.sandbox.my.salesforce.com",
                "instanceUrl": "https://myorg.sandbox.my.salesforce.com"}
        assert_org_is_safe(info)

    def test_scratch_url_passes_when_is_scratch_absent(self):
        info = {"username": "admin@scratch.scratch.my.salesforce.com",
                "instanceUrl": "https://myorg.scratch.my.salesforce.com"}
        assert_org_is_safe(info)

    def test_production_url_without_flags_raises(self):
        info = {"username": "admin@example.my.salesforce.com",
                "instanceUrl": "https://example.my.salesforce.com"}
        with pytest.raises(ValueError, match="production safety"):
            assert_org_is_safe(info)

    def test_explicit_is_sandbox_true_passes(self):
        info = _org(username="admin@something.my.salesforce.com",
                    instance_url="https://something.my.salesforce.com",
                    is_sandbox=True)
        assert_org_is_safe(info)

    def test_explicit_is_scratch_true_passes(self):
        info = _org(username="admin@something.my.salesforce.com",
                    instance_url="https://something.my.salesforce.com",
                    is_scratch=True)
        assert_org_is_safe(info)

class TestSafeOrgsPass:
    def test_dev_org_with_is_sandbox_true_passes(self):
        assert_org_is_safe(_org(is_sandbox=True))

    def test_scratch_org_passes(self):
        assert_org_is_safe(_org(is_scratch=True))

    @pytest.mark.parametrize("alias", ["AFT3", "AFTDX5", "na-dev", "TD2"])
    def test_permitted_aliases_pass_when_url_is_safe(self, alias):
        assert_org_is_safe(_org(alias=alias))


class TestConstants:
    def test_blocked_org_aliases_constant_unchanged(self):
        assert BLOCKED_ORG_ALIASES == {"PPCDM", "PPCaccenture"}

    def test_safe_url_markers_include_develop_pattern(self):
        assert "develop.my.salesforce.com" in _SAFE_URL_MARKERS

    def test_safe_url_markers_include_sandbox_and_scratch(self):
        assert any("sandbox" in m for m in _SAFE_URL_MARKERS)
        assert any("scratch" in m for m in _SAFE_URL_MARKERS)


class TestCallSiteUsesCanonicalDenylist:
    def test_inject_module_imports_is_org_blocked(self):
        import inject
        assert hasattr(inject, "is_org_blocked")

    def test_inject_module_imports_from_org_denylist(self):
        source = (
            Path(__file__).resolve().parent.parent / "capture" / "inject.py"
        ).read_text(encoding="utf-8")
        assert "org_denylist import" in source

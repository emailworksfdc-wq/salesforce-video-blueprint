"""Tests for replay_browser.py production-org safety guards.

All tests are deterministic and do not touch real orgs or launch real browsers.
They mock the `sf` CLI boundary and browser driver.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, Mock, patch

import pytest

from sf_video_blueprint.models import ActionType, ExtractedAction, UIContext
from sf_video_blueprint.replay_browser import (
    BLOCKED_ORG_ALIASES,
    BlockedOrgError,
    BrowserReplayAdapter,
    ProductionOrgError,
    _guess_alias_from_url,
    _is_production_org,
    _redact_secret,
    _redact_url,
    resolve_org_info_from_url,
)


# ============================================================================
# Helper functions tests
# ============================================================================


def test_is_production_org_sandbox():
    """Sandbox orgs are not production."""
    assert not _is_production_org(
        is_sandbox=True,
        is_scratch=False,
        instance_url="https://my-sandbox.sandbox.my.salesforce.com",
        username="user@example.com.sandbox",
    )


def test_is_production_org_scratch():
    """Scratch orgs are not production."""
    assert not _is_production_org(
        is_sandbox=False,
        is_scratch=True,
        instance_url="https://my-scratch.scratch.my.salesforce.com",
        username="user@example.com.scratch",
    )


def test_is_production_org_dev_url():
    """Orgs with .develop.my.salesforce.com are not production."""
    assert not _is_production_org(
        is_sandbox=False,
        is_scratch=False,
        instance_url="https://my-dev.develop.my.salesforce.com",
        username="user@example.com",
    )


def test_is_production_org_test_salesforce_com():
    """Orgs on test.salesforce.com are not production."""
    assert not _is_production_org(
        is_sandbox=False,
        is_scratch=False,
        instance_url="https://test.salesforce.com",
        username="user@example.com",
    )


def test_is_production_org_username_suffix():
    """Usernames with .sandbox/.scratch/.dev suffixes are not production."""
    assert not _is_production_org(
        is_sandbox=False,
        is_scratch=False,
        instance_url="https://mycompany.my.salesforce.com",
        username="user@salesforce.com.sandbox",
    )


def test_is_production_org_true():
    """An org without sandbox/scratch flags and no dev markers is production."""
    assert _is_production_org(
        is_sandbox=False,
        is_scratch=False,
        instance_url="https://mycompany.my.salesforce.com",
        username="user@salesforce.com",
    )


def test_guess_alias_from_url_sandbox():
    """Guess alias from sandbox URL."""
    assert _guess_alias_from_url("https://my-sandbox.sandbox.my.salesforce.com") == "my-sandbox"


def test_guess_alias_from_url_develop():
    """Guess alias from develop URL."""
    assert _guess_alias_from_url("https://my-dev.develop.my.salesforce.com") == "my-dev"


def test_guess_alias_from_url_scratch():
    """Guess alias from scratch URL."""
    assert _guess_alias_from_url("https://my-scratch.scratch.my.salesforce.com") == "my-scratch"


def test_guess_alias_from_url_my_domain():
    """Cannot guess alias from My Domain URL."""
    assert _guess_alias_from_url("https://mycompany.my.salesforce.com") is None


def test_redact_url_my_domain():
    """Redact My Domain URLs."""
    assert _redact_url("https://mycompany.my.salesforce.com") == "https://<redacted>.my.salesforce.com"


def test_redact_url_generic():
    """Redact generic salesforce.com URLs."""
    assert _redact_url("https://na1.salesforce.com") == "<redacted>.salesforce.com"


def test_redact_secret():
    """Redact secrets for logging."""
    assert _redact_secret("00Dxx0000000001!AR8...verylongtoken") == "00Dxx000***"
    assert _redact_secret("short") == "***"


# ============================================================================
# resolve_org_info_from_url tests
# ============================================================================


@patch("sf_video_blueprint.replay_browser.subprocess.run")
def test_resolve_org_info_from_url_sandbox_matched(mock_run):
    """Sandbox org matched by URL in `sf org list`."""
    mock_run.return_value = Mock(
        stdout=json.dumps({
            "status": 0,
            "result": {
                "nonScratchOrgs": [
                    {
                        "alias": "my-sandbox",
                        "username": "user@example.com.sandbox",
                        "instanceUrl": "https://my-sandbox.sandbox.my.salesforce.com",
                        "isSandbox": True,
                        "isScratch": False,
                        "id": "00Dxx0000000001",
                    }
                ],
                "scratchOrgs": [],
            },
        }),
        returncode=0,
    )

    org_info = resolve_org_info_from_url(
        "https://my-sandbox.sandbox.my.salesforce.com",
        allow_production=False,
    )

    assert org_info["alias"] == "my-sandbox"
    assert org_info["isSandbox"] is True
    mock_run.assert_called_once_with(
        ["sf", "org", "list", "--json"],
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    )


@patch("sf_video_blueprint.replay_browser.subprocess.run")
def test_resolve_org_info_from_url_production_refused(mock_run):
    """Production org refused by default."""
    mock_run.return_value = Mock(
        stdout=json.dumps({
            "status": 0,
            "result": {
                "nonScratchOrgs": [
                    {
                        "alias": "my-prod",
                        "username": "user@salesforce.com",
                        "instanceUrl": "https://mycompany.my.salesforce.com",
                        "isSandbox": False,
                        "isScratch": False,
                        "id": "00Dxx0000000002",
                    }
                ],
                "scratchOrgs": [],
            },
        }),
        returncode=0,
    )

    with pytest.raises(ProductionOrgError, match="Production orgs are not allowed by default"):
        resolve_org_info_from_url(
            "https://mycompany.my.salesforce.com",
            allow_production=False,
        )


@patch("sf_video_blueprint.replay_browser.subprocess.run")
def test_resolve_org_info_from_url_production_allowed_with_override(mock_run):
    """Production org allowed with explicit override."""
    mock_run.return_value = Mock(
        stdout=json.dumps({
            "status": 0,
            "result": {
                "nonScratchOrgs": [
                    {
                        "alias": "my-prod",
                        "username": "user@salesforce.com",
                        "instanceUrl": "https://mycompany.my.salesforce.com",
                        "isSandbox": False,
                        "isScratch": False,
                        "id": "00Dxx0000000002",
                    }
                ],
                "scratchOrgs": [],
            },
        }),
        returncode=0,
    )

    org_info = resolve_org_info_from_url(
        "https://mycompany.my.salesforce.com",
        allow_production=True,
    )

    assert org_info["alias"] == "my-prod"
    assert org_info["isSandbox"] is False


@patch("sf_video_blueprint.replay_browser.subprocess.run")
def test_resolve_org_info_from_url_blocked_org(mock_run):
    """PPCDM and PPCaccenture are hard-blocked."""
    mock_run.return_value = Mock(
        stdout=json.dumps({
            "status": 0,
            "result": {
                "nonScratchOrgs": [
                    {
                        "alias": "PPCDM",
                        "username": "user@example.com.sandbox",
                        "instanceUrl": "https://ppcdm.sandbox.my.salesforce.com",
                        "isSandbox": True,
                        "isScratch": False,
                        "id": "00Dxx0000000003",
                    }
                ],
                "scratchOrgs": [],
            },
        }),
        returncode=0,
    )

    with pytest.raises(BlockedOrgError, match="permanently out of scope"):
        resolve_org_info_from_url(
            "https://ppcdm.sandbox.my.salesforce.com",
            allow_production=False,
        )


@patch("sf_video_blueprint.replay_browser.subprocess.run")
def test_resolve_org_info_from_url_no_match_fail_closed(mock_run):
    """Fail closed when org cannot be resolved."""
    mock_run.return_value = Mock(
        stdout=json.dumps({
            "status": 0,
            "result": {
                "nonScratchOrgs": [],
                "scratchOrgs": [],
            },
        }),
        returncode=0,
    )

    with pytest.raises(ValueError, match="Cannot resolve org metadata"):
        resolve_org_info_from_url(
            "https://unknown.my.salesforce.com",
            allow_production=False,
        )


@patch("sf_video_blueprint.replay_browser.subprocess.run")
def test_resolve_org_info_from_url_cli_timeout(mock_run):
    """CLI timeout raises ValueError."""
    mock_run.side_effect = subprocess.TimeoutExpired(cmd=["sf", "org", "list"], timeout=10)

    with pytest.raises(ValueError, match="timed out after 10s"):
        resolve_org_info_from_url("https://my-sandbox.sandbox.my.salesforce.com")


# ============================================================================
# BrowserReplayAdapter tests
# ============================================================================


def test_browser_replay_adapter_init():
    """Adapter initializes with defaults."""
    adapter = BrowserReplayAdapter()
    assert adapter.org_url is None
    assert adapter.live_enabled is False  # SF_BLUEPRINT_PLAYWRIGHT not set
    assert adapter.headless is True
    assert not adapter._org_verified


@patch("sf_video_blueprint.replay_browser.subprocess.run")
@patch.dict("os.environ", {"SF_BLUEPRINT_PLAYWRIGHT": "0"})
def test_open_org_sandbox_dry_run(mock_run):
    """open_org verifies sandbox in dry-run mode."""
    mock_run.return_value = Mock(
        stdout=json.dumps({
            "status": 0,
            "result": {
                "nonScratchOrgs": [
                    {
                        "alias": "my-sandbox",
                        "username": "user@example.com.sandbox",
                        "instanceUrl": "https://my-sandbox.sandbox.my.salesforce.com",
                        "isSandbox": True,
                        "isScratch": False,
                    }
                ],
                "scratchOrgs": [],
            },
        }),
        returncode=0,
    )

    adapter = BrowserReplayAdapter()
    adapter.open_org("https://my-sandbox.sandbox.my.salesforce.com")

    assert adapter.org_url == "https://my-sandbox.sandbox.my.salesforce.com"
    assert adapter._org_verified


@patch("sf_video_blueprint.replay_browser.subprocess.run")
@patch.dict("os.environ", {"SF_BLUEPRINT_PLAYWRIGHT": "0"})
def test_open_org_production_refused(mock_run):
    """open_org refuses production org."""
    mock_run.return_value = Mock(
        stdout=json.dumps({
            "status": 0,
            "result": {
                "nonScratchOrgs": [
                    {
                        "alias": "my-prod",
                        "username": "user@salesforce.com",
                        "instanceUrl": "https://mycompany.my.salesforce.com",
                        "isSandbox": False,
                        "isScratch": False,
                    }
                ],
                "scratchOrgs": [],
            },
        }),
        returncode=0,
    )

    adapter = BrowserReplayAdapter()

    with pytest.raises(ProductionOrgError, match="Production orgs are not allowed"):
        adapter.open_org("https://mycompany.my.salesforce.com")


@patch("sf_video_blueprint.replay_browser.subprocess.run")
@patch.dict("os.environ", {"SF_BLUEPRINT_PLAYWRIGHT": "0", "SF_ALLOW_PRODUCTION_ORG": "1"})
def test_open_org_production_allowed_with_env(mock_run):
    """open_org allows production org with SF_ALLOW_PRODUCTION_ORG=1."""
    mock_run.return_value = Mock(
        stdout=json.dumps({
            "status": 0,
            "result": {
                "nonScratchOrgs": [
                    {
                        "alias": "my-prod",
                        "username": "user@salesforce.com",
                        "instanceUrl": "https://mycompany.my.salesforce.com",
                        "isSandbox": False,
                        "isScratch": False,
                    }
                ],
                "scratchOrgs": [],
            },
        }),
        returncode=0,
    )

    adapter = BrowserReplayAdapter()
    adapter.open_org("https://mycompany.my.salesforce.com")

    assert adapter.org_url == "https://mycompany.my.salesforce.com"
    assert adapter._org_verified


@patch("sf_video_blueprint.replay_browser.subprocess.run")
def test_open_org_with_frontdoor_sandbox(mock_run):
    """open_org_with_frontdoor authenticates sandbox via frontdoor."""
    # Mock `sf org display` response.
    display_response = Mock(
        stdout=json.dumps({
            "status": 0,
            "result": {
                "alias": "my-sandbox",
                "username": "user@example.com.sandbox",
                "instanceUrl": "https://my-sandbox.sandbox.my.salesforce.com",
                "isSandbox": True,
                "isScratch": False,
                "id": "00Dxx0000000001",
            },
        }),
        returncode=0,
    )

    # Mock `sf org open --url-only` response.
    open_response = Mock(
        stdout=json.dumps({
            "status": 0,
            "result": {
                "url": "https://my-sandbox.sandbox.my.salesforce.com/secur/frontdoor.jsp?sid=REDACTED",
            },
        }),
        returncode=0,
    )

    mock_run.side_effect = [display_response, open_response]

    adapter = BrowserReplayAdapter()
    adapter.open_org_with_frontdoor("my-sandbox")

    assert adapter.org_url == "https://my-sandbox.sandbox.my.salesforce.com"
    assert adapter._org_verified
    assert mock_run.call_count == 2


@patch("sf_video_blueprint.replay_browser.subprocess.run")
def test_open_org_with_frontdoor_blocked_org(mock_run):
    """open_org_with_frontdoor refuses PPCDM/PPCaccenture."""
    mock_run.return_value = Mock(
        stdout=json.dumps({
            "status": 0,
            "result": {
                "alias": "PPCDM",
                "username": "user@example.com.sandbox",
                "instanceUrl": "https://ppcdm.sandbox.my.salesforce.com",
                "isSandbox": True,
                "isScratch": False,
                "id": "00Dxx0000000003",
            },
        }),
        returncode=0,
    )

    adapter = BrowserReplayAdapter()

    with pytest.raises(BlockedOrgError, match="permanently out of scope"):
        adapter.open_org_with_frontdoor("PPCDM")


@patch("sf_video_blueprint.replay_browser.subprocess.run")
def test_open_org_with_frontdoor_production_refused(mock_run):
    """open_org_with_frontdoor refuses production org without override."""
    mock_run.return_value = Mock(
        stdout=json.dumps({
            "status": 0,
            "result": {
                "alias": "my-prod",
                "username": "user@salesforce.com",
                "instanceUrl": "https://mycompany.my.salesforce.com",
                "isSandbox": False,
                "isScratch": False,
                "id": "00Dxx0000000002",
            },
        }),
        returncode=0,
    )

    adapter = BrowserReplayAdapter()

    with pytest.raises(ProductionOrgError, match="Production orgs are not allowed"):
        adapter.open_org_with_frontdoor("my-prod")


@patch("sf_video_blueprint.replay_browser.subprocess.run")
@patch.dict("os.environ", {"SF_ALLOW_PRODUCTION_ORG": "1"})
def test_open_org_with_frontdoor_production_allowed_logged(mock_run):
    """open_org_with_frontdoor allows production org with override and logs warning."""
    display_response = Mock(
        stdout=json.dumps({
            "status": 0,
            "result": {
                "alias": "my-prod",
                "username": "user@salesforce.com",
                "instanceUrl": "https://mycompany.my.salesforce.com",
                "isSandbox": False,
                "isScratch": False,
                "id": "00Dxx0000000002",
            },
        }),
        returncode=0,
    )

    open_response = Mock(
        stdout=json.dumps({
            "status": 0,
            "result": {
                "url": "https://mycompany.my.salesforce.com/secur/frontdoor.jsp?sid=REDACTED",
            },
        }),
        returncode=0,
    )

    mock_run.side_effect = [display_response, open_response]

    adapter = BrowserReplayAdapter()
    adapter.open_org_with_frontdoor("my-prod")

    assert adapter.org_url == "https://mycompany.my.salesforce.com"
    assert adapter._org_verified


@patch("sf_video_blueprint.replay_browser.subprocess.run")
@patch.dict("os.environ", {"SF_ALLOW_PRODUCTION_ORG": "1"})
def test_open_org_with_frontdoor_blocked_org_no_override(mock_run):
    """open_org_with_frontdoor refuses PPCDM/PPCaccenture even with SF_ALLOW_PRODUCTION_ORG=1."""
    mock_run.return_value = Mock(
        stdout=json.dumps({
            "status": 0,
            "result": {
                "alias": "PPCaccenture",
                "username": "user@example.com.sandbox",
                "instanceUrl": "https://ppcaccenture.sandbox.my.salesforce.com",
                "isSandbox": True,
                "isScratch": False,
                "id": "00Dxx0000000004",
            },
        }),
        returncode=0,
    )

    adapter = BrowserReplayAdapter()

    with pytest.raises(BlockedOrgError, match="permanently out of scope"):
        adapter.open_org_with_frontdoor("PPCaccenture")


# ============================================================================
# Secret redaction in perform_action
# ============================================================================


@patch.dict("os.environ", {"SF_BLUEPRINT_PLAYWRIGHT": "1"})
@patch("sf_video_blueprint.replay_browser.BrowserReplayAdapter._perform_live_action")
def test_perform_action_redacts_exception_with_frontdoor_url(mock_perform):
    """perform_action redacts exception messages containing frontdoor URLs."""
    adapter = BrowserReplayAdapter()
    adapter.org_url = "https://my-sandbox.sandbox.my.salesforce.com"
    adapter._org_verified = True

    # Mock _perform_live_action to raise an exception with a frontdoor URL.
    mock_perform.side_effect = RuntimeError(
        "Navigation failed to https://my-sandbox.sandbox.my.salesforce.com/secur/frontdoor.jsp?sid=REDACTED"
    )

    action = ExtractedAction(
        step_id="step-1",
        sequence=1,
        timestamp_ms=1000,
        action_type=ActionType.CLICK,
        target="button:Save",
        value=None,
        confidence=1.0,
        ui_context=UIContext(url=None, object_name=None, modal_name=None, view_name=None, selector_hint=None),
    )

    success, message, code = adapter.perform_action(action)

    assert not success
    assert "exception message redacted" in message
    assert "frontdoor.jsp" not in message
    # The error classifier detects "navigation" in the message, so it returns TRANSIENT_NAVIGATION.
    assert code == "TRANSIENT_NAVIGATION"


# ============================================================================
# Network trace redaction
# ============================================================================


@patch.dict("os.environ", {"SF_BLUEPRINT_PLAYWRIGHT": "1"})
def test_flush_step_network_trace_redacts_query_strings(tmp_path):
    """_flush_step_network_trace redacts query strings from URLs."""
    adapter = BrowserReplayAdapter()
    adapter.artifacts_dir = tmp_path
    adapter._pending_network_events = [
        {
            "url": "https://my-sandbox.sandbox.my.salesforce.com/services/data/v58.0/sobjects?fields=Id,Name",
            "method": "GET",
            "status": 200,
        },
        {
            "url": "https://my-sandbox.sandbox.my.salesforce.com/aura?r=1&token=abc123",
            "method": "POST",
            "status": 200,
        },
    ]

    action = ExtractedAction(
        step_id="step-1",
        sequence=1,
        timestamp_ms=1000,
        action_type=ActionType.CLICK,
        target="button:Save",
        value=None,
        confidence=1.0,
        ui_context=UIContext(url=None, object_name=None, modal_name=None, view_name=None, selector_hint=None),
    )

    trace_path = adapter._flush_step_network_trace(action)

    with open(trace_path, encoding="utf-8") as f:
        trace = json.load(f)

    assert len(trace) == 2
    assert trace[0]["url"] == "https://my-sandbox.sandbox.my.salesforce.com/services/data/v58.0/sobjects?<redacted>"
    assert trace[1]["url"] == "https://my-sandbox.sandbox.my.salesforce.com/aura?<redacted>"


# ============================================================================
# Verify blocked org aliases constant
# ============================================================================


def test_blocked_org_aliases_constant():
    """BLOCKED_ORG_ALIASES contains PPCDM and PPCaccenture."""
    assert "PPCDM" in BLOCKED_ORG_ALIASES
    assert "PPCaccenture" in BLOCKED_ORG_ALIASES
    assert len(BLOCKED_ORG_ALIASES) == 2

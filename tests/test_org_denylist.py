"""Tests for the org deny-list — DEFECT L4-4.

This is a SAFETY test file, not a style one. `PPCDM` and `PPCaccenture` are
permanently out of scope for this project, even read-only. A deny-list that
misses its target because of a typo is a safety defect.

Measured before the fix:

    telemetry._FORBIDDEN_ORG_ALIASES = {"PPCDM", "PPCaccenture", "ppcdm", "ppaccenture"}
                                                                          ^^^^^^^^^^^^
    _is_org_forbidden("ppcaccenture")  -> False   # the REAL lowercase spelling
    _is_org_forbidden("PPCACCENTURE")  -> False
    _is_org_forbidden("PpCaccenture")  -> False
    _is_org_forbidden(" PPCaccenture ")-> False

    replay_browser: `alias in BLOCKED_ORG_ALIASES` is a bare set membership test
    _is_org_blocked("ppcdm")           -> False   # lowercase PPCDM not blocked at all
    _is_org_blocked("ppcaccenture")    -> False

The deny-set contained `ppaccenture` (one `c`) where it meant `ppcaccenture`.
So the lowercase form of a hard-blocked org — the form a shell user is most
likely to type — sailed straight through both guards.
"""

from __future__ import annotations

import pytest

from sf_video_blueprint.org_denylist import (
    BLOCKED_ORG_ALIASES,
    is_org_blocked,
    normalize_org_identifier,
)

# ============================================================================
# Normalization
# ============================================================================


class TestNormalizeOrgIdentifier:
    """Normalization folds case, whitespace and punctuation."""

    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("PPCDM", "ppcdm"),
            ("ppcdm", "ppcdm"),
            ("PpCdM", "ppcdm"),
            ("  PPCDM  ", "ppcdm"),
            ("PPC-DM", "ppcdm"),
            ("PPC_DM", "ppcdm"),
            ("PPC.DM", "ppcdm"),
            ("PPC DM", "ppcdm"),
            ("PPCaccenture", "ppcaccenture"),
            ("ppc-accenture", "ppcaccenture"),
            ("PPC_ACCENTURE", "ppcaccenture"),
            ("", ""),
        ],
    )
    def test_normalizes(self, raw: str, expected: str) -> None:
        assert normalize_org_identifier(raw) == expected

    def test_none_is_empty(self) -> None:
        """A missing alias must normalize to empty, not crash."""
        assert normalize_org_identifier(None) == ""

    def test_digits_are_preserved(self) -> None:
        """Normalization must not strip digits — many aliases carry them."""
        assert normalize_org_identifier("AFT3") == "aft3"
        assert normalize_org_identifier("na-dev-2") == "nadev2"


# ============================================================================
# The deny-list itself — the point of the whole file
# ============================================================================


class TestBlockedOrgsAreActuallyBlocked:
    """Every spelling a human or a CLI could plausibly produce must be refused."""

    @pytest.mark.parametrize(
        "alias",
        [
            # PPCDM
            "PPCDM",
            "ppcdm",
            "PpCdM",
            "  PPCDM  ",
            "PPC-DM",
            "PPC_DM",
            "ppc.dm",
            # PPCaccenture — the typo'd entry meant this one and missed it.
            "PPCaccenture",
            "ppcaccenture",
            "PPCACCENTURE",
            "PpCaccenture",
            "  PPCaccenture  ",
            "PPC-accenture",
            "ppc_accenture",
            "PPC.Accenture",
        ],
    )
    def test_blocked(self, alias: str) -> None:
        assert is_org_blocked(alias) is True, f"{alias!r} MUST be blocked"

    def test_the_original_typo_spelling_is_still_refused(self) -> None:
        """`ppaccenture` (one `c`) was the typo in the deny-set.

        It stays blocked. A near-miss of a hard-blocked org name is not an org
        this project should ever touch either, and refusing it costs nothing.
        """
        assert is_org_blocked("ppaccenture") is True
        assert is_org_blocked("PPaccenture") is True

    def test_derived_aliases_are_blocked(self) -> None:
        """A sandbox or clone of a blocked org is still that org.

        `PPCDM.uat`, `ppcdm-clone`, `PPCaccenture_sandbox` all reach a forbidden
        org. Deny-lists are matched on containment of the normalized token for
        this reason; see the false-positive note in test_safe_orgs.
        """
        assert is_org_blocked("PPCDM.uat") is True
        assert is_org_blocked("ppcdm-clone") is True
        assert is_org_blocked("PPCaccenture_sandbox") is True
        assert is_org_blocked("ppcdm@example.com") is True

    def test_username_and_instance_url_forms_are_blocked(self) -> None:
        """The bypass surface named in the defect ledger: reaching a blocked org
        via username or instance URL without ever naming its alias."""
        assert is_org_blocked("admin@ppcdm.com") is True
        assert is_org_blocked("https://ppcdm.my.salesforce.com") is True
        assert is_org_blocked("https://ppcaccenture.sandbox.my.salesforce.com") is True

    def test_empty_and_none_are_not_blocked_but_are_not_crashes(self) -> None:
        """An absent alias is not a blocked alias. Callers fail closed on
        unknown orgs by a separate path (`_verify_org_is_sandbox`); this
        function answers exactly one question and must not conflate them."""
        assert is_org_blocked("") is False
        assert is_org_blocked(None) is False


class TestSafeOrgsAreNotBlocked:
    """The deny-list must not become a wildcard. False positives block real work."""

    @pytest.mark.parametrize(
        "alias",
        [
            "AFT3",
            "my-scratch-org",
            "dev-sandbox",
            "uat",
            "na-dev",
            "TD2",
            "TDProj",
            "illektra-sbx",
            "AFTDX5",
            "acme-accenture",  # contains "accenture" but not "ppcaccenture"
            "accenture",
            "ppc",  # the prefix alone is not a blocked org
            "dm",
            "cdm-project",
        ],
    )
    def test_safe_orgs(self, alias: str) -> None:
        assert is_org_blocked(alias) is False, f"{alias!r} must NOT be blocked"

    def test_containment_false_positive_is_accepted_deliberately(self) -> None:
        """Honest statement of the tradeoff.

        Containment matching means a hypothetical unrelated alias that happens
        to embed `ppcdm` (e.g. `ppcdmx`) is refused. That is accepted: a
        false positive costs one refused run and a clear error message, while a
        false negative touches an org that is permanently out of scope. The
        deny-list is deliberately biased toward refusing.
        """
        assert is_org_blocked("ppcdmx") is True


# ============================================================================
# Both call sites must actually use it
# ============================================================================


class TestCallSitesUseTheSharedDenylist:
    """A correct helper that no guard calls is worthless — this is exactly the
    "validate_trace never called in production" defect from round 4."""

    def test_telemetry_guard_blocks_real_lowercase_spelling(self) -> None:
        from sf_video_blueprint.telemetry import _is_org_forbidden

        assert _is_org_forbidden("ppcaccenture") is True
        assert _is_org_forbidden("PPCACCENTURE") is True
        assert _is_org_forbidden("ppcdm") is True
        assert _is_org_forbidden(" PPCaccenture ") is True

    def test_telemetry_verify_org_refuses_without_calling_the_cli(self) -> None:
        """A blocked org must be refused before any subprocess runs. If the
        guard fell through to `sf org display`, this test would touch the CLI.
        """
        from unittest.mock import patch

        from sf_video_blueprint.telemetry import _verify_org_is_sandbox

        with patch("sf_video_blueprint.telemetry.subprocess.run") as mock_run:
            is_safe, detail = _verify_org_is_sandbox("ppcaccenture")

        assert is_safe is False
        assert mock_run.call_count == 0, "blocked org must short-circuit before the CLI"
        assert "forbidden" in detail.lower()

    def test_replay_browser_exports_the_shared_matcher(self) -> None:
        from sf_video_blueprint.replay_browser import _is_org_blocked

        assert _is_org_blocked("ppcdm") is True
        assert _is_org_blocked("ppcaccenture") is True
        assert _is_org_blocked("AFT3") is False

    def test_canonical_constant_is_unchanged(self) -> None:
        """The human-readable constant keeps its two canonical names; the
        matching logic, not the constant, is what got fixed."""
        assert BLOCKED_ORG_ALIASES == {"PPCDM", "PPCaccenture"}

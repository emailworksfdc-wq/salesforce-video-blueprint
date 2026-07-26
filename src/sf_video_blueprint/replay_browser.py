from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import subprocess
from typing import Any

from .models import ActionType, ExtractedAction
from .org_denylist import BLOCKED_ORG_ALIASES as _CANONICAL_BLOCKED
from .org_denylist import blocked_org_message
from .org_denylist import is_org_blocked as _is_org_blocked
from .replay import SalesforceUIAdapter


# Hard-blocked org aliases per project rules (PPCDM and PPCaccenture are
# permanently out of scope, even read-only).
#
# DEFECT L4-4: this set used to be matched with a bare `alias in
# BLOCKED_ORG_ALIASES`, which is case- and punctuation-sensitive — so `ppcdm`,
# `PPCACCENTURE`, ` PPCDM ` and `PPC-accenture` all sailed through. Matching now
# goes through `_is_org_blocked`, which normalizes first. The set itself is kept
# as the canonical human-readable names for error messages and is re-exported
# for callers that assert on it.
BLOCKED_ORG_ALIASES = set(_CANONICAL_BLOCKED)


class ProductionOrgError(ValueError):
    """Raised when attempting to replay against a production org."""
    pass


class BlockedOrgError(ValueError):
    """Raised when attempting to replay against a permanently blocked org alias."""
    pass


def resolve_org_info_from_url(org_url: str, allow_production: bool = False) -> dict[str, Any]:
    """Resolve org metadata from an org URL by calling `sf org display --json`.

    This function attempts to find an org in the local CLI state that matches
    the given URL. It is NOT foolproof — if the org is not authenticated locally,
    or if the URL is a My Domain that doesn't exactly match what the CLI stores,
    this will fail.

    Detection strategy and honest limits:

    1. **Alias-based lookup**: If the URL contains an alias-like segment (e.g.,
       `my-sandbox.sandbox.my.salesforce.com` → try `my-sandbox` as an alias),
       we try to resolve it via `sf org display`.
    2. **`sf org display` fields**: The CLI returns `isSandbox`, `isScratch`, and
       `instanceUrl`. These are authoritative for orgs that the CLI knows about.
    3. **Fail-closed on ambiguity**: If we cannot resolve the org (not authenticated
       locally, URL doesn't match), we REFUSE to proceed. A guard that guesses
       "probably sandbox" is worse than none.
    4. **My Domain URLs cannot be reliably classified by pattern alone**. A URL
       like `https://mycompany.my.salesforce.com` could be production or could be
       a sandbox with a custom My Domain. We depend on the CLI's org metadata.
    5. **Production escape hatch**: If `allow_production=True` AND the org is
       positively identified as production, we allow it but log a warning. This
       must be impossible to trip accidentally.

    Args:
        org_url: The Salesforce org URL (instance URL or My Domain).
        allow_production: If True, allow production orgs (logged). Default False.

    Returns:
        Dict with keys: alias, username, isSandbox, isScratch, instanceUrl, id.

    Raises:
        BlockedOrgError: If the org alias is in BLOCKED_ORG_ALIASES.
        ProductionOrgError: If the org is production and allow_production=False.
        ValueError: If org metadata cannot be resolved or is ambiguous.
    """
    # Try to find an org in the local CLI state that matches this URL.
    # Strategy: `sf org list --json` returns all authenticated orgs with their
    # instance URLs. Match by URL.
    try:
        result = subprocess.run(
            ["sf", "org", "list", "--json"],
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        )
        data = json.loads(result.stdout)
        if data.get("status") != 0:
            raise ValueError(f"sf org list failed: {data.get('message', 'unknown error')}")

        orgs = data.get("result", {}).get("nonScratchOrgs", []) + data.get("result", {}).get("scratchOrgs", [])

        # Normalize the input URL for matching (strip trailing slash, lowercase).
        normalized_url = org_url.rstrip("/").lower()

        # Try to find an org whose instanceUrl matches.
        matched_org = None
        for org in orgs:
            instance_url = org.get("instanceUrl", "").rstrip("/").lower()
            if instance_url == normalized_url or normalized_url.startswith(instance_url):
                matched_org = org
                break

        if not matched_org:
            # Fallback: try to extract an alias from the URL and query directly.
            # E.g., `https://my-sandbox.sandbox.my.salesforce.com` → try `my-sandbox`.
            alias_guess = _guess_alias_from_url(org_url)
            if alias_guess:
                try:
                    result = subprocess.run(
                        ["sf", "org", "display", "--target-org", alias_guess, "--json"],
                        check=True,
                        capture_output=True,
                        text=True,
                        timeout=10,
                    )
                    display_data = json.loads(result.stdout)
                    if display_data.get("status") == 0:
                        matched_org = display_data["result"]
                except (subprocess.CalledProcessError, json.JSONDecodeError, KeyError):
                    pass

        if not matched_org:
            raise ValueError(
                f"Cannot resolve org metadata for URL '{org_url}'. The org is not "
                f"authenticated locally (no `sf org list` entry matches), or the URL "
                f"does not match the CLI's stored instanceUrl. Production safety "
                f"guard fails closed: refusing to proceed when org type is unknown."
            )

        # Now we have authoritative metadata. Check the safety rules.
        alias = matched_org.get("alias") or matched_org.get("username")

        # Rule 1: Hard-block PPCDM and PPCaccenture, no override.
        # Checked against the resolved alias, the username AND the instance URL:
        # a blocked org can be reached without ever naming its alias.
        for candidate in (
            alias,
            matched_org.get("username", ""),
            matched_org.get("instanceUrl", ""),
        ):
            if _is_org_blocked(candidate):
                raise BlockedOrgError(blocked_org_message(candidate))

        # Rule 2: Refuse production unless allow_production=True.
        is_sandbox = matched_org.get("isSandbox", False)
        is_scratch = matched_org.get("isScratch", False)
        instance_url = matched_org.get("instanceUrl", "")

        is_production = _is_production_org(
            is_sandbox=is_sandbox,
            is_scratch=is_scratch,
            instance_url=instance_url,
            username=matched_org.get("username", ""),
        )

        if is_production and not allow_production:
            raise ProductionOrgError(
                f"Org '{alias}' is a production org (isSandbox={is_sandbox}, "
                f"isScratch={is_scratch}, instanceUrl='{instance_url}'). "
                f"Production orgs are not allowed by default. To override, set "
                f"SF_ALLOW_PRODUCTION_ORG=1 (env var) or pass allow_production=True. "
                f"This override does NOT work for PPCDM/PPCaccenture."
            )

        if is_production and allow_production:
            # Log the override. Do NOT log the full URL or instance URL (secret leak).
            print(f"[replay_browser] WARNING: Production org '{_redact_url(instance_url)}' "
                  f"allowed via explicit override (allow_production=True).")

        return matched_org

    except subprocess.TimeoutExpired as exc:
        raise ValueError("sf org list timed out after 10s. Cannot verify org safety.") from exc
    except subprocess.CalledProcessError as exc:
        raise ValueError(f"sf org list failed with exit code {exc.returncode}.") from exc
    except json.JSONDecodeError as exc:
        raise ValueError(f"sf org list returned malformed JSON: {exc}") from exc


def _guess_alias_from_url(url: str) -> str | None:
    """Try to guess an org alias from a Salesforce URL.

    E.g., `https://my-sandbox.sandbox.my.salesforce.com` → `my-sandbox`.
    This is a fallback heuristic and is NOT reliable.
    """
    # Strip protocol and path.
    url = url.lower().replace("https://", "").replace("http://", "").split("/")[0]
    # Try to extract the subdomain before .sandbox. or .my.salesforce.com.
    if ".sandbox.my.salesforce.com" in url:
        return url.split(".sandbox.my.salesforce.com")[0]
    if ".develop.my.salesforce.com" in url:
        return url.split(".develop.my.salesforce.com")[0]
    if ".scratch.my.salesforce.com" in url:
        return url.split(".scratch.my.salesforce.com")[0]
    # If it's a My Domain URL (no sandbox marker), we can't guess reliably.
    return None


def _is_production_org(
    is_sandbox: bool,
    is_scratch: bool,
    instance_url: str,
    username: str,
) -> bool:
    """Determine if an org is production based on CLI metadata.

    Returns True if the org is NOT a sandbox, NOT a scratch org, and does NOT
    have dev/sandbox/scratch markers in the instance URL or username.
    """
    # Positive sandbox/scratch indicators → not production.
    if is_sandbox or is_scratch:
        return False

    # Username suffixes that indicate non-production.
    safe_username_suffixes = (".sandbox", ".scratch", ".dev")
    if any(suffix in username for suffix in safe_username_suffixes):
        return False

    # Instance URL markers that indicate non-production.
    safe_url_markers = (
        ".develop.my.salesforce.com",
        ".sandbox.my.salesforce.com",
        ".scratch.my.salesforce.com",
        "test.salesforce.com",
    )
    if any(marker in instance_url.lower() for marker in safe_url_markers):
        return False

    # If none of the above, assume production (fail closed).
    return True


def _redact_url(url: str) -> str:
    """Redact the subdomain from a Salesforce URL to avoid logging My Domain names.

    E.g., `https://mycompany.my.salesforce.com` → `https://<redacted>.my.salesforce.com`.
    """
    if ".my.salesforce.com" in url:
        # Extract protocol and reconstruct with redacted subdomain.
        parts = url.split("//", 1)
        protocol = parts[0] if len(parts) > 1 else "https:"
        return f"{protocol}//<redacted>.my.salesforce.com"
    if ".salesforce.com" in url:
        return "<redacted>.salesforce.com"
    return "<redacted>"


def _redact_secret(value: str) -> str:
    """Redact a secret (token, frontdoor URL, session ID) for logging.

    Shows the first 8 characters and masks the rest.
    """
    if len(value) <= 8:
        return "***"
    return value[:8] + "***"


class BrowserReplayAdapter(SalesforceUIAdapter):
    """
    Browser-backed replay adapter with production-org safety guards.

    This implementation intentionally avoids hard dependencies on browser
    libraries so it can run in environments where Playwright is not installed.
    Set SF_BLUEPRINT_PLAYWRIGHT=1 to enable live mode after wiring locators.

    Production safety:
    - Refuses production orgs by default (fail-closed when org type is ambiguous).
    - Hard-blocks PPCDM and PPCaccenture with no override.
    - Escape hatch: SF_ALLOW_PRODUCTION_ORG=1 (logged, but still blocks PPCDM/PPCaccenture).
    - Never automates the Salesforce login form (see `open_org_with_frontdoor` instead).
    - Redacts secrets (frontdoor URLs, tokens, session IDs) from logs and exceptions.
    """

    def __init__(self) -> None:
        self.org_url: str | None = None
        self.live_enabled = os.getenv("SF_BLUEPRINT_PLAYWRIGHT", "0") == "1"
        self.headless = os.getenv("SF_BLUEPRINT_HEADLESS", "1") == "1"
        self.artifacts_dir = Path(os.getenv("SF_BLUEPRINT_ARTIFACTS_DIR", "./outputs/replay_artifacts"))
        self._playwright: Any | None = None
        self._browser: Any | None = None
        self._context: Any | None = None
        self._page: Any | None = None
        self._pending_network_events: list[dict[str, Any]] = []
        self._org_verified: bool = False  # Track if org safety was verified.

    def open_org(self, org_url: str) -> None:
        """Open a Salesforce org by navigating to its instance URL.

        **DEPRECATED for new code**: This method does NOT enforce production-org
        safety guards and will attempt to automate the login form if credentials
        are in the environment. Use `open_org_with_frontdoor` instead.

        Args:
            org_url: The Salesforce org URL (instance URL or My Domain).
        """
        # Verify org safety if not already done.
        if not self._org_verified:
            allow_production = os.getenv("SF_ALLOW_PRODUCTION_ORG", "0") == "1"
            try:
                resolve_org_info_from_url(org_url, allow_production=allow_production)
                self._org_verified = True
            except (ProductionOrgError, BlockedOrgError) as exc:
                raise exc
            except ValueError as exc:
                # Org metadata could not be resolved. Fail closed.
                raise ValueError(
                    f"Cannot verify org safety for '{_redact_url(org_url)}': {exc}. "
                    f"Production safety guard fails closed."
                ) from exc

        self.org_url = org_url
        if self.live_enabled:
            self._ensure_session()
            self._page.goto(org_url, wait_until="domcontentloaded")
            self._wait_ready_state()
            # NOTE: _try_salesforce_login is removed. It violates policy.
            # Use open_org_with_frontdoor instead.

    def open_org_with_frontdoor(self, org_alias: str) -> None:
        """Open a Salesforce org via frontdoor.jsp (sanctioned auth path).

        This is the ONLY sanctioned way to authenticate for browser replay. It
        calls `sf org open --url-only` to get a signed frontdoor.jsp URL, which
        bypasses MFA/SSO legitimately, and navigates to it.

        Production safety guards are enforced:
        - PPCDM and PPCaccenture are hard-blocked by alias (no override).
        - Production orgs are refused unless SF_ALLOW_PRODUCTION_ORG=1 is set.
        - The frontdoor URL is redacted from logs and exception messages.

        Args:
            org_alias: Salesforce org alias or username (must be authenticated locally).

        Raises:
            BlockedOrgError: If the org is PPCDM or PPCaccenture.
            ProductionOrgError: If the org is production and SF_ALLOW_PRODUCTION_ORG!=1.
            ValueError: If the org is not authenticated or metadata cannot be resolved.
            subprocess.CalledProcessError: If `sf org open` fails.
        """
        # 0. Refuse a blocked org BEFORE spawning any subprocess. The old code
        # only checked the alias `sf org display` echoed back, so a blocked org
        # was contacted before being refused. Checking the caller's own argument
        # first means no process touches it at all.
        if _is_org_blocked(org_alias):
            raise BlockedOrgError(blocked_org_message(org_alias))

        # 1. Verify org safety by alias.
        allow_production = os.getenv("SF_ALLOW_PRODUCTION_ORG", "0") == "1"
        try:
            result = subprocess.run(
                ["sf", "org", "display", "--target-org", org_alias, "--json"],
                check=True,
                capture_output=True,
                text=True,
                timeout=10,
            )
            data = json.loads(result.stdout)
            if data.get("status") != 0:
                raise ValueError(f"sf org display failed: {data.get('message', 'unknown error')}")

            org_info = data["result"]
            alias = org_info.get("alias") or org_info.get("username")

            # Rule 1: Hard-block PPCDM and PPCaccenture. Re-checked against the
            # RESOLVED identity, not just the caller's argument: an innocuous
            # local alias can point at a blocked org.
            for candidate in (
                alias,
                org_info.get("username", ""),
                org_info.get("instanceUrl", ""),
            ):
                if _is_org_blocked(candidate):
                    raise BlockedOrgError(blocked_org_message(candidate))

            # Rule 2: Refuse production unless allow_production=True.
            is_production = _is_production_org(
                is_sandbox=org_info.get("isSandbox", False),
                is_scratch=org_info.get("isScratch", False),
                instance_url=org_info.get("instanceUrl", ""),
                username=org_info.get("username", ""),
            )

            if is_production and not allow_production:
                raise ProductionOrgError(
                    f"Org '{alias}' is a production org. Production orgs are not allowed "
                    f"by default. To override, set SF_ALLOW_PRODUCTION_ORG=1 (env var). "
                    f"This override does NOT work for PPCDM/PPCaccenture."
                )

            if is_production and allow_production:
                instance_url = org_info.get("instanceUrl", "")
                print(f"[replay_browser] WARNING: Production org '{_redact_url(instance_url)}' "
                      f"allowed via explicit override (SF_ALLOW_PRODUCTION_ORG=1).")

            self._org_verified = True

        except subprocess.TimeoutExpired as exc:
            raise ValueError("sf org display timed out after 10s. Cannot verify org safety.") from exc
        except subprocess.CalledProcessError as exc:
            raise ValueError(f"sf org display failed with exit code {exc.returncode}.") from exc
        except json.JSONDecodeError as exc:
            raise ValueError(f"sf org display returned malformed JSON: {exc}") from exc

        # 2. Get the frontdoor URL.
        try:
            result = subprocess.run(
                ["sf", "org", "open", "--url-only", "--target-org", org_alias, "--json"],
                check=True,
                capture_output=True,
                text=True,
                timeout=10,
            )
            data = json.loads(result.stdout)
            if data.get("status") != 0:
                raise ValueError(f"sf org open failed: {data.get('message', 'unknown error')}")

            frontdoor_url = data.get("result", {}).get("url")
            if not frontdoor_url:
                raise ValueError("sf org open did not return a URL in result.url")

            # Redact the frontdoor URL from any error messages.
            self.org_url = org_info.get("instanceUrl")

            if self.live_enabled:
                self._ensure_session()
                # Navigate to the frontdoor URL. Do NOT log it.
                self._page.goto(frontdoor_url, wait_until="domcontentloaded")
                self._wait_ready_state()
                print(f"[replay_browser] Authenticated to org '{alias}' via frontdoor (redacted).")

        except subprocess.TimeoutExpired as exc:
            raise ValueError("sf org open timed out after 10s.") from exc
        except subprocess.CalledProcessError as exc:
            raise ValueError(f"sf org open failed with exit code {exc.returncode}.") from exc
        except json.JSONDecodeError as exc:
            raise ValueError(f"sf org open returned malformed JSON: {exc}") from exc

    def perform_action(self, action: ExtractedAction) -> tuple[bool, str, str | None]:
        if not self.org_url:
            return False, "Org not initialized.", "UI_NOT_INITIALIZED"

        if not self.live_enabled:
            return True, f"Dry-run replayed {action.action_type.value} on {action.target}", None

        # Live mode contract: map canonical action to browser operation.
        try:
            self._perform_live_action(action)
            screenshot_path = self._capture_step_screenshot(action)
            network_trace_path = self._flush_step_network_trace(action)
        except Exception as exc:  # noqa: BLE001
            code = self._classify_action_exception(exc)
            # Redact any URLs from the exception message to avoid leaking frontdoor
            # URLs or session tokens in query strings.
            exc_msg = str(exc)
            if "frontdoor.jsp" in exc_msg or "sid=" in exc_msg or "retURL=" in exc_msg:
                exc_msg = "<exception message redacted: may contain frontdoor URL or token>"
            return False, f"Live replay failed: {exc_msg}", code
        return (
            True,
            (
                "Live replay action completed. "
                f"screenshot={screenshot_path}; network_trace={network_trace_path}"
            ),
            None,
        )

    def _perform_live_action(self, action: ExtractedAction) -> None:
        self._ensure_session()
        op = action_to_browser_operation(action)
        action_type = op["type"]
        selectors = op["selectors"]

        if action_type == ActionType.NAVIGATE.value:
            url = action.value or action.ui_context.url
            if not url:
                raise ValueError("Navigate action missing URL in value or ui_context.url.")
            # Redact frontdoor URLs from any logging or exceptions.
            if "frontdoor.jsp" in url:
                # Do NOT log the full URL. Log only that a frontdoor nav occurred.
                print("[replay_browser] Navigating via frontdoor (URL redacted).")
            self._page.goto(url, wait_until="domcontentloaded")
            return

        if action_type == ActionType.WAIT.value:
            duration_ms = int(op.get("duration_ms", 1000))
            self._page.wait_for_timeout(duration_ms)
            return

        locator = self._resolve_locator(selectors)
        if locator is None:
            raise RuntimeError(f"No locator matched for target: {action.target}")
        self._wait_actionable(locator)

        if action_type in {ActionType.CLICK.value, ActionType.SUBMIT.value}:
            before_url = self._page.url
            locator.click(timeout=5000)
            self._wait_post_action(before_url)
            return

        if action_type == ActionType.INPUT.value:
            locator.fill(action.value or "", timeout=5000)
            return

        if action_type == ActionType.SELECT.value:
            locator.select_option(label=action.value or "", timeout=5000)
            return

        if action_type == ActionType.SCROLL.value:
            locator.scroll_into_view_if_needed(timeout=5000)
            return

        if action_type == ActionType.HOTKEY.value:
            if not action.value:
                raise ValueError("Hotkey action missing key combo value.")
            self._page.keyboard.press(action.value)
            return

        if action_type == ActionType.ASSERT.value:
            text = action.value or ""
            self._page.get_by_text(text, exact=False).first.wait_for(timeout=5000)
            return

        raise NotImplementedError(f"Unsupported action type: {action_type}")

    def _ensure_session(self) -> None:
        if self._page is not None:
            return
        try:
            from playwright.sync_api import sync_playwright
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError("Playwright is not installed. Run: pip install playwright && playwright install chromium") from exc

        self._playwright = sync_playwright().start()
        self._browser = self._playwright.chromium.launch(headless=self.headless)
        self._context = self._browser.new_context()
        self._page = self._context.new_page()
        self.artifacts_dir.mkdir(parents=True, exist_ok=True)
        self._register_network_listeners()

    def _resolve_locator(self, selectors: list[str]) -> Any | None:
        scopes = [self._page]
        scope_selectors = [
            "iframe[title*='Salesforce']",
            "iframe[name*='salesforce']",
            "iframe",
            "[role='dialog']",
            ".slds-modal__container",
        ]
        for scope_selector in scope_selectors:
            try:
                frame_or_scope = self._page.frame_locator(scope_selector)
                scopes.append(frame_or_scope)
            except Exception:  # noqa: BLE001
                continue

        for scope in scopes:
            for selector in selectors:
                try:
                    locator = scope.locator(selector).first
                    if locator.count() > 0:
                        return locator
                except Exception:  # noqa: BLE001
                    continue
        return None


    def _wait_actionable(self, locator: Any) -> None:
        locator.wait_for(state="visible", timeout=5000)
        locator.scroll_into_view_if_needed(timeout=5000)

    def _wait_post_action(self, before_url: str | None = None) -> None:
        try:
            if before_url and self._page.url != before_url:
                return
            self._page.wait_for_load_state("networkidle", timeout=3000)
        except Exception:  # noqa: BLE001
            self._wait_ready_state()

    def _wait_ready_state(self) -> None:
        self._page.wait_for_load_state("domcontentloaded", timeout=5000)
        try:
            self._page.locator(".slds-spinner").first.wait_for(state="hidden", timeout=3000)
        except Exception:  # noqa: BLE001
            return

    def _classify_action_exception(self, exc: Exception) -> str:
        msg = str(exc).lower()
        name = exc.__class__.__name__.lower()
        if "timeout" in msg or "timeout" in name:
            return "TIMEOUT"
        if "detached" in msg or "stale" in msg:
            return "STALE_REFERENCE"
        if "no locator matched" in msg or "strict mode violation" in msg:
            return "ELEMENT_NOT_FOUND"
        if "target closed" in msg or "page closed" in msg:
            return "TARGET_CLOSED"
        if "navigation" in msg:
            return "TRANSIENT_NAVIGATION"
        return "UI_RUNTIME_ERROR"

    def close(self) -> None:
        if self._context is not None:
            self._context.close()
            self._context = None
        if self._browser is not None:
            self._browser.close()
            self._browser = None
        if self._playwright is not None:
            self._playwright.stop()
            self._playwright = None
        self._page = None

    def __del__(self) -> None:
        self.close()

    def _register_network_listeners(self) -> None:
        def on_response(response: Any) -> None:
            try:
                request = response.request
                record = {
                    "captured_at": datetime.now(timezone.utc).isoformat(),
                    "url": response.url,
                    "status": response.status,
                    "method": request.method,
                    "resource_type": request.resource_type,
                }
                self._pending_network_events.append(record)
                if len(self._pending_network_events) > 250:
                    self._pending_network_events = self._pending_network_events[-250:]
            except Exception:  # noqa: BLE001
                return

        self._page.on("response", on_response)

    def _capture_step_screenshot(self, action: ExtractedAction) -> str:
        step_slug = _safe_slug(action.step_id)
        path = self.artifacts_dir / f"{step_slug}.png"
        # NOTE: Screenshot may capture sensitive data in the page content (e.g.,
        # PII in records). This is acceptable for debugging artifacts that are
        # stored locally. Do NOT upload screenshots to public locations.
        # The filename itself does not leak secrets (it's just the step_id).
        self._page.screenshot(path=str(path), full_page=True)
        return str(path)

    def _flush_step_network_trace(self, action: ExtractedAction) -> str:
        step_slug = _safe_slug(action.step_id)
        path = self.artifacts_dir / f"{step_slug}.network.json"
        filtered_events = [
            item
            for item in self._pending_network_events
            if _is_interesting_network_url(item.get("url", ""))
        ]
        # Redact query strings from URLs in the network trace, as they may contain
        # session tokens, access tokens, or frontdoor retURL params.
        redacted_events = []
        for event in filtered_events:
            redacted_event = event.copy()
            url = redacted_event.get("url", "")
            if "?" in url:
                redacted_event["url"] = url.split("?")[0] + "?<redacted>"
            redacted_events.append(redacted_event)

        path.write_text(json.dumps(redacted_events, indent=2), encoding="utf-8")
        self._pending_network_events = []
        return str(path)


def build_selector_candidates(action: ExtractedAction) -> list[str]:
    """
    Generate resilient selector candidates for dynamic Salesforce pages.
    """
    candidates: list[str] = []
    target = action.target.strip()
    context_prefixes = _context_selector_prefixes(action)

    if target.startswith("button:"):
        label = target.split(":", 1)[1].strip()
        raw = [f"button:has-text('{label}')", f"input[value='{label}']", f"[title='{label}']", f"[aria-label='{label}']"]
        candidates.extend(_apply_context_prefixes(raw, context_prefixes))
    elif target.startswith("input:"):
        label = target.split(":", 1)[1].strip()
        raw = [f"input[name='{label}']", f"input[aria-label='{label}']", f"label:has-text('{label}') + input"]
        candidates.extend(_apply_context_prefixes(raw, context_prefixes))
    elif target.startswith("link:"):
        label = target.split(":", 1)[1].strip()
        raw = [f"a:has-text('{label}')", f"[role='link'][aria-label='{label}']"]
        candidates.extend(_apply_context_prefixes(raw, context_prefixes))
    elif target.startswith("text:"):
        label = target.split(":", 1)[1].strip()
        candidates.extend([f"text={label}"])
    if action.ui_context.selector_hint:
        candidates.insert(0, action.ui_context.selector_hint)
    return candidates


def action_to_browser_operation(action: ExtractedAction) -> dict[str, Any]:
    op = {
        "type": action.action_type.value,
        "target": action.target,
        "value": action.value,
        "selectors": build_selector_candidates(action),
    }
    if action.action_type == ActionType.WAIT:
        op["duration_ms"] = int(action.value or "1000")
    return op


def _context_selector_prefixes(action: ExtractedAction) -> list[str]:
    prefixes: list[str] = []
    if action.ui_context.modal_name:
        modal = action.ui_context.modal_name.strip()
        prefixes.append(f"[role='dialog']:has-text('{modal}')")
        prefixes.append(".slds-modal__container")
    if action.ui_context.object_name:
        obj = action.ui_context.object_name.strip()
        prefixes.append(f"[data-aura-class*='{obj}']")
    if action.ui_context.view_name:
        view = action.ui_context.view_name.strip()
        prefixes.append(f"[role='main']:has-text('{view}')")
    return prefixes


def _apply_context_prefixes(selectors: list[str], prefixes: list[str]) -> list[str]:
    if not prefixes:
        return selectors
    contextual: list[str] = []
    for prefix in prefixes:
        for selector in selectors:
            contextual.append(f"{prefix} {selector}")
    return contextual + selectors


def _safe_slug(value: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_-]+", "_", value).strip("_") or "step"


def _is_interesting_network_url(url: str) -> bool:
    patterns = ("/services/data/", "/services/apexrest/", "/aura", "/webruntime/", "/flow/")
    return any(pattern in url for pattern in patterns)


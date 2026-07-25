from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
from typing import Any

from .models import ActionType, ExtractedAction
from .replay import SalesforceUIAdapter


class BrowserReplayAdapter(SalesforceUIAdapter):
    """
    Browser-backed replay adapter.

    This implementation intentionally avoids hard dependencies on browser
    libraries so it can run in environments where Playwright is not installed.
    Set SF_BLUEPRINT_PLAYWRIGHT=1 to enable live mode after wiring locators.
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

    def open_org(self, org_url: str) -> None:
        self.org_url = org_url
        if self.live_enabled:
            self._ensure_session()
            self._page.goto(org_url, wait_until="domcontentloaded")
            self._wait_ready_state()
            self._try_salesforce_login()

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
            return False, f"Live replay failed: {exc}", code
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

    def _try_salesforce_login(self) -> None:
        username = os.getenv("SF_USERNAME")
        password = os.getenv("SF_PASSWORD")
        if not username or not password:
            return
        try:
            if self._page.locator("#username").count() and self._page.locator("#password").count():
                self._page.fill("#username", username)
                self._page.fill("#password", password)
                self._page.click("#Login")
                self._wait_ready_state()
        except Exception:  # noqa: BLE001
            return

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
        path.write_text(json.dumps(filtered_events, indent=2), encoding="utf-8")
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


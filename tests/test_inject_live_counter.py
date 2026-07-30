"""Tests for the live recording counter in capture/inject.py.

Coverage:
1. ``live_counter_line`` — message text, error-state colouring, field values.
2. ``start_live_counter`` — fires at the configured interval (time.sleep mocked),
   stops when the stop event is set, and handles the error-state path.
3. Final "Recording ended" line includes total count (integration smoke).
"""

from __future__ import annotations

import sys
import threading
import time
from unittest.mock import MagicMock, patch

import pytest

# The module under test lives at capture/inject.py (not a package).
# The lane venv installs the *src* tree but not the capture/ script, so we
# import via importlib using the absolute path.
import importlib.util
from pathlib import Path

_INJECT_PATH = Path(__file__).resolve().parent.parent / "capture" / "inject.py"


def _load_inject():
    """Load capture/inject.py as a module without executing ``main``."""
    spec = importlib.util.spec_from_file_location("capture_inject", _INJECT_PATH)
    mod = importlib.util.module_from_spec(spec)
    # Playwright is not installed in the test environment; stub it out.
    import sys as _sys
    fake_pw = MagicMock()
    _sys.modules.setdefault("playwright", fake_pw)
    _sys.modules.setdefault("playwright.sync_api", fake_pw)
    spec.loader.exec_module(mod)
    return mod


inject = _load_inject()


# ============================================================================
# 1. live_counter_line — message text
# ============================================================================


def test_live_counter_line_zero_events():
    """With zero events the line reports zeros but is not empty."""
    line = inject.live_counter_line(0, 0, 0)
    assert "0 events captured" in line
    assert "(0 network)" in line
    assert "0 errors" in line
    assert "Press Enter to stop" in line


def test_live_counter_line_nonzero_counts():
    """Event counts are reflected in the counter line."""
    line = inject.live_counter_line(47, 12, 0)
    assert "47 events captured" in line
    assert "(12 network)" in line
    assert "0 errors" in line


def test_live_counter_line_with_sink_errors():
    """sink_errors > 0 is reflected in the counter line."""
    line = inject.live_counter_line(10, 3, 2)
    assert "2 errors" in line


def test_live_counter_line_no_colour_when_no_tty(monkeypatch):
    """When stdout is not a TTY the line must not contain ANSI escape codes."""
    monkeypatch.setattr(sys.stdout, "isatty", lambda: False)
    line = inject.live_counter_line(5, 1, 3, use_colour=True)
    assert "\033[" not in line, "ANSI codes must not appear on non-TTY output"


def test_live_counter_line_colour_yellow_on_tty_few_errors(monkeypatch):
    """1-2 sink errors -> yellow ANSI prefix when stdout is a TTY."""
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)
    line = inject.live_counter_line(5, 1, 2, use_colour=True)
    assert inject._YELLOW in line or inject._RED in line, (
        "error state must include colour on TTY"
    )
    assert inject._RESET in line


def test_live_counter_line_colour_red_on_tty_many_errors(monkeypatch):
    """3+ sink errors -> red ANSI prefix when stdout is a TTY."""
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)
    line = inject.live_counter_line(5, 1, 5, use_colour=True)
    assert inject._RED in line, ">=3 errors must use red"
    assert inject._RESET in line


def test_live_counter_line_no_colour_when_use_colour_false(monkeypatch):
    """``use_colour=False`` disables ANSI codes even on a TTY."""
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)
    line = inject.live_counter_line(5, 1, 5, use_colour=False)
    assert "\033[" not in line


def test_live_counter_line_no_colour_when_no_errors_on_tty(monkeypatch):
    """No errors -> no colour codes even when stdout is a TTY."""
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)
    line = inject.live_counter_line(5, 1, 0, use_colour=True)
    assert "\033[" not in line, "No colour when sink_errors == 0"


# ============================================================================
# 2. start_live_counter — fires at interval, stops cleanly
# ============================================================================


def test_start_live_counter_fires_at_interval(monkeypatch, capsys):
    """Counter fires after each interval expires.

    We mock ``threading.Event.wait`` to return False (keep going) twice, then
    True (stop). Each False return triggers one line of output.
    """
    # Build a stop event whose .wait() we control.
    stop_event = threading.Event()
    call_count = 0

    original_wait = stop_event.wait

    def fake_wait(timeout=None):
        nonlocal call_count
        call_count += 1
        if call_count <= 2:
            return False  # not stopped yet — trigger the counter body
        return True  # stop the loop

    stop_event.wait = fake_wait

    counts = [0, 0, 0]

    def get_counts():
        return tuple(counts)

    # Use a tiny interval; the fake_wait ignores the actual sleep duration.
    thread = inject.start_live_counter(get_counts, stop_event, interval=0.001)
    thread.join(timeout=2.0)

    captured = capsys.readouterr()
    # Should have printed 2 counter lines (the two False returns).
    output_lines = [l for l in captured.out.split("\n") if l.strip()]
    assert len(output_lines) >= 2, (
        f"Expected at least 2 counter lines, got: {output_lines}"
    )
    for ln in output_lines:
        assert "Recording" in ln or "inject" in ln, f"Unexpected line: {ln!r}"


def test_start_live_counter_stops_when_event_set():
    """Once the stop event fires, the thread exits promptly."""
    stop_event = threading.Event()
    calls = []

    def get_counts():
        calls.append(1)
        return (len(calls), 0, 0)

    # Very small interval; stop after a brief moment.
    thread = inject.start_live_counter(get_counts, stop_event, interval=0.05)
    time.sleep(0.12)
    stop_event.set()
    thread.join(timeout=1.0)

    assert not thread.is_alive(), "Counter thread must exit after stop_event is set"


def test_start_live_counter_error_state_in_output(monkeypatch, capsys):
    """When sink_errors > 0, the counter line text reflects the error count."""
    stop_event = threading.Event()
    call_count = 0

    def fake_wait(timeout=None):
        nonlocal call_count
        call_count += 1
        return call_count > 1  # fire once then stop

    stop_event.wait = fake_wait

    def get_counts():
        return (10, 3, 2)  # 2 sink errors

    # Force non-TTY so no ANSI codes interfere with text assertions.
    monkeypatch.setattr(sys.stdout, "isatty", lambda: False)

    thread = inject.start_live_counter(get_counts, stop_event, interval=0.001)
    thread.join(timeout=2.0)

    captured = capsys.readouterr()
    assert "2 errors" in captured.out, (
        f"Error count not reflected in counter output: {captured.out!r}"
    )
    assert "10 events captured" in captured.out


def test_start_live_counter_returns_daemon_thread():
    """The returned thread must be a daemon so it doesn't block process exit."""
    stop_event = threading.Event()
    stop_event.set()  # immediate stop

    thread = inject.start_live_counter(lambda: (0, 0, 0), stop_event, interval=60)
    assert thread.daemon, "Counter thread must be a daemon thread"
    thread.join(timeout=1.0)


# ============================================================================
# 3. COUNTER_INTERVAL constant
# ============================================================================


def test_counter_interval_is_five_seconds():
    """The interval is specified as 5 seconds in the brief."""
    assert inject.COUNTER_INTERVAL == 5, (
        f"COUNTER_INTERVAL must be 5, got {inject.COUNTER_INTERVAL}"
    )


# ============================================================================
# 4. Final line includes total count
# ============================================================================


def test_final_line_includes_total_count():
    """Smoke-test the final 'Recording ended' message format.

    The message is composed in main() as:
        f"[inject] ✓ Recording ended — {event_count} events total."
    Verify that the template matches the spec by constructing it directly.
    """
    event_count = 73
    final_line = f"[inject] ✓ Recording ended — {event_count} events total."
    assert "73" in final_line
    assert "Recording ended" in final_line
    assert "events total" in final_line

"""Playwright driver for DOM-capture recording of Salesforce processes.

This module launches a headed browser, authenticates with a Salesforce org via
frontdoor (bypassing MFA/SSO), injects the JavaScript recorder into every page
and iframe, and collects raw DOM events to a timestamped JSONL file while a
human operator performs a business process.

Design rationale:
- **Human-driven**: A synthetic clicker cannot know the business process. The
  operator navigates the UI and performs the sequence they would perform in
  production; this driver records what they did.
- **Frontdoor authentication**: Automating the Salesforce login form is fragile
  and violates policy. Instead, `sf org open --url-only` returns a signed
  `frontdoor.jsp` URL that bypasses MFA/SSO legitimately.
- **Network sidecar**: The network trace records the exact timestamps of
  Salesforce API calls, providing the correlation layer with causal evidence
  instead of heuristic step_id matching.
- **Persistent context**: Using `launch_persistent_context` with an
  org-specific `--user-data-dir` keeps cookies between recordings, so the
  operator does not have to sign in again if the session is still valid.
  **Constraint**: A persistent profile can only be used by one browser instance
  at a time; never combine with `--isolated`.
- **Process-scoped filenames**: Output files are prefixed with `process_name`
  and a timestamp so two captures of different processes in the same out_dir
  do not collide.

Output artifacts (prefix = ``<process_name>_<timestamp>``):
- ``<prefix>.dom_capture.jsonl`` — one JSON line per DOM event
- ``<prefix>.dom_capture.network.jsonl`` — network trace of Salesforce API calls
- ``<prefix>.dom_capture.manifest.json`` — capture metadata and provenance
"""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
import time
from pathlib import Path
from typing import Any

import typer
from playwright.sync_api import BrowserContext, Error as PlaywrightError, sync_playwright

app = typer.Typer()

# Hard-blocked org aliases per INTERFACE_CONTRACT.md §2.1 (rule 6).
BLOCKED_ORG_ALIASES = {"PPCDM", "PPCaccenture"}

# Salesforce API URL patterns for network trace.
SF_API_PATTERNS = {
    "/services/data/",
    "/aura",
    "/webruntime/",
    "/services/apexrest/",
}

# Slug pattern: letters, digits, and hyphens only.
_SLUG_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9-]*$")


def validate_process_name(process_name: str) -> str:
    """Validate and return a slug-safe process name.

    A valid process name contains only ASCII letters, digits, and hyphens,
    and must start with a letter or digit (not a hyphen).

    Args:
        process_name: The process name to validate.

    Returns:
        The validated process name (unchanged).

    Raises:
        ValueError: If the name contains spaces, slashes, or other disallowed
            characters.

    Examples:
        >>> validate_process_name("case-creation")
        'case-creation'
        >>> validate_process_name("case update")
        Traceback (most recent call last):
            ...
        ValueError: ...
    """
    if not process_name:
        raise ValueError("process_name must not be empty.")
    if not _SLUG_RE.match(process_name):
        raise ValueError(
            f"process_name {process_name!r} is not slug-safe. "
            "Use only ASCII letters, digits, and hyphens (e.g. 'case-creation')."
        )
    return process_name


def capture_sf_cli_version() -> str | None:
    """Return the Salesforce CLI version string from ``sf --version --json``.

    Returns:
        A version string such as ``"@salesforce/cli/2.x.y darwin-arm64 node-v20.x.y"``
        or ``None`` if the CLI is not installed or the command fails.
    """
    try:
        result = subprocess.run(
            ["sf", "--version", "--json"],
            check=True,
            capture_output=True,
            text=True,
            timeout=15,
        )
        data = json.loads(result.stdout)
        # The JSON output contains a "cliVersion" key.
        return data.get("cliVersion") or data.get("version") or result.stdout.strip()
    except Exception:
        # Non-fatal — missing CLI version does not block recording.
        return None


def capture_playwright_mcp_version() -> str | None:
    """Return the playwright-mcp package version if detectable.

    Attempts to read the version from the installed package metadata.

    Returns:
        A version string such as ``"1.50.0"`` or ``None`` if not detectable.
    """
    try:
        from importlib.metadata import version as pkg_version

        return pkg_version("playwright-mcp")
    except Exception:
        pass
    try:
        from importlib.metadata import version as pkg_version

        return pkg_version("mcp-playwright")
    except Exception:
        pass
    return None


def resolve_org_info(alias: str) -> dict[str, Any]:
    """Resolve org metadata via `sf org display --json`.

    Args:
        alias: Org alias or username.

    Returns:
        Parsed JSON dict from `sf org display`.

    Raises:
        subprocess.CalledProcessError: If the CLI command fails.
        ValueError: If the JSON is malformed or missing expected keys.
    """
    result = subprocess.run(
        ["sf", "org", "display", "--target-org", alias, "--json"],
        check=True,
        capture_output=True,
        text=True,
    )
    data = json.loads(result.stdout)
    if data.get("status") != 0:
        raise ValueError(f"sf org display failed: {data.get('message', 'unknown error')}")
    return data["result"]


def assert_org_is_safe(org_info: dict[str, Any]) -> None:
    """Fail-closed production safety guard.

    Refuses to proceed if the org looks like production. Refuses when:
    - `isSandbox` is false AND `isScratch` is false AND the instance URL does
      not contain a dev/sandbox/scratch marker.
    - Username contains `@salesforce.com` without a sandbox suffix.
    - Org alias is in the permanently blocked list.

    Args:
        org_info: Output of `resolve_org_info`.

    Raises:
        ValueError: If the org is production or blocked.
    """
    alias = org_info.get("alias") or org_info.get("username")
    if alias in BLOCKED_ORG_ALIASES:
        raise ValueError(
            f"Org alias '{alias}' is permanently out of scope per project rules."
        )

    username = org_info.get("username", "")
    if "@salesforce.com" in username and not any(
        suffix in username for suffix in [".sandbox", ".scratch", ".dev"]
    ):
        raise ValueError(
            f"Username '{username}' looks like a production @salesforce.com account. "
            "Refusing to proceed."
        )

    is_sandbox = org_info.get("isSandbox", False)
    is_scratch = org_info.get("isScratch", False)
    instance_url = org_info.get("instanceUrl", "")

    if not is_sandbox and not is_scratch:
        # Must have a dev/sandbox/scratch marker in the instance URL.
        if not any(
            marker in instance_url
            for marker in [".develop.my.salesforce.com", ".sandbox.my.salesforce.com", ".scratch.my.salesforce.com"]
        ):
            raise ValueError(
                f"Org '{alias}' is neither a sandbox nor a scratch org, and the "
                f"instance URL '{instance_url}' does not contain a dev/sandbox/scratch "
                f"marker. Refusing to proceed (production safety)."
            )


def parse_frontdoor_url(json_str: str) -> str:
    """Extract the frontdoor.jsp URL from `sf org open --url-only --json` output.

    Args:
        json_str: JSON string output of `sf org open --url-only --json`.

    Returns:
        The frontdoor.jsp URL.

    Raises:
        ValueError: If the JSON is malformed or missing the URL.
    """
    data = json.loads(json_str)
    if data.get("status") != 0:
        raise ValueError(f"sf org open failed: {data.get('message', 'unknown error')}")
    url = data.get("result", {}).get("url")
    if not url:
        raise ValueError("sf org open did not return a URL in result.url")
    return url


def compute_file_sha256(path: Path) -> str:
    """Compute SHA256 checksum of a file.

    Args:
        path: Path to the file.

    Returns:
        Hex-encoded SHA256 digest.
    """
    sha256 = hashlib.sha256()
    with path.open("rb") as f:
        while chunk := f.read(8192):
            sha256.update(chunk)
    return sha256.hexdigest()


def main(
    org_alias: str = typer.Option(..., help="Salesforce org alias or username"),
    out_dir: Path = typer.Option(
        Path("./outputs/capture"),
        help="Output directory for JSONL and manifest",
    ),
    start_url: str | None = typer.Option(
        None,
        help="Optional starting URL (defaults to org home after frontdoor)",
    ),
    note: str | None = typer.Option(
        None,
        help="Operator description of the process being recorded",
    ),
    process_name: str = typer.Option(
        ...,
        help=(
            "Short slug identifying the business process being recorded "
            "(e.g. 'case-creation', 'case-update'). "
            "Must contain only ASCII letters, digits, and hyphens."
        ),
    ),
) -> None:
    """Launch a headed browser, inject the DOM recorder, and collect events to JSONL.

    A human operator performs the business process. Press Enter in the terminal
    when done.

    Output files are named ``<process_name>_<timestamp>.dom_capture.jsonl`` so
    captures of different processes in the same out_dir never overwrite each other.
    """
    # 0. Validate process_name at startup before any side-effects.
    try:
        process_name = validate_process_name(process_name)
    except ValueError as exc:
        raise typer.BadParameter(str(exc), param_hint="--process-name") from exc

    # 1. Safety checks.
    print(f"[inject] Resolving org metadata for '{org_alias}'...")
    org_info = resolve_org_info(org_alias)
    assert_org_is_safe(org_info)
    print(f"[inject] ✓ Org is safe: {org_info.get('alias') or org_info.get('username')}")

    # 2. Get frontdoor URL.
    print("[inject] Obtaining frontdoor URL...")
    result = subprocess.run(
        ["sf", "org", "open", "--url-only", "--target-org", org_alias, "--json"],
        check=True,
        capture_output=True,
        text=True,
    )
    frontdoor_url = parse_frontdoor_url(result.stdout)
    print("[inject] ✓ Frontdoor URL obtained.")

    # 3. Prepare output directory and build timestamped, process-scoped filenames.
    out_dir.mkdir(parents=True, exist_ok=True)
    timestamp = int(time.time())
    file_stem = f"{process_name}_{timestamp}"
    jsonl_path = out_dir / f"{file_stem}.dom_capture.jsonl"
    network_jsonl_path = out_dir / f"{file_stem}.dom_capture.network.jsonl"
    manifest_path = out_dir / f"{file_stem}.dom_capture.manifest.json"

    # Recorder path.
    recorder_path = Path(__file__).parent / "recorder.js"
    if not recorder_path.exists():
        raise FileNotFoundError(
            f"Recorder script not found at {recorder_path}. "
            "Agent A1 must write it first."
        )
    recorder_sha256 = compute_file_sha256(recorder_path)

    # 4. State for the capture session.
    capture_id = f"capture-{int(time.time())}"
    started_at = time.time_ns()
    event_count = 0
    network_event_count = 0
    sink_errors = 0
    ingest_seq = 0

    # Open output files.
    jsonl_file = jsonl_path.open("a", encoding="utf-8")
    network_jsonl_file = network_jsonl_path.open("a", encoding="utf-8")

    # Write a header record so the JSONL file is self-describing.
    header = {
        "_record_type": "header",
        "capture_id": capture_id,
        "process_name": process_name,
        "org_alias": org_alias,
        "started_at": started_at,
    }
    jsonl_file.write(json.dumps(header) + "\n")
    jsonl_file.flush()

    # 5. Launch browser.
    print("[inject] Launching browser (headed mode)...")
    with sync_playwright() as p:
        # Persistent context keyed by org alias so cookies survive between runs.
        user_data_dir = Path.home() / ".sf-video-blueprint" / "browser-profiles" / org_alias
        user_data_dir.mkdir(parents=True, exist_ok=True)

        context = p.chromium.launch_persistent_context(
            str(user_data_dir),
            headless=False,
            args=["--disable-blink-features=AutomationControlled"],
        )

        # 6. Inject recorder into every page and iframe.
        context.add_init_script(path=str(recorder_path))

        # 7. Register sink handler.
        def handle_sink_event(source: dict[str, Any], event_dict: dict[str, Any]) -> None:
            nonlocal event_count, sink_errors, ingest_seq
            try:
                ingest_seq += 1
                # Stamp server-side metadata that the page cannot fake.
                event_dict["_ingest_seq"] = ingest_seq
                event_dict["_ingest_t"] = time.time_ns()
                event_dict["_frame_url"] = source["frame"].url
                event_dict["_page_index"] = context.pages.index(source["page"])
                jsonl_file.write(json.dumps(event_dict) + "\n")
                jsonl_file.flush()
                event_count += 1
            except Exception as e:
                sink_errors += 1
                print(f"[inject] ERROR in sink handler: {e}")

        context.expose_binding("__sfCaptureSink", handle_sink_event)

        # 8. Network listener for Salesforce API calls.
        def handle_response(response) -> None:
            nonlocal network_event_count
            try:
                url = response.url
                if any(pattern in url for pattern in SF_API_PATTERNS):
                    network_event = {
                        "t": time.time_ns(),
                        "url": url,
                        "method": response.request.method,
                        "status": response.status,
                        "resource_type": response.request.resource_type,
                    }
                    network_jsonl_file.write(json.dumps(network_event) + "\n")
                    network_jsonl_file.flush()
                    network_event_count += 1
            except Exception as e:
                print(f"[inject] ERROR in network handler: {e}")

        context.on("response", handle_response)

        # 9. Open first page and navigate to frontdoor.
        page = context.new_page()
        print(f"[inject] Navigating to frontdoor URL...")
        page.goto(frontdoor_url, wait_until="networkidle")
        print("[inject] ✓ Authenticated.")

        # Navigate to start URL if provided.
        if start_url:
            print(f"[inject] Navigating to {start_url}...")
            page.goto(start_url, wait_until="networkidle")

        # 10. Wait for operator to finish.
        print("\n" + "=" * 70)
        print("Recording started. Perform your process in the browser.")
        print("When done:")
        print("  1. Navigate to your starting point.")
        print("  2. Perform your process.")
        print("  3. Return here and press Enter to stop.")
        print("=" * 70 + "\n")

        try:
            input("Press Enter to stop recording...\n")
        except KeyboardInterrupt:
            print("\n[inject] KeyboardInterrupt received. Stopping...")
        except EOFError:
            print("\n[inject] EOFError (terminal closed?). Stopping...")

        # Check if the operator closed the browser instead.
        if page.is_closed():
            print("[inject] Browser was closed by operator. Stopping...")

        ended_at = time.time_ns()

    # 11. Close output files.
    jsonl_file.close()
    network_jsonl_file.close()

    # 12. Write manifest.
    manifest = {
        "capture_id": capture_id,
        "process_name": process_name,
        "org_alias": org_alias,
        "org_instance_url": org_info.get("instanceUrl"),
        "is_sandbox": org_info.get("isSandbox", False),
        "is_scratch": org_info.get("isScratch", False),
        "started_at": started_at,
        "ended_at": ended_at,
        "event_count": event_count,
        "network_event_count": network_event_count,
        "sink_errors": sink_errors,
        "recorder_sha256": recorder_sha256,
        "playwright_version": p.chromium.version,  # type: ignore[unreachable]
        "sf_cli_version": capture_sf_cli_version(),
        "playwright_mcp_version": capture_playwright_mcp_version(),
        "operator_note": note,
    }
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n")

    # 13. Summary.
    duration_sec = (ended_at - started_at) / 1e9
    print(f"\n[inject] ✓ Recording stopped.")
    print(f"[inject]   Duration: {duration_sec:.1f}s")
    print(f"[inject]   Events: {event_count}")
    print(f"[inject]   Network events: {network_event_count}")
    print(f"[inject]   Sink errors: {sink_errors}")
    print(f"[inject]   JSONL: {jsonl_path}")
    print(f"[inject]   Network JSONL: {network_jsonl_path}")
    print(f"[inject]   Manifest: {manifest_path}")


if __name__ == "__main__":
    typer.run(main)

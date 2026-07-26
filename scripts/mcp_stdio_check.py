#!/usr/bin/env python3
"""Drive the installed MCP server over real stdio JSON-RPC as a CI gate.

The unit tests call the tool functions directly, which proves the logic but NOT
that the thing is actually installable and speaks the protocol. This script
launches the `sf-blueprint-mcp` console script as a subprocess and talks to it the
way Claude Desktop, Cursor, or any other harness would.

It catches the failures unit tests structurally cannot:

- the console-script entry point is missing or points at the wrong symbol
- a stray `print()` corrupts the stdout JSON-RPC stream
- a tool returns something that is not JSON-serializable
- the server crashes on startup in a clean environment

It also re-asserts the contract that matters most, over the wire this time: a spec
derived from mock telemetry must come back `passed: false`. If a future change
lets that run pass, this exits non-zero — making the gate weaker is a defect, not
a fix.

Usage:
    python scripts/mcp_stdio_check.py <capture-path> [server-command]
"""

from __future__ import annotations

import asyncio
import json
import shutil
import sys
from pathlib import Path

try:
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
except ModuleNotFoundError:
    sys.exit(
        "FAIL: the 'mcp' package is not installed.\n"
        "This check needs it: pip install -e '.[dev,mcp]'"
    )

EXPECTED_TOOLS = {
    "health",
    "validate_capture",
    "derive_spec",
    "score_spec",
    "emit_agent_bundle",
    "emit_test_spec",
    "preview_api_names",
}


def fail(message: str) -> None:
    print(f"FAIL: {message}")
    sys.exit(1)


def _payload(result) -> dict:
    """Extract the JSON body of a tool result, failing loudly if it is not JSON."""
    if not result.content:
        fail("tool returned no content")
    text = result.content[0].text
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        fail(f"tool result was not valid JSON ({exc}): {text[:200]!r}")
        raise  # unreachable; keeps type checkers happy


async def run(capture: str, command: str) -> None:
    params = StdioServerParameters(command=command, args=[], env=None)

    async with (
        stdio_client(params) as (read, write),
        ClientSession(read, write) as session,
    ):
        init = await session.initialize()
        print(f"  connected: {init.serverInfo.name}")

        listing = await session.list_tools()
        names = {tool.name for tool in listing.tools}
        if names != EXPECTED_TOOLS:
            fail(
                "tool set mismatch over the wire.\n"
                f"  missing: {sorted(EXPECTED_TOOLS - names)}\n"
                f"  unexpected: {sorted(names - EXPECTED_TOOLS)}"
            )
        print(f"  all {len(names)} tools advertised")

        for tool in listing.tools:
            if not tool.description:
                fail(f"tool {tool.name} has no description; a model cannot use it")
            if not tool.inputSchema:
                fail(f"tool {tool.name} has no input schema")
        print("  every tool has a description and an input schema")

        health = _payload(await session.call_tool("health", {}))
        if not health.get("ok"):
            fail(f"health returned not-ok: {health}")
        contacts_org = health.get("capabilities", {}).get("contactsSalesforceOrg")
        # This used to require exactly False. That was true until the server gained
        # a real compile call, and asserting it now would force the server to make a
        # *false* disclosure — the worse of the two failures. The property that still
        # has to hold is that contacting an org is conditional and the condition is
        # named: False (never) is fine, and so is a string that says what triggers it.
        # An unconditional True is not, because a harness reading this needs to know
        # that a default invocation stays offline.
        if contacts_org is True:
            fail(
                "health declares contactsSalesforceOrg: true unconditionally. Every "
                "tool but emit_agent_bundle(org_alias=...) is offline; saying "
                "otherwise misinforms a harness about what a default call does."
            )
        if contacts_org is not False and not (
            isinstance(contacts_org, str) and "org_alias" in contacts_org
        ):
            fail(
                "health must say either that this server never contacts an org "
                "(False) or exactly what makes it do so — the disclosure has to name "
                f"the org_alias condition. Got: {contacts_org!r}"
            )
        if not health.get("limitations"):
            fail("health must disclose the project's limitations")
        print(f"  health ok (version {health['serverVersion']})")

        derived = _payload(
            await session.call_tool(
                "derive_spec",
                {
                    "capture_path": capture,
                    "org_url": "https://example-dev.develop.my.salesforce.com",
                },
            )
        )
        if not derived.get("ok"):
            fail(f"derive_spec failed: {derived.get('error')}")
        if not derived.get("intent"):
            fail("derive_spec returned no intent")
        print(
            f"  derive_spec ok: intent={derived['intent']!r} "
            f"score={derived['score']} passed={derived['passed']}"
        )

        # The contract. CI has no org, so telemetry is mock and the gate must
        # refuse. A pass here means the gate was weakened.
        if derived["provenance"]["telemetry_source"] != "mock":
            fail(
                "expected telemetry_source='mock' in CI (there is no org here), "
                f"got {derived['provenance']['telemetry_source']!r}"
            )
        if derived["evidence_is_real"] is not False:
            fail("a mock-telemetry run reported evidence_is_real=True")
        if derived["passed"] is not False:
            fail(
                "CONTRACT VIOLATION: a spec built from MOCK telemetry passed the "
                "quality gate over MCP. The gate has been weakened; that is a "
                "defect, not a fix."
            )
        if not derived["blocking_issues"]:
            fail("a blocked run reported no blocking issues")
        print("  contract holds: mock telemetry is refused by the gate")

        missing = _payload(
            await session.call_tool(
                "derive_spec", {"capture_path": "/nonexistent/capture.jsonl"}
            )
        )
        if missing.get("ok") is not False or missing["error"]["code"] != "NOT_FOUND":
            fail(f"a missing file should return a NOT_FOUND envelope, got {missing}")
        print("  error path returns structured data, not a crash")

        names_preview = _payload(
            await session.call_tool(
                "preview_api_names", {"process_description": "Update Case Status"}
            )
        )
        expected_router = f"go_to_{names_preview['subagentName']}"
        if names_preview["routerActionName"] != expected_router:
            fail(
                "router action name diverged from the subagent name: "
                f"{names_preview['routerActionName']!r} != {expected_router!r}"
            )
        print("  cross-artifact naming stays consistent")


def main() -> None:
    if len(sys.argv) < 2:
        sys.exit(f"usage: {sys.argv[0]} <capture-path> [server-command]")

    capture = Path(sys.argv[1]).resolve()
    if not capture.is_file():
        fail(f"no capture file at {capture}")

    command = sys.argv[2] if len(sys.argv) > 2 else "sf-blueprint-mcp"
    if shutil.which(command) is None:
        fail(
            f"{command!r} is not on PATH. The console script did not install — "
            "check [project.scripts] in pyproject.toml."
        )

    print(f"Driving {command} over stdio JSON-RPC...")
    asyncio.run(run(str(capture), command))
    print("\nPASS: the MCP server installs, speaks the protocol, and holds its contract.")


if __name__ == "__main__":
    main()

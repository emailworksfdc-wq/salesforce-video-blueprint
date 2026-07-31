# MCP server — install and use

**STATUS: IMPLEMENTED.** Verified by `tests/test_mcp_server.py` (34 tests) and by
`scripts/mcp_stdio_check.py`, which launches the installed executable and drives
it over real stdio JSON-RPC in CI.

`sf-blueprint-mcp` exposes this pipeline to any AI tool that speaks the
[Model Context Protocol](https://modelcontextprotocol.io) — Claude Code, Claude
Desktop, Cursor, Windsurf, Continue, Zed, or your own client. Install it once and
point whichever harness you use at it.

---

## What it can and cannot do

**Every tool is offline and read-only.** No tool contacts a Salesforce org,
launches a browser, or writes outside a path you explicitly pass. An agent driving
this server cannot modify an org through it. That is deliberate: an LLM deciding on
its own to replay recorded clicks against a live org is the failure this project
should not enable.

**Telemetry is always mocked.** Collecting real telemetry requires a live org, so
every spec produced through MCP is stamped `telemetry_source: "mock"` and
**cannot pass the score gate**. A result of `passed: false` with a `mock`
provenance is the expected outcome, not something to work around. Call `health`
for the full limitation list.

---

## Install a Claude Code skill (quickest path)

If you use **Claude Code**, the fastest way to get started is to install the
bundled skill. It teaches Claude how to drive the CLI and MCP tools automatically,
fires on phrases like "capture my Salesforce process" or "build an Agentforce
agent from this recording", and includes a quick-start, flag reference, and
troubleshooting guide.

**Global install** (works in every project):

```bash
mkdir -p ~/.claude/skills
curl -fsSL https://raw.githubusercontent.com/emailworksfdc-wq/salesforce-video-blueprint/main/skills/sf-blueprint.md \
  -o ~/.claude/skills/sf-blueprint.md
```

**Project-scoped** (checked-in alongside your code):

```bash
mkdir -p .claude/skills
cp skills/sf-blueprint.md .claude/skills/sf-blueprint.md
```

Then just tell Claude Code what you want in plain English — the skill activates
automatically. No further configuration needed for pure CLI use.

To also expose the MCP tools (so Claude can call `derive_spec`, `score_spec`,
etc. as structured tool calls), continue with the MCP install below.

---

## Install

Requires **Python ≥ 3.11**.

```bash
pip install "sf-video-blueprint[mcp] @ git+https://github.com/emailworksfdc-wq/salesforce-video-blueprint.git@main"
```

> Install from `@main`, not `@v0.1.0` — the v0.1.0 tag predates a dependency fix
> and does not install on Python 3.12+. See [Known defects](../README.md#known-defects).

Verify:

```bash
which sf-blueprint-mcp
```

The `[mcp]` extra is what pulls in the protocol SDK. Without it the command
installs but exits with a message telling you to add the extra — the CLI and
library work fine without it.

### Isolated install (recommended)

`pipx` keeps the server and its dependencies out of your other environments:

```bash
pipx install "sf-video-blueprint[mcp] @ git+https://github.com/emailworksfdc-wq/salesforce-video-blueprint.git@main"
```

`uv` works too, and needs no install step at all — see the `uvx` config below.

---

## Configure your harness

The server speaks **stdio**. Any MCP client can launch it; only the config file
location and JSON shape differ.

### Claude Code

The repo ships a `.mcp.json` at its root — cloning the repo is enough to wire up the server in Claude Code automatically.

**If you installed with `pipx` or the binary is on your `$PATH`**, the default `.mcp.json` works as-is:

```json
{
  "mcpServers": {
    "sf-blueprint": {
      "command": "sf-blueprint-mcp"
    }
  }
}
```

**If you installed with `pip install -e .` into a project venv**, the binary is at `.venv/bin/sf-blueprint-mcp` and is NOT on your system `$PATH`. Update `.mcp.json` to use the relative path:

```json
{
  "mcpServers": {
    "sf-blueprint": {
      "command": ".venv/bin/sf-blueprint-mcp"
    }
  }
}
```

Or add it from the command line using the absolute path:

```bash
claude mcp add sf-blueprint -- "$(pwd)/.venv/bin/sf-blueprint-mcp"
```

### Claude Desktop

`~/Library/Application Support/Claude/claude_desktop_config.json` on macOS,
`%APPDATA%\Claude\claude_desktop_config.json` on Windows:

```json
{
  "mcpServers": {
    "sf-blueprint": {
      "command": "sf-blueprint-mcp"
    }
  }
}
```

Restart Claude Desktop after editing. If the server does not appear, use an
absolute path — GUI apps do not inherit your shell `PATH`:

```json
{
  "mcpServers": {
    "sf-blueprint": {
      "command": "/Users/you/.local/bin/sf-blueprint-mcp"
    }
  }
}
```

Find it with `which sf-blueprint-mcp`.

### Cursor

`.cursor/mcp.json` in your project, or `~/.cursor/mcp.json` globally. Same shape:

```json
{
  "mcpServers": {
    "sf-blueprint": {
      "command": "sf-blueprint-mcp"
    }
  }
}
```

### Any other stdio client

Launch `sf-blueprint-mcp` with no arguments and speak MCP over its stdin/stdout.
Configuration keys vary by client (`command` / `args` / `env` is the usual trio),
but nothing about this server is harness-specific.

### Without installing (uv)

```json
{
  "mcpServers": {
    "sf-blueprint": {
      "command": "uvx",
      "args": [
        "--from",
        "sf-video-blueprint[mcp] @ git+https://github.com/emailworksfdc-wq/salesforce-video-blueprint.git@main",
        "sf-blueprint-mcp"
      ]
    }
  }
}
```

### Optional environment variables

| Variable | Default | Effect |
| --- | --- | --- |
| `SF_BLUEPRINT_MCP_LOG_LEVEL` | `INFO` | Log verbosity. `DEBUG` for troubleshooting. Logs go to **stderr** — stdout is the JSON-RPC transport. |

---

## Tools

| Tool | Purpose |
| --- | --- |
| `health` | Version, capabilities, and this project's real limitations. **Call this first.** |
| `validate_capture` | Integrity-check a `dom_capture.jsonl` without deriving anything. Reports discarded lines and why. |
| `derive_spec` | The core tool. Capture → agent spec + score. Optionally writes the spec JSON. |
| `score_spec` | Score an existing spec JSON against the 7-dimension gate. |
| `emit_agent_bundle` | Emit an Agentforce Agent Script (`.agent`) bundle plus its `.bundle-meta.xml`. |
| `emit_test_spec` | Emit a test spec in the `legacy` (AiEvaluationDefinition) or `ngt` (AiTestingDefinition) dialect. |
| `preview_api_names` | Show the topic / subagent / router API names this project would generate. |

### Response envelope

Every tool returns the same wrapper, so a client can branch on `ok` without
knowing which tool it called:

```json
{
  "ok": true,
  "requestId": "a1b2c3d4e5f6",
  "serverVersion": "0.1.0",
  "durationMs": 42
}
```

Failures return `ok: false` and a typed error instead of raising — a stack trace
gives a model nothing to act on, an error code does:

```json
{
  "ok": false,
  "requestId": "a1b2c3d4e5f6",
  "error": {
    "code": "VALIDATION",
    "message": "Capture failed integrity validation; no spec was built.",
    "findings": ["DATA LOSS: Zero events parsed, but 3 lines were skipped."],
    "remedy": "Run validate_capture for detail. Re-record rather than forcing this file through."
  }
}
```

Codes in use: `VALIDATION`, `NOT_FOUND`, `DEPENDENCY`, `INTERNAL`.

---

## Example session

Ask your assistant, in plain language:

> Validate `~/recordings/case_triage.dom_capture.jsonl`, derive an agent spec from
> it, and tell me what is stopping it from passing the quality gate.

It will call `validate_capture`, then `derive_spec`, and report something like:

```
Intent:     Update Case (Status)   (confidence 0.70)
Score:      79/100  band=low  passed=false
Blocking:   Spec was built from mock/unknown telemetry, not a live org.

Weakest dimensions:
  testability          0/10   no observed failure path
  provenance_integrity 0/5    telemetry_source=mock
  evidence_grounding  25/30
```

That verdict is correct and is not a bug. To raise it, capture better evidence: a
recording that exercises a failure path, and a live-mode run with real telemetry.
**Never lower a threshold** — see [CONTRIBUTING.md](../CONTRIBUTING.md#1-never-weaken-a-gate-to-make-a-number-go-up).

---

## Troubleshooting

**Server does not appear in the client.** Check the command resolves
(`which sf-blueprint-mcp`) and use an absolute path in GUI apps, which do not
inherit your shell `PATH`.

**"The MCP server needs the 'mcp' package".** The extra was not installed. Reinstall
with `[mcp]` in the specifier — the quotes matter in zsh.

**Client reports a protocol/parse error.** Something wrote to stdout, which is the
JSON-RPC transport. Server logs go to stderr by design; if you have modified the
server, check for a stray `print()`. `tests/test_mcp_server.py::test_no_tool_writes_to_stdout`
guards this.

**Every spec comes back `passed: false`.** Working as designed — telemetry is
mocked over MCP. See [What it can and cannot do](#what-it-can-and-cannot-do).

**Reproduce the whole handshake yourself:**

```bash
python scripts/mcp_stdio_check.py examples/case_triage.dom_capture.jsonl
```

---

## Deviation from the original design

[`docs/mcp-product-spec.md`](mcp-product-spec.md) specified a generic
`workflow.plan` / `execute` / `status` / `result` / `cancel` / `health` tool set
with idempotency keys and a run registry. **This server exposes the pipeline's
actual capabilities instead**, because every operation here is synchronous,
offline, and side-effect free — there is no long-running mutating job for which a
run registry or an idempotency key would mean anything. Implementing `cancel` for
a call that returns in 40 ms would be decoration, and `execute` with a
free-text `workflowId` would hide the seven real capabilities behind a string
parameter a model has to guess.

Adopted from that spec: the response envelope, the error taxonomy, and structured
stderr logging with `requestId` / `tool` / `durationMs` / `outcome` / `error.code`.

Deferred with it: HTTP/SSE transport (stdio covers every target harness), auth
(there is nothing to authorize — no org contact, no writes), and rate limiting
(no remote dependency to protect).

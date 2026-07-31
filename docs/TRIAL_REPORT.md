# Trial Run Report — salesforce-video-blueprint New User Simulation

**Date:** 2026-07-31  
**Tester:** Simulated brand-new user (Claude Code, Sonnet 4.6)  
**Repo:** https://github.com/emailworksfdc-wq/salesforce-video-blueprint  
**Working dir:** `/tmp/sfvb-trial-user/salesforce-video-blueprint/`  
**Report output:** `/Users/akshay.kashyap/.claude/jobs/a508223b/tmp/trial_run/TRIAL_REPORT.md`

---

## Phase A: Install

### Step 1 — Clone

```bash
cd /tmp/sfvb-trial-user
git clone https://github.com/emailworksfdc-wq/salesforce-video-blueprint.git
# Exit 0. Clean clone, no issues.
```

**Result:** Success. Repo cloned in ~2 seconds. No friction.

---

### Step 2 — Python version check

```
Python 3.13.14
```

README warns that the system macOS Python is 3.9 and will fail. Tested on 3.13 — works fine. The `v0.1.0` tag had a `greenlet` wheel failure on 3.13; `main` (v0.1.1) resolves `greenlet 3.5.4` from a wheel and installs cleanly.

---

### Step 3 — `python3 -m venv .venv && .venv/bin/pip install -e ".[dev,mcp]"`

```
Successfully installed MarkupSafe-3.0.3 annotated-doc-0.0.5 ...
sf-video-blueprint-0.1.1 ...
```

**Result:** Success. All 54 packages installed without error. The `greenlet` fix on `main` is confirmed working. One informational pip upgrade notice (`26.1.2 → 26.2`) — not an error.

**Notable:** `mcp 1.29.0` pulled in; without the `[mcp]` extra this package is absent and the MCP server tests skip (as documented).

---

### Step 4 — Test suite

```bash
.venv/bin/python -m pytest -q
# 1806 passed, 1 skipped in 33.80s
```

**Result:** Exactly matches the README claim. Zero failures.

The 1 skip is the `SF_BLUEPRINT_E2E_DIR` opt-in check as documented. No surprises.

---

## Phase B: CLI

### Step 5 — Entry point check

```bash
.venv/bin/sf-blueprint --help
```

Output:
```
Usage: sf-blueprint [OPTIONS] COMMAND [ARGS]...
Generate Salesforce process blueprint from video inputs.

Commands:
  capture   Launch a headed browser, inject the DOM recorder...
  run
  refine    Iteratively refine an agent spec using the offline scoring loop.
  iterate   Iteratively refine an agent spec against a real Salesforce org.
  deploy    Emit an Agentforce bundle from a capture and deploy it to a sandbox.
  pipeline  One-click pipeline: capture → run → refine (offline) → iterate (live, optional).
```

**Friction:** The `run` command shows no description text in the help listing — just a blank line. Every other subcommand has a description. Minor UX gap.

---

### Step 6 — CLI run on bundled example

```bash
.venv/bin/sf-blueprint run \
  --capture examples/case_triage.dom_capture.jsonl \
  --org-url "https://example-dev.develop.my.salesforce.com" \
  --output-path outputs/case_triage.html
```

Output:
```
EXTRACTION: noise reduction: 8 raw events -> 10 actions (coalesced 0 input, dropped 0 bubbling, 0 scroll, 0 keydown, synthesized 2 navigate)
EXTRACTION: Step 2: weak target (no accessible name), using positional form
EXTRACTION: Step 7: weak target (no accessible name), using positional form
EXTRACTION: Step 8: weak target (no accessible name), using positional form
Master blueprint generated: outputs/case_triage.html
Agent spec (machine-readable) generated: outputs/case_triage.agent-spec.json
Derived intent: Update Case (Status) (confidence 0.70)
WARNING: this run contains SIMULATED data and is not audit evidence. Simulated: replay (no browser drove the org; every step auto-succeeds); telemetry and data deltas (fabricated sample values, not org data)
```

Exit: 0. **Result: Success.**

Outputs produced:
- `outputs/case_triage.html` — human-readable blueprint
- `outputs/case_triage.agent-spec.json` — machine-readable spec

The WARNING line is expected and correct: without a live org, telemetry is mocked.

---

### Step 7 — Score the spec

#### README example (broken)

```python
# From README — copied verbatim:
r = score_spec_file('outputs/case_triage.agent-spec.json')
```

**Error:**
```
AttributeError: 'str' object has no attribute 'exists'
  File ".../spec_score.py", line 610, in score_spec_file
    if not path.exists():
```

**Root cause:** `score_spec_file` calls `.exists()` on its argument directly (`path: Path` type annotation), but does not coerce a `str` to `Path` before use. The README code example passes a bare string, which triggers this immediately.

**Workaround:**
```python
from pathlib import Path
r = score_spec_file(Path('outputs/case_triage.agent-spec.json'))
```

#### Actual score result

```
85/100  band=low  passed=False
  evidence_grounding      25/30
  completeness            15/15
  honesty                 20/20
  specificity             10/10
  testability              5/10
  placeholder_freedom     10/10
  provenance_integrity     0/5
BLOCKED: Spec was built from mock/unknown telemetry, not a live org.
         Cannot reach the top band without observed server-side behaviour.
```

**Result:** Gate working as designed. 85 raw points, blocked by mock telemetry. `testability=5/10` (up from the README's example of 0/10 — the example capture may have been updated). `provenance_integrity=0/5` for mock telemetry.

---

## Phase C: Skill

### Step 8 — Install skill

```bash
mkdir -p ~/.claude/skills
cp skills/sf-blueprint.md ~/.claude/skills/sf-blueprint.md
# -rw-r--r--  7.1K  sf-blueprint.md
```

**Result:** Success. Clean install.

#### What the skill contains

- **Frontmatter:** `name: sf-blueprint`, `description`, `triggers` array with 8 regex patterns
- **Trigger phrases:** "capture.*salesforce", "record.*process.*salesforce", "agentforce agent.*from.*recording", "agent spec.*from.*capture", "sf-blueprint", "salesforce-video-blueprint", "dom_capture", "blueprint.*pipeline", "refine.*agent.*spec", "deploy.*agentforce.*bundle"
- **176 lines** covering: quick-start (3 commands), CLI commands + key flags table, MCP tools table, typical MCP session sequence, quality gate rules, security rules, permitted dev orgs, architecture diagram, troubleshooting table, install options
- **Skill file is honest:** includes known limitations, explicitly states `passed: false` with mock telemetry is expected behavior not a bug

#### How it works

Claude Code loads `.claude/skills/*.md` automatically. When a user prompt matches any trigger pattern, the skill's content is injected into context, giving the model CLI flag references, the MCP tool inventory, security rules (never target production, tokens not on argv, blocked orgs), and troubleshooting guidance — without the user needing to know any of this.

---

## Phase D: MCP

### Step 9 — `.mcp.json` validation

```bash
cat .mcp.json | python3 -m json.tool
```

Output:
```json
{
    "mcpServers": {
        "sf-blueprint": {
            "command": "sf-blueprint-mcp"
        }
    }
}
```

**Valid JSON. Minimal and correct.**

**Friction:** The command is `"sf-blueprint-mcp"` with no path qualifier. This works if the binary is on `$PATH` (e.g., after `pipx install`). With a project-local `pip install -e .`, the binary lives at `.venv/bin/sf-blueprint-mcp` and is NOT on system `$PATH`. GUI MCP clients (Claude Desktop, Cursor) that don't inherit the shell's venv activation will fail to launch the server. The skill's troubleshooting table calls this out — "GUI apps don't inherit PATH" — but the `.mcp.json` itself gives no hint. A more robust default would be `"command": ".venv/bin/sf-blueprint-mcp"` or document an explicit absolute-path override.

---

### Step 10 — MCP health (stdio JSON-RPC)

Script: `/tmp/sfvb-trial-user/mcp_health_check.py`

```
Launching: .venv/bin/sf-blueprint-mcp

=== MCP Server Connected ===
Server name:    sf-video-blueprint
Server version: 1.29.0         ← MCP protocol version; serverVersion in body is 0.1.1

=== Tool List (11 tools) ===
  derive_spec
  emit_agent_bundle
  emit_test_spec
  health
  preview_api_names
  run_deploy
  run_iterate
  run_pipeline_full
  run_stage5_round
  score_spec
  validate_capture

=== Health Response ===
ok:            True
serverVersion: 0.1.1

Capabilities:
  offline: by default; emit_agent_bundle(org_alias=...) compiles against an org; run_stage5_round and run_iterate call sf agent test run-eval against a live org; run_deploy validates and deploys to an org when given org_alias
  readOnly: mostly; run_deploy(org_alias=...) deploys metadata to the target org when not given validate_only=True or dry_run=True
  contactsSalesforceOrg: emit_agent_bundle when given org_alias; run_stage5_round and run_iterate always (they require org_alias); run_deploy when given org_alias
  launchesBrowser: False
  telemetry: mock-only — collecting real telemetry needs a live org

Known Limitations:
  - An emitted .agent bundle may be syntactically invalid...validated exactly once (2026-07-26)...
  - `locallyValid: true` is not org validation. validate_locally() reported zero findings on the exact file the Salesforce compiler rejected with 24 errors.
  - Compiling is syntax, not semantics. No agent has been published...
  - Telemetry is always mocked here, so every derived spec is stamped telemetry_source=mock and cannot pass the score gate. That is correct behaviour, not a bug to work around.
  - Video files are not supported. The video extractor is a stub...
  - Capture ingest can silently discard events: the integrity gate only refuses at >=50% loss...
  - No agent actions (@apex.*/@flow.*) are ever emitted, by design...
```

**Result:** Server starts, advertises 11 tools, health reports honest limitations.

**Observation:** `init.serverInfo.version` is `1.29.0` (the MCP library version) while `health.serverVersion` is `0.1.1` (the project version). These are different things and both are correct, but could confuse a user who sees `1.29.0` and thinks the project is at v1.29.

---

### Step 11 — MCP stdio protocol check (`mcp_stdio_check.py`)

```bash
.venv/bin/python scripts/mcp_stdio_check.py \
  examples/case_triage.dom_capture.jsonl \
  .venv/bin/sf-blueprint-mcp
```

Output:
```
Driving .venv/bin/sf-blueprint-mcp over stdio JSON-RPC...
  connected: sf-video-blueprint
  all 11 tools advertised
  every tool has a description and an input schema
  health ok (version 0.1.1)
  derive_spec ok: intent='Update Case (Status)' score=85 passed=False
  contract holds: mock telemetry is refused by the gate
  error path returns structured data, not a crash
  cross-artifact naming stays consistent

PASS: the MCP server installs, speaks the protocol, and holds its contract.
```

Exit: 0. **Full PASS.** This script is a comprehensive integration test:
- Protocol handshake (initialize)
- Tool enumeration and schema validation
- Health contract checks (offline-by-default disclosure, limitations present)
- `derive_spec` result verification (intent, score, passed=False for mock)
- Error envelope for missing file (`NOT_FOUND` code)
- Cross-artifact name consistency (`preview_api_names` routerActionName = `go_to_<subagentName>`)

---

### Step 12 — `derive_spec` in-process

Script: `/tmp/sfvb-trial-user/derive_spec_inprocess.py`

```python
from sf_video_blueprint import mcp_server
result = mcp_server.run_pipeline_full(
    capture_path=str(CAPTURE),
    skip_refine=True,
)
```

#### Key fields

| Field | Value |
|---|---|
| `ok` | `True` |
| `score` | `85` |
| `displayScore` | `59` |
| `intent` | `"Update Case (Status)"` |
| `passed` | `false` |
| `blockingIssues` | `["Spec was built from mock/unknown telemetry..."]` |
| `evidenceIsReal` | `false` |
| `provenance.telemetry_source` | `"mock"` |
| `provenance.extraction_source` | `"dom-capture"` |
| `eventsParsed` | `8` |
| `actionsExtracted` | `10` |
| `skippedLineCount` | `0` |
| `lossRatio` | `0.0` |

**Result:** Success. All fields populated correctly.

**Observation — `score` vs `displayScore`:** The MCP envelope returns both `score=85` (raw) and `displayScore=59` (capped at `MAX_BLOCKED_DISPLAY_TOTAL=59` when blocking issues present). This is intentional — prevents a user from seeing "85/100" and thinking a blocked spec is "nearly there". The cap is a deliberate anti-misread feature. However, without reading `spec_score.py`, this looks like a scoring bug. The envelope has no field explaining why the two differ; a `displayScoreNote` field would make this self-documenting.

**Observation — `blocking_issues` key vs `blockingIssues` envelope key:** The Python `SpecScore` dataclass uses `blocking_issues` (snake_case), but the MCP JSON envelope uses `blockingIssues` (camelCase). They're two separate things (one is a Python field, one is a JSON serialization), but in-process code that tries `result.get("blocking_issues")` returns `None` while `result.get("blockingIssues")` returns the list. The README example and the brief's requested print format (`blockingIssues`) uses the envelope key, which is correct — but it's a gotcha worth documenting.

---

## Friction Log

Every friction point, in order encountered:

| # | Step | Description | Severity |
|---|---|---|---|
| F1 | Clone | Repo not cloned yet in working dir at session start — had to find it via `find`. Minor for a real new user who would clone from GitHub. | Low |
| F2 | Install | `pip` upgrade notice (`26.1.2 → 26.2`). Not an error but adds noise to install output. | Trivial |
| F3 | CLI `--help` | `run` subcommand shows blank description in help output — every other subcommand has one. First impression gap. | Low |
| F4 | Score API | `score_spec_file('string')` crashes with `AttributeError: 'str' object has no attribute 'exists'`. README example uses a bare string. New user copies README verbatim → immediate error. Fix: wrap arg in `Path()`. | **Medium** |
| F5 | `.mcp.json` | `"command": "sf-blueprint-mcp"` assumes binary is on `$PATH`. Not true after `pip install -e .` in a project venv. GUI MCP clients will silently fail to launch the server. Skill troubleshooting mentions this but the config file doesn't. | Medium |
| F6 | MCP health | `init.serverInfo.version` = `1.29.0` (MCP library version) vs `health.serverVersion` = `0.1.1` (project version). Not wrong, but potentially confusing — could look like the project is at v1.29. | Low |
| F7 | `displayScore` | MCP envelope has `score=85` and `displayScore=59` with no explanation of the difference inline. Looks like a scoring inconsistency without reading source. A `displayScoreNote` field would self-document the cap. | Low |
| F8 | `blocking_issues` vs `blockingIssues` | Python dataclass key (snake_case) vs MCP envelope key (camelCase) — in-process callers must use the envelope's camelCase key. Not a bug, but a common footgun when switching between Python API and MCP API. | Low |
| F9 | Score README example | The README score example shows `79/100` but the actual run produces `85/100`. The example appears to be from an older version of the spec. Non-critical — the format is correct — but the number mismatch could confuse a new user comparing their output to docs. | Low |
| F10 | No `sf-blueprint run` description | Minor UX: in `--help` output, `run` has no one-line description (other commands have one). | Low |

---

## Summary Results

### Install
| Check | Result |
|---|---|
| Clone | ✅ Clean |
| Python version | ✅ 3.13.14 — works |
| `pip install -e ".[dev,mcp]"` | ✅ All packages installed |
| `greenlet` wheel (known `v0.1.0` defect) | ✅ Fixed on main — no issue |

### Tests
| Metric | Value |
|---|---|
| Passed | 1806 |
| Skipped | 1 (expected — E2E opt-in) |
| Failed | 0 |
| Duration | 33.80s |

### CLI
| Check | Result |
|---|---|
| Entry point exists | ✅ `sf-blueprint` available in `.venv/bin/` |
| `run` on example capture | ✅ Exit 0, outputs generated |
| Score: total / band | 85/100, `low` band |
| Score: passed | `False` (mock telemetry, by design) |
| Score README example | ⚠️ Crashes on bare string — needs `Path()` wrapping |

### Skill
| Check | Result |
|---|---|
| File present | ✅ `skills/sf-blueprint.md` (176 lines) |
| Install method | ✅ `cp skills/sf-blueprint.md ~/.claude/skills/` |
| Trigger patterns | ✅ 8 regex patterns covering all major entry points |
| Content quality | ✅ CLI flags, MCP tools, security rules, troubleshooting table |
| Honesty | ✅ Explicitly states mock-telemetry `passed: false` is expected |

### MCP
| Check | Result |
|---|---|
| `.mcp.json` valid JSON | ✅ |
| `.mcp.json` correct structure | ✅ minimal `mcpServers` config |
| Binary on PATH | ⚠️ Not system PATH — only in `.venv/bin/`. GUI clients will fail. |
| Health tool | ✅ ok=true, version=0.1.1, limitations present |
| 11 tools advertised | ✅ All present with descriptions and input schemas |
| `derive_spec` over wire | ✅ intent, score, passed=False, contract holds |
| Error envelope | ✅ `NOT_FOUND` for missing file |
| Full stdio check | ✅ PASS (mcp_stdio_check.py) |
| In-process `run_pipeline_full` | ✅ All expected fields populated |

---

## Overall UX Assessment

| Area | Score | Notes |
|---|---|---|
| **Install** | 9/10 | Clean, fast, matches docs. Only friction: pip upgrade notice and the `v0.1.0` tag being broken (install from `@main` is the right call but requires reading the README). |
| **CLI** | 7/10 | Output is informative, WARNING line is excellent. Loses points for: `run` missing help description, README `score_spec_file` example crashes on string input, score number mismatch with docs. Core functionality works. |
| **Skill** | 8/10 | Well-structured, honest, comprehensive. Trigger patterns are good. Would benefit from a line explaining `displayScore` vs `score`. |
| **MCP** | 8/10 | Excellent — health disclosure is unusually honest, limitations are verbose and specific (the "validated exactly once" note is a standout). Loses points for: `.mcp.json` PATH assumption, `score`/`displayScore` discrepancy not self-documented, MCP library version appearing as server version in `serverInfo`. |

---

## Top 3 Issues to Fix

1. **`score_spec_file` must accept `str`** (or coerce in the function body): `Path(path)` in the first line fixes it. The README example, any copy-paste from docs, and any dynamic path construction breaks today.

2. **`.mcp.json` should use `.venv/bin/sf-blueprint-mcp`** (or document the absolute-path override more prominently) for project-local installs. The current config silently fails for any GUI MCP client that doesn't inherit the shell venv.

3. **`sf-blueprint run` needs a `--help` description** — blank subcommand description is a minor but immediate UX regression that makes the tool look unfinished on first `--help` scan.

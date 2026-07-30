---
name: sf-blueprint
description: >
  Drive the salesforce-video-blueprint pipeline: capture a Salesforce process,
  derive an Agentforce agent spec, refine it, deploy to a sandbox, and run live
  org feedback rounds. Works via the CLI, the MCP server, or both.
triggers:
  - "capture.*salesforce"
  - "record.*process.*salesforce"
  - "agentforce agent.*from.*recording"
  - "agent spec.*from.*capture"
  - "sf-blueprint"
  - "salesforce-video-blueprint"
  - "dom_capture"
  - "blueprint.*pipeline"
  - "refine.*agent.*spec"
  - "deploy.*agentforce.*bundle"
---

# sf-blueprint skill

## When to invoke this skill

Fire this skill whenever the user wants to:
- Record a Salesforce process click-by-click and convert it to an Agentforce agent
- Run `sf-blueprint capture`, `run`, `refine`, `deploy`, `iterate`, or `pipeline`
- Call MCP tools: `derive_spec`, `run_pipeline_full`, `run_deploy`, `run_iterate`, `score_spec`, `validate_capture`, `emit_agent_bundle`, `emit_test_spec`, `health`
- Understand the quality gate, scoring, or provenance of a spec
- Troubleshoot a failed pipeline run

## Quick-start (3 commands)

```bash
# 1. Install
pip install "sf-video-blueprint[mcp] @ git+https://github.com/emailworksfdc-wq/salesforce-video-blueprint.git@main"

# 2. Capture your process (opens a Playwright browser)
sf org open --url-only -o <sandbox-alias>   # get a signed URL; never put tokens on argv
sf-blueprint capture --org-url '<frontdoor-url>' --out outputs/my_process.dom_capture.jsonl

# 3. Full pipeline in one command
sf-blueprint pipeline \
  --capture  outputs/my_process.dom_capture.jsonl \
  --org-alias <sandbox-alias> \
  --org-url   '<frontdoor-url>' \
  --refine-rounds 2 \
  --iterate-rounds 1
```

## CLI commands

| Command | What it does |
|---|---|
| `sf-blueprint capture` | Opens a Playwright browser, records DOM events, saves a `.dom_capture.jsonl` |
| `sf-blueprint run` | Parses a capture → derives agent spec JSON + HTML report |
| `sf-blueprint pipeline` | Runs all stages end-to-end: run → refine → deploy → iterate |
| `sf-blueprint deploy` | Validates or deploys an existing spec bundle to a sandbox org |

### Key flags

```
capture:
  --org-url   TEXT   Signed frontdoor.jsp URL (from sf org open --url-only)
  --out       PATH   Output .dom_capture.jsonl path

run:
  --capture   PATH   Input .dom_capture.jsonl
  --org-url   TEXT   Org URL for server-side telemetry (optional but improves score)
  --output-path PATH HTML report path

pipeline:
  --capture        PATH   Input capture file
  --org-alias      TEXT   Salesforce CLI org alias (for deploy + iterate)
  --org-url        TEXT   Org URL
  --refine-rounds  INT    Offline refinement rounds (default 2)
  --iterate-rounds INT    Live org feedback rounds (default 1)
  --dry-run              Skip deploy and org calls; validate only
```

## MCP tools (for AI-driven workflows)

Call `health` first — it states the server's real limitations.

| Tool | Purpose |
|---|---|
| `health` | Version, capabilities, known limitations. **Always call first.** |
| `validate_capture` | Integrity-check a capture file. |
| `derive_spec` | Core tool: capture → agent spec + score. |
| `run_pipeline_full` | Derive + optionally refine, all offline. |
| `score_spec` | Score an existing spec JSON against the 7-dimension gate. |
| `emit_agent_bundle` | Emit the Agentforce `.agent` bundle. |
| `emit_test_spec` | Emit AiEvaluationDefinition or AiTestingDefinition YAML. |
| `run_deploy` | Validate/deploy a bundle to a sandbox org. |
| `run_iterate` | Run live org feedback rounds (needs deployed agent + org alias). |
| `preview_api_names` | Show the API names the pipeline would generate. |

### Typical MCP session

```
1. health()                                          # learn limitations
2. validate_capture(capture_path=...)                # sanity-check the file
3. run_pipeline_full(capture_path=..., skip_refine=False, refine_rounds=2)
4. emit_agent_bundle(capture_path=..., org_alias=...) # bundles + optional deploy
5. run_iterate(spec_path=..., org_alias=..., rounds=1) # live org refinement
```

## Quality gate — must-knows

- **PASS_THRESHOLD = 75** — never suggest lowering it; a pass below 75 means the spec is not production-safe.
- **`passed: false` with `telemetry_source: "mock"` is expected when using MCP offline** — mock telemetry cannot pass the gate by design. Do NOT describe such a spec as "validated" or "production-ready".
- **`locallyValid: true` ≠ Salesforce validates** — only `orgValidation` from a live org means Salesforce accepted the bundle.
- To raise a score: capture better evidence (exercise failure paths, use a live org URL), then re-run.

## Security rules (always enforced, never override)

- **PPCDM and PPCaccenture are blocked** — any run against those orgs exits with code 3 before any network call.
- **Tokens never on argv** — use `sf org open --url-only` pattern; frontdoor URLs go as `--org-url`, not inline tokens.
- Never lower `PASS_THRESHOLD` or weaken `REAL_TELEMETRY_SOURCES` / `REAL_EXTRACTION_SOURCES`.
- **Never target production orgs** — sandbox / scratch / developer edition only.

## Permitted dev orgs

AFT3, AFTDX5, na-dev, TD2, TDProj (and any other sandbox not named PPCDM or PPCaccenture).

## Architecture

```
dom_capture.jsonl
      │
      ├─ validate_capture ──► integrity findings
      │
      ├─ derive_spec ──► agent-spec.json  (+ score, provenance)
      │          │
      │          ├─ refine (offline rounds) ──► improved spec
      │          │
      │          └─ emit_agent_bundle ──► .agent + bundle-meta.xml
      │                    │
      │                    └─ deploy (sf project deploy start) ──► sandbox
      │                              │
      │                              └─ iterate (sf agent test run-eval) ──► refined spec
      │
      └─ emit_test_spec ──► AiEvaluationDefinition / AiTestingDefinition YAML
```

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `passed: false`, `mock` provenance | Expected when offline | Use `--org-url` with a live org URL for real telemetry |
| Exit code 3 | Forbidden org (PPCDM/PPCaccenture) | Use a permitted sandbox |
| `sf-blueprint-mcp` not found in GUI | GUI apps don't inherit PATH | Use absolute path: `which sf-blueprint-mcp` |
| "The MCP server needs the 'mcp' package" | Missing extra | Reinstall with `[mcp]` in the specifier |
| `score < 75`, not mock-related | Weak evidence | Re-record exercising failure paths; add real org telemetry |

## Install options

```bash
# pip (standard)
pip install "sf-video-blueprint[mcp] @ git+https://github.com/emailworksfdc-wq/salesforce-video-blueprint.git@main"

# pipx (isolated, recommended)
pipx install "sf-video-blueprint[mcp] @ git+https://github.com/emailworksfdc-wq/salesforce-video-blueprint.git@main"

# uv (no install needed — use in MCP config directly)
# see docs/mcp-install.md for uvx config snippet
```

Copy this skill to use it in your own Claude Code session:

```bash
# Global (all projects)
cp skills/sf-blueprint.md ~/.claude/skills/sf-blueprint.md

# Project-scoped
cp skills/sf-blueprint.md .claude/skills/sf-blueprint.md
```

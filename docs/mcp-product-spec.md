# MCP Product Specification

> **STATUS: PARTLY IMPLEMENTED — THE TOOL SET BELOW WAS NOT BUILT AS SPECIFIED.**
>
> An MCP server now exists: `src/sf_video_blueprint/mcp_server.py`, installed as
> `sf-blueprint-mcp` via the `[mcp]` extra. See
> [`mcp-install.md`](mcp-install.md) for how to install and use it.
>
> **It does not implement the `workflow.*` tool set specified below.** It exposes
> the pipeline's actual capabilities instead — see
> [What was built instead](#what-was-built-instead). Read the sections below as the
> original design intent, and check each one's own status note before citing it as
> a control.
>
> | Section | Status |
> | --- | --- |
> | Minimum Tool Set (`workflow.*`) | ❌ Not built — replaced, see below |
> | Contract Principles (response envelope) | ✅ Implemented |
> | Idempotency | ❌ Not built — not applicable, see below |
> | Error Taxonomy | 🟡 Partly — 4 of 10 codes are reachable |
> | Observability (structured logs) | ✅ Implemented |
> | Transport (HTTP/SSE, auth, rate limiting) | ❌ Not built — stdio only |

Package target: `@salesforce-video-blueprint/mcp-workflow` — **not used.** The
server ships inside this Python package rather than as a separate npm package,
because it calls the pipeline in-process; a separate package would need to shell
out to the CLI and re-parse its output.

## What was built instead

Seven tools, each mapping to a real pipeline capability:

| Tool | Purpose |
| --- | --- |
| `health` | Version, capabilities, and this project's real limitations |
| `validate_capture` | Integrity-check a capture without deriving anything |
| `derive_spec` | Capture → agent spec + score |
| `score_spec` | Score an existing spec JSON against the gate |
| `emit_agent_bundle` | Emit the `.agent` bundle + `.bundle-meta.xml` |
| `emit_test_spec` | Emit a `legacy` or `ngt` test spec |
| `preview_api_names` | Show the topic/subagent/router API names |

**Why the deviation.** The `workflow.*` shape models a long-running mutating job
service. This pipeline is none of those things: every operation is synchronous
(the slowest returns in well under a second), offline, and side-effect free. A
`workflow.cancel` for a call that has already returned would be decoration, and
`workflow.execute` with a free-text `workflowId` would hide seven concrete
capabilities behind a string parameter the model has to guess correctly.

The parts of this spec that carried real value — the envelope, the error
taxonomy, the structured logs — were implemented as written.

## Minimum Tool Set

> ❌ **Not built.** Superseded by the seven tools above.

- `workflow.plan`
- `workflow.execute`
- `workflow.status`
- `workflow.result`
- `workflow.cancel`
- `workflow.health`

## Contract Principles

> ✅ **Implemented**, with one substitution.

- JSON Schema 2020-12 for all tool input/output — **generated** from the tool
  signatures and docstrings by the MCP SDK, not hand-authored. Every tool exposes
  an input schema; `scripts/mcp_stdio_check.py` fails the build if one does not.
- Shared response envelope: `ok`, `requestId`, `serverVersion`, `error` (when not
  ok) — all present. **`timestamp` was replaced by `durationMs`.** A wall-clock
  stamp on a synchronous call tells a caller nothing it does not already know,
  whereas elapsed time is actionable. `requestId` correlates a response to its log
  line.
- Semantic versioning: `serverVersion` is read from the installed package
  metadata, so it cannot drift from `pyproject.toml`. No deprecation windows are
  defined yet — there are no external consumers to deprecate against.

## Idempotency

> ❌ **Not built — not applicable.**
>
> Idempotency keys exist to make a retried *mutating* call safe. No tool on this
> server mutates anything: they are all offline, read-only, and side-effect free
> apart from writing to an output path the caller explicitly names. Retrying any
> of them is already safe, so a key would add a required parameter and a replay
> cache to protect against a hazard that does not exist.
>
> Revisit if a tool ever deploys to an org or replays against a live org. Both are
> deliberately absent — see the security note in `mcp_server.py`.

Required for mutating tools:

- `idempotencyKey` required for execute/cancel
- identical key + identical request fingerprint => replay original response
- identical key + different fingerprint => conflict error

## Error Taxonomy

> 🟡 **Partly implemented.** Four codes are reachable; six are not, because an
> offline read-only server cannot produce them. Declaring constants for
> unreachable states would suggest a client should handle cases that never occur.

| Code | Status |
| --- | --- |
| `VALIDATION` | ✅ Bad arguments, or a capture that fails the integrity gate |
| `NOT_FOUND` | ✅ No file at the given path |
| `DEPENDENCY` | ✅ A filesystem write failed |
| `INTERNAL` | ✅ Unexpected pipeline failure |
| `AUTH`, `PERMISSION` | ❌ Nothing to authenticate against — no org contact |
| `CONFLICT` | ❌ No mutable state to conflict over |
| `RATE_LIMIT` | ❌ No remote dependency to protect |
| `TIMEOUT`, `UNAVAILABLE` | ❌ No network call that can hang or be down |

Categories:

- VALIDATION
- AUTH
- PERMISSION
- NOT_FOUND
- CONFLICT
- RATE_LIMIT
- DEPENDENCY
- INTERNAL
- TIMEOUT
- UNAVAILABLE

## Observability Contract

> ✅ **Structured logs implemented.** ❌ **Metrics and tracing not built.**
>
> Every tool call emits one JSON log line to **stderr** (stdout is the JSON-RPC
> transport; writing there corrupts the stream) carrying `requestId`, `tool`,
> `durationMs`, `outcome`, and `error.code` on failure. `runId` is omitted: it
> identifies a pipeline run, and a tool call maps to exactly one, so `requestId`
> already distinguishes them.
>
> Metrics and tracing are deliberately absent. A stdio server is a short-lived
> subprocess of the client with nowhere to export to, and there is no dependency
> graph to trace — no network calls, no org, no queue. Revisit if an HTTP
> transport is ever added.

Structured logs must include:

- `requestId`, `tool`, `runId`, `durationMs`, `outcome`, `error.code`

Metrics:

- request totals/latency by tool
- workflow run counts by state
- step duration
- idempotency replay count
- upstream dependency failures

Tracing:

- root span per tool call
- child spans per workflow step and dependency

## CI Quality Gates

> 🟡 **Partly implemented.** The `mcp-server` job in `.github/workflows/ci.yml`
> installs the `[mcp]` extra, asserts the console script landed on PATH, then runs
> `scripts/mcp_stdio_check.py` — which launches the installed executable and drives
> it over real stdio JSON-RPC. `tests/test_mcp_server.py` covers the tools directly.

| Gate | Status |
| --- | --- |
| Schema validation | ✅ Every tool must expose a description and input schema, or the build fails |
| Contract tests for success + each error class | ✅ Success, `VALIDATION`, and `NOT_FOUND` are covered end to end |
| Secret scanning | ✅ Repository-level secret scanning + push protection are enabled |
| Backward compatibility check | ❌ Not built — no external consumers yet |
| Idempotency replay/conflict tests | ❌ N/A — see [Idempotency](#idempotency) |
| Observability emission checks | 🟡 Only that nothing writes to stdout, which is the failure that breaks the transport |
| Load smoke | ❌ Not built — no concurrency or throughput claim is made |

One gate not in this spec but worth naming, because it protects the project's
core property: the stdio check asserts over the wire that a spec derived from
**mock** telemetry comes back `passed: false`. If a change ever lets that run
pass, CI fails. Making the gate weaker is a defect, not a fix.

- schema validation
- contract tests for success + each error class
- backward compatibility check
- idempotency replay and conflict tests
- security scan and secret scan
- observability emission checks
- load smoke for execute/status

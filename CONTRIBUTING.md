# Contributing

Thanks for looking at this. A few things about this project are unusual, so
please read the [Ground rules](#ground-rules) before opening a PR.

## Development setup

Requires **Python ≥ 3.11**. The code uses PEP 604 unions (`str | None`)
evaluated at runtime, so 3.9 — the macOS system `python3` — will fail at import.

```bash
git clone https://github.com/emailworksfdc-wq/salesforce-video-blueprint.git
cd salesforce-video-blueprint

python3 -m venv .venv
.venv/bin/pip install -e ".[dev,mcp]"
.venv/bin/python -m pytest -q
```

You should see `1309 passed, 2 skipped` (the count grows as fixes land). No
Salesforce org, network access, or credentials are needed — every test is
hermetic and offline.

Omit the `mcp` extra and you get `1269 passed, 3 skipped` instead: the MCP server
tests `importorskip` the optional dependency. **Keep it that way** — a plain
`pip install -e ".[dev]"` must not produce a red suite. CI runs both
configurations for exactly this reason.

Two skips are expected on a green run.
`test_real_e2e_artifacts_validate_against_new_schemas` validates artifacts from a
real pipeline run against the JSON schemas; point `SF_BLUEPRINT_E2E_DIR` at a
directory containing a `dom_capture.jsonl` and `blueprint.agent-spec.json` to
enable it. `test_c11_on_lane_02_real_capture_when_available` asserts the score gate
does not accuse its own builder of padding on a *real* recorded capture; it stays
skipped until such a capture is committed to `examples/` and survives ingest.
Both are skips by design, not gaps to paper over — if you make either pass by
weakening what it asserts, you have removed a check rather than satisfied it.

### Working on the MCP server

The unit tests call the tool functions directly. That proves the logic but not
that the server installs and speaks the protocol, so run the stdio check too:

```bash
.venv/bin/python scripts/mcp_stdio_check.py examples/case_triage.dom_capture.jsonl
```

It launches the installed `sf-blueprint-mcp` executable as a subprocess and drives
it over real JSON-RPC. It is what catches a broken entry point, a stray `print()`
on stdout (which corrupts the transport), or a non-serializable tool return.

## Ground rules

### 1. Never weaken a gate to make a number go up

`spec_score.py` (threshold 75) and `scripts/score_run.py` (threshold 85) exist to
make bad output *fail loudly*. Lowering a threshold, removing a dimension, or
loosening a placeholder pattern so a run passes is a **defect, not a fix** — and
will be rejected. If a spec should score higher, capture better evidence.

`tests/test_gaming_resistance.py` exists specifically to catch this. If your
change makes those tests fail, the change is wrong.

### 2. Never invent evidence

The core value of this pipeline is that the spec only claims what the recording
proved. If a run observed no data change, the spec must say so. Do not add
fallbacks that fabricate plausible entities, topics, actions, or failure paths to
fill a gap. An honest gap is the feature.

Concretely: do not emit `@apex.*` or `@flow.*` action references unless the
recording proves the action exists in the org. Referencing a non-existent action
produces a bundle that fails to deploy for reasons the operator cannot see.

### 3. Never log or persist a secret

- Tokens go in environment variables (`SF_ACCESS_TOKEN`), never in argv — argv is
  world-readable via `ps`.
- Never write a `frontdoor.jsp?sid=` URL to a report, log, or artifact.
- When a leak detector *finds* a secret, the finding must **not** echo the
  secret's value. Reports get shared; echoing defeats the check. See the comment
  at `dom_capture.py` in `validate_trace` for the canonical example.

### 4. Never target production

Replay re-executes recorded actions against a live org. Sandbox and scratch orgs
only. The production guard fails closed on purpose — do not "fix" it by making
an undeterminable org type default to allowed.

### 5. Keep documentation honest

Every doc is labelled **accurate** or **aspirational** in the README. If you add
a doc describing something not yet built, label it aspirational. If you implement
something a doc described, move it. A doc that overstates the code is a bug.

If you fix a defect listed in `docs/DEFECT_LEDGER.md`, update the ledger in the
same PR.

## Making a change

1. Branch from `main`: `git checkout -b fix/short-description`
2. **Write the failing test first.** This repo is test-heavy by design; a fix
   without a test that would have caught the bug is incomplete.
3. Keep the change focused. One concern per PR.
4. Run the full suite: `.venv/bin/python -m pytest -q`
5. Open a PR and fill in the template. CI must be green to merge.

### Commit messages

[Conventional Commits](https://www.conventionalcommits.org/):

```
feat(ingest): accept null role/name in RawRoleName
fix(score): stop double-counting deduped utterances
docs(readme): correct the score_run.py invocation
test(capture): cover BOM-prefixed capture files
ci: run pytest on 3.11 through 3.13
```

Scopes follow the module map in the README (`capture`, `ingest`, `extract`,
`replay`, `telemetry`, `correlate`, `spec`, `score`, `naming`, `agentforce`,
`eval`, `iterate`, `docs`, `ci`).

### Code style

Match the surrounding code. This codebase favours:

- Explicit `frozenset` / module-level constants over inline literals for any
  security-relevant list, so the rule is greppable and testable.
- Docstrings that explain *why* a constraint exists, not what the code does.
- Type hints everywhere; `from __future__ import annotations` at the top.
- Fail-closed defaults. When state is unknown, refuse rather than assume.

`ruff` runs in CI as an advisory check. It is not currently blocking, because the
existing code has not been linted end to end — please don't reformat unrelated
files in a functional PR.

## Reporting a defect

Open an issue with the bug-report template. The most useful reports include the
module and line, plus the smallest capture trace that reproduces it.

**Do not attach a real capture file.** Captures embed record IDs, field values,
and potentially PII from your org. Reduce to a synthetic minimum first — see
`examples/case_triage.dom_capture.jsonl` for the shape.

### Security issues

Do not open a public issue for a vulnerability that could expose org data or
credentials. Use GitHub's private vulnerability reporting on this repository
instead.

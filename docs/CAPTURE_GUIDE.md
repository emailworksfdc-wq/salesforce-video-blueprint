# Capture Guide — From Recording to a Passing Spec

This guide walks you through the full one-click flow: capture a Salesforce
process in your browser, run the pipeline to derive a spec, and iterate until
the spec passes the quality gate.

Prerequisites: Python 3.11+, Playwright installed, `sf` CLI authenticated
to a scratch or sandbox org, and the package installed:

```bash
git clone https://github.com/emailworksfdc-wq/salesforce-video-blueprint.git
cd salesforce-video-blueprint
python3 -m venv .venv
.venv/bin/pip install -e ".[dev,mcp]"
```

---

## Step 1 — Capture (`sf-blueprint capture`)

The capture step opens a headed browser, authenticates with your org via the
Salesforce frontdoor (no password, no MFA), and records every DOM event as you
work through a process. When you press Enter the recording stops and three files
are written.

```bash
python capture/inject.py \
  --org-alias <your-scratch-or-sandbox-alias> \
  --out-dir   outputs/capture/my_process \
  --note      "Case creation: New → Subject → Description → Status → Save"
```

What `inject.py` does:

1. Calls `sf org display --target-org <alias>` to verify the org is a sandbox,
   scratch org, or Developer Edition. It refuses production and both permanently
   blocked org aliases (`PPCDM`, `PPCaccenture`) before making any network call.
2. Calls `sf org open --url-only --target-org <alias>` to get a signed
   `frontdoor.jsp` URL. No password is typed into the browser form.
3. Launches a headed Chromium window, navigates to the frontdoor URL, and
   injects `capture/recorder.js` into every page and frame.
4. You perform the process manually. The recorder captures clicks, inputs,
   navigation, and field changes as JSON lines.
5. When you press Enter, the browser closes and the files are written.

**Output files:**

| File | Contents |
|------|----------|
| `dom_capture.jsonl` | One JSON line per DOM event (click, input, navigate, change) |
| `dom_capture.network.jsonl` | Network trace of Salesforce API calls (timestamps for correlation) |
| `dom_capture.manifest.json` | Capture metadata: event count, session id, recorder SHA256, org alias |

**What makes a usable capture:**

- Complete the process in a single recording session without navigating away or
  opening unrelated pages. The pipeline derives a single intent per recording;
  extraneous pages introduce noise.
- Finish every step you start. An abandoned half-completed form produces
  orphaned input events with no matching Save, which the extractor marks as
  ambiguous and may discard.
- Avoid rapid-fire repeated clicks on the same element. The recorder emits one
  event per interaction; a debounce check in the extractor coalesces runs of
  identical inputs, which is correct behaviour — not a bug.
- The `--note` flag is for your own reference. It is written to the manifest
  and appears in `validate_trace` output. Use it to describe the process in one
  sentence.

**Checking event count before proceeding:**

```bash
wc -l outputs/capture/my_process/dom_capture.jsonl
```

Compare the count to `examples/PROCESS_CATALOG.md`. If the count is far below
the expected range, the recorder may not have injected into all frames. If it
is far above, you may have captured multiple processes or navigated somewhere
unintended.

---

## Step 2 — Run (`sf-blueprint run`)

The run step reads the JSONL file, validates it, extracts actions, builds a
spec, and writes an HTML blueprint plus a machine-readable JSON spec.

```bash
sf-blueprint \
  --capture outputs/capture/my_process/dom_capture.jsonl \
  --org-url  "https://your-sandbox.sandbox.my.salesforce.com" \
  --output-path outputs/my_process.html
```

The CLI validates the capture before extraction:

- **`SECURITY CRITICAL:`** findings (redaction leaks) abort the run immediately.
  Fix the recorder and re-record. No spec is written.
- **`DATA LOSS:`** findings (≥50% of events skipped) abort the run. Check for
  recorder/parser version drift. The manifest shows how many events the recorder
  wrote; the parser shows how many it accepted.
- **`EVIDENCE INCOMPLETE:`** findings (10%–49% line loss) are non-blocking
  warnings. The spec is built from a partial recording and is stamped as real
  evidence — but incomplete evidence.

After a successful run, check the derived intent and confidence:

```
Derived intent: Create Case (confidence 0.73)
WARNING: this run contains SIMULATED data and is not audit evidence.
  Simulated: telemetry
```

The warning appears whenever `--mode mock` is active (the default). The
confidence figure reflects how much of the recording evidence supported the
derived intent. For a spec to pass the quality gate it needs live-org telemetry
— see Step 4.

**Scoring the spec:**

```bash
.venv/bin/python -c "
from sf_video_blueprint.spec_score import score_spec_file
r = score_spec_file('outputs/my_process.agent-spec.json')
print(f'{r.total}/100  band={r.band}  passed={r.passed}')
for k, v in r.dimensions.items():
    print(f'  {k:22} {v.score:>3}/{v.max_score}')
for b in r.blocking_issues:
    print('BLOCKED:', b)
"
```

A passing spec requires:
- Score ≥ 75/100
- No blocking issues (most commonly: mock telemetry, zero guardrails, or an
  unresolved intent)

A fresh single-process recording with clean selectors and no failure paths
typically scores 75–82 with mock telemetry, and is blocked regardless of the
number. See `docs/DEFECT_LEDGER.md` §D14 for the residual threshold-surfing
risk.

---

## Step 3 — Iterate (`refine`)

The `refine` function runs the pipeline through offline improvements to raise
the spec score without re-recording. Call it when:

- The score is in the 60–80 range and you suspect UNRESOLVED placeholders or
  duplicate steps are dragging it down.
- The spec has low specificity (generic verbs like "update" with no object).
- You want to understand why each change is happening.

```python
from sf_video_blueprint.iterate import refine
from sf_video_blueprint.pipeline import run_pipeline

result = run_pipeline(
    "outputs/capture/my_process/dom_capture.jsonl",
    org_url="https://your-sandbox.sandbox.my.salesforce.com",
)

refined = refine(
    result.spec,
    result.score,
    max_rounds=3,
    improvement_threshold=2,
)

print(f"Score: {refined.score.total}  converged={refined.converged}")
print(f"Rounds run: {refined.rounds_run}")
```

The offline loop does not contact an org. It applies deterministic
string-substitution improvements to UNRESOLVED placeholders, deduplicates
identical orchestration steps, and sharpens vague entity names. It stops when:

- The score improvement between two consecutive rounds is below
  `improvement_threshold` (default 2 points), OR
- `max_rounds` is reached

**What the offline loop cannot fix:**

- Mock-telemetry blocking issue — this requires a live-org capture (Step 4)
- Zero-testability score — this requires a failure-path recording
- Unresolved intent when no field or object name was captured at all — re-record
  with a more complete process

---

## Step 4 — Earn a passing spec (live-org telemetry)

A spec built from DOM capture alone is blocked from passing the quality gate
because `telemetry_source` is `"mock"`, not `"live-org"`. To earn a passing
spec:

```bash
export SF_ACCESS_TOKEN=$(sf org display --target-org <alias> --json \
  | python3 -c "import sys,json; print(json.load(sys.stdin)['result']['accessToken'])")

sf-blueprint \
  --capture   outputs/capture/my_process/dom_capture.jsonl \
  --org-url   "https://your-sandbox.sandbox.my.salesforce.com" \
  --mode      live \
  --access-token "$SF_ACCESS_TOKEN" \
  --output-path outputs/my_process_live.html
```

With `--mode live` the pipeline calls Salesforce REST APIs during the run to
collect field-change snapshots and record-creation confirmations. These are the
server-side signals that promote `evidence_grounding` from inference to the
data-delta tier and remove the blocking issue.

**Important constraints:**

- Only scratch orgs, sandboxes, and Developer Edition orgs are accepted. The
  org deny-list (`telemetry.py`, `replay_browser.py`, `org_denylist.py`) refuses
  any org whose alias, username, or instance URL matches the blocked patterns.
  `PPCDM` and `PPCaccenture` are permanently blocked and cannot be overridden.
- The access token is a session credential. Do not commit it, log it, or write
  it to the spec JSON. The CLI redacts it from all output.
- `--track-record ObjectApiName:RecordId` registers specific records for
  field-diff polling. Use it when the process touches a known record and you
  want the before/after field snapshot in the spec.

---

## Troubleshooting

**The capture file loses half its events.**

Check `dom_capture.manifest.json` for the `event_count` field and compare it
to the JSONL line count. A large gap means the browser tab reloaded mid-capture
(losing buffered events), or the recorder was not injected into all iframes.
Re-record with the `inject.py` driver, which re-injects the script on every
navigation event.

**`DATA LOSS` abort at runtime.**

The ingest schema has changed since the recording was made. Pull the latest
`recorder.js` and re-record. The manifest's `recorder_sha256` fingerprints which
version of the recorder wrote the file, so you can bisect if needed.

**`selector_confidence` is 0.35 for most events.**

The recording landed on tier-7 (CSS path) or tier-8 (XPath) selectors because
the pages had no ARIA labels, no `data-testid` attributes, and no visible text
on the clicked elements. This is common on custom LWC components and Field
Service Lightning pages. The spec will still be derived, but it will be fragile
at replay time. See `examples/README.md` for the full tier table.

**Intent is `UNRESOLVED:`.**

The extractor could not map the observed events to a known action type. Common
causes: all events were on a single element (no field changes captured), or the
process touched only navigation events with no inputs or clicks on labelled
controls. Refine the recording to include at least one field-level change event.

**Score is stuck at 75–79 after iterating.**

The offline loop has nothing left to improve. The remaining gap is usually:
`testability=0` (no failure path in the recording) or
`provenance_integrity=0/5` (mock telemetry). Testability requires a second
recording that intentionally triggers a validation error — for example, saving
a Case with no Subject to capture the required-field error message. Provenance
integrity requires live-org telemetry.

---

## What to record next

See `examples/PROCESS_CATALOG.md` for a curated list of ten Salesforce
processes, with expected event counts, the objects they touch, and what the
pipeline can derive from each.

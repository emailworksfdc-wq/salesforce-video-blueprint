# Step 5: DOM Capture — Operator Runbook

## Status: implemented but not verified against a live Salesforce org

This document describes **what the code does**, not what it might do. Read the limitations section before recording anything you plan to rely on.

## What this replaces and why

The original `HeuristicVideoExtractor` never decoded video. It checked that the file existed and returned one hardcoded step (`button:Save`) regardless of the video's actual content. A 16-byte text file named `sample_video.mp4` would "succeed" with the same placeholder output as a real 10-minute screen recording.

**The problem CV-on-video never solves:** Computer vision on video pixels yields pixel coordinates (`click at (427, 618)`). Replay needs selectors (`button[aria-label='Save']`). Nothing bridges the two reliably — pixel coordinates break when the window resizes, the theme changes, or Salesforce deploys a layout update.

**DOM capture is the right answer:** A JavaScript probe injected into the live page captures clicks, inputs, and navigation **at selector level** while you perform a real business process. The capture includes:
- All selector strategies (testid, ARIA, Salesforce field API names, CSS paths, XPath)
- Shadow DOM traversal (Lightning Web Components hide the real target behind shadow boundaries)
- Redaction of passwords, tokens, SSNs, and credit cards **before writing to disk**
- Network trace of Salesforce API calls for correlation with backend telemetry

The output is `dom_capture.jsonl` — one JSON line per interaction — which the pipeline can replay deterministically and correlate with backend telemetry (Apex logs, Flow interviews, validation rules).

## Prerequisites

1. **Python ≥ 3.11**. The system `python3` on macOS is 3.9 and will fail with `SyntaxError: invalid syntax` on PEP 604 type unions. Use the project virtualenv:
   ```bash
   ./.venv/bin/python --version  # Must be 3.11+
   ```
2. **The project virtualenv** installed:
   ```bash
   uv venv --python 3.13 .venv
   uv pip install --python .venv/bin/python -e ".[dev]"
   ```
3. **Playwright + chromium** installed:
   ```bash
   ./.venv/bin/python -m playwright install chromium
   ```
4. **Salesforce CLI** authenticated to a dev or sandbox org:
   ```bash
   sf org list
   ```

## SAFETY RULES (READ FIRST)

### 1. Dev/scratch/sandbox orgs ONLY — never production

The driver re-executes recorded actions on every replay. A recorded "Create Case" writes a new Case every time the pipeline runs. There is no idempotency guard, no cleanup, and **no undo**.

The driver includes a fail-closed safety check (`assert_org_is_safe` in `capture/inject.py`) that refuses to proceed if:
- `isSandbox=false` AND `isScratch=false` AND the instance URL does not contain `.develop.`, `.sandbox.`, or `.scratch.`
- The username is `@salesforce.com` without a `.sandbox`, `.scratch`, or `.dev` suffix
- The org alias is `PPCDM` or `PPCaccenture` (permanently out of scope per project rules)

**This guard has no override flag.** If it refuses your org, the org is not safe for replay. Do not edit the guard to bypass it — that is a policy violation, not a technical blocker.

### 2. Authentication: frontdoor URLs, not login automation

The Salesforce login form is **never** automated. Instead, `sf org open --url-only` returns a signed `frontdoor.jsp` URL that bypasses MFA and SSO legitimately. The driver navigates to that URL, the org recognizes the signature, and the session is authenticated.

This is how Salesforce's own MCP server and browser plugins handle authentication. It is not a workaround; it is the documented pattern.

### 3. Permanently blocked orgs

`PPCDM` and `PPCaccenture` are out of scope for this project, even for read-only operations. The guard will refuse them by alias. If you need to work with these orgs, use a different tool.

## Recording a process (step by step)

### 1. Choose a dev or sandbox org

```bash
sf org list
```

Pick an org alias from the list. Confirm it shows `Sandbox: true` or `Scratch Org: true`. Never use a production org.

### 2. Run the capture driver

```bash
./.venv/bin/python -m capture.inject \
  --org-alias your-sandbox-alias \
  --out-dir ./outputs/capture \
  --start-url "https://your-org.lightning.force.com/lightning/o/Case/list" \
  --note "Create a Case with Status=Working, record a comment, mark Closed"
```

**Flags** (confirmed from `capture/inject.py` lines 165–178):
- `--org-alias` (required): Salesforce org alias or username
- `--out-dir` (optional, default `./outputs/capture`): Output directory for JSONL and manifest
- `--start-url` (optional): URL to navigate to after authentication (defaults to org home)
- `--note` (optional): Operator description of the process being recorded (appears in manifest)

### 3. What you'll see

The driver:
1. Resolves the org metadata via `sf org display --json` and runs the safety check
2. Obtains a signed frontdoor URL via `sf org open --url-only`
3. Launches a **headed** Chromium browser window (you will see the browser appear)
4. Navigates to the frontdoor URL (authentication happens silently)
5. If you provided `--start-url`, navigates there next
6. Displays this message in the terminal:

   ```
   ======================================================================
   Recording started. Perform your process in the browser.
   When done:
     1. Navigate to your starting point.
     2. Perform your process.
     3. Return here and press Enter to stop.
   ======================================================================
   ```

### 4. Perform your process in the browser

- **Do not close the terminal.** The driver is waiting for you to press Enter.
- **Do not multitask in other tabs.** Open new tabs if needed, but perform only ONE business process in the recording session. Multiple unrelated processes in one capture will confuse the spec builder.
- **Pause briefly between logical steps** (e.g., after clicking Save, wait for the toast message to appear before navigating away). The noise reducer drops scroll and rapid-fire clicks that look like event bubbling; deliberate pacing ensures meaningful steps are captured.
- **Complete the process.** A recording that stops before clicking Save yields no data delta, so `spec_builder` cannot derive an intent and will honestly report "incomplete evidence."

### 5. Stop the recording

When your process is complete, return to the terminal and **press Enter**. The driver will:
- Stop event collection
- Write three files:
  - `dom_capture.jsonl` (one JSON line per DOM event)
  - `dom_capture.network.jsonl` (Salesforce API calls)
  - `dom_capture.manifest.json` (capture metadata)
- Print a summary:
  ```
  [inject] ✓ Recording stopped.
  [inject]   Duration: 47.2s
  [inject]   Events: 142
  [inject]   Network events: 23
  [inject]   Sink errors: 0
  [inject]   JSONL: ./outputs/capture/dom_capture.jsonl
  [inject]   Network JSONL: ./outputs/capture/dom_capture.network.jsonl
  [inject]   Manifest: ./outputs/capture/dom_capture.manifest.json
  ```

**If you close the browser instead:** The driver detects this and stops recording immediately. Check the summary to confirm events were captured.

## How to record WELL (highest-value section)

Capture quality caps everything downstream. A brittle capture with weak selectors produces a brittle spec that fails on the first Salesforce layout change.

### 1. Start from a known state

Do not start mid-session with cached data or partial state. Best practice:
- Log out and log back in (via frontdoor, not the login form)
- Navigate to the app home or list view where the process naturally starts
- Clear any filters or search terms that might affect the process

### 2. One complete business process per recording

**Good:** "Create a Case with Status=Working, add a comment, mark Closed."

**Bad:** "Create a Case, then check my email, then update an Account, then come back and close the Case."

Multiple unrelated processes in one capture will produce a spec with ambiguous intent. The spec builder correlates UI steps with backend telemetry (Apex logs, Flow interviews), but if three unrelated processes ran in the same 60-second window, the correlation is ambiguous.

### 3. Record a FAILING variant too

Record the process **successfully completing**, then record it **failing** (e.g., trigger a validation error by leaving a required field blank, or violate a field constraint).

**Why:** `spec_builder` reports what it observed. If you only record the success path, the derived spec will honestly say "Error paths: UNTESTED" and the quality gate will flag it as incomplete. A spec that cannot describe error handling is not production-ready.

### 4. Use `--track-record` on the pipeline run

After capturing, run the pipeline with `--track-record <Object>:<RecordId>` so field deltas are captured:

```bash
./.venv/bin/python -m sf_video_blueprint.cli ./outputs/capture/dom_capture.jsonl \
  --org-url "https://your-org.my.salesforce.com" \
  --mode live \
  --track-record Case:500xx0000012345AAA
```

**What this does:** The pipeline queries the record before and after each step and diffs the fields. This is what upgrades a spec from "Interact with Case Status" (weak, no causal claim) to "Update Case (Status: New → Working)" (strong, field-level evidence).

Without `--track-record`, the spec builder can only infer intent from UI labels and telemetry. With it, the spec includes per-field evidence.

### 5. Do not multitask in other tabs

The recorder captures events from **all pages in the browser context**, not just the Salesforce tab. If you switch to another tab mid-recording, those events will be captured too and will appear as noise.

If you need a second tab (e.g., to reference documentation), open it before starting the recording, then ignore it during the process.

## What comes out

Three files:

| File | Purpose | Git status |
| --- | --- | --- |
| `dom_capture.jsonl` | Raw DOM events (clicks, inputs, navigation), one JSON line per event | `.gitignore`d |
| `dom_capture.network.jsonl` | Salesforce API calls (timestamps, URLs, status codes) | `.gitignore`d |
| `dom_capture.manifest.json` | Capture metadata (org alias, duration, event count, recorder SHA256) | `.gitignore`d |

**These files contain real org data** — record IDs, field values, URLs with record IDs in the path, network payloads. Treat them as sensitive. They are gitignored by default; keep them that way.

### Example: one line from `dom_capture.jsonl`

```json
{
  "v": 1,
  "seq": 12,
  "t": 1737830000123,
  "type": "click",
  "url": "https://your-org.lightning.force.com/lightning/r/Case/500xx0000012345AAA/view",
  "frame_path": [],
  "selectors": {
    "test_id": null,
    "aria": "button[aria-label='Save']",
    "role_name": {"role": "button", "name": "Save"},
    "label_for": null,
    "sf_field": null,
    "css_path": "div.slds-form > button.slds-button_brand",
    "text": "Save",
    "xpath": "/html/body/div[1]/..."
  },
  "element": {
    "tag": "button",
    "type": null,
    "name": null,
    "id": null,
    "classes": ["slds-button", "slds-button_brand"],
    "aria_label": "Save",
    "text": "Save",
    "is_in_modal": false,
    "modal_label": null,
    "shadow_depth": 2
  },
  "value": null,
  "value_redacted": false,
  "sf": {
    "object": "Case",
    "record_id": "500xx0000012345AAA",
    "page_type": "record_home",
    "app": null
  }
}
```

**Key fields:**
- `selectors`: All selector strategies the recorder could compute. `dom_extractor.py` ranks these by stability (ARIA > Salesforce field API name > CSS path > XPath) and emits the best one as the primary selector.
- `shadow_depth`: Number of shadow boundaries traversed from the clicked element to the document root. Lightning Web Components typically have `shadow_depth >= 1`.
- `sf`: Salesforce context parsed from the URL (object type, record ID, page type). App name is **not derivable from the URL alone** and will be `null`.
- `value_redacted`: If `true`, the `value` field was scrubbed (password, token, SSN, credit card) and was **never written to disk**, not even temporarily.

## Redaction: what is and is not protected

### What is redacted at capture time (before touching disk)

The recorder (see `capture/recorder.js`, function `maybeRedactValue`) checks **every** input/change event against these patterns:

- `input[type=password]` — always redacted
- Field names matching: `password`, `secret`, `token`, `ssn`, `social security`, `credit card`, `cvv`, `cvc`, `pin`, `api key`, `routing`, `account number`
- Values matching:
  - Credit card patterns (13-19 digits, optional separators) — Luhn-validated to avoid false positives
  - SSN patterns (`###-##-####` with sanity checks)

When a match is found, the event is written with `value: null` and `value_redacted: true`. The sensitive value **never exists in the JSONL file**, not even temporarily.

This is implemented in `capture/recorder.js` lines 66–97 and `src/sf_video_blueprint/redaction.py` (the Python-side redaction layer).

### What is NOT redacted

- **Record IDs** (`500xx0000012345AAA`) — captured verbatim. The redaction module (`redaction.py`) includes a Salesforce ID detector with checksum validation, but it is **not enabled by default** in the DOM capture flow. If you need to scrub record IDs, pass a `RedactionPolicy` with `redact_record_ids=True` when processing the JSONL. (This is a known gap; the pipeline does not apply policy-based redaction to raw capture yet.)
- **Field values** (Status, Priority, Subject, Description) — captured verbatim unless they match a secret pattern.
- **Names, emails, phone numbers** — captured verbatim. The redaction module can detect these (see `redaction.py` lines 61–76), but they are not redacted by default because false positives are annoying (e.g., "Phone" as a field label vs. an actual phone number).
- **URLs** — captured verbatim, including record IDs and query parameters.

**The takeaway:** An operator who assumes full anonymization will leak data. The default redaction is **secrets only** (passwords, tokens, cards, SSNs). Business data (record IDs, field values, names) is captured as-is.

If you need stricter redaction, see `src/sf_video_blueprint/redaction.py` and configure a `RedactionPolicy` before processing the JSONL.

## Feeding the capture into the pipeline

### Command

```bash
./.venv/bin/python -m sf_video_blueprint.cli ./outputs/capture/dom_capture.jsonl \
  --org-url "https://your-org.my.salesforce.com" \
  --mode live \
  --track-record Case:500xx0000012345AAA
```

**What changes:**
- The `--video-path` argument now accepts a JSONL file, not a video.
- Provenance in the output HTML flips from `stub` to `dom-capture`, so the banner changes from red ("SIMULATED") to amber ("from DOM capture, not verified against live Salesforce").
- The quality gate no longer flags extraction as simulated (the `evidence_is_real_ok` check passes).

**Telemetry is a separate axis.** A DOM capture with `--mode mock` (the default) still uses mock telemetry, so the report will still say "Telemetry: SIMULATED" and the quality gate will refuse it. Use `--mode live` and set `SF_ACCESS_TOKEN` to get real telemetry.

### Example output structure

```
outputs/
├── master_blueprint.html          # Human-readable report
├── master_blueprint.agent-spec.json   # Derived agent spec (machine-readable)
├── capture/
│   ├── dom_capture.jsonl
│   ├── dom_capture.network.jsonl
│   └── dom_capture.manifest.json
└── replay_manifest.json           # NOT YET EMITTED (schema exists, no writer)
```

## Reading the noise-reduction warnings

The pipeline prints a line like this:

```
noise reduction: 142 raw events -> 18 actions
  (coalesced 47 input, dropped 63 bubbling, 12 scroll, 2 keydown, synthesized 0 navigate)
```

**What this means:**

| Metric | What was dropped | Why |
| --- | --- | --- |
| `coalesced N input` | Consecutive input/change events on the same element (typing "Working" emits 7 input events; noise reducer keeps only the final value) | A raw trace of typing one word is 10–20 events; the meaningful step is "set Status to Working" |
| `dropped N bubbling` | Click events on non-interactive containers followed within 150ms by a click on a descendant interactive element (event bubbling duplicates) | Lightning stops propagation everywhere, but when it doesn't, a single click can emit 2–3 events up the DOM tree |
| `dropped N scroll` | Scroll events not immediately followed by interaction with a different element | Scroll is only meaningful when it enables reaching an off-screen element; otherwise it's mouse-wheel noise |
| `dropped N keydown` | Keydown events that are not modifier combos (Ctrl+S, Cmd+K) | Input events already capture the final value; keydown is redundant unless it's a hotkey |
| `synthesized N navigate` | URL changes between consecutive events (emitted as synthetic `NAVIGATE` actions) | The recorder does not emit `navigate` events automatically (it would double-count when a click causes navigation); the reducer inserts them when it sees a URL change |

**A good capture:**
- Dozens of actions (10–50 for a typical process)
- High-confidence selectors (tier 1–4: testid, ARIA, label-for, SF field API name)
- Low or zero `dropped_bubbling` (means the process was deliberate, not frantic clicking)

**A bad capture:**
- Single-digit actions (incomplete process, or everything was noise)
- All selectors at tier 7 (CSS path) or tier 8 (XPath) = brittle replay
- High `dropped_bubbling` (rapid-fire clicking, or the app emits duplicate events everywhere)

The noise reducer is implemented in `src/sf_video_blueprint/dom_extractor.py` lines 484–682. Every reduction is auditable: the `ReductionReport` is emitted to `ActionExtractionBundle.warnings` so you can see exactly what was dropped.

## Troubleshooting

### Zero events captured

**Symptom:** Manifest shows `event_count: 0`.

**Causes:**
1. The recorder script was not injected. Check the terminal output for `[inject] ✓ Authenticated.` — if you don't see that, the browser never loaded.
2. The sink binding failed. The recorder emits events to `window.__sfCaptureSink` (exposed by Playwright via `context.expose_binding`). If the binding does not exist, events are buffered in `eventBuffer` but never written. This is a recorder bug if it happens (report it).
3. You did not interact with the page. The recorder only captures events you trigger; if you navigated to the page and immediately pressed Enter without clicking anything, zero events is correct.

**Fix:**
- Check the browser console (`F12` → Console tab) for JavaScript errors.
- Check that `window.__sfCapture` is defined (type `window.__sfCapture` in the console; it should return an object with `start`, `stop`, `drain` methods).
- If the console shows `SecurityError` or `cross-origin` errors, the page might have blocked the injected script. This is rare in Salesforce orgs but can happen in Experience Cloud sites with strict CSP.

### Events with only low-tier selectors (confidence 0.35)

**Symptom:** The noise-reduction summary shows 20 actions, but the HTML report flags every step as "low confidence" (red badge). The action table shows selectors like `div.container > div > button` (CSS path) or `/html/body/div[1]/...` (XPath).

**Cause:** The recorder could not compute stable selectors. This happens when:
- The element has no `data-testid`, no ARIA label, no Salesforce field API name, and no visible text.
- The element is dynamically generated with no stable attributes (e.g., a generic `<div>` container with a `click` listener).

**What this means for replay:** CSS paths and XPath are brittle. They break when:
- The layout changes (e.g., a new element is inserted above the target, shifting the XPath index)
- The CSS classes change (Salesforce renames SLDS classes between releases)
- The shadow DOM structure changes (Lightning components get refactored)

**Fix:**
- **Advocate for testid attributes.** If you control the component, add `data-testid='save-button'` to the markup. This is the tier-1 selector (confidence 0.95) and survives layout changes.
- **Use visible text.** If the element has stable text content, the recorder will emit it as tier-6 (`text: "Save"`), which is better than CSS path but worse than ARIA.
- **File a bug with the Salesforce team.** If a core Lightning component (e.g., `lightning-button`) has no ARIA label and no stable selector, that is an accessibility bug. Salesforce components are supposed to expose `aria-label` or `role` + accessible name.

### Cross-origin iframes

**Symptom:** The action list includes steps like `iframe[cross-origin]` with no further detail.

**Cause:** The recorder runs per-frame. When an iframe is cross-origin, the browser's same-origin policy prevents the parent frame's script from accessing the iframe's content. The recorder can detect that an iframe exists, but it cannot inject itself into the iframe or read its DOM.

**What this means:** Events inside the cross-origin iframe are not captured. The recording will have a gap.

**Fix:**
- If you control the iframe, serve it from the same origin as the parent, or enable CORS headers that allow script injection.
- If you do not control the iframe (e.g., a third-party payment widget), you cannot capture events inside it. The recording will end at the iframe boundary. Consider this a limitation of DOM capture (browser extensions and screen recorders have the same issue).

### Persistent-profile lock error

**Symptom:**
```
Error: Cannot start the browser: the user-data-dir at /Users/you/.sf-video-blueprint/browser-profiles/your-org is in use by another Chromium process
```

**Cause:** The driver uses `launch_persistent_context` with an org-specific `--user-data-dir` so cookies survive between recordings. A persistent profile can only be used by one browser instance at a time.

**Fix:**
- **Sequential recordings:** Wait for the first recording to finish before starting a second one.
- **Parallel recordings:** Use different org aliases (each gets its own profile directory), OR pass `--isolated` (not implemented yet; see `capture/inject.py` line 238 comment) to use an in-memory profile.
- **Never combine `--isolated` with `--user-data-dir`.** They contradict each other. `--isolated` uses an ephemeral in-memory profile; `--user-data-dir` uses a persistent on-disk profile. Combining them is a Playwright error.

### Org safety guard refuses a sandbox org

**Symptom:**
```
ValueError: Org 'my-sandbox' is neither a sandbox nor a scratch org, and the instance URL 'https://my-sandbox.my.salesforce.com' does not contain a dev/sandbox/scratch marker. Refusing to proceed (production safety).
```

**Cause:** The org metadata from `sf org display` reports `isSandbox: false` and `isScratch: false`, AND the instance URL does not contain `.develop.`, `.sandbox.`, or `.scratch.`.

**Why this happens:**
- The org is actually production.
- The org is a sandbox, but the Salesforce CLI metadata is stale or incorrect.
- The org is a sandbox, but it uses a custom domain that does not include the word "sandbox."

**Fix:**
- Run `sf org display --target-org your-alias` and check the output. If `Sandbox: false`, the org is production and you must use a different org.
- If you are certain the org is a sandbox (you created it as a sandbox, it shows up in Setup → Sandboxes), file a bug with the Salesforce CLI team. The metadata should report `isSandbox: true`.
- **Do not edit the guard in `capture/inject.py` to bypass this check.** The guard is fail-closed by design. If it refuses your org, the org is not safe for replay.

## Known limitations (DO NOT SOFTEN THESE)

### 1. Not verified against a live Salesforce org

The driver code (`capture/inject.py` and `capture/recorder.js`) was written from Salesforce documentation and Playwright API docs. It has **not been run against a real Salesforce org yet**. The following assumptions are unverified:

- **Lightning attribute assumptions:** The recorder assumes that Salesforce fields expose `data-field-api-name` or `data-name` attributes, and that Lightning components follow the SLDS class naming conventions. These assumptions are based on Salesforce developer docs, not observed in a live browser.
- **Shadow DOM selector translation:** The recorder emits CSS paths with ` >>> ` separators to pierce shadow boundaries. Playwright's `locator()` API is documented to support this, but it has not been tested against a live Lightning component.
- **Modal detection:** The recorder assumes modals have `role='dialog'`, `.slds-modal`, or `.uiModal` classes. Classic Salesforce and Experience Cloud may use different markup.

### 2. App name is not derivable from the URL

The Salesforce app name (e.g., "Service Console") does not appear in the URL. It might be in the DOM (in a nav element with a class like `.appName`), but the recorder does not scrape it yet. The `sf.app` field in the JSONL will be `null`.

**Implication:** The derived spec cannot include app-scoped instructions like "In the Service Console, navigate to Cases." It can only say "Navigate to /lightning/o/Case/list."

### 3. Correlation between UI steps and telemetry is not causally proven

The pipeline correlates UI steps with telemetry (Apex logs, Flow interviews) by timestamp and `step_id`. It proves telemetry was **fetched during** a step, not **caused by** it.

**Example:** You click Save at timestamp 1000ms. An Apex trigger fires at timestamp 1050ms. The pipeline says "Step 3 (Save) → ApexLog[trigger_id]" because the log entry was created within the step's time window. But if a scheduled job also ran at 1050ms, the pipeline cannot distinguish which one the step caused.

Causal proof requires the Apex log to include the user action ID or a request ID that ties back to the UI event. Salesforce does not expose this in the REST API today.

### 4. Experience Cloud / Community pages are untested

The recorder was designed for Lightning Experience (core Salesforce UI). Experience Cloud sites use different markup, different class names, and sometimes different authentication flows. The recorder might work, or it might not.

If you need to capture an Experience Cloud process, run a small test first (record 3 clicks, inspect the JSONL, confirm selectors are tier 1–4). Do not assume it works.

### 5. Classic Salesforce is out of scope

Classic Salesforce (the pre-Lightning UI) uses iframes, non-semantic markup, and inline event handlers. The recorder will capture events, but the selectors will all be tier 7–8 (CSS path, XPath), which means brittle replay.

If you need to automate Classic, use a different tool (e.g., Selenium IDE, or Salesforce's own UI Automation Recorder).

## Further reading

- **Contract:** `docs/INTERFACE_CONTRACT.md` section 2 (DOM capture wire format, selector ranking, noise reduction)
- **Recorder implementation:** `capture/recorder.js` (shadow DOM, redaction, selector computation)
- **Driver implementation:** `capture/inject.py` (Playwright, frontdoor auth, org safety guard)
- **Noise reducer:** `src/sf_video_blueprint/dom_extractor.py` (coalescing, bubbling detection, action mapping)
- **Redaction layer:** `src/sf_video_blueprint/redaction.py` (secret detection, Luhn check, Salesforce ID checksum)
- **Replay patterns:** `docs/replay-hardening.md` (selector translation, retry/backoff, Salesforce-specific wait conditions)

---

**Version:** DOM capture driver v1 (2026-07-25)  
**Owner:** Agent A10 (orchestrator: Step 5 runbook author)  
**Status:** Implemented, not verified against live Salesforce org

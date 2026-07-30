# Examples

## `case_triage.dom_capture.jsonl`

**Synthetic. Not from a real org.** Every record ID, field value, org URL, and
timestamp in this file is fabricated for documentation purposes.

An 8-event capture of a plausible Case triage process:

| # | Event | What it represents |
| --- | --- | --- |
| 1 | `click` **New** | Open the new-Case modal from a list view |
| 2 | `input` **Subject** | `"Printer offline on floor 3"` |
| 3 | `input` **Description** | Free-text detail |
| 4 | `change` **Priority** | `High` |
| 5 | `click` **Save** | Commit the new Case |
| 6 | `navigate` | Land on the created record page |
| 7 | `change` **Status** | `Working` |
| 8 | `click` **Save** | Commit the status change |

Run the pipeline on it with no org, no network, and no credentials:

```bash
.venv/bin/python -m sf_video_blueprint.cli \
  --capture examples/case_triage.dom_capture.jsonl \
  --org-url "https://example-dev.develop.my.salesforce.com" \
  --output-path outputs/case_triage.html
```

The derived intent is `Update Case (Status)` at confidence 0.70, and the spec
scores **79/100 with `passed=False`** — blocked because telemetry was mock rather
than from a live org. That refusal is the intended behaviour, not a failure; see
the Quick start section of the top-level README.

The companion `case_triage.dom_capture.manifest.json` shows the manifest shape
the recorder writes alongside a real capture. Note that
`parse_capture_file` does not currently load it — see `docs/DEFECT_LEDGER.md`.

## `case_creation_aft3.dom_capture.jsonl`

**Real. Recorded in a Developer Edition org on 2026-07-26, then redacted.** This
is the first real Salesforce recording this project has ever had. 175 events from
an actual Case creation: click New → Subject → Description → Status → Priority →
Case Origin → Save.

Redacted: org host, org id, username, and record ids are replaced with fixed-width
placeholders. Preserved byte-for-byte: event order, `_ingest_seq`, shadow depths,
LWC tag names, and every null selector. The redaction is enforced by
`tests/test_real_capture_aft3.py::test_real_capture_contains_no_secrets`.

**It does not parse.** 171 of its 175 events (98%) are rejected by
`dom_capture.py`, so `run_pipeline` raises `CaptureRejected` and produces no spec
at all:

```text
DATA LOSS: 171 of 175 lines were skipped (98%). More than half the capture was
discarded. Check for recorder/parser version drift or schema mismatch.
```

Every rejection has the same root cause: `recorder.js` emits
`role_name={"role": null, "name": null}` for LWC custom elements that have no
implicit ARIA role, but `RawRoleName` declares both fields as non-nullable `str`.
The synthetic example above never exercises that shape, which is why the ingest
path looked healthy. See `_shared/findings/lane-02.md` for the full defect list.

This file is kept deliberately unfixed. It is the regression fixture that proves
the ingest path is not yet real-DOM-capable.

## What is `selector_confidence`, and why does it matter?

Every DOM event in a capture carries one or more **selectors** — CSS paths,
XPath strings, ARIA role+name pairs, Salesforce field names, and so on.
`selector_confidence` is the float between 0.0 and 1.0 that the pipeline
assigns to the **best** selector it found for each event, based on how stable
and replay-safe that selector is expected to be.

The mapping (`selectors.confidence_for_tier`) is:

| Tier | Selector type | Confidence | Meaning |
|------|---------------|------------|---------|
| 1 | `data-testid` / `data-qa` attribute | 0.95 | Deliberate test contract; stable across deployments |
| 2 | ARIA role + accessible name | 0.95 | Semantic identity; survives UI reskins |
| 3 | `aria-label` / `aria-labelledby` | 0.85 | Stable in well-labelled UIs; breaks when labels change |
| 4 | `<label for="...">` relationship | 0.85 | Reliable on standard record forms |
| 5 | Salesforce `sf_field` attribute | 0.80 | Org-schema-stable; survives Lightning reskins |
| 6 | Visible `text` content | 0.60 | Breaks on translation or label edits |
| 7 | CSS path | 0.35 | Brittle; auto-generated Lightning IDs penalised further |
| 8 | XPath | 0.35 | Most brittle; fallback of last resort |

**What this means in practice:**

- `selector_confidence` is **not** the same as intent confidence. A recording
  where every click lands on a well-labelled ARIA button (`confidence=0.95`) can
  still produce a low-intent-confidence spec if the captured events are
  ambiguous about what the process was doing.

- The `selector_confidence` of each action flows into `spec.confidence` via
  `DomCaptureExtractor`. A session dominated by tier-7 CSS selectors will have
  a lower aggregate confidence ceiling than one built on tier-1 test IDs, but
  the intent-derivation logic may still produce a confident reading.

- **For replay**: tier 7-8 selectors are brittle. If your recording has many
  tier-7 CSS selectors, replaying it against a different org sandbox (or after
  a Spring/Summer release) is likely to fail with "element not found". Prefer
  recordings where the ARIA role+name or `sf_field` attributes are populated.

- **For diagnosis**: run `selectors.explain(ranked_selectors)` to see the full
  ranked list and the rationale for each candidate. The `selector_health()`
  function summarises the list as `strong`, `moderate`, or `weak`.

See `examples/PROCESS_CATALOG.md` for a per-process breakdown of which
selector tiers to expect for each Salesforce process type.

---

## Process catalog

Not sure what to record, or whether your capture event count is in the right
range? See **[examples/PROCESS_CATALOG.md](PROCESS_CATALOG.md)** for ten
canonical Salesforce processes with expected event counts, objects touched, and
a description of what the pipeline can derive from each. The Case creation
process already has an AFT3 capture annotated there.

---

## Adding an example

Two rules:

1. **Synthetic, or real-and-redacted — never real-and-raw.** Captures embed
   record IDs and field values verbatim. For a synthetic example, fabricate IDs in
   the obvious `500XX000000001` style and use
   `example-dev.develop.my.salesforce.com` for URLs so nobody mistakes them for
   real. For a redacted real capture, strip org host, org id, username, session
   ids and record ids, add a secret-scan test like the one guarding
   `case_creation_aft3`, and label the file as real in this README so no reader
   assumes its DOM shape was invented.
2. **It must survive validation — or be labelled as a failing fixture.**
   `dom_capture.py` treats the recorder as untrusted and rejects malformed events.
   Run the pipeline on your example before committing it. If the parser drops
   events, either fix the capture or, as with `case_creation_aft3`, document the
   loss and pin it in a test — do not commit a lossy capture silently.

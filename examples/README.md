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

## Adding an example

Two rules:

1. **Synthetic only.** Never commit a capture taken from a real org. Captures
   embed record IDs and field values verbatim. Fabricate IDs in the obvious
   `500XX000000001` style and use `example-dev.develop.my.salesforce.com` for
   URLs so nobody mistakes them for real.
2. **It must survive validation.** `dom_capture.py` treats the recorder as
   untrusted and rejects malformed events. Run the pipeline on your example
   before committing it — if the parser drops events, the run aborts with a
   `DATA LOSS` error rather than quietly producing a thin spec.

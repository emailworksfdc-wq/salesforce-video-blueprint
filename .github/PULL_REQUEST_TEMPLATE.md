<!--
Thanks for contributing. Please read CONTRIBUTING.md if you haven't — this
project has a few non-obvious rules, especially around the score gate.
-->

## What this changes

<!-- One or two sentences. What is different after this PR? -->

## Why

<!-- What problem does this solve? If it fixes a defect in docs/DEFECT_LEDGER.md,
     link the entry. If it closes an issue, write "Closes #123". -->

## How it was verified

<!-- Paste the relevant test output. "Tests pass" is not enough — show which
     test would have caught the bug before the fix. -->

```
$ .venv/bin/python -m pytest -q
```

## Checklist

- [ ] Full suite passes locally (`.venv/bin/python -m pytest -q`)
- [ ] Added a test that **fails without this change** (for fixes) or covers the
      new behaviour (for features)
- [ ] Updated `docs/DEFECT_LEDGER.md` if this fixes or introduces a known defect
- [ ] Updated `CHANGELOG.md` under `[Unreleased]`
- [ ] Updated the README status table if a stage's grade changed
- [ ] Commit messages follow Conventional Commits

## Project-specific declarations

Tick only what applies, and explain any box you tick.

- [ ] **This changes a quality gate** (`spec_score.py`, `scripts/score_run.py`,
      placeholder patterns, or `markers.py`).
      Gates may be made *stricter*, never *weaker*. Explain the direction and why:

- [ ] **This adds a code path that could emit un-observed content** into a spec
      (a default, a fallback, an inferred value). Explain why it cannot fabricate
      evidence the recording did not prove:

- [ ] **This touches secret or PII handling** (tokens, redaction, URL scrubbing,
      leak detection). Confirm no value is logged, persisted, echoed in a
      finding, or passed via argv:

- [ ] **This touches an org-safety guard** (production detection, blocked
      aliases, login flow). Confirm it still fails closed when org state is
      unknown:

- [ ] None of the above.

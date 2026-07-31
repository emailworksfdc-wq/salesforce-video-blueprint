# Re-test Report — fixes from 955f3df

Date: 2026-07-31

## Fix 1 — score_spec_file str input

Status: PASS

Output:
```
PASS: str input works, score= 85
```

`score_spec_file('outputs/case_triage.agent-spec.json')` — bare string, no AttributeError, score returned correctly.

---

## Fix 2 — run --help description

Status: PASS

Output (exact run line from --help):
```
│ run       Derive a conversational agent spec from a capture file and score   │
```

Previously blank. Now has a description.

---

## Fix 3 — displayScoreNote

Status: PASS

Output:
```
score: 85
displayScore: 59
displayScoreNote: Capped below the moderate band because blocking issues are present. Use `score` for version comparisons; use `displayScore` when showing a human.
```

`displayScoreNote` is present, non-None, and explains the cap in plain English. The guidance ("Use `score` for version comparisons; use `displayScore` when showing a human") is exactly what was missing.

---

## Fix 4 — mcp-install.md venv docs

Status: PASS

Output (matching lines from docs/mcp-install.md):
```
**If you installed with `pip install -e .` into a project venv**, the binary is at
`.venv/bin/sf-blueprint-mcp` and is NOT on your system `$PATH`. Update `.mcp.json`
to use the relative path:

{
  "mcpServers": {
    "sf-blueprint": {
      "command": ".venv/bin/sf-blueprint-mcp"
    }
  }
}

Or add it from the command line using the absolute path:
  claude mcp add sf-blueprint -- "$(pwd)/.venv/bin/sf-blueprint-mcp"
```

The venv PATH gap is now explicitly documented with both a relative-path `.mcp.json` snippet and an absolute-path CLI command.

---

## Full test suite

Status: PASS

Output:
```
1806 passed, 1 skipped in 24.33s
```

Zero regressions introduced by 955f3df.

---

## Overall

ALL FIXES VERIFIED

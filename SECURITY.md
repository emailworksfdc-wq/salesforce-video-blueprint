# Security Policy

## Reporting a vulnerability

**Do not open a public issue.** Use
[private vulnerability reporting](https://github.com/emailworksfdc-wq/salesforce-video-blueprint/security/advisories/new)
on this repository.

Please report privately anything that could:

- expose a Salesforce access token, session ID, or frontdoor URL
- cause org data or PII to be written to an artifact, log, or terminal
- let the pipeline target a **production** org despite the fail-closed guard
- let a stub or fabricated run be stamped as real evidence, or let a spec pass
  the quality gate without the evidence to justify it

That last category is treated as a security issue here, not merely a bug: this
tool exists so a human can decide whether to build an agent from a recording. A
spec that overclaims causes real-world action on a false premise.

## Supported versions

Pre-1.0. Only the latest release on `main` receives fixes.

| Version | Supported |
| --- | --- |
| 0.1.x | ✅ |
| < 0.1 | ❌ |

## Operational safety model

Users of this tool should understand what it does with credentials and org data.

**Credentials**

- Tokens are read from the `SF_ACCESS_TOKEN` environment variable. Never pass a
  token as a command-line argument — argv is world-readable via `ps`.
- Authentication to an org for replay uses a signed frontdoor URL from
  `sf org open --url-only`. The login form is never automated.
- `sid`, `access_token`, and session parameters are stripped from URLs before
  anything is written to an artifact.
- **Known gap:** one telemetry code path passes an access token in a subprocess
  argument list, where it is visible to other local processes and can surface in
  an operator-visible timeout message. Tracked in `docs/DEFECT_LEDGER.md`. Do not
  run live telemetry collection on a shared or untrusted host until it is fixed.

**Org data**

- Capture traces and HTML blueprints embed record IDs and field values
  **verbatim**. `outputs/` and `inputs/` are gitignored for this reason. Treat
  every artifact as sensitive and never attach one to a public issue.
- `redaction.py` implements secret and PII redaction, but **is not yet called
  from the pipeline**. Do not rely on automatic redaction.
- The redaction-leak detector currently inspects only one of the eight
  field-identity signals the recorder captures, so a secret identified by input
  type or Salesforce field API name may not be flagged.

**Org targeting**

- Replay *re-executes* recorded actions. A recorded "Create Case" writes a new
  record on every run. There is no idempotency guard and no cleanup. Use sandbox
  or scratch orgs only.
- The production guard fails closed: if org type cannot be determined, replay
  refuses rather than assuming safety. `SF_ALLOW_PRODUCTION_ORG=1` overrides it
  and is logged loudly.
- Two org aliases are hard-blocked with **no override path**, in both the replay
  and telemetry layers.

## Scope

Out of scope for a security report:

- The known defects already listed in `docs/DEFECT_LEDGER.md` (report a *new*
  exploitation path, not the entry itself)
- Findings that require an attacker to already control the machine running the
  pipeline
- The deliberate absence of a feature (for example: no MCP server, no automatic
  redaction). These are documented gaps, not vulnerabilities.

# Open-Source MCP Release Checklist

> **STATUS: PARTLY SATISFIED — no public package release has happened.**
>
> The MCP server itself exists (`src/sf_video_blueprint/mcp_server.py`, installed
> as `sf-blueprint-mcp`; see [`mcp-install.md`](mcp-install.md)). This checklist
> was written for a **public npm/PyPI release**, which has not been done — the
> package installs from the git URL only.
>
> The boxes below are unchecked because they are unchecked, not because nothing
> exists. What is genuinely done:
>
> - Install, quickstart, per-harness config, limitations, and troubleshooting are
>   documented in [`mcp-install.md`](mcp-install.md).
> - Tool docs match behaviour: the tool descriptions a client sees *are* the
>   docstrings, so they cannot drift.
> - A realistic end-to-end example is tested on every CI run
>   (`scripts/mcp_stdio_check.py` against `examples/case_triage.dom_capture.jsonl`).
> - Secret scanning and push protection are enabled on the repository.
> - Apache-2.0 licensed; `CONTRIBUTING.md` and `SECURITY.md` are in place.
>
> What is not:
>
> - No PyPI/npm publication, so no release automation, no signed artifacts, no
>   published version-support policy.
> - No backward-compatibility gate — there are no external consumers yet.
> - `sf agent validate authoring-bundle` has never been run against any output of
>   this project, so no output has org-level validation. **This is the single most
>   important gap before anyone relies on the emitted bundles.**

## Documentation

- [ ] README includes install, quickstart, config, auth model, limitations, troubleshooting.
- [ ] Tool/API docs match current behavior (inputs, outputs, errors, limits/timeouts).

## Schemas and Contracts

- [ ] All MCP schemas validate (required fields, enums, examples, error envelopes).
- [ ] Versioning and deprecation policy are documented.

## Examples

- [ ] One minimal hello-world example exists.
- [ ] One realistic end-to-end workflow example exists.
- [ ] Example scripts are tested against current release candidate.

## CI and Release Automation

- [ ] PR/main CI green (lint, typecheck, unit/integration tests, build/package checks).
- [ ] Release pipeline dry run verified (tagging, artifacts, checksums/signatures if used).

## Security

- [ ] Secret scanning and dependency vulnerability scanning pass.
- [ ] No credentials in repository history or tracked files.
- [ ] Threat model and security policy/contact path are published.
- [ ] Auth scopes are least-privilege.

## Legal and Licensing

- [ ] SPDX-compatible license file is present.
- [ ] Third-party licenses and attributions are audited and compliant.

## Changelog and Release Notes

- [ ] Release notes include highlights, breaking changes, migration steps, known issues.
- [ ] Changelog entries are complete and version-aligned.

# Open-Source MCP Release Checklist

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

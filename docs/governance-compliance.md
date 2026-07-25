# Governance and Compliance Standard

## Data Classification

Every field and artifact must be classified before production use:

- Public
- Internal
- Confidential
- Restricted
- Regulated

Unknown classification must fail closed and route to human review.

## PII Controls

- Data minimization by default.
- Purpose binding for every PII processing path.
- Least privilege for agent running user and tool scopes.
- Deny high-risk PII in free-form prompts without explicit approved workflow.

## Masking and Redaction Layers

- Pre-ingest redaction: pattern-based PII stripping.
- In-flight context policy: remove non-allowlisted sensitive fields.
- Post-output scan: block leakage in responses and logs.
- Mask before writing any conversation memory.

## Retention and Deletion

- Retention windows vary by data class and region.
- Separate policy for runtime context, logs, embeddings, and review artifacts.
- Verifiable deletion and DSAR (access/correct/delete/export) workflows required.

## Consent and Notice

- Explicit notice that user is interacting with an AI agent.
- Consent gates for sensitive processing when required.
- Channel opt-out and global opt-out must propagate to all stores and pipelines.

## Audit Requirements

Every high-impact operation must log:

- policy version
- model version
- prompt template version
- action/tool invocation
- identity and permission scope
- run and step correlation IDs

## Deployment Gates (Mandatory)

- data inventory complete
- legal basis mapped by region
- masking controls tested
- retention controls verified
- consent and opt-out end-to-end verified
- access controls validated
- incident runbook tested

Any failed gate blocks production deployment.

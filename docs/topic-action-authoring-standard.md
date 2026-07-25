# Topic and Action Authoring Standard

## Core Principles

- Keep topics narrow and non-overlapping.
- Use instructions for conversational guidance, not deterministic security logic.
- Implement deterministic validation and access checks inside actions.
- Start with minimal instructions, then expand only from trace evidence.
- Define explicit disambiguation and fallback paths.

## Topic Template

- Topic Name
- Classification Description
- Scope (can do / cannot do)
- Success Criteria

Classification should include:

- user-language examples
- clear inclusion criteria
- explicit exclusions for adjacent topics

## Instruction Patterns

### Always/Never

Use only for high-value constraints:

- Always collect required context before state-changing actions.
- Never execute sensitive operations without verification.
- Never fabricate unavailable data.

### If-Then Policies

- If condition A, then action X.
- If missing required input, request standardized clarifying inputs.
- If policy block is triggered, escalate to human handoff path.

### Disambiguation

When multiple topics are plausible:

1. state two likely interpretations
2. ask one decisive clarifying question
3. defer irreversible action until clarified

### Fallback Contract

Fallback triggers:

- no confident topic match
- repeated missing required data
- action failure
- policy block

Fallback response:

1. acknowledge limitation
2. offer safe alternative
3. capture minimum routing context
4. escalate with structured handoff payload

## Action Design Template

- Action API name
- Description (what/when/preconditions)
- Inputs (name, required, format, example)
- Outputs (name, type, meaning)
- Security controls (sharing/FLS/OLS/auth checks)
- Failure taxonomy (recoverable/non-recoverable behavior)

## Quality Rubric (0-2 each)

Score dimensions:

- topic separability
- classification clarity
- scope discipline
- instruction minimalism
- absolute usage quality
- decision explicitness
- disambiguation quality
- fallback robustness
- action semantics
- security and determinism

Interpretation:

- 17-20: production-ready
- 13-16: good, needs hardening
- 9-12: moderate risk
- <9: high risk

## Review Checklist

- Each topic maps to one distinct job-to-be-done.
- Adjacent topic overlap is eliminated or explicitly disambiguated.
- Sensitive rules are enforced in action logic, not prompt text alone.
- State-changing actions include confirmation/verification.
- Fallback and escalation behavior is tested and deterministic.
- Regression suite runs after every instruction/topic update.

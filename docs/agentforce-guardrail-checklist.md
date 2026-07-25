# Agentforce Guardrail Checklist

## 1) Least Privilege

- Use a dedicated agent execution user (not a shared human account).
- Grant only required object/field/action permissions.
- Scope external credentials and API permissions to minimal operations.
- Deny by default outside allowlisted domains.

Pass criteria:
- Unauthorized action attempts are blocked and logged.
- Access review shows no unused high-risk permissions.

## 2) Topic Constraints

- Define explicit in-scope and out-of-scope intents.
- Add refusal/reroute behavior for unsupported requests.
- Require disambiguation before irreversible operations.

Pass criteria:
- Off-topic prompts are consistently refused/rerouted.
- On-topic prompts route correctly in validation set.

## 3) Action Security

- Validate input schema and required fields on every action.
- Enforce preconditions for risky actions (verification/confirmation/policy checks).
- Sanitize outputs before downstream rendering.

Pass criteria:
- Invalid and unauthorized calls fail closed.
- High-risk actions require explicit confirmation and policy pass.

## 4) Handoff Controls

- Define deterministic handoff triggers (confidence, policy hit, repeated failures).
- Pass minimal context to human queue with default redaction.
- Preserve complete handoff audit trail.

Pass criteria:
- Handoff scenarios trigger correctly in tests.
- Queue receives actionable but least-privilege context.

## 5) Monitoring and Response

- Log topic routing, action invocation/denial, refusals, handoffs, policy hits.
- Alert on denial spikes, injection patterns, and failed handoffs.
- Run recurring adversarial and leakage tests.

Pass criteria:
- Dashboards expose all guardrail metrics.
- No critical guardrail failures in release test cycle.

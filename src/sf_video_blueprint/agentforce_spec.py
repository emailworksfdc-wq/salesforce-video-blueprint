from __future__ import annotations

"""Bridge from DerivedAgentSpec to Agentforce spec YAML.

This module transforms the recording-derived spec (spec_builder.py) into the YAML
shape required by ``sf agent generate authoring-bundle --spec <file>``. It is the
payoff step: the user's goal is "input a recording, get a conversational AI agent
spec, iterate on it, build it as an Agentforce agent."

**Key impedance mismatch:** The Agentforce spec YAML has no dedicated fields for
guardrails or failure_handling. These must be carried forward as instruction prose
within topic descriptions, or they are silently lost between our pipeline and the
generated agent. This is a known limitation of the current CLI contract.

**Refuse-by-default:** If the derived spec is incomplete (low confidence, missing
objects, unresolved intent), this module refuses to emit a misleading YAML by
default. Use ``allow_incomplete=True`` to override and inject visible gap markers.

Design constraints:

* **Determinism:** Same input produces byte-identical YAML (no timestamps, UUIDs,
  or dict-ordering nondeterminism).
* **Key order is significant:** The CLI expects keys in a specific order. Our YAML
  emitter preserves that order exactly.
* **No invention:** One recorded process yields one topic. Do not pad to
  ``max_topics`` with fabricated topics.
* **Provenance is visible:** A comment block at the top names the recording,
  confidence, and unknowns count.
"""

from dataclasses import dataclass
from pathlib import Path
import re

from .spec_builder import DerivedAgentSpec, DerivedEntity
from .naming import topic_api_name, dedupe_names
from .markers import PLACEHOLDER_MARKERS


class AgentforceSpecError(Exception):
    """Base exception for Agentforce spec generation."""
    pass


class InsufficientEvidenceError(AgentforceSpecError):
    """Raised when the derived spec lacks the evidence to emit a valid agent spec.

    This is the refuse-by-default mechanism. To override, pass
    ``allow_incomplete=True`` to ``build_agent_spec_yaml``.
    """
    pass


# Compatibility alias: B6's tests import this directly. The authoritative
# implementation is now in naming.py — all callers should prefer topic_api_name.
_to_api_name = topic_api_name


def _escape_yaml_string(value: str) -> str:
    """Escape a string for safe YAML emission.

    Uses double-quoted style when the string contains characters that need
    escaping (colons, hashes, leading dashes, quotes, newlines). Otherwise
    returns the plain string.
    """
    # If empty, emit empty quotes
    if not value:
        return '""'

    # Patterns that require quoting
    needs_quotes = (
        value.startswith(('-', ' ', '"', "'", '#')) or
        value.endswith((' ', '"', "'")) or
        ': ' in value or
        ' #' in value or
        '\n' in value or
        '"' in value or
        value in ('true', 'false', 'null', 'yes', 'no', 'on', 'off')
    )

    if not needs_quotes:
        return value

    # Use double-quoted style with escaping
    # Note: single quotes don't need escaping in double-quoted YAML strings
    escaped = value.replace('\\', '\\\\').replace('"', '\\"').replace('\n', '\\n')
    return f'"{escaped}"'


def _emit_yaml_block_scalar(text: str, indent: int = 0) -> str:
    """Emit a multi-line string as a YAML block scalar (|-).

    This is safer for long descriptions that may contain special characters.
    """
    prefix = ' ' * indent
    lines = text.strip().split('\n')
    result = f"{prefix}|-\n"
    for line in lines:
        result += f"{prefix}  {line}\n"
    return result.rstrip('\n')


@dataclass
class AgentSpecYaml:
    """Agentforce agent spec in the exact shape the CLI expects.

    Key order is significant and is preserved in ``to_yaml()``.
    """
    agent_type: str  # "internal" | "customer"
    company_name: str
    company_description: str
    company_website: str | None
    role: str
    max_num_of_topics: int
    agent_user: str | None
    enrich_logs: bool
    tone: str  # "formal" | "casual" | "neutral"
    prompt_template_name: str | None
    grounding_context: str | None
    topics: list[dict[str, str]]  # [{"name": str, "description": str}]

    # Provenance comments (not part of the schema, emitted as YAML comments)
    provenance_recording_id: str | None = None
    provenance_confidence: float | None = None
    provenance_unknowns_count: int = 0

    def to_yaml(self) -> str:
        """Emit YAML with keys in the canonical order.

        Deterministic: same input always produces byte-identical output.
        """
        lines: list[str] = []

        # Provenance comment block at the top
        if self.provenance_recording_id:
            lines.append(f"# Generated from recording: {self.provenance_recording_id}")
        if self.provenance_confidence is not None:
            lines.append(f"# Confidence: {self.provenance_confidence:.3f}")
        if self.provenance_unknowns_count > 0:
            lines.append(f"# Unknowns: {self.provenance_unknowns_count} gap(s) in evidence")
        if lines:
            lines.append("")

        # Key order is significant
        lines.append(f"agentType: {_escape_yaml_string(self.agent_type)}")
        lines.append(f"companyName: {_escape_yaml_string(self.company_name)}")

        # Multi-line description as block scalar
        if '\n' in self.company_description or len(self.company_description) > 80:
            lines.append("companyDescription: |-")
            for line in self.company_description.strip().split('\n'):
                lines.append(f"  {line}")
        else:
            lines.append(f"companyDescription: {_escape_yaml_string(self.company_description)}")

        if self.company_website:
            lines.append(f"companyWebsite: {_escape_yaml_string(self.company_website)}")

        # Role as block scalar for safety (may contain instructions)
        if '\n' in self.role or len(self.role) > 80:
            lines.append("role: |-")
            for line in self.role.strip().split('\n'):
                lines.append(f"  {line}")
        else:
            lines.append(f"role: {_escape_yaml_string(self.role)}")

        lines.append(f"maxNumOfTopics: {self.max_num_of_topics}")

        if self.agent_user:
            lines.append(f"agentUser: {_escape_yaml_string(self.agent_user)}")

        lines.append(f"enrichLogs: {str(self.enrich_logs).lower()}")
        lines.append(f"tone: {_escape_yaml_string(self.tone)}")

        if self.prompt_template_name:
            lines.append(f"promptTemplateName: {_escape_yaml_string(self.prompt_template_name)}")

        if self.grounding_context:
            if '\n' in self.grounding_context or len(self.grounding_context) > 80:
                lines.append("groundingContext: |-")
                for line in self.grounding_context.strip().split('\n'):
                    lines.append(f"  {line}")
            else:
                lines.append(f"groundingContext: {_escape_yaml_string(self.grounding_context)}")

        # Topics (list of maps)
        lines.append("topics:")
        if not self.topics:
            lines.append("  []")
        else:
            for topic in self.topics:
                lines.append(f"  - name: {_escape_yaml_string(topic['name'])}")
                # Description as block scalar if multi-line or long
                desc = topic['description']
                if '\n' in desc or len(desc) > 80:
                    lines.append("    description: |-")
                    for line in desc.strip().split('\n'):
                        lines.append(f"      {line}")
                else:
                    lines.append(f"    description: {_escape_yaml_string(desc)}")

        return '\n'.join(lines) + '\n'


def role_from_spec(spec: DerivedAgentSpec) -> str:
    """Derive the agent's role from the spec.

    The role is prose describing the agent's job, specific to the recording.
    Must never be generic.
    """
    if spec.intent.startswith("UNRESOLVED:"):
        return "[NEEDS EVIDENCE: role could not be derived — intent is unresolved]"

    parts = [spec.intent]

    # Include orchestration context
    if spec.entities:
        entity_names = [e.name for e in spec.entities if e.field_api_name]
        if entity_names:
            parts.append(f"Collect: {', '.join(sorted(set(entity_names[:5])))}.")

    # Add confirmation requirement (from orchestration_steps in spec_builder)
    parts.append("Confirm before writing, and report the result.")

    return ' '.join(parts)


def topics_from_spec(spec: DerivedAgentSpec) -> list[dict[str, str]]:
    """Derive topics from the observed process.

    One recorded process legitimately yields ONE primary topic. Do not pad to
    max_topics with invented topics — inventing topics the recording never
    demonstrated is the failure mode this project exists to correct.

    Returns a list of {name: str, description: str} dicts.
    """
    # CRITICAL: Always derive the topic name via naming.topic_api_name, even for
    # unresolved intents. The [NEEDS EVIDENCE] marker belongs in the description
    # (as honest signalling), but the NAME is a reference key that agent_script.py
    # and eval_spec.py consume — if we invent a name here, cross-artifact linkage
    # breaks and the generated test suite never matches the spec YAML.
    #
    # naming.topic_api_name already strips the UNRESOLVED: prefix and returns
    # FALLBACK_TOPIC_NAME when no tokens remain, so this is the correct call for
    # ALL intents, resolved or not.
    topic_name = topic_api_name(spec.intent)

    if spec.intent.startswith("UNRESOLVED:"):
        return [{
            "name": topic_name,
            "description": "[NEEDS EVIDENCE: topic could not be derived — intent is unresolved]"
        }]

    # Derive topic name from intent using the canonical shared naming module.
    # The naming module strips parentheticals correctly, so we pass the full intent.
    # "Update Case (Status)" -> "Update_Case_Status"

    # Build description from orchestration + guardrails + failure handling
    description_parts = []

    # Core orchestration
    if spec.orchestration_steps:
        description_parts.append("Process:")
        for step in spec.orchestration_steps:
            description_parts.append(f"- {step}")

    # Guardrails (critical — must not be silently lost)
    if spec.guardrails:
        description_parts.append("\nGuardrails:")
        for guard in spec.guardrails:
            description_parts.append(f"- {guard}")

    # Failure handling (if observed)
    if spec.failure_handling and not spec.failure_handling[0].startswith("No failures"):
        description_parts.append("\nError handling:")
        for handling in spec.failure_handling:
            description_parts.append(f"- {handling}")

    description = '\n'.join(description_parts)

    return [{
        "name": topic_name,
        "description": description
    }]


def build_agent_spec_yaml(
    spec: DerivedAgentSpec,
    *,
    company_name: str,
    company_description: str,
    agent_type: str = "internal",
    tone: str = "formal",
    max_topics: int = 5,
    agent_user: str | None = None,
    company_website: str | None = None,
    enrich_logs: bool = False,
    prompt_template_name: str | None = None,
    grounding_context: str | None = None,
    allow_incomplete: bool = False,
    recording_id: str | None = None,
) -> AgentSpecYaml:
    """Build an Agentforce agent spec YAML from a DerivedAgentSpec.

    This is the intellectual core of the bridge: deriving role and topics from
    the observed process.

    Args:
        spec: The derived spec from spec_builder.py
        company_name: Company name for the agent
        company_description: Brief company description
        agent_type: "internal" (default) or "customer"
        tone: "formal" (default), "casual", or "neutral"
        max_topics: Max topics the agent can handle (CLI uses this for LLM context)
        agent_user: Optional org username for running the agent
        company_website: Optional company website URL
        enrich_logs: Whether to enrich logs with additional context
        prompt_template_name: Optional custom prompt template
        grounding_context: Optional grounding context for the agent
        allow_incomplete: If False (default), raise InsufficientEvidenceError when
            evidence is insufficient. If True, inject visible gap markers.
        recording_id: Optional recording ID for provenance comments

    Returns:
        AgentSpecYaml ready to emit via .to_yaml()

    Raises:
        InsufficientEvidenceError: If evidence is insufficient and
            allow_incomplete is False.
    """
    # Refuse-by-default checks
    errors = []
    if spec.intent.startswith("UNRESOLVED:"):
        errors.append("Intent is unresolved — recording did not demonstrate a completed action.")
    if spec.confidence < 0.4:
        errors.append(f"Confidence too low ({spec.confidence:.3f} < 0.4) — need clearer recording.")
    if not spec.objects_touched:
        errors.append("No objects touched — cannot identify the target Salesforce object.")

    if errors and not allow_incomplete:
        msg = "Cannot generate agent spec — insufficient evidence:\n"
        msg += '\n'.join(f"  - {err}" for err in errors)
        msg += "\n\nTo override and inject gap markers, pass allow_incomplete=True."
        raise InsufficientEvidenceError(msg)

    # Derive role and topics
    role = role_from_spec(spec)
    topics = topics_from_spec(spec)

    return AgentSpecYaml(
        agent_type=agent_type,
        company_name=company_name,
        company_description=company_description,
        company_website=company_website,
        role=role,
        max_num_of_topics=max_topics,
        agent_user=agent_user,
        enrich_logs=enrich_logs,
        tone=tone,
        prompt_template_name=prompt_template_name,
        grounding_context=grounding_context,
        topics=topics,
        provenance_recording_id=recording_id,
        provenance_confidence=spec.confidence,
        provenance_unknowns_count=len(spec.unknowns),
    )


def write_agent_spec_yaml(path: Path, spec_yaml: AgentSpecYaml) -> Path:
    """Write the agent spec YAML to disk.

    Args:
        path: Output path (will be created if parent dirs exist)
        spec_yaml: The spec to write

    Returns:
        The path written to (for chaining)
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    content = spec_yaml.to_yaml()
    path.write_text(content, encoding='utf-8')
    return path

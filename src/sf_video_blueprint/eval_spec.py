"""Test spec emission for Agentforce evaluation.

Emits test specifications in BOTH dialects:
- Legacy `AiEvaluationDefinition` (Testing Center)
- NGT `AiTestingDefinition` (Agentforce Studio)

All scorer/metric names are sourced from the CLI's ngtScorerCatalog.js and utils.js.
Do NOT invent scorer names; the CLI validates at deploy time.

**CRITICAL CONSTRAINT:** This module does NOT fabricate test cases for untested
scenarios. If a failure path was not observed, the gap is recorded explicitly
rather than inventing a plausible test. This is the core failure mode the
project corrects: tests that look thorough but prove nothing.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import json
from pathlib import Path
import re
from typing import Any

from .naming import topic_api_name
from .spec_builder import DerivedAgentSpec, DerivedEntity

# --- Confirmed scorer/metric names from CLI source ---
# From @salesforce/agents/lib/ngtScorerCatalog.js
NGT_SCORERS_NEEDING_EXPECTED = frozenset(
    {
        "topic_sequence_match",
        "action_sequence_match",
        "agent_handoff_match",
        "bot_response_rating",
        "response_match",
    }
)

NGT_SCORERS_QUALITY = frozenset(
    {
        "coherence",
        "conciseness",
        "factuality",
        "completeness",
        "task_resolution",
        "output_latency_milliseconds",
    }
)

# From @salesforce/agents/lib/utils.js line 66
LEGACY_METRICS = frozenset(
    {
        "completeness",
        "coherence",
        "conciseness",
        "output_latency_milliseconds",
    }
)


# Compatibility alias for tests that may import _to_api_name.
# The canonical implementation is now in naming.py, which keeps
# parentheticals (critical: "Update Case (Status)" must not become "UpdateCase").
_to_api_name = topic_api_name


@dataclass(slots=True)
class TestCaseDerivation:
    """Provenance for a single test case: why it exists and what it tests."""

    utterance: str
    purpose: str  # "happy path", "entity collection", "guardrail", "failure path"
    evidence: str  # which observed step/field/guardrail triggered this case
    gaps: list[str] = field(default_factory=list)  # what we could NOT derive


@dataclass(slots=True)
class LegacyTestCase:
    utterance: str
    expectedTopic: str | None = None
    expectedActions: list[str] = field(default_factory=list)
    expectedOutcome: str | None = None
    customEvaluations: list[dict[str, Any]] = field(default_factory=list)
    metrics: list[str] = field(default_factory=list)
    contextVariables: list[dict[str, str]] = field(default_factory=list)
    conversationHistory: list[dict[str, str]] = field(default_factory=list)


@dataclass(slots=True)
class LegacyTestSpec:
    name: str
    subjectType: str
    subjectName: str
    testCases: list[LegacyTestCase]
    description: str | None = None
    subjectVersion: str | None = None


@dataclass(slots=True)
class NgtInput:
    utterance: str
    contextVariables: list[dict[str, str]] = field(default_factory=list)
    conversationHistory: list[dict[str, Any]] = field(default_factory=list)


@dataclass(slots=True)
class NgtScorer:
    name: str
    expected: str | None = None


@dataclass(slots=True)
class NgtTestCase:
    inputs: list[NgtInput]
    scorers: list[NgtScorer]


@dataclass(slots=True)
class NgtTestSpec:
    name: str
    subjectType: str
    subjectName: str
    testCases: list[NgtTestCase]
    description: str | None = None
    subjectVersion: str | None = None


def build_legacy_test_spec(
    spec: DerivedAgentSpec,
    *,
    name: str,
    subject_name: str,
    subject_type: str = "AGENT",
) -> tuple[LegacyTestSpec, list[TestCaseDerivation]]:
    """Derive a legacy AiEvaluationDefinition test spec from observed behaviour.

    Returns (test_spec, derivations) where derivations records provenance and gaps.
    """
    test_cases: list[LegacyTestCase] = []
    derivations: list[TestCaseDerivation] = []

    topic_name = topic_api_name(spec.intent)

    # --- Case 1: Happy path ---
    happy_utterance = _phrase_as_user_request(spec.intent, spec.entities)
    test_cases.append(
        LegacyTestCase(
            utterance=happy_utterance,
            expectedTopic=topic_name,
            expectedActions=[],  # see derivation rule 7
            expectedOutcome=f"Confirms the {spec.intent.lower()} completed successfully.",
            metrics=list(LEGACY_METRICS),
        )
    )
    derivations.append(
        TestCaseDerivation(
            utterance=happy_utterance,
            purpose="happy path",
            evidence=f"Derived from intent: {spec.intent}",
            gaps=[
                "expectedActions left empty: action API names were not observed. "
                "Re-run with action telemetry enabled or manually fill from deployed agent metadata."
            ],
        )
    )

    # --- Case 2: Entity collection tests ---
    for entity in spec.entities:
        # Skip entities with empty or whitespace-only names
        if not entity.name or not entity.name.strip():
            continue
        if entity.field_api_name and entity.object_api_name:
            utterance = _omit_entity_from_request(spec.intent, entity, spec.entities)
            test_cases.append(
                LegacyTestCase(
                    utterance=utterance,
                    expectedTopic=topic_name,
                    expectedOutcome=f"Asks the user to provide the {entity.name}.",
                    metrics=list(LEGACY_METRICS),
                )
            )
            derivations.append(
                TestCaseDerivation(
                    utterance=utterance,
                    purpose="entity collection",
                    evidence=f"Entity {entity.name} ({entity.object_api_name}.{entity.field_api_name}) observed in data delta.",
                )
            )

    # --- Case 3: Guardrail cases ---
    confirmation_guardrail = any(
        "confirmation" in g.lower() for g in spec.guardrails
    )
    if confirmation_guardrail:
        test_cases.append(
            LegacyTestCase(
                utterance=happy_utterance,  # same as happy path but we check for confirmation
                expectedTopic=topic_name,
                expectedOutcome="Requests explicit user confirmation before writing.",
                metrics=list(LEGACY_METRICS),
            )
        )
        derivations.append(
            TestCaseDerivation(
                utterance=happy_utterance,
                purpose="guardrail: confirmation-before-write",
                evidence="Guardrail: 'Require explicit user confirmation before writing' observed in spec.",
            )
        )

    scope_guardrail = any("scope" in g.lower() for g in spec.guardrails)
    if scope_guardrail:
        off_topic_utterance = "What is the weather today?"
        test_cases.append(
            LegacyTestCase(
                utterance=off_topic_utterance,
                expectedOutcome="Refuses the request as out of scope.",
                metrics=list(LEGACY_METRICS),
            )
        )
        derivations.append(
            TestCaseDerivation(
                utterance=off_topic_utterance,
                purpose="guardrail: scope",
                evidence="Guardrail: 'Scope the agent to the objects listed' observed in spec.",
            )
        )

    # --- Case 4: Failure path cases (ONLY if observed) ---
    # Per derivation rule 4: if failure_handling says "UNTESTED", do NOT fabricate.
    untested = any("UNTESTED" in fh for fh in spec.failure_handling)
    if not untested:
        for fh in spec.failure_handling:
            # spec_builder._derive_failure_handling emits:
            # "Observed <layer> failure during recording: <reason>"
            # Detect this stable fragment to catch ALL observed failures (validation, apex, flow, ...)
            if "failure during recording" in fh.lower():
                # Extract the failure layer from the message (e.g., "Observed validation failure...")
                layer_match = re.search(r"Observed\s+(\w+)\s+failure", fh, re.IGNORECASE)
                layer = layer_match.group(1).lower() if layer_match else "unknown"

                # We observed a failure, so we can test for it
                failure_utterance = _introduce_validation_error(
                    spec.intent, spec.entities
                )
                test_cases.append(
                    LegacyTestCase(
                        utterance=failure_utterance,
                        expectedTopic=topic_name,
                        expectedOutcome=f"Returns the {layer} error message without retrying.",
                        metrics=list(LEGACY_METRICS),
                    )
                )
                derivations.append(
                    TestCaseDerivation(
                        utterance=failure_utterance,
                        purpose=f"failure path: {layer} error",
                        evidence=fh,
                    )
                )
            elif "validation error" in fh.lower():
                # Legacy shape: "On validation error, return the offending field and message"
                # This is the second sentence for validation failures, so we've already
                # handled it in the "failure during recording" branch above.
                # Keep this branch for backward compatibility with older specs.
                pass
    else:
        derivations.append(
            TestCaseDerivation(
                utterance="(no failure case generated)",
                purpose="failure path gap",
                evidence="spec.failure_handling indicates error paths are UNTESTED.",
                gaps=[
                    "No failure test cases emitted. Record a failing run to observe validation/permission/async errors."
                ],
            )
        )

    test_spec = LegacyTestSpec(
        name=name,
        subjectType=subject_type,
        subjectName=subject_name,
        testCases=test_cases,
        description=f"Test suite derived from recorded run: {spec.intent}",
    )

    return test_spec, derivations


def build_ngt_test_spec(
    spec: DerivedAgentSpec,
    *,
    name: str,
    subject_name: str,
) -> tuple[NgtTestSpec, list[TestCaseDerivation]]:
    """Derive an NGT AiTestingDefinition test spec from observed behaviour.

    Returns (test_spec, derivations).
    """
    test_cases: list[NgtTestCase] = []
    derivations: list[TestCaseDerivation] = []

    topic_name = topic_api_name(spec.intent)

    # --- Case 1: Happy path ---
    happy_utterance = _phrase_as_user_request(spec.intent, spec.entities)
    ngt_input = NgtInput(utterance=happy_utterance)
    ngt_scorers = [
        NgtScorer(name="topic_sequence_match", expected=topic_name),
        # no action_sequence_match: we don't know action API names (rule 7)
        NgtScorer(
            name="bot_response_rating",
            expected=f"Confirms the {spec.intent.lower()} completed successfully.",
        ),
        NgtScorer(name="completeness"),
        NgtScorer(name="coherence"),
    ]
    test_cases.append(NgtTestCase(inputs=[ngt_input], scorers=ngt_scorers))
    derivations.append(
        TestCaseDerivation(
            utterance=happy_utterance,
            purpose="happy path",
            evidence=f"Derived from intent: {spec.intent}",
            gaps=[
                "action_sequence_match scorer omitted: action API names were not observed."
            ],
        )
    )

    # --- Case 2: Entity collection tests ---
    for entity in spec.entities:
        # Skip entities with empty or whitespace-only names
        if not entity.name or not entity.name.strip():
            continue
        if entity.field_api_name and entity.object_api_name:
            utterance = _omit_entity_from_request(spec.intent, entity, spec.entities)
            ngt_input = NgtInput(utterance=utterance)
            ngt_scorers = [
                NgtScorer(name="topic_sequence_match", expected=topic_name),
                NgtScorer(
                    name="bot_response_rating",
                    expected=f"Asks the user to provide the {entity.name}.",
                ),
            ]
            test_cases.append(NgtTestCase(inputs=[ngt_input], scorers=ngt_scorers))
            derivations.append(
                TestCaseDerivation(
                    utterance=utterance,
                    purpose="entity collection",
                    evidence=f"Entity {entity.name} ({entity.object_api_name}.{entity.field_api_name}) observed.",
                )
            )

    # --- Case 3: Guardrail cases ---
    confirmation_guardrail = any(
        "confirmation" in g.lower() for g in spec.guardrails
    )
    if confirmation_guardrail:
        ngt_input = NgtInput(utterance=happy_utterance)
        ngt_scorers = [
            NgtScorer(name="topic_sequence_match", expected=topic_name),
            NgtScorer(
                name="bot_response_rating",
                expected="Requests explicit user confirmation before writing.",
            ),
        ]
        test_cases.append(NgtTestCase(inputs=[ngt_input], scorers=ngt_scorers))
        derivations.append(
            TestCaseDerivation(
                utterance=happy_utterance,
                purpose="guardrail: confirmation-before-write",
                evidence="Guardrail observed in spec.",
            )
        )

    scope_guardrail = any("scope" in g.lower() for g in spec.guardrails)
    if scope_guardrail:
        off_topic_utterance = "What is the weather today?"
        ngt_input = NgtInput(utterance=off_topic_utterance)
        ngt_scorers = [
            NgtScorer(
                name="bot_response_rating", expected="Refuses the request as out of scope."
            )
        ]
        test_cases.append(NgtTestCase(inputs=[ngt_input], scorers=ngt_scorers))
        derivations.append(
            TestCaseDerivation(
                utterance=off_topic_utterance,
                purpose="guardrail: scope",
                evidence="Scope guardrail observed in spec.",
            )
        )

    # --- Case 4: Failure path (ONLY if observed) ---
    untested = any("UNTESTED" in fh for fh in spec.failure_handling)
    if not untested:
        for fh in spec.failure_handling:
            # spec_builder._derive_failure_handling emits:
            # "Observed <layer> failure during recording: <reason>"
            # Detect this stable fragment to catch ALL observed failures (validation, apex, flow, ...)
            if "failure during recording" in fh.lower():
                # Extract the failure layer from the message (e.g., "Observed validation failure...")
                layer_match = re.search(r"Observed\s+(\w+)\s+failure", fh, re.IGNORECASE)
                layer = layer_match.group(1).lower() if layer_match else "unknown"

                failure_utterance = _introduce_validation_error(
                    spec.intent, spec.entities
                )
                ngt_input = NgtInput(utterance=failure_utterance)
                ngt_scorers = [
                    NgtScorer(name="topic_sequence_match", expected=topic_name),
                    NgtScorer(
                        name="bot_response_rating",
                        expected=f"Returns the {layer} error message without retrying.",
                    ),
                ]
                test_cases.append(NgtTestCase(inputs=[ngt_input], scorers=ngt_scorers))
                derivations.append(
                    TestCaseDerivation(
                        utterance=failure_utterance,
                        purpose=f"failure path: {layer} error",
                        evidence=fh,
                    )
                )
            elif "validation error" in fh.lower():
                # Legacy shape: "On validation error, return the offending field and message"
                # This is the second sentence for validation failures, so we've already
                # handled it in the "failure during recording" branch above.
                # Keep this branch for backward compatibility with older specs.
                pass
    else:
        derivations.append(
            TestCaseDerivation(
                utterance="(no failure case generated)",
                purpose="failure path gap",
                evidence="spec.failure_handling indicates error paths are UNTESTED.",
                gaps=[
                    "No failure test cases emitted. Record a failing run to observe errors."
                ],
            )
        )

    test_spec = NgtTestSpec(
        name=name,
        subjectType="AGENT",
        subjectName=subject_name,
        testCases=test_cases,
        description=f"NGT test suite derived from recorded run: {spec.intent}",
    )

    return test_spec, derivations


def write_test_spec(path: Path, test_spec: LegacyTestSpec | NgtTestSpec) -> Path:
    """Write a test spec to YAML with provenance comments.

    Detects dialect by type. Key order matches the CLI's write order so diffs are clean.
    """
    path.parent.mkdir(parents=True, exist_ok=True)

    if isinstance(test_spec, LegacyTestSpec):
        yaml_content = _emit_legacy_yaml(test_spec)
    else:
        yaml_content = _emit_ngt_yaml(test_spec)

    path.write_text(yaml_content, encoding="utf-8")
    return path


def detect_runner_hint() -> str:
    """Return the recommended test runner for new test specs.

    `sf agent generate test-spec` auto-detects the runner by querying the org
    for AiEvaluationDefinition and AiTestingDefinition metadata. Guessing from
    the CLI side risks deploying the wrong shape. The safest default is
    'testing-center' (legacy), which works on all orgs. For NGT, the user should
    either pass `--test-runner agentforce-studio` explicitly or let the CLI decide.

    Returns: 'testing-center'
    """
    return "testing-center"


# --- Internal helpers for test case generation ---


def _phrase_as_user_request(intent: str, entities: list[DerivedEntity]) -> str:
    """Turn an intent + entities into a natural user utterance.

    Example: intent="Update Case (Status)", entities=[{name: "recordId"}, {name: "status"}]
    -> "Update the status on case {recordId} to {status}"

    **Degenerate-case handling:**
    - Empty intent: returns "[NEEDS EVIDENCE: no intent observed]" (visible marker)
    - Empty entity names: excluded from placeholders (not silently dropped)
    - Duplicate entity names: each appears exactly once in the utterance
    - Unresolved intent (starts with "UNRESOLVED:"): keeps the marker in the base
      so the utterance is obviously incomplete rather than plausible-looking

    The governing principle: an obviously incomplete utterance is better than one
    that looks finished but validates nothing.
    """
    # strip trailing parentheticals
    base = re.sub(r"\s*\([^)]*\)\s*$", "", intent).strip()

    # Degenerate case: empty or whitespace-only intent
    if not base or not base.strip():
        return "[NEEDS EVIDENCE: no intent observed]"

    # Keep UNRESOLVED: prefix visible in the utterance so the test case reads as incomplete
    # (naming.py strips it for API names, but here we want the test to fail visibly)

    if not entities:
        return base

    # Deduplicate entity names (case-insensitive) while preserving order.
    # Two entities with the same name should not produce "{status} {status}".
    seen_names: set[str] = set()
    unique_entities: list[DerivedEntity] = []
    for e in entities:
        # Skip entities with empty/whitespace-only names
        if not e.name or not e.name.strip():
            continue
        key = e.name.strip().lower()
        if key not in seen_names:
            seen_names.add(key)
            unique_entities.append(e)

    # If we have a recordId entity, assume it's an update intent
    has_record_id = any(e.name == "recordId" for e in unique_entities)
    if has_record_id and "update" in base.lower():
        obj = unique_entities[0].object_api_name or "record"
        # find the field entity (not recordId)
        field_entities = [e for e in unique_entities if e.name != "recordId"]
        if field_entities:
            field = field_entities[0].name
            return f"Update the {field} on {obj.lower()} {{recordId}} to {{{field}}}"

    # fallback: generic request
    placeholders = " ".join(f"{{{e.name}}}" for e in unique_entities if e.name != "recordId")
    result = f"{base} {placeholders}".strip()

    # Final check: if the result is empty or whitespace-only (shouldn't happen but guard it),
    # return a visible marker rather than an empty string
    if not result or not result.strip():
        return "[NEEDS EVIDENCE: unresolvable utterance]"

    return result


def _omit_entity_from_request(
    intent: str, entity: DerivedEntity, all_entities: list[DerivedEntity]
) -> str:
    """Generate an utterance that omits one entity, forcing the agent to collect it.

    Applies the same degenerate-case handling as _phrase_as_user_request:
    empty/whitespace entity names are excluded, and empty intents return a marker.
    """
    base = re.sub(r"\s*\([^)]*\)\s*$", "", intent).strip()

    # Degenerate case: empty intent
    if not base or not base.strip():
        return "[NEEDS EVIDENCE: no intent observed]"

    # Filter other entities, excluding the target entity and any with empty names
    other_entities = [
        e for e in all_entities
        if e.name != entity.name and e.name and e.name.strip()
    ]

    # Deduplicate by name (case-insensitive)
    seen_names: set[str] = set()
    unique_others: list[DerivedEntity] = []
    for e in other_entities:
        key = e.name.strip().lower()
        if key not in seen_names:
            seen_names.add(key)
            unique_others.append(e)

    if not unique_others:
        return base

    placeholders = " ".join(f"{{{e.name}}}" for e in unique_others if e.name != "recordId")
    result = f"{base} {placeholders}".strip()

    if not result or not result.strip():
        return "[NEEDS EVIDENCE: unresolvable utterance]"

    return result


def _introduce_validation_error(
    intent: str, entities: list[DerivedEntity]
) -> str:
    """Generate an utterance with an invalid value to trigger a validation error.

    Applies the same degenerate-case handling: empty intent returns a marker.
    """
    base = re.sub(r"\s*\([^)]*\)\s*$", "", intent).strip()

    # Degenerate case: empty intent
    if not base or not base.strip():
        return "[NEEDS EVIDENCE: no intent observed] with an invalid value"

    # Use an obviously invalid value
    # Filter entities to exclude empty names
    valid_entities = [e for e in entities if e.name and e.name.strip()]
    has_record_id = any(e.name == "recordId" for e in valid_entities)

    if has_record_id:
        return f"{base} {{recordId}} with an invalid value that will fail validation"
    return f"{base} with an invalid value"


# --- YAML emission (minimal, correct, deterministic) ---
# PyYAML is not in pyproject.toml dependencies, so we implement a minimal emitter.
# Key order MUST match the CLI's write order for clean diffs.


def _emit_legacy_yaml(spec: LegacyTestSpec) -> str:
    """Emit a legacy AiEvaluationDefinition test spec as YAML.

    Key order: name, description, subjectType, subjectName, subjectVersion, testCases.
    """
    lines = [
        "# Legacy AiEvaluationDefinition test spec",
        "# Generated from recorded run",
        f"# Subject: {spec.subjectName}",
        "",
        f"name: {_yaml_string(spec.name)}",
    ]
    if spec.description:
        lines.append(f"description: {_yaml_string(spec.description)}")
    lines.append(f"subjectType: {spec.subjectType}")
    lines.append(f"subjectName: {_yaml_string(spec.subjectName)}")
    if spec.subjectVersion:
        lines.append(f"subjectVersion: {_yaml_string(spec.subjectVersion)}")

    lines.append("testCases:")
    for tc in spec.testCases:
        lines.append(f"  - utterance: {_yaml_string(tc.utterance)}")
        if tc.expectedTopic:
            lines.append(f"    expectedTopic: {_yaml_string(tc.expectedTopic)}")
        if tc.expectedActions:
            lines.append("    expectedActions:")
            for action in tc.expectedActions:
                lines.append(f"      - {_yaml_string(action)}")
        if tc.expectedOutcome:
            lines.append(f"    expectedOutcome: {_yaml_string(tc.expectedOutcome)}")
        if tc.customEvaluations:
            lines.append("    customEvaluations:")
            for ce in tc.customEvaluations:
                lines.append(f"      - name: {_yaml_string(ce['name'])}")
                if "label" in ce:
                    lines.append(f"        label: {_yaml_string(ce['label'])}")
                if "parameters" in ce:
                    lines.append("        parameters:")
                    for param in ce["parameters"]:
                        lines.append(
                            f"          - name: {_yaml_string(param['name'])}"
                        )
                        lines.append(
                            f"            value: {_yaml_string(param['value'])}"
                        )
        if tc.metrics:
            lines.append("    metrics:")
            for metric in tc.metrics:
                lines.append(f"      - {metric}")
        if tc.contextVariables:
            lines.append("    contextVariables:")
            for cv in tc.contextVariables:
                lines.append(f"      - name: {_yaml_string(cv['name'])}")
                lines.append(f"        value: {_yaml_string(cv['value'])}")
        if tc.conversationHistory:
            lines.append("    conversationHistory:")
            for turn in tc.conversationHistory:
                lines.append(f"      - role: {turn['role']}")
                lines.append(f"        message: {_yaml_string(turn['message'])}")
                if "topic" in turn:
                    lines.append(f"        topic: {_yaml_string(turn['topic'])}")

    return "\n".join(lines) + "\n"


def _emit_ngt_yaml(spec: NgtTestSpec) -> str:
    """Emit an NGT AiTestingDefinition test spec as YAML.

    Key order: name, description, subjectType, subjectName, subjectVersion, testCases.
    Each testCase: inputs, scorers.
    """
    lines = [
        "# NGT AiTestingDefinition test spec",
        "# Generated from recorded run",
        f"# Subject: {spec.subjectName}",
        "",
        f"name: {_yaml_string(spec.name)}",
    ]
    if spec.description:
        lines.append(f"description: {_yaml_string(spec.description)}")
    lines.append(f"subjectType: {spec.subjectType}")
    lines.append(f"subjectName: {_yaml_string(spec.subjectName)}")
    if spec.subjectVersion:
        lines.append(f"subjectVersion: {_yaml_string(spec.subjectVersion)}")

    lines.append("testCases:")
    for tc in spec.testCases:
        lines.append("  - inputs:")
        for inp in tc.inputs:
            lines.append(f"      - utterance: {_yaml_string(inp.utterance)}")
            if inp.contextVariables:
                lines.append("        contextVariables:")
                for cv in inp.contextVariables:
                    lines.append(f"          - name: {_yaml_string(cv['name'])}")
                    lines.append(f"            value: {_yaml_string(cv['value'])}")
            if inp.conversationHistory:
                lines.append("        conversationHistory:")
                for turn in inp.conversationHistory:
                    lines.append(f"          - role: {turn['role']}")
                    lines.append(f"            message: {_yaml_string(turn['message'])}")
                    if "topic" in turn:
                        lines.append(f"            topic: {_yaml_string(turn['topic'])}")
                    if "index" in turn:
                        lines.append(f"            index: {turn['index']}")
        lines.append("    scorers:")
        for scorer in tc.scorers:
            lines.append(f"      - name: {scorer.name}")
            if scorer.expected is not None:
                lines.append(f"        expected: {_yaml_string(scorer.expected)}")

    return "\n".join(lines) + "\n"


def _yaml_string(value: str) -> str:
    """Quote a string for YAML if it contains special characters.

    Handles: colons, quotes, apostrophes, question marks, newlines.
    """
    if not value:
        return '""'

    # Check if quoting is needed
    needs_quotes = (
        ":" in value
        or "?" in value
        or value.startswith(" ")
        or value.endswith(" ")
        or "\n" in value
        or '"' in value
        or "'" in value
        or value.startswith("#")
        or value.startswith("-")
        or value.startswith("[")
        or value.startswith("{")
    )

    if not needs_quotes:
        return value

    # Use double quotes and escape internal double quotes
    escaped = value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
    return f'"{escaped}"'

"""Tests for the compiler-measured Agent Script grammar rules.

Every expectation here corresponds to a probe that was actually run against the
Salesforce compilation API from AFT3. The verbatim compiler output for each is
recorded in ``docs/AGENT_SCRIPT_GRAMMAR.md``; where a test asserts a specific
error, the docstring names the probe that established it.
"""

from __future__ import annotations

import pytest

from sf_video_blueprint.agent_script_grammar import (
    COMPILER_NAME_LIMIT,
    SUPPORTED_TARGET_SCHEMES,
    VALID_INVOCATION_NAMESPACES,
    check_action_grammar,
    check_target_scheme,
)


class TestTargetSchemes:
    """The supported-scheme list came verbatim from the compiler's own error."""

    def test_the_24_schemes_the_compiler_enumerated(self):
        """Probe tgt_bogus_scheme made the compiler list everything it supports."""
        # If Salesforce adds a scheme this set must be re-measured, not guessed.
        assert len(SUPPORTED_TARGET_SCHEMES) == 24
        # Spot-check the ones that matter to this project.
        for scheme in ("apex", "flow", "prompt", "mcpTool", "standardInvocableAction"):
            assert scheme in SUPPORTED_TARGET_SCHEMES

    def test_scheme_matching_is_case_sensitive(self):
        """The compiler lists `apexRest`, so `apexrest` is not the same token."""
        assert "apexRest" in SUPPORTED_TARGET_SCHEMES
        assert "apexrest" not in SUPPORTED_TARGET_SCHEMES
        assert check_target_scheme("apexrest://Foo") is not None

    @pytest.mark.parametrize(
        "target",
        [
            "apex://CheckWeather",
            "flow://Get_Resort_Hours",
            "mcpTool://SomeTool",
            "prompt://SomeTemplate",
        ],
    )
    def test_accepts_targets_the_compiler_accepted(self, target):
        """Probes tgt_apex_missing / tgt_flow_missing / ns_mcp / ns_prompt_scheme."""
        assert check_target_scheme(target) is None

    def test_rejects_unsupported_scheme(self):
        """Probe tgt_bogus_scheme: `banana://` -> CompilationError, exit 1."""
        problem = check_target_scheme("banana://Nope")
        assert problem is not None
        assert "banana" in problem

    def test_rejects_target_with_no_scheme(self):
        """Probe tgt_no_scheme: a bare name -> "has an invalid target"."""
        problem = check_target_scheme("SFVB_TEST_NoScheme")
        assert problem is not None
        assert "invalid target" in problem

    def test_rejects_scheme_naming_no_resource(self):
        assert check_target_scheme("apex://") is not None


class TestInvocationNamespaces:
    """`@apex.*` / `@flow.*` are NOT how Apex and Flow are invoked."""

    @pytest.mark.parametrize("namespace", ["apex", "flow", "prompt", "standard", "action", "agent_action"])
    def test_namespaces_the_compiler_refuses(self, namespace):
        """Probes act_*: "'<ns>' is not a valid invocation target"."""
        errors = check_action_grammar(f"            do_it: @{namespace}.SomeThing\n")
        assert errors, f"@{namespace}. should be reported as invalid"
        assert "not a valid invocation target" in errors[0]

    def test_apex_error_names_the_real_construct(self):
        """A rejection is only useful if it says what to write instead."""
        errors = check_action_grammar("            do_it: @apex.MyClass\n")
        assert 'target: "apex://' in errors[0]
        assert "@actions." in errors[0]

    def test_flow_error_names_the_real_construct(self):
        errors = check_action_grammar("            do_it: @flow.My_Flow\n")
        assert 'target: "flow://' in errors[0]

    def test_accepts_the_namespaces_the_compiler_recognised(self):
        """`actions`, `utils`, `subagent`, `topic` are all real namespaces."""
        for namespace in ("actions", "utils", "subagent", "topic"):
            assert namespace in VALID_INVOCATION_NAMESPACES

    def test_accepts_the_real_apex_invocation_shape(self):
        """The shape the org-authored Local_Info_Agent bundle actually uses."""
        content = """        actions:
            check_weather: @actions.check_weather

    actions:
        check_weather:
            description: "Fetch the weather forecast."
            label: "Check Weather"
            target: "apex://CheckWeather"
"""
        assert check_action_grammar(content) == []

    def test_flags_unknown_utils_member(self):
        """Probe act_utilsbad: "'no_such_util' is not defined in utils"."""
        errors = check_action_grammar("            x: @utils.no_such_util\n")
        assert errors
        assert "not defined in utils" in errors[0]

    def test_allows_the_two_utils_members_the_emitter_uses(self):
        content = """            go_to_x: @utils.transition to @subagent.x
            escalate_to_human: @utils.escalate
"""
        assert check_action_grammar(content) == []

    def test_variable_sources_are_not_treated_as_invocations(self):
        """`source: @MessagingSession.Id` is a different construct entirely.

        The first-party template emits exactly this, so a checker that flagged it
        would reject Salesforce's own boilerplate.
        """
        content = """    EndUserId: linked string
        source: @MessagingSession.MessagingEndUserId
"""
        assert check_action_grammar(content) == []

    def test_reports_line_numbers(self):
        content = "system:\n\n            do_it: @apex.Foo\n"
        errors = check_action_grammar(content)
        assert errors and errors[0].startswith("Line 3:")


class TestFirstPartyOutputIsClean:
    """The checker must not reject what Salesforce's own generator produces."""

    def test_first_party_template_shape_passes(self):
        """Reproduces the router + escalation blocks byte-for-byte from
        `@salesforce/agents/lib/templates/agentScriptTemplate.js` (v1.6.6),
        which validated with exit 0 against AFT3.
        """
        content = """start_agent agent_router:
    reasoning:
        instructions: ->
            | Select the tool that best matches the user's message.
        actions:
            go_to_escalation: @utils.transition to @subagent.escalation
            go_to_off_topic: @utils.transition to @subagent.off_topic
            go_to_ambiguous_question: @utils.transition to @subagent.ambiguous_question

subagent escalation:
    reasoning:
        instructions: ->
            | If a user explicitly asks to transfer to a live agent, escalate.
        actions:
            escalate_to_human: @utils.escalate
                description: "Call this tool to escalate to a human agent."
"""
        assert check_action_grammar(content) == []


class TestValidateLocallyCatchesWhatTheCompilerCaught:
    """`validate_locally` was blind to the action grammar and false-positived on
    the first-party continuation-line style. Both were measured against AFT3.
    """

    APEX_INVOCATION = """system:
    instructions: "You are an AI Agent."

config:
    developer_name: "Probe"

start_agent agent_router:
    reasoning:
        instructions: ->
            | Route the user.
        actions:
            go_to_probe: @utils.transition to @subagent.probe

subagent escalation:
    reasoning:
        instructions: ->
            | Escalate.
        actions:
            escalate_to_human: @utils.escalate

subagent off_topic:
    reasoning:
        instructions: ->
            | Redirect.

subagent ambiguous_question:
    reasoning:
        instructions: ->
            | Clarify.

subagent probe:
    label: "Probe"
    description: "Probe subagent"

    reasoning:
        instructions: ->
            | Probe line.
        actions:
            do_the_thing: @apex.SFVB_TEST_NoSuchApexClass
                description: "An Apex action."
"""

    def test_reports_the_apex_invocation_the_compiler_rejected(self):
        """MEASURED: this exact shape failed with
        "Cannot invoke '@apex.SFVB_TEST_NoSuchApexClass' — 'apex' is not a valid
        invocation target." while `validate_locally` reported nothing about it.
        """
        from sf_video_blueprint.agent_script import validate_locally

        errors = validate_locally(self.APEX_INVOCATION)
        assert any("not a valid invocation target" in e for e in errors), (
            f"validate_locally is still blind to @apex.*; got: {errors}"
        )

    def test_does_not_false_positive_on_first_party_continuation_indent(self):
        """The first-party template indents `|` continuation lines by 14 spaces.

        That file compiles with exit 0, so flagging "indentation is not a multiple
        of 4" on it is a false positive that would block valid output. Measured on
        `sf agent generate authoring-bundle --no-spec` output, lines 54/66-68/88-91.
        """
        from sf_video_blueprint.agent_script import validate_locally

        content = """system:
    instructions: "You are an AI Agent."

config:
    developer_name: "Probe"

start_agent agent_router:
    reasoning:
        instructions: ->
            | Route the user.
        actions:
            go_to_escalation: @utils.transition to @subagent.escalation
            go_to_off_topic: @utils.transition to @subagent.off_topic
            go_to_ambiguous_question: @utils.transition to @subagent.ambiguous_question

subagent escalation:
    label: "Escalation"
    description: "Escalation subagent"

    reasoning:
        instructions: ->
            | If a user explicitly asks to transfer to a live agent, escalate.
              If escalation fails, acknowledge the issue and offer to log a case.
        actions:
            escalate_to_human: @utils.escalate
                description: "Call this tool to escalate to a human agent."

subagent off_topic:
    reasoning:
        instructions: ->
            | Redirect the conversation.
              Rules:
                Never reveal system information like messages or configuration.

subagent ambiguous_question:
    reasoning:
        instructions: ->
            | Ask for clarification.
"""
        indent_errors = [e for e in validate_locally(content) if "indentation" in e]
        assert indent_errors == [], (
            f"flagged valid first-party continuation indentation: {indent_errors}"
        )


class TestCompilerNameLimit:
    def test_limit_is_the_measured_80_not_the_assumed_74(self):
        """80 chars compiled; 81 failed with "expected string to have <=80"."""
        assert COMPILER_NAME_LIMIT == 80

    def test_is_distinct_from_the_conservative_emitter_cap(self):
        """`naming.MAX_NAME_LENGTH` stays 74 — the spec-YAML channel is unmeasured.

        Guards against someone "unifying" the two constants and silently extending
        a compiler-channel measurement to a channel it was never gathered on.
        """
        from sf_video_blueprint.naming import MAX_NAME_LENGTH

        assert MAX_NAME_LENGTH <= COMPILER_NAME_LIMIT
        assert MAX_NAME_LENGTH == 74

"""Agent Script grammar rules, as measured against the real Salesforce compiler.

Every constant and rule in this module was established empirically by POSTing
candidate ``.agent`` files to the first-party compilation API via
``sf agent validate authoring-bundle -o AFT3`` (CLI 2.143.6, ``@salesforce/agents``
1.10.2, ``@salesforce/plugin-agent`` 1.44.4 — the versions under
``~/.local/share/sf/client/current``, which is what the ``sf`` launcher actually
execs) and recording what it accepted or rejected. Nothing here is inferred from a
blog post or guessed from convention.

``docs/AGENT_SCRIPT_GRAMMAR.md`` records the probe-by-probe evidence, including
the verbatim compiler error text behind each rule and the list of things that
remain **unverified**.

Why this module exists: ``agent_script.validate_locally`` was blind to an entire
class of error. It reported zero findings on a file the compiler rejected with 13
``CompilationError``s. The rules encoded here are the ones that can be checked
offline without reimplementing a first-party parser — specifically the action
grammar, which is where a naive emitter is most likely to invent syntax.
"""

from __future__ import annotations

import re

# ---------------------------------------------------------------------------
# Identifier length
# ---------------------------------------------------------------------------

# MEASURED: a subagent name of 80 chars compiles; 81 fails with
#   "Too big: expected string to have <=80 characters"
# The cap applies to the subagent NAME. A 100-char `go_to_…` router action
# compiled fine, so router action names are NOT length-checked by the compiler.
#
# This mirrors `naming.COMPILER_VERIFIED_NAME_LIMIT`. It is deliberately NOT the
# same thing as `naming.MAX_NAME_LENGTH` (74), which stays lower because
# `topic_api_name` also feeds the spec YAML / test-spec channel, whose limit has
# never been measured. Do not "fix" one to match the other.
COMPILER_NAME_LIMIT = 80

# ---------------------------------------------------------------------------
# Action target schemes
# ---------------------------------------------------------------------------

# GROUND TRUTH, verbatim from the compiler. Probing a bogus scheme
# (`target: "banana://X"`) makes the compiler enumerate everything it supports:
#
#   Action 'do_it' uses unsupported target scheme "banana://". Supported schemes:
#   api, apex, apexRest, auraEnabled, cdpMlPrediction, createCatalogItemRequest,
#   decisionTableAction, executeIntegrationProcedure, expressionSet,
#   externalConnector, externalService, flow, generatePromptResponse,
#   integrationProcedureAction, mcpTool, namedQuery, placeholder, prompt,
#   quickAction, retriever, runExpressionSet, serviceCatalog, slack,
#   standardInvocableAction.
#
# Case matters: the compiler lists `apexRest`, not `apexrest`.
SUPPORTED_TARGET_SCHEMES = frozenset(
    {
        "api",
        "apex",
        "apexRest",
        "auraEnabled",
        "cdpMlPrediction",
        "createCatalogItemRequest",
        "decisionTableAction",
        "executeIntegrationProcedure",
        "expressionSet",
        "externalConnector",
        "externalService",
        "flow",
        "generatePromptResponse",
        "integrationProcedureAction",
        "mcpTool",
        "namedQuery",
        "placeholder",
        "prompt",
        "quickAction",
        "retriever",
        "runExpressionSet",
        "serviceCatalog",
        "slack",
        "standardInvocableAction",
    }
)

# ---------------------------------------------------------------------------
# Invocation namespaces
# ---------------------------------------------------------------------------

# MEASURED: the compiler distinguishes two failure modes, which tells us which
# namespaces exist at all.
#
# `@apex.Foo`, `@flow.Foo`, `@prompt.Foo`, `@standard.Foo`, `@action.Foo`,
# `@agent_action.Foo` all fail with:
#     "Cannot invoke '@apex.Foo' — 'apex' is not a valid invocation target."
# i.e. the NAMESPACE does not exist.
#
# `@actions.Foo`, `@topic.Foo`, `@utils.Foo` fail with:
#     "'Foo' is not defined in actions"
# i.e. the namespace IS real and only the member was missing.
#
# So Apex and Flow are NOT invoked as `@apex.*` / `@flow.*`. They are declared as
# a subagent-level `actions:` block with `target: "apex://Cls"` and then referenced
# as `@actions.<name>`. Confirmed against the org-authored `Local_Info_Agent`
# bundle retrieved from AFT3, which uses exactly that shape.
VALID_INVOCATION_NAMESPACES = frozenset({"actions", "utils", "subagent", "topic", "variables", "outputs"})

# Namespaces a naive emitter is likely to invent. Mapping each to the real
# construct turns a compiler rejection into an actionable local error.
INVALID_NAMESPACE_HINTS = {
    "apex": 'declare an actions: block with target: "apex://<ClassName>" and reference it as @actions.<name>',
    "flow": 'declare an actions: block with target: "flow://<FlowApiName>" and reference it as @actions.<name>',
    "prompt": 'declare an actions: block with target: "prompt://<TemplateApiName>"',
    "standard": 'declare an actions: block with target: "standardInvocableAction://<name>"',
    "action": "the namespace is plural: @actions.<name>",
    "agent_action": "the namespace is @actions.<name>",
}

# `@utils.` members the compiler accepted. `@utils.no_such_util` fails with
# "'no_such_util' is not defined in utils", so the namespace is closed.
KNOWN_UTILS_MEMBERS = frozenset({"transition", "escalate"})

# ---------------------------------------------------------------------------
# config: block identifiers
# ---------------------------------------------------------------------------

# GROUND TRUTH, verbatim from the compiler. A `developer_name` the pattern rejects
# produces:
#
#   Invalid string: must match pattern /^[A-Za-z](_?[A-Za-z0-9])*$/ for config
#
# Measured consequences of that regex — every one of these was rejected:
#   "Case Updater" (space), "case-updater" (hyphen), "9lives" (leading digit),
#   "_leading" (leading underscore), "Trailing_" (trailing underscore),
#   "Double__Underscore" (consecutive underscores), "é_accent" (non-ASCII).
# Accepted: "Valid_Name_1", "lower_ok", "A", "a1", "a_1_b_2", and 80 x "a".
#
# Note this is STRICTER than the subagent-name pattern, which does permit one
# run of two underscores: /^[A-Za-z](_?[A-Za-z0-9])*(__(_?[A-Za-z0-9])*)?$/.
# `developer_name` has no such allowance, so a name that is legal as a subagent
# can still be illegal as a developer_name.
CONFIG_DEVELOPER_NAME_PATTERN = re.compile(r"^[A-Za-z](_?[A-Za-z0-9])*$")

# MEASURED: 80 chars of `developer_name` compiles; 81 fails with
#   "Too big: expected string to have <=80 characters for config"
CONFIG_NAME_LIMIT = 80

# MEASURED by omitting one config key at a time from an otherwise-valid file.
# Omitting `developer_name` -> the pattern error above (the compiler treats a
# missing name as an empty string). Omitting `description` ->
# "Missing required field 'description'". Omitting `agent_label` or
# `default_agent_user` compiles fine, so neither is required.
REQUIRED_CONFIG_KEYS = frozenset({"developer_name", "description"})

_TARGET_LINE = re.compile(r'^\s*target:\s*"([^"]*)"\s*$')
_CONFIG_KEY_LINE = re.compile(r'^\s+([a-z_]+):\s*"(.*)"\s*$')
_SCHEME = re.compile(r"^([A-Za-z][A-Za-z0-9]*)://(.*)$")
# Matches an invocation such as `@apex.Foo` / `@actions.bar` / `@utils.escalate`.
#
# The leading `(?<![\\w.@-])` guard is load-bearing: without it the local part of an
# email address is read as an invocation. The org-authored `Local_Info_Agent` bundle
# retrieved from AFT3 carries an `afdx-agent@testdrive.org<suffix>` value for
# `default_agent_user`, and this checker reported `cannot invoke '@testdrive.org<suffix>'`
# on it — a false positive on a bundle the org itself authored and accepted.
# (The suffix is an org-specific identifier and is deliberately not reproduced here.)
# Re-validating that exact value through the compilation API returns exit 0.
_INVOCATION = re.compile(r"(?<![\w.@-])@([A-Za-z_][A-Za-z0-9_]*)\.([A-Za-z0-9_.]+)")


def check_target_scheme(target: str) -> str | None:
    """Return an error string when ``target`` is not a compiler-supported URI.

    Mirrors the two distinct errors the compiler raises: a missing scheme and an
    unsupported one are reported differently, so the local message says which.

    >>> check_target_scheme("apex://MyClass") is None
    True
    >>> check_target_scheme("banana://X")
    'unsupported target scheme "banana://" ...'
    """
    match = _SCHEME.match(target)
    if not match:
        return (
            f'invalid target "{target}": expected a URI with a supported scheme '
            f"({', '.join(sorted(SUPPORTED_TARGET_SCHEMES))})"
        )
    scheme = match.group(1)
    if scheme not in SUPPORTED_TARGET_SCHEMES:
        return (
            f'unsupported target scheme "{scheme}://": supported schemes are '
            f"{', '.join(sorted(SUPPORTED_TARGET_SCHEMES))}"
        )
    if not match.group(2):
        return f'target "{target}" has a supported scheme but names no resource'
    return None


def check_developer_name(name: str) -> str | None:
    """Return an error string when ``name`` is an illegal ``config: developer_name``.

    The compiler enforces ``/^[A-Za-z](_?[A-Za-z0-9])*$/`` and an 80-char cap on
    this field. It is the one ``config:`` value this project passes straight
    through from a caller without checking, so a caller that derives it from a
    process name ("Update Case Status") produces a bundle that cannot compile.

    >>> check_developer_name("Case_Updater") is None
    True
    >>> check_developer_name("Case Updater")
    'developer_name "Case Updater" is invalid: ...'
    """
    if len(name) > CONFIG_NAME_LIMIT:
        return (
            f'developer_name "{name[:24]}…" is {len(name)} chars; the compiler '
            f"rejects anything over {CONFIG_NAME_LIMIT} with "
            f'"Too big: expected string to have <={CONFIG_NAME_LIMIT} characters"'
        )
    if not CONFIG_DEVELOPER_NAME_PATTERN.match(name):
        return (
            f'developer_name "{name}" is invalid: must match '
            f"{CONFIG_DEVELOPER_NAME_PATTERN.pattern} — start with a letter, then "
            "letters/digits singly separated by underscores (no spaces, hyphens, "
            "leading digit, trailing or doubled underscore, or non-ASCII)"
        )
    return None


def check_config_block(content: str) -> list[str]:
    """Report ``config:`` block errors the real compiler would raise.

    Checks the ``developer_name`` identifier rule and the presence of the keys
    the compiler requires. Both were measured; see the module constants.

    Args:
        content: A complete ``.agent`` file body.

    Returns:
        Error strings, line-numbered where a specific line is at fault.
    """
    errors: list[str] = []
    lines = content.split("\n")

    # Locate the `config:` block: from `config:` to the next unindented line.
    start: int | None = None
    for i, line in enumerate(lines):
        if line.rstrip() == "config:":
            start = i
            break
    if start is None:
        # The compiler reports this as "Missing config block".
        return ["Missing config block; the compiler requires it (unlike system:)"]

    found: dict[str, tuple[int, str]] = {}
    for offset, line in enumerate(lines[start + 1 :], start=start + 2):
        if line and not line[0].isspace():
            break
        match = _CONFIG_KEY_LINE.match(line)
        if match:
            found[match.group(1)] = (offset, match.group(2))

    for key in sorted(REQUIRED_CONFIG_KEYS):
        if key not in found:
            errors.append(
                f"config: is missing required key '{key}' "
                "(the compiler rejects the bundle without it)"
            )

    if "developer_name" in found:
        lineno, value = found["developer_name"]
        problem = check_developer_name(value)
        if problem:
            errors.append(f"Line {lineno}: {problem}")

    return errors


def check_action_grammar(content: str) -> list[str]:
    """Report Agent Script action-grammar errors the real compiler would raise.

    Catches the two classes that ``validate_locally`` was blind to:

    1. An invocation in a namespace the compiler does not recognise — most
       importantly ``@apex.Foo`` / ``@flow.Bar``, which look plausible and are
       exactly what a naive emitter invents, but are rejected outright.
    2. An ``actions:`` declaration whose ``target:`` URI uses an unsupported
       scheme, or no scheme at all.

    This is a *conservative* checker: it reports only what was measured against
    the compiler. It is NOT a parser and a clean result does not mean the bundle
    compiles — run ``sf agent validate authoring-bundle`` for that.

    Args:
        content: A complete ``.agent`` file body.

    Returns:
        Error strings, each prefixed with the 1-based line number.
    """
    errors: list[str] = []

    for lineno, line in enumerate(content.split("\n"), start=1):
        # 1. Unrecognised invocation namespaces.
        for namespace, member in _INVOCATION.findall(line):
            if namespace in VALID_INVOCATION_NAMESPACES:
                if namespace == "utils" and member not in KNOWN_UTILS_MEMBERS:
                    errors.append(
                        f"Line {lineno}: '{member}' is not defined in utils "
                        f"(known members: {', '.join(sorted(KNOWN_UTILS_MEMBERS))})"
                    )
                continue
            # Skip `@MessagingSession.Id`-style variable sources, which are a
            # different construct: they appear only as a `source:` value.
            if line.lstrip().startswith("source:"):
                continue
            hint = INVALID_NAMESPACE_HINTS.get(namespace)
            detail = f" — instead, {hint}" if hint else ""
            errors.append(
                f"Line {lineno}: cannot invoke '@{namespace}.{member}' — "
                f"'{namespace}' is not a valid invocation target{detail}"
            )

        # 2. Action target URIs.
        target_match = _TARGET_LINE.match(line)
        if target_match:
            problem = check_target_scheme(target_match.group(1))
            if problem:
                errors.append(f"Line {lineno}: {problem}")

    return errors

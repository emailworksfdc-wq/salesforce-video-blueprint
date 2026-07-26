"""Invoke Salesforce's Agent Script compiler on an emitted bundle.

Every org validation this project has ever performed was typed by hand at a
shell prompt. Nothing in the codebase called the compiler, so no code path could
answer the only question that matters about an emitted bundle: does Salesforce
accept it? :func:`agent_script.validate_locally` is this project's own opinion
about its own output, and it was measurably wrong once already — it reported zero
findings on a file the compiler rejected with 13 ``CompilationError``s.

This module is the call site for the real verdict.

How the command actually behaves, measured against AFT3 on 2026-07-26 (CLI
2.143.6, ``@salesforce/plugin-agent`` 1.40.5):

- ``sf agent validate authoring-bundle`` declares ``requiresProject = true``. It
  resolves the bundle by API name beneath a package directory, so a loose
  ``.agent`` file cannot be passed to it. :func:`write_validation_project`
  scaffolds the minimum layout it will accept.
- It does **not** require the bundle to be deployed. It reads the local file and
  POSTs the contents to the first-party compile endpoint; the org connection is
  used for authentication only. Ten validate-only probes left no metadata in the
  org, confirmed with ``sf org list metadata``.
- Exit 0 with ``success: true`` means the Agent Script compiled. Exit 1 returns
  the compiler's errors, whose text is the useful part: ``Too big: expected
  string to have <=80 characters`` names the fix, and a paraphrase does not.

Compilation is syntax, not behaviour. A bundle that compiles has not been shown
to *do* anything.
"""

from __future__ import annotations

import json
import re
import subprocess
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

# The call is a network round trip to the compile endpoint. Measured latency on
# AFT3 was a few seconds; 120s is slack for a slow org, not an expectation.
DEFAULT_TIMEOUT_SECONDS = 120

# Same hard block as `telemetry._FORBIDDEN_ORG_ALIASES`. These two orgs are out
# of scope entirely — not even read-only — so the alias is rejected before the
# subprocess starts rather than being trusted to the CLI.
_FORBIDDEN_ORG_ALIASES = frozenset({"ppcdm", "ppcaccenture", "ppaccenture"})

# The CLI colourises its output even under --json, and prints notices ahead of
# the payload. Strip the escapes and start at the first brace.
_ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")

_BUNDLE_META_XML = (
    '<?xml version="1.0" encoding="UTF-8"?>\n'
    '<AiAuthoringBundle xmlns="http://soap.sforce.com/2006/04/metadata">\n'
    "  <bundleType>AGENT</bundleType>\n"
    "</AiAuthoringBundle>\n"
)

_PROJECT_JSON = {
    "packageDirectories": [{"path": "force-app", "default": True}],
    "name": "sfvb-agent-validation",
    "namespace": "",
    "sfdcLoginUrl": "https://login.salesforce.com",
    "sourceApiVersion": "67.0",
}


class CompileOutcome(Enum):
    """Why a bundle is or is not known to compile.

    ``SKIPPED`` exists so that "we did not ask Salesforce" can never be confused
    with "Salesforce said yes". CI has no org; without a third state the check
    would either fail there permanently or lie.
    """

    COMPILED = "compiled"  # The compiler accepted it: exit 0, success true
    REJECTED = "rejected"  # The compiler refused it and said why
    SKIPPED = "skipped"  # No org configured; the compiler was never asked
    BLOCKED = "blocked"  # Org alias is out of scope; refused locally
    ERROR = "error"  # CLI missing, timed out, or returned unparseable output


@dataclass(slots=True)
class CompileResult:
    """The verdict on one bundle, with the compiler's own error text."""

    outcome: CompileOutcome
    detail: str = ""
    errors: list[str] = field(default_factory=list)
    command: str = ""  # The argv actually run, for an operator to reproduce

    @property
    def compiled(self) -> bool:
        """True only when Salesforce accepted the bundle.

        Deliberately false for SKIPPED: an unasked question is not a pass.
        """
        return self.outcome is CompileOutcome.COMPILED

    @property
    def is_failure(self) -> bool:
        """True when this should fail a build.

        SKIPPED is not a failure — it is the expected state offline. BLOCKED is,
        because it means a caller aimed at an out-of-scope org.
        """
        return self.outcome in (
            CompileOutcome.REJECTED,
            CompileOutcome.BLOCKED,
            CompileOutcome.ERROR,
        )


def _default_runner(cmd: list[str], *, cwd: Path, timeout: int) -> Any:
    """The real subprocess boundary. Injected as `runner` so tests stay offline."""
    return subprocess.run(
        cmd,
        cwd=str(cwd),
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


def org_is_forbidden(org_alias: str) -> bool:
    """True for the two orgs that are out of scope for this project entirely."""
    return org_alias.strip().lower() in _FORBIDDEN_ORG_ALIASES


def write_validation_project(
    project_dir: Path, agent_source: str, *, developer_name: str
) -> Path:
    """Scaffold the project layout ``sf agent validate authoring-bundle`` requires.

    The command needs a project root and locates a bundle by API name under a
    package directory. Writing the source byte-for-byte matters: the verdict has
    to describe the caller's file, not a normalised copy of it.

    Args:
        project_dir: Directory to become the project root. Created if absent.
        agent_source: The complete ``.agent`` file body.
        developer_name: Bundle API name; also the file stem the CLI resolves.

    Returns:
        The bundle directory that was written.
    """
    project_dir = Path(project_dir)
    bundle_dir = (
        project_dir / "force-app" / "main" / "default" / "aiAuthoringBundles" / developer_name
    )
    bundle_dir.mkdir(parents=True, exist_ok=True)

    project_file = project_dir / "sfdx-project.json"
    if not project_file.is_file():
        project_file.write_text(json.dumps(_PROJECT_JSON, indent=2) + "\n", encoding="utf-8")

    # newline="" so the emitted bytes survive verbatim on every platform.
    (bundle_dir / f"{developer_name}.agent").write_text(
        agent_source, encoding="utf-8", newline=""
    )
    (bundle_dir / f"{developer_name}.bundle-meta.xml").write_text(
        _BUNDLE_META_XML, encoding="utf-8"
    )
    return bundle_dir


# A rejection has to come from the compiler. These mark a failure that happened
# before compilation — a bad invocation or a bundle the CLI could not locate —
# and reporting them as REJECTED would send an operator to fix an emitter that
# was never broken. Measured: passing `--name` yields "Nonexistent flag: --name".
_HARNESS_ERROR_MARKERS = (
    "nonexistent flag",
    "see more help with --help",
    "no authoring bundle found",
    "requires the project",
    "no project found",
    "must be run from within a project",
    "no authorization information",
    "no default environment",
    "not authorized",
    "expired access/refresh token",
)

# Error class names oclif reports for the same category of problem.
_HARNESS_ERROR_NAMES = (
    "nonexistentflagerror",
    "noauthoringbundlefounderror",
    "requiresprojecterror",
    "nodefaultenvvalue",
    "namedorgnotfound",
)


def _looks_like_harness_error(payload: dict[str, Any], errors: list[str]) -> str | None:
    """Return a reason when the failure is the invocation, not the Agent Script."""
    name = str(payload.get("name") or "").strip().lower()
    if name.replace("_", "") in _HARNESS_ERROR_NAMES:
        return str(payload.get("message") or name)

    haystacks = [str(payload.get("message") or "")] + errors
    for text in haystacks:
        low = text.lower()
        for marker in _HARNESS_ERROR_MARKERS:
            if marker in low:
                return text
    return None


def _extract_errors(payload: dict[str, Any]) -> list[str]:
    """Pull the compiler's error strings out of the --json envelope.

    The errors live under ``data`` on this plugin version but ``result`` is the
    conventional key, so both are read. Each entry is a dict with a
    ``description``; anything unexpected is stringified rather than dropped —
    losing an error would turn a rejection into a silent pass.
    """
    container = payload.get("data")
    if not isinstance(container, dict):
        container = payload.get("result")
    if not isinstance(container, dict):
        container = {}

    raw = container.get("errors") or []
    if isinstance(raw, dict):
        raw = [raw]

    errors: list[str] = []
    for entry in raw:
        if isinstance(entry, dict):
            text = entry.get("description") or entry.get("message")
            errors.append(str(text) if text else json.dumps(entry, sort_keys=True))
        else:
            errors.append(str(entry))

    message = payload.get("message")
    if not errors and message:
        errors.append(str(message))
    return errors


def _succeeded(payload: dict[str, Any]) -> bool:
    """Whether the payload reports a successful compile.

    Requires an explicit ``success: true``. A missing field is not success — that
    fails closed, so an envelope change cannot silently green the build.
    """
    for key in ("data", "result"):
        container = payload.get(key)
        if isinstance(container, dict) and "success" in container:
            return container["success"] is True
    return False


def validate_bundle_with_org(
    agent_source: str,
    *,
    developer_name: str,
    org_alias: str | None,
    project_dir: Path,
    timeout: int = DEFAULT_TIMEOUT_SECONDS,
    runner: Callable[..., Any] | None = None,
) -> CompileResult:
    """Ask Salesforce whether ``agent_source`` compiles.

    Skips cleanly when ``org_alias`` is None so this can sit in a pipeline that
    also runs offline. The skip is reported as :attr:`CompileOutcome.SKIPPED` and
    never as a pass.

    No credential is ever placed on the argv — only the org alias, which the CLI
    resolves itself. Anything on argv is world-readable through ``ps``.

    Args:
        agent_source: The complete ``.agent`` file body to compile.
        developer_name: Bundle API name to validate.
        org_alias: Org alias to authenticate with, or None to skip.
        project_dir: Directory to scaffold the throwaway project into.
        timeout: Seconds to allow the CLI. The call is a network round trip.
        runner: Subprocess boundary, injected by tests.

    Returns:
        A :class:`CompileResult` carrying the compiler's verbatim errors.
    """
    if not org_alias:
        return CompileResult(
            outcome=CompileOutcome.SKIPPED,
            detail=(
                "No org configured, so the Agent Script compiler was not asked. "
                "This is not a pass: run with an org alias to get Salesforce's verdict."
            ),
        )

    if org_is_forbidden(org_alias):
        return CompileResult(
            outcome=CompileOutcome.BLOCKED,
            detail=(
                f"Org alias {org_alias!r} is out of scope for this project and was "
                "refused before any CLI call."
            ),
        )

    run = runner or _default_runner
    project_dir = Path(project_dir)

    try:
        write_validation_project(project_dir, agent_source, developer_name=developer_name)
    except OSError as exc:
        return CompileResult(
            outcome=CompileOutcome.ERROR,
            detail=f"Could not scaffold a validation project at {project_dir}: {exc}",
        )

    cmd = [
        "sf",
        "agent",
        "validate",
        "authoring-bundle",
        "--target-org",
        org_alias,
        # `--api-name`, not `--name`. The latter is the intuitive guess and the
        # CLI rejects it with "Nonexistent flag: --name".
        "--api-name",
        developer_name,
        "--json",
    ]
    printable = " ".join(cmd)

    try:
        completed = run(cmd, cwd=project_dir, timeout=timeout)
    except FileNotFoundError:
        return CompileResult(
            outcome=CompileOutcome.ERROR,
            detail=(
                "The `sf` CLI was not found on PATH, so the bundle was not compiled. "
                "Install the Salesforce CLI and the agent plugin."
            ),
            command=printable,
        )
    except subprocess.TimeoutExpired:
        return CompileResult(
            outcome=CompileOutcome.ERROR,
            detail=f"`sf agent validate authoring-bundle` timed out after {timeout}s.",
            command=printable,
        )
    except Exception as exc:  # noqa: BLE001 - any failure here must fail closed
        return CompileResult(
            outcome=CompileOutcome.ERROR,
            detail=f"Could not run the Salesforce CLI: {exc}",
            command=printable,
        )

    stdout = getattr(completed, "stdout", "") or ""
    stderr = getattr(completed, "stderr", "") or ""
    cleaned = _ANSI.sub("", stdout)
    brace = cleaned.find("{")

    if brace == -1:
        return CompileResult(
            outcome=CompileOutcome.ERROR,
            detail=(
                "The CLI returned no JSON payload, so no verdict could be read. "
                f"stderr: {_ANSI.sub('', stderr).strip()[:400]}"
            ),
            command=printable,
        )

    try:
        payload = json.loads(cleaned[brace:])
    except (json.JSONDecodeError, ValueError) as exc:
        return CompileResult(
            outcome=CompileOutcome.ERROR,
            detail=f"Could not parse the CLI's JSON output ({exc}); treating as not compiled.",
            command=printable,
        )

    if not isinstance(payload, dict):
        return CompileResult(
            outcome=CompileOutcome.ERROR,
            detail="The CLI's JSON output was not an object; treating as not compiled.",
            command=printable,
        )

    errors = _extract_errors(payload)

    if _succeeded(payload) and not errors:
        return CompileResult(
            outcome=CompileOutcome.COMPILED,
            detail="Salesforce compiled the Agent Script: exit 0, success true.",
            command=printable,
        )

    # Before calling anything a rejection, make sure the compiler is what refused.
    harness_reason = _looks_like_harness_error(payload, errors)
    if harness_reason:
        return CompileResult(
            outcome=CompileOutcome.ERROR,
            detail=(
                "The CLI failed before compiling the Agent Script, so this is not a "
                f"verdict on the bundle: {harness_reason.strip()[:400]}"
            ),
            command=printable,
        )

    if errors:
        return CompileResult(
            outcome=CompileOutcome.REJECTED,
            detail=f"Salesforce rejected the Agent Script with {len(errors)} error(s).",
            errors=errors,
            command=printable,
        )

    # Neither an explicit success nor any error text: fail closed rather than
    # guess. A verdict we cannot read is not a verdict.
    return CompileResult(
        outcome=CompileOutcome.ERROR,
        detail=(
            "The CLI reported neither success nor any error, so the bundle's status "
            "is unknown. Treating as not compiled."
        ),
        command=printable,
    )

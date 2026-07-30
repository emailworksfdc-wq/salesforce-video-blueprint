"""Deploy an Agentforce authoring bundle to a sandbox org.

Combines the two-step Salesforce workflow into a single callable:

1. ``sf agent validate authoring-bundle`` — compiles the Agent Script against the
   org's compile endpoint. This is the same call that :mod:`org_validation` makes
   when building the bundle, but called explicitly here so the deploy gate can
   refuse on a compiler rejection rather than proceeding to deploy a known-broken
   bundle.

2. ``sf project deploy start`` — deploys the bundle as ``AiAuthoringBundle``
   metadata. Called only when validation passes or when ``--validate-only`` is
   false and the caller opts in. A ``--dry-run`` flag mirrors Salesforce's own
   ``--dry-run`` option (checks permission and metadata without committing).

Why the two-step exists (from measurements on 2026-07-26, AFT3):

- ``sf agent validate authoring-bundle`` does NOT deploy — it POSTs to a compile
  endpoint; the org connection is auth only. Ten validate probes left no metadata.
- ``sf project deploy start`` deploys. Deploying without validating first has
  worked on the single measured case, but no guarantee was ever stated by
  Salesforce about which errors the deploy surface catches versus which the
  compiler catches. Cheap to always validate first.
- ``sf agent validate`` has ``--json`` output; ``sf project deploy start`` has
  ``--json`` too. Both are parsed here so the caller gets machine-readable errors.

Security invariants:

- Every org alias is checked against :func:`org_validation.org_is_forbidden`
  before any subprocess is spawned. A forbidden alias causes an immediate
  ``DeployResult`` with ``outcome=DeployOutcome.BLOCKED`` and NO subprocess.
- Credentials never appear on argv. The CLI resolves the org alias itself.
- No token, session id, or ``frontdoor.jsp`` URL is logged or returned.
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

from .org_denylist import is_org_blocked
from .org_validation import (
    CompileOutcome,
    CompileResult,
    validate_bundle_with_org,
    write_validation_project,
)

_ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
DEFAULT_TIMEOUT_SECONDS = 180  # deploy is slower than validate


class DeployOutcome(Enum):
    """Why a deploy did or did not happen.

    ``SKIPPED`` distinguishes "we did not try" from "we tried and failed". A
    ``--validate-only`` run is VALIDATED (compiler said yes) or REJECTED, never
    DEPLOYED or SKIPPED.
    """

    DEPLOYED = "deployed"       # Both validate and deploy succeeded
    VALIDATED = "validated"     # validate-only: compiler accepted the bundle
    REJECTED = "rejected"       # Compiler refused; deploy was not attempted
    DRY_RUN = "dry_run"         # Deploy --dry-run passed
    BLOCKED = "blocked"         # Org alias is out of scope; refused before CLI
    ERROR = "error"             # CLI missing, timeout, or unparseable output
    SKIPPED = "skipped"         # No org alias supplied


@dataclass(slots=True)
class DeployResult:
    """The complete outcome of a validate-then-deploy operation.

    Carries both the compiler verdict and the deploy verdict (when attempted),
    so callers can see which step failed without re-reading logs.
    """

    outcome: DeployOutcome
    org_alias: str = ""
    developer_name: str = ""
    detail: str = ""

    #: Compiler errors from ``sf agent validate authoring-bundle``, verbatim.
    validation_errors: list[str] = field(default_factory=list)

    #: Deploy errors from ``sf project deploy start``, verbatim.
    deploy_errors: list[str] = field(default_factory=list)

    #: The argv strings that were run (for reproduction). Never contains tokens.
    validate_command: str = ""
    deploy_command: str = ""

    #: True only when ``sf project deploy start`` confirmed a successful deploy.
    deployed: bool = False

    #: True when the bundle compiled (validation succeeded), regardless of deploy.
    compiled: bool = False

    #: Whether this was a dry run (Salesforce's own ``--dry-run`` flag).
    dry_run: bool = False

    @property
    def succeeded(self) -> bool:
        """True for DEPLOYED and DRY_RUN (dry run is also a success outcome)."""
        return self.outcome in (DeployOutcome.DEPLOYED, DeployOutcome.DRY_RUN)


def _default_runner(cmd: list[str], *, cwd: Path, timeout: int) -> Any:
    """Real subprocess boundary. Injected by tests to stay offline."""
    return subprocess.run(
        cmd,
        cwd=str(cwd),
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


def _parse_deploy_output(stdout: str, stderr: str) -> tuple[bool, list[str], str]:
    """Extract success flag and errors from ``sf project deploy start --json``.

    Returns (succeeded, errors, detail). Fails closed on unparseable output:
    a deploy whose success cannot be confirmed is not reported as a success.
    """
    cleaned = _ANSI.sub("", stdout)
    brace = cleaned.find("{")
    if brace == -1:
        detail = _ANSI.sub("", stderr).strip()[:400] or "No JSON in CLI output"
        return False, [detail], detail

    try:
        payload = json.loads(cleaned[brace:])
    except (json.JSONDecodeError, ValueError):
        return False, ["Could not parse CLI output as JSON"], "parse error"

    if not isinstance(payload, dict):
        return False, ["CLI output was not a JSON object"], "not an object"

    # Success: result.status == 0 or result.success == true
    result = payload.get("result") or {}
    if isinstance(result, dict):
        if result.get("success") is True:
            return True, [], "deploy succeeded"
        if result.get("status") == 0:
            return True, [], "deploy succeeded (status 0)"

    # Collect errors from the standard envelope shapes.
    # sf project deploy start uses several nested layouts depending on version:
    #   result.failures[{message}]
    #   result.details.failures[{message|problem}]
    #   result.deployDetails.componentFailures[{problem}]
    #   data.failures[...]
    errors: list[str] = []

    def _collect_failure_entries(raw: object) -> None:
        if not isinstance(raw, list):
            return
        for entry in raw:
            if isinstance(entry, dict):
                msg = (
                    entry.get("message")
                    or entry.get("problem")
                    or entry.get("description")
                    or ""
                )
                if msg:
                    errors.append(str(msg))
            else:
                errors.append(str(entry))

    for key in ("result", "data"):
        container = payload.get(key)
        if not isinstance(container, dict):
            continue
        # Flat failures list
        _collect_failure_entries(container.get("failures"))
        # Nested: result.details.failures or result.deployDetails.componentFailures
        for nested_key in ("details", "deployDetails"):
            nested = container.get(nested_key)
            if isinstance(nested, dict):
                _collect_failure_entries(nested.get("failures"))
                _collect_failure_entries(nested.get("componentFailures"))

    if not errors:
        msg = payload.get("message") or ""
        if msg:
            errors.append(str(msg))

    detail = f"{len(errors)} error(s)" if errors else "deploy failed (unknown reason)"
    return False, errors, detail


def deploy_bundle(
    agent_source: str,
    *,
    developer_name: str,
    org_alias: str | None,
    project_dir: Path,
    validate_only: bool = False,
    dry_run: bool = False,
    timeout: int = DEFAULT_TIMEOUT_SECONDS,
    runner: Callable[..., Any] | None = None,
) -> DeployResult:
    """Validate and optionally deploy an Agentforce authoring bundle.

    The two-step flow:
    1. ``sf agent validate authoring-bundle`` — always runs when an org is given.
       On rejection the result is REJECTED and the deploy is skipped.
    2. ``sf project deploy start`` — runs only when step 1 passes and
       ``validate_only`` is False.

    Args:
        agent_source: The complete ``.agent`` file body.
        developer_name: Bundle API name (also the SFDX bundle directory).
        org_alias: Org alias to target. Checked against the deny-list before any
            CLI call. When None the result is SKIPPED.
        project_dir: Scratch directory for the throwaway SFDX project.
        validate_only: When True, stop after validation and return VALIDATED.
        dry_run: Pass ``--dry-run`` to ``sf project deploy start``. Has no effect
            when ``validate_only`` is True. Result is DRY_RUN on success.
        timeout: Seconds to allow per CLI call.
        runner: Subprocess boundary, injected by tests.

    Returns:
        A :class:`DeployResult` describing what happened and why.
    """
    if not org_alias:
        return DeployResult(
            outcome=DeployOutcome.SKIPPED,
            developer_name=developer_name,
            detail=(
                "No org alias supplied. Validate with an org alias to get Salesforce's "
                "verdict; deploy requires one too."
            ),
        )

    if is_org_blocked(org_alias):
        return DeployResult(
            outcome=DeployOutcome.BLOCKED,
            org_alias=org_alias,
            developer_name=developer_name,
            detail=(
                f"Org alias {org_alias!r} is permanently out of scope for this project "
                "and was refused before any CLI call."
            ),
        )

    run = runner or _default_runner
    project_dir = Path(project_dir)

    # --- Step 1: validate --------------------------------------------------
    compile_result: CompileResult = validate_bundle_with_org(
        agent_source,
        developer_name=developer_name,
        org_alias=org_alias,
        project_dir=project_dir,
        timeout=timeout,
        runner=runner,
    )

    if compile_result.outcome is CompileOutcome.REJECTED:
        return DeployResult(
            outcome=DeployOutcome.REJECTED,
            org_alias=org_alias,
            developer_name=developer_name,
            detail=(
                f"Salesforce rejected the bundle with "
                f"{len(compile_result.errors)} error(s). Deploy was not attempted."
            ),
            validation_errors=list(compile_result.errors),
            validate_command=compile_result.command,
            compiled=False,
        )

    if compile_result.outcome in (CompileOutcome.ERROR, CompileOutcome.BLOCKED):
        return DeployResult(
            outcome=DeployOutcome.ERROR,
            org_alias=org_alias,
            developer_name=developer_name,
            detail=compile_result.detail,
            validation_errors=list(compile_result.errors),
            validate_command=compile_result.command,
            compiled=False,
        )

    # Compiled or skipped (no org — but we checked org above, so this is COMPILED
    # or the validate call itself was skipped for some reason).
    compiled = compile_result.outcome is CompileOutcome.COMPILED

    if validate_only:
        return DeployResult(
            outcome=DeployOutcome.VALIDATED,
            org_alias=org_alias,
            developer_name=developer_name,
            detail="Bundle compiled. Deploy not attempted (--validate-only).",
            validate_command=compile_result.command,
            compiled=compiled,
        )

    # --- Step 2: deploy ----------------------------------------------------
    # The project was already scaffolded by validate_bundle_with_org, but we
    # call write_validation_project again to be safe — it is idempotent.
    try:
        write_validation_project(project_dir, agent_source, developer_name=developer_name)
    except OSError as exc:
        return DeployResult(
            outcome=DeployOutcome.ERROR,
            org_alias=org_alias,
            developer_name=developer_name,
            detail=f"Could not scaffold deploy project at {project_dir}: {exc}",
            validate_command=compile_result.command,
            compiled=compiled,
        )

    deploy_cmd = [
        "sf",
        "project",
        "deploy",
        "start",
        "--target-org",
        org_alias,
        "--source-dir",
        str(
            project_dir
            / "force-app"
            / "main"
            / "default"
            / "aiAuthoringBundles"
            / developer_name
        ),
        "--json",
    ]
    if dry_run:
        deploy_cmd.append("--dry-run")

    printable_deploy = " ".join(deploy_cmd)

    try:
        completed = run(deploy_cmd, cwd=project_dir, timeout=timeout)
    except FileNotFoundError:
        return DeployResult(
            outcome=DeployOutcome.ERROR,
            org_alias=org_alias,
            developer_name=developer_name,
            detail="The `sf` CLI was not found on PATH.",
            validate_command=compile_result.command,
            deploy_command=printable_deploy,
            compiled=compiled,
        )
    except subprocess.TimeoutExpired:
        return DeployResult(
            outcome=DeployOutcome.ERROR,
            org_alias=org_alias,
            developer_name=developer_name,
            detail=f"`sf project deploy start` timed out after {timeout}s.",
            validate_command=compile_result.command,
            deploy_command=printable_deploy,
            compiled=compiled,
        )
    except Exception as exc:  # noqa: BLE001
        return DeployResult(
            outcome=DeployOutcome.ERROR,
            org_alias=org_alias,
            developer_name=developer_name,
            detail=f"Could not run the Salesforce CLI: {exc}",
            validate_command=compile_result.command,
            deploy_command=printable_deploy,
            compiled=compiled,
        )

    stdout = getattr(completed, "stdout", "") or ""
    stderr = getattr(completed, "stderr", "") or ""
    success, errors, detail = _parse_deploy_output(stdout, stderr)

    if success:
        return DeployResult(
            outcome=DeployOutcome.DRY_RUN if dry_run else DeployOutcome.DEPLOYED,
            org_alias=org_alias,
            developer_name=developer_name,
            detail=detail,
            validate_command=compile_result.command,
            deploy_command=printable_deploy,
            deployed=not dry_run,
            compiled=compiled,
            dry_run=dry_run,
        )

    return DeployResult(
        outcome=DeployOutcome.ERROR,
        org_alias=org_alias,
        developer_name=developer_name,
        detail=detail,
        deploy_errors=errors,
        validate_command=compile_result.command,
        deploy_command=printable_deploy,
        compiled=compiled,
        dry_run=dry_run,
    )

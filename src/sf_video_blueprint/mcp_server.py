"""MCP server exposing the blueprint pipeline to any MCP-capable AI harness.

Runs over stdio, so it works with Claude Desktop, Claude Code, Cursor, Windsurf,
Continue, or anything else that speaks the Model Context Protocol.

    sf-blueprint-mcp

Design notes worth knowing before adding a tool here:

**Most tools are read-only and offline.** Nothing in the offline tools contacts a
Salesforce org, launches a browser, or writes outside an explicitly supplied output
path. An agent driving this server cannot mutate an org through those tools. That
is deliberate: an LLM deciding on its own to replay recorded clicks against a live
org is precisely the failure this project should not enable.

**run_stage5_round and run_iterate contact a live org.** Both tools shell out to
``sf agent test run-eval``, which sends test cases to a real Salesforce agent. They
refuse forbidden org aliases (PPCDM, PPCaccenture) before any network call is made.
Feedback stamped with a source outside :data:`stage5.REAL_FEEDBACK_SOURCES` is
blocking and is never reported as evidence of a passing agent.

**Tools return honest structure, not prose.** Each result carries provenance and,
where relevant, the reasons a score was withheld. An agent that reads only the
headline number would still see `passed: false` and the blocking issue next to it.

**Nothing here can launder mock evidence into real evidence.** Telemetry is always
mocked because collecting it requires a live org, so `evidence_is_real` is always
false for specs produced through this server. The score gate treats those specs
exactly as it treats any other mock run.

Deviation from `docs/mcp-product-spec.md`: that document specifies a generic
`workflow.plan/execute/status/cancel` tool set with idempotency keys and run-state
tracking. This server exposes the pipeline's actual capabilities instead, because
every operation is synchronous, offline, and side-effect free — there is no
long-running mutating job for which a run registry or idempotency key would mean
anything. The response envelope, error taxonomy, and structured-log fields from
that spec ARE implemented.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import tempfile
import time
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4

from .markers import scan_text
from .naming import router_action_name, snake_case, subagent_name, topic_api_name
from .pipeline import CaptureRejected, run_pipeline
from .spec_score import PASS_THRESHOLD

try:
    from mcp.server.fastmcp import FastMCP
except ModuleNotFoundError as exc:  # pragma: no cover - exercised by the CLI guard
    raise SystemExit(
        "The MCP server needs the 'mcp' package, which is an optional extra.\n"
        "Install it with:  pip install 'sf-video-blueprint[mcp]'"
    ) from exc


SERVER_NAME = "sf-video-blueprint"


def _server_version() -> str:
    """Read the installed distribution version instead of hardcoding one.

    A literal here silently drifts from `pyproject.toml` the first time the
    version is bumped, and the envelope's `serverVersion` is what a client uses to
    tell builds apart. Falls back when running from a source tree that was never
    installed.
    """
    from importlib.metadata import PackageNotFoundError, version

    try:
        return version("sf-video-blueprint")
    except PackageNotFoundError:
        return "0.0.0+unknown"


SERVER_VERSION = _server_version()

# What a client reports as the server version comes from the MCP SDK, not from
# here — FastMCP takes no `version` argument, so `initialize` responds with the
# SDK's own version. Our version travels in the response envelope's
# `serverVersion` field and in `health()`, which is why both exist.
SERVER_INSTRUCTIONS = """\
Turns a recorded Salesforce process (a dom_capture.jsonl click trace) into a
conversational agent spec, an Agentforce Agent Script bundle, and test specs.

Call `health` first: it lists this server's real limitations, several of which
change how you should interpret the output.

Most tools are offline and read-only. No tool in that set contacts a Salesforce
org, launches a browser, or modifies anything outside a path you explicitly pass.
Three tools are exceptions: `run_stage5_round` and `run_iterate` shell out to
`sf agent test run-eval`, which sends test cases to a live agent in the org you
supply. `run_deploy` validates and deploys a bundle to the org you supply; use
`validate_only=True` for a read-only compile check. All three refuse PPCDM and
PPCaccenture before any network call.

Two things to carry into how you report results:

1. Telemetry is always mocked here, because collecting real telemetry needs a
   live org. Every derived spec is therefore stamped `telemetry_source: "mock"`
   and CANNOT pass the quality gate. A `passed: false` with a `mock` provenance is
   the expected outcome, not a failure to work around. Do not describe such a spec
   as validated, verified, or production-ready.

2. `locallyValid: true` means a bundle passed this project's own structural
   checks, not Salesforce's — and those checks were measurably wrong once, on a
   file the compiler rejected with 24 errors while `validate_locally` reported
   zero findings on it. Salesforce's verdict comes only from `emit_agent_bundle`
   with an `org_alias`, reported under `orgValidation`. Treat
   `outcome: "skipped"` as "not asked", never as a pass. Note also that compiling
   proves syntax only: no agent has been published and nothing has checked
   whether a compiled agent behaves as its spec describes.

When a spec scores low, the fix is to capture better evidence — a recording that
exercises a failure path, or a live-mode run with real telemetry. Never suggest
lowering a score threshold.\
"""

log = logging.getLogger("sf_video_blueprint.mcp")


def _configure_logging() -> None:
    """Send logs to stderr. Called from `main`, never at import.

    stdout is the JSON-RPC transport — a log record written there corrupts the
    protocol stream and the client disconnects with a parse error. `basicConfig`
    defaults to stderr, but only by default, so it is passed explicitly.

    Deliberately not called at module scope: importing this module must not
    reconfigure the root logger of a host application that happens to import it.
    """
    logging.basicConfig(
        level=os.environ.get("SF_BLUEPRINT_MCP_LOG_LEVEL", "INFO").upper(),
        stream=sys.stderr,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

mcp = FastMCP(SERVER_NAME, instructions=SERVER_INSTRUCTIONS)


# ---------------------------------------------------------------------------
# Response envelope and error taxonomy (from docs/mcp-product-spec.md)
# ---------------------------------------------------------------------------

# Categories are the spec's taxonomy. Only the ones an offline, read-only server
# can actually produce are used; the rest would be decoration.
ERROR_VALIDATION = "VALIDATION"
ERROR_NOT_FOUND = "NOT_FOUND"
ERROR_DEPENDENCY = "DEPENDENCY"
ERROR_INTERNAL = "INTERNAL"


def _ok(tool: str, request_id: str, started: float, **payload: Any) -> dict[str, Any]:
    duration_ms = int((time.monotonic() - started) * 1000)
    log.info(
        json.dumps(
            {
                "requestId": request_id,
                "tool": tool,
                "durationMs": duration_ms,
                "outcome": "ok",
            }
        )
    )
    return {
        "ok": True,
        "requestId": request_id,
        "serverVersion": SERVER_VERSION,
        "durationMs": duration_ms,
        **payload,
    }


def _err(
    tool: str,
    request_id: str,
    started: float,
    code: str,
    message: str,
    **detail: Any,
) -> dict[str, Any]:
    duration_ms = int((time.monotonic() - started) * 1000)
    log.warning(
        json.dumps(
            {
                "requestId": request_id,
                "tool": tool,
                "durationMs": duration_ms,
                "outcome": "error",
                "error": {"code": code},
            }
        )
    )
    return {
        "ok": False,
        "requestId": request_id,
        "serverVersion": SERVER_VERSION,
        "durationMs": duration_ms,
        "error": {"code": code, "message": message, **detail},
    }


def _resolve(path_str: str) -> Path:
    """Expand and resolve a client-supplied path.

    No sandbox root is enforced. An MCP server runs with the user's own
    privileges on the user's own machine and the client already chose to launch
    it, so a path allow-list here would be security theatre. Do not read this as
    "paths are validated".
    """
    return Path(path_str).expanduser().resolve()


def _jsonable(value: Any) -> Any:
    """Convert dataclasses and sets into JSON-safe structures."""
    if is_dataclass(value) and not isinstance(value, type):
        return {k: _jsonable(v) for k, v in asdict(value).items()}
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_jsonable(v) for v in value]
    return value


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------


@mcp.tool()
def health() -> dict[str, Any]:
    """Report server version, capabilities, and the project's honest limitations.

    Call this first. The `limitations` field is not boilerplate — it names the
    stages that do not work, so an agent does not plan around capabilities that
    are absent.
    """
    request_id = uuid4().hex[:12]
    started = time.monotonic()
    return _ok(
        "health",
        request_id,
        started,
        server=SERVER_NAME,
        status="ok",
        passThreshold=PASS_THRESHOLD,
        tools=[
            "health",
            "validate_capture",
            "derive_spec",
            "score_spec",
            "emit_agent_bundle",
            "emit_test_spec",
            "preview_api_names",
            "run_stage5_round",
            "run_iterate",
            "run_deploy",
        ],
        capabilities={
            # Most tools are offline. Three tools contact an org when given an
            # org_alias: emit_agent_bundle (compiles, never deploys),
            # run_stage5_round, and run_iterate (both run sf agent test run-eval
            # against a live agent). The two stage-5 tools refuse PPCDM and
            # PPCaccenture before any network call is attempted.
            "offline": (
                "by default; emit_agent_bundle(org_alias=...) compiles against an org; "
                "run_stage5_round and run_iterate call sf agent test run-eval against a live org; "
                "run_deploy validates and deploys to an org when given org_alias"
            ),
            "readOnly": (
                "mostly; run_deploy(org_alias=...) deploys metadata to the target org "
                "when not given validate_only=True or dry_run=True"
            ),
            "contactsSalesforceOrg": (
                "emit_agent_bundle when given org_alias; "
                "run_stage5_round and run_iterate always (they require org_alias); "
                "run_deploy when given org_alias"
            ),
            "launchesBrowser": False,
            "telemetry": "mock-only — collecting real telemetry needs a live org",
        },
        # Each entry is parenthesised rather than relying on bare implicit
        # concatenation: in a list of strings, a single missing comma silently
        # merges two limitations into one and drops a disclosure.
        limitations=[
            (
                "An emitted .agent bundle may be syntactically invalid. "
                "`sf agent validate authoring-bundle` has been run against this "
                "project's output exactly once (2026-07-26, bundle "
                "SFVB_TEST_Case_Triage, exit 0), for one intent shape on one org "
                "and CLI version. It passed only after an emitter fix: the "
                "compiler rejected the pre-fix bundle with 24 CompilationErrors. "
                "Any other spec shape is unvalidated. A bundle is only known "
                "to compile when emit_agent_bundle is called with an org_alias "
                "and reports orgValidation.compiled true; without one, nothing "
                "here has Salesforce's verdict. Run the CLI — it needs no deploy."
            ),
            (
                "`locallyValid: true` is not org validation. validate_locally() "
                "reported zero findings on the exact file the Salesforce compiler "
                "rejected with 24 errors, so it is blind to that error class."
            ),
            (
                "Compiling is syntax, not semantics. No agent built from this "
                "project's output has been published, and nothing has verified that "
                "a compiled agent behaves as its spec describes. `[NEEDS EVIDENCE: "
                "...]` markers compile successfully, so the compiler is not a safety "
                "net for evidence quality either."
            ),
            (
                "Telemetry is always mocked here, so every derived spec is stamped "
                "telemetry_source=mock and cannot pass the score gate. That is "
                "correct behaviour, not a bug to work around."
            ),
            (
                "Video files are not supported. The video extractor is a stub that "
                "returns one placeholder step for any input. Use a dom_capture.jsonl."
            ),
            (
                "Capture ingest can silently discard events: the integrity gate only "
                "refuses at >=50% loss, so check skipped_line_count on every result."
            ),
            (
                "No agent actions (@apex.*/@flow.*) are ever emitted, by design — "
                "referencing an action that may not exist in the target org produces "
                "a bundle that fails to deploy for invisible reasons."
            ),
        ],
    )


@mcp.tool()
def validate_capture(capture_path: str) -> dict[str, Any]:
    """Check a dom_capture.jsonl for integrity problems without deriving a spec.

    Use this before `derive_spec` to see what a recording contains and whether it
    lost anything. Reports parsed event count, discarded lines with reasons, and
    any security or data-loss findings.

    Findings prefixed `SECURITY CRITICAL:` or `DATA LOSS:` mean `derive_spec` will
    refuse the file. A redaction leak is reported without echoing the offending
    value.

    Args:
        capture_path: Path to a dom_capture.jsonl trace.
    """
    request_id = uuid4().hex[:12]
    started = time.monotonic()
    tool = "validate_capture"

    path = _resolve(capture_path)
    if not path.is_file():
        return _err(tool, request_id, started, ERROR_NOT_FOUND, f"No file at {path}")

    try:
        from .dom_capture import parse_capture_file, validate_trace

        trace = parse_capture_file(path)
        findings = validate_trace(trace)
    except Exception as exc:  # noqa: BLE001 - surface parse failures as data, not a crash
        return _err(
            tool, request_id, started, ERROR_VALIDATION, f"Could not parse capture: {exc}"
        )

    fatal = [f for f in findings if f.startswith(("SECURITY CRITICAL:", "DATA LOSS:"))]
    parsed = len(trace.events)
    skipped = len(trace.skipped_lines)
    total = parsed + skipped

    return _ok(
        tool,
        request_id,
        started,
        path=str(path),
        eventsParsed=parsed,
        skippedLineCount=skipped,
        skippedLines=[{"line": n, "reason": r} for n, r in trace.skipped_lines],
        lossRatio=round(skipped / total, 4) if total else 0.0,
        warnings=list(trace.warnings),
        findings=list(findings),
        fatalFindings=fatal,
        wouldBeRejected=bool(fatal),
        hasManifest=trace.manifest is not None,
        note=(
            "A non-zero skippedLineCount means part of the recording was discarded. "
            "The integrity gate only refuses at >=50% loss, so a smaller loss still "
            "yields a spec that is stamped as real dom-capture evidence."
        ),
    )


@mcp.tool()
def derive_spec(
    capture_path: str,
    org_url: str = "https://example.my.salesforce.com",
    output_path: str | None = None,
) -> dict[str, Any]:
    """Derive a conversational agent spec from a recorded process, and score it.

    This is the core tool. It parses the capture, extracts actions, correlates
    them, derives intent/entities/orchestration/guardrails, and scores the result.

    Offline and side-effect free: no org is contacted. Telemetry is mocked, so the
    returned spec is always stamped `telemetry_source: "mock"` and will not pass
    the score gate. Read `blocking_issues` for why, and `recommendations` for what
    additional recording would raise it.

    The spec claims only what the recording proved. Fields the run could not
    observe appear in `unknowns` rather than being filled with plausible values.

    Args:
        capture_path: Path to a dom_capture.jsonl trace.
        org_url: The org the recording came from. Metadata only; never contacted.
        output_path: Optional path to write the spec JSON. Nothing is written when
            omitted.
    """
    request_id = uuid4().hex[:12]
    started = time.monotonic()
    tool = "derive_spec"

    path = _resolve(capture_path)
    if not path.is_file():
        return _err(tool, request_id, started, ERROR_NOT_FOUND, f"No file at {path}")

    try:
        result = run_pipeline(path, org_url=org_url)
    except CaptureRejected as exc:
        return _err(
            tool,
            request_id,
            started,
            ERROR_VALIDATION,
            str(exc),
            findings=exc.findings,
            remedy=(
                "The capture failed integrity validation, so no spec was built. "
                "Run validate_capture for detail. Re-record rather than trying to "
                "force this file through."
            ),
        )
    except Exception as exc:  # noqa: BLE001
        return _err(tool, request_id, started, ERROR_INTERNAL, f"Pipeline failed: {exc}")

    written: str | None = None
    if output_path:
        target = _resolve(output_path)
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            from .spec_builder import write_spec

            write_spec(target, result.spec, result.provenance)
            written = str(target)
        except Exception as exc:  # noqa: BLE001
            return _err(
                tool, request_id, started, ERROR_DEPENDENCY, f"Could not write spec: {exc}"
            )

    summary = result.summary()
    return _ok(
        tool,
        request_id,
        started,
        spec=_jsonable(result.spec),
        **summary,
        writtenTo=written,
    )


@mcp.tool()
def score_spec(spec_path: str) -> dict[str, Any]:
    """Score an existing agent-spec JSON file against the quality gate.

    Seven weighted dimensions summing to 100, pass threshold 75. A spec must
    ALSO be backed by real evidence to pass: one built from mock telemetry or stub
    extraction is capped and blocked no matter how complete it looks.

    Threshold surfing is detected — a spec that scrapes past the total while
    leaving several dimensions near zero is flagged rather than passed.

    Args:
        spec_path: Path to an agent-spec JSON file produced by derive_spec.
    """
    request_id = uuid4().hex[:12]
    started = time.monotonic()
    tool = "score_spec"

    path = _resolve(spec_path)
    if not path.is_file():
        return _err(tool, request_id, started, ERROR_NOT_FOUND, f"No file at {path}")

    try:
        from .spec_score import score_spec_file

        score = score_spec_file(path)
    except Exception as exc:  # noqa: BLE001
        return _err(tool, request_id, started, ERROR_VALIDATION, f"Could not score: {exc}")

    return _ok(
        tool,
        request_id,
        started,
        path=str(path),
        total=score.total,
        # The total a caller should SHOW a human. Capped into the low band whenever a
        # blocking issue is present, because "79/100" reads as near-success for a spec
        # that is not evidence-backed at all. `total` stays raw for callers comparing
        # versions across refinement rounds. See SpecScore.display_total.
        displayTotal=score.display_total,
        maxTotal=score.max_total,
        band=score.band,
        passed=score.passed,
        passThreshold=PASS_THRESHOLD,
        dimensions={
            name: {
                "score": d.score,
                "maxScore": d.max_score,
                "findings": list(d.findings),
                "evidence": list(d.evidence),
            }
            for name, d in score.dimensions.items()
        },
        blockingIssues=list(score.blocking_issues),
        recommendations=list(score.recommendations),
        note=(
            "Do not raise this score by weakening the gate — that is a defect, not "
            "a fix. Raise it by capturing better evidence: real telemetry via a "
            "live-mode run, and a recording that exercises a failure path."
        ),
    )


@mcp.tool()
def emit_agent_bundle(
    capture_path: str,
    developer_name: str,
    agent_label: str,
    output_dir: str | None = None,
    org_alias: str | None = None,
) -> dict[str, Any]:
    """Emit an Agentforce Agent Script (.agent) bundle from a recorded process.

    Produces the .agent source and its .bundle-meta.xml, plus the findings from
    local validation. The emitted agent is deliberately a topic router with no
    @apex.* or @flow.* actions: those namespaces do not exist. The compiler
    rejects `@apex.Foo` with "'apex' is not a valid invocation target" — Apex and
    Flow are reached through a subagent-level `actions:` block whose `target:` is
    `apex://Cls` or `flow://Name`.

    Pass `org_alias` to have Salesforce itself compile the bundle via
    `sf agent validate authoring-bundle`. Without it, `orgValidation.outcome` is
    `skipped`, which is never reported as a pass.

    IMPORTANT: local validation is this project's own opinion, not Salesforce's.
    Measured on 2026-07-26: the Salesforce compiler rejected an emitted bundle with
    24 errors in the derived `reasoning:` block while `validate_locally` reported
    zero findings on that same file. That bug is fixed, but the independence is the
    point — a clean local pass is not evidence. Compile with `org_alias` (or the
    CLI directly) before trusting the result; it needs no deploy.

    Args:
        capture_path: Path to a dom_capture.jsonl trace.
        developer_name: API name for the agent, e.g. `Case_Triage_Agent`.
        agent_label: Human-readable label, e.g. `Case Triage Agent`.
        output_dir: Optional directory to write the bundle into. Returned inline
            when omitted.
        org_alias: Optional org alias to compile against. Read-only: the command
            POSTs the file to the compile endpoint and deploys nothing.
    """
    request_id = uuid4().hex[:12]
    started = time.monotonic()
    tool = "emit_agent_bundle"

    path = _resolve(capture_path)
    if not path.is_file():
        return _err(tool, request_id, started, ERROR_NOT_FOUND, f"No file at {path}")

    # Refused here, before the pipeline runs, so an out-of-scope alias cannot
    # reach the network by any path through this tool.
    from .org_validation import org_is_forbidden

    if org_alias and org_is_forbidden(org_alias):
        return _err(
            tool,
            request_id,
            started,
            ERROR_VALIDATION,
            f"Org alias {org_alias!r} is out of scope for this project and was refused.",
            remedy="Use a Developer Edition or sandbox org you own.",
        )

    try:
        result = run_pipeline(path, org_url="https://example.my.salesforce.com")
    except CaptureRejected as exc:
        return _err(
            tool, request_id, started, ERROR_VALIDATION, str(exc), findings=exc.findings
        )
    except Exception as exc:  # noqa: BLE001
        return _err(tool, request_id, started, ERROR_INTERNAL, f"Pipeline failed: {exc}")

    try:
        from .agent_script import (
            InsufficientEvidenceError,
            build_agent_script,
            build_bundle_meta_xml,
            validate_locally,
        )

        script = build_agent_script(
            result.spec, developer_name=developer_name, agent_label=agent_label
        )
        # Takes no arguments: the AiAuthoringBundle meta file carries only the
        # apiVersion, not the agent's name.
        meta = build_bundle_meta_xml()
        findings = validate_locally(script)
    except InsufficientEvidenceError as exc:
        return _err(
            tool,
            request_id,
            started,
            ERROR_VALIDATION,
            f"Not enough observed evidence to emit a bundle: {exc}",
            remedy="Re-record a fuller session rather than lowering the bar.",
        )
    except Exception as exc:  # noqa: BLE001
        return _err(tool, request_id, started, ERROR_INTERNAL, f"Emit failed: {exc}")

    written: dict[str, str] = {}
    if output_dir:
        target = _resolve(output_dir)
        try:
            target.mkdir(parents=True, exist_ok=True)
            agent_file = target / f"{developer_name}.agent"
            meta_file = target / f"{developer_name}.bundle-meta.xml"
            agent_file.write_text(script, encoding="utf-8")
            meta_file.write_text(meta, encoding="utf-8")
            written = {"agent": str(agent_file), "bundleMeta": str(meta_file)}
        except Exception as exc:  # noqa: BLE001
            return _err(
                tool, request_id, started, ERROR_DEPENDENCY, f"Could not write bundle: {exc}"
            )

    # Ask Salesforce. Uses a throwaway project under the system temp dir because
    # the command requires a project root and resolves the bundle by API name.
    from .org_validation import CompileOutcome, validate_bundle_with_org

    with tempfile.TemporaryDirectory(prefix="sfvb-validate-") as scratch:
        compile_result = validate_bundle_with_org(
            script,
            developer_name=developer_name,
            org_alias=org_alias,
            project_dir=Path(scratch),
        )

    org_validation = {
        "outcome": compile_result.outcome.value,
        "compiled": compile_result.compiled,
        "detail": compile_result.detail,
        "errors": list(compile_result.errors),
        "command": compile_result.command or None,
    }

    if compile_result.outcome is CompileOutcome.COMPILED:
        next_step = (
            "Salesforce compiled this bundle (exit 0, success true). Compilation is "
            "syntax only — nothing here shows the agent behaves correctly."
        )
    elif compile_result.outcome is CompileOutcome.REJECTED:
        next_step = (
            "Salesforce REJECTED this bundle. Fix the emitter against the verbatim "
            "errors in orgValidation.errors; do not hand-edit the output."
        )
    else:
        next_step = (
            "Salesforce was not asked whether this compiles "
            f"({compile_result.outcome.value}). `locallyValid` is this project's own "
            "opinion and has been wrong before. Re-run with org_alias set, or run "
            "`sf agent validate authoring-bundle` yourself."
        )

    return _ok(
        tool,
        request_id,
        started,
        developerName=developer_name,
        agentLabel=agent_label,
        agentScript=script,
        bundleMetaXml=meta,
        localValidationFindings=list(findings),
        locallyValid=not findings,
        orgValidation=org_validation,
        writtenTo=written or None,
        provenance=dict(result.provenance),
        nextStep=next_step,
    )


@mcp.tool()
def emit_test_spec(
    capture_path: str,
    test_name: str,
    subject_name: str,
    dialect: str = "legacy",
) -> dict[str, Any]:
    """Emit an Agentforce test spec derived from a recorded process.

    Two dialects are supported: `legacy` produces an AiEvaluationDefinition, and
    `ngt` produces an AiTestingDefinition. Test topic names are generated by the
    same naming module the agent bundle uses, so a test's expectedTopic matches
    the topic the bundle actually declares.

    Returns the YAML plus per-case derivation notes, including gaps where the
    recording did not supply enough to assert an outcome.

    Args:
        capture_path: Path to a dom_capture.jsonl trace.
        test_name: API name for the test definition.
        subject_name: API name of the agent under test.
        dialect: `legacy` (AiEvaluationDefinition) or `ngt` (AiTestingDefinition).
    """
    request_id = uuid4().hex[:12]
    started = time.monotonic()
    tool = "emit_test_spec"

    if dialect not in ("legacy", "ngt"):
        return _err(
            tool,
            request_id,
            started,
            ERROR_VALIDATION,
            f"dialect must be 'legacy' or 'ngt', got {dialect!r}",
        )

    path = _resolve(capture_path)
    if not path.is_file():
        return _err(tool, request_id, started, ERROR_NOT_FOUND, f"No file at {path}")

    try:
        result = run_pipeline(path, org_url="https://example.my.salesforce.com")
    except CaptureRejected as exc:
        return _err(
            tool, request_id, started, ERROR_VALIDATION, str(exc), findings=exc.findings
        )
    except Exception as exc:  # noqa: BLE001
        return _err(tool, request_id, started, ERROR_INTERNAL, f"Pipeline failed: {exc}")

    try:
        from .eval_spec import (
            build_legacy_test_spec,
            build_ngt_test_spec,
            render_test_spec,
        )

        if dialect == "legacy":
            spec_obj, derivations = build_legacy_test_spec(
                result.spec, name=test_name, subject_name=subject_name
            )
        else:
            spec_obj, derivations = build_ngt_test_spec(
                result.spec, name=test_name, subject_name=subject_name
            )
        yaml_text = render_test_spec(spec_obj)
    except Exception as exc:  # noqa: BLE001
        return _err(tool, request_id, started, ERROR_INTERNAL, f"Emit failed: {exc}")

    return _ok(
        tool,
        request_id,
        started,
        dialect=dialect,
        testName=test_name,
        subjectName=subject_name,
        yaml=yaml_text,
        caseCount=len(derivations),
        derivations=_jsonable(derivations),
        note=(
            "Running these tests requires `sf agent test create/run/results`, which "
            "this project does not yet invoke. The specs are emitted but have never "
            "been executed against an org."
        ),
    )


@mcp.tool()
def preview_api_names(process_description: str) -> dict[str, Any]:
    """Show the Salesforce API names this project would generate for a process.

    Useful before emitting anything: it reveals the topic API name, the subagent
    reference, and the router action name that must stay mutually consistent
    across the agent bundle and its test spec.

    The length budget accounts for the six-character `go_to_` router prefix, so a
    topic name that fits may still be truncated at the router. Both are shown.

    Args:
        process_description: A short process description, e.g. "Update Case Status".
    """
    request_id = uuid4().hex[:12]
    started = time.monotonic()
    tool = "preview_api_names"

    if not process_description.strip():
        return _err(
            tool, request_id, started, ERROR_VALIDATION, "process_description is empty"
        )

    try:
        topic = topic_api_name(process_description)
        subagent = subagent_name(process_description)
        # All three take the raw intent, not each other's output — router_action_name
        # calls subagent_name internally. Passing `subagent` here would normalise
        # twice and could diverge from what the emitter produces.
        router = router_action_name(process_description)
    except Exception as exc:  # noqa: BLE001
        return _err(tool, request_id, started, ERROR_INTERNAL, f"Naming failed: {exc}")

    placeholder_hits = scan_text(process_description)
    return _ok(
        tool,
        request_id,
        started,
        input=process_description,
        topicApiName=topic,
        subagentName=subagent,
        routerActionName=router,
        snakeCase=snake_case(process_description),
        lengths={"topic": len(topic), "subagent": len(subagent), "router": len(router)},
        placeholderMarkersFound=list(placeholder_hits),
        note=(
            "The 80-character API name cap is enforced at "
            "`sf agent publish authoring-bundle`. These names have never been "
            "round-tripped through a real org."
        ),
    )


def _spec_from_json(spec_json: str) -> Any:
    """Deserialize a DerivedAgentSpec from its JSON representation.

    Accepts the on-disk format written by spec_builder.write_spec — the same
    shape that score_spec_file reads — so a caller can pipe the output of
    derive_spec directly into run_stage5_round or run_iterate.

    Raises:
        ValueError: If the JSON is malformed or missing required keys.
    """
    from .spec_builder import DerivedAgentSpec, DerivedEntity, SpecEvidence

    try:
        data = json.loads(spec_json)
    except json.JSONDecodeError as exc:
        raise ValueError(f"spec_json is not valid JSON: {exc}") from exc

    if not isinstance(data, dict):
        raise ValueError(f"spec_json must be a JSON object, got {type(data).__name__}")

    entities = [
        DerivedEntity(
            name=ent.get("name") or "",
            object_api_name=ent.get("object_api_name") or "",
            field_api_name=ent.get("field_api_name") or "",
            evidence=[
                SpecEvidence(source=e.get("source") or "", detail=e.get("detail") or "")
                for e in ent.get("evidence", [])
            ],
        )
        for ent in data.get("entities", [])
    ]
    evidence = [
        SpecEvidence(source=e.get("source") or "", detail=e.get("detail") or "")
        for e in data.get("evidence", [])
    ]
    return DerivedAgentSpec(
        intent=data.get("intent", ""),
        confidence=float(data.get("confidence", 0.0)),
        objects_touched=list(data.get("objects_touched", [])),
        entities=entities,
        orchestration_steps=list(data.get("orchestration_steps", [])),
        guardrails=list(data.get("guardrails", [])),
        failure_handling=list(data.get("failure_handling", [])),
        unknowns=list(data.get("unknowns", [])),
        evidence=evidence,
    )


@mcp.tool()
def run_stage5_round(
    spec_json: str,
    org_alias: str,
    agent_api_name: str,
    test_spec_name: str,
    out_dir: str,
) -> dict[str, Any]:
    """Run one stage-5 round: emit a test spec, run it against a live agent, fold verdicts in.

    This is the tool that actually learns from a real Agentforce agent. It emits a
    legacy AiEvaluationDefinition from the supplied spec, runs it against
    ``agent_api_name`` in ``org_alias`` via ``sf agent test run-eval``, parses the
    real per-case verdicts, folds them into the spec as added observations, and
    re-scores. The round directory is written to ``out_dir/round-<N>`` and is
    never overwritten.

    Feedback that did not come from a live org (injected runners, fabricated
    JSON) is blocked and reported as such. A round whose feedback is synthetic
    produces blocking_issues and trustworthy=false; it never advances the spec.

    Forbidden org aliases (PPCDM, PPCaccenture) are refused before any network
    call is made.

    Args:
        spec_json: The DerivedAgentSpec as JSON (output of derive_spec.spec, or
            written by score_spec). Must be the on-disk schema from spec_builder.
        org_alias: Salesforce org alias for ``sf agent test run-eval``. Required.
        agent_api_name: API name of the deployed Agentforce agent under test.
        test_spec_name: Base name for the emitted AiEvaluationDefinition.
        out_dir: Directory to write the round output. The round is written to
            ``out_dir/round-1/`` (or the next available round number).
    """
    request_id = uuid4().hex[:12]
    started = time.monotonic()
    tool = "run_stage5_round"

    from .org_validation import org_is_forbidden

    if org_is_forbidden(org_alias):
        return _err(
            tool,
            request_id,
            started,
            ERROR_VALIDATION,
            f"Org alias {org_alias!r} is out of scope for this project and was refused.",
            remedy="Use a Developer Edition or sandbox org you own.",
        )

    try:
        spec = _spec_from_json(spec_json)
    except ValueError as exc:
        return _err(tool, request_id, started, ERROR_VALIDATION, f"Could not parse spec_json: {exc}")

    try:
        from .stage5 import (
            assert_round_unwritten,
            run_agent_eval,
            stage5_round,
            write_round,
            RUN_EVAL_DIALECT,
        )
        from .eval_spec import build_legacy_test_spec, write_test_spec
    except ImportError as exc:
        return _err(tool, request_id, started, ERROR_DEPENDENCY, f"Required module not available: {exc}")

    out = _resolve(out_dir)

    # Determine the next round number from existing round-N directories.
    existing = sorted(
        int(d.name.split("-")[1])
        for d in out.glob("round-*")
        if d.is_dir() and d.name.split("-")[1:] and d.name.split("-")[1].isdigit()
    )
    round_number = (max(existing) + 1) if existing else 1

    try:
        assert_round_unwritten(out, round_number)
    except Exception as exc:
        return _err(tool, request_id, started, ERROR_VALIDATION, str(exc))

    try:
        round_dir = out / f"round-{round_number}"
        round_dir.mkdir(parents=True, exist_ok=True)

        test_spec_obj, _derivations = build_legacy_test_spec(
            spec, name=f"{test_spec_name}_r{round_number}", subject_name=agent_api_name
        )
        spec_path = write_test_spec(round_dir / "testSpec.yaml", test_spec_obj)

        feedback = run_agent_eval(
            spec_path,
            org_alias=org_alias,
            api_name=agent_api_name,
            dialect=RUN_EVAL_DIALECT,
        )

        round_result = stage5_round(spec, feedback, round_number=round_number)
        write_round(out, round_result)
    except Exception as exc:  # noqa: BLE001
        return _err(tool, request_id, started, ERROR_INTERNAL, f"Stage-5 round failed: {exc}")

    rd = round_result.to_dict()
    score_after = rd.get("score_after") or {}
    score_before = rd.get("score_before") or {}
    return _ok(
        tool,
        request_id,
        started,
        roundNumber=round_result.round_number,
        trustworthy=round_result.trustworthy,
        score=score_after.get("total"),
        scoreBefore=score_before.get("total"),
        passed=score_after.get("passed", False),
        blockingIssues=round_result.blocking_issues,
        stopReason=None,
        findings=round_result.findings,
        notes=round_result.notes,
        feedbackSource=round_result.feedback.source,
        feedbackIsReal=round_result.feedback.is_real,
        caseCount=len(round_result.feedback.cases),
        passedCount=round_result.feedback.passed_count,
        failedCount=round_result.feedback.failed_count,
        writtenTo=str(out / f"round-{round_number}" / "round.json"),
    )


@mcp.tool()
def run_iterate(
    spec_json: str,
    org_alias: str,
    agent_api_name: str,
    test_spec_name: str,
    out_dir: str,
    rounds: int = 1,
) -> dict[str, Any]:
    """Run the stage-5 refinement loop: emit, evaluate against live agent, repeat.

    Each round emits a legacy test spec, runs it against the live agent in
    ``org_alias``, folds the real per-case verdicts into the spec as added
    observations, and re-scores. The loop runs ``rounds`` times (default 1). The
    spec carried forward is always the adjusted spec from the previous round,
    but only when that round was trustworthy (real org feedback, no blocking
    issues). A round with synthetic feedback annotates the spec as unvalidated
    and does not advance it.

    Round directories are written to ``out_dir/round-N/`` and are never
    overwritten. An attempt to overwrite raises rather than silently replacing
    the existing audit trail.

    Forbidden org aliases (PPCDM, PPCaccenture) are refused before any network
    call is made.

    Args:
        spec_json: The DerivedAgentSpec as JSON (output of derive_spec.spec, or
            written by score_spec). Must be the on-disk schema from spec_builder.
        org_alias: Salesforce org alias for ``sf agent test run-eval``. Required.
        agent_api_name: API name of the deployed Agentforce agent under test.
        test_spec_name: Base name for the emitted AiEvaluationDefinitions.
        out_dir: Root directory for round output. Each round writes to
            ``out_dir/round-N/``.
        rounds: Number of round trips to run. Each costs real org LLM calls.
            Must be >= 1. Default 1.
    """
    request_id = uuid4().hex[:12]
    started = time.monotonic()
    tool = "run_iterate"

    from .org_validation import org_is_forbidden

    if org_is_forbidden(org_alias):
        return _err(
            tool,
            request_id,
            started,
            ERROR_VALIDATION,
            f"Org alias {org_alias!r} is out of scope for this project and was refused.",
            remedy="Use a Developer Edition or sandbox org you own.",
        )

    if rounds < 1:
        return _err(
            tool,
            request_id,
            started,
            ERROR_VALIDATION,
            f"rounds must be >= 1, got {rounds}",
        )

    try:
        spec = _spec_from_json(spec_json)
    except ValueError as exc:
        return _err(tool, request_id, started, ERROR_VALIDATION, f"Could not parse spec_json: {exc}")

    out = _resolve(out_dir)

    try:
        from .iterate import refine_with_org_feedback
    except ImportError as exc:
        return _err(tool, request_id, started, ERROR_DEPENDENCY, f"Required module not available: {exc}")

    try:
        round_results = refine_with_org_feedback(
            spec,
            out_dir=out,
            org_alias=org_alias,
            agent_api_name=agent_api_name,
            test_spec_name=test_spec_name,
            rounds=rounds,
        )
    except Exception as exc:  # noqa: BLE001
        return _err(tool, request_id, started, ERROR_INTERNAL, f"Iterate failed: {exc}")

    rounds_run = len(round_results)
    last = round_results[-1] if round_results else None
    last_dict = last.to_dict() if last else {}
    score_after = last_dict.get("score_after") or {}
    blocking = last.blocking_issues if last else []

    # The stop reason for the iterate loop: trustworthy exit if any round passed,
    # or summarise why the loop ended.
    if last and last.trustworthy and score_after.get("passed"):
        stop_reason = "passed"
    elif last and not last.feedback.is_real:
        stop_reason = "synthetic-feedback"
    elif rounds_run >= rounds:
        stop_reason = f"completed {rounds_run} round(s)"
    else:
        stop_reason = "unknown"

    return _ok(
        tool,
        request_id,
        started,
        roundsRun=rounds_run,
        finalScore=score_after.get("total"),
        passed=score_after.get("passed", False),
        blockingIssues=blocking,
        stopReason=stop_reason,
        rounds=[
            {
                "roundNumber": r.round_number,
                "trustworthy": r.trustworthy,
                "score": (r.to_dict().get("score_after") or {}).get("total"),
                "passed": (r.to_dict().get("score_after") or {}).get("passed", False),
                "blockingIssues": r.blocking_issues,
                "feedbackSource": r.feedback.source,
                "passedCount": r.feedback.passed_count,
                "failedCount": r.feedback.failed_count,
            }
            for r in round_results
        ],
    )


@mcp.tool()
def run_deploy(
    capture_path: str,
    developer_name: str,
    agent_label: str,
    org_alias: str,
    validate_only: bool = False,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Validate and optionally deploy an Agentforce bundle derived from a capture.

    Runs two steps when ``validate_only`` is false:

    1. ``sf agent validate authoring-bundle`` — compiles the Agent Script. If the
       compiler rejects the bundle, the deploy is skipped and the errors are
       returned verbatim so the caller can fix the emitter rather than the output.
    2. ``sf project deploy start`` — deploys the ``AiAuthoringBundle`` metadata.
       Pass ``dry_run=True`` to exercise the deploy path without committing.

    Forbidden org aliases (PPCDM, PPCaccenture) are refused before any network
    call is made. No credential ever appears on argv.

    Args:
        capture_path: Path to a dom_capture.jsonl trace.
        developer_name: Salesforce API name for the bundle, e.g. ``Case_Triage_Agent``.
        agent_label: Human-readable label, e.g. ``Case Triage Agent``.
        org_alias: Org alias to validate/deploy against. Required.
        validate_only: Stop after ``sf agent validate authoring-bundle``. Reports
            VALIDATED on success; deploy is not attempted.
        dry_run: Pass ``--dry-run`` to ``sf project deploy start``. Checks
            permissions and metadata without committing. Implies deploy attempt
            (not ``validate_only``).
    """
    request_id = uuid4().hex[:12]
    started = time.monotonic()
    tool = "run_deploy"

    from .org_validation import org_is_forbidden

    if org_is_forbidden(org_alias):
        return _err(
            tool,
            request_id,
            started,
            ERROR_VALIDATION,
            f"Org alias {org_alias!r} is out of scope for this project and was refused.",
            remedy="Use a Developer Edition or sandbox org you own.",
        )

    path = _resolve(capture_path)
    if not path.is_file():
        return _err(tool, request_id, started, ERROR_NOT_FOUND, f"No file at {path}")

    try:
        result = run_pipeline(path, org_url="https://example.my.salesforce.com")
    except CaptureRejected as exc:
        return _err(
            tool, request_id, started, ERROR_VALIDATION, str(exc), findings=exc.findings
        )
    except Exception as exc:  # noqa: BLE001
        return _err(tool, request_id, started, ERROR_INTERNAL, f"Pipeline failed: {exc}")

    try:
        from .agent_script import InsufficientEvidenceError, build_agent_script

        agent_source = build_agent_script(
            result.spec, developer_name=developer_name, agent_label=agent_label
        )
    except InsufficientEvidenceError as exc:
        return _err(
            tool,
            request_id,
            started,
            ERROR_VALIDATION,
            f"Not enough observed evidence to emit a bundle: {exc}",
            remedy="Re-record a fuller session rather than lowering the bar.",
        )
    except Exception as exc:  # noqa: BLE001
        return _err(tool, request_id, started, ERROR_INTERNAL, f"Emit failed: {exc}")

    try:
        from .deploy import DeployOutcome, deploy_bundle
    except ImportError as exc:
        return _err(
            tool, request_id, started, ERROR_DEPENDENCY, f"deploy module not available: {exc}"
        )

    with tempfile.TemporaryDirectory(prefix="sfvb-mcp-deploy-") as scratch:
        deploy_result = deploy_bundle(
            agent_source,
            developer_name=developer_name,
            org_alias=org_alias,
            project_dir=Path(scratch),
            validate_only=validate_only,
            dry_run=dry_run,
        )

    outcome = deploy_result.outcome
    succeeded = deploy_result.succeeded or outcome in (
        DeployOutcome.VALIDATED,
        DeployOutcome.DRY_RUN,
        DeployOutcome.DEPLOYED,
    )

    return _ok(
        tool,
        request_id,
        started,
        outcome=outcome.value,
        developerName=developer_name,
        agentLabel=agent_label,
        orgAlias=org_alias,
        detail=deploy_result.detail,
        compiled=deploy_result.compiled,
        deployed=deploy_result.deployed,
        dryRun=deploy_result.dry_run,
        validateOnly=validate_only,
        validationErrors=list(deploy_result.validation_errors),
        deployErrors=list(deploy_result.deploy_errors),
        validateCommand=deploy_result.validate_command or None,
        deployCommand=deploy_result.deploy_command or None,
        succeeded=succeeded,
        note=(
            "Deployment mutates the org. Use validate_only=True for a read-only "
            "compile check, or dry_run=True to exercise the deploy path without "
            "committing. Forbidden org aliases (PPCDM, PPCaccenture) are refused "
            "before any CLI call."
        ),
    )


def main() -> None:
    """Entry point for the `sf-blueprint-mcp` console script."""
    _configure_logging()
    log.info(
        json.dumps(
            {
                "event": "starting",
                "server": SERVER_NAME,
                "version": SERVER_VERSION,
                "transport": "stdio",
            }
        )
    )
    mcp.run()


if __name__ == "__main__":
    main()

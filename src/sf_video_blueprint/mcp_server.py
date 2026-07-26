"""MCP server exposing the blueprint pipeline to any MCP-capable AI harness.

Runs over stdio, so it works with Claude Desktop, Claude Code, Cursor, Windsurf,
Continue, or anything else that speaks the Model Context Protocol.

    sf-blueprint-mcp

Design notes worth knowing before adding a tool here:

**Every tool is read-only and offline.** Nothing in this server contacts a
Salesforce org, launches a browser, or writes outside an explicitly supplied
output path. An agent driving this server cannot mutate an org through it. That is
deliberate: an LLM deciding on its own to replay recorded clicks against a live
org is precisely the failure this project should not enable.

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

Every tool is offline and read-only. No tool contacts a Salesforce org, launches
a browser, or modifies anything outside a path you explicitly pass.

Two things to carry into how you report results:

1. Telemetry is always mocked here, because collecting real telemetry needs a
   live org. Every derived spec is therefore stamped `telemetry_source: "mock"`
   and CANNOT pass the quality gate. A `passed: false` with a `mock` provenance is
   the expected outcome, not a failure to work around. Do not describe such a spec
   as validated, verified, or production-ready.

2. Nothing this server emits has ever been validated against a real Salesforce
   org. `locallyValid: true` means it passed this project's own structural checks,
   not Salesforce's. Always tell the user to run
   `sf agent validate authoring-bundle` before trusting a bundle.

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
        ],
        capabilities={
            "offline": True,
            "readOnly": True,
            "contactsSalesforceOrg": False,
            "launchesBrowser": False,
            "telemetry": "mock-only — collecting real telemetry needs a live org",
        },
        limitations=[
            "No output from this server has ever been validated against a real "
            "Salesforce org. `sf agent validate authoring-bundle` has never been "
            "run, so an emitted .agent bundle may be syntactically invalid.",
            "Telemetry is always mocked here, so every derived spec is stamped "
            "telemetry_source=mock and cannot pass the score gate. That is "
            "correct behaviour, not a bug to work around.",
            "Video files are not supported. The video extractor is a stub that "
            "returns one placeholder step for any input. Use a dom_capture.jsonl.",
            "Capture ingest can silently discard events: the integrity gate only "
            "refuses at >=50% loss, so check skipped_line_count on every result.",
            "No agent actions (@apex.*/@flow.*) are ever emitted, by design — "
            "referencing an action that may not exist in the target org produces "
            "a bundle that fails to deploy for invisible reasons.",
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
) -> dict[str, Any]:
    """Emit an Agentforce Agent Script (.agent) bundle from a recorded process.

    Produces the .agent source and its .bundle-meta.xml, plus the findings from
    local validation. The emitted agent is deliberately a topic router with no
    @apex.* or @flow.* actions: referencing an action that may not exist in the
    target org yields a bundle that fails to deploy for reasons the operator
    cannot see.

    IMPORTANT: local validation is this project's own opinion, not Salesforce's.
    `sf agent validate authoring-bundle` has never been run against any output of
    this project. Validate with the Salesforce CLI before trusting the result.

    Args:
        capture_path: Path to a dom_capture.jsonl trace.
        developer_name: API name for the agent, e.g. `Case_Triage_Agent`.
        agent_label: Human-readable label, e.g. `Case Triage Agent`.
        output_dir: Optional directory to write the bundle into. Returned inline
            when omitted.
    """
    request_id = uuid4().hex[:12]
    started = time.monotonic()
    tool = "emit_agent_bundle"

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
        writtenTo=written or None,
        provenance=dict(result.provenance),
        nextStep=(
            "Validate with the Salesforce CLI before trusting this: "
            "`sf agent validate authoring-bundle`. That has never been run against "
            "this project's output, so the bundle may be invalid."
        ),
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
        from .eval_spec import build_legacy_test_spec, build_ngt_test_spec, render_test_spec

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

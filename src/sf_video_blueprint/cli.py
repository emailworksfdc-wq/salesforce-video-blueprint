from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
from uuid import uuid4

import typer

from .correlation import correlate_all
from .dom_capture import parse_capture_file, validate_trace
from .dom_extractor import DomCaptureExtractor
from .extractor import HeuristicVideoExtractor
from .html_report import AgentBlueprintSection, DataProvenance, MasterBlueprintRenderer
from .models import ActionType, ExtractedAction
from .org_validation import org_is_forbidden
from .redaction import scrub_collected_telemetry
from .replay import NoopUIAdapter, ReplayEngine, ReplayRunMetadata, SalesforceUIAdapter
from .replay_browser import BrowserReplayAdapter
from .salesforce_collectors import SalesforceRestClient, SalesforceTelemetryCollector
from .spec_builder import (
    DerivedAgentSpec,
    DerivedEntity,
    SpecEvidence,
    build_agent_spec,
    write_spec,
)
from .spec_score import PASS_THRESHOLD
from .telemetry import (
    MockTelemetryCollector,
    TelemetryCollector,
    TelemetryLayer,
    TelemetryRegistry,
)

app = typer.Typer(help="Generate Salesforce process blueprint from video inputs.")


# ---------------------------------------------------------------------------
# capture sub-command
# ---------------------------------------------------------------------------

@app.command()
def capture(
    org_alias: str = typer.Option(..., help="Salesforce org alias or username"),
    out_dir: Path = typer.Option(
        Path("./outputs/capture"),
        help="Output directory for JSONL and manifest",
    ),
    start_url: str | None = typer.Option(
        None,
        help="Optional starting URL (defaults to org home after frontdoor)",
    ),
    note: str | None = typer.Option(
        None,
        help="Operator description of the process being recorded",
    ),
) -> None:
    """Launch a headed browser, inject the DOM recorder, and collect events to JSONL.

    A human operator performs the business process in the browser. Press Enter
    in the terminal when done. Requires playwright to be installed:

        pip install playwright && playwright install chromium

    The output artifacts (dom_capture.jsonl, dom_capture.network.jsonl,
    dom_capture.manifest.json) can then be passed to 'sf-blueprint run
    --capture <out_dir>/dom_capture.jsonl'.
    """
    # Guard: confirm playwright is importable before we attempt anything else.
    # This must be a lazy import -- importing playwright at module load time would
    # make the entire CLI un-importable on machines that only have the base package.
    import importlib as _importlib

    try:
        _importlib.import_module("playwright.sync_api")
    except ModuleNotFoundError:
        typer.secho(
            "ERROR: playwright is not installed. The 'capture' sub-command requires it.\n"
            "Install it with:\n"
            "    pip install playwright\n"
            "    playwright install chromium",
            fg=typer.colors.RED,
            bold=True,
        )
        raise typer.Exit(code=1)

    # Lazy-import the capture module so this file stays importable without playwright.
    # Try the project-root package path (normal development / installed layout).
    try:
        _inject = _importlib.import_module("capture.inject")
    except ModuleNotFoundError as exc:
        typer.secho(
            f"ERROR: Could not import the capture module: {exc}\n"
            "Ensure capture/inject.py is present in the project root.",
            fg=typer.colors.RED,
            bold=True,
        )
        raise typer.Exit(code=1) from exc

    _inject.main(
        org_alias=org_alias,
        out_dir=out_dir,
        start_url=start_url,
        note=note,
    )



def _redact_sensitive_url(url: str) -> str:
    """Redact session IDs and access tokens from URLs before persisting or displaying.

    Standing rule: never log or persist a token, session id, or frontdoor.jsp URL.
    The report is an audit artifact that may be shared; secrets must be redacted
    rather than omitted so the audit trail shows a URL was used.
    """
    # Redact frontdoor.jsp sid parameter
    url = re.sub(r'(\?|&)(sid)=([^&]+)', r'\1\2=[REDACTED]', url, flags=re.IGNORECASE)
    # Redact access_token parameter (sometimes seen in OAuth flows)
    url = re.sub(r'(\?|&)(access_token)=([^&]+)', r'\1\2=[REDACTED]', url, flags=re.IGNORECASE)
    # Redact session parameter variants
    url = re.sub(r'(\?|&)(session|sessionId|session_id)=([^&]+)', r'\1\2=[REDACTED]', url, flags=re.IGNORECASE)
    return url


def _parse_tracked_records(values: list[str]) -> list[tuple[str, str]]:
    parsed: list[tuple[str, str]] = []
    for raw in values:
        if ":" not in raw:
            raise typer.BadParameter(f"Invalid --track-record value '{raw}'. Use ObjectApiName:RecordId")
        object_api_name, record_id = raw.split(":", 1)
        parsed.append((object_api_name.strip(), record_id.strip()))
    return parsed




def _load_spec_from_json(spec_path: Path) -> DerivedAgentSpec:
    """Reconstruct a DerivedAgentSpec from a JSON file written by write_spec.

    The JSON schema mirrors DerivedAgentSpec.to_dict() plus a top-level
    provisioning key which is ignored here (it belongs to the run, not the spec).
    """
    data = json.loads(spec_path.read_text(encoding="utf-8"))
    entities = [
        DerivedEntity(
            name=e["name"],
            object_api_name=e.get("object_api_name"),
            field_api_name=e.get("field_api_name"),
            evidence=[
                SpecEvidence(source=ev["source"], detail=ev["detail"])
                for ev in e.get("evidence", [])
            ],
        )
        for e in data.get("entities", [])
    ]
    evidence = [
        SpecEvidence(source=ev["source"], detail=ev["detail"])
        for ev in data.get("evidence", [])
    ]
    return DerivedAgentSpec(
        intent=data["intent"],
        confidence=float(data["confidence"]),
        objects_touched=list(data.get("objects_touched", [])),
        entities=entities,
        orchestration_steps=list(data.get("orchestration_steps", [])),
        guardrails=list(data.get("guardrails", [])),
        failure_handling=list(data.get("failure_handling", [])),
        unknowns=list(data.get("unknowns", [])),
        evidence=evidence,
    )

def find_last_capture(capture_dir: Path) -> "Path | None":
    """Find the most recently modified ``*.dom_capture.jsonl`` file under *capture_dir*.

    The function scans only the immediate children of *capture_dir* and returns
    the file with the highest ``st_mtime``.  If the directory does not exist or
    contains no matching files, ``None`` is returned.

    Args:
        capture_dir: Directory to search (e.g. ``./outputs/capture``).

    Returns:
        The most recently modified ``.dom_capture.jsonl`` file, or ``None`` if
        the directory is empty or does not exist.
    """
    if not capture_dir.exists():
        return None
    candidates = sorted(
        capture_dir.glob("*.dom_capture.jsonl"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    return candidates[0] if candidates else None


def _read_event_count_from_manifest(capture_path: Path) -> "int | None":
    """Read the ``event_count`` field from the companion manifest, if it exists.

    The manifest is expected to be a JSON file whose name is derived from the
    capture file by replacing ``.dom_capture.jsonl`` with
    ``.dom_capture.manifest.json``.

    Args:
        capture_path: Path to the ``.dom_capture.jsonl`` capture file.

    Returns:
        The integer ``event_count`` from the manifest, or ``None`` if the
        manifest is absent or unparseable.
    """
    # The companion manifest lives next to the capture file.
    # Naming convention (B01): <stem>.dom_capture.manifest.json
    # where <stem>.dom_capture.jsonl is the capture.
    stem = capture_path.name.removesuffix(".dom_capture.jsonl")
    manifest_path = capture_path.parent / f"{stem}.dom_capture.manifest.json"
    if not manifest_path.exists():
        return None
    try:
        data = json.loads(manifest_path.read_text(encoding="utf-8"))
        count = data.get("event_count")
        return int(count) if count is not None else None
    except (json.JSONDecodeError, ValueError, OSError):
        return None

@app.command()
def run(
    video_path: Path = typer.Argument(
        None,
        exists=True,
        help="Path to recording file. Uses the stub extractor, which does NOT "
        "decode video — prefer --capture.",
    ),
    capture: Path | None = typer.Option(
        None,
        exists=True,
        help="Path to a dom_capture.jsonl produced by capture/inject.py. This is "
        "real observed evidence and is preferred over the video path.",
    ),
    last_capture: bool = typer.Option(
        False,
        "--last-capture",
        help="Use the most recently modified *.dom_capture.jsonl found under "
        "--capture-dir (default: ./outputs/capture). Ignored (with a warning) "
        "if --capture is also specified.",
    ),
    capture_dir: Path = typer.Option(
        Path("./outputs/capture"),
        help="Directory searched by --last-capture. Has no effect unless "
        "--last-capture is also given.",
    ),
    org_url: str = typer.Option(..., help="Target Salesforce org URL."),
    username: str = typer.Option("analyst@example.com", help="Replay username for trace metadata."),
    profile_name: str = typer.Option("System Administrator", help="Replay profile name."),
    output_path: Path = typer.Option(Path("./outputs/master_blueprint.html"), help="Output HTML path."),
    spec_output: Path | None = typer.Option(
        None,
        help="Output path for the machine-readable agent spec (JSON). "
        "Defaults to <output-path>.agent-spec.json.",
    ),
    mode: str = typer.Option(
        "mock",
        help="Execution mode: mock or live.",
    ),
    access_token: str | None = typer.Option(
        None,
        help="Salesforce access token for live telemetry. Defaults to SF_ACCESS_TOKEN env var.",
    ),
    track_record: list[str] = typer.Option(
        [],
        help="Record to monitor for field diffs; format ObjectApiName:RecordId. Repeatable.",
    ),
) -> None:
    # --last-capture resolution: find the most recent *.dom_capture.jsonl in
    # capture_dir and use it as --capture, unless --capture was already given.
    if last_capture:
        if capture is not None:
            typer.secho(
                "WARNING: --last-capture is ignored because --capture was also specified. "
                "Using explicit --capture path.",
                fg=typer.colors.YELLOW,
            )
        else:
            found = find_last_capture(capture_dir)
            if found is None:
                typer.secho(
                    f"ERROR: --last-capture found no *.dom_capture.jsonl files under "
                    f"{capture_dir}. Run 'sf-blueprint capture' first, or pass an "
                    "explicit path with --capture.",
                    fg=typer.colors.RED,
                    bold=True,
                )
                raise typer.Exit(code=1)
            event_count = _read_event_count_from_manifest(found)
            count_msg = f" ({event_count} events)" if event_count is not None else ""
            typer.echo(f"--last-capture: using {found}{count_msg}")
            capture = found
    
    
    if capture is not None:
        # Real observed evidence: a DOM trace recorded click-by-click from the org.
        # CRITICAL SECURITY BOUNDARY: validate_trace must run BEFORE extraction
        # to detect redaction leaks and integrity violations.
        trace = parse_capture_file(capture)
        findings = validate_trace(trace)

        # Categorize findings by severity
        security_critical = [f for f in findings if f.startswith("SECURITY CRITICAL:")]
        data_loss = [f for f in findings if f.startswith("DATA LOSS:")]
        security_warnings = [f for f in findings if f.startswith("SECURITY:")]
        other = [f for f in findings if not any(f.startswith(p) for p in ["SECURITY CRITICAL:", "DATA LOSS:", "SECURITY:"])]

        # FAIL CLOSED: Any security-critical finding (especially redaction leaks) aborts the run
        if security_critical:
            typer.secho("CAPTURE VALIDATION FAILED — SECURITY CRITICAL ISSUES DETECTED:", fg=typer.colors.RED, bold=True)
            for finding in security_critical:
                typer.secho(f"  {finding}", fg=typer.colors.RED)
            typer.secho(
                "\nThe capture contains a REDACTION LEAK: a value was flagged as sensitive "
                "but was not actually redacted by the recorder. This is a recorder bug that "
                "leaks sensitive data (card numbers, passwords) into the capture file.",
                fg=typer.colors.RED,
            )
            typer.secho(
                "ABORTING: Cannot build a spec from a capture with confirmed redaction leaks. "
                "Fix the recorder and re-record. No spec JSON file will be emitted.",
                fg=typer.colors.RED,
                bold=True,
            )
            raise typer.Exit(code=1)

        # FAIL CLOSED: Data loss findings also abort (100% loss or >50% loss)
        if data_loss:
            typer.secho("CAPTURE VALIDATION FAILED — DATA LOSS DETECTED:", fg=typer.colors.RED, bold=True)
            for finding in data_loss:
                typer.secho(f"  {finding}", fg=typer.colors.RED)
            typer.secho(
                "\nABORTING: Cannot build a spec from a capture with material data loss. "
                "Check for recorder/parser version drift or schema mismatch.",
                fg=typer.colors.RED,
                bold=True,
            )
            raise typer.Exit(code=1)

        # DEFECT L4-7: incomplete-evidence findings get their own block. Below
        # the 50% fail-closed threshold this used to be surfaced nowhere at all,
        # so a capture missing 40% of its events was stamped as real evidence in
        # silence. Non-blocking by design — the gate is unchanged — but it must
        # not be one yellow line among many.
        incomplete = [f for f in findings if f.startswith("EVIDENCE INCOMPLETE:")]
        if incomplete:
            typer.secho(
                "CAPTURE IS INCOMPLETE — evidence was lost:",
                fg=typer.colors.YELLOW,
                bold=True,
            )
            for finding in incomplete:
                typer.secho(f"  {finding}", fg=typer.colors.YELLOW)
            typer.secho(
                f"  Parsed {len(trace.events)} events; "
                f"{len(trace.skipped_lines)} line(s) discarded "
                f"({trace.loss_ratio:.0%} line loss)"
                + (
                    f"; {trace.manifest_gap} event(s) never reached the parser"
                    if trace.manifest_gap
                    else ""
                )
                + ".",
                fg=typer.colors.YELLOW,
            )
            typer.secho(
                "  The spec below is derived from a PARTIAL recording. It is still "
                "stamped as real dom-capture evidence, because it is real — but it "
                "is not complete.",
                fg=typer.colors.YELLOW,
            )

        # Surface all other findings as warnings (non-blocking)
        remaining = [f for f in (security_warnings + other) if f not in incomplete]
        for finding in remaining:
            typer.secho(f"CAPTURE VALIDATION: {finding}", fg=typer.colors.YELLOW)

        extraction = DomCaptureExtractor().extract(capture)
        extraction_source = "dom-capture"
        source_path = capture
    elif video_path is not None:
        # HeuristicVideoExtractor does not decode the video; it emits one
        # placeholder step for any input. Track that so the report can never
        # present it as observed.
        extraction = HeuristicVideoExtractor().extract(video_path)
        extraction_source = "stub"
        source_path = video_path
    else:
        raise typer.BadParameter(
            "Provide --capture <dom_capture.jsonl> (real evidence) or a video path "
            "(stub extraction, produces placeholder steps)."
        )

    for warning in extraction.warnings:
        typer.secho(f"EXTRACTION: {warning}", fg=typer.colors.YELLOW)

    # Redact secrets from org_url before persisting in metadata/report
    # The ORIGINAL org_url (with sid) is used for REST API calls, but the REDACTED
    # version goes into the audit artifact.
    redacted_org_url = _redact_sensitive_url(org_url)

    run_metadata = ReplayRunMetadata(
        run_id=f"run-{uuid4().hex[:8]}",
        org_url=redacted_org_url,
        username=username,
        profile_name=profile_name,
        role_name=None,
        environment=mode,
    )

    replay_adapter: SalesforceUIAdapter = NoopUIAdapter() if mode == "mock" else BrowserReplayAdapter()
    replay_engine = ReplayEngine(adapter=replay_adapter)
    replay_events = replay_engine.replay(run_metadata, extraction.actions)

    telemetry = TelemetryRegistry()
    collector: TelemetryCollector
    if mode == "live":
        token = access_token or os.getenv("SF_ACCESS_TOKEN")
        if not token:
            raise typer.BadParameter("Live mode requires --access-token or SF_ACCESS_TOKEN.")
        collector = SalesforceTelemetryCollector(
            SalesforceRestClient(org_url, token),
            tracked_records=_parse_tracked_records(track_record),
        )
    else:
        collector = MockTelemetryCollector()

    for action in extraction.actions:
        telemetry.collect_step(collector, run_metadata.run_id, action.step_id)

    # Org records are scrubbed on ingest by `TelemetryRegistry` itself — in live mode
    # the snapshots and payloads are whole Salesforce records, and `_derive_entities`
    # interpolates their field values into entity evidence, so an unscrubbed token in
    # a Case field lands in agent-spec.json. Extraction's choke point cannot see
    # these: they were fetched after it ran.
    #
    # This second pass stays as defence in depth for anything appended to the registry
    # outside its own ingest methods. It is idempotent, so on already-clean data it
    # finds nothing and returns no categories — which is why the reported categories
    # are the UNION of both passes. Reporting only this pass's result would make the
    # run stop saying the control fired, and a silent control cannot be audited.
    telemetry_categories = list(
        dict.fromkeys(
            [
                *telemetry.redaction_categories,
                *scrub_collected_telemetry(telemetry.events, telemetry.snapshots),
            ]
        )
    )
    if telemetry_categories:
        typer.echo(
            f"REDACTION: scrubbed telemetry values from the org "
            f"(categories: {', '.join(telemetry_categories)})"
        )

    analyses = correlate_all(extraction.actions, replay_events, telemetry.events, telemetry.snapshots)

    # The agent spec is DERIVED from the correlated run, never hardcoded: the
    # whole point of the pipeline is that a different recording yields a
    # different spec.
    spec = build_agent_spec(extraction.actions, analyses)
    provenance = DataProvenance(
        extraction_source=extraction_source,
        telemetry_source="live-org" if mode == "live" else "mock",
        replay_source="browser" if mode == "live" else "noop",
        agent_spec_source="derived",
    )
    ai_sections = [
        AgentBlueprintSection(
            intent=spec.intent,
            required_entities=[item.name for item in spec.entities],
            orchestration_steps=spec.orchestration_steps,
            guardrails=spec.guardrails,
            failure_handling=spec.failure_handling,
            derived=True,
        )
    ]

    renderer = MasterBlueprintRenderer()
    path = renderer.write_html(output_path, extraction, run_metadata, analyses, ai_sections, provenance)

    spec_path = spec_output or output_path.with_suffix(".agent-spec.json")
    written_spec = write_spec(
        spec_path,
        spec,
        {
            "extraction_source": provenance.extraction_source,
            "telemetry_source": provenance.telemetry_source,
            "replay_source": provenance.replay_source,
            "run_id": run_metadata.run_id,
            "recording_id": extraction.recording_id,
            # Which file the steps were actually read from, so a spec can be traced
            # back to its evidence rather than just to a run id.
            "source_path": str(source_path),
        },
    )

    typer.echo(f"Master blueprint generated: {path}")
    typer.echo(f"Agent spec (machine-readable) generated: {written_spec}")
    typer.echo(f"Derived intent: {spec.intent} (confidence {spec.confidence:.2f})")
    if provenance.is_simulated:
        typer.secho(
            "WARNING: this run contains SIMULATED data and is not audit evidence. "
            f"Simulated: {'; '.join(provenance.simulated_parts)}",
            fg=typer.colors.RED,
            bold=True,
        )
    for unknown in spec.unknowns:
        typer.secho(f"UNKNOWN: {unknown}", fg=typer.colors.YELLOW)





@app.command()
def refine(
    spec_json: Path = typer.Argument(
        ...,
        help="Path to an agent-spec.json produced by the 'run' command.",
        exists=True,
    ),
    out_dir: Path = typer.Option(
        Path("./outputs/iterations"),
        help="Output directory. Each round is written to a versioned sub-directory (v1/, v2/, ...).",
    ),
    company_name: str = typer.Option("Acme Corp", help="Company name for the agent spec YAML."),
    company_description: str = typer.Option(
        "A company using Salesforce.",
        help="Company description for the agent spec YAML.",
    ),
    max_rounds: int = typer.Option(5, help="Maximum number of refinement rounds."),
    summary: bool = typer.Option(
        False,
        "--summary",
        help="Write a human-readable Markdown summary to <out_dir>/iteration_summary.md.",
    ),
) -> None:
    """Iteratively refine an agent spec using the offline scoring loop.

    Reads an agent-spec.json, scores it, applies deterministic improvements, and
    writes versioned outputs to <out_dir>/v1/, v2/, etc.  Stops when the spec
    passes the quality gate, converges, or reaches --max-rounds.

    Use --summary to also write <out_dir>/iteration_summary.md with a
    human-readable view of what happened across all rounds.
    """
    from .iterate import refine, write_iteration_report, write_iteration_summary

    spec = _load_spec_from_json(spec_json)

    typer.echo(f"Loaded spec: {spec_json}")
    typer.echo(f"Intent: {spec.intent}")
    typer.echo(f"Output dir: {out_dir}")

    result = refine(
        spec,
        out_dir=out_dir,
        company_name=company_name,
        company_description=company_description,
        max_rounds=max_rounds,
        use_cli=False,
    )

    # Always write the JSON report (machine-readable contract)
    report_path = write_iteration_report(out_dir / "iteration_report", result)
    typer.echo(f"Iteration report: {report_path}")

    # Optionally write a human-readable Markdown summary
    if summary:
        summary_path = write_iteration_summary(out_dir / "iteration_summary.md", result)
        typer.echo(f"Iteration summary: {summary_path}")

    typer.echo(
        f"Done — {result.rounds_run} round(s), stop reason: {result.stop_reason}. "
        f"Best version: v{result.best.version} (score {result.best.score.total}/{result.best.score.max_total})."
    )


@app.command()
def iterate(
    spec: Path = typer.Option(
        ...,
        help="Path to agent-spec.json produced by the run command.",
    ),
    org_alias: str = typer.Option(
        ...,
        help="Salesforce org alias for running agent tests.",
    ),
    agent_api_name: str = typer.Option(
        ...,
        help="API name of the deployed Agentforce agent to test against.",
    ),
    test_spec_name: str = typer.Option(
        ...,
        help="Name prefix for the generated AiEvaluationDefinition test specs.",
    ),
    rounds: int = typer.Option(
        1,
        help="Number of refinement rounds to run. Each round costs real org LLM calls.",
        min=1,
    ),
    out_dir: Path = typer.Option(
        ...,
        help="Output directory for round artifacts. Each round writes round-N/ sub-dirs.",
    ),
) -> None:
    """Iteratively refine an agent spec by running it against a real Salesforce org.

    Each round: emits a test spec, runs sf agent test run-eval against the org,
    parses real per-case verdicts, folds them into the spec as observations, and
    re-scores. Round artifacts are written to out-dir/round-N/ and are never
    overwritten.

    Exits non-zero if the final score is below the pass threshold
    (PASS_THRESHOLD=75) or if a blocking issue remains in the final round.
    """
    if not spec.exists():
        typer.secho(f"ERROR: spec file not found: {spec}", fg=typer.colors.RED, bold=True)
        raise typer.Exit(code=1)
    if not spec.is_file():
        typer.secho(f"ERROR: spec path is not a file: {spec}", fg=typer.colors.RED, bold=True)
        raise typer.Exit(code=1)
    if org_is_forbidden(org_alias):
        typer.secho(
            "ERROR: org alias " + repr(org_alias) + " is strictly out of scope for this project. "
            "PPCDM and PPCaccenture may not be targeted by this tool.",
            fg=typer.colors.RED, bold=True,
        )
        raise typer.Exit(code=1)
    out_dir_resolved = Path(out_dir).resolve()
    try:
        out_dir_resolved.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        typer.secho(
            f"ERROR: cannot create output directory {str(out_dir)!r}: {exc}",
            fg=typer.colors.RED, bold=True,
        )
        raise typer.Exit(code=1)
    probe = out_dir_resolved / ".write_probe"
    try:
        probe.write_text("probe", encoding="utf-8")
        probe.unlink()
    except OSError as exc:
        typer.secho(
            f"ERROR: output directory {str(out_dir)!r} is not writable: {exc}",
            fg=typer.colors.RED, bold=True,
        )
        raise typer.Exit(code=1)
    try:
        agent_spec = _load_spec_from_json(spec)
    except (KeyError, ValueError, json.JSONDecodeError) as exc:
        typer.secho(
            f"ERROR: could not parse spec file {str(spec)!r}: {exc}",
            fg=typer.colors.RED, bold=True,
        )
        raise typer.Exit(code=1)
    typer.echo(
        f"Loaded spec: {agent_spec.intent!r} "
        f"(confidence {agent_spec.confidence:.2f}, {len(agent_spec.entities)} entities)"
    )
    typer.echo(
        f"Running {rounds} refinement round(s) against org {org_alias!r}, "
        f"agent {agent_api_name!r}..."
    )
    from .iterate import refine_with_org_feedback
    round_results = refine_with_org_feedback(
        agent_spec,
        out_dir=out_dir_resolved,
        org_alias=org_alias,
        agent_api_name=agent_api_name,
        test_spec_name=test_spec_name,
        rounds=rounds,
    )
    for r in round_results:
        score_after = r.score_after
        if score_after is not None:
            score_val = score_after.total
            passed_val = score_after.passed
        else:
            score_val = None
            passed_val = None
        stop_reason_parts: list[str] = []
        if r.blocking_issues:
            stop_reason_parts.append("blocking: " + "; ".join(r.blocking_issues))
        if not r.trustworthy:
            stop_reason_parts.append("round not trustworthy (synthetic runner or blocked)")
        score_display = f"{score_val}/{score_after.max_total}" if score_after is not None else "n/a"
        passed_display = "PASS" if passed_val else "FAIL"
        stop_display = " [" + "; ".join(stop_reason_parts) + "]" if stop_reason_parts else ""
        typer.echo(f"Round {r.round_number}: score={score_display} passed={passed_display}{stop_display}")
    if not round_results:
        typer.secho("ERROR: no rounds were completed.", fg=typer.colors.RED, bold=True)
        raise typer.Exit(code=1)
    final_round = round_results[-1]
    final_score = final_round.score_after
    if final_score is None:
        typer.secho(
            "ERROR: final round produced no score; cannot determine pass/fail.",
            fg=typer.colors.RED, bold=True,
        )
        raise typer.Exit(code=1)
    if final_score.total < PASS_THRESHOLD:
        typer.secho(
            f"FAIL: final score {final_score.total}/{final_score.max_total} "
            f"is below pass threshold ({PASS_THRESHOLD}).",
            fg=typer.colors.RED, bold=True,
        )
        raise typer.Exit(code=1)
    if final_round.blocking_issues:
        typer.secho(
            "FAIL: final round has blocking issue(s): " + "; ".join(final_round.blocking_issues),
            fg=typer.colors.RED, bold=True,
        )
        raise typer.Exit(code=1)
    typer.secho(
        f"PASS: final score {final_score.total}/{final_score.max_total} >= threshold ({PASS_THRESHOLD}).",
        fg=typer.colors.GREEN, bold=True,
    )

@app.command()
def deploy(
    capture: Path = typer.Option(
        ...,
        exists=True,
        help="Path to a dom_capture.jsonl produced by 'sf-blueprint capture'.",
    ),
    developer_name: str = typer.Option(
        ...,
        help="Salesforce API name for the bundle, e.g. 'Case_Triage_Agent'.",
    ),
    agent_label: str = typer.Option(
        ...,
        help="Human-readable label, e.g. 'Case Triage Agent'.",
    ),
    org_alias: str = typer.Option(
        ...,
        help="Salesforce org alias or username for deployment.",
    ),
    validate_only: bool = typer.Option(
        False,
        "--validate-only",
        help=(
            "Run sf agent validate authoring-bundle but do not deploy. "
            "Reports VALIDATED on success, REJECTED on compiler errors."
        ),
    ),
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help=(
            "Pass --dry-run to sf project deploy start. Checks permissions "
            "and metadata without committing the deploy. Implies deploy attempt."
        ),
    ),
    out_dir: Path = typer.Option(
        Path("./outputs/deploy"),
        help="Directory for the scaffold project and emitted bundle files.",
    ),
) -> None:
    """Emit an Agentforce bundle from a capture and deploy it to a sandbox org.

    Runs two steps:

    1. sf agent validate authoring-bundle — compiles the Agent Script and
       reports any errors before a deploy is attempted.
    2. sf project deploy start — deploys the AiAuthoringBundle metadata.

    Use --validate-only to stop after step 1. Use --dry-run to exercise the
    deploy path without committing. Either flag still requires --org-alias.

    Refuses PPCDM and PPCaccenture before any CLI call is made.
    """
    if org_is_forbidden(org_alias):
        typer.secho(
            f"ERROR: org alias {org_alias!r} is permanently out of scope for this "
            "project. PPCDM and PPCaccenture may not be targeted by this tool.",
            fg=typer.colors.RED,
            bold=True,
        )
        raise typer.Exit(code=1)

    from .agent_script import (
        InsufficientEvidenceError,
        build_agent_script,
    )
    from .deploy import DeployOutcome, deploy_bundle
    from .pipeline import CaptureRejected, run_pipeline

    # Run the capture through the pipeline to get a spec.
    try:
        result = run_pipeline(capture, org_url="https://example.my.salesforce.com")
    except CaptureRejected as exc:
        typer.secho(
            f"ERROR: capture failed integrity validation: {exc}",
            fg=typer.colors.RED,
            bold=True,
        )
        raise typer.Exit(code=1) from exc

    # Emit the Agent Script bundle.
    try:
        agent_source = build_agent_script(
            result.spec,
            developer_name=developer_name,
            agent_label=agent_label,
        )
    except InsufficientEvidenceError as exc:
        typer.secho(
            f"ERROR: insufficient evidence to emit a bundle: {exc}",
            fg=typer.colors.RED,
            bold=True,
        )
        raise typer.Exit(code=1) from exc

    import tempfile as _tempfile

    out_dir.mkdir(parents=True, exist_ok=True)
    with _tempfile.TemporaryDirectory(prefix="sfvb-deploy-") as scratch:
        deploy_result = deploy_bundle(
            agent_source,
            developer_name=developer_name,
            org_alias=org_alias,
            project_dir=Path(scratch),
            validate_only=validate_only,
            dry_run=dry_run,
        )

    outcome = deploy_result.outcome

    if outcome is DeployOutcome.BLOCKED:
        typer.secho(
            f"ERROR: {deploy_result.detail}",
            fg=typer.colors.RED,
            bold=True,
        )
        raise typer.Exit(code=1)

    if outcome is DeployOutcome.REJECTED:
        typer.secho(
            f"DEPLOY REJECTED: {deploy_result.detail}",
            fg=typer.colors.RED,
            bold=True,
        )
        for err in deploy_result.validation_errors:
            typer.secho(f"  {err}", fg=typer.colors.RED)
        raise typer.Exit(code=1)

    if outcome is DeployOutcome.ERROR:
        typer.secho(
            f"DEPLOY ERROR: {deploy_result.detail}",
            fg=typer.colors.RED,
            bold=True,
        )
        for err in deploy_result.deploy_errors:
            typer.secho(f"  {err}", fg=typer.colors.RED)
        raise typer.Exit(code=1)

    if outcome is DeployOutcome.VALIDATED:
        typer.secho(
            f"VALIDATED: {developer_name} compiled successfully "
            f"(--validate-only; deploy not attempted).",
            fg=typer.colors.GREEN,
            bold=True,
        )
        return

    if outcome is DeployOutcome.DRY_RUN:
        typer.secho(
            f"DRY RUN: {developer_name} would deploy successfully (--dry-run).",
            fg=typer.colors.GREEN,
            bold=True,
        )
        return

    if outcome is DeployOutcome.DEPLOYED:
        typer.secho(
            f"DEPLOYED: {developer_name} deployed to {org_alias}.",
            fg=typer.colors.GREEN,
            bold=True,
        )
        return

    # SKIPPED or unexpected outcome
    typer.echo(f"Outcome: {outcome.value} — {deploy_result.detail}")


if __name__ == "__main__":
    app()


from __future__ import annotations

from datetime import datetime, timezone
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
from .redaction import scrub_collected_telemetry
from .replay import NoopUIAdapter, ReplayEngine, ReplayRunMetadata, SalesforceUIAdapter
from .replay_browser import BrowserReplayAdapter
from .salesforce_collectors import SalesforceRestClient, SalesforceTelemetryCollector
from .spec_builder import build_agent_spec, write_spec
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


if __name__ == "__main__":
    app()


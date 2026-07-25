from __future__ import annotations

from datetime import datetime, timezone
import os
from pathlib import Path
from uuid import uuid4

import typer

from .correlation import correlate_all
from .extractor import HeuristicVideoExtractor
from .html_report import AgentBlueprintSection, MasterBlueprintRenderer
from .models import ExtractedAction
from .replay import ReplayEngine, ReplayRunMetadata, SalesforceUIAdapter
from .replay_browser import BrowserReplayAdapter
from .salesforce_collectors import SalesforceRestClient, SalesforceTelemetryCollector
from .telemetry import CorrelationKey, ObjectSnapshot, TelemetryCollector, TelemetryEvent, TelemetryLayer, TelemetryRegistry

app = typer.Typer(help="Generate Salesforce process blueprint from video inputs.")


def _parse_tracked_records(values: list[str]) -> list[tuple[str, str]]:
    parsed: list[tuple[str, str]] = []
    for raw in values:
        if ":" not in raw:
            raise typer.BadParameter(f"Invalid --track-record value '{raw}'. Use ObjectApiName:RecordId")
        object_api_name, record_id = raw.split(":", 1)
        parsed.append((object_api_name.strip(), record_id.strip()))
    return parsed


class NoopUIAdapter(SalesforceUIAdapter):
    def open_org(self, org_url: str) -> None:
        _ = org_url

    def perform_action(self, action: ExtractedAction) -> tuple[bool, str, str | None]:
        if action.action_type == ActionType.CLICK:
            return True, "Click action replayed.", None
        return True, "Action replayed.", None


class MockTelemetryCollector(TelemetryCollector):
    def collect_for_step(self, run_id: str, step_id: str) -> list[TelemetryEvent]:
        return [
            TelemetryEvent(
                correlation=CorrelationKey(run_id=run_id, step_id=step_id, event_time=datetime.now(timezone.utc)),
                layer=TelemetryLayer.FLOW,
                event_name="FlowInterviewExecuted",
                status="success",
                payload={"flowApiName": "Sample_Flow"},
            )
        ]

    def snapshot_changes(self, run_id: str, step_id: str) -> list[ObjectSnapshot]:
        return [
            ObjectSnapshot(
                correlation=CorrelationKey(run_id=run_id, step_id=step_id, event_time=datetime.now(timezone.utc)),
                object_api_name="Case",
                record_id="500xx0000012345AAA",
                before={"Status": "New"},
                after={"Status": "Working"},
                changed_fields=["Status"],
            )
        ]

@app.command()
def run(
    video_path: Path = typer.Argument(..., exists=True, help="Path to recording file."),
    org_url: str = typer.Option(..., help="Target Salesforce org URL."),
    username: str = typer.Option("analyst@example.com", help="Replay username for trace metadata."),
    profile_name: str = typer.Option("System Administrator", help="Replay profile name."),
    output_path: Path = typer.Option(Path("./outputs/master_blueprint.html"), help="Output HTML path."),
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
    extractor = HeuristicVideoExtractor()
    extraction = extractor.extract(video_path)
    run_metadata = ReplayRunMetadata(
        run_id=f"run-{uuid4().hex[:8]}",
        org_url=org_url,
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

    analyses = correlate_all(extraction.actions, replay_events, telemetry.events, telemetry.snapshots)
    ai_sections = [
        AgentBlueprintSection(
            intent="Update case status from UI workflow",
            required_entities=["caseId", "newStatus", "userContext"],
            orchestration_steps=[
                "Validate caller authorization for record update.",
                "Load current record state and confirm editable status.",
                "Execute update flow and persist status change.",
                "Return confirmation with audit metadata.",
            ],
            guardrails=[
                "Reject updates when record is locked.",
                "Require explicit confirmation for terminal statuses.",
            ],
            failure_handling=[
                "On validation error, surface exact field-level issue.",
                "On flow/apex exception, log correlation key and return fallback guidance.",
            ],
        )
    ]

    renderer = MasterBlueprintRenderer()
    path = renderer.write_html(output_path, extraction, run_metadata, analyses, ai_sections)
    typer.echo(f"Master blueprint generated: {path}")


if __name__ == "__main__":
    app()


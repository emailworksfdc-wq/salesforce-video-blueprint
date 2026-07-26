"""In-process pipeline API: capture file in, derived-and-scored spec out.

This is the reusable core that both the CLI and the MCP server call. It exists
because assembling the pipeline by hand takes seven imports and a dozen lines of
glue, and every consumer that did that by hand would be coupled to internal
module layout.

The contract here is the same as everywhere else in this project: the returned
spec claims only what the capture proved, and the provenance block says exactly
where the evidence came from. Nothing in this module can promote a mock run to
looking like a real one.

Typical use:

    from sf_video_blueprint.pipeline import run_pipeline

    result = run_pipeline("dom_capture.jsonl", org_url="https://x.sandbox.my.salesforce.com")
    print(result.spec.intent, result.score.total, result.score.passed)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from uuid import uuid4

from .correlation import correlate_all
from .dom_capture import parse_capture_file, validate_trace
from .dom_extractor import DomCaptureExtractor
from .markers import extraction_is_real, telemetry_is_real
from .replay import NoopUIAdapter, ReplayEngine, ReplayRunMetadata
from .spec_builder import DerivedAgentSpec, build_agent_spec
from .spec_score import SpecScore, score_spec
from .telemetry import MockTelemetryCollector, TelemetryRegistry


class CaptureRejected(RuntimeError):
    """The capture failed integrity validation and no spec was built.

    Raised for findings prefixed `SECURITY CRITICAL:` or `DATA LOSS:`. Building a
    spec on top of a capture that leaked a secret or lost half its events would
    produce an artifact that looks trustworthy and is not, so this is fail-closed
    by design. `findings` carries the reasons.
    """

    def __init__(self, message: str, findings: list[str]) -> None:
        super().__init__(message)
        self.findings = findings


@dataclass(frozen=True)
class PipelineResult:
    """Everything one pipeline run produced, plus how much to trust it."""

    spec: DerivedAgentSpec
    score: SpecScore
    provenance: dict[str, str]

    #: Non-fatal notes from parsing and extraction (dropped noise, weak targets,
    #: coalesced inputs). Worth surfacing: they explain a thin spec.
    warnings: list[str] = field(default_factory=list)

    #: Lines the parser could not turn into events, as (line number, reason).
    #: Non-empty means the recording was partially discarded even when the run
    #: was allowed to proceed — the integrity gate only refuses at >=50% loss, so
    #: this is the only place a smaller loss becomes visible to a caller.
    skipped_lines: list[tuple[int, str]] = field(default_factory=list)

    #: Events that survived parsing, and actions after noise reduction.
    events_parsed: int = 0
    actions_extracted: int = 0

    #: Events the recorder reported writing that the parser never received, or
    #: None when there was no manifest to compare against (DEFECT L4-7).
    #: `None` means UNKNOWABLE, not zero — a capture with no manifest has no
    #: witness for this class of loss.
    manifest_gap: int | None = None

    @property
    def loss_ratio(self) -> float:
        """Fraction of content lines the parser could not use, 0.0–1.0."""
        total = self.events_parsed + len(self.skipped_lines)
        if total == 0:
            return 0.0
        return len(self.skipped_lines) / total

    @property
    def evidence_is_complete(self) -> bool:
        """True only when nothing was lost through EITHER channel.

        Distinct from `evidence_is_real`: a capture can be entirely real and
        still be missing 40% of the session. Realness is about provenance,
        completeness is about how much of the session survived.
        """
        return not self.skipped_lines and not self.manifest_gap

    @property
    def evidence_is_real(self) -> bool:
        """True only when BOTH extraction and telemetry came from real sources.

        A run with a real capture but mock telemetry is False. That is the point:
        partial realness is not realness, and the score gate treats it the same
        way.
        """
        return extraction_is_real(
            self.provenance.get("extraction_source")
        ) and telemetry_is_real(self.provenance.get("telemetry_source"))

    def summary(self) -> dict[str, Any]:
        """A JSON-safe digest suitable for logging or returning over a wire."""
        return {
            "intent": self.spec.intent,
            "confidence": self.spec.confidence,
            "objects_touched": list(self.spec.objects_touched),
            "entity_count": len(self.spec.entities),
            "step_count": len(self.spec.orchestration_steps),
            "unknowns": list(self.spec.unknowns),
            "score": self.score.total,
            "max_score": self.score.max_total,
            "band": self.score.band,
            "passed": self.score.passed,
            "blocking_issues": list(self.score.blocking_issues),
            "recommendations": list(self.score.recommendations),
            "dimensions": {
                name: {"score": d.score, "max_score": d.max_score, "findings": list(d.findings)}
                for name, d in self.score.dimensions.items()
            },
            "provenance": dict(self.provenance),
            "evidence_is_real": self.evidence_is_real,
            "events_parsed": self.events_parsed,
            "actions_extracted": self.actions_extracted,
            # Surfaced unconditionally, and as a count plus detail, because a
            # silent partial capture is the failure mode most likely to make a
            # thin spec look like a complete one.
            "skipped_line_count": len(self.skipped_lines),
            "skipped_lines": [
                {"line": line, "reason": reason} for line, reason in self.skipped_lines
            ],
            # DEFECT L4-7: the ratio and the recorder-side gap, not just the raw
            # count. "3 skipped lines" reads as negligible until you know the
            # capture only had 8 lines, and a truncated capture leaves NO skipped
            # lines at all — only `manifest_gap` witnesses that one.
            "loss_ratio": round(self.loss_ratio, 4),
            "manifest_gap": self.manifest_gap,
            "evidence_is_complete": self.evidence_is_complete,
            "warnings": list(self.warnings),
        }


def run_pipeline(
    capture_path: str | Path,
    *,
    org_url: str,
    username: str = "analyst@example.com",
    profile_name: str = "System Administrator",
    run_id: str | None = None,
    telemetry_collector: Any | None = None,
) -> PipelineResult:
    """Parse a capture, derive an agent spec from it, and score the result.

    Offline and side-effect free **by default**: no org is contacted, no browser
    is launched, nothing is written to disk. Default telemetry is mocked, which
    means the result is stamped `telemetry_source: "mock"` and will not pass the
    score gate. That is correct — a spec cannot be called evidence-backed without
    observed server-side behaviour.

    Args:
        capture_path: A `dom_capture.jsonl` trace from the recorder.
        org_url: The org the recording came from. Recorded as metadata only.
        username: Replay metadata for the audit trail.
        profile_name: Replay metadata for the audit trail.
        run_id: Optional stable run id. Generated when omitted.
        telemetry_collector: Optional collector implementing
            :class:`~sf_video_blueprint.telemetry.TelemetryCollector`. Pass a
            :class:`~sf_video_blueprint.live_telemetry.LiveOrgTelemetryCollector`
            to observe a real org; omit for the offline mock path.

            The resulting `telemetry_source` is derived from what the collector
            actually observed, never from the caller's intent. A live collector
            that came back empty is stamped `"unavailable"`, not `"live-org"` —
            passing a live collector is a request to observe, not a licence to
            claim observation. See :func:`_resolve_telemetry_source`.

    Raises:
        FileNotFoundError: The capture file does not exist.
        CaptureRejected: The capture failed integrity validation.
    """
    path = Path(capture_path)
    if not path.is_file():
        raise FileNotFoundError(f"No capture file at {path}")

    trace = parse_capture_file(path)

    # Fail closed BEFORE deriving anything. A leaked secret or a materially
    # truncated capture must stop the run, not colour a caveat in the output.
    findings = validate_trace(trace)
    fatal = [f for f in findings if f.startswith(("SECURITY CRITICAL:", "DATA LOSS:"))]
    if fatal:
        raise CaptureRejected(
            "Capture failed integrity validation; no spec was built.", fatal
        )

    extraction = DomCaptureExtractor().extract(path)

    metadata = ReplayRunMetadata(
        run_id=run_id or f"run-{uuid4().hex[:8]}",
        org_url=org_url,
        username=username,
        profile_name=profile_name,
        role_name=None,
        environment="mock",
    )

    replay_events = ReplayEngine(adapter=NoopUIAdapter()).replay(metadata, extraction.actions)

    if telemetry_collector is None:
        registry = _collect_mock_telemetry(extraction.actions, metadata.run_id)
        telemetry_source = "mock"
    else:
        registry = _collect_telemetry(
            telemetry_collector, extraction.actions, metadata.run_id
        )
        telemetry_source = _resolve_telemetry_source(registry)

    analyses = correlate_all(
        extraction.actions, replay_events, registry.events, registry.snapshots
    )

    spec = build_agent_spec(extraction.actions, analyses)
    provenance = {
        "extraction_source": "dom-capture",
        "telemetry_source": telemetry_source,
        "replay_source": "noop",
        "agent_spec_source": "derived",
        "run_id": metadata.run_id,
        "source_path": str(path),
    }

    return PipelineResult(
        spec=spec,
        score=score_spec(spec, provenance=provenance),
        provenance=provenance,
        warnings=list(trace.warnings) + list(extraction.warnings),
        skipped_lines=trace.skipped_lines,
        events_parsed=len(trace.events),
        actions_extracted=len(extraction.actions),
        manifest_gap=trace.manifest_gap,
    )


def _collect_mock_telemetry(actions: list[Any], run_id: str) -> TelemetryRegistry:
    """Populate a telemetry registry with fabricated data for each step.

    Every event this produces is invented. See `MockTelemetryCollector` — the run
    is stamped `telemetry_source: "mock"` so the score gate refuses it.
    """
    return _collect_telemetry(MockTelemetryCollector(), actions, run_id)


def _collect_telemetry(
    collector: Any, actions: list[Any], run_id: str
) -> TelemetryRegistry:
    """Drive any `TelemetryCollector` over every extracted action."""
    registry = TelemetryRegistry()
    for action in actions:
        registry.collect_step(collector, run_id, action.step_id)
    return registry


def _resolve_telemetry_source(registry: TelemetryRegistry) -> str:
    """Derive the provenance stamp from what a collector actually returned.

    A caller supplying a live collector is asking for observation, not asserting
    it happened. An org that returned nothing — wrong window, no tracked record,
    expired session, unlicensed surface — yields an empty registry, and an empty
    registry is stamped `"unavailable"`, which is absent from
    `markers.REAL_TELEMETRY_SOURCES` and so blocks at the score gate.

    Deliberately derived from the collected rows rather than from the collector's
    type: a `LiveOrgTelemetryCollector` pointed at an org with no matching history
    is indistinguishable, evidence-wise, from no org at all, and must be reported
    the same way.
    """
    if registry.events or registry.snapshots:
        return "live-org"
    return "unavailable"

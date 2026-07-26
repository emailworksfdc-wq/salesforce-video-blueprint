from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

from .correlation import StepAnalysis
from .models import ActionExtractionBundle
from .replay import ReplayRunMetadata


@dataclass(slots=True)
class AgentBlueprintSection:
    intent: str
    required_entities: list[str]
    orchestration_steps: list[str]
    guardrails: list[str]
    failure_handling: list[str]
    derived: bool = False
    """True when this section was derived from observed run data rather than
    supplied as a static example. Rendered as a provenance badge."""


@dataclass(slots=True)
class DataProvenance:
    """Where each part of the blueprint came from.

    The report is explicitly an audit artifact, so simulated or placeholder
    content must never be visually indistinguishable from real org evidence.
    """

    extraction_source: str = "stub"     # "stub" | "cv" | "dom-capture"
    telemetry_source: str = "mock"      # "mock" | "live-org"
    replay_source: str = "noop"         # "noop" | "browser"
    agent_spec_source: str = "static-example"  # "static-example" | "derived"

    # Positive list of extraction sources that represent real, non-simulated data
    # Fail-safe design: an unrecognised source is treated as simulated/unknown rather
    # than silently trusted. This is the correct failure mode for an audit artifact.
    _REAL_EXTRACTION_SOURCES = frozenset({"dom-capture", "cv"})

    @property
    def is_simulated(self) -> bool:
        return (
            self.telemetry_source == "mock"
            or self.extraction_source not in self._REAL_EXTRACTION_SOURCES
            or self.replay_source == "noop"
        )

    @property
    def simulated_parts(self) -> list[str]:
        parts: list[str] = []
        if self.extraction_source not in self._REAL_EXTRACTION_SOURCES:
            parts.append("action extraction (video is not decoded; steps are placeholders)")
        if self.replay_source == "noop":
            parts.append("replay (no browser drove the org; every step auto-succeeds)")
        if self.telemetry_source == "mock":
            parts.append("telemetry and data deltas (fabricated sample values, not org data)")
        if self.agent_spec_source == "static-example":
            parts.append("agent spec (static example, not derived from this recording)")
        return parts


class MasterBlueprintRenderer:
    def __init__(self, template_dir: Path | None = None) -> None:
        if template_dir is None:
            template_dir = Path(__file__).parent / "templates"
        # NOTE: select_autoescape() keys off the file extension, and our template
        # is named "master_blueprint.html.j2" -> suffix ".j2", which is NOT in the
        # enabled list. That silently disabled escaping for every value in the
        # report, including org-controlled strings (record names, validation
        # messages, OCR text). Force it on for .j2 templates explicitly.
        self.env = Environment(
            loader=FileSystemLoader(template_dir),
            autoescape=select_autoescape(
                enabled_extensions=("html", "xml", "j2"),
                default_for_string=True,
                default=True,
            ),
        )

    def render(
        self,
        extraction: ActionExtractionBundle,
        run: ReplayRunMetadata,
        analyses: list[StepAnalysis],
        agent_sections: list[AgentBlueprintSection],
        provenance: DataProvenance | None = None,
    ) -> str:
        template = self.env.get_template("master_blueprint.html.j2")
        return template.render(
            generated_at=datetime.now(timezone.utc).isoformat(),
            extraction=extraction,
            run=run,
            analyses=analyses,
            agent_sections=agent_sections,
            provenance=provenance or DataProvenance(),
        )

    def write_html(
        self,
        output_path: Path,
        extraction: ActionExtractionBundle,
        run: ReplayRunMetadata,
        analyses: list[StepAnalysis],
        agent_sections: list[AgentBlueprintSection],
        provenance: DataProvenance | None = None,
    ) -> Path:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(
            self.render(extraction, run, analyses, agent_sections, provenance),
            encoding="utf-8",
        )
        return output_path


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


class MasterBlueprintRenderer:
    def __init__(self, template_dir: Path | None = None) -> None:
        if template_dir is None:
            template_dir = Path(__file__).parent / "templates"
        self.env = Environment(
            loader=FileSystemLoader(template_dir),
            autoescape=select_autoescape(["html", "xml"]),
        )

    def render(
        self,
        extraction: ActionExtractionBundle,
        run: ReplayRunMetadata,
        analyses: list[StepAnalysis],
        agent_sections: list[AgentBlueprintSection],
    ) -> str:
        template = self.env.get_template("master_blueprint.html.j2")
        return template.render(
            generated_at=datetime.now(timezone.utc).isoformat(),
            extraction=extraction,
            run=run,
            analyses=analyses,
            agent_sections=agent_sections,
        )

    def write_html(
        self,
        output_path: Path,
        extraction: ActionExtractionBundle,
        run: ReplayRunMetadata,
        analyses: list[StepAnalysis],
        agent_sections: list[AgentBlueprintSection],
    ) -> Path:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(
            self.render(extraction, run, analyses, agent_sections),
            encoding="utf-8",
        )
        return output_path


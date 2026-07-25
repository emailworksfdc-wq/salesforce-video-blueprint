from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol
from uuid import uuid4

from .models import ActionExtractionBundle, ActionType, EvidenceArtifact, EvidenceType, ExtractedAction, UIContext


class VideoActionExtractor(Protocol):
    def extract(self, video_path: Path) -> ActionExtractionBundle: ...


class HeuristicVideoExtractor:
    """
    Production placeholder extractor.

    Replace internals with CV/OCR/object-detection based timeline extraction.
    The class keeps the canonical output contract stable for downstream steps.
    """

    def extract(self, video_path: Path) -> ActionExtractionBundle:
        if not video_path.exists():
            raise FileNotFoundError(f"Video file not found: {video_path}")

        now = datetime.now(timezone.utc)
        frame_artifact = EvidenceArtifact(
            artifact_id="evid-001",
            evidence_type=EvidenceType.VIDEO_FRAME,
            path_or_uri=f"file://{video_path}",
            captured_at=now,
            confidence=0.8,
            metadata={
                "extractor": "heuristic",
                "note": "Baseline extraction. Replace with CV pipeline for production.",
            },
        )

        action = ExtractedAction(
            step_id="step-001",
            sequence=1,
            timestamp_ms=1000,
            action_type=ActionType.CLICK,
            target="button:Save",
            value=None,
            ui_context=UIContext(page_title="Unknown", url=None),
            confidence=0.75,
            inferred_intent="Commit the current form",
            expected_outcome="Record state is updated",
            evidence_ids=[frame_artifact.artifact_id],
        )

        return ActionExtractionBundle(
            recording_id=f"rec-{uuid4().hex[:8]}",
            source_video_path=str(video_path),
            extracted_at=now,
            actions=[action],
            evidence=[frame_artifact],
            warnings=[
                "Heuristic extraction in use; enable CV/OCR plugins for full fidelity.",
            ],
        )


from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class ActionType(str, Enum):
    CLICK = "click"
    INPUT = "input"
    NAVIGATE = "navigate"
    SELECT = "select"
    SUBMIT = "submit"
    WAIT = "wait"
    SCROLL = "scroll"
    HOTKEY = "hotkey"
    ASSERT = "assert"


class EvidenceType(str, Enum):
    SCREENSHOT = "screenshot"
    OCR = "ocr"
    DOM_SNAPSHOT = "dom_snapshot"
    VIDEO_FRAME = "video_frame"
    LOG_SNIPPET = "log_snippet"
    NETWORK_EVENT = "network_event"
    DATA_SNAPSHOT = "data_snapshot"


class UIContext(BaseModel):
    page_title: str | None = None
    app_name: str | None = None
    object_name: str | None = None
    view_name: str | None = None
    modal_name: str | None = None
    selector_hint: str | None = None
    url: str | None = None


class EvidenceArtifact(BaseModel):
    artifact_id: str
    evidence_type: EvidenceType
    path_or_uri: str
    captured_at: datetime
    note: str | None = None
    confidence: float = Field(ge=0.0, le=1.0)
    metadata: dict[str, Any] = Field(default_factory=dict)


class ExtractedAction(BaseModel):
    step_id: str
    sequence: int = Field(ge=1)
    timestamp_ms: int = Field(ge=0)
    action_type: ActionType
    target: str
    value: str | None = None
    ui_context: UIContext = Field(default_factory=UIContext)
    confidence: float = Field(ge=0.0, le=1.0)
    inferred_intent: str | None = None
    preconditions: list[str] = Field(default_factory=list)
    expected_outcome: str | None = None
    evidence_ids: list[str] = Field(default_factory=list)


class ActionExtractionBundle(BaseModel):
    recording_id: str
    source_video_path: str
    extracted_at: datetime
    actions: list[ExtractedAction]
    evidence: list[EvidenceArtifact]
    warnings: list[str] = Field(default_factory=list)


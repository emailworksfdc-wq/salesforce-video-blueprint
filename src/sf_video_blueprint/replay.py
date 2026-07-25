from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
import random
import time
from typing import Protocol

from .models import ExtractedAction


class ReplayStatus(str, Enum):
    SUCCESS = "success"
    FAILED = "failed"
    SKIPPED = "skipped"
    RETRIED = "retried"


@dataclass(slots=True)
class RetryPolicy:
    max_attempts: int = 3
    initial_delay_ms: int = 500
    backoff_multiplier: float = 2.0
    retryable_error_codes: tuple[str, ...] = (
        "ELEMENT_NOT_FOUND",
        "TIMEOUT",
        "STALE_REFERENCE",
        "TRANSIENT_NAVIGATION",
        "TARGET_CLOSED",
    )


@dataclass(slots=True)
class ReplayRunMetadata:
    run_id: str
    org_url: str
    username: str
    profile_name: str
    role_name: str | None
    started_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    environment: str = "unknown"
    browser: str = "chromium"
    release_label: str | None = None


@dataclass(slots=True)
class ReplayEvent:
    run_id: str
    step_id: str
    attempted_at: datetime
    status: ReplayStatus
    attempt_no: int
    duration_ms: int
    message: str
    error_code: str | None = None


class SalesforceUIAdapter(Protocol):
    def open_org(self, org_url: str) -> None: ...
    def perform_action(self, action: ExtractedAction) -> tuple[bool, str, str | None]: ...


class ReplayEngine:
    def __init__(self, adapter: SalesforceUIAdapter, retry_policy: RetryPolicy | None = None) -> None:
        self.adapter = adapter
        self.retry_policy = retry_policy or RetryPolicy()

    def replay(
        self,
        metadata: ReplayRunMetadata,
        actions: list[ExtractedAction],
    ) -> list[ReplayEvent]:
        self.adapter.open_org(metadata.org_url)
        events: list[ReplayEvent] = []
        for action in sorted(actions, key=lambda a: a.sequence):
            events.extend(self._run_step(metadata.run_id, action))
        return events

    def _run_step(self, run_id: str, action: ExtractedAction) -> list[ReplayEvent]:
        result: list[ReplayEvent] = []
        attempt_no = 0
        while attempt_no < self.retry_policy.max_attempts:
            attempt_no += 1
            started = datetime.now(timezone.utc)
            ok, message, error_code = self.adapter.perform_action(action)
            elapsed = int((datetime.now(timezone.utc) - started).total_seconds() * 1000)
            status = ReplayStatus.SUCCESS if ok else ReplayStatus.FAILED
            if not ok and attempt_no < self.retry_policy.max_attempts:
                status = ReplayStatus.RETRIED
            result.append(
                ReplayEvent(
                    run_id=run_id,
                    step_id=action.step_id,
                    attempted_at=started,
                    status=status,
                    attempt_no=attempt_no,
                    duration_ms=elapsed,
                    message=message,
                    error_code=error_code,
                )
            )
            if ok:
                break
            if error_code not in self.retry_policy.retryable_error_codes:
                break
            delay_ms = int(
                self.retry_policy.initial_delay_ms
                * (self.retry_policy.backoff_multiplier ** (attempt_no - 1))
            )
            jitter_ms = int(delay_ms * random.uniform(-0.2, 0.2))
            time.sleep(max(delay_ms + jitter_ms, 0) / 1000)
        return result


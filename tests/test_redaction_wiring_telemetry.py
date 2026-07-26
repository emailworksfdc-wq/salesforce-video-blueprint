"""Live-org telemetry values must be scrubbed before they reach artifacts.

This covers the leak channel that the extraction choke point structurally cannot
see. `DomCaptureExtractor._redact_actions` scrubs everything that came out of the
capture file, but `ObjectSnapshot.before` / `.after` are whole records fetched
from the org by `SalesforceRestClient.get_record` AFTER extraction has finished.
They flow into `spec_builder._derive_entities`, which interpolates the before and
after values directly into entity evidence details, and from there into the spec
JSON and the HTML report.

Measured, not hypothetical: with `--mode live --track-record Case:<id>`, a Case
whose Description contains a token puts that token in `agent-spec.json` verbatim.
Lane 02 is recording a real org session, so this is the live path.

`TelemetryRegistry.collect_step` is the single funnel — both `cli.py` and
`pipeline.py` obtain every event and snapshot through it, and nothing else in the
tree appends to `.events` or `.snapshots`. Scrubbing there covers both callers.

CANARY HYGIENE: planted values are obviously fake and never interpolated into an
assertion message.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

from sf_video_blueprint.correlation import correlate_all
from sf_video_blueprint.replay import NoopUIAdapter, ReplayEngine, ReplayRunMetadata
from sf_video_blueprint.spec_builder import build_agent_spec
from sf_video_blueprint.telemetry import (
    CorrelationKey,
    ObjectSnapshot,
    TelemetryCollector,
    TelemetryEvent,
    TelemetryLayer,
    TelemetryRegistry,
)

# AWS-key-SHAPED, body is the literal word FAKE. Unusable as a credential.
PLANTED_KEY_SHAPED = "AKIAFAKEFAKEFAKE0002"

# RFC 2606 reserves .invalid — cannot resolve or receive mail.
PLANTED_EMAIL = "telemetry-leak@example.invalid"

# Legitimate org data that MUST survive: a real 18-char id (valid checksum) and
# ordinary field values. Record ids are retained on purpose — they are the audit
# trail that ties a spec back to the record it was derived from.
LEGIT_RECORD_ID = "5008d000004Xy9tAAC"
LEGIT_STATUS_BEFORE = "New"
LEGIT_STATUS_AFTER = "Escalated"


class _StubCollector(TelemetryCollector):
    """Stands in for `SalesforceTelemetryCollector` returning real org data.

    Shaped exactly like the live collector's output: `after` is a whole record as
    returned by `get_record`, so every field value the org holds is present.
    """

    def __init__(self, snapshot: ObjectSnapshot, event: TelemetryEvent) -> None:
        self._snapshot = snapshot
        self._event = event

    def collect_for_step(self, run_id: str, step_id: str) -> list[TelemetryEvent]:
        return [self._event]

    def snapshot_changes(self, run_id: str, step_id: str) -> list[ObjectSnapshot]:
        return [self._snapshot]


def _key() -> CorrelationKey:
    return CorrelationKey(
        run_id="run-test", step_id="step-001", event_time=datetime.now(UTC)
    )


def _snapshot_with(after: dict) -> ObjectSnapshot:
    return ObjectSnapshot(
        correlation=_key(),
        object_api_name="Case",
        record_id=LEGIT_RECORD_ID,
        before={"Status": LEGIT_STATUS_BEFORE, "Description": "original text"},
        after=after,
        changed_fields=sorted(after),
    )


def _event_with(payload: dict) -> TelemetryEvent:
    return TelemetryEvent(
        correlation=_key(),
        layer=TelemetryLayer.FLOW,
        event_name="FlowInterviewFetch",
        status="ok",
        payload=payload,
    )


def _collect(snapshot: ObjectSnapshot, event: TelemetryEvent) -> TelemetryRegistry:
    """Route through the real funnel every caller uses."""
    registry = TelemetryRegistry()
    registry.collect_step(_StubCollector(snapshot, event), "run-test", "step-001")
    return registry


# ---------------------------------------------------------------------------
# Leak blocked
# ---------------------------------------------------------------------------


def test_secret_in_field_value_is_scrubbed_at_collection() -> None:
    """A token sitting in a real Case field must not survive collection."""
    registry = _collect(
        _snapshot_with({"Status": LEGIT_STATUS_AFTER, "Description": f"key {PLANTED_KEY_SHAPED}"}),
        _event_with({}),
    )

    blob = json.dumps([s.after for s in registry.snapshots])
    assert PLANTED_KEY_SHAPED not in blob, "a key-shaped secret survived telemetry collection"


def test_secret_in_before_value_is_scrubbed() -> None:
    """`before` is also a fetched record and leaks the same way."""
    snapshot = _snapshot_with({"Status": LEGIT_STATUS_AFTER})
    snapshot.before = {"Status": LEGIT_STATUS_BEFORE, "Description": f"old key {PLANTED_KEY_SHAPED}"}

    registry = _collect(snapshot, _event_with({}))

    assert PLANTED_KEY_SHAPED not in json.dumps(
        [s.before for s in registry.snapshots]
    ), "a key-shaped secret survived in the before-image"


def test_secret_in_telemetry_payload_is_scrubbed() -> None:
    """`payload` holds raw SOQL result rows — whole records from the org."""
    registry = _collect(
        _snapshot_with({"Status": LEGIT_STATUS_AFTER}),
        _event_with({"records": [{"Id": LEGIT_RECORD_ID, "Note__c": f"token {PLANTED_KEY_SHAPED}"}]}),
    )

    assert PLANTED_KEY_SHAPED not in json.dumps(
        [e.payload for e in registry.events]
    ), "a key-shaped secret survived in a telemetry payload"


def test_secret_does_not_reach_the_derived_spec(tmp_path) -> None:
    """End to end: the spec JSON is the artifact operators share.

    `_derive_entities` interpolates before/after values into evidence details, so
    this is the path that actually put the secret on disk.
    """
    from sf_video_blueprint.dom_extractor import DomCaptureExtractor

    event_json = {
        "v": 1, "seq": 1, "t": 1737830000000, "type": "input",
        "url": "https://example.my.salesforce.com/lightning/r/Case/view",
        "frame_path": [],
        "selectors": {"test_id": None, "aria": None, "role_name": None, "label_for": None,
                      "sf_field": "Description", "css_path": "input[name=Description]",
                      "text": None, "xpath": None},
        "element": {"tag": "input", "type": "text", "name": "Description", "id": "Description",
                    "classes": [], "aria_label": "Description", "text": "Description",
                    "is_in_modal": False, "modal_label": None, "shadow_depth": 0},
        "value": "some notes", "value_redacted": False,
        "sf": {"object": "Case", "record_id": LEGIT_RECORD_ID,
               "page_type": "record", "app": "Service"},
    }
    capture = tmp_path / "c.jsonl"
    capture.write_text(json.dumps(event_json) + "\n", encoding="utf-8")

    bundle = DomCaptureExtractor().extract(capture)
    metadata = ReplayRunMetadata(
        run_id="run-test",
        org_url="https://example.my.salesforce.com",
        username="analyst@example.com",
        profile_name="System Administrator",
        role_name=None,
        environment="live",
    )
    replay_events = ReplayEngine(adapter=NoopUIAdapter()).replay(metadata, bundle.actions)

    action = bundle.actions[0]
    at = datetime.fromtimestamp(action.timestamp_ms / 1000, tz=UTC)
    snapshot = ObjectSnapshot(
        correlation=CorrelationKey(run_id="run-test", step_id=action.step_id, event_time=at),
        object_api_name="Case",
        record_id=LEGIT_RECORD_ID,
        before={"Description": "original text"},
        after={"Description": f"rotated key {PLANTED_KEY_SHAPED} for {PLANTED_EMAIL}"},
        changed_fields=["Description"],
    )
    registry = TelemetryRegistry()
    registry.collect_step(_StubCollector(snapshot, _event_with({})), "run-test", action.step_id)

    analyses = correlate_all(
        bundle.actions, replay_events, registry.events, registry.snapshots
    )
    spec_json = json.dumps(build_agent_spec(bundle.actions, analyses).to_dict())

    assert PLANTED_KEY_SHAPED not in spec_json, "a key-shaped secret reached the derived spec"
    assert PLANTED_EMAIL not in spec_json, "an email reached the derived spec"


def test_manual_event_payload_is_scrubbed() -> None:
    """`append_manual_event` is the registry's other entry point and must scrub too.

    It appends straight to `self.events`, bypassing `collect_step` entirely. No
    in-tree caller uses it today, so this test is guarding the shape of the
    guarantee rather than a live leak: "everything in this registry is scrubbed",
    not "everything that arrived by the expected route is scrubbed".
    """
    registry = TelemetryRegistry()
    registry.append_manual_event(
        run_id="run-test",
        step_id="step-001",
        layer=TelemetryLayer.FLOW,
        event_name="ManualNote",
        status="ok",
        payload={"note": f"key {PLANTED_KEY_SHAPED} seen", "contact": PLANTED_EMAIL},
    )

    blob = json.dumps([e.payload for e in registry.events])
    assert PLANTED_KEY_SHAPED not in blob, "a key-shaped secret survived a manual event"
    assert PLANTED_EMAIL not in blob, "an email survived a manual event"


def test_manual_event_preserves_legitimate_payload() -> None:
    """The scrub must not corrupt an ordinary manual annotation."""
    registry = TelemetryRegistry()
    registry.append_manual_event(
        run_id="run-test",
        step_id="step-001",
        layer=TelemetryLayer.FLOW,
        event_name="ManualNote",
        status="ok",
        payload={"note": "Operator confirmed the panel swap", "attempts": 2, "retried": False},
    )

    payload = registry.events[0].payload
    assert payload["note"] == "Operator confirmed the panel swap"
    assert payload["attempts"] == 2
    assert payload["retried"] is False


# ---------------------------------------------------------------------------
# False-positive safety
# ---------------------------------------------------------------------------


def test_legitimate_field_values_survive_collection() -> None:
    """Ordinary picklist and text values must come through byte-identical.

    If scrubbing corrupted `Status`, every derived spec would describe a
    transition the org never made — worse than no redaction at all.
    """
    registry = _collect(
        _snapshot_with({"Status": LEGIT_STATUS_AFTER, "Description": "Panel replaced on site"}),
        _event_with({"records": [{"Id": LEGIT_RECORD_ID, "Status": "Completed"}]}),
    )

    snap = registry.snapshots[0]
    assert snap.after["Status"] == LEGIT_STATUS_AFTER
    assert snap.after["Description"] == "Panel replaced on site"
    assert snap.before["Status"] == LEGIT_STATUS_BEFORE
    assert registry.events[0].payload["records"][0]["Status"] == "Completed"


def test_record_ids_and_changed_fields_are_retained() -> None:
    """Record ids and field API names are the audit trail — never redacted.

    `changed_fields` drives entity derivation; masking a field name there would
    silently change what the agent spec asks for.
    """
    registry = _collect(
        _snapshot_with({"Status": LEGIT_STATUS_AFTER, "Description": "ok"}),
        _event_with({"records": [{"Id": LEGIT_RECORD_ID}]}),
    )

    snap = registry.snapshots[0]
    assert snap.record_id == LEGIT_RECORD_ID, "record id was redacted; audit trail lost"
    assert snap.object_api_name == "Case"
    assert snap.changed_fields == ["Description", "Status"]
    assert registry.events[0].payload["records"][0]["Id"] == LEGIT_RECORD_ID


def test_luhn_passing_epoch_timestamps_survive() -> None:
    """Numeric telemetry must not be mistaken for a credit card.

    Measured: ~10% of epoch-millisecond values pass the Luhn check (1700000000004
    is one). A naive text pass over serialized telemetry would rewrite them as
    `[REDACTED:credit_card]`, corrupting the correlation timeline.
    """
    luhn_passing_epoch = 1700000000004
    registry = _collect(
        _snapshot_with({"Status": LEGIT_STATUS_AFTER, "LastModifiedEpoch__c": luhn_passing_epoch}),
        _event_with({"durationMs": luhn_passing_epoch, "soql": "SELECT Id FROM Case LIMIT 1"}),
    )

    assert registry.snapshots[0].after["LastModifiedEpoch__c"] == luhn_passing_epoch
    assert registry.events[0].payload["durationMs"] == luhn_passing_epoch


def test_non_string_values_pass_through_unchanged() -> None:
    """Booleans, numbers, and None must not be stringified by the scrub pass."""
    registry = _collect(
        _snapshot_with({"IsClosed": False, "NumberOfEmployees": 42, "Rating": None}),
        _event_with({"ok": True, "count": 0}),
    )

    after = registry.snapshots[0].after
    assert after["IsClosed"] is False
    assert after["NumberOfEmployees"] == 42
    assert after["Rating"] is None
    assert registry.events[0].payload["ok"] is True
    assert registry.events[0].payload["count"] == 0

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

There are now TWO scrub points, and the order matters:

1. `TelemetryRegistry.collect_step` / `.append_manual_event` scrub on ingest. This
   is the boundary — every caller is covered, including `pipeline.py` and any test
   helper that builds a registry directly, without having to know the rule exists.
2. `redaction.scrub_collected_telemetry` still runs in `cli.py` after collection
   and before `correlate_all`, as defence in depth over anything a future caller
   might append by hand.

Ingest scrubbing was originally filed as a recommendation rather than done, because
`telemetry.py` belonged to another lane (orchestrator bulletin 02). That owner took
the handoff, so the two tests that pinned the gap are gone and their inverses
(`test_registry_ingest_is_itself_a_boundary` and friends) pin the guarantee.

CANARY HYGIENE: planted values are obviously fake and never interpolated into an
assertion message.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

from sf_video_blueprint.correlation import correlate_all
from sf_video_blueprint.redaction import scrub_collected_telemetry
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
    """Collect then scrub, in the same order `cli.py` does.

    Ingest now scrubs on its own, and the CLI's pass still runs after it. This
    helper keeps BOTH calls so the tests exercise the shipped sequence rather than a
    convenience shortcut — and so the second pass is exercised for idempotence on
    every case below, not just the one test that names it.
    """
    registry = TelemetryRegistry()
    registry.collect_step(_StubCollector(snapshot, event), "run-test", "step-001")
    scrub_collected_telemetry(registry.events, registry.snapshots)
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
    scrub_collected_telemetry(registry.events, registry.snapshots)

    analyses = correlate_all(
        bundle.actions, replay_events, registry.events, registry.snapshots
    )
    spec_json = json.dumps(build_agent_spec(bundle.actions, analyses).to_dict())

    assert PLANTED_KEY_SHAPED not in spec_json, "a key-shaped secret reached the derived spec"
    assert PLANTED_EMAIL not in spec_json, "an email reached the derived spec"


def test_scrub_reports_categories_so_the_run_can_say_it_fired() -> None:
    """A silent control cannot be audited; the CLI echoes the union of both passes.

    Ingest scrubs first, so by the time `scrub_collected_telemetry` runs there is
    nothing left for it to find and it correctly reports nothing. The categories the
    CLI prints therefore come from whichever pass actually fired. Asserting the
    union is what keeps the audit line honest wherever the scrub happens to land.
    """
    registry = TelemetryRegistry()
    registry.collect_step(
        _StubCollector(
            _snapshot_with({"Description": f"key {PLANTED_KEY_SHAPED} for {PLANTED_EMAIL}"}),
            _event_with({}),
        ),
        "run-test",
        "step-001",
    )

    second_pass = scrub_collected_telemetry(registry.events, registry.snapshots)
    categories = [*registry.redaction_categories, *second_pass]

    assert "aws_key" in categories
    assert "email" in categories
    # Categories name the KIND of value, never the value itself.
    assert PLANTED_KEY_SHAPED not in " ".join(categories)


# ---------------------------------------------------------------------------
# Ingest IS the boundary (lane 06 -> lane 04 handoff, now closed)
# ---------------------------------------------------------------------------
#
# These two tests replace `test_registry_ingest_is_not_itself_a_boundary` and
# `test_manual_event_payload_is_a_known_uncovered_path`, which asserted the gap
# while `telemetry.py` was owned by another lane. That owner (lane 04) has since
# moved the scrub onto ingest, so the gap-asserting tests were deleted exactly as
# their own assertion messages instructed. Their inverses live here: the property
# that used to be pinned as a known limit is now pinned as a guarantee.


def test_registry_ingest_is_itself_a_boundary() -> None:
    """A caller that never calls the CLI's scrub still gets clean records.

    This is the whole point of scrubbing at ingest rather than at one call site:
    the guarantee belongs to `TelemetryRegistry`, so it holds for every present and
    future caller, not just the two in-tree ones. `pipeline.py` and any test helper
    that builds a registry directly are covered without knowing the rule exists.
    """
    registry = TelemetryRegistry()
    registry.collect_step(
        _StubCollector(
            _snapshot_with({"Description": f"key {PLANTED_KEY_SHAPED}"}), _event_with({})
        ),
        "run-test",
        "step-001",
    )

    # NOTE: no scrub_collected_telemetry call. That is the assertion.
    assert PLANTED_KEY_SHAPED not in json.dumps([s.after for s in registry.snapshots]), (
        "a key-shaped secret survived registry ingest without an explicit scrub call"
    )


def test_ingest_scrub_covers_before_images_and_event_payloads() -> None:
    """All three leak-bearing surfaces, not just `after`.

    `before` is a fetched record too, and `payload` holds raw SOQL rows. Scrubbing
    only the one surface a test happened to plant in is the failure mode here.
    """
    snapshot = _snapshot_with({"Status": LEGIT_STATUS_AFTER})
    snapshot.before = {"Status": LEGIT_STATUS_BEFORE, "Description": f"old {PLANTED_KEY_SHAPED}"}
    event = _event_with({"records": [{"Id": LEGIT_RECORD_ID, "Note__c": f"tok {PLANTED_KEY_SHAPED}"}]})

    registry = TelemetryRegistry()
    registry.collect_step(_StubCollector(snapshot, event), "run-test", "step-001")

    assert PLANTED_KEY_SHAPED not in json.dumps([s.before for s in registry.snapshots]), (
        "a secret survived ingest in the before-image"
    )
    assert PLANTED_KEY_SHAPED not in json.dumps([e.payload for e in registry.events]), (
        "a secret survived ingest in an event payload"
    )


def test_manual_event_payload_is_scrubbed_on_append() -> None:
    """`append_manual_event` is a second ingest door and must scrub like the first.

    It has no in-tree caller today, which is precisely why it is worth covering:
    the first caller to appear would otherwise reintroduce the leak silently.
    """
    registry = TelemetryRegistry()
    registry.append_manual_event(
        run_id="run-test",
        step_id="step-001",
        layer=TelemetryLayer.FLOW,
        event_name="ManualNote",
        status="ok",
        payload={"note": f"key {PLANTED_KEY_SHAPED} seen"},
    )

    assert PLANTED_KEY_SHAPED not in json.dumps([e.payload for e in registry.events]), (
        "a key-shaped secret survived append_manual_event"
    )


def test_the_cli_scrub_is_still_called_and_still_reports() -> None:
    """Ingest scrubbing must not make the CLI's audit line go silent.

    `scrub_collected_telemetry` stays wired in `cli.py` as defence in depth, and it
    is what echoes `REDACTION: scrubbed telemetry values from the org`. If ingest
    left nothing for it to find it would return no categories and the run would
    stop saying the control fired — a silent control cannot be audited. So the
    registry records what it scrubbed and the CLI reports from that.
    """
    registry = TelemetryRegistry()
    registry.collect_step(
        _StubCollector(
            _snapshot_with({"Description": f"key {PLANTED_KEY_SHAPED} for {PLANTED_EMAIL}"}),
            _event_with({}),
        ),
        "run-test",
        "step-001",
    )

    assert "aws_key" in registry.redaction_categories
    assert "email" in registry.redaction_categories
    # Categories name the KIND of value, never the value itself.
    assert PLANTED_KEY_SHAPED not in " ".join(registry.redaction_categories)

    # And the second pass over already-clean data is a no-op, not a corruption.
    before_second_pass = json.dumps([s.after for s in registry.snapshots])
    scrub_collected_telemetry(registry.events, registry.snapshots)
    assert json.dumps([s.after for s in registry.snapshots]) == before_second_pass, (
        "the scrub is not idempotent; running it twice changes the data"
    )


def test_the_cli_still_announces_the_redaction_it_performed(tmp_path, monkeypatch) -> None:
    """Drives the real CLI, because this is the regression moving the scrub caused.

    Moving the scrub to ingest made `scrub_collected_telemetry` find nothing, so the
    CLI's `REDACTION:` line disappeared while redaction was still happening — the
    control went quiet, which reads as "no secrets found". Caught only by asserting
    on the CLI's own output, so that is what this does.
    """
    from typer.testing import CliRunner

    from sf_video_blueprint import cli as cli_module

    event_json = {
        "v": 1, "seq": 1, "t": 1737830000000, "type": "click",
        "url": "https://example.my.salesforce.com/lightning/r/Case/view",
        "frame_path": [],
        "selectors": {"test_id": None, "aria": None, "role_name": None, "label_for": None,
                      "sf_field": None, "css_path": "button.save", "text": "Save", "xpath": None},
        "element": {"tag": "button", "type": None, "name": None, "id": None, "classes": [],
                    "aria_label": "Save", "text": "Save", "is_in_modal": False,
                    "modal_label": None, "shadow_depth": 0},
        "value": None, "value_redacted": False,
        "sf": {"object": "Case", "record_id": LEGIT_RECORD_ID,
               "page_type": "record", "app": "Service"},
    }
    capture = tmp_path / "c.jsonl"
    capture.write_text(json.dumps(event_json) + "\n", encoding="utf-8")

    # Stand in for the mock collector with one that returns a planted secret, so the
    # scrub has something real to find on the default (non-live) path.
    class _LeakyCollector(TelemetryCollector):
        def collect_for_step(self, run_id: str, step_id: str) -> list[TelemetryEvent]:
            return [
                TelemetryEvent(
                    correlation=CorrelationKey(
                        run_id=run_id, step_id=step_id, event_time=datetime.now(UTC)
                    ),
                    layer=TelemetryLayer.FLOW,
                    event_name="FlowInterviewFetch",
                    status="ok",
                    payload={"note": f"key {PLANTED_KEY_SHAPED}"},
                )
            ]

        def snapshot_changes(self, run_id: str, step_id: str) -> list[ObjectSnapshot]:
            return []

    monkeypatch.setattr(cli_module, "MockTelemetryCollector", _LeakyCollector)

    out = tmp_path / "report.html"
    result = CliRunner().invoke(
        cli_module.app,
        ["run", "--capture", str(capture), "--org-url", "https://example.my.salesforce.com",
         "--output-path", str(out)],
    )

    assert result.exit_code == 0, result.stdout
    assert "REDACTION: scrubbed telemetry values from the org" in result.stdout, (
        "the CLI performed redaction but stopped reporting it"
    )
    assert "aws_key" in result.stdout
    # The announcement names the category, never the value.
    assert PLANTED_KEY_SHAPED not in result.stdout, "the CLI echoed the secret it redacted"


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

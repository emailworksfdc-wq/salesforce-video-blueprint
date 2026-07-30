"""Tests for the iterate sub-command in cli.py.

Coverage:
  - happy path: rounds are written, per-round output is correct
  - forbidden org is refused before any org call
  - missing spec aborts with non-zero exit
  - _load_spec_from_json round-trip integrity
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from sf_video_blueprint.cli import _load_spec_from_json, app
from sf_video_blueprint.spec_builder import (
    DerivedAgentSpec,
    DerivedEntity,
    SpecEvidence,
    write_spec,
)
from sf_video_blueprint.spec_score import PASS_THRESHOLD

FIXTURE = Path(__file__).parent / "fixtures" / "run_eval_aft3_coral_cloud_booking.json"


def _real_payload() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def _make_spec(
    *,
    confidence: float = 0.7,
    unknowns: list[str] | None = None,
    failure_handling: list[str] | None = None,
) -> DerivedAgentSpec:
    return DerivedAgentSpec(
        intent="Update Case (Status)",
        confidence=confidence,
        objects_touched=["Case"],
        entities=[
            DerivedEntity(
                name="status",
                object_api_name="Case",
                field_api_name="Status",
                evidence=[SpecEvidence("data-delta", "Case.Status observed")],
            )
        ],
        orchestration_steps=["Resolve the Case", "SUBMIT on button:Save -> writes Status"],
        guardrails=["Require explicit user confirmation before writing: Status."],
        failure_handling=failure_handling or ["Observed validation failure path"],
        unknowns=unknowns or [],
        evidence=[SpecEvidence("telemetry", "backend layers observed: lwc")],
    )


def _make_spec_json(tmp_path: Path, **spec_kwargs: Any) -> Path:
    spec = _make_spec(**spec_kwargs)
    spec_path = tmp_path / "agent-spec.json"
    write_spec(spec_path, spec, {
        "extraction_source": "dom-capture",
        "telemetry_source": "mock",
        "replay_source": "noop",
        "run_id": "run-test001",
        "recording_id": "rec-test001",
        "source_path": "/dev/null",
    })
    return spec_path


def _make_runner(payload: dict | None = None):
    if payload is None:
        payload = _real_payload()
    class _Done:
        returncode = 0
        stdout = json.dumps(payload)
        stderr = ""
    def runner(cmd: list[str], timeout: int) -> _Done:
        return _Done()
    return runner


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


def test_load_spec_from_json_round_trips_intent(tmp_path: Path) -> None:
    spec = _make_spec()
    spec_path = tmp_path / "spec.json"
    write_spec(spec_path, spec, {"extraction_source": "dom-capture", "telemetry_source": "mock",
                                  "replay_source": "noop", "run_id": "r1",
                                  "recording_id": "rec1", "source_path": "/dev/null"})
    loaded = _load_spec_from_json(spec_path)
    assert loaded.intent == spec.intent


def test_load_spec_from_json_round_trips_entities(tmp_path: Path) -> None:
    spec = _make_spec()
    spec_path = tmp_path / "spec.json"
    write_spec(spec_path, spec, {"extraction_source": "dom-capture", "telemetry_source": "mock",
                                  "replay_source": "noop", "run_id": "r1",
                                  "recording_id": "rec1", "source_path": "/dev/null"})
    loaded = _load_spec_from_json(spec_path)
    assert len(loaded.entities) == len(spec.entities)
    assert loaded.entities[0].name == spec.entities[0].name
    assert loaded.entities[0].object_api_name == spec.entities[0].object_api_name


def test_load_spec_from_json_round_trips_confidence(tmp_path: Path) -> None:
    spec = _make_spec(confidence=0.65)
    spec_path = tmp_path / "spec.json"
    write_spec(spec_path, spec, {"extraction_source": "dom-capture", "telemetry_source": "mock",
                                  "replay_source": "noop", "run_id": "r1",
                                  "recording_id": "rec1", "source_path": "/dev/null"})
    loaded = _load_spec_from_json(spec_path)
    assert abs(loaded.confidence - 0.65) < 0.001


def test_load_spec_from_json_raises_on_missing_intent(tmp_path: Path) -> None:
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"confidence": 0.5}), encoding="utf-8")
    with pytest.raises(KeyError):
        _load_spec_from_json(bad)


def test_load_spec_from_json_raises_on_bad_json(tmp_path: Path) -> None:
    bad = tmp_path / "bad.json"
    bad.write_text("not json at all", encoding="utf-8")
    with pytest.raises(json.JSONDecodeError):
        _load_spec_from_json(bad)


def test_load_spec_from_json_round_trips_unknowns(tmp_path: Path) -> None:
    spec = _make_spec(unknowns=["No telemetry observed"])
    spec_path = tmp_path / "spec.json"
    write_spec(spec_path, spec, {"extraction_source": "dom-capture", "telemetry_source": "mock",
                                  "replay_source": "noop", "run_id": "r1",
                                  "recording_id": "rec1", "source_path": "/dev/null"})
    loaded = _load_spec_from_json(spec_path)
    assert loaded.unknowns == spec.unknowns


def test_iterate_missing_spec_aborts(runner: CliRunner, tmp_path: Path) -> None:
    result = runner.invoke(app, [
        "iterate", "--spec", str(tmp_path / "does_not_exist.json"),
        "--org-alias", "AFT3", "--agent-api-name", "My_Agent",
        "--test-spec-name", "T", "--out-dir", str(tmp_path / "out"),
    ])
    assert result.exit_code != 0
    assert "not found" in result.stdout.lower() or "ERROR" in result.stdout


def test_iterate_forbidden_org_ppcdm_is_refused(runner: CliRunner, tmp_path: Path) -> None:
    spec_path = _make_spec_json(tmp_path)
    result = runner.invoke(app, [
        "iterate", "--spec", str(spec_path), "--org-alias", "PPCDM",
        "--agent-api-name", "My_Agent", "--test-spec-name", "T",
        "--out-dir", str(tmp_path / "out"),
    ])
    assert result.exit_code != 0, "PPCDM must be refused"
    assert "out of scope" in result.stdout.lower() or "ERROR" in result.stdout


def test_iterate_forbidden_org_ppcaccenture_is_refused(runner: CliRunner, tmp_path: Path) -> None:
    spec_path = _make_spec_json(tmp_path)
    result = runner.invoke(app, [
        "iterate", "--spec", str(spec_path), "--org-alias", "PPCaccenture",
        "--agent-api-name", "My_Agent", "--test-spec-name", "T",
        "--out-dir", str(tmp_path / "out"),
    ])
    assert result.exit_code != 0, "PPCaccenture must be refused"
    assert "out of scope" in result.stdout.lower() or "ERROR" in result.stdout


def test_iterate_happy_path_writes_round_one(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spec_path = _make_spec_json(tmp_path)
    out_dir = tmp_path / "out"
    import sf_video_blueprint.iterate as iterate_module
    orig_refine = iterate_module.refine_with_org_feedback

    def stub(spec, *, out_dir, org_alias, agent_api_name, test_spec_name, rounds, **kw):
        return orig_refine(spec, out_dir=out_dir, org_alias=org_alias,
                           agent_api_name=agent_api_name, test_spec_name=test_spec_name,
                           rounds=rounds, runner=_make_runner())

    monkeypatch.setattr(iterate_module, "refine_with_org_feedback", stub)
    result = runner.invoke(app, [
        "iterate", "--spec", str(spec_path), "--org-alias", "AFT3",
        "--agent-api-name", "Coral_Cloud_Booking_Agent",
        "--test-spec-name", "SFVB_Case", "--out-dir", str(out_dir),
    ])
    assert (out_dir / "round-1" / "round.json").exists(), f"round.json missing; {result.stdout}"
    assert "Round 1:" in result.stdout
    assert "score=" in result.stdout
    assert "passed=" in result.stdout


def test_iterate_happy_path_two_rounds(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spec_path = _make_spec_json(tmp_path)
    out_dir = tmp_path / "out"
    import sf_video_blueprint.iterate as iterate_module
    orig_refine = iterate_module.refine_with_org_feedback

    def stub(spec, *, out_dir, org_alias, agent_api_name, test_spec_name, rounds, **kw):
        return orig_refine(spec, out_dir=out_dir, org_alias=org_alias,
                           agent_api_name=agent_api_name, test_spec_name=test_spec_name,
                           rounds=rounds, runner=_make_runner())

    monkeypatch.setattr(iterate_module, "refine_with_org_feedback", stub)
    result = runner.invoke(app, [
        "iterate", "--spec", str(spec_path), "--org-alias", "AFT3",
        "--agent-api-name", "Coral_Cloud_Booking_Agent",
        "--test-spec-name", "SFVB_Case", "--rounds", "2",
        "--out-dir", str(out_dir),
    ])
    assert (out_dir / "round-1" / "round.json").exists(), f"round-1 missing; {result.stdout}"
    assert (out_dir / "round-2" / "round.json").exists(), f"round-2 missing; {result.stdout}"
    assert "Round 1:" in result.stdout
    assert "Round 2:" in result.stdout


def test_iterate_exits_nonzero_below_threshold(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spec_path = _make_spec_json(tmp_path)
    out_dir = tmp_path / "out"
    import sf_video_blueprint.iterate as iterate_module
    orig_refine = iterate_module.refine_with_org_feedback
    captured: list[Any] = []

    def stub(spec, *, out_dir, org_alias, agent_api_name, test_spec_name, rounds, **kw):
        results = orig_refine(spec, out_dir=out_dir, org_alias=org_alias,
                              agent_api_name=agent_api_name, test_spec_name=test_spec_name,
                              rounds=rounds, runner=_make_runner())
        captured.extend(results)
        return results

    monkeypatch.setattr(iterate_module, "refine_with_org_feedback", stub)
    result = runner.invoke(app, [
        "iterate", "--spec", str(spec_path), "--org-alias", "AFT3",
        "--agent-api-name", "Coral_Cloud_Booking_Agent",
        "--test-spec-name", "SFVB_Case", "--out-dir", str(out_dir),
    ])
    assert len(captured) == 1
    final_score = captured[0].score_after
    if final_score is not None and final_score.total < PASS_THRESHOLD:
        assert result.exit_code != 0
        assert "FAIL" in result.stdout


def test_iterate_help_text_is_accessible(runner: CliRunner) -> None:
    result = runner.invoke(app, ["iterate", "--help"])
    assert result.exit_code == 0
    assert "spec" in result.stdout.lower()


def test_iterate_spec_directory_aborts(runner: CliRunner, tmp_path: Path) -> None:
    result = runner.invoke(app, [
        "iterate", "--spec", str(tmp_path), "--org-alias", "AFT3",
        "--agent-api-name", "My_Agent", "--test-spec-name", "T",
        "--out-dir", str(tmp_path / "out"),
    ])
    assert result.exit_code != 0


def test_iterate_malformed_spec_json_aborts(runner: CliRunner, tmp_path: Path) -> None:
    bad_spec = tmp_path / "bad.json"
    bad_spec.write_text("this is not json", encoding="utf-8")
    result = runner.invoke(app, [
        "iterate", "--spec", str(bad_spec), "--org-alias", "AFT3",
        "--agent-api-name", "My_Agent", "--test-spec-name", "T",
        "--out-dir", str(tmp_path / "out"),
    ])
    assert result.exit_code != 0


def test_iterate_existing_run_command_unaffected(runner: CliRunner) -> None:
    result = runner.invoke(app, ["run", "--help"])
    assert result.exit_code == 0
    # Strip ANSI escape codes for plain-text search (rich may emit colour codes)
    import re
    plain = re.sub(r"\[[0-9;]*m", "", result.stdout)
    assert "--capture" in plain or "capture" in plain.lower()


def test_iterate_stop_reason_shown_when_not_trustworthy(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spec_path = _make_spec_json(tmp_path)
    out_dir = tmp_path / "out"
    import sf_video_blueprint.iterate as iterate_module
    orig_refine = iterate_module.refine_with_org_feedback

    def stub(spec, *, out_dir, org_alias, agent_api_name, test_spec_name, rounds, **kw):
        return orig_refine(spec, out_dir=out_dir, org_alias=org_alias,
                           agent_api_name=agent_api_name, test_spec_name=test_spec_name,
                           rounds=rounds, runner=_make_runner())

    monkeypatch.setattr(iterate_module, "refine_with_org_feedback", stub)
    result = runner.invoke(app, [
        "iterate", "--spec", str(spec_path), "--org-alias", "AFT3",
        "--agent-api-name", "Coral_Cloud_Booking_Agent",
        "--test-spec-name", "SFVB_Case", "--out-dir", str(out_dir),
    ])
    assert ("not trustworthy" in result.stdout.lower() or "synthetic" in result.stdout.lower()), f"Expected trustworthy notice; {result.stdout}"

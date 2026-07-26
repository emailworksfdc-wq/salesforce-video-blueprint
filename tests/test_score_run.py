from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "score_run.py"

ALL_GATES_PASSING = {
    "preflight_ok": True,
    "execution_ok": True,
    "telemetry_ok": True,
    "artifacts_ok": True,
    "negative_tests_ok": True,
    "critical_issue": False,
}

GOOD_SPEC = {
    "intent": "Update Opportunity (StageName)",
    "confidence": 0.75,
    "objects_touched": ["Opportunity"],
    "entities": [
        {
            "name": "stageName",
            "object_api_name": "Opportunity",
            "field_api_name": "StageName",
            "evidence": [
                {"source": "data-delta", "detail": "Opportunity.StageName changed 'Prospecting' -> 'Closed Won' at step 3"}
            ],
        }
    ],
    "orchestration_steps": [
        "Resolve and load the target Opportunity record; confirm the caller may act on it.",
        "SUBMIT on button:Save -> writes StageName (backend: validation, workflow)",
        "Return a confirmation that names the record and the fields changed.",
    ],
    "guardrails": [
        "Enforce object- and field-level security on Opportunity for the running user.",
        "Require explicit user confirmation before writing: StageName.",
    ],
    "failure_handling": ["Observed validation failure during recording: StageName must be one of approved values"],
    "unknowns": [],
    # The top-level evidence trail, as build_agent_spec actually emits it. This
    # fixture used to carry `"evidence": []`, which no real run can produce — the
    # builder unconditionally appends the "N action(s) in recording" entry — so the
    # fixture was claiming to represent a clean derived run while omitting the one
    # field that records the run. The gate now reads this trail, so the fixture has
    # to be faithful to the builder.
    "evidence": [
        {"source": "telemetry", "detail": "backend layers observed: validation, workflow"},
        {"source": "extraction", "detail": "5 action(s) in recording"},
        {"source": "data-delta", "detail": "objects mutated: Opportunity"},
    ],
    # Both provenance axes must claim real sources. The gate checks
    # extraction_source structurally (see markers.py) rather than sniffing step
    # text for stub-looking strings, so a spec that does not say where its steps
    # came from cannot pass — omitting the key fails closed.
    "provenance": {"telemetry_source": "live-org", "extraction_source": "dom-capture"},
}


def _run(tmp_path: Path, summary: dict, spec: dict | None, html: str = "clean report") -> dict:
    out_dir = tmp_path
    (out_dir / "mock_blueprint.html").write_text(html, encoding="utf-8")
    (out_dir / "live_blueprint.html").write_text(html, encoding="utf-8")
    if spec is not None:
        (out_dir / "live_blueprint.agent-spec.json").write_text(json.dumps(spec), encoding="utf-8")
    summary_path = out_dir / "run_summary.json"
    summary_path.write_text(json.dumps(summary), encoding="utf-8")

    proc = subprocess.run(
        [sys.executable, str(SCRIPT), str(summary_path), str(out_dir)],
        capture_output=True,
        text=True,
        check=False,
    )
    payload = json.loads(proc.stdout)
    payload["_returncode"] = proc.returncode
    return payload


def test_clean_derived_run_passes(tmp_path: Path) -> None:
    result = _run(tmp_path, ALL_GATES_PASSING, GOOD_SPEC)
    assert result["pass"] is True
    assert result["_returncode"] == 0
    assert result["blocking_issues"] == []


def test_placeholder_content_blocks_pass(tmp_path: Path) -> None:
    """The core regression: a run full of stub content must not score as passing."""
    result = _run(
        tmp_path,
        ALL_GATES_PASSING,
        GOOD_SPEC,
        html="<p>Update case status from UI workflow</p><p>Sample_Flow</p>",
    )
    assert result["pass"] is False
    assert result["_returncode"] != 0
    assert any("placeholder" in issue for issue in result["blocking_issues"])


def test_stub_extraction_spec_blocks_pass(tmp_path: Path) -> None:
    """Steps from the stub extractor must not pass, however clean they look.

    This replaces the old ``button:Save`` placeholder marker. That string was a
    proxy for the stub, but a real DOM capture of an operator clicking Save
    produces it too, so the marker would have failed every genuine run once Step 5
    started working. The check is now structural: what produced the steps, not
    whether a step's text resembles the stub's output.
    """
    spec = dict(
        GOOD_SPEC,
        provenance={"telemetry_source": "live-org", "extraction_source": "stub"},
    )
    result = _run(tmp_path, ALL_GATES_PASSING, spec)
    assert result["pass"] is False
    # spec_score.py now provides the blocking message
    assert any("stub" in issue.lower() or "extraction" in issue.lower() for issue in result["blocking_issues"])


def test_missing_extraction_source_blocks_pass(tmp_path: Path) -> None:
    """An unrecognised or absent extraction_source fails closed, not open."""
    spec = dict(GOOD_SPEC, provenance={"telemetry_source": "live-org"})
    result = _run(tmp_path, ALL_GATES_PASSING, spec)
    assert result["pass"] is False


def test_real_save_click_is_not_placeholder(tmp_path: Path) -> None:
    """``button:Save`` in a real capture is evidence, not a placeholder marker."""
    spec = dict(
        GOOD_SPEC,
        orchestration_steps=["SUBMIT on button:Save -> writes Opportunity.StageName"],
    )
    result = _run(tmp_path, ALL_GATES_PASSING, spec, html="<p>SUBMIT on button:Save</p>")
    assert result["pass"] is True, result["blocking_issues"]


def test_mock_telemetry_spec_blocks_pass(tmp_path: Path) -> None:
    spec = dict(GOOD_SPEC, provenance={"telemetry_source": "mock"})
    result = _run(tmp_path, ALL_GATES_PASSING, spec)
    assert result["pass"] is False
    # spec_score.py now provides the blocking message
    assert any("mock" in issue.lower() or "telemetry" in issue.lower() for issue in result["blocking_issues"])


def test_missing_spec_blocks_pass(tmp_path: Path) -> None:
    result = _run(tmp_path, ALL_GATES_PASSING, None)
    assert result["pass"] is False
    assert result["gates"]["spec_derived_ok"] is False


def test_unresolved_intent_blocks_pass(tmp_path: Path) -> None:
    spec = dict(GOOD_SPEC, intent="UNRESOLVED: nothing observed", confidence=0.05)
    result = _run(tmp_path, ALL_GATES_PASSING, spec)
    assert result["pass"] is False


def test_spec_with_no_objects_blocks_pass(tmp_path: Path) -> None:
    spec = dict(GOOD_SPEC, objects_touched=[])
    result = _run(tmp_path, ALL_GATES_PASSING, spec)
    assert result["pass"] is False
    # spec_score.py now provides the blocking message
    assert any("object" in issue.lower() for issue in result["blocking_issues"])


def test_critical_issue_actually_blocks(tmp_path: Path) -> None:
    """Previously unfailable: critical_issue was a hardcoded False literal."""
    summary = dict(ALL_GATES_PASSING, critical_issue=True)
    result = _run(tmp_path, summary, GOOD_SPEC)
    assert result["pass"] is False
    assert any("critical_issue" in issue for issue in result["blocking_issues"])


def test_failed_preflight_lowers_score(tmp_path: Path) -> None:
    summary = dict(ALL_GATES_PASSING, preflight_ok=False)
    result = _run(tmp_path, summary, GOOD_SPEC)
    assert result["gates"]["preflight_ok"] is False
    assert result["score"] < result["max_score"]


# === CRITICAL DEFECT FIX: CI gate must be no weaker than in-process scorer ===


def test_f1_bad_spec_fails_ci_gate():
    """F1: The bad spec that scores 66/100 in spec_score.py must FAIL CI.

    MEASURED DEFECT: This spec currently scores 100/100 and PASSES CI while
    spec_score.py gives it 66/100 and FAILS. The CI gate is weaker than the
    in-process gate, so CI is the effective gate and the weaker one wins.

    The bad spec has:
    - Minimal evidence (SpecEvidence("data-delta", "x"))
    - Duplicate orchestration steps ("Resolve the Case" x2)
    - Duplicate generic guardrails ("Validate input" x2)
    - Explicitly untested failure handling
    - No unknowns declared

    spec_score.py correctly fails this. CI must fail it too.
    """
    import tempfile

    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_path = Path(tmpdir)

        bad_spec = {
            "intent": "Update Case (Status)",
            "confidence": 0.7,
            "objects_touched": ["Case"],
            "entities": [
                {
                    "name": "status",
                    "object_api_name": "Case",
                    "field_api_name": "Status",
                    "evidence": [{"source": "data-delta", "detail": "x"}],
                }
            ],
            "orchestration_steps": ["Resolve the Case", "Resolve the Case"],
            "guardrails": ["Validate input", "Validate input"],
            "failure_handling": [
                "No failures were observed in this run, so error paths are UNTESTED. "
                "Record a failing variant before relying on this spec."
            ],
            "unknowns": [],
            "evidence": [],
            "provenance": {"telemetry_source": "live-org", "extraction_source": "dom-capture"},
        }

        result = _run(tmp_path, ALL_GATES_PASSING, bad_spec)

        assert result["pass"] is False, (
            f"F1 REGRESSION: The bad spec PASSED CI with score {result['score']}/100. "
            "It must FAIL because spec_score.py fails it."
        )
        assert result["_returncode"] != 0, "CI must exit non-zero when the bad spec fails"
        assert result["blocking_issues"], "Bad spec must have blocking issues"


def test_ci_gate_never_weaker_than_spec_score():
    """CI gate result must never be MORE permissive than spec_score.

    Invariant: not (score_run_passes and not spec_score_passes)

    Build several specs across the pass/fail boundary and assert the invariant.
    """
    import tempfile
    from sf_video_blueprint.spec_score import score_spec_file

    test_cases = [
        # (name, spec_dict, expected_to_pass_spec_score)
        (
            "good_spec",
            {
                "intent": "Update Opportunity (StageName)",
                "confidence": 0.75,
                "objects_touched": ["Opportunity"],
                "entities": [
                    {
                        "name": "stageName",
                        "object_api_name": "Opportunity",
                        "field_api_name": "StageName",
                        "evidence": [
                            {"source": "data-delta", "detail": "Opportunity.StageName changed 'Prospecting' -> 'Closed Won' at step 3"}
                        ],
                    }
                ],
                "orchestration_steps": [
                    "Resolve and load the target Opportunity record",
                    "SUBMIT on button:Save -> writes StageName",
                    "Return confirmation",
                ],
                "guardrails": [
                    "Enforce object- and field-level security on Opportunity",
                    "Require explicit user confirmation before writing: StageName",
                ],
                "failure_handling": ["Observed validation failure during recording: StageName must be one of approved values"],
                "unknowns": [],
                "evidence": [],
                "provenance": {"telemetry_source": "live-org", "extraction_source": "dom-capture"},
            },
            True,
        ),
        (
            "bad_spec_minimal_evidence",
            {
                "intent": "Update Case (Status)",
                "confidence": 0.7,
                "objects_touched": ["Case"],
                "entities": [
                    {
                        "name": "status",
                        "object_api_name": "Case",
                        "field_api_name": "Status",
                        "evidence": [{"source": "data-delta", "detail": "x"}],
                    }
                ],
                "orchestration_steps": ["Resolve the Case", "Resolve the Case"],
                "guardrails": ["Validate input", "Validate input"],
                "failure_handling": ["No failures were observed in this run, so error paths are UNTESTED."],
                "unknowns": [],
                "evidence": [],
                "provenance": {"telemetry_source": "live-org", "extraction_source": "dom-capture"},
            },
            False,
        ),
        (
            "placeholder_spec",
            {
                "intent": "Update Case (Status)",
                "confidence": 0.7,
                "objects_touched": ["Case"],
                "entities": [
                    {
                        "name": "status",
                        "object_api_name": "Case",
                        "field_api_name": "Status",
                        "evidence": [{"source": "data-delta", "detail": "TODO: add real evidence"}],
                    }
                ],
                "orchestration_steps": ["Step 1", "Step 2"],
                "guardrails": ["Guardrail 1"],
                "failure_handling": [],
                "unknowns": [],
                "evidence": [],
                "provenance": {"telemetry_source": "live-org", "extraction_source": "dom-capture"},
            },
            False,
        ),
    ]

    with tempfile.TemporaryDirectory() as tmpdir:
        for name, spec_dict, expected_pass in test_cases:
            tmp_path = Path(tmpdir) / name
            tmp_path.mkdir()

            # Run CI gate
            ci_result = _run(tmp_path, ALL_GATES_PASSING, spec_dict)

            # Run spec_score independently
            spec_path = tmp_path / "live_blueprint.agent-spec.json"
            spec_score_result = score_spec_file(spec_path)

            # The invariant: CI must never pass when spec_score fails
            if ci_result["pass"] and not spec_score_result.passed:
                raise AssertionError(
                    f"CI gate WEAKER than spec_score for {name}: "
                    f"CI passed (score {ci_result['score']}) but spec_score failed "
                    f"(score {spec_score_result.total}/{spec_score_result.max_total}, "
                    f"blocking: {spec_score_result.blocking_issues})"
                )


def test_honesty_invariant_unknowns_not_penalized():
    """Honesty invariant: declaring unknowns must not penalize vs hiding them.

    For two otherwise-identical specs, the one that declares its unknowns must
    never score lower or be blocked when the other is not.

    This tests the HONESTY INVERSION fix: lines 95-98 of the OLD score_run.py
    added blocking issues for low confidence and declared unknowns, which
    inverted the honesty principle.
    """
    import tempfile

    with tempfile.TemporaryDirectory() as tmpdir:
        # Spec A: declares unknowns and has reasonable confidence
        spec_with_unknowns = {
            "intent": "Update Case (Status)",
            "confidence": 0.6,
            "objects_touched": ["Case"],
            "entities": [
                {
                    "name": "status",
                    "object_api_name": "Case",
                    "field_api_name": "Status",
                    "evidence": [
                        {"source": "data-delta", "detail": "Case.Status changed 'New' -> 'Working' at step 2"}
                    ],
                }
            ],
            "orchestration_steps": [
                "Resolve and load the target Case record",
                "SUBMIT on button:Save -> writes Status",
            ],
            "guardrails": ["Enforce FLS on Case"],
            "failure_handling": ["Observed validation failure during recording: Status must be valid"],
            "unknowns": ["Whether workflow rules fire after this write is not observed"],
            # Faithful to build_agent_spec, which always records the run itself.
            "evidence": [
                {"source": "extraction", "detail": "4 action(s) in recording"},
                {"source": "data-delta", "detail": "objects mutated: Case"},
            ],
            "provenance": {"telemetry_source": "live-org", "extraction_source": "dom-capture"},
        }

        # Spec B: identical but hides unknowns (claims to know everything)
        spec_without_unknowns = dict(spec_with_unknowns, unknowns=[])

        tmp_a = Path(tmpdir) / "with_unknowns"
        tmp_a.mkdir()
        result_a = _run(tmp_a, ALL_GATES_PASSING, spec_with_unknowns)

        tmp_b = Path(tmpdir) / "without_unknowns"
        tmp_b.mkdir()
        result_b = _run(tmp_b, ALL_GATES_PASSING, spec_without_unknowns)

        # The honest spec (with unknowns) must NOT score lower than the dishonest one
        assert result_a["pass"] or not result_b["pass"], (
            f"HONESTY INVERSION: Spec with unknowns declared FAILED (pass={result_a['pass']}, "
            f"score={result_a['score']}) while identical spec hiding unknowns PASSED "
            f"(pass={result_b['pass']}, score={result_b['score']}). "
            "Declaring unknowns must not be penalized."
        )

        # Both should actually pass (this is a decent spec with honest caveats)
        assert result_a["pass"], (
            f"Spec with declared unknowns should PASS but failed: {result_a['blocking_issues']}"
        )


def test_list_valued_provenance_does_not_crash():
    """CRASH FIX: list-valued provenance source must not raise TypeError.

    extraction_is_real/telemetry_is_real do `source in FROZENSET`, which raises
    TypeError: unhashable type: 'list' if source is a list.

    The fix: handle non-string sources defensively (fail closed: non-string = not real).
    """
    import tempfile

    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_path = Path(tmpdir)

        spec_with_list_provenance = {
            "intent": "Update Case (Status)",
            "confidence": 0.7,
            "objects_touched": ["Case"],
            "entities": [
                {
                    "name": "status",
                    "object_api_name": "Case",
                    "field_api_name": "Status",
                    "evidence": [{"source": "data-delta", "detail": "Case.Status changed"}],
                }
            ],
            "orchestration_steps": ["Resolve the Case", "Submit"],
            "guardrails": ["Enforce FLS on Case"],
            "failure_handling": [],
            "unknowns": [],
            "evidence": [],
            # TRIGGER: extraction_source is a list, not a string
            "provenance": {"telemetry_source": "live-org", "extraction_source": ["dom-capture"]},
        }

        # This should NOT crash
        result = _run(tmp_path, ALL_GATES_PASSING, spec_with_list_provenance)

        # It should fail closed (non-string source = not real)
        assert result["pass"] is False, (
            "List-valued provenance source should fail closed (treated as not real)"
        )
        assert any("extraction" in issue.lower() or "provenance" in issue.lower()
                   for issue in result["blocking_issues"]), (
            f"Expected blocking issue about extraction source, got: {result['blocking_issues']}"
        )


def test_self_reported_gates_are_labeled():
    """Self-reported gates must be labeled so readers know they are not verified."""
    import tempfile

    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_path = Path(tmpdir)
        result = _run(tmp_path, ALL_GATES_PASSING, GOOD_SPEC)

        # Result must include the self_reported_gates list
        assert "self_reported_gates" in result, "Result must list which gates are self-reported"
        assert "preflight_ok" in result["self_reported_gates"]
        assert "execution_ok" in result["self_reported_gates"]
        assert "telemetry_ok" in result["self_reported_gates"]
        assert "artifacts_ok" in result["self_reported_gates"]
        assert "negative_tests_ok" in result["self_reported_gates"]

        # Independently verified gates must NOT be in the self-reported list
        assert "evidence_is_real_ok" not in result["self_reported_gates"]
        assert "spec_derived_ok" not in result["self_reported_gates"]

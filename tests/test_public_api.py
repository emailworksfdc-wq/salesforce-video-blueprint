"""Tests for the package's public API surface.

`__init__.py` used to be an empty docstring, so `dir(sf_video_blueprint)` was `[]`
and `from sf_video_blueprint import run_pipeline` raised ImportError — every
consumer had to reach into submodules and was coupled to internal layout. These
tests pin the surface so it cannot silently regress.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import sf_video_blueprint

EXAMPLE = Path(__file__).parent.parent / "examples" / "case_triage.dom_capture.jsonl"

EXPECTED_EXPORTS = {
    "CaptureRejected",
    "DerivedAgentSpec",
    "DerivedEntity",
    "PipelineResult",
    "SpecScore",
    "build_agent_spec",
    "run_pipeline",
    "score_spec",
    "score_spec_file",
    "__version__",
}


def test_all_is_explicit() -> None:
    assert set(sf_video_blueprint.__all__) == EXPECTED_EXPORTS


def test_dir_is_discoverable() -> None:
    """`dir()` returning [] made the package look empty to anyone exploring it."""
    assert set(sf_video_blueprint.__dir__()) == EXPECTED_EXPORTS


@pytest.mark.parametrize("name", sorted(EXPECTED_EXPORTS))
def test_every_advertised_export_actually_resolves(name: str) -> None:
    """An `__all__` entry that does not resolve is worse than no export at all."""
    assert getattr(sf_video_blueprint, name) is not None


def test_top_level_import_of_run_pipeline() -> None:
    from sf_video_blueprint import run_pipeline

    assert callable(run_pipeline)


def test_unknown_attribute_raises_attribute_error() -> None:
    with pytest.raises(AttributeError, match="no attribute"):
        sf_video_blueprint.does_not_exist  # noqa: B018


def test_version_is_reported() -> None:
    assert sf_video_blueprint.__version__
    assert sf_video_blueprint.__version__ != "0.0.0+unknown"


def test_lazy_attribute_is_cached() -> None:
    """Repeated access must not re-enter the import machinery each time."""
    first = sf_video_blueprint.run_pipeline
    assert sf_video_blueprint.run_pipeline is first
    assert "run_pipeline" in vars(sf_video_blueprint)


def test_end_to_end_through_the_public_surface_only() -> None:
    """The documented three-line usage must work with no submodule imports."""
    from sf_video_blueprint import PipelineResult, run_pipeline

    result = run_pipeline(EXAMPLE, org_url="https://example.my.salesforce.com")

    assert isinstance(result, PipelineResult)
    assert result.spec.intent
    # Reaffirmed at the public boundary: a library consumer must not be able to
    # obtain a passing verdict from a mock run.
    assert result.score.passed is False
    assert result.evidence_is_real is False


def test_capture_rejected_is_catchable_from_the_top_level(tmp_path: Path) -> None:
    """Consumers need the exception type without knowing which module defines it."""
    from sf_video_blueprint import CaptureRejected, run_pipeline

    bad = tmp_path / "bad.jsonl"
    bad.write_text("nope\n{unclosed\nnah\n", encoding="utf-8")

    with pytest.raises(CaptureRejected):
        run_pipeline(bad, org_url="https://example.my.salesforce.com")

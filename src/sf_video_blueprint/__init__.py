"""Turn a recorded Salesforce process into a scored conversational agent spec.

The supported entry point is :func:`run_pipeline`:

    from sf_video_blueprint import run_pipeline

    result = run_pipeline("dom_capture.jsonl", org_url="https://x.sandbox.my.salesforce.com")
    print(result.spec.intent, result.score.total, result.score.passed)

Read `result.score.passed` before trusting anything downstream. A run assembled
in-process uses mock telemetry, so it is stamped `telemetry_source: "mock"` and
will not pass — see :attr:`PipelineResult.evidence_is_real`. That is the designed
behaviour: a spec is not evidence-backed without observed server-side behaviour.

Everything re-exported here is a stable surface. Anything reached by importing a
submodule directly is internal and may move between versions.

The names are imported lazily via ``__getattr__`` so that ``import
sf_video_blueprint`` stays cheap and, more importantly, so a consumer who only
wants one symbol does not pay for the whole dependency graph. Eager imports here
would make the package unimportable in an environment missing an optional
dependency of an unrelated module.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

__all__ = [
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
]

# Import eagerly under a type checker so `from sf_video_blueprint import X`
# resolves for editors and mypy; at runtime the lazy path below is used.
if TYPE_CHECKING:
    from .pipeline import CaptureRejected, PipelineResult, run_pipeline
    from .spec_builder import DerivedAgentSpec, DerivedEntity, build_agent_spec
    from .spec_score import SpecScore, score_spec, score_spec_file

_LAZY: dict[str, str] = {
    "CaptureRejected": "pipeline",
    "PipelineResult": "pipeline",
    "run_pipeline": "pipeline",
    "DerivedAgentSpec": "spec_builder",
    "DerivedEntity": "spec_builder",
    "build_agent_spec": "spec_builder",
    "SpecScore": "spec_score",
    "score_spec": "spec_score",
    "score_spec_file": "spec_score",
}


def _get_version() -> str:
    from importlib.metadata import PackageNotFoundError, version

    try:
        return version("sf-video-blueprint")
    except PackageNotFoundError:
        # Running from a source tree that was never installed.
        return "0.0.0+unknown"


def __getattr__(name: str) -> Any:
    if name == "__version__":
        return _get_version()
    module_name = _LAZY.get(name)
    if module_name is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    from importlib import import_module

    value = getattr(import_module(f".{module_name}", __name__), name)
    # Cache on the module so repeated access skips the import machinery.
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted(__all__)

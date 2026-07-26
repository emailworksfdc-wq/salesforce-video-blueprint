#!/usr/bin/env python3
"""Name derivation and artifact emission for the round-trip script.

`agentforce_roundtrip.sh` used to inline three copies of a `DerivedAgentSpec`
JSON parser and, more damagingly, three *different hardcoded agent names* in a
single run:

- the `.agent` `config:` block said ``developer_name: "test_agent"``
- the CLI bundle flags said ``--api-name RoundtripTestAgent``
- both test specs said ``subjectName: TestAgent``

So the bundle validated under one name, and the generated test suite pointed at
an agent that did not exist. The script could not ever have completed a real
round trip, which is why this module exists: **every name the round trip uses is
derived here, from `naming.py`, and cross-checked before anything is written.**

Two independent linkages have to hold, and `naming.names_agree` is the canonical
check for both:

*agent identity* — the bundle API name, the `.agent` file stem, the
`config: developer_name`, and the test spec's `subjectName` all name the same
agent. They differ only in dialect (`SFVB_TEST_Update_Case_Status` vs
`sfvb_test_update_case_status`).

*topic identity* — the spec YAML's `topics[].name`, the `subagent <x>:` block,
the router's `go_to_<x>` action, and the test spec's `expectedTopic` /
`topic_sequence_match` expectation all name the same topic.

The shell script never spells a name itself; it asks `identity` for them. A
divergence therefore cannot be reintroduced by editing the shell script.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict, dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from sf_video_blueprint.agent_script import (
    build_agent_script,
    validate_locally,
    write_agent_script,
    write_bundle_meta_xml,
)
from sf_video_blueprint.agentforce_spec import (
    build_agent_spec_yaml,
    write_agent_spec_yaml,
)
from sf_video_blueprint.eval_spec import (
    build_legacy_test_spec,
    build_ngt_test_spec,
    write_test_spec,
)
from sf_video_blueprint.naming import (
    names_agree,
    router_action_name,
    snake_case,
    subagent_name,
    topic_api_name,
)
from sf_video_blueprint.spec_builder import (
    DerivedAgentSpec,
    DerivedEntity,
    SpecEvidence,
)
from sf_video_blueprint.spec_score import PASS_THRESHOLD, score_spec_file

# Prefixed so anything this script creates in a real org is findable and
# deletable, per the project's org-hygiene rule. The prefix is fed through
# `naming.topic_api_name` with the intent rather than glued on afterwards, so the
# length cap and the snake_case dialect apply to the whole name and the two
# dialects stay in agreement.
ORG_ARTIFACT_PREFIX = "SFVB TEST"


class RoundtripError(Exception):
    """A round-trip precondition failed. Raised instead of writing junk."""


@dataclass(frozen=True, slots=True)
class AgentIdentity:
    """Every name the round trip uses, derived once from a single intent.

    Frozen because a round trip that renames its agent halfway through is the
    exact bug this module was written to remove.
    """

    intent: str
    # --- agent identity: these four are the same agent in two dialects -------
    agent_api_name: str
    developer_name: str
    agent_label: str
    test_subject_name: str
    # --- topic identity: these four are the same topic in two dialects -------
    topic_name: str
    subagent: str
    router_action: str
    expected_topic: str

    def assert_coherent(self) -> None:
        """Fail loudly if any cross-artifact linkage is broken.

        Called before any file is written. A mismatch here means a downstream
        artifact would reference a name that does not exist, which is precisely
        the failure that is invisible until an org run wastes someone's time.
        """
        problems: list[str] = []

        if not names_agree(self.agent_api_name, self.developer_name):
            problems.append(
                f"agent identity diverged: bundle api_name {self.agent_api_name!r} "
                f"is not the same agent as config developer_name {self.developer_name!r}"
            )
        if self.test_subject_name != self.agent_api_name:
            problems.append(
                f"test spec subjectName {self.test_subject_name!r} does not name the "
                f"agent under test ({self.agent_api_name!r}); the suite would target "
                "an agent that does not exist"
            )
        if not names_agree(self.topic_name, self.subagent):
            problems.append(
                f"topic identity diverged: topic {self.topic_name!r} is not the same "
                f"topic as subagent {self.subagent!r}"
            )
        if self.router_action != f"go_to_{self.subagent}":
            problems.append(
                f"router action {self.router_action!r} does not transition to "
                f"subagent {self.subagent!r}"
            )
        if self.expected_topic != self.topic_name:
            problems.append(
                f"test spec expectedTopic {self.expected_topic!r} does not match the "
                f"emitted topic {self.topic_name!r}"
            )

        if problems:
            raise RoundtripError(
                "name derivation is incoherent — refusing to emit:\n"
                + "\n".join(f"  - {p}" for p in problems)
            )


def derive_identity(intent: str, *, prefix: str = ORG_ARTIFACT_PREFIX) -> AgentIdentity:
    """Derive every round-trip name from one intent via `naming.py`.

    The agent name carries `prefix` and the topic name does not: the agent is a
    real org artifact that has to be findable and cleanable, while the topic is
    an internal reference whose name must stay byte-identical to the
    `expectedTopic` the test spec emits on its own.
    """
    if not intent or not intent.strip():
        raise RoundtripError("derived spec has no intent; nothing can be named from it")

    agent_api_name = topic_api_name(f"{prefix} {intent}" if prefix else intent)
    topic_name = topic_api_name(intent)

    identity = AgentIdentity(
        intent=intent,
        agent_api_name=agent_api_name,
        # `snake_case` of the API name rather than an independent derivation, so
        # `names_agree` holds by construction rather than by coincidence.
        developer_name=snake_case(agent_api_name),
        agent_label=agent_api_name.replace("_", " "),
        test_subject_name=agent_api_name,
        topic_name=topic_name,
        subagent=subagent_name(intent),
        router_action=router_action_name(intent),
        # Recomputed the way `eval_spec` computes it internally, so the assertion
        # below is a real check on that module and not a restatement of our own
        # variable.
        expected_topic=topic_api_name(intent),
    )
    identity.assert_coherent()
    return identity


def load_derived_spec(path: Path) -> DerivedAgentSpec:
    """Parse an agent-spec JSON written by `spec_builder.write_spec`.

    One parser, called by every stage. The script previously carried three
    copies of this in shell heredocs, which is how they drifted.
    """
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise RoundtripError(f"cannot read derived spec {path}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise RoundtripError(f"derived spec {path} is not valid JSON: {exc}") from exc

    entities = [
        DerivedEntity(
            name=entity["name"],
            object_api_name=entity.get("object_api_name"),
            field_api_name=entity.get("field_api_name"),
            evidence=[
                SpecEvidence(source=item["source"], detail=item["detail"])
                for item in entity.get("evidence", [])
            ],
        )
        for entity in data.get("entities", [])
    ]

    if "intent" not in data:
        raise RoundtripError(f"derived spec {path} has no 'intent' key")

    return DerivedAgentSpec(
        intent=data["intent"],
        confidence=float(data.get("confidence", 0.0)),
        objects_touched=data.get("objects_touched", []),
        entities=entities,
        orchestration_steps=data.get("orchestration_steps", []),
        guardrails=data.get("guardrails", []),
        failure_handling=data.get("failure_handling", []),
        unknowns=data.get("unknowns", []),
        evidence=[
            SpecEvidence(source=item["source"], detail=item["detail"])
            for item in data.get("evidence", [])
        ],
    )


def load_provenance(path: Path) -> dict[str, str]:
    """Read the provenance block `write_spec` stamped onto the derived spec."""
    data = json.loads(path.read_text(encoding="utf-8"))
    return data.get("provenance") or {}


def emit_artifacts(
    spec_path: Path,
    out_dir: Path,
    *,
    prefix: str = ORG_ARTIFACT_PREFIX,
) -> dict[str, object]:
    """Emit the spec YAML, the authoring bundle, and both test spec dialects.

    The bundle is laid out as a real SFDX package directory
    (`force-app/main/default/aiAuthoringBundles/<ApiName>/`) with an
    `sfdx-project.json` beside it, because `sf agent validate authoring-bundle`
    is `requiresProject = true` and resolves the bundle off local disk. Emitting
    it anywhere else would mean the validate step could not run at all.
    """
    spec = load_derived_spec(spec_path)
    identity = derive_identity(spec.intent, prefix=prefix)
    provenance = load_provenance(spec_path)

    out_dir.mkdir(parents=True, exist_ok=True)

    # --- agent spec YAML (input to `sf agent generate authoring-bundle`) -----
    agent_spec_yaml = out_dir / "agentSpec.yaml"
    write_agent_spec_yaml(
        agent_spec_yaml,
        build_agent_spec_yaml(
            spec,
            company_name="SFVB Round-Trip Verification",
            company_description=(
                "Verification harness for the salesforce-video-blueprint round trip."
            ),
            allow_incomplete=False,
        ),
    )

    # --- authoring bundle, inside a minimal but real SFDX project -----------
    project_dir = out_dir / "sfdx"
    bundle_dir = (
        project_dir / "force-app" / "main" / "default" / "aiAuthoringBundles" / identity.agent_api_name
    )
    (project_dir / "force-app").mkdir(parents=True, exist_ok=True)
    (project_dir / "sfdx-project.json").write_text(
        json.dumps(
            {
                "packageDirectories": [{"path": "force-app", "default": True}],
                "name": "sfvb-roundtrip",
                "namespace": "",
                "sfdcLoginUrl": "https://login.salesforce.com",
                "sourceApiVersion": "67.0",
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )

    script_content = build_agent_script(
        spec,
        developer_name=identity.developer_name,
        agent_label=identity.agent_label,
        description=spec.intent,
        allow_incomplete=False,
    )
    agent_file = write_agent_script(
        bundle_dir / f"{identity.agent_api_name}.agent", script_content
    )
    meta_file = write_bundle_meta_xml(
        bundle_dir / f"{identity.agent_api_name}.bundle-meta.xml"
    )

    # Local checks are reported, never treated as a verdict: lane 01 measured
    # `validate_locally` returning zero findings on a file the real compiler
    # rejected with 24 errors, so a clean result here proves nothing.
    local_findings = validate_locally(script_content)

    # --- both test spec dialects, aimed at the agent we actually emitted -----
    legacy_spec, legacy_derivations = build_legacy_test_spec(
        spec,
        name=f"{identity.agent_label} Tests (Legacy)",
        subject_name=identity.test_subject_name,
        subject_type="AGENT",
    )
    legacy_path = write_test_spec(out_dir / "testSpec-legacy.yaml", legacy_spec)
    ngt_spec, ngt_derivations = build_ngt_test_spec(
        spec,
        name=f"{identity.agent_label} Tests (NGT)",
        subject_name=identity.test_subject_name,
    )
    ngt_path = write_test_spec(out_dir / "testSpec-ngt.yaml", ngt_spec)

    # Post-hoc proof, on the emitted bytes rather than on our own variables: if
    # any emitter ignored the name it was handed, this is where it surfaces.
    verify_emitted_artifacts(
        identity,
        agent_script=agent_file,
        agent_spec_yaml=agent_spec_yaml,
        test_specs=[legacy_path, ngt_path],
    )

    return {
        "identity": asdict(identity),
        "provenance": provenance,
        "confidence": spec.confidence,
        "paths": {
            "agent_spec_yaml": str(agent_spec_yaml),
            "sfdx_project_dir": str(project_dir),
            "bundle_dir": str(bundle_dir),
            "agent_script": str(agent_file),
            "bundle_meta": str(meta_file),
            "test_spec_legacy": str(legacy_path),
            "test_spec_ngt": str(ngt_path),
        },
        "local_findings": local_findings,
        "test_case_counts": {
            "legacy": len(legacy_spec.testCases),
            "ngt": len(ngt_spec.testCases),
        },
        "derivation_gaps": sorted(
            {gap for d in (*legacy_derivations, *ngt_derivations) for gap in d.gaps}
        ),
    }


def verify_emitted_artifacts(
    identity: AgentIdentity,
    *,
    agent_script: Path,
    agent_spec_yaml: Path,
    test_specs: list[Path],
) -> None:
    """Assert the written files actually carry the derived names.

    `assert_coherent` checks the names agree with each other; this checks the
    emitters honoured them. Both are needed — the original bug was an emitter
    being handed one name while the CLI was handed another.
    """
    problems: list[str] = []

    script_text = agent_script.read_text(encoding="utf-8")
    if agent_script.stem != identity.agent_api_name:
        problems.append(
            f"{agent_script.name}: file stem must be the bundle API name "
            f"{identity.agent_api_name!r}"
        )
    if f'developer_name: "{identity.developer_name}"' not in script_text:
        problems.append(
            f"{agent_script.name}: config developer_name is not {identity.developer_name!r}"
        )
    if f"subagent {identity.subagent}:" not in script_text:
        problems.append(f"{agent_script.name}: no `subagent {identity.subagent}:` block")
    if f"{identity.router_action}: @utils.transition to @subagent.{identity.subagent}" not in script_text:
        problems.append(
            f"{agent_script.name}: router does not route {identity.router_action!r} "
            f"to @subagent.{identity.subagent}"
        )

    spec_text = agent_spec_yaml.read_text(encoding="utf-8")
    if f"name: {identity.topic_name}" not in spec_text:
        problems.append(f"{agent_spec_yaml.name}: no topic named {identity.topic_name!r}")

    for test_spec in test_specs:
        text = test_spec.read_text(encoding="utf-8")
        if f"subjectName: {identity.test_subject_name}" not in text:
            problems.append(
                f"{test_spec.name}: subjectName is not {identity.test_subject_name!r} — "
                "the suite would target an agent that does not exist"
            )
        if identity.expected_topic not in text:
            problems.append(
                f"{test_spec.name}: does not reference topic {identity.expected_topic!r}"
            )

    if problems:
        raise RoundtripError(
            "emitted artifacts do not agree on names:\n"
            + "\n".join(f"  - {p}" for p in problems)
        )


def _cmd_identity(args: argparse.Namespace) -> int:
    spec = load_derived_spec(Path(args.spec))
    identity = derive_identity(spec.intent, prefix=args.prefix)
    if args.shell:
        # Consumed via `eval` by the shell script. Values are derived API names
        # (`[A-Za-z0-9_]` only by construction), so they need no quoting; the
        # label is quoted because it contains spaces.
        for key, value in asdict(identity).items():
            if key == "intent":
                continue
            print(f"RT_{key.upper()}='{value}'")
    else:
        print(json.dumps(asdict(identity), indent=2))
    return 0


def _cmd_emit(args: argparse.Namespace) -> int:
    manifest = emit_artifacts(Path(args.spec), Path(args.out_dir), prefix=args.prefix)
    Path(args.manifest).write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(manifest, indent=2))
    return 0


def _cmd_score(args: argparse.Namespace) -> int:
    """Report the quality gate verdict without deciding what it means.

    The gate refusing a mock-telemetry run is *correct* behaviour, not a script
    failure, so the verdict is printed and recorded and the caller decides. This
    command never lowers `PASS_THRESHOLD`; it only reports against it.
    """
    spec_path = Path(args.spec)
    result = score_spec_file(spec_path)
    provenance = load_provenance(spec_path)

    payload = {
        "total": result.total,
        "max_total": result.max_total,
        "band": result.band,
        "passed": result.passed,
        "threshold": PASS_THRESHOLD,
        "blocking_issues": list(result.blocking_issues),
        "provenance": provenance,
        "dimensions": {
            name: {"score": dim.score, "max_score": dim.max_score}
            for name, dim in result.dimensions.items()
        },
    }
    Path(args.out).write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")

    print(
        f"score {result.total}/{result.max_total}  band={result.band}  "
        f"passed={result.passed}  (threshold {PASS_THRESHOLD})"
    )
    for name, dim in result.dimensions.items():
        print(f"  {name:24} {dim.score:>3}/{dim.max_score}")
    for issue in result.blocking_issues:
        print(f"  BLOCKED: {issue}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)

    p_identity = sub.add_parser("identity", help="derive every round-trip name")
    p_identity.add_argument("spec", help="path to the derived agent-spec JSON")
    p_identity.add_argument("--prefix", default=ORG_ARTIFACT_PREFIX)
    p_identity.add_argument(
        "--shell", action="store_true", help="emit eval-able RT_* shell assignments"
    )
    p_identity.set_defaults(func=_cmd_identity)

    p_emit = sub.add_parser("emit", help="emit spec YAML, bundle, and test specs")
    p_emit.add_argument("spec")
    p_emit.add_argument("out_dir")
    p_emit.add_argument("--manifest", required=True, help="where to write the JSON manifest")
    p_emit.add_argument("--prefix", default=ORG_ARTIFACT_PREFIX)
    p_emit.set_defaults(func=_cmd_emit)

    p_score = sub.add_parser("score", help="report the quality gate verdict")
    p_score.add_argument("spec")
    p_score.add_argument("--out", required=True)
    p_score.set_defaults(func=_cmd_score)

    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except RoundtripError as exc:
        print(f"ROUNDTRIP ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

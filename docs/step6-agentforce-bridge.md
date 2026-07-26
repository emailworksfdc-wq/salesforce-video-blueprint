# Step 6 Operator Runbook — Agentforce Bridge and Iteration Loop

**Owner:** B10  
**Purpose:** Bridge derived specs to Agentforce agents and run the iteration loop to improve them.

This runbook documents the real CLI chain, the test-runner dialects, the iteration loop that is the user's stated end goal, and every gap that exists today. It matches the honest tone of the project README: no marketing, no capabilities that aren't wired.

---

## 1. The Real CLI Chain

These facts are extracted from the installed Salesforce CLI (`sf 2.143.6`, `@salesforce/plugin-agent`, `@salesforce/agents`) and from `scripts/agentforce_roundtrip.sh` (B9), not from guesses:

```
DerivedAgentSpec (JSON)
    ↓ agentforce_spec.py (B1)
agentSpec.yaml
    ↓ sf agent generate authoring-bundle --spec <yaml> --target-org <org>
Agent Script (.agent)
    ↓ sf agent validate authoring-bundle --api-name <name> --target-org <org>
Validation result
    ↓ sf agent publish (when ready)
Live agent
```

**Key facts:**

1. `sf agent generate authoring-bundle` REQUIRES an org because it calls the org's LLM to expand topics. No local-only path exists.
2. `sf agent create --spec` also exists but Salesforce does NOT recommend it. Non-Agent-Script agents are less flexible and harder to maintain. Always use the authoring-bundle route.
3. `sf agent validate authoring-bundle` is the ONLY authority on whether an `.agent` file is syntactically correct. Local structural checks in `agent_script.validate_locally()` catch obvious errors (tabs, unclosed quotes, missing blocks) but are NOT a substitute for CLI validation — measured: it reported zero findings on a file the compiler rejected with 24 errors.
4. **`validate` does NOT require a deployed bundle, or `generate` to have run.** It is `requiresProject = true`: it resolves `--api-name` against a local SFDX package directory and POSTs the file content to the compile API, using the org for auth only. Naming a bundle that is not on local disk yields `AABNotFound: … Searched in: <project>/force-app/`, never an org lookup. So the chain above can be entered at `.agent` — you can emit locally and validate, with no deploy and no org metadata created.
5. The CLI writes Agent Script files in a specific key order and with specific indentation (4 spaces, load-bearing). A single wrong indent level produces a compile error or semantically different behavior.

---

## 2. The Two Test-Runner Dialects and Why Conflating Them Fails

Salesforce has TWO test-runner systems for Agentforce agents, with incompatible YAML shapes:

### Legacy: AiEvaluationDefinition (Testing Center)

```yaml
name: My_Tests
subjectType: AGENT
subjectName: My_Agent
testCases:
  - utterance: "Set case 500... to Working"
    expectedTopic: Update_Case_Status
    expectedActions: ["UpdateCaseStatus"]
    expectedOutcome: "Confirms the case status is now Working"
    metrics: [completeness, coherence, conciseness, output_latency_milliseconds]
```

**Valid metrics:** `completeness`, `coherence`, `conciseness`, `output_latency_milliseconds` (from `@salesforce/agents/lib/utils.js` line 66).

Legacy expectation fields (`expectedTopic`, `expectedActions`, `expectedOutcome`) map to runtime assertion names: `topic_sequence_match`/`topic_assertion`, `action_sequence_match`/`actions_assertion`, `bot_response_rating`/`output_validation`.

### NGT: AiTestingDefinition (Agentforce Studio)

```yaml
name: My_Tests
subjectName: My_Agent
testCases:
  - inputs:
      - utterance: "..."
        conversationHistory: []
    scorers:
      - name: topic_match
        expected: Update_Case_Status
```

**Scorers requiring `expected:`** (from `@salesforce/agents/lib/ngtScorerCatalog.js`):  
`topic_sequence_match`, `action_sequence_match`, `agent_handoff_match`, `bot_response_rating`, `response_match`

**Quality scorers (no `expected` needed):**  
`coherence`, `conciseness`, `factuality`, `completeness`, `task_resolution`, `output_latency_milliseconds`

**Critical differences:**

- NGT uses `inputs` (list) + `scorers` (list), not flat expectation fields
- NGT requires `conversationHistory: []` for the `task_resolution` scorer
- Multi-agent subjects REQUIRE an `agent_handoff_match` scorer
- Guessing which runner the org uses is unsafe — `sf agent generate test-spec` auto-detects from org metadata

**Default to legacy/testing-center** unless you know the target org only supports `AiTestingDefinition`. When in doubt, let the CLI decide via `sf agent generate test-spec`.

---

## 3. The Iteration Loop — The User's Actual Goal

The user's stated objective is: "run that spec over and over to improve the spec to finally build it as an Agentforce agent."

`iterate.py` (B5) implements two paths:

### Offline (default, `use_cli=False`)

```
derived spec (JSON)  →  agentforce_spec.yaml  →  score (offline, deterministic)
                                                       ↓
                                                refine role prompt
                                                       ↓
                                    apply_offline_improvements() [STUB — see §9]
                                                       ↓
                                                 next version
```

- **No org calls**, no LLM, fully deterministic
- Cheap to iterate (< 1 second per round)
- Improvements are local transformations: tightening prose, normalizing names, reordering steps
- **CANNOT invent** entities, topics, or failure paths (no new evidence)
- The OFFLINE score (from `spec_score.py`) is the arbiter of convergence

### CLI mode (`use_cli=True`)

```
agentSpec.yaml (v1)
    ↓ sf agent generate agent-spec --spec v1.yaml --role "<refined>" --target-org <org>
agentSpec.yaml (v2, regenerated topics via org's LLM)
    ↓ score both versions offline
keep the higher score
```

- Shells to `sf agent generate agent-spec` which regenerates topics via the org's LLM
- **Costs org calls** and is nondeterministic (LLM may change behavior)
- The OFFLINE score remains the arbiter — even CLI-regenerated specs are scored deterministically
- **LIMITATION:** The CLI round-trip does NOT re-parse generated YAML back into a `DerivedAgentSpec`. B5 currently applies offline improvements in the loop; CLI regeneration is wired but the resulting YAML is not ingested back into the loop (see §9).

### Versioned Output (v1/, v2/, ...)

Every iteration writes a new versioned directory (`v1/`, `v2/`, ...) containing:

- `agent-spec.json` (the derived spec as JSON)
- `agentSpec.yaml` (the Agentforce spec)
- Score breakdown and notes

Versions are **never overwritten**. The audit trail IS the product. Overwriting a version destroys provenance and is forbidden.

### Stopping Conditions

The loop stops when any of these is true:

1. **Pass threshold reached:** `score.total >= PASS_THRESHOLD` (75) and no blocking issues
2. **Regression:** Score dropped from the previous round (keep the better version and stop)
3. **Convergence:** Improvement < `epsilon` (default 2 points) for 2 consecutive rounds
4. **Max rounds:** Reached `max_rounds` (default 5)
5. **InsufficientEvidenceError:** The RECORDING is inadequate and no amount of iteration can fix it — go re-record (exit code 5)

Exit code 5 is an **informative, legitimate failure**, not a bug. It means the recording did not capture enough data to derive a meaningful spec. The correct resolution is to re-record the process with `--track-record ObjectApiName:RecordId` to capture field deltas.

---

## 4. The Anti-Gaming Guard and Why It Exists

A loop that optimizes a score will find the cheapest path to a higher number. The cheapest path is deleting honest caveats — trimming `unknowns`, removing error-handling notes, or dropping low-confidence evidence.

`iterate.py` includes an **anti-gaming guard** at line 176:

```python
if score.total > prev.score.total and curr_unknowns_count < prev_unknowns_count:
    notes.append(
        "WARNING: score improved but unknowns decreased — verify this is honest "
        "refinement (filling gaps with evidence) and not gaming the metric by "
        "deleting caveats."
    )
```

This flags when a score improves WHILE `unknowns` shrink without new evidence. It's a signal that the loop may be hiding gaps rather than filling them.

**Offline improvements may NEVER invent:**

- Entities (no new fields)
- Topics (no new conversation branches)
- Failure paths (no fabricated error scenarios)

Allowed improvements:

- Tightening role prose (removing vague words)
- Normalizing entity names (camelCase consistency)
- Reordering orchestration steps (logical flow)
- Expanding guardrail wording (but not inventing new guardrails)

---

## 5. How the Spec Is Scored Offline

`spec_score.py` (B4) scores a `DerivedAgentSpec` deterministically, without an org, using seven dimensions:

| Dimension | Weight | What It Measures |
|-----------|--------|------------------|
| `evidence_grounding` | 30 | Every entity/guardrail traceable to a `SpecEvidence` entry. Entities from `"data-delta"` score HIGHER than `"inference"`. This is the MOST IMPORTANT dimension. |
| `completeness` | 15 | `objects_touched` non-empty, entities non-empty, orchestration steps non-trivial, guardrails present, failure_handling present. |
| `honesty` | 20 | Unknowns are DECLARED rather than hidden. A spec with declared `unknowns` at low confidence scores BETTER than high confidence with hidden gaps. |
| `specificity` | 10 | Intent is a concrete verb+object, not generic; no `UNRESOLVED`; topic/role text names real objects and fields. |
| `testability` | 10 | Required entities explicit enough to write test utterances against; failure paths observed (not merely asserted). |
| `placeholder_freedom` | 10 | Scans for markers like `Sample_Flow`, `button:Save`, `500xx0000012345AAA`, `UNRESOLVED:`, `TODO`, `FIXME`, `NEEDS EVIDENCE`, `Lorem`. |
| `provenance_integrity` | 5 | If `extraction_source` is `"stub"` or `telemetry_source` is `"mock"`, the spec CANNOT reach the top band. Hard cap. |

**Total:** 100 points  
**Pass threshold:** 75  
**Bands:** `low` (<60), `moderate` (60–74), `high` (75+)

### The Honesty Asymmetry (Critical)

This is deliberate and load-bearing:

- **High confidence (≥0.7) + structural gaps (no objects/entities):** Score = 0 (dishonest)
- **Low confidence (<0.7) + explicit `unknowns`:** Score = max (honest)
- **Low confidence + gaps NOT declared in `unknowns`:** Score = max/2 (somewhat honest)

Why? Because a loop optimizing a score will find the cheapest path to a higher number, and the cheapest path is deleting caveats. The honesty asymmetry **rewards explicit unknowns** so the loop is incentivized to surface gaps rather than hide them.

### Falsifiability

`spec_score.py` includes a self-check (`_assert_scorer_is_falsifiable()`) that creates two synthetic specs (one good, one bad) and confirms:

1. The good spec scores higher than the bad spec
2. The bad spec scores below `PASS_THRESHOLD`
3. The bad spec does NOT pass

A gate that always returns 100/100 trains the loop to fabricate data. The scorer must be able to FAIL.

---

## 6. Running the Round-Trip

`scripts/agentforce_roundtrip.sh` (B9) drives capture → derive → score → emit → validate → report, and reports honestly at every stage. It runs **fully offline by default**; the one org-dependent stage is opt-in behind `--org`.

### Usage

```bash
bash scripts/agentforce_roundtrip.sh [--capture <file>] [--spec <file>] [--org <alias>]
                                     [--out <dir>] [--keep-going]
```

**Options:**

- `--capture <file>`: DOM capture JSONL to derive a spec from. Defaults to `examples/case_triage.dom_capture.jsonl`, so the script runs with no arguments at all.
- `--spec <file>`: Start from an existing `DerivedAgentSpec` JSON instead of capturing. Mutually exclusive with `--capture`.
- `--org <alias>`: Run S5 (`sf agent validate authoring-bundle`) against this alias. Sandbox, scratch, or `.develop.my.salesforce.com` only. PPCDM and PPCaccenture are hard-blocked by alias with no override. **Omit it and S5 reports `SKIPPED` — the script never claims validation it did not perform.**
- `--out <dir>`: Output directory (default `./outputs/roundtrip`).
- `--keep-going`: Do not stop at the first failed stage. Still exits non-zero.

**Environment:**

- `PY_BIN`: Python interpreter (≥ 3.11). Auto-detected if unset.

There is no `DRY_RUN`. Offline is the default, so the old inversion — where org calls happened unless you opted out, and you had to pass a dummy alias to stay local — is gone.

### Stages (S1–S6)

| Stage | Description | Org required |
|-------|-------------|--------------|
| `s1_derive_spec` | Run the pipeline on the capture to produce the derived spec JSON. `SKIPPED` when `--spec` is given. | No |
| `s2_derive_names` | Derive every API name from `naming.py` via `roundtrip_lib.py identity` and assert all cross-artifact linkages agree. | No |
| `s3_score_gate` | Report the `spec_score` verdict. The verdict is **reported, never enforced down**: a mock-telemetry run is *supposed* to fail the gate. | No |
| `s4_emit_artifacts` | Emit `agentSpec.yaml`, the authoring bundle inside a real SFDX project, and both test-spec dialects — then re-read the written bytes and verify every derived name appears in them. | No |
| `s5_org_validate` | `sf agent validate authoring-bundle` — the only authority on `.agent` grammar. `SKIPPED` unless `--org` is given. | Yes |
| `s6_summary` | Write `roundtrip_summary.json` and print a verdict that names every skipped stage. | No |

`sf agent validate authoring-bundle` is `requiresProject = true`: it resolves the bundle from a **local** SFDX package directory and POSTs the file content to the compile API, using the org for auth only. **No deploy is required**, which is why S4 writes a real `sfdx-project.json` and why this script creates no metadata in the org. (The earlier claim that validation requires an already-deployed bundle is wrong; see `_shared/findings/lane-01.md` and lane 07's report.)

**Exit code table:**

| Code | Meaning |
|------|---------|
| 0 | Every stage that **ran** passed. Skipped stages are reported, not counted as passes. |
| 1 | At least one stage failed |
| 2 | Bad arguments, or preflight failure (no usable Python, missing `sf`) |
| 3 | Org safety guard tripped |
| 5 | `InsufficientEvidenceError` — the recording is inadequate (a real finding, not a bug) |

### Offline run (no org, no credentials)

```bash
bash scripts/agentforce_roundtrip.sh --out ./outputs/roundtrip
```

Runs S1–S4 and S6, reports S5 as `SKIPPED`, and ends with:

```
LOCAL ROUND TRIP COMPLETE — NOTHING WAS VALIDATED BY SALESFORCE.
```

`roundtrip_summary.json` carries `"salesforce_validated": false` and `"org_alias": null`. There is deliberately **no single `"pass"` boolean** in the summary: the previous version wrote `{"pass": true}` while both org stages were skipped, and a downstream reader could not tell the difference between "validated" and "not attempted".

### With an org

```bash
bash scripts/agentforce_roundtrip.sh --org my-sandbox --out ./outputs/roundtrip
```

Adds S5. Measured against a Developer Edition org on 2026-07-26: exit 0, `{"success": true}`, and the run ends with

```
ROUND TRIP COMPLETE — Salesforce validated SFVB_TEST_Update_Case_Status in org AFT3.
```

The first time this ran it did **not** pass — the compiler returned 24 `CompilationError`s in the derived subagent's `instructions` block while `validate_locally()` reported zero findings on the same file. That emitter defect is fixed (see §9); the lesson that local validation is not a verdict is not. What S5 passing licenses is narrow: one bundle, one intent, one org, one CLI version, syntax only. It says nothing about whether the agent behaves correctly, and nothing has been published.

### Outputs

```
<out>/roundtrip.agent-spec.json     derived spec (S1)
<out>/roundtrip.html                HTML blueprint (S1)
<out>/score.json                    gate verdict, dimensions, blocking issues (S3)
<out>/agentSpec.yaml                Agentforce spec YAML (S4)
<out>/sfdx/sfdx-project.json        so `sf agent validate` can resolve the bundle
<out>/sfdx/force-app/main/default/aiAuthoringBundles/<ApiName>/<ApiName>.agent
<out>/sfdx/force-app/main/default/aiAuthoringBundles/<ApiName>/<ApiName>.bundle-meta.xml
<out>/testSpec-legacy.yaml          AiEvaluationDefinition dialect (S4)
<out>/testSpec-ngt.yaml             AiTestingDefinition dialect (S4)
<out>/emit_manifest.json            derived names + every emitted path (S4)
<out>/roundtrip_summary.json        per-stage status, derived names, what was skipped (S6)
<out>/logs/                         stdout/stderr per stage, plus s5_validate.json
```

`<ApiName>` is derived, not fixed — it is `naming.topic_api_name("SFVB TEST " + intent)`, e.g. `SFVB_TEST_Update_Case_Status` for the bundled example capture. The `SFVB_TEST_` prefix exists so anything that does reach an org is findable and deletable.

### One name, derived once

The script does not spell a single API name. `scripts/roundtrip_lib.py identity` derives all of them from `naming.py` and refuses to continue unless two linkages hold:

- **agent identity** — bundle API name ≡ `.agent` file stem ≡ `config: developer_name` ≡ test spec `subjectName`
- **topic identity** — spec YAML `topics[].name` ≡ `subagent <x>:` ≡ router `go_to_<x>` ≡ test spec `expectedTopic`

`naming.names_agree()` is the canonical check for both dialect pairs. This is enforced twice: once on the derived names before anything is written, and again on the written bytes afterwards (`verify_emitted_artifacts`), because the original bug was an emitter being handed one name while the CLI was handed another. `tests/test_roundtrip_lib.py` pins it, including a regression guard that rejects the exact three-name triple this script used to carry.

### In CI

Because S1–S4 and S6 need no org, the whole offline chain runs on every push. The `roundtrip` job runs the script with no `--org` and then hands the summary to `scripts/roundtrip_check.py`, which reads the **summary rather than the exit code** and fails if the run claims validation it skipped or if any artifact names a different agent. S5 is never run in CI — CI has no org, and the job asserts that it is reported as `skipped` rather than assumed to have passed.

---

## 7. Safety

### Sandbox/Scratch/Dev Only

- The org safety guard (S3 preflight) refuses to proceed unless `instanceUrl` matches:
  - `*.sandbox.my.salesforce.com`
  - `*.scratch.my.salesforce.com`
  - `*.develop.my.salesforce.com`
- PPCDM and PPCaccenture are **hard-blocked by alias name** (no override)
- The guard is fail-closed: if `sf org display` fails or `instanceUrl` cannot be resolved, the script exits with code 3

### Secrets

- Tokens are passed via environment variables (`SF_ACCESS_TOKEN`), never as argv
- Rationale: `ps aux` is world-readable on Unix systems

### No Override

There is NO override for the org safety guard. If you need to run against a production org (DON'T), you must edit the script source — that is a deliberate friction.

---

## 8. The Impedance Mismatch — Be Explicit, It Is the Key Design Honesty

Our `DerivedAgentSpec` (from `spec_builder.py`) has first-class fields for:

- `guardrails: list[str]`
- `failure_handling: list[str]`

The Agentforce spec YAML has NO dedicated fields for these. They survive only as instruction prose inside topic descriptions.

From `agentforce_spec.py` (B1) line 275:

```python
# Guardrails (critical — must not be silently lost)
if spec.guardrails:
    description_parts.append("\nGuardrails:")
    for guard in spec.guardrails:
        description_parts.append(f"- {guard}")

# Failure handling (if observed)
if spec.failure_handling and not spec.failure_handling[0].startswith("No failures"):
    description_parts.append("\nError handling:")
    for handling in spec.failure_handling:
        description_parts.append(f"- {handling}")
```

This is **lossy**. Guardrails and failure handling are embedded as text in `topics[].description`, not as structured fields. A human MUST verify they survived in the generated `.agent` file.

### What a Reviewer Should Check

Before publishing an agent, confirm:

1. **Guardrails are present** in the `.agent` instructions (search for the guardrail text)
2. **No `[NEEDS EVIDENCE` markers** remain in the `.agent` file
3. **Topic names are consistent** between `agentSpec.yaml`, the `.agent` subagent names, and the test spec `expectedTopic` values
4. **Actions are wired** (see §9 — actions are NOT fabricated)
5. **Failure paths were actually recorded** (not assumed)
6. **Provenance is `dom-capture` + `live-org`**, not `stub`/`mock` (check the JSON's `provenance` key)

---

## 9. What Is NOT Wired (Stated Plainly)

Read the source: B1, B2, B3, B5 reported their own gaps. This section consolidates them.

### Actions Are Not Fabricated

From `agent_script.py` (B2) line 19:

> CONSTRAINT: This module NEVER fabricates `@apex.Foo` or `@flow.Bar` action references. The only safe actions are `@utils.transition to @subagent.X` and `@utils.escalate`. If the recording observed a Flow/Apex invocation, emit a clearly-marked instruction line noting that, not a fake action reference.

**Reality:** Generated `.agent` files contain only routing actions (`go_to_<topic>`) and transitions (`@utils.transition to @subagent.<name>`). No Flow or Apex actions are emitted. If the recording observed a Flow execution (via telemetry), it appears as a prose note in the subagent's instructions, not as a wired action.

⚠️ **CORRECTED 2026-07-26 — do not hand-add `@flow.`/`@apex.` to a `.agent` file.**
This section previously said a human MUST manually add the `@flow.FlowName` or
`@apex.ClassName.methodName` action reference. **The Agent Script compiler rejects
both namespaces outright.** Measured against org AFT3 with
`sf agent validate authoring-bundle --json`, by appending a single `actions:`
entry to an otherwise-compiling bundle:

```
@flow.SFVB_TEST_Nonexistent_Flow -> exit 1
    CompilationError: Cannot invoke '@flow.SFVB_TEST_Nonexistent_Flow' —
    'flow' is not a valid invocation target.
@apex.SFVB_TEST_NoClass -> exit 1
    CompilationError: Cannot invoke '@apex.SFVB_TEST_NoClass' —
    'apex' is not a valid invocation target.
```

The invocation namespace is a **closed set**, and unknown namespaces fail
differently from unknown members of a known namespace:

```
@nonsense_ns.Foo    -> "'nonsense_ns' is not a valid invocation target."
@utils.no_such_util -> "'no_such_util' is not defined in utils"
```

So the failure is not "the Flow doesn't exist yet" — the dialect does not accept
`flow`/`apex` as invocation targets at all. Wiring a real Flow into an agent is
therefore **not** an edit to the `.agent` file; it happens through a different
surface (the metadata/publish path), which this project has not measured. Until
someone measures it, treat the prose note in the instructions as the end of what
this project can produce. The emitter's refusal is now guarded by
`test_emitter_never_uses_a_compiler_rejected_namespace`.

### expectedActions Is Intentionally Empty

From `eval_spec.py` (B3) line 169:

```python
expectedActions=[],  # see derivation rule 7
```

And line 179 (in the derivations):

```python
gaps=[
    "expectedActions left empty: action API names were not observed. "
    "Re-run with action telemetry enabled or manually fill from deployed agent metadata."
],
```

**Reality:** The test specs emit `expectedActions: []` (legacy) or omit the `action_sequence_match` scorer (NGT) because action API names are not observed by the telemetry collector. To fill this, either:

1. Re-run the recording with action telemetry enabled (requires deeper instrumentation), OR
2. After deploying the agent, query its metadata and manually populate `expectedActions` in the test spec

### Offline Improvement Is a Stub

From `iterate.py` (B5) line 319:

```python
def _apply_offline_improvements(spec: DerivedAgentSpec, score: Any) -> DerivedAgentSpec:
    """Apply deterministic, local improvements without new evidence.
    
    ...
    
    This is a placeholder for deterministic transformations. Real implementation
    would parse recommendations and apply specific fixes.
    """
    # Placeholder: for now, return the spec unchanged. A production version would
    # apply specific transformations based on score.recommendations.
    return spec
```

**Reality:** Offline improvements are NOT implemented. The loop can run and score successive versions, but when `use_cli=False`, each iteration returns the same spec unchanged. The only way to get a different spec is to use `use_cli=True`, which shells to the CLI and regenerates topics via the org's LLM.

### CLI Round-Trip Does Not Re-Parse YAML

From `iterate.py` (B5) line 240:

```python
# NOTE: This is a simplification. A production version would need to either:
# (1) round-trip YAML -> DerivedAgentSpec (requires a parser agent), OR
# (2) keep iterating on the YAML directly and score YAML only.
# For this prototype, we'll apply offline improvements instead when use_cli=False.
pass
```

**Reality:** When `use_cli=True`, the loop calls `sf agent generate agent-spec --spec <prev>.yaml --role "<refined>"` and writes `<next>.yaml`, but it does NOT parse the new YAML back into a `DerivedAgentSpec`. The loop continues to score and refine based on the original derived spec. The CLI-generated YAML is written to disk but not ingested back into the iteration.

### The Emitted `.agent` Did Not Compile (Fixed — Now Compiler-Verified)

From `agent_script.py` (B2) line 12:

> CRITICAL: This module emits actual code in a grammar owned by Salesforce. The ONLY authoritative reference for Agent Script syntax is: `@salesforce/agents/lib/templates/agentScriptTemplate.js`. Do not invent syntax.

**Reality (updated — this has now been measured, and the first result was a failure):** validation has run. The first submission exited 1 with 24 `CompilationError`s, the first being ``Syntax error: unexpected `->` [Ln 108, Col 8]`` in the derived subagent's `instructions` block: `->` is only legal as a key's value (`instructions: ->`), and the `|` continuation lines must indent one level deeper than that key. Both facts are now compiler-verified rather than inferred from the template. After the emitter fix the same bundle compiles — exit 0, `{"success": true}`.

`validate_locally()` reported **zero** findings on the rejected file, before and after. So local structural checks are not merely "not a substitute" for CLI validation — they were demonstrably blind to the entire error class that the only real test found. Reproduce either result with `bash scripts/agentforce_roundtrip.sh --org <alias>` (S5).

### Three Emitters Derive Topic API Names Independently (Coupling Risk)

From `eval_spec.py` (B3) line 66:

```python
def _to_api_name(text: str) -> str:
    """Turn a phrase into a CapitalCase API name.
    
    This is the same normalisation B1/B2 use. The three emitters MUST agree on
    topic-name derivation or the test suite will reference topics that don't
    exist in the generated spec.
    
    COUPLING RISK: This logic is duplicated by convention across three agents.
    If it diverges, round-trip tests break silently. Centralising this into a
    shared utility (spec_builder, or a new naming.py) would be safer, but
    changing spec_builder is orchestrator-gated, so for now this comment flags
    the risk.
    """
```

**Reality (updated — this has been fixed):** the duplication is gone. All three emitters now delegate to `naming.py`: `agentforce_spec._to_api_name` and `eval_spec._to_api_name` are aliases of `naming.topic_api_name`, and `agent_script` imports `topic_api_name` / `subagent_name` / `router_action_name` / `snake_case` directly (`to_snake_case` survives only as a back-compat shim). The docstring above is a historical note.

The *consumer* side is where divergence could still be reintroduced, because a caller can hand different names to different emitters — which is exactly the bug `agentforce_roundtrip.sh` shipped with. `scripts/roundtrip_lib.py` closes that: one `AgentIdentity`, derived once, asserted coherent before emission and re-verified against the written bytes afterwards. See §6.

---

## 10. Worked Example (End to End)

### Recording → Derived Spec

```bash
export SF_ACCESS_TOKEN="$(cat ~/.sf_token)"
.venv/bin/python -m sf_video_blueprint.cli ./inputs/update_case.mp4 \
  --org-url "https://my-sandbox.my.salesforce.com" \
  --mode live \
  --track-record Case:500xx0000012345AAA \
  --spec-output ./outputs/derived_spec.json
```

Outputs: `./outputs/derived_spec.json` (a `DerivedAgentSpec` as JSON)

### Derived Spec → YAML

```bash
.venv/bin/python -c "
import json, pathlib
from sf_video_blueprint.agentforce_spec import build_agent_spec_yaml, write_agent_spec_yaml
from sf_video_blueprint.spec_builder import DerivedAgentSpec, DerivedEntity, SpecEvidence

data = json.loads(pathlib.Path('./outputs/derived_spec.json').read_text())
# Parse JSON to DerivedAgentSpec (roundtrip_lib.load_derived_spec is the one parser)
spec_yaml = build_agent_spec_yaml(
    derived_spec,
    company_name='Acme Corp',
    company_description='Case management',
    allow_incomplete=False
)
write_agent_spec_yaml(pathlib.Path('./outputs/agentSpec.yaml'), spec_yaml)
"
```

Outputs: `./outputs/agentSpec.yaml`

### YAML → .agent (CLI, Requires Org)

```bash
sf agent generate authoring-bundle \
  --target-org my-sandbox \
  --spec ./outputs/agentSpec.yaml \
  --name "Case Updater" \
  --api-name "CaseUpdater" \
  --output-dir ./outputs/authoring_bundle \
  --force-overwrite
```

Outputs: `./outputs/authoring_bundle/agent/CaseUpdater.agent`

### Validate .agent (CLI, Requires Org)

```bash
sf agent validate authoring-bundle \
  --target-org my-sandbox \
  --api-name "CaseUpdater"
```

Exit codes: 0=pass, 1=compilation errors, 2=404, 3=500

### Derived Spec → Test Spec

```bash
.venv/bin/python -c "
import json, pathlib
from sf_video_blueprint.eval_spec import build_legacy_test_spec, write_test_spec
from sf_video_blueprint.spec_builder import DerivedAgentSpec, DerivedEntity, SpecEvidence

data = json.loads(pathlib.Path('./outputs/derived_spec.json').read_text())
# Parse JSON to DerivedAgentSpec (roundtrip_lib.load_derived_spec is the one parser)
test_spec, derivations = build_legacy_test_spec(
    derived_spec,
    name='CaseUpdater_Tests',
    subject_name='CaseUpdater',
    subject_type='AGENT'
)
write_test_spec(pathlib.Path('./outputs/testSpec.yaml'), test_spec)
"
```

Outputs: `./outputs/testSpec.yaml`

### Score the Spec (Offline)

```bash
.venv/bin/python -c "
from sf_video_blueprint.spec_score import score_spec_file
score = score_spec_file(pathlib.Path('./outputs/derived_spec.json'))
print(score.summary())
for dim in score.dimensions.values():
    print(f'{dim.name}: {dim.score}/{dim.max_score}')
"
```

Example output:

```
PASS: 78/100 (high band), 0 blocking issue(s)
evidence_grounding: 25/30
completeness: 12/15
honesty: 20/20
specificity: 8/10
testability: 8/10
placeholder_freedom: 10/10
provenance_integrity: 5/5
```

### Iterate to Improve

```bash
.venv/bin/python -c "
from sf_video_blueprint.iterate import refine
from sf_video_blueprint.spec_score import score_spec_file
import json, pathlib

data = json.loads(pathlib.Path('./outputs/derived_spec.json').read_text())
# Parse JSON to DerivedAgentSpec (roundtrip_lib.load_derived_spec is the one parser)

result = refine(
    derived_spec,
    out_dir=pathlib.Path('./outputs/iterations'),
    company_name='Acme Corp',
    company_description='Case management',
    max_rounds=5,
    epsilon=2,
    org_alias='my-sandbox',
    use_cli=False  # or True to shell to sf agent generate agent-spec
)

print(f'Best version: v{result.best.version}, score: {result.best.score.total}/{result.best.score.max_total}')
print(f'Stop reason: {result.stop_reason}')
"
```

Outputs: `./outputs/iterations/v1/`, `v2/`, ..., each with `agent-spec.json`, `agentSpec.yaml`, and score breakdown.

### Validate End-to-End (All Stages)

```bash
bash scripts/agentforce_roundtrip.sh \
  --spec ./outputs/derived_spec.json \
  --org my-sandbox \
  --out ./outputs/roundtrip
```

Runs S1–S6 and reports each stage as `pass`, `FAIL`, or `SKIPPED`. Drop `--org` to run everything except S5 offline; the final line then says explicitly that nothing was validated by Salesforce.

---

## 11. What a Human Must Review Before Publishing an Agent

A checklist for reviewers:

| Check | Where | Why |
|-------|-------|-----|
| Guardrails present | `.agent` instructions | Agentforce spec YAML has no guardrail field; they're embedded in topic descriptions and can be lost |
| No `[NEEDS EVIDENCE` markers | `.agent`, YAML, test specs | Markers indicate `allow_incomplete=True` was used; these are gaps, not finished work |
| Topic names consistent | YAML, `.agent`, test specs | Three emitters derive names independently; if they diverge, tests reference topics that don't exist |
| Actions wired | `.agent` subagent actions | No Flow/Apex actions are auto-generated. **Do not hand-add `@flow.X`/`@apex.X` either — the compiler rejects those namespaces ("not a valid invocation target", measured on AFT3 2026-07-26).** Only `@utils.transition`/`@utils.escalate`/`@subagent.X` compile |
| Failure paths recorded | Derived spec JSON | If `failure_handling` says "UNTESTED", no failure test cases exist. Record a failing run to observe error paths. |
| Provenance is real | Derived spec JSON | Check `provenance.extraction_source` and `provenance.telemetry_source`. If either is `stub` or `mock`, the spec is fabricated. |
| CLI validation passed | S4 output logs | Local structural checks are NOT authoritative. Only `sf agent validate authoring-bundle` confirms grammar correctness. |

---

## Report to Orchestrator

### Sections Written

All 11 required sections:

1. The real CLI chain (extracted from B9, CLI flags verified)
2. The two test-runner dialects and why conflating them fails (scorer/metric names from `ngtScorerCatalog.js` and `utils.js`)
3. The iteration loop (offline vs CLI mode, versioned output, stopping conditions)
4. The anti-gaming guard (line 176 of `iterate.py`, rationale for honesty asymmetry)
5. How the spec is scored offline (7 dimensions, weights, pass threshold, falsifiability check)
6. Running the round-trip (B9 stages, exit codes, DRY_RUN mode, real example)
7. Safety (sandbox/scratch/dev only, token passing, no override)
8. The impedance mismatch (guardrails/failure_handling are lossy, what to check in `.agent`)
9. What is NOT wired (actions not fabricated, `expectedActions` empty, offline improvement stub, CLI round-trip does not re-parse, three emitters derive names independently)
10. Worked example (recording → spec → YAML → `.agent` → validate → test spec → score → iterate)
11. What a human must review (checklist with 7 items)

### CLI Commands/Flags Documented and Where Confirmed

| Command | Flags | Confirmed From |
|---------|-------|---------------|
| `sf agent generate authoring-bundle` | `--target-org`, `--spec`, `--name`, `--api-name`, `--output-dir`, `--force-overwrite` | B9 lines 387–394, `--help` output |
| `sf agent validate authoring-bundle` | `--target-org`, `--api-name` | B9 lines 433–436, `--help` output |
| `sf agent generate agent-spec` | `--spec`, `--output-file`, `--role`, `--target-org` | B5 lines 358–370 |
| `sf agent generate test-spec` | (auto-detects runner) | B3 line 458, INTERFACE_CONTRACT.md §3.3 |
| `sf org display` | `--target-org`, `--json` | B9 lines 143–144 |

### Inconsistencies Found Between Modules

1. **Topic name derivation is duplicated across three modules:**
   - `agentforce_spec._to_api_name()` (B1, line 52): converts to `Capitalized_API_Name`
   - `agent_script.to_snake_case()` (B2, line 40): converts to `snake_case`
   - `eval_spec._to_api_name()` (B3, line 60): converts to `CapitalCase`
   
   **Risk:** If these diverge, test specs will reference topics that don't exist in the agent. B3 documents this as a coupling risk (line 66) but does not resolve it. Recommendation: Centralize topic-name derivation into a shared utility (e.g., `naming.py` or add to `spec_builder.py` if orchestrator allows).

2. **Placeholder marker lists are duplicated:**
   - `spec_score.PLACEHOLDER_MARKERS` (B4, line 54): 10 markers
   - `scripts/score_run.py`: (not read by me, but B4 notes drift risk at line 52)
   
   **Risk:** If these lists diverge, one gate may pass content the other would fail. B4 recommends centralizing these markers in a shared constants module.

3. **Offline improvement is a stub but the loop runs:**
   - `iterate.refine()` (B5) runs and scores multiple rounds
   - `_apply_offline_improvements()` (B5, line 319) returns the spec unchanged
   - The loop only produces different specs when `use_cli=True` (which shells to the CLI)
   
   **Status:** This is documented as "not wired" in §9. Not an inconsistency, but a limitation.

4. **CLI round-trip does not re-parse YAML:**
   - `iterate.refine()` (B5, line 240) notes that CLI-generated YAML is not ingested back into the loop
   - The loop continues to score and refine based on the original `DerivedAgentSpec`
   
   **Status:** Documented as "not wired" in §9. Not an inconsistency, but a gap.

5. **Exit code 5 (`InsufficientEvidenceError`) is used by both the emitters (B1, B2) and the roundtrip script (B9):**
   - Consistent across modules
   - B9 documents it as "legitimate failure" (line 649)
   
   **Status:** Consistent and correct.

6. **Test spec dialect detection:**
   - B3 (line 458) recommends "prefer letting the CLI decide" via `sf agent generate test-spec`
   - INTERFACE_CONTRACT.md (line 284) says "default to legacy/testing-center unless the target org only has AiTestingDefinition"
   
   **Minor tension:** B3's recommendation (let CLI decide) is safer than the contract's guidance (default to legacy). Not a breaking inconsistency, but the contract could be updated to say "prefer CLI detection over guessing."

All inconsistencies flagged for orchestrator review.
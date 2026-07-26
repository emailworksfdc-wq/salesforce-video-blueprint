# Interface contract — convergence round 2 (scoring & refinement loop)

Addendum to `docs/INTERFACE_CONTRACT.md`. Same rules apply: **one owner per file**, never
edit a file you do not own, never relax a gate. This addendum pins the behaviour the
round-2 fixes must produce, so two owners can work in parallel against a fixed interface.

## Why this round exists

Round 1 fixed derivation and naming. Round 2 targets the *scoring gate and the refinement
loop*, where six defects were confirmed empirically. Their common shape: **the gate cannot
fail, and the loop's only improvement mechanism is a no-op or actively harmful.** A spec
with duplicated steps, duplicated guardrails and explicitly UNTESTED error paths scored
100/100.

The governing principle from `spec_score.py`'s own docstring is currently violated:

> The scorer MUST be falsifiable — a beautifully structured spec built from fabricated
> data is worse than an honest incomplete one, because it invites trust.

## Confirmed defects

| ID | File | Defect |
|----|------|--------|
| D1 | `spec_score.py` | `_score_testability` matches the substring `"observed"`, which appears in the builder's *negative* sentinel `"No failures were observed in this run, so error paths are UNTESTED."` The declaration that error paths are untested therefore scores full marks for "failure paths observed". Same bug suppresses the `score_spec` "record a failing variant" recommendation. |
| D2 | `spec_score.py` | `_score_evidence_grounding` subtracts an inference penalty. `spec_builder._derive_entities` *unconditionally* adds an inference-grounded `recordId` entity whenever an object was observed. Every real spec is penalised for a field the builder mandates, and **deleting entities raises the total by up to 24 points.** |
| D3 | `iterate.py` | `_apply_offline_improvements` cannot raise the score on any input; deduplicating two identical orchestration steps *lowers* it 3 points, because `_score_completeness` rewards `len(steps) > 1` without checking distinctness. The loop has no working improvement path. |
| D4 | `iterate.py` | `refine()` scores with `score_spec` (in-memory), never `score_spec_file`, so `provenance_integrity` is awarded a free 5/5 and the hard stub/mock cap — the entire purpose of dimension 7 — is bypassed inside the loop. |
| D5 | both | Consequence of D1+D4: an obviously bad spec reaches 100/100 and `band="high"`. |
| D6 | `iterate.py` | The anti-gaming guard reads `current_spec.unknowns` on **both** sides of the comparison (`iterate.py:178-179`), so the condition is `n < n` and can never fire. Dead code presenting as a safety mechanism. |

## Pinned interface

### 1. `score_spec` signature (owner: spec_score.py)

```python
def score_spec(
    spec: DerivedAgentSpec,
    *,
    yaml_text: str | None = None,
    agent_script_text: str | None = None,
    provenance: dict[str, str] | None = None,   # NEW
) -> SpecScore: ...
```

Provenance semantics — **fail closed**, matching `scripts/score_run.py`:

* `provenance` supplied and both axes real (`markers.extraction_is_real` and
  `markers.telemetry_is_real`) → `provenance_integrity` = 5/5, no blocker.
* `provenance` supplied and either axis not real → `provenance_integrity` = 0 **and** a
  blocking issue **and** `passed = False`. Same behaviour `score_spec_file` has today.
* `provenance is None` (caller did not say) → `provenance_integrity` = **0** with a finding
  explaining that provenance was not supplied, and **no** blocking issue.

  Rationale: awarding 5 free points for silence is exactly defect D4. Scoring 0 without a
  blocker keeps `PASS_THRESHOLD = 75` reachable for genuine in-memory scoring (max 95) while
  removing the free ride. Do **not** add a blocker here — it would make every in-memory score
  fail and break the falsifiability self-check.

`score_spec_file` must be refactored to delegate provenance evaluation to the same helper
rather than duplicating it. Extract it as a module-level function so `iterate.py` never needs
to reimplement it:

```python
def score_provenance(provenance: dict[str, str] | None) -> tuple[DimensionScore, list[str]]:
    """Returns (dimension score, blocking issues)."""
```

### 2. `refine` signature (owner: iterate.py)

```python
def refine(
    spec: DerivedAgentSpec,
    *,
    out_dir: Path,
    company_name: str,
    company_description: str,
    max_rounds: int = 5,
    epsilon: int = 2,
    org_alias: str | None = None,
    use_cli: bool = False,
    provenance: dict[str, str] | None = None,   # NEW
) -> IterationResult: ...
```

`refine` must pass `provenance=provenance` into every `score_spec` call. It must **not**
reimplement provenance logic — import `score_provenance` / pass the kwarg.

## Required invariants (write these as tests)

These are behavioural requirements, not implementation instructions. Choose whatever formula
satisfies them; the tests are the contract.

### Anti-gaming (the important ones)

* **G1 — deletion must not pay.** Removing a genuinely-observed entity (evidence source
  `data-delta` or `ui-action`) from a spec must **never raise** `SpecScore.total`.
* **G2 — mandated inference is not the spec's fault.** Adding the `recordId` entity that
  `spec_builder._derive_entities` unconditionally emits (evidence source `inference`,
  `field_api_name == "Id"`) must **never lower** `SpecScore.total`. A recording cannot avoid
  it, so it must not be scored against the spec.
* **G3 — honesty must pay.** A spec that *declares* an unknown must score **>=** the
  otherwise-identical spec with that unknown deleted. Getting this backwards trains the loop
  to hide gaps.
* **G4 — refinement is monotone.** `_apply_offline_improvements(s, score_spec(s))` must
  **never lower** the total, for every spec in the test corpus.
* **G5 — refinement is effective.** There must exist at least one realistic below-threshold
  spec on which `_apply_offline_improvements` **raises** the total. Prove it with a test that
  asserts a strict increase. If no such spec exists, the loop is decorative.

### Falsifiability

* **F1 — the bad spec must fail.** Strengthen `_assert_scorer_is_falsifiable` and add real
  tests: the spec below must score **< 75** and `passed is False`.

  ```python
  DerivedAgentSpec(
      intent="Update Case (Status)",
      confidence=0.7,
      objects_touched=["Case"],
      entities=[DerivedEntity("status", "Case", "Status",
                              [SpecEvidence("data-delta", "x")])],
      orchestration_steps=["Resolve the Case", "Resolve the Case"],   # duplicated
      guardrails=["Validate input", "Validate input"],                # duplicated, generic
      failure_handling=["No failures were observed in this run, "
                        "so error paths are UNTESTED."],              # explicitly untested
      unknowns=[],
      evidence=[],
  )
  ```

  It currently scores 100/100. That is the regression to kill.

* **F2 — untested != observed.** `_score_testability` must award **0** for the failure-path
  half when `failure_handling` contains only the builder's negative sentinel, and full marks
  only when a genuinely observed failure is present. Do not match the bare substring
  `"observed"`. Use the stable contract `spec_builder._derive_failure_handling` actually
  emits: observed entries start `"Observed <layer> failure during recording:"`; the negative
  sentinel contains `"error paths are UNTESTED"`. This is the same lesson as round 1's
  `eval_spec` fix — match the stable emitted fragment, not an incidental word.

* **F3 — duplicates are not completeness.** `_score_completeness` must count *distinct*
  non-trivial orchestration steps, so two identical steps do not read as a two-step process.
  This is what makes D3's dedup score-neutral instead of a 3-point regression.

* **F4 — the dead guard must fire.** After fixing D6, add a test that the unknowns-deletion
  warning actually appears in `SpecVersion.notes` when a later round has fewer unknowns than
  its parent at a higher score. A safety mechanism that cannot trigger is worse than none,
  because it reads as covered.

## Hard prohibitions (unchanged from round 1)

* **Never relax a gate.** `PASS_THRESHOLD` stays 75. `scripts/score_run.py` thresholds are
  untouchable. Making the gate weaker is a defect, not a fix. Every change in this round must
  make the gate strictly *more* able to fail.
* **`_apply_offline_improvements` must never invent evidence.** No new entities, objects,
  topics, or failure scenarios. It may only tighten prose, dedupe, normalise names, reorder,
  and make existing guardrails name objects/fields already present in the spec.
* **Never shrink `unknowns` and never raise `confidence`** in the refinement path.
* **Pure and deterministic.** No clocks, no randomness, no network, no org, no LLM in the
  scorer or the offline loop. The stopping condition depends on determinism.
* **Never mutate the input spec.** Return new objects.
* **Do not weaken a test to make it pass.** If a fixture is wrong, fix the fixture and add a
  test proving the new behaviour fails in the right cases — in *both* directions.

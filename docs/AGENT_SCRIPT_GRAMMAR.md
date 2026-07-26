# Agent Script grammar — what is actually known

Every rule below was established by sending a candidate `.agent` file to
Salesforce's own compilation API and recording the verdict. Nothing here is
inferred from a blog post, and nothing is a guess dressed as a fact.

**How each claim was measured**

```
sf agent validate authoring-bundle --api-name <probe> -o AFT3 --json
```

- CLI `@salesforce/cli 2.143.6`; `@salesforce/plugin-agent` **1.44.4** (`agent 1.44.4 (core)`);
  `@salesforce/agents` **1.10.2**
- Compiler endpoint `POST https://api.salesforce.com/einstein/ai-agent/v1.1/authoring/scripts`,
  `afScriptVersion: "2.0.0"`
- Org `AFT3` (Developer Edition, `IsSandbox=false`), 2026-07-26

**Validation needs no deploy.** The command reads the `.agent` file from the local
SFDX project and POSTs its *contents*; the org supplies auth only. All probes
below were validated without deploying anything. (Established by lane 01 and
independently reproduced here.)

**There are two `sf` installs on this machine and only one of them runs.** An
earlier draft of this file cited `@salesforce/agents` **1.6.6** /
`@salesforce/plugin-agent` **1.40.5** and called `agent_script.py`'s citation of
1.10.2 wrong. That was backwards. `/usr/local/lib/sf/bin/sf` is a launcher, not
the CLI: it sets `CLIENT_HOME=$XDG_DATA_HOME/sf/client` and execs
`$CLIENT_HOME/bin/sf`, which resolves through `current ->  2.143.6-4b4ce38`.

| Tree | CLI | `@salesforce/agents` | `plugin-agent` | Runs? |
|---|---|---|---|---|
| `~/.local/share/sf/client/2.143.6-4b4ce38` | 2.143.6 | **1.10.2** | **1.44.4** | **yes** |
| `/usr/local/lib/sf` | 2.137.7 | 1.6.6 | 1.40.5 | no — shadowed |

Corroborated by `sf version --json` → `@salesforce/cli/2.143.6` and
`sf plugins --core` → `agent 1.44.4 (core)`. So `agent_script.py`'s 1.10.2
citation is **correct**, and every probe below was answered by 1.10.2 / 1.44.4.

---

## 1. The assumption table

Each row is one of the brief's open questions. "Source" is the specific probe or
artifact; "our status" is where this repo stands **after** this lane and lane 01.

| # | Assumption in the code | Real rule | Source | Our status |
|---|---|---|---|---|
| 1 | `naming.MAX_NAME_LENGTH = 74`, budgeting a 6-char `go_to_` prefix inside an assumed 80-char cap | Cap is **80 inclusive on the subagent name itself**. 81 fails. The `go_to_` prefix is **not** inside the budget — a 100-char router action compiles | probes `len74/75/80/81/100/120/255`; verbatim error `Too big: expected string to have <=80 characters` | **Assumption's *reasoning* was wrong**; the value 74 is kept deliberately (see §6) |
| 2 | `subagent <snake_case>:` declaration form | Correct, and `topic <name>:` / `@topic.` also compile | probe `B`; first-party template comment: "supports both `topic` and `subagent` … for backward compatibility" | **Correct** |
| 3 | `system:` is the required first line | **Not required at all.** A file starting at `config:` compiles; so does `config:` before `system:`. `config:` IS required (`Missing config block`) | probes `E`, `F`, `6e` | **Fixed this lane** — the `system:` check was removed as a false positive |
| 4 | `.bundle-meta.xml` carries only `apiVersion` | It carries **`bundleType`** (no `apiVersion` anywhere). Org-authored bundles add `<target>Name.v1</target>` | first-party `scriptAgent.js:141-144`; retrieved `Local_Info_Agent.bundle-meta.xml` | **Docstring wording is wrong, emitted bytes are right** |
| 5 | No `@apex.*` / `@flow.*` may be referenced (safety choice) | **`@apex.*` and `@flow.*` are not valid syntax at all** — the safety choice happens to coincide with the grammar. Apex/Flow are reached a completely different way (§3) | probes `act_apexbare`, `act_apexdot`, `act_flowbare` | **Correct outcome, wrong stated reason** |
| 6 | A bundle can be valid with no action at all | Yes. A subagent with only `instructions:` and no `actions:` block compiles; so does one with no derived subagent beyond the standard three | probes `D`, `G` | **Correct** |
| 7 | Router/subagent naming dialects in `naming.py` | `go_to_<snake>` ↔ `subagent <snake>` linkage is required: a dangling `@subagent.X` is a hard error | probe `H`: `'does_not_exist' is not defined in subagent` | **Correct** |
| 8 | Duplicate `subagent` blocks are fatal corruption | **The compiler accepts them.** Two `subagent escalation:` blocks compiled with exit 0 | probe `J` | **Our check is stricter than the compiler** (defensible, but it is a house rule, not grammar) |
| 9 | Orphaned subagents (defined, unreferenced) are errors | **The compiler accepts them** | probe `I` | **House rule, not grammar** |
| 10 | Indentation must be a multiple of 4 | **False.** Block-scalar continuation lines legitimately sit at 14 and 10 spaces in Salesforce's own output | first-party generator output lines 27, 54, 66-79 | **Was a false positive — fixed this lane** |
| 11 | `config: developer_name` is a free-text field the caller supplies | It must match **`/^[A-Za-z](_?[A-Za-z0-9])*$/`** and be ≤80 chars. `"Update Case Status"` cannot compile | probes `dn_*` (9 cases); verbatim `Invalid string: must match pattern /^[A-Za-z](_?[A-Za-z0-9])*$/ for config` | **Was unchecked — fixed this lane** (§5a) |
| 12 | `config:` is optional like `system:` | **`config:` IS required** — `Missing config block`. Within it, `developer_name` and `description` are required; `agent_label` and `default_agent_user` are not | probe `NoConfig` | **Correct after fix** |
| 13 | A blank line inside a block scalar creates a paragraph break | **It cannot be expressed at all** — the compiler drops empty instruction lines however they are spelled | probes `Blank{a..e}`, compared via `compiledArtifact` (§7) | **No emitter change needed** |

---

## 2. Confirmed by the compiler

Reproduced here, but **first established by lane 01** — cited, not re-measured:

1. `->` is legal only as a key's value (`instructions: ->`); a bare `->` is
   `Syntax error: unexpected `->``.
2. `|` continuation lines must indent strictly deeper than the key owning the `->`.
3. Subagent name cap is **80 inclusive**.
4. Router action names are **not length-checked**.
5. Both `subagent`/`@subagent.` and `topic`/`@topic.` compile.
6. `[NEEDS EVIDENCE: …]` markers **compile successfully** — the compiler is not a
   safety net for evidence quality.
7. `.agent` round-trips byte-identically through deploy → retrieve.

---

## 3. The action grammar — this lane's main finding

Lane 01 could not test this because the emitter never emits Apex or Flow
references. This is the gap, now closed.

### 3.1 `@apex.*` and `@flow.*` do not exist

```
$ # target: @apex.SFVB_TEST_NoSuchApexClass
CompilationError: Cannot invoke '@apex.SFVB_TEST_NoSuchApexClass' — 'apex' is not a valid invocation target.

$ # target: @flow.SFVB_TEST_NoSuchFlow
CompilationError: Cannot invoke '@flow.SFVB_TEST_NoSuchFlow' — 'flow' is not a valid invocation target.

$ # target: @apex.SFVB_TEST_Cls.doThing
CompilationError: '@apex' is not a recognized namespace
```

The emitter's refusal to fabricate `@apex.Foo` was justified as a *safety* choice.
It turns out to be a *syntax* requirement too: such a reference could never have
compiled.

### 3.2 Two error shapes distinguish real namespaces from fictional ones

This is the useful diagnostic. The compiler says something different depending on
whether the namespace exists:

| Probe | Compiler output | Reading |
|---|---|---|
| `@apex.X` | `'apex' is not a valid invocation target` | namespace does not exist |
| `@flow.X` | `'flow' is not a valid invocation target` | namespace does not exist |
| `@prompt.X` | `'prompt' is not a valid invocation target` | namespace does not exist |
| `@standard.X` | `'standard' is not a valid invocation target` | namespace does not exist |
| `@action.X` (singular) | `'action' is not a valid invocation target` | namespace does not exist |
| `@agent_action.X` | `'agent_action' is not a valid invocation target` | namespace does not exist |
| `@record.Case.query` | `'@record' is not a recognized namespace` | namespace does not exist |
| `@actions.X` (plural) | `'X' is not defined in actions` | **namespace is real**, member missing |
| `@topic.X` | `'X' is not defined in topic` | **namespace is real**, member missing |
| `@utils.no_such_util` | `'no_such_util' is not defined in utils` | **namespace is real**, member closed set |
| `@variables.undeclared` | `'undeclared' is not defined in variables` | **namespace is real**, member missing |

So the real invocation namespaces are `actions`, `utils`, `subagent`, `topic`,
`variables`, `outputs`. `@utils.` accepts exactly `transition` and `escalate`.

### 3.3 How Apex and Flow are ACTUALLY invoked

Ground truth is the org-authored `Local_Info_Agent` bundle retrieved from AFT3
(`sf project retrieve start -m AiAuthoringBundle:Local_Info_Agent_1`) — a real,
org-accepted artifact this project did not write. It declares a
**subagent-level `actions:` block** whose `target:` is a URI, then references it
as `@actions.<name>`:

```
subagent local_weather:
    reasoning:
        instructions: ->
            | … run the action {!@actions.check_weather} and summarize the results.
        actions:
            check_weather: @actions.check_weather
                with dateToCheck = ...

    actions:                                   # <- declaration, subagent level
        check_weather:
            description: "Fetch the weather forecast for Coral Cloud Resort."
            label: "Check Weather"
            target: "apex://CheckWeather"      # <- Apex reached HERE, not @apex.
            include_in_progress_indicator: True
            progress_indicator_message: "Checking local weather..."
            inputs:
                dateToCheck: object
                    complex_data_type_name: "lightning__dateType"
                    is_required: True
            outputs:
                maxTemperature: number
                    is_displayable: True
                    filter_from_agent: False
```

Flow is identical with `target: "flow://Get_Resort_Hours"`. Verified by
reconstructing this shape from scratch as probe `tgt_apex_full` → **exit 0**.

Note two further constructs visible in that real bundle, both compiler-confirmed:
`with <input> = ...` binds action inputs, and
`set @variables.X = @outputs.Y` writes an action output back to a variable.
Conditionals on variables also compile (probe `ns_var_cond`):

```
            if @variables.reservation_required:
                | The facility REQUIRES a reservation.
            else:
                | No reservation needed.
```

### 3.4 The 24 supported target schemes — verbatim

Handing the compiler a bogus scheme makes it enumerate everything it accepts.
This is the single highest-value byte sequence in this document:

```
$ # target: "banana://SFVB_TEST_Nope"
CompilationError: Action 'do_it' uses unsupported target scheme "banana://".
Supported schemes: api, apex, apexRest, auraEnabled, cdpMlPrediction,
createCatalogItemRequest, decisionTableAction, executeIntegrationProcedure,
expressionSet, externalConnector, externalService, flow, generatePromptResponse,
integrationProcedureAction, mcpTool, namedQuery, placeholder, prompt,
quickAction, retriever, runExpressionSet, serviceCatalog, slack,
standardInvocableAction.
```

A target with no scheme is rejected differently:

```
$ # target: "SFVB_TEST_NoScheme"
CompilationError: Action 'do_it' has an invalid target "SFVB_TEST_NoScheme".
Expected a URI with a supported scheme: api, apex, apexRest, …
```

Scheme matching is **case-sensitive** — the list says `apexRest`, not `apexrest`.

### 3.5 The compiler does NOT verify that the target exists

`target: "apex://SFVB_TEST_NoSuchApexClass"` and
`target: "flow://SFVB_TEST_NoSuchFlow"` both compile with **exit 0**, and

```
$ sf data query -o AFT3 -q "SELECT Name FROM ApexClass WHERE Name LIKE 'SFVB_TEST%'"
totalSize: 0
```

confirms no such class exists. **Compilation validates the URI shape, not
referential integrity.** A bundle referencing a nonexistent Apex class passes
validation and would fail only at publish or run time. This is a real limit on
how much assurance `sf agent validate` can give.

Related: an action declared but never referenced compiles (`tgt_unreferenced`),
but referencing an action with no declaration does not:

```
$ # reasoning references @actions.do_it, no declaration block
CompilationError: 'do_it' is not defined in actions
```

---

## 4. Byte-level comparison: Salesforce's generator vs ours

Generated with
`sf agent generate authoring-bundle --no-spec --name "SFVB TEST Grammar Probe" -o AFT3`
and compared against `build_agent_script()` for an equivalent spec.

**Identical** — `system:`, `config:` (all four keys, same order), `variables:`
(all five, including the anomalous 10-space indent on `VerifiedCustomerId`'s
description), `language:`, `start_agent agent_router:`, and the three standard
subagents' text.

**Differences:**

| Aspect | First-party | Ours | Verdict |
|---|---|---|---|
| Multi-line instruction style | `\| first line` then **continuation text** indented +2 with no pipe | one `\|` per line, all at the same indent | **Both compile.** Ours is arguably more robust — it cannot accidentally merge lines |
| `.bundle-meta.xml` indent | 2 spaces | 2 spaces | identical (org *returns* 4; cosmetic) |
| `.bundle-meta.xml` trailing newline | **absent** | present | both accepted |
| `.bundle-meta.xml` `<target>` | absent when generated; org adds `<target>Name.v1</target>` | absent | fine for validate + deploy |
| Trailing blank lines | 2 | 1 | irrelevant |
| Derived subagent instructions | placeholder prose | real observed steps | ours is the point of the project |

Sizes: first-party `.agent` 5641 bytes / 104 lines; `.bundle-meta.xml` 160 bytes,
no trailing newline.

---

## 5. What `validate_locally()` can and cannot do

Fixed this lane:

- **Now catches** invalid invocation namespaces (`@apex.*`, `@flow.*`,
  `@prompt.*`, …) and bad `target:` schemes — the class it was completely blind
  to. Measured: it previously returned `[]` for a file the compiler rejected.
- **No longer false-positives** on block-scalar continuation indentation, which
  had flagged 8 lines of Salesforce's own valid output.

Cross-checked against 26 probes with recorded compiler verdicts: **24 agree, 0
false positives on valid files.** The 2 it does not model are cross-reference
resolution (`'X' is not defined in actions`/`topic`), which needs a symbol table
rather than a lint pass.

**It is still not a compiler.** A clean result does not mean the bundle compiles.
Stricter than the grammar, deliberately: duplicate subagent blocks and orphaned
subagents are accepted by Salesforce but reported here as house rules.

A missing `system:` block **is no longer reported** — that check was a false
positive and has been removed, along with the test that asserted it (retargeted
to `config:`, the block the compiler actually requires).

---

## 5a. The `config:` block — measured field by field

`developer_name` is the only `config:` value this project takes verbatim from a
caller, and it is the most constrained field in the file.

| Field | Rule | Verbatim compiler error when violated |
|---|---|---|
| `developer_name` | **Required.** Must match `/^[A-Za-z](_?[A-Za-z0-9])*$/`, max **80** chars | `Invalid string: must match pattern /^[A-Za-z](_?[A-Za-z0-9])*$/ for config` · `Too big: expected string to have <=80 characters for config` |
| `description` | **Required.** No length cap found (1000 chars compiled); empty string is accepted | `Missing required field 'description'` |
| `agent_label` | **Optional.** Free text; 255 chars and empty both compiled | — |
| `default_agent_user` | **Optional.** Free text; an email address, arbitrary text, and empty all compiled | — |

Rejected `developer_name` values, each measured: `"Case Updater"` (space),
`"case-updater"` (hyphen), `"9lives"` (leading digit), `"_leading"` (leading
underscore), `"Trailing_"` (trailing underscore), `"Double__Underscore"`
(consecutive underscores), `"é_accent"` (non-ASCII). Accepted: `"Valid_Name_1"`,
`"lower_ok"`, `"A"`, `"a1"`, `"a_1_b_2"`, 80×`"a"`.

**This pattern is stricter than the subagent-name pattern.** A subagent name may
contain one run of two underscores —
`/^[A-Za-z](_?[A-Za-z0-9])*(__(_?[A-Za-z0-9])*)?$/`, and `subagent
double__underscore:` compiled — but `developer_name: "Double__Underscore"` was
rejected. The two rules must not be unified.

**Why this mattered.** `build_agent_script(developer_name=...)` writes the value
straight into `config:`. A caller passing a human-readable process name — exactly
what this project derives from a recording — produced a bundle that could never
compile, and `validate_locally` reported **zero findings** on it.
`check_config_block` now catches it. Nine cases were re-run end-to-end against the
real compiler — the six rejections above plus `"Case_Triage_Agent"`, `"lower_ok"`
and `"A"` — and the local verdict agrees with the compiler on **9/9**.

### A false positive on Salesforce's own artifact

`check_action_grammar` reported
`cannot invoke '@testdrive.orgab948baa' — 'testdrive' is not a valid invocation target`
on the **org-authored** `Local_Info_Agent` bundle retrieved from AFT3, because
that bundle's `default_agent_user` is an agent-user address of the form
`afdx-agent@testdrive.org<id>-<uuid>` and the `@` was read as an invocation.
Re-validating that exact value through the compilation API returns **exit 0**. The
invocation regex now requires the `@` not to follow a word character. After the
fix, the retrieved org bundle passes `validate_locally` with **0 findings**.

---

## 6. Requests for other lanes (I do not own these files)

**To lane 07 (`naming.py`):** `MAX_NAME_LENGTH = 74` should stay 74, but its
justification is now known to be wrong. The comment says 74 "budgets for the
`go_to_` prefix inside an assumed 80-char cap". Measured: the cap applies to the
subagent name alone and router actions are unchecked, so the prefix needs no
budget. 74 is still the right *value* because `topic_api_name` also feeds the spec
YAML / `expectedTopic` channel, whose limit **nobody has measured**. Suggested
wording: keep 74 as a conservative cap for the *unmeasured metadata channel*, not
as prefix arithmetic. Also, `validate_locally`'s router-action 80-char check is
now known to be a non-rule — it enforces something the compiler does not.

**To lane 10 (docs):** two corrections. (a) `agent_script.py`'s module docstring
says the only authoritative reference is `agentScriptTemplate.js`; the compiler
API is strictly more authoritative and the template omits the entire action
grammar in §3. (b) Its `CONSTRAINT` paragraph frames avoiding `@apex.Foo` as
purely a safety choice — it is also a syntax error, which is a stronger argument.
Its `@salesforce/agents` 1.10.2 citation needs no change: it is the version that
actually runs (see the two-installs table at the top of this file).

---

## 6a. Exit 0 is not the whole verdict — read `compiledArtifact`

Every other section of this file uses `sf agent validate`'s exit code. That is
enough for grammar, but it cannot see *semantic* differences between two files
the compiler both accepts. To measure those, drive the first-party
`ScriptAgent.compile()` directly and inspect the `compiledArtifact` it returns —
the CLI discards it and reports only `{"success": true}`:

```js
const { ScriptAgent } = await import('~/.local/share/sf/client/current/node_modules/@salesforce/agents/lib/index.js')
const agent = new ScriptAgent({ connection, project, aabName })
const resp  = await agent.compile()          // resp.compiledArtifact
```

The artifact turns each `|` line into a separate state-update that appends to
`AgentScriptInternal_agent_instructions`.

**Finding: a paragraph break inside a block scalar cannot be expressed at all.**
For `| AAA` / *separator* / `| BBB`, every spelling produced the **same two**
appends, `"\nAAA"` then `"\nBBB"` — all with exit 0:

| Separator spelling | Compiled appends |
|---|---|
| `\|` (bare pipe) | `"\nAAA"`, `"\nBBB"` |
| `\| ` (pipe + space) | `"\nAAA"`, `"\nBBB"` |
| a truly empty line (no pipe) | `"\nAAA"`, `"\nBBB"` |
| two consecutive empty lines | `"\nAAA"`, `"\nBBB"` |

Only a line with real content survives — a zero-width space produced a third
append, which is a hack rather than a fix. Confirmed identically on
`@salesforce/agents` 1.10.2 (the version that runs) and 1.6.6.

**Consequence for our emitter: none.** `_block_scalar` keeping `|` on an empty
line is correct — it costs nothing and matches the surrounding dialect. An
in-progress change to make empty lines pipe-free was **reverted** when this
measurement disproved its premise; `_block_scalar` is lane 01's function and is
now byte-identical to what lane 01 merged. Anything in this repo that relies on
blank lines to separate prose paragraphs in an instruction block is relying on
something the compiler discards.

---

## 7. Explicitly NOT verified

Being precise about the edges, because an unverified claim here would poison the
rest:

- **Whether any of this runs.** Compilation is syntax + reference resolution. No
  agent was published from these probes and no conversation was held. A bundle
  can compile and behave wrongly.
- **`apex://` / `flow://` against real, existing targets.** Every Apex/Flow probe
  used a deliberately nonexistent name to isolate the grammar. The input/output
  contract a real Apex class must satisfy (`@InvocableMethod` shape, supported
  parameter types) is **unverified** — the compiler never checked it.
- **21 of the 24 schemes.** Only `apex`, `flow`, `prompt` and `mcpTool` were
  probed. The other 20 come from the compiler's own error text, which is strong
  evidence they are accepted, but their *syntax* was not exercised.
- **The metadata-channel name limit.** 80 was measured on the *compiler* channel.
  The spec-YAML / `expectedTopic` path reaches Salesforce through
  `sf agent generate` and publish, which were not probed. This is exactly why
  `MAX_NAME_LENGTH` stays 74.
- **`with <input> = ...` and `set @variables.X = @outputs.Y` semantics.** Both
  appear in the org-authored bundle and compile when reproduced, but the literal
  `...` placeholder was copied as-is; real binding expressions were not explored.
- **Full `inputs:`/`outputs:` type system.** `string`, `number`, `boolean` and
  `object` (with `complex_data_type_name`) are attested in the retrieved bundle.
  The complete type list and which attributes are required are unknown.
- **Comment syntax.** Still unverified; no probe attempted it.
- **`system:` optionality across versions.** Measured as optional at
  `afScriptVersion` 2.0.0 on one org, one day. It would be unwise to rely on.
- **Whether the dropped blank line changes agent behaviour.** §6a proves the
  compiler discards it. Whether the resulting run-together prose measurably
  degrades the agent's responses is a Stage-5 question nobody has run.
- **Everything in §6a on any version other than 1.10.2 and 1.6.6.** The
  `compiledArtifact` shape is `@beta` in the first-party library and may change.

---

## Reproducing this

Probe scripts are in `.lane-tmp/` (untracked): `probe.sh` writes a candidate into
a throwaway SFDX project and validates it; `mkcases.py`, `mkactions.py`,
`mktargets.py`, `mklen.py` generate the candidates; `dump_instr.mjs` drives
`ScriptAgent.compile()` and prints the `compiledArtifact` instruction appends
(§6a). None contain tokens or frontdoor URLs. Every probe was validate-only and
left **no** org artifact.

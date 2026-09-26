# Implementation Progress

Last updated: 2026-09-26 (M4 complete and verified; not committed)

## Current milestone

**M4 — Application/agent control layer: DONE, verified.**
M5 has not started and waits for explicit go-ahead.

## Current repository state

- Branch `main`.
  - `34d0896` — M1.
  - `d2c6e1a` — M2 + M3 checkpoint, committed at the start of M4 at the user's request
    (234 tests passing, credential scan clean).
  - M4 is uncommitted in the working tree on top of `d2c6e1a`.
- M4 changes (`git status` against `d2c6e1a`):
  - new: `src/harness/orchestrator/{__init__,state,protocol,plan,observe,executor,interpreter,orchestrator,report}.py`,
    `src/harness/model/factory.py`, `tests/orchestration_helpers.py`, `tests/test_{run_state,planner,
    action_protocol,orchestrator,interpreter,model_factory}.py`
  - modified: `src/harness/cli.py` (run → model factory → orchestrator), `src/harness/config.py`
    (`max_model_calls`, `max_tool_calls`), `tests/test_cli.py`, `tests/test_config.py`, `arch.md`,
    `README.md`, `.env.example`, `REQUIREMENTS.md`, `PROGRESS.md`
  - **No M2 or M3 source file changed** (`git diff` over `tools/ repo/ context/ metrics.py
    model/client.py model/types.py` is empty).
- **Not built:** live provider adapter, verification engine, evidence ledger, failure
  classification/repair/replan, compaction, run directory/report files.

### M1 tests intentionally changed

Nine M1 CLI tests asserted that accepted input exits 0 with "Harness skeleton ready". In M4,
`harness run` continues to model execution. With no adapter it now prints the same accepted-input
report, then `error: No model provider is configured …` / `… not supported by this build …`, and
exits 2. The tests now assert that stricter behavior (input accepted and reported, precise provider
error, "No model was called and the repository was not modified"). Their input-handling coverage
is unchanged.

## Gap analysis (after M4)

| Req | Item | Status |
|---|---|---|
| R1 | Accept task; inspect repository; determine relevant files | IMPLEMENTED AND VERIFIED |
| R1 | Understand task; modify implementation | PARTIAL (scripted model only) |
| R1 | Verify modifications | MISSING |
| R2 | All four items | IMPLEMENTED AND VERIFIED |
| R3 | Lifecycle; plan; execute | IMPLEMENTED AND VERIFIED |
| R3 | Finish/terminate | PARTIAL (clean termination; no verified finish) |
| R3 | Verify; repair | MISSING |
| R4 | Working vs permanent; snippets selected | IMPLEMENTED AND VERIFIED |
| R4 | Repeated context avoided | PARTIAL |
| R4 | Compaction | MISSING |
| R5 | All six items | IMPLEMENTED AND VERIFIED (driven end to end by the executor) |
| R6 | Failed test detected; infinite loops prevented | PARTIAL (recorded / budget-bounded only) |
| R6 | Classification; repair/replan; retry limit | MISSING |
| R7 | All | MISSING |
| R8 | Model calls, tool calls tracked; context usage controlled | IMPLEMENTED AND VERIFIED |
| R8 | Expensive operations avoided | PARTIAL |
| R8 | Targeted tests before full suite | MISSING |
| R9 | `make run` | PARTIAL (no live adapter); rest VERIFIED |
| R10 | All | IMPLEMENTED AND VERIFIED |
| R11 | Provider abstraction | VERIFIED; text-only / configurable PARTIAL |
| R12 | Dependencies declared | VERIFIED; others PARTIAL |

## Missing P0 components

1. ~~Scaffolding, config, CLI~~ (M1) · ~~model interface, tools~~ (M2) · ~~repository intelligence,
   working set, context manager~~ (M3) · ~~RunState, planner, executor, orchestrator~~ (M4)
2. Verification engine: baseline tests, layered verification, regression check, diff review,
   acceptance-criteria mapping (R7)
3. Evidence ledger (R7)
4. Failure classification, repair/replan loop, `max_repair_cycles`, no-progress detection (R6)
5. Compaction of old observations; "unchanged since" re-read marker (R4)
6. Run directory / report files (R8, arch §19)
7. Live provider adapter, once announced (R11, R9 `make run`)

## Dependency ordering

```text
M4 control layer (done) ─▶ M5 verification + ledger ─▶ M6 repair/replan loop ─▶ M7 report, compaction,
                                                                                 live adapter, hardening
```

## Next 3 implementation milestones

### M5 — Verification engine and evidence ledger (R7, R1 verify)

- READY_FOR_VERIFICATION → VERIFYING → VERIFIED | NEEDS_REPAIR (add the transitions)
- Baseline test run before EXECUTE; targeted then broader tests from `plan.verification_candidates`
  or discovered commands; regression check against baseline; `git_diff`/`git_diff_stat` review;
  acceptance-criteria → evidence mapping; explicit INCONCLUSIVE when no command exists
- Done when: scripted runs reach VERIFIED only with passing evidence after the last patch, and
  NEEDS_REPAIR on a failing fix.

### M6 — Failure recovery (R6, R3 repair)

- Failure classes and signatures, repair and replan paths, `max_repair_cycles`, no-progress detection
- Done when: fail → repair → pass and stop-at-limit E2E tests pass.

### M7 — Run report, compaction, CLI, submission hardening (R4, R8, R9, R12)

- Run directory (`events.jsonl`, `state.json`, `report.md`), deterministic compaction, live
  adapter once announced, clean-environment rehearsal.

## Completed

- M1 (`34d0896`), M2 + M3 (`d2c6e1a`), M4 (2026-09-26, uncommitted).

## Currently implementing

Nothing. Awaiting go-ahead for M5.

## Verified — M4 evidence

macOS (Darwin 25.6.0), project root. Scripts: `<scratchpad>/verify_m4.sh` (build/test matrix)
and `<scratchpad>/trace_m4.py` (ScriptedModel trace).

| # | DoD item | Evidence | Result |
|---|---|---|---|
| 1 | Previous tests pass | all 234 pre-M4 tests pass (9 M1 CLI assertions updated to M4 behavior, above) | OK |
| 2–3 | Typed RunState, enforced phases | `tests.test_run_state` (9): happy path, invalid moves, VERIFIED/NEEDS_REPAIR unreachable, terminals final | OK |
| 4, 10 | Discovery wired, shared ToolContext | `test_discovery_runs_before_first_model_call_and_shares_the_tool_context`: order `[discover, plan]`, discovery ctx `is` registry ctx, `ctx.metrics is state.metrics` | OK |
| 5 | Planner uses bounded WorkingSet | `test_planner_uses_the_m3_working_set`; `test_planner_receives_bounded_working_set_not_the_repository` (3,000-char limit, unrelated file content absent) | OK |
| 6 | Structured plan | `tests.test_planner` (6; 13 rejection cases) | OK |
| 7 | Native + text → same ToolCall path | `tests.test_action_protocol` (7); `test_native_and_text_tool_calls_both_dispatch` | OK |
| 8–9 | One action per step; results update state | `test_one_action_per_model_call`, `test_each_observation_feeds_the_next_request` | OK |
| 11–12 | Budgets and timeout from config | `BudgetTest` (7), `test_command_timeout_comes_from_configuration` | OK |
| 13 | Authoritative counts | `test_counts_come_from_shared_metrics`; `RunState.model_calls/tool_calls` read `ExecutionMetrics` | OK |
| 14–15 | Patch invalidation, modified files | `test_read_then_patch_then_complete`, `test_stale_working_set_evidence_is_removed_after_patch` | OK |
| 16 | Command failure semantics | `test_failing_tests_are_a_successful_tool_call_with_a_failed_command` | OK |
| 17 | Missing commands not invented | `test_missing_commands_are_stated_not_invented`, `test_invented_verification_command_is_rejected` | OK |
| 18 | Portable interpreter | `tests.test_interpreter` (8); `test_planner_sees_resolved_interpreter_not_bare_python` | OK |
| 19 | complete → READY_FOR_VERIFICATION only | integration tests; transition table; CLI says "NOT been verified" | OK |
| 20 | Malformed output ends cleanly | `MalformedOutputTest` (9) | OK |
| 21–22 | Offline ScriptedModel E2E, no key | `OrchestratorCase` removes `AI_API_KEY`; trace below | OK |
| 23 | No live provider invented | `test_no_adapter_is_shipped`; `make run` → precise provider error | OK |
| 24 | Offline setup | matrix below | OK |

**Build/test matrix** (`verify_m4.sh`): working tree `make clean && make setup && make test` → 0/0/0,
`Ran 303 tests … OK`; fresh copy Python 3.10.19 → 0/0 (303 OK); fresh copy offline
(`PIP_NO_INDEX=1`, Python 3.12) → 0/0 (303 OK, 1 offline warning); fresh copy without ripgrep → 0/0
(303, 2 skipped).

**Test count: 303** (234 before M4 + 69 new: run_state 9, planner 6, action_protocol 7,
orchestrator 31, interpreter 8, model_factory 4, cli +3, config +1). Suite ≈ 16 s.

### ScriptedModel trace (`trace_m4.py`, 204-file repository, fake key present in the environment)

```text
order:        discover_for_task -> model:plan -> model:step1 -> model:step2 -> model:step3 -> model:step4
transitions:  DISCOVER -> PLAN -> EXECUTE -> READY_FOR_VERIFICATION
repository:   204 files, 6,606 bytes; planner message 1,501 chars; working set 1,063 chars;
              discovery read 1 candidate file; filler content in planner input: False
step 1: read_file   success=True outcome=ok          stale=True  paths=[src/math_utils.py]
step 2: apply_patch success=True outcome=ok          stale=False paths=[src/math_utils.py]
step 3: run_tests   success=True outcome=command_ok  exit=0
executor request sizes: [5336, 5539, 5601, 6218]
math_utils evidence in working context after patch: False
final: READY_FOR_VERIFICATION; steps=4 model_calls=5 tool_calls=3 command_calls=1 modified=[src/math_utils.py]
fake key present anywhere in state/requests: False
budget max_steps=3:       BUDGET_EXHAUSTED steps 3/3; model_calls=4 tool_calls=3; 3 scripted responses unused
budget max_model_calls=3: BUDGET_EXHAUSTED model_calls 3/3; steps=2 tool_calls=2
budget max_tool_calls=2:  BUDGET_EXHAUSTED tool_calls 2/2; steps=3 model_calls=4
```

**Manual `make run`** (key set, no provider): prints the accepted input, then
`error: No model provider is configured (AI_MODEL_PROVIDER is not set) …`, "No model was called and the
repository was not modified", exit 2. With `AI_MODEL_PROVIDER=acme`: "Configured model provider is not
supported by this build: 'acme' (supported: none yet)". Fake key in output: 0 occurrences.
`harness inspect` still works with the key unset.

**Credential scan** over tracked + untracked files: no matches.

## Known issues / limitations

- The step budget counts executor iterations only; the planner call counts toward
  `max_model_calls`, not `max_steps`.
- A tool budget of N allows N dispatches. If the model then chooses another tool, the run ends
  `BUDGET_EXHAUSTED` without dispatching it (one model call is spent on that choice by design,
  so the model can still `complete` right after its last allowed tool).
- The profile summary in the repository context still shows M3's unresolved `python …` spelling.
  The planner accepts it as an alias of the resolved command.
- `sys.executable` (the harness interpreter) is the fallback when the repository has no virtualenv.
  It will not have the target repository's dependencies (e.g. pytest). M5 must treat
  "interpreter lacks the test framework" as an environment problem, not a code failure.
- Executor window sizes (`ExecutorSettings`) are internal constants, not user configuration.
- From earlier milestones: the command policy is a guardrail, not a sandbox; `make` echoes
  `Error 2` after the harness's own message.

## Notes for M5

- **Entry state:** `RunState` in `READY_FOR_VERIFICATION`. Add transitions
  `READY_FOR_VERIFICATION → VERIFYING → VERIFIED | NEEDS_REPAIR` in `state.TRANSITIONS`; the enum
  members already exist, so nothing else needs renaming.
- **What to verify with:** `state.plan.verification_candidates` (chosen, resolved), falling back to
  `state.repo_profile.test_commands` passed through `orchestrator.interpreter.resolve_command`. Either
  can be empty; M5 must produce an explicit "no verification command" outcome, never invent one.
- **What changed:** `state.modified_files` (patched files) and `git_diff`/`git_diff_stat` (these include
  untracked new files). A baseline test run should happen before EXECUTE to separate pre-existing failures.
- **What was claimed:** `state.plan.acceptance_criteria` and `state.terminal_reason` (the model's
  complete summary). Neither is evidence.
- **Command outcomes:** `Observation.outcome` distinguishes `command_ok` / `command_failed` /
  `command_timed_out` from `tool_error`. Keep `success` (tool) and `outcome` (command) separate.
- **Counts and budgets:** reuse `state.metrics` and the run's `ToolContext`/registry. Verification tool
  calls count toward `max_tool_calls`, so decide whether verification gets its own budget.
- **Unused so far:** `max_repair_cycles` (config) and `ContextManager.record_fact` (episodic facts;
  a natural home for baseline results).
- **Live model:** add an entry to `model/factory.ADAPTERS` once the provider is announced; nothing
  else depends on the provider.

## Important architectural decisions

- (M4) One `ToolContext` per run (root, limits, metrics) shared by discovery, tools and the metered
  model; config `command_timeout_seconds` flows into its `ToolLimits`.
- (M4) Explicit phase table, with VERIFIED/NEEDS_REPAIR unreachable until M5. Every failure ends in a
  terminal phase with a structured `Failure`, and no exception escapes `Orchestrator.run`.
- (M4) Strict JSON protocols (plan and actions), whole-response only, no repair or retry. Native and
  text tool calls converge on `ToolCall` → `ToolRegistry.dispatch_call`.
- (M4) Budgets checked before the operation that would exceed them; exhaustion never costs an
  extra model call.
- (M4) Executor requests are rebuilt from bounded windows each step (not an accumulating conversation).
- (M4) Tool errors are observations; only a tool crash (`internal_error`) is terminal.
- (M4) The planner may only choose discovered verification commands; Python interpreters are
  resolved as repository venv → `sys.executable` → PATH.
- (M4) `harness run` fails precisely when no adapter exists; `ADAPTERS` is empty by design.
- (M3) Repository intelligence owns scope; bounded working set; `inspect` needs no key.
- (M2) Pure-Python patching, no implicit shell, read-only git, single path boundary.
- (M1) Python ≥ 3.10, stdlib only, `unittest`, `PYTHONPATH=src` in make targets.

## Commands

```sh
export AI_API_KEY="..."
make setup        # idempotent, works offline
make run          # interactive; stops with a precise error until a live adapter exists
make test         # 303 tests, offline, no key needed
make clean
PYTHONPATH=src .venv/bin/python -m harness inspect --repo PATH [--task "TEXT"]
```

## Last successful test

2026-09-26: `make clean && make setup && make test` gave 303 tests OK (Python 3.14.3); also 303 OK on
3.10.19, on 3.12 offline, and without ripgrep (2 skipped).

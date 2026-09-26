# Implementation Progress

Last updated: 2026-09-26 (M5 complete and verified; not committed)

## Current milestone

**M5 — Baseline verification, VerificationEngine, EvidenceLedger, bounded repair: DONE, verified.**
M6 has not started and waits for explicit go-ahead.

## Current repository state

- Branch `main`: `34d0896` M1 · `d2c6e1a` M2+M3 · `a11b23a` M4 (committed at the start of M5 at the
  user's request, after a clean credential scan) · M5 uncommitted on top.
- M5 changes (against `a11b23a`):
  - new: `src/harness/verify/{__init__,commands,outcomes,ledger,engine,recovery}.py`,
    `tests/test_verification.py`, `tests/test_verification_outcomes.py`
  - modified: `orchestrator/{state,executor,orchestrator,report}.py`, `cli.py`, `config.py`,
    `tools/commands.py`, `tools/patch.py`, tests (`orchestration_helpers`, `test_orchestrator`,
    `test_run_state`, `test_cli`, `test_config`, `test_commands`), `arch.md`, `README.md`,
    `.env.example`, `REQUIREMENTS.md`, `PROGRESS.md`
- **Not built:** live provider adapter, replanning, compaction, run directory/report files,
  targeted (per-test) command selection.

### Defects found and fixed during M5

1. **Stale Python bytecode made verification judge old code (M2 command layer).** The baseline test
   run wrote `__pycache__/*.pyc`. The fix patch (`x + 2` → `x + 1`) had the same size and was
   applied within the same second, and Python validates `.pyc` by whole-second mtime + size, so
   the post-change run executed the pre-patch bytecode ("still fails exactly as before").
   Fix: `run_command`/`run_tests` set `PYTHONDONTWRITEBYTECODE=1`, which also stops the harness
   creating `__pycache__` in target repos. Regression test:
   `test_same_size_edit_in_the_same_second_is_not_hidden_by_bytecode_cache`.
2. **`HARNESS_MAX_REPAIR_CYCLES=0` was rejected (M1 config).** It was parsed as a positive integer,
   but M5 needs 0 = "verify, never repair". It is now parsed as a non-negative integer (tested).
3. **Sub-make output was not recognised as an environment error (found while writing M5).** Under
   `make test`, the fixture's own `make test` runs as a sub-make and reports `make[1]: …`. The
   pattern now accepts `make[N]:`. This matters because evaluators launch via `make run`.
4. **Circular import `verify.commands` ↔ `orchestrator`**: resolved with a function-local import.

### M4 tests intentionally changed

M4 treated `READY_FOR_VERIFICATION` as terminal and PLAN → EXECUTE as direct. Since M5 every
completed execution is baselined and verified, so:

- transition lists include BASELINING / VERIFYING / verdicts;
- tool-call totals add the fixture's fixed overhead (6 baseline + 4 per verification round, named
  constants in `test_orchestrator.py`);
- scripts that complete without the fix run with `max_repair_cycles=0`, so they end right
  after verification instead of asking the scripted model for a repair;
- the completion summary is now `state.completion_claims`, not `terminal_reason`;
- "VERIFIED/NEEDS_REPAIR unreachable" became "VERIFIED only through VERIFYING" and "repair
  cannot bypass verification".

Each test keeps its original subject: one action per step, observation feedback, staleness,
budgets checked before dispatch, and malformed output handling.

## Gap analysis (after M5)

| Req | Item | Status |
|---|---|---|
| R1 | Accept, inspect, determine files, **verify modifications** | IMPLEMENTED AND VERIFIED |
| R1 | Understand task; modify implementation | PARTIAL (scripted model only) |
| R2 | All | IMPLEMENTED AND VERIFIED |
| R3 | Lifecycle, plan, execute, **verify, repair, finish/terminate** | IMPLEMENTED AND VERIFIED |
| R4 | Working vs permanent; snippets selected | VERIFIED; repeated context PARTIAL; compaction MISSING |
| R5 | All | IMPLEMENTED AND VERIFIED |
| R6 | **All five items** | IMPLEMENTED AND VERIFIED (repair path; replanning not implemented) |
| R7 | **All five items** | IMPLEMENTED AND VERIFIED (criteria assessment conservative: often UNKNOWN) |
| R8 | Calls tracked; context controlled | VERIFIED; expensive ops PARTIAL; targeted tests MISSING |
| R9 | `make run` | PARTIAL (no live adapter); rest VERIFIED |
| R10 | All | IMPLEMENTED AND VERIFIED |
| R11 | Provider abstraction | VERIFIED; text-only / configurable PARTIAL |
| R12 | Dependencies declared | VERIFIED; clean-env / docs PARTIAL |

## Missing P0 components

1. Live provider adapter, once announced (R11, R9 `make run`)
2. Compaction of old observations; "unchanged since" re-read marker (R4)
3. Targeted test selection before the full suite (R8)
4. Run directory / report files (arch §19)
5. (Quality) replanning and a stop rule for repeated identical repair failures (R6 beyond the checklist)

## Dependency ordering

```text
M5 verification + repair (done) ─▶ M6 context compaction + targeted tests ─▶ M7 run report, live adapter,
                                                                              clean-environment rehearsal
```

## Next 3 implementation milestones

### M6 — Context compaction and targeted verification (R4, R8)

- Deterministic compaction of old observations; "unchanged since step N" markers for re-reads
- Targeted test commands (e.g. `-k`/test-id selection derived from failing ids or changed files),
  run before the full suite; the full suite still decides VERIFIED
- Done when: long runs keep a flat request size with compacted history, and verification runs
  targeted tests first with exact tool accounting.

### M7 — Run report and CLI integration (R8, R9)

- Run directory (`events.jsonl`, `state.json`, `report.md`) with evidence ledger export
- Done when: every scripted scenario leaves a complete, secret-free run directory.

### M8 — Live adapter and submission hardening (R9, R11, R12)

- The organizer-prescribed provider in `model/factory.ADAPTERS`; clean-environment rehearsal of
  `make setup && make run`; Linux check.

## Completed

- M1 `34d0896`, M2+M3 `d2c6e1a`, M4 `a11b23a`, M5 (2026-09-26, uncommitted).

## Currently implementing

Nothing. Awaiting go-ahead for M6.

## Verified — M5 evidence

macOS (Darwin 25.6.0), project root. Scripts: `<scratchpad>/verify_m5.sh` (build/test matrix)
and `<scratchpad>/trace_m5.py` (scenario traces).

**Build/test matrix:** working tree `make clean && make setup && make test` → 0/0/0, `Ran 338 tests OK`;
fresh copy Python 3.10.19 → 0/0 (338 OK); fresh copy offline (`PIP_NO_INDEX=1`, Python 3.12) → 0/0
(338 OK, 1 offline warning); fresh copy without ripgrep → 0/0 (338, 2 skipped).

**Test count: 338** (303 before M5 + 35: test_verification 18, test_verification_outcomes 11,
test_orchestrator +1, test_run_state +2, test_cli +1, test_config +1, test_commands +1).

| # | DoD item | Evidence |
|---|---|---|
| 1 | M1–M4 tests pass | 303 prior tests pass (M4 lifecycle assertions updated as described above) |
| 2–3 | Baseline before editing; initial state captured | `test_baseline_runs_before_the_first_edit`, `test_dirty_repository_is_preserved_and_not_attributed` |
| 4 | Only discovered commands | `select_verification_commands`; `test_no_verification_command_is_unverified`; M4 invented-command rejection |
| 5–6 | Classified outcomes; environment ≠ code | `ClassifyTest` (5), `test_environment_error_blocks_without_repair` |
| 7 | Baseline vs post comparison | `CompareTest` (2; 15 cases) |
| 8–10 | Typed report, ledger, criteria linked or UNKNOWN | `VerificationReport`/`EvidenceLedger`; `LedgerTest` (3); criteria assertions in integration tests |
| 11 | `complete` is never proof | `test_completion_claim_alone_is_not_evidence` |
| 12–13 | First-pass VERIFIED; failure → NEEDS_REPAIR | `test_baseline_fail_then_post_pass_is_verified`, `RepairTest` |
| 14–16 | Same Executor/Registry; concise evidence; fresh content | `test_repair_request_has_fresh_content_and_failure_evidence` |
| 17–18 | Exact repair limit; every repair re-verified | `LimitTest` (0 and 1 cycles), trace (0/1/2), transition table tests |
| 19 | Global budgets include verification/repair | exact totals in `RepairTest`; `test_no_tool_budget_left_for_verification_is_never_verified`; `test_no_model_budget_left_for_repair_but_verification_still_runs` |
| 20 | No repair for environment errors | environment test: 3 model calls, 0 repairs |
| 21 | No-command tasks never VERIFIED | `test_no_verification_command_is_unverified` |
| 22 | Pre-existing vs new failures | `test_unrelated_failure_is_reported_not_blamed` (IMPROVED, risk lists `test_legacy_format`) |
| 23–24 | Git/diff evidence; no destructive rollback | SNAPSHOT/DIFF/FILE_CHANGE items; no `git reset/checkout/clean` in `src/` (grep) |
| 25–27 | Offline scripted repair; no key; no live provider | whole suite runs without `AI_API_KEY`; `ADAPTERS` empty |
| 28 | Offline setup | matrix above |

### Scenario trace (`trace_m5.py`, fake key in the environment)

```text
first-pass            VERIFIED          r1:VERIFIED[V1:FIXED]                               model=4 tool=12 repairs=0
repair                VERIFIED          r1:NEEDS_REPAIR/TASK_TEST_FAILURE | r2:VERIFIED[V1:FIXED]   model=7 tool=19 repairs=1
pre-existing-failure  VERIFIED          r1:VERIFIED[V1:IMPROVED]                            model=3 tool=11
regression            VERIFIED          r1:NEEDS_REPAIR/REGRESSION[REGRESSED] | r2:VERIFIED[UNCHANGED_PASS, weak]   model=5 tool=17 repairs=1
environment-error     BLOCKED           r1:BLOCKED/ENVIRONMENT_ERROR[V1:ENVIRONMENT]        model=3 tool=11 repairs=0
no-verification       UNVERIFIED        r1:UNVERIFIED/NO_VERIFICATION_EVIDENCE              model=3 tool=7
max_repair_cycles=0   BUDGET_EXHAUSTED  repair_cycles 0/0; 1 round;  model=3; 4 scripted responses unused
max_repair_cycles=1   BUDGET_EXHAUSTED  repair_cycles 1/1; 2 rounds; model=5; 2 unused
max_repair_cycles=2   BUDGET_EXHAUSTED  repair_cycles 2/2; 3 rounds; model=7; 0 unused
model budget 3        BUDGET_EXHAUSTED  model_calls 3/3 in repair; verification round still ran
tool budget 8         BUDGET_EXHAUSTED  tool_calls 8/8 in verification; no report, never VERIFIED
fake key found anywhere (state, report, requests, evidence): False
```

Repair-run ledger (causal history): E1 initial snapshot CLEAN · E2 baseline TEST_FAILURE · E3
after-baseline snapshot CLEAN · E4 post-1 TEST_FAILURE · E5 snapshot · E6 FILE_CHANGE (execute) · E7 DIFF ·
E8 criterion FAIL (refs E4) · E9 post-2 PASS · E10 snapshot · E11 FILE_CHANGE (execute, repair-1; 2 patches)
· E12 DIFF · E13 criterion PASS (refs E2, E9). Transitions: DISCOVER → PLAN → BASELINING → EXECUTE →
READY_FOR_VERIFICATION → VERIFYING → NEEDS_REPAIR → REPAIRING → READY_FOR_VERIFICATION → VERIFYING → VERIFIED.

**git / credentials:** `git diff --stat` shows only the files listed above; the credential-pattern scan
over tracked + untracked files found no matches; `grep` finds no `git reset|checkout|clean` in `src/`.

## Known issues / limitations

- **Weak evidence can verify.** A plan-selected test command that passes before and after,
  while the run changed files, is accepted as VERIFIED and reported with the risk "may not
  exercise the change". The regression→repair scenario depends on this. An unselected fallback
  suite that passes both times is not enough (UNVERIFIED).
- **Suite fallback blames less.** When the plan selects no command, an unchanged failure of the
  discovered suite is treated as pre-existing unless a criterion maps to a still-failing test
  (then TASK_TEST_FAILURE). A task whose test name shares no identifier with its criteria can end
  UNVERIFIED instead of NEEDS_REPAIR.
- **Criteria matching is lexical** (criterion identifiers vs failing/fixed test ids). Most
  natural-language criteria stay UNKNOWN; that is intentional.
- **Environment heuristics are small.** Unusual tool errors are classified as code failures and
  would trigger a repair attempt.
- **Fixed verification overhead.** Every git run spends 3 + 2 + 3-per-round git tool calls on
  snapshots, which counts toward `max_tool_calls`.
- **No stop rule for repeated failures.** A repair that repeats an identical failure only produces
  a warning in the next repair context; the run continues until `max_repair_cycles`.
- **Suite-level commands only.** Verification runs whole suites; slow suites cost `command_timeout`
  per round.
- **`sys.executable` fallback interpreter** (from M4) usually lacks the target's dependencies. That
  now correctly ends BLOCKED (ENVIRONMENT_ERROR) rather than in pointless repairs.

## Notes for M6

- **Where things live:** `state.verification_commands` (V1…), `state.baseline`,
  `state.verification_reports` (one per round), `state.evidence` (EvidenceLedger),
  `state.changes` (ChangeLedger), `state.repair_cycles`, `state.completion_claims`.
- **Compaction:** the executor request now also carries `# Verification` (baseline + repair
  context). `RepairContext` excerpts are bounded (1 500 output, 2 000 diff). Compaction should
  treat verification facts (`ContextManager` kinds `verification` and `repair_attempt`) as
  permanent-ish, since they drive repair.
- **Targeted tests:** `outcomes.failing_tests()` already extracts test ids per runner. A targeted
  command can be derived from them, but must stay discovered or derived deterministically, never
  invented, and must keep the same `VerificationCommand` id across baseline and post-change,
  otherwise comparison breaks.
- **Budget accounting:** verification uses `registry.dispatch` directly (no model), always checking
  `max_tool_calls` first. Keep that pattern for any new verification step.
- **Report files:** `format_run(state)` is the text report; `EvidenceItem`s and
  `VerificationReport`s are frozen dataclasses (asdict-serializable, with enums as strings).
- **Live adapter:** unchanged. One entry in `model/factory.ADAPTERS`.

## Important architectural decisions

- (M5) Baseline runs after planning and before any edit; the same command ids are compared across
  rounds; results are classified deterministically, and a model never decides pass/fail.
- (M5) Verdict policy in arch.md §14: NEEDS_REPAIR on repairable findings; VERIFIED only with
  positive evidence (strong, or weak and flagged); BLOCKED when the environment prevents every
  command; otherwise UNVERIFIED. Budget exhaustion is never VERIFIED.
- (M5) Pre-existing failures and dirty working trees are reported, not blamed, and never cleaned.
  No destructive git operations; repairs are forward patches tracked in the ChangeLedger.
- (M5) Repair reuses the M4 Executor/ToolRegistry with a bounded RepairContext and fresh file
  reads, limited by `max_repair_cycles` checked before any model call.
- (M5) Harness-run commands never write Python bytecode.
- (M4) One ToolContext per run; strict JSON protocols; budgets checked before the operation.
- (M3) Repository intelligence owns scope; bounded working set.
- (M2) Pure-Python patching, no implicit shell, read-only git, single path boundary.
- (M1) Python ≥ 3.10, stdlib only, `unittest`, `PYTHONPATH=src` in make targets.

## Commands

```sh
export AI_API_KEY="..."
make setup        # idempotent, works offline
make run          # interactive; stops with a precise error until a live adapter exists
make test         # 338 tests, offline, no key needed
make clean
PYTHONPATH=src .venv/bin/python -m harness inspect --repo PATH [--task "TEXT"]
```

## Last successful test

2026-09-26: `make clean && make setup && make test` gave 338 tests OK (Python 3.14.3); also 338 OK on
3.10.19, on 3.12 offline, and without ripgrep (2 skipped).

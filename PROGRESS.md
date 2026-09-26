# Implementation Progress

Last updated: 2026-09-26 (M6 + DeepSeek/Qwen adapter + AUDIT.md fixes + live Bedrock evaluation; not committed).

## Current state

- Branch `main`: `34d0896` M1 · `d2c6e1a` M2+M3 · `a11b23a` M4 · `95e3c32` M5 (committed at the start of
  M6 at the user's request after a clean credential scan) · M6 uncommitted on top.
- M6 changes (against `95e3c32`):
  - new: `src/harness/verify/targeting.py`, `src/harness/context/{facts,compaction}.py`,
    `src/harness/telemetry.py`, `tests/test_m6_{targeting,policy,compaction,telemetry,integration}.py`
  - modified: `verify/{commands,engine,recovery}.py`, `orchestrator/{state,executor,orchestrator,report}.py`,
    `context/manager.py`, `cli.py`, `config.py`, `.gitignore` (`.harness/`), tests (`test_cli`,
    `test_config`, `test_orchestrator`, `test_verification`), `arch.md`, `README.md`, `.env.example`,
    `REQUIREMENTS.md`, `PROGRESS.md`
  - no M2 tool, M3 repository or model source changed.
- Harness capabilities: git-aware discovery → strict JSON plan → targeted + suite baseline before any
  edit → one-action-per-step execution with deterministic compaction → evidence-based verification
  (VERIFIED only on strong evidence) → bounded repair with a repeated-failure stop rule → run
  artifacts and `harness runs` / `harness report`.

### Defects and changes found during M6

1. **Test runs wrote artifacts into this project.** Once telemetry was wired into the CLI, the M5 CLI
   run tests used the default runs root (`<checkout>/.harness/runs`) and left 3 run directories there.
   They were deleted, and those tests now use a temporary `HARNESS_RUNS_DIR` and assert the artifacts.
   The orchestrator writes artifacts only when given a `RunRecorder`, so lower-level tests never do.
2. **Criterion evidence was overwritten.** When two commands (targeted + suite) fixed the same test,
   only the later command's evidence was linked. Both are linked now.
3. **Policy tightened (intentional).** The M5 regression scenario (pass→pass after repair) now ends
   UNVERIFIED instead of VERIFIED, and its test was updated as the brief requires.
4. **M4/M5 tests pin `targeted_tests=False`** in their run helpers so their exact overhead constants
   (one suite command) still hold; targeting has its own tests. The 30-identical-reads M4 test now uses
   30 distinct `read_range` calls, because identical calls are (correctly) stopped by the no-progress rule.

## Final gap analysis

| Req | Status |
|---|---|
| R1 | **VERIFIED** (live: 21/24 tasks VERIFIED across 8 Qwen/DeepSeek models on Bedrock, independently re-checked) |
| R2 Repository navigation | **VERIFIED** (all items) |
| R3 Orchestration | **VERIFIED** (all items) |
| R4 Context management | **VERIFIED** (all items; compaction added in M6) |
| R5 Tool use | **VERIFIED** (all items) |
| R6 Failure recovery | **VERIFIED** (all items; repeated-failure stop rule added in M6) |
| R7 Verification | **VERIFIED** (all items; stricter policy in M6) |
| R8 Efficiency | **VERIFIED** (all items; targeted tests added in M6) |
| R9 Makefile | setup/test/clean **VERIFIED**; `make run` **PARTIAL** (no live adapter) |
| R10 Credentials | **VERIFIED** |
| R11 Model | provider abstraction **VERIFIED**; text-only, configurable **PARTIAL** (provider not announced) |
| R12 Reproducibility | dependencies **VERIFIED**; clean-environment and docs **PARTIAL** (macOS only; not the evaluator environment) |

## Live model evaluation — 2026-09-26 (post-M6: adapter + audit fixes)

Endpoint: AWS Bedrock OpenAI-compatible (`bedrock-mantle.us-east-1.api.aws/v1`) with a short-term Bedrock
API key; `AI_MODEL_ADAPTER=openai_compatible`. 8 models x 3 tasks through `harness run` (CLI, real HTTP):
A one-line bug, B two pricing bugs (discount + rounding, 2 of 3 tests failing), C feature addition
(missing function imported by the tests). Each result was re-checked outside the harness (tests re-run,
`git status` of `tests/`).

| Round | Harness state | VERIFIED |
|---|---|---|
| baseline | adapter only | 6/24 (DeepSeek V3.2 3/3; most Qwen 0/3: protocol mismatches) |
| 2 | + native complete/blocked, tolerant action shapes, JSON-in-prose, text-markup tool calls, bounded invalid-reply feedback, `edit_file`/`write_file` | 18/24 |
| 3 | + one corrective re-plan, repeat/test-passed/low-budget hints, `python`->`python3`, quoted commands | **21/24** |

Round 3 per model: qwen3-coder-480b, qwen3-235b-2507, qwen3-coder-next, qwen3-next-80b, qwen3-32b,
deepseek.v3.2: 3/3 each; deepseek.v3.1 2/3 (B: no edit, stopped by the no-progress rule);
qwen3-coder-30b 1/3 (B: step budget; C: raw newline in text JSON, fixed afterwards and re-run VERIFIED 2/2).
Every VERIFIED result also passes independently; every failure also fails independently (no false
success); no run modified, deleted or added a test file; 618 artifact files scanned: no key or key fragment.

## What still prevents real evaluator execution

1. **Evaluator endpoint unknown.** The `openai_compatible` adapter works live (above), but the
   organizers' endpoint, model ids and key scope are not confirmed, so no default is built in; the
   evaluator must set `AI_MODEL_ADAPTER`, `AI_MODEL`, `AI_BASE_URL` (documented in README/.env.example).
   Without them `make run` exits 2 with a precise message and modifies nothing.
2. **Target interpreter.** For `python -m <runner>` the first interpreter that can import the runner is
   used (target venv, harness, PATH `python3`/`python`); if none can, the run ends BLOCKED
   (environment error), never mis-verified. No real pytest run has been executed on this machine
   (pytest is not installed anywhere here).
3. **Only exercised on macOS** (Python 3.10–3.14). `make setup` now falls back to a pip-less venv and
   replaces partial venvs (simulated); not yet run on Linux.

## Completed

- M1 `34d0896`, M2+M3 `d2c6e1a`, M4 `a11b23a`, M5 `95e3c32`, M6 (2026-09-26, uncommitted).

## Currently implementing

Nothing. Remaining: confirm the evaluator's endpoint/model ids, and a Linux rehearsal.

## Verified — M6 evidence

macOS (Darwin 25.6.0), project root. Scripts: `<scratchpad>/verify_m6.sh` (build/test matrix),
`<scratchpad>/trace_m6.py` (scenario traces with real artifacts).

**Build/test matrix:** working tree `make clean && make setup && make test` → 0/0/0, `Ran 380 tests OK`;
fresh copy Python 3.10.19 → 0/0 (380 OK); fresh copy offline (`PIP_NO_INDEX=1`, Python 3.12) → 0/0
(380 OK, 1 offline warning); fresh copy without ripgrep → 0/0 (380, 2 skipped). No `.harness/` was
created in the project by the suite.

**Test count: 380** (338 before M6 + 42: targeting 12, policy/no-progress 8, compaction 6,
telemetry/CLI 9, integration + scale 6, config 1).

| # | DoD item | Evidence |
|---|---|---|
| 1 | Previous tests pass | 338 pass (changes listed above) |
| 2–4 | Targeted selection, targeted first, safe fallback | `DerivationTest` (7), `EscalationTest` (5): evidence order T1, V1, T1, V1; unsupported frameworks → no target with reason |
| 5 | Weak evidence cannot verify | `test_unchanged_pass_alone_is_not_verified`, `test_new_passing_test_alone_is_weak` |
| 6–9 | Compaction, no model call, facts survive, stale removed | `CompactionIntegrationTest` (5), `ProtectedFactTest` |
| 10–11 | Repeated failure / no progress stop | `NoProgressTest` (4) |
| 12–16 | events/summary/report, no secrets, no target pollution | `ArtifactTest` (5), `RunsDirTest` (2) |
| 17 | runs/report CLI without key | `RunsCliTest` (2); manual run below |
| 18 | Real resource metrics | summary/report counters equal RunState; event counts equal model/tool call counts |
| 19 | Large repository selective | `ScaleTest` (3): 1,005 files, ≤ 10 files read, < 5% of bytes |
| 20 | Offline end-to-end VERIFIED | `FinalIntegrationTest` (3) |
| 21 | Offline setup | matrix above |

### Scenario trace (`trace_m6.py`; fake key in the environment; every run writes artifacts)

```text
targeted-integration     VERIFIED    r1:VERIFIED[T1:FIXED,V1:FIXED]                        model=4  tool=14 cmds=4
   T1 = python -m unittest tests.test_math_utils.AddOneTest.test_add_one (targeted), V1 = unittest discover (suite)
   ledger: E2 baseline T1 TEST_FAILURE, E3 baseline V1 TEST_FAILURE, E5 post T1 PASS, E6 post V1 PASS
fail-repair-pass         VERIFIED    r1:NEEDS_REPAIR/TASK_TEST_FAILURE[T1] | r2:VERIFIED[T1:FIXED,V1:FIXED]  model=7 repairs=1
no-change-repair         UNVERIFIED  repeated_failure_no_progress {no_progress: true, repeated_failures: 1}  model=4 repairs=1, 6 scripted responses unused
repeated-failure         UNVERIFIED  repeated_failure_no_progress {repeated_failures: 2, limit: 2}  repairs=2 of 5 allowed, 6 responses unused
weak-evidence            UNVERIFIED  r1:UNVERIFIED/NO_VERIFICATION_EVIDENCE[V1:UNCHANGED_PASS]
long-history-compaction  VERIFIED    3 compactions, model=22 (= 1 + steps: no compaction model call)
scale-1000-files         VERIFIED    inventory 1,005 files; read 2 config + 1 candidate (385 bytes); working set 1,065 chars
executor-no-progress     BLOCKED     no_progress {tool: read_file, repeats: 4}; 2 responses unused
artifacts: 31 files, 213 KB; fake key present: False; artifacts inside any target repository: False
target repositories: only src/math_utils.py modified (none for the no-progress run)
```

`harness runs` and `harness report <id>` on those artifacts with `AI_API_KEY` unset returned exit 0, and
listed/printed the runs without calling the model factory (mock asserted in `RunsCliTest`).

**git / credentials:** `git status` shows only the files listed above; credential-pattern scan over
tracked + untracked files found no matches; no `git reset|checkout|clean` in `src/`.

## Known limitations

- **Token counts are only as good as the model client.** ScriptedModel reports deterministic *estimates*
  (`chars // 4`). A live adapter must pass real provider usage. The report labels tokens "as reported
  by the model client".
- **Targeted selection is lexical.** A test is selected when its name contains a task term; otherwise
  the whole test file/module/package is targeted. Only unittest, pytest and go are supported.
- **Weak evidence is never enough.** Tasks whose existing tests already pass and whose new tests cannot
  be shown failing beforehand end UNVERIFIED unless every criterion is structurally provable. This is
  intentional.
- **Criteria matching is lexical** (M5). Most natural-language criteria stay UNKNOWN.
- **Compaction thresholds are character-based.** When irreducible content (task, plan, repair context,
  tools) alone exceeds the threshold, the record says `reached_limit: false`.
- **Artifact generation is outside the run's budgets** (e.g. the final git diff).
- **Guardrails, not a sandbox** (M2): the command policy cannot see inside `python -c`, `make` or test code.

## Important architectural decisions

- (M6) Targeted tests are derived deterministically before the baseline with stable ids (`T1`…), and
  the suite they narrow becomes a regression check that runs only after the target passes.
- (M6) VERIFIED requires strong evidence (fail→pass, fewer failures, or all-structural criteria);
  weak pass→pass evidence means "no detected regression" only.
- (M6) Repeated-failure / no-progress stop rules end repair loops early, without model calls.
- (M6) Compaction is deterministic and staged; protected facts, criteria and the current failure are
  never dropped.
- (M6) Artifacts live in one deterministic runs root, never inside the target repository, and are
  bounded and redacted; recording proxies do not change accounting.
- (M5) Baseline before edit; deterministic classification; evidence ledger; no destructive git.
- (M4) One ToolContext per run; strict JSON protocols; budgets checked before operations.
- (M3) Repository intelligence owns scope; bounded working set.
- (M2) Pure-Python patching, no implicit shell, read-only git, single path boundary.
- (M1) Python ≥ 3.10, stdlib only, `unittest`, `PYTHONPATH=src` in make targets.

## Commands

```sh
export AI_API_KEY="..."
make setup        # idempotent, works offline
make run          # interactive; stops with a precise error until a live adapter exists
make test         # 380 tests, offline, no key needed
make clean
PYTHONPATH=src .venv/bin/python -m harness inspect --repo PATH [--task "TEXT"]
PYTHONPATH=src .venv/bin/python -m harness runs
PYTHONPATH=src .venv/bin/python -m harness report RUN_ID [--json]
```

## Last successful test

2026-09-26: `make clean && make setup && make test` gave 380 tests OK (Python 3.14.3); also 380 OK on
3.10.19, on 3.12 offline, and without ripgrep (2 skipped).

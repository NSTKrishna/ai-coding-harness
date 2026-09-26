# Hostile technical audit

> **Status (2026-09-26, after the audit):** fixed with regression tests - B1, B2, B3, B4, H1, H3, M1,
> M2, M3, M4 (tests/test_audit_regressions.py, test_interpreter.py, test_secrets.py,
> test_m6_compaction.py, test_verification_outcomes.py); H2 fixed in the Makefile and exercised on macOS
> with a simulated missing ensurepip (not yet on Linux). Open: M5 (Python >= 3.10 kept), L1-L4, I1-I5.
> The live adapter now exists (see PROGRESS.md "Live model evaluation"). The findings below are the
> original audit text.

Audited commit: `954bd96` (M6). Platform used: macOS (Darwin 25.6.0), Python 3.14.3 / 3.11 / 3.10.
Method: read CLAUDE.md, arch.md, REQUIREMENTS.md, PROGRESS.md, README.md, Makefile, pyproject.toml
and the source/tests, then **ran adversarial scenarios through the real orchestrator** (real
temporary git repositories, real subprocesses, ScriptPedModel standing in for the model). Probe
scripts live outside the repository (`<scratchpad>/audit_probes.py`, `audit_probes2.py`) and are
reproduced inline below. No implementation file was changed.

Only observed behaviour counts. PROGRESS.md/REQUIREMENTS.md statuses were not trusted.

---

## Evaluator readiness

**NOT READY.**

Two independent reasons:

1. **Blocked by the official provider (not the team's fault).** `make run` accepts input and then
   stops with exit 2 ("No model provider is configured …") because `model/factory.ADAPTERS` is empty.
   This cannot be fixed until the provider, model and endpoint are announced.
2. **Not ready even with a provider.** Four reproducible routes make the harness report
   **VERIFIED for an unfixed task** (B1–B4 below). A live model *will* take some of these routes
   (editing a failing test is common model behaviour). Those are defects in code the team controls.

### Exact remaining integration work once the provider is announced

| # | Work | Where |
|---|---|---|
| 1 | Implement one `ModelClient` adapter (HTTP via `urllib`), mapping provider text / native tool calls / usage / finish reason to `ModelResponse`, raising `ModelError(retryable=…)` | new `src/harness/model/adapters/<provider>.py`, registered in `model/factory.ADAPTERS` |
| 2 | Set `DEFAULT_MODEL_PROVIDER`, `DEFAULT_MODEL`, `DEFAULT_BASE_URL`. The evaluator exports only `AI_API_KEY`, so without defaults `make run` still fails with "No model provider is configured" | `src/harness/config.py` (marked block) |
| 3 | Transient-error retry/backoff (documented as planned in arch §9, not implemented) | adapter or `MeteredModelClient` |
| 4 | Check context sizes against the model's real window. The executor threshold is 60 000 **characters**; the planner request is bounded by `max_context_chars` (24 000) + task (8 000) + overhead | `ContextLimits` defaults |
| 5 | One recorded live run (arch §26 item 4) and the live smoke test that arch §23 describes but that does not exist | tests |

Everything else in the evaluator path is currently implementable and is assessed below.

---

## Findings

Severity legend: BLOCKER (must fix before submission), HIGH (should fix before submission),
MEDIUM, LOW, INFORMATIONAL.

### BLOCKER

#### B1 — Editing the failing test produces VERIFIED

- **Finding:** Verification trusts test files that the run itself modified. A patch that changes the
  failing test's expectation to match the buggy code gives `FIXED` for both the targeted test and the
  suite, VERIFIED, and the acceptance criterion "add_one(1) returns 2" marked **PASS** while
  `add_one(1)` still returns 3.
- **Evidence:** `verify/engine.py::_assess_command` treats any `FIXED`/`IMPROVED` as strong evidence
  without checking whether the command's test files are in the run's `ChangeLedger`;
  `_assess_criteria` maps criteria to test ids by word match only.
- **Reproduction** (buggy fixture `tests/orchestration_helpers.BUGGY_REPO`, default limits):
  ```text
  plan → apply_patch tests/test_math_utils.py: assertEqual(add_one(1), 2) → assertEqual(add_one(1), 3) → complete
  => VERIFIED | T1 FIXED, V1 FIXED | crit PASS: add_one(1) returns 2
  ```
- **Why it matters:** the model can "pass" any task by editing the tests, and the harness reports success
  with linked evidence.
- **Fix:** record which pre-existing test files the run changed or deleted (the ChangeLedger already
  knows). Evidence from a command whose passing depends on such files must not be strong, and
  criteria must not PASS through them. Flag "test files modified by the run" as a risk; treat new test
  files as weak (already the policy).
- **Must fix before submission:** yes.

#### B2 — Deleting the failing test produces VERIFIED (Python-version dependent)

- **Finding:** Removing the failing test file makes the suite pass. With a target interpreter ≤ 3.11,
  `unittest discover` prints "Ran 0 tests / OK" with exit 0, so the comparison is `FIXED` and the
  run is VERIFIED, with the criterion PASS through the vanished test id. (Python ≥ 3.12 exits 5 for
  "NO TESTS RAN", so the same scenario behaves differently per interpreter.) `IMPROVED` is also
  reachable by deleting some of several failing tests: a failing id that disappears counts as
  "fixed".
- **Evidence:** `outcomes.compare` (a failing id missing from the post set is treated as fixed);
  `outcomes.tests_run` is computed but not used to reject a drop in test count.
- **Reproduction:** buggy fixture, a target `.venv/bin/python` → `python3.11`, `targeted_tests=False`,
  patch `--- a/tests/test_math_utils.py / +++ /dev/null`, complete →
  `VERIFIED | V1 FIXED | crit PASS: add_one(1) returns 2`.
- **Why it matters:** same as B1, and it is platform dependent.
- **Fix:** a test id counts as fixed only if it still exists and was observed passing (e.g. run the
  targeted/verbose command, or require `tests_run(post) >= tests_run(baseline)` and no deleted test
  files). A post-change test count lower than baseline must never be strong evidence.
- **Must fix before submission:** yes.

#### B3 — "Structural" criteria verify a task with no change

- **Finding:** If every acceptance criterion names an existing file and contains "exist/add/create/present",
  `_decide` returns VERIFIED ("structural evidence"), even when the run changed nothing. The criteria are
  written by the model (the plan), and DIFF_PROBLEM only fires when the plan contains an `edit` step.
- **Reproduction:** `NO_COMMAND_REPO`, plan with `acceptance_criteria=["src/math_utils.py exists"]`,
  `steps=[{"kind":"inspect",...}]`, then `complete` →
  `VERIFIED | structural evidence: every acceptance criterion is a file that exists after the change`.
- **Why it matters:** a model can end any task VERIFIED by writing trivially true criteria and no edits.
- **Fix:** structural PASS only for paths the run created or changed (ChangeLedger `net_changed`), and
  never VERIFIED with zero net changes unless the task is explicitly verification-only.
- **Must fix before submission:** yes.

#### B4 — An unrelated check going fail→pass verifies the task while the task's test still fails

- **Finding:** Any command's `FIXED` counts as strong evidence, including build/lint `check`
  commands. An `UNCHANGED_FAILURE` of the fallback suite is treated as pre-existing, not blamed. So
  fixing only a lint problem yields VERIFIED while the bug remains and its test still fails.
- **Reproduction:** buggy fixture without `tests/__init__.py` (no targeted command), a Makefile
  `lint:` target that fails while `src/math_utils.py` contains "TODO", criterion "the increment
  helper returns the next integer", and a patch that only removes the TODO line →
  `VERIFIED | V1 suite UNCHANGED_FAILURE (pre-existing), V2 check FIXED | crit UNKNOWN`.
- **Why it matters:** "success" comes from evidence unrelated to the task.
- **Fix:** only test-kind evidence tied to the task (targeted, plan-selected, or a suite whose failing
  ids changed) can be strong. Check commands only guard against regressions. When the only test
  evidence is an unchanged failing suite, the verdict must not be VERIFIED.
- **Must fix before submission:** yes.

### HIGH

#### H1 — Python verification uses the harness's stdlib-only venv; pytest projects are always BLOCKED

- **Finding:** `orchestrator/interpreter.resolve_python` prefers the target's `.venv`, then
  `sys.executable` (the harness's own `.venv`, which `make setup` creates with no packages), then PATH.
  Any target that is not already inside its own `.venv` and uses pytest gets `No module named pytest`,
  so the run ends ENVIRONMENT_ERROR → BLOCKED, even if a PATH `python3` has pytest.
- **Evidence:** on this machine pytest is importable by none of `python3`, `python3.11`, `python3.12`,
  `/usr/bin/python3` or the harness venv. **No pytest code path (targeted selector, output
  classification) has ever been executed**; only synthetic strings are tested.
- **Why it matters:** most Python evaluator repositories use pytest. The harness is conservative
  (no false success), but it cannot verify them.
- **Fix:** choose the first candidate interpreter that can import the suite's framework (`-c "import
  pytest"`, a cheap bounded probe), preferring the target venv, then PATH `python3`, then
  `sys.executable`. Record the choice as evidence. Add one test that actually runs pytest (skipped when
  it is unavailable).
- **Must fix before submission:** strongly recommended.

#### H2 — `make setup` is likely to fail on stock Debian/Ubuntu (UNVERIFIED — needs Linux)

- **Finding:** `make setup` runs `python3 -m venv .venv`. On Debian/Ubuntu without the
  `python3-venv` package, this fails at the `ensurepip` step, leaving a partial `.venv`. A second
  `make setup` then passes the `test -x $(VENV_PY)` guard and silently continues with a venv that
  has no pip. Stock Ubuntu 20.04 (3.8) and Debian 11 (3.9) also fail the ≥ 3.10 check; that failure
  is clear but fatal.
- **Evidence:** Makefile line 22; never executed on Linux (PROGRESS.md says so).
- **Why it matters:** the evaluator contract is `make setup && make run` on an unknown environment.
- **Fix:** because the harness needs no pip at runtime, fall back to `python3 -m venv --without-pip`
  (or skip the venv entirely and run with `PYTHONPATH=src python3`). Detect a partial venv. Run
  `make setup && make test` once in a Linux container (with and without `python3-venv`).
- **Must fix before submission:** yes (cheap, high impact).

#### H3 — Word-matched acceptance criteria reject correct fixes and bless incorrect ones

- **Finding:** `_assess_criteria` marks a criterion FAIL when any failing test id contains one of the
  criterion's identifiers, including tests that already failed identically at baseline. A correct fix
  was rejected: the run ended UNVERIFIED `repeated_failure_no_progress` after repair attempts. (The
  PASS side is already covered by B1/B2.)
- **Reproduction:** `BUGGY_REPO` plus a pre-existing failing test `test_add_one_logs_nothing`
  (`assertTrue(False)`), with the correct `FIX_PATCH` →
  `T1 IMPROVED, criteria FAIL → NEEDS_REPAIR → … UNVERIFIED repeated_failure_no_progress`.
- **Why it matters:** false failures on repositories with pre-existing failures, which is exactly the case
  M5 claims to handle.
- **Fix:** a criterion can only FAIL on tests that pass→fail or are new failures, never on failures
  unchanged from baseline. More generally, keep word-matching as a *hint* (UNKNOWN) rather than
  PASS/FAIL evidence.
- **Must fix before submission:** yes.

### MEDIUM

#### M1 — Stale repair context: "Current diff" and failure output are not current after an in-cycle patch

- **Finding:** `RepairContext` is built once per repair cycle and re-rendered unchanged at every repair
  step. After the model's repair patch, the prompt still says "Current diff (excerpt)" and shows the
  superseded diff, plus the old failure output.
- **Reproduction:** repair script `[plan(SEL), WRONG_PATCH, complete, REPAIR_PATCH, read_file,
  complete]`. At repair step 2 the file contains `x + 1`, but the prompt's "Current diff" still shows
  `+    return x + 3` and "AssertionError: 4 != 2".
- **Test enshrining it:** `tests/test_m6_compaction.py::test_essential_context_survives` asserts the
  pre-repair diff is present in the **last** repair request, which is after the repair patch.
- **Fix:** label it "diff/failure at verification round N". After a patch during repair, mark it
  stale, or refresh the diff from `git_diff` (a counted tool call).
- **Must fix before submission:** recommended.

#### M2 — A baseline TIMEOUT followed by a post-change PASS counts as strong `FIXED`

- **Evidence:** `outcomes.compare`: `post == PASS and baseline != PASS → FIXED`, including a baseline
  `TIMEOUT`. A slow or cold first run (a common environment effect) plus any unrelated edit gives
  "strong evidence".
- **Fix:** TIMEOUT → PASS is weak, unless the task is about the hang (not decidable), or re-run the
  baseline once on timeout.
- **Must fix before submission:** recommended.

#### M3 — Unstable output without test ids is compared by hash, so harmless output changes look like regressions

- **Evidence:** `outcomes.output_hash` hashes the last 30 normalized lines. Runners whose failures are
  not parsed (mocha, custom scripts, `make test` wrappers, jest detail lines) produce ordering- or
  message-dependent hashes. A different hash means `CHANGED_FAILURE`, which becomes `REGRESSION` and
  triggers repair attempts on noise.
- **Fix:** without test ids, treat fail→fail with a different hash as `UNCHANGED_FAILURE`/inconclusive,
  not a regression. A regression requires a pass→fail or new test ids.
- **Must fix before submission:** recommended.

#### M4 — Child processes inherit other secrets and the harness `PYTHONPATH`

- **Evidence (probe):** a `run_command` child sees `AI_API_KEY=None` (correct), but also
  `OTHER_SERVICE_TOKEN=tok-visible` and `PYTHONPATH=<harness>/src`. Only `AI_API_KEY` is scrubbed. The
  harness's `PYTHONPATH` leaks into target test runs; a target with a top-level `harness` package would
  import the wrong one, and import semantics differ from a clean run.
- **Fix:** drop `PYTHONPATH` from child environments (and `PYTHONHOME`). Consider an allowlist
  environment for verification commands. Document that other variables are inherited.
- **Must fix before submission:** remove `PYTHONPATH`, yes; the environment allowlist is optional.

#### M5 — Python ≥ 3.10 requirement against unknown evaluator images

- **Evidence:** `make setup` exits with a clear message on 3.8/3.9, but that is still a failed setup.
  Only 3.10–3.14 were tested (on macOS).
- **Fix:** keep the requirement but make it prominent in README, or test 3.8/3.9 compatibility (code
  uses `X | Y` only in annotations under `from __future__ import annotations`, so it may be feasible).
- **Must fix before submission:** decide explicitly.

### LOW

- **L1 — `max_verification_commands` can remove the regression check entirely.** With a cap of 1, only
  `T1` runs and the broad suite is never run, yet the run can be VERIFIED; the omission appears only as
  a risk line. Fix: never cap away the suite of a targeted command, or downgrade the verdict.
- **L2 — `summary.json` `modified_files` ≠ `RunState.modified_files`.** Summary uses `net_changed()`, so a
  patched-then-reverted file disappears, while the text report and state still list it. Document the
  difference or report both.
- **L3 — Repeated-failure rule does not catch alternating failures** (A→B→A→B). Bounded by
  `max_repair_cycles`, so not an infinite loop.
- **L4 — Non-interactive `make run` without stdin** exits 2 ("no input received"). How the evaluator
  supplies the task is unknown; `make run ARGS=...` is documented.

### INFORMATIONAL

- **I1 — Guardrail, not sandbox.** The command policy blocks obvious damage, but `python -c`, scripts,
  `make` targets, test code and network access are unrestricted and run with the user's permissions.
  Target test code can read absolute paths outside the repository, including the harness checkout's
  `.env`. arch.md §25 states this correctly.
- **I2 — Reads outside the tool budget.** Discovery (`RepoReader`), targeting (`load_text_lines`) and
  `final.diff` (`tools.git.git_diff`) read the repository without `ToolRegistry` and outside
  `max_tool_calls`. They go through the path boundary and are documented; the counts are in
  `DiscoveryMetrics`, not tool calls.
- **I3 — Token numbers in all tests and traces are ScriptedModel estimates (`chars // 4`),** not
  measurements.
- **I4 — Dead or test-only code:** `context.facts.RepositoryFact` is never used; `RunState.summary()` /
  `RunSummary` are used only by tests; `Executor.last_snapshot` duplicates `RunState.context_snapshot`.
- **I5 — Configuration not consumed until an adapter exists:** `AI_MODEL`, `AI_BASE_URL`.

---

## Runtime wiring audit

Traced through `Orchestrator._run` (`src/harness/orchestrator/orchestrator.py`):

| Stage | Called? | Notes |
|---|---|---|
| intake | yes | `ToolContext.create`; one context shared (tested by `WiringTest`) |
| discovery | yes | `discover_for_task(..., ctx=ctx)` before the planner (order asserted) |
| context | yes | `ContextManager` + `ContextRenderer` in `Executor.build_request` |
| planner | yes | `Planner.create_plan` through `MeteredModelClient` |
| baseline | yes | `VerificationEngine.baseline()` before `EXECUTE` |
| executor / tools | yes | every model action goes through `registry.dispatch_call`; no bypass found |
| verification | yes | every result goes through `EvidenceLedger.record_*` |
| repair | yes | same `Executor`; limits checked before model calls |
| re-verification | yes | every repair ends in READY_FOR_VERIFICATION → VERIFYING (transition table) |
| telemetry | yes, CLI only | `RunRecorder` only when passed; `Orchestrator` default writes nothing |

No duplicate counters were found: `RunState` reads `ExecutionMetrics`, and event counts equal the
state counts (tested). The only paths outside `ToolRegistry` are listed in I2. **The verdict logic,
not the wiring, is where the defects are (B1–B4, H3).**

## Recovery audit

Traced with real runs (M5/M6 tests and the probes above):

- **Fresh content:** yes. Changed files are re-read with `read_file`, stale evidence is removed, and the
  first repair request contains the current file. **But** the repair brief itself goes stale after an
  in-cycle patch (M1).
- **Memory of earlier attempts:** yes. Protected `VerificationFact`/`FailureFact`/`RepairAttemptFact`
  are carried into the repair prompt.
- **Same-failure and no-progress detection:** yes. The no-change repair stops after 1 cycle with the
  remaining script unused; the same failure stops after 2 of 5 cycles.
- **`max_repair_cycles` exact:** 0, 1 and 2 were checked; no extra model call after exhaustion
  (remaining scripted responses asserted).
- **Global budgets:** exact totals asserted, including verification and fresh reads.
- **Environment failures:** no repair is triggered (3 model calls, BLOCKED).
- **Weak point:** repair is triggered by word-matched criteria (H3), and can be *avoided* by editing
  tests (B1).

## Context audit

- **Whole repository not loaded:** on the 1 005-file fixture, 3 files (385 bytes) were read. Verified.
- **WorkingSet limits:** enforced on rendered text (tested for budgets from 80 to 10 000). Verified.
- **Compaction limits:** real and model-free (same model-call count). When irreducible content exceeds
  the threshold, the record honestly says `reached_limit: false`; there is no hard ceiling. The
  irreducible parts are the task (≤ 8 000), plan (≤ ~7 000), repair context (≤ ~5 000), baseline brief
  (≤ ~700 per command) and tool list (~5 000), so roughly **25–30k chars minimum**, well under the
  60k default.
- **Permanent context, current failure, criteria, repair attempts survive:** yes (tested).
- **Stale file content removed:** yes for file content. No for the repair diff and failure text (M1).
- **Repeated observations collapsed:** yes, under compaction.

## Tool security audit

Path traversal, absolute paths, symlink escapes, `cwd` escapes, patch escapes and `.git` writes are
rejected by the single `resolve_in_repo` boundary, with integration tests on real files. Destructive
commands are blocked by a small policy with explicit shell-script inspection; `AI_API_KEY` is removed
from children. **Not sandboxed** (I1). Other environment variables and `PYTHONPATH` are inherited (M4).
Output is bounded (head/tail, per-stream cap) and redacted in registry results and artifacts.

## Telemetry audit

Verified with real runs: all four files parse; `seq` is contiguous; the run id matches; event counts equal
the model/tool counters; every evidence id referenced in `summary.json` exists; no API key in 31
artifact files across 8 runs; no prompts or model reasoning are recorded (only sizes, purposes, short
summaries); artifacts are never inside the target repository (relocation tested); `final.diff` includes
only harness-changed paths and names pre-existing changes. Report wording follows the terminal status.
**However, a VERIFIED report produced through B1–B4 is well formed and wrong.** Telemetry faithfully
records the unsound verdict. Minor inconsistency: L2.

## Cross-platform audit

| Item | Assessment |
|---|---|
| Process groups (`start_new_session` + `os.killpg`) | POSIX-portable; macOS only tested → **UNVERIFIED on Linux** |
| Makefile (`test -x`, `find -prune -exec`, `$(CURDIR)` with spaces) | GNU make/find compatible by reading → **UNVERIFIED** |
| venv creation | likely failure on Debian/Ubuntu without `python3-venv` (H2) → **UNVERIFIED** |
| `.pth` hidden-flag workaround | macOS-specific issue, handled by `PYTHONPATH`; harmless on Linux |
| `/private/var` vs `/var` path resolution | tests use `resolve()`; Linux `/tmp` is not a symlink → probably fine, **UNVERIFIED** |
| Case-sensitive filesystems | macOS default is case-insensitive; nothing found depending on it → **UNVERIFIED** |
| `git` (≥ 2.x, `GIT_OPTIONAL_LOCKS`, `ls-files -z`) | standard |
| `rg` optional | fallback tested with rg removed from PATH |
| unittest "no tests" exit code | differs 3.11 vs 3.12+ (B2) |
| Windows | unsupported (Makefile `.venv/bin`, `killpg`); not claimed otherwise, but README does not say "POSIX only" |

## Test-quality audit

380 tests pass. Real strengths: tool, git, patch, discovery and orchestration tests use real files,
processes and git repositories, not mocks. The CLI `run` path is tested only with a mocked factory,
which is unavoidable without a provider. Weaknesses:

- **Fixtures are lexically aligned with the heuristics.** Every task says "add_one", every test is named
  `test_add_one`, so targeting and criteria matching always "work". H3 shows what happens when names
  align by accident.
- **No adversarial model behaviour:** no test where the model edits or deletes tests, writes trivially
  true criteria, or fixes something unrelated. B1–B4 went unnoticed because of this.
- **pytest and go targets are derived but never executed;** their output parsing is tested only against
  synthetic strings.
- **One test enshrines a defect** (`test_essential_context_survives` asserts the stale repair diff, M1).
- **Implementation-detail tests** pin exact tool-call overhead (`BASELINE_TOOLS = 6`, …). They are useful
  for accounting but break on any snapshot change; acceptable.
- **Never run on Linux.**

**Smallest set of missing adversarial tests** (each targets a real defect found above):

1. Model edits the failing test expectation → must not be VERIFIED (B1).
2. Model deletes the failing test, run with a ≤ 3.11 target interpreter → must not be VERIFIED (B2).
3. Plan with only "file exists" criteria and no change → must not be VERIFIED (B3).
4. Unrelated check fail→pass while the task test still fails → must not be VERIFIED (B4).
5. Correct fix with an unrelated pre-existing failure sharing an identifier → must not be rejected (H3).
6. Repair prompt after an in-cycle patch must not present the old diff as current (M1).
7. One real pytest run (skip if pytest is absent) exercising the targeted selector and output parsing (H1).
8. `make setup && make test` in a Debian container without `python3-venv` (H2).

## Documentation audit

| Claim | Location | Status |
|---|---|---|
| "VERIFIED only on observed evidence" / "the model's claim is never evidence" | README, arch §14, PROGRESS | **Contradicted** by B1–B4: evidence can be manufactured by the model's own edits |
| "Every acceptance criterion links to passing evidence" style claims, "criteria PASS only from observed evidence" | REQUIREMENTS R7, arch §14 | **Misleading**: word matching gives PASS through edited or deleted tests (B1, B2) |
| "Pre-existing failures are distinguished from new failures" | arch §14, REQUIREMENTS | Partly: commands yes; criteria no (H3) |
| "Stale file content never reappears" | REQUIREMENTS R4 evidence, arch §8 | File content yes; repair diff and failure text no (M1) |
| Optional live smoke test with `HARNESS_LIVE_TESTS=1` | arch §23 | **Not implemented** |
| Transient model retries | arch §9 | Marked planned; correct |
| Quick start `export AI_API_KEY; make setup; make run` | README | Accurate about the provider stop; silent about Linux venv (H2), the Python ≥ 3.10 prerequisite on older distros, and that the target's test dependencies must be importable (H1) |
| Tokens "as reported by the model client" | reports | Accurate wording; all current numbers are estimates (I3) |
| Security: guardrail, not sandbox | arch §25 | Accurate; it should also mention inherited environment variables and `PYTHONPATH` (M4) |
| Telemetry location and contents | arch §19, README | Accurate |

---

## Requirements audit

Classification: VERIFIED / PARTIAL / MISSING / INCORRECTLY CLAIMED (claimed `[x]` but disproved above).
Evidence type: U = unit, I = integration (real files, processes, git; scripted model), E = end-to-end
through the CLI (mocked model factory).

| Req | Item | Claimed | Audit | Implementation | Evidence (type) |
|---|---|---|---|---|---|
| R1 | Accept task | x | VERIFIED | `cli.py` `_collect_input`; `Orchestrator.run` intake | `test_cli` incl. subprocess + pty (U/E) |
| R1 | Understand task | ~ | PARTIAL | `orchestrator/plan.py` | strict plan parsing, scripted output only (U/I) |
| R1 | Inspect existing repository | x | VERIFIED | `repo/*`, DISCOVER phase | `test_discovery`, `WiringTest` (I) |
| R1 | Determine relevant files | x | VERIFIED (lexically-aligned fixtures) | `repo/discovery.py` | `FixtureRankingTest`, `ScaleTest` (I) |
| R1 | Modify implementation | ~ | PARTIAL | `apply_patch` via executor | scripted runs only (I) |
| R1 | Verify modifications | x | **INCORRECTLY CLAIMED** | `verify/engine.py` | B1–B4 produce VERIFIED for unfixed tasks |
| R2 | Repository analyzer exists | x | VERIFIED | `repo/profile.py` | `test_repo_profile` (I) |
| R2 | Deterministic search before model calls | x | VERIFIED | orchestrator order | `WiringTest` order assertion (I) |
| R2 | Targeted file discovery | x | VERIFIED | `repo/discovery.py` | `test_discovery` (I) |
| R2 | No blind full-repository load | x | VERIFIED | inventory stat-only, bounded reads | `ScaleTest` (1 005 files, 3 read) (I) |
| R3 | Explicit lifecycle | x | VERIFIED | `state.TRANSITIONS` | `test_run_state` (U), orchestrator tests (I) |
| R3 | Plan | x | VERIFIED | `Planner` | `test_planner` (U/I) |
| R3 | Execute | x | VERIFIED | `Executor` | `ReadPatchIntegrationTest` (I) |
| R3 | Verify | x | **INCORRECTLY CLAIMED** (phase exists; verdict unsound) | VERIFYING phase | B1–B4 |
| R3 | Repair | x | VERIFIED (mechanics) | `RecoveryController` + `Executor` | `RepairTest`, `NoProgressTest` (I) |
| R3 | Finish/terminate | x | VERIFIED | terminal phases, no escaping exceptions | `MalformedOutputTest`, budgets (I) |
| R4 | Working vs permanent context | x | VERIFIED | `ContextManager` | `test_context_manager` (U) |
| R4 | Relevant snippets selected | x | VERIFIED | `build_working_set` | `test_working_set` (U/I) |
| R4 | Old observations compacted | x | VERIFIED | `context/compaction.py` | `test_m6_compaction` (I) |
| R4 | Repeated unnecessary context avoided | x | VERIFIED, with M1 caveat | dedupe, stale removal, windows | compaction + no-progress tests (I) |
| R5 | Read file | x | VERIFIED | `tools/files.py` | `test_file_tools` (I) |
| R5 | Search repository | x | VERIFIED | `tools/search.py` | `test_search` incl. no-rg run (I) |
| R5 | Apply patch | x | VERIFIED | `tools/patch.py` | `test_patch` (I) |
| R5 | Execute command | x | VERIFIED | `tools/commands.py` | `test_commands` (I) |
| R5 | Run tests | x | VERIFIED (unittest executed; pytest/go never) | `run_tests` | `CommandObservationTest`, verification tests (I) |
| R5 | Inspect git diff | x | VERIFIED | `tools/git.py` | `test_git_tools` (I) |
| R6 | Failed test detected | x | VERIFIED | `verify/outcomes.py` | `test_verification_outcomes` (U), `RepairTest` (I) |
| R6 | Failure classified | x | VERIFIED (heuristic; see M2, M3) | `outcomes.py` | same |
| R6 | Repair/replan path | x | VERIFIED for repair; replan missing | `recovery.py` | `RepairTest` (I) |
| R6 | Retry limit | x | VERIFIED | `max_repair_cycles`, repeated-failure rule | `LimitTest`, `NoProgressTest` (I) |
| R6 | Infinite loops prevented | x | VERIFIED | budgets + stop rules | budget tests (I) |
| R7 | Tests executed | x | VERIFIED | baseline + rounds | `test_verification` (I) |
| R7 | Git diff inspected | x | VERIFIED as recorded; **not used** to detect test tampering (B1, B2) | snapshots, DIFF evidence | `test_dirty_repository…` (I) |
| R7 | Acceptance criteria checked | x | **INCORRECTLY CLAIMED** | `_assess_criteria` | word matching gives false PASS (B1, B2) and false FAIL (H3) |
| R7 | Success based on evidence | x | **INCORRECTLY CLAIMED** | `_decide` | B1–B4 |
| R7 | Failed verification triggers repair | x | VERIFIED | orchestrator loop | `RepairTest` (I) |
| R8 | Model calls tracked | x | VERIFIED | `MeteredModelClient` | `test_model`, event-count parity (U/I) |
| R8 | Tool calls tracked | x | VERIFIED | `ToolRegistry` | `test_registry`, parity (U/I) |
| R8 | Context usage controlled | x | VERIFIED | working set + compaction | (I) |
| R8 | Targeted tests before full suite | x | VERIFIED for unittest; pytest/go derivation only | `verify/targeting.py` | `EscalationTest` (I), `DerivationTest` (U) |
| R8 | Expensive operations avoided | x | VERIFIED | escalation, stop rules, selective discovery | (I) |
| R9 | Root Makefile | x | VERIFIED | `Makefile` | present |
| R9 | make setup works | x | PARTIAL (macOS verified; Linux risk H2) | `Makefile` | matrix runs on macOS (E) |
| R9 | make run works | ~ | PARTIAL — blocked by provider | `cli.py` | provider error path tested (E) |
| R9 | make test works | x | VERIFIED on macOS | `Makefile` | 380 tests, 4 environments (E) |
| R9 | make clean works | x | VERIFIED | `Makefile` | matrix (E) |
| R10 | Reads AI_API_KEY | x | VERIFIED | `config.py` | `test_config` (U) |
| R10 | No hardcoded credentials | x | VERIFIED | — | credential scan; `test_secrets` (I) |
| R10 | .env.example clean | x | VERIFIED | `.env.example` | `test_env_example_has_no_credential` (U) |
| R11 | Text-only | ~ | PARTIAL | text protocol | no live model |
| R11 | Model configurable | ~ | PARTIAL | `AI_MODEL*` read, unused until an adapter exists | (U) |
| R11 | Provider abstraction | x | VERIFIED | `model/client.py`, `factory.py` | `test_model_factory` (U) |
| R12 | Clean environment setup tested | ~ | PARTIAL | — | macOS fresh copies only |
| R12 | Dependencies declared | x | VERIFIED | `pyproject.toml` (none) | (E) |
| R12 | No undocumented commands | ~ | PARTIAL | README | missing: Linux prerequisites, target test dependencies (H1, H2) |

**Items incorrectly claimed as satisfied: 4** (R1 verify modifications; R3 verify; R7 acceptance
criteria checked; R7 success based on evidence). The R7 git-diff item is verified as recorded, but
the diff is not used to catch test tampering. All four share one root cause: the verdict can be
satisfied by evidence the run itself manufactured.

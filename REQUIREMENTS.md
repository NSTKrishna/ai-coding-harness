# Hackathon Requirement Matrix

Status:
- [ ] not implemented
- [~] partially implemented
- [x] verified

---

## R1 — Autonomous software engineering harness

- [x] Accept software engineering task
- [~] Understand task
- [x] Inspect existing repository
- [x] Determine relevant files
- [~] Modify implementation
- [x] Verify modifications

Evidence (M5, 2026-09-26):
- Verify modifications: every completed execution is verified by the deterministic
  VerificationEngine. The same discovered command runs before any edit (baseline) and after it,
  and the outcomes are compared (tests/test_verification.py, 18 tests;
  tests/test_verification_outcomes.py, 11 tests). The model's `complete` is stored as a claim
  and is never evidence (`test_completion_claim_alone_is_not_evidence`).
- Modify implementation stays partial: patches and repairs are verified end to end, but only
  with scripted model output (no live model exists).

Evidence (M4, 2026-09-26):
- The task now flows through the run: `Orchestrator.run(repo, task)` goes INTAKE → DISCOVER
  (M3 discovery on the run's `ToolContext`) → PLAN → EXECUTE → `READY_FOR_VERIFICATION`
  (tests/test_orchestrator.py, 31 tests). Inspecting the repository and determining relevant
  files are now part of every run, not standalone components.
- Understand task: partial. The planner produces a validated `TaskPlan` (understanding,
  acceptance criteria, steps) from bounded context, but only scripted model output has been
  parsed; no live model exists.
- Modify implementation: partial. `ReadPatchIntegrationTest` reads, patches and completes
  with ScriptedModel: `src/math_utils.py` is changed, recorded in `modified_files`, and the
  stale context is invalidated. No live model has produced a patch.
- Verify: still missing. `complete` only reaches `READY_FOR_VERIFICATION`, never `VERIFIED`.

Evidence (M3):
- M3 (2026-09-26): `RepositoryAnalyzer` profiles a repository and `discover_for_task` ranks
  relevant files with reasons (tests/test_repo_profile.py, tests/test_discovery.py; `harness
  inspect` on Python/TS/Go/303-file fixtures). Partial: these components are verified on their
  own, but `harness run` does not use them yet (orchestrator is M4). "Understand task" stays
  open: M3 extracts search signals only; no goal or acceptance-criteria extraction.
- M1 (2026-09-26): `harness run` accepts the task via `--task`, `--task-file` or an
  interactive prompt, and validates repo path + task (tests/test_cli.py, 21 tests;
  real-pty run of `make run`). Partial: the task is accepted and validated, not yet acted on.

---

## R2 — Repository navigation

- [x] Repository analyzer exists
- [x] Uses deterministic search before model calls
- [x] Supports targeted file discovery
- [x] Does not blindly load entire repository

Evidence (M3, 2026-09-26):
- Analyzer: `RepositoryAnalyzer`/`analyze_repository` (src/harness/repo/profile.py) builds a
  git-aware inventory (tracked + untracked, `.gitignore` respected, noise directories dropped;
  filesystem fallback outside git) and a `RepoProfile` with evidence for every indicator
  (tests/test_repo_inventory.py 12, tests/test_repo_profile.py 7).
- Targeted discovery: ranked `CandidateFile`s with reasons; source/test pairing; one-hop
  imports. Fixture orderings asserted for Python (`service.py`, `test_service.py`), TypeScript
  (`token.ts`, `token.test.ts`), Go (`parser.go`, `parser_test.go`), non-git
  (tests/test_discovery.py, 20 tests).
- Not loading the whole repository: the inventory uses `stat` only. On a 303-file repository an
  anchored task reads 2 config + 1 candidate files, and the test asserts at most 8 candidate
  reads and under 5% of files read in total (`test_discovery_reads_few_files`). The working set is
  bounded by `ContextLimits` (tests/test_working_set.py).
- Search before model calls (M4): `WiringTest.test_discovery_runs_before_first_model_call_and_shares_the_tool_context`
  records the order `["discover", "plan"]`, and the planner request contains the working set.
  The M4 trace shows `discover_for_task -> model:plan -> model:step1 …`. Discovery needs no model
  or API key.

Evidence (M2):
- M2 (2026-09-26): primitives only. `find_files` (glob) and `search_text` (ripgrep with an
  identical pure-Python fallback) return structured, bounded, deterministic results
  (tests/test_file_tools.py, tests/test_search.py). No repository analyzer, ranking or
  "search before model" ordering exists yet.

---

## R3 — Orchestration

- [x] Explicit execution lifecycle exists
- [x] Plan
- [x] Execute
- [x] Verify
- [x] Repair
- [x] Finish/terminate

Evidence (M5, 2026-09-26):
- Lifecycle extended with BASELINING, VERIFYING, NEEDS_REPAIR, REPAIRING, VERIFIED, UNVERIFIED.
  VERIFIED is reachable only from VERIFYING, and repair cannot bypass verification
  (`test_verified_only_through_verifying`, `test_repair_cannot_bypass_verification`).
- Verify: VERIFYING rounds with typed `VerificationReport`s. Repair: NEEDS_REPAIR → REPAIRING
  (same Executor/ToolRegistry) → READY_FOR_VERIFICATION → VERIFYING
  (`test_failed_verification_is_repaired_and_reverified`, `test_regression_is_repaired_then_verified`).
- Finish/terminate: runs end VERIFIED / UNVERIFIED / BLOCKED / BUDGET_EXHAUSTED / errors, each with
  a structured reason. The M5 trace covers first-pass, repair, pre-existing failure, regression,
  environment error, no command, repair limits 0/1/2, and model/tool budget exhaustion.

Evidence (M4, 2026-09-26):
- Lifecycle: `Phase` enum with an enforced transition table; invalid moves raise
  `InvalidTransition`; VERIFYING/VERIFIED/NEEDS_REPAIR are unreachable in M4
  (tests/test_run_state.py, 9 tests).
- Plan: strict JSON `TaskPlan`, validated and never filled in (tests/test_planner.py, 6 tests with
  13 rejection cases; malformed plans end in MODEL_ERROR/`invalid_plan`).
- Execute: one action per model call, observations feed the next request
  (`test_one_action_per_model_call`, `test_each_observation_feeds_the_next_request`).
- Finish/terminate: partial. Every run ends in a terminal phase with a structured reason, with no
  traceback escaping (malformed output, model errors, tool crashes, budgets, invalid input all
  tested). A verified finish does not exist until M5.
- Verify, Repair: not implemented (M5).

---

## R4 — Context management

- [x] Working context separated from permanent context
- [x] Relevant files/snippets selected
- [ ] Old observations can be summarized/compacted
- [~] Repeated unnecessary context avoided

Evidence (M4, 2026-09-26):
- Executor requests are rebuilt from bounded windows each step (6 observations, 20 history
  lines) instead of resending the whole conversation. Request size stops growing once the
  windows fill (`test_execution_requests_stay_bounded`). Stale pre-edit content is replaced by a
  re-read marker and the file's working-set evidence is removed after a patch. Still partial:
  no compaction/summarization and no "unchanged since" re-read marker.

Evidence (M3, 2026-09-26):
- `ContextManager` (src/harness/context/manager.py): permanent (task, repo summary; never
  evicted), working (bounded by count and characters, lowest priority evicted first) and
  episodic stores (tests/test_context_manager.py, 10 tests).
- `build_working_set` selects snippets from the top files by value and enforces
  `max_active_files`, `max_candidates`, `max_evidence_items`, `max_snippet_lines` and
  `max_context_chars` on the exact rendered text, reporting omissions
  (tests/test_working_set.py, 10 tests, including budgets from 80 to 10 000 chars).
- Compaction: not implemented. Over-budget items are evicted, not summarized.
- Repeated context: partial. Identical content from the same source is stored once
  (`test_repeated_content_is_stored_once`); the planned "unchanged since turn N" re-read marker
  does not exist.

---

## R5 — Tool use

- [x] Read file
- [x] Search repository
- [x] Apply patch
- [x] Execute command
- [x] Run tests
- [x] Inspect git diff

Evidence (M4, 2026-09-26): all tools are now driven by the executor through
`ToolRegistry.dispatch_call` in end-to-end ScriptedModel runs. Run tests: discovered test
commands (with resolved interpreters) are offered to the planner, and the executor runs
`run_tests`. A failing suite is recorded as `success=True, outcome="command_failed", exit_code=1`
with the assertion excerpt (`CommandObservationTest`). Deciding *when* tests prove the task
is verification (R7), not this item.

Evidence (M2, 2026-09-26) — tool primitives, verified in isolation; nothing drives them
autonomously yet (the agent loop is M3):
- Read file: `read_file`, `read_range`, `list_files`, `find_files`; boundary, symlink, decode,
  binary, size-limit cases (tests/test_file_tools.py, 19 tests).
- Search: `search_text`, ripgrep and Python engines asserted identical; also passes with rg
  removed from PATH (tests/test_search.py; fresh-copy run without rg: 144 passed, 2 rg-only skipped).
- Apply patch: `apply_patch`, pure-Python unified diff; success, offset tolerance, mismatch
  diagnostics, all-or-nothing, create/delete, outside-repo/symlink/.git rejection, CRLF
  (tests/test_patch.py, 14 tests).
- Execute command: `run_command` with timeout, bounded output, process-group kill, cwd
  confinement, command policy (tests/test_commands.py).
- Run tests: `run_tests` runs a *supplied* test command and returns a structured
  `CommandResult` (pass and fail cases). Partial: M3 now discovers test commands with evidence
  (tests/test_command_discovery.py), but nothing yet connects discovery to execution.
- Inspect git diff: `git_status`, `git_diff`, `git_diff_stat`, read-only (index bytes unchanged),
  `not_git_repo` outside git (tests/test_git_tools.py).

---

## R6 — Failure recovery

- [x] Failed test detected
- [x] Failure classified
- [x] Repair/replan path exists
- [x] Retry limit exists
- [x] Infinite loops prevented

Evidence (M5, 2026-09-26):
- Detected and classified deterministically: `CommandStatus` (PASS, TEST/BUILD/LINT/TYPECHECK
  failure, ENVIRONMENT_ERROR, TIMEOUT, TOOL_ERROR, NOT_RUN), baseline-vs-post `Comparison`, and
  `FailureClass` (TASK_TEST_FAILURE, REGRESSION, BUILD/LINT/TYPECHECK_FAILURE, COMMAND_TIMEOUT,
  ENVIRONMENT_ERROR, NO_VERIFICATION_EVIDENCE, DIFF_PROBLEM) (tests/test_verification_outcomes.py).
  Environment errors never trigger repair (`test_environment_error_blocks_without_repair`: BLOCKED,
  3 model calls, 0 repairs).
- Repair path: RecoveryController → bounded RepairContext (failure output, fresh file contents,
  diff, prior attempts) → the same Executor. Replanning is not implemented; the repair path is.
- Retry limit: `max_repair_cycles` enforced exactly, checked before any model call. With 0, 1
  and 2 cycles the run makes 3, 5 and 7 model calls and has 1, 2 and 3 verification rounds
  (`LimitTest`, M5 trace).
- Loops: the verify/repair loop is bounded by `max_repair_cycles` and by the global step, model
  and tool budgets, which are never reset.

Evidence (M4, 2026-09-26) — bounded termination only, no recovery:
- Failed test detected: partial. A failing test command is recorded as a structured observation
  (`outcome="command_failed"`, exit code, stderr excerpt), but nothing acts on it.
- Infinite loops prevented: partial. The executor loop is bounded by `max_steps`,
  `max_model_calls` and `max_tool_calls`, checked before the operation, with exact counts tested
  (`BudgetTest`, 7 tests). No repair loop exists yet to bound, and `max_repair_cycles` is unused.
- Classification, repair/replan and retry limits: not implemented (M5).

---

## R7 — Verification

- [x] Tests are executed
- [x] Git diff inspected
- [x] Acceptance criteria checked
- [x] Success based on evidence
- [x] Failed verification triggers repair

Evidence (M5, 2026-09-26):
- Tests executed: only discovered commands, before editing (baseline) and after each execution
  or repair. `BASELINE_NOT_AVAILABLE` is recorded when none exists
  (`test_baseline_runs_before_the_first_edit`, `test_no_verification_command_is_unverified`).
- Git diff inspected: git_status / git_diff_stat / git_diff snapshots at start, after baseline and at
  every round. Dirty repositories are preserved and pre-existing changes are not attributed
  (`test_dirty_repository_is_preserved_and_not_attributed`).
- Acceptance criteria checked: each criterion is PASS / FAIL / UNKNOWN with evidence ids. PASS only
  from observed evidence (a related test failed before and passes after, or a structural file
  check); otherwise UNKNOWN. Conservative by design: many criteria will stay UNKNOWN.
- Success based on evidence: VERIFIED only with positive evidence (strong: fail→pass,
  fewer failures, more tests passing; weak and flagged: plan-selected tests pass before and
  after while files changed). No command → UNVERIFIED; environment → BLOCKED; no tool budget
  left for verification → BUDGET_EXHAUSTED, never VERIFIED. The EvidenceLedger holds typed items
  that refer only to observed events (`test_evidence_refers_to_observed_events`).
- Failed verification triggers repair: NEEDS_REPAIR → REPAIRING → re-verification
  (`RepairTest`, `RegressionTest`).

---

## R8 — Efficiency

- [x] Model calls tracked
- [x] Tool calls tracked
- [x] Context usage controlled
- [ ] Targeted tests before full suite
- [~] Expensive operations avoided when unnecessary

Evidence (M5, 2026-09-26):
- Verification needs no model call. It still runs when the model budget is exhausted, and only
  the repair is refused (`test_no_model_budget_left_for_repair_but_verification_still_runs`).
- Verification and repair use the same global counters (e.g. a repaired run: 7 model calls,
  19 tool calls = 6 baseline + 2 execute + 4 verify + 1 fresh read + 2 repair + 4 verify, asserted
  exactly). `max_verification_commands` caps commands per round; the rest are recorded NOT_RUN
  (`test_verification_command_cap`). No repair is attempted for environment errors.
- Targeted tests before full suite: still missing. Discovery only finds suite-level commands.

Evidence (M4, 2026-09-26):
- Budgets enforced before the operation that would exceed them, exact counts asserted:
  `max_steps=3` → 3 steps, 4 model calls, 3 tool calls; `max_model_calls=3` → exactly 3 model calls;
  `max_tool_calls=2` → exactly 2 dispatches (the third chosen tool is not run);
  `max_model_calls=1` → planner only; `max_model_calls=0` → no call at all; no extra model call to
  announce exhaustion (tests/test_orchestrator.py `BudgetTest`, M4 trace).
- Counts are authoritative: one `ExecutionMetrics` shared by `MeteredModelClient`, `ToolRegistry`
  and `RunState` (`test_counts_come_from_shared_metrics`, `WiringTest`).
- Context usage controlled: the planner gets the bounded working set (204-file repository → 1,501-char
  planner message; `test_planner_receives_bounded_working_set_not_the_repository` with a
  3,000-char limit), and executor requests are windowed and stop growing
  (`test_execution_requests_stay_bounded`).
- Command timeout wired from configuration (`test_command_timeout_comes_from_configuration`).

Evidence (M3, 2026-09-26):
- `DiscoveryMetrics` report inventory size, content searches, files matched, files read
  (analysis vs discovery), bytes, candidates, selected files and working-set size. Example
  (303-file fixture): 3 content searches, 2 files matched, 2 config + 1 candidate files read
  (328 bytes), 2 candidates, 1,007 chars.
- Expensive operations avoided, partially: keyword search is skipped when identifier search
  already found a definition; keywords present in ≥ max(20, 30%) of files are ignored; import
  expansion is one hop from 5 files; command discovery never executes anything. Nothing here
  concerns test execution or model calls yet.
- Context usage stays partial: the working set is bounded and tested, but the model-call
  context (turn history) does not exist yet.

Evidence (M2, 2026-09-26):
- `ExecutionMetrics` (src/harness/metrics.py). `MeteredModelClient` counts every `generate()`
  attempt including failures, plus reported tokens (tests/test_model.py). `ToolRegistry` counts
  every dispatch including failures; `command_calls` counts only launched commands
  (tests/test_registry.py, tests/test_commands.py::test_counters).
- Context usage partial: tool outputs are bounded (read, search, list, command and diff caps
  in `ToolLimits`). No context manager or compaction exists.

---

# Mandatory submission requirements

## R9 — Makefile

- [x] root Makefile exists
- [x] make setup works
- [~] make run works
- [x] make test works
- [x] make clean works

Evidence (M1, 2026-09-26):
- `make clean` → exit 0; `make setup` → exit 0, run twice (idempotent); `make test` → 35 tests OK.
- Fresh copy of tracked files, Python 3.10.19: setup=0 test=0 run=0 clean=0.
- Fresh copy, Python 3.12 with `PIP_NO_INDEX=1` (simulated offline): setup=0 (warns that the
  console script is unavailable) test=0 run=0 clean=0.
- `make run` partial: launches interactively (verified through a real pty) and validates
  config/input. M4: it then stops with "No model provider is configured …" or "Configured model
  provider is not supported by this build …" (exit 2, nothing modified), because no live adapter
  exists. The run path behind it is tested with an injected model (`RunExecutionTest`).
- M2 fix (2026-09-26): `make test` had started failing because Python 3.14 skipped the venv's
  `.pth` files after macOS set the "hidden" flag on `.venv`. Targets now set `PYTHONPATH=src`.
  Re-verified: `make clean && make setup && make test` → 0/0/0.

---

## R10 — API credential

- [x] reads AI_API_KEY
- [x] no hardcoded credentials
- [x] .env.example contains no real credential

Evidence (M5, 2026-09-26, additional):
- With a fake key in the environment, the M5 trace across 11 scenarios found it in no RunState,
  report, model request or evidence item.

Evidence (M4, 2026-09-26, additional):
- ScriptedModel runs need no key (`AI_API_KEY` removed in `OrchestratorCase`). With a fake key set
  in the environment, it appears nowhere in RunState, its summary or any model request (M4
  trace), nor in `make run` output. The run's redactor also covers model-provided summaries.

Evidence (M3, 2026-09-26, additional):
- `harness inspect` and `discover_for_task` never load the key (tests/test_inspect_cli.py,
  tests/test_discovery.py::test_no_api_key_needed); if the key is set, `inspect` still redacts it
  from repository content it prints (`test_key_in_repository_content_is_redacted`).

Evidence (M2, 2026-09-26, additional):
- Child processes run with `AI_API_KEY` removed from their environment; the tool registry
  redacts the key from every result and error string (tests/test_secrets.py).
- Credential-pattern scan over all tracked and new files: no matches.

Evidence (M1, 2026-09-26):
- `src/harness/config.py` reads `AI_API_KEY` from the environment, then `.env`; missing or blank
  key → exit 2 with an actionable message (tests/test_config.py, tests/test_cli.py; `make run`
  without the key → exit 2).
- The key is excluded from `repr(Config)` and redacted from CLI output; every CLI test asserts
  the key never appears in stdout/stderr. Six manual `make run`/`harness` runs: key never printed.
- Credential-pattern grep over all tracked files: no matches. The only key-like strings are the
  test fixture value `test-key-7f3a9c1e5b`.
- `test_env_example_has_no_credential` asserts `AI_API_KEY` and model fields are empty in `.env.example`.
- Re-run the credential scan before submission.

---

## R11 — Model

- [~] text-only
- [~] model configurable
- [x] provider abstraction exists

Evidence (M4, 2026-09-26):
- `model/factory.create_model_client` is the single construction boundary; it fails explicitly for
  an unset or unsupported provider and ships no adapter (tests/test_model_factory.py). The
  orchestrator depends only on `ModelClient`. Still partial: no live model.

Evidence (M2, 2026-09-26):
- `ModelClient.generate(ModelRequest) -> ModelResponse` with provider-neutral frozen dataclasses
  (src/harness/model/). Verified with `ScriptedModel`, offline, no API key (tests/test_model.py).
  No live provider adapter exists: provider/model are still unannounced.
- Text-only partial: message content is text only (no image/audio types), but no live model
  has been called.

Evidence (M1, 2026-09-26):
- `AI_MODEL_PROVIDER`, `AI_MODEL`, `AI_BASE_URL` are loaded and validated
  (test_model_settings_come_from_environment, test_invalid_base_url_is_rejected). Partial: no
  model client consumes them yet. Defaults are unset until the organizers announce the model.

---

## R12 — Reproducibility

- [~] clean environment setup tested
- [x] dependencies declared
- [~] evaluator does not need undocumented commands

Evidence (M1, 2026-09-26):
- Fresh copies of the tracked files set up and tested on Python 3.10.19 and 3.12.13
  (offline-simulated). Partial: only on the developer's macOS, not in an evaluator-like environment.
- `pyproject.toml`: `requires-python = ">=3.10"`, `dependencies = []` (stdlib only). setuptools
  is needed only for the optional console script, and `make setup` works without it.
- README documents `export AI_API_KEY`, `make setup`, `make run`. Partial: the full run flow
  does not exist yet.
- M2 (2026-09-26): fresh copies pass `make setup && make test` (146 tests) on Python 3.10.19,
  on Python 3.12 with `PIP_NO_INDEX=1`, and with ripgrep absent from PATH.
- M3 (2026-09-26): same three fresh-copy runs with 234 tests: all pass (2 ripgrep-only tests
  skipped without ripgrep). Still no runtime dependencies.
- M4 (2026-09-26): same runs with 303 tests: all pass on Python 3.10.19, offline 3.12, and
  without ripgrep (2 skipped).
- M5 (2026-09-26): same runs with 338 tests: all pass (2 ripgrep-only tests skipped without
  ripgrep). Still no runtime dependencies.

---

# Final acceptance

Submission is ready ONLY when every mandatory requirement above is:

[x]

and has evidence.

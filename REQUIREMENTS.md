# Hackathon Requirement Matrix

Status:
- [ ] not implemented
- [~] partially implemented
- [x] verified

---

## R1 — Autonomous software engineering harness

- [~] Accept software engineering task
- [ ] Understand task
- [~] Inspect existing repository
- [~] Determine relevant files
- [ ] Modify implementation
- [ ] Verify modifications

Evidence:
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
- [~] Uses deterministic search before model calls
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
- Search before model calls: partial. Discovery is deterministic and needs no model or API key
  (`test_no_api_key_needed`, `inspect` run with the key unset), but the orchestrator that runs it
  before the first model call does not exist yet (M4).

Evidence (M2):
- M2 (2026-09-26): primitives only. `find_files` (glob) and `search_text` (ripgrep with an
  identical pure-Python fallback) return structured, bounded, deterministic results
  (tests/test_file_tools.py, tests/test_search.py). No repository analyzer, ranking or
  "search before model" ordering exists yet.

---

## R3 — Orchestration

- [ ] Explicit execution lifecycle exists
- [ ] Plan
- [ ] Execute
- [ ] Verify
- [ ] Repair
- [ ] Finish/terminate

Evidence:
TBD

---

## R4 — Context management

- [x] Working context separated from permanent context
- [x] Relevant files/snippets selected
- [ ] Old observations can be summarized/compacted
- [~] Repeated unnecessary context avoided

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
- [~] Run tests
- [x] Inspect git diff

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

- [ ] Failed test detected
- [ ] Failure classified
- [ ] Repair/replan path exists
- [ ] Retry limit exists
- [ ] Infinite loops prevented

Evidence:
TBD

---

## R7 — Verification

- [ ] Tests are executed
- [ ] Git diff inspected
- [ ] Acceptance criteria checked
- [ ] Success based on evidence
- [ ] Failed verification triggers repair

Evidence:
TBD

---

## R8 — Efficiency

- [x] Model calls tracked
- [x] Tool calls tracked
- [~] Context usage controlled
- [ ] Targeted tests before full suite
- [~] Expensive operations avoided when unnecessary

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
  config/input, but the agent itself is not implemented yet.
- M2 fix (2026-09-26): `make test` had started failing because Python 3.14 skipped the venv's
  `.pth` files after macOS set the "hidden" flag on `.venv`. Targets now set `PYTHONPATH=src`.
  Re-verified: `make clean && make setup && make test` → 0/0/0.

---

## R10 — API credential

- [x] reads AI_API_KEY
- [x] no hardcoded credentials
- [x] .env.example contains no real credential

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

---

# Final acceptance

Submission is ready ONLY when every mandatory requirement above is:

[x]

and has evidence.

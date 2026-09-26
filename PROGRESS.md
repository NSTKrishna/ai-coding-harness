# Implementation Progress

Last updated: 2026-09-26 (M3 complete and verified; M2 and M3 not committed)

## Current milestone

**M3 — Repository intelligence and bounded working context: DONE, verified.**
M4 has not started and waits for explicit go-ahead.

## Current repository state

- Branch `main`. HEAD `34d0896` (M1). **There is no M2 commit.** M2 and M3 are both uncommitted
  in the working tree (the project rules forbid automatic commits). Before M3 started, the M2
  working tree was copied to `<scratchpad>/m2-baseline` (39 files) so that M3's changes could
  be listed separately (below).
- Built:
  - **M1:** CLI (`harness run`), config, Makefile, packaging
  - **M2:** model interface (`ModelClient`, `ScriptedModel`, `MeteredModelClient`), `ExecutionMetrics`,
    `ToolRegistry` + 11 tools
  - **M3:** `harness/repo/` (inventory, classification, facts, profile, command discovery, task
    signals, candidate discovery/ranking, report), `harness/context/` (`WorkingSet`,
    `ContextManager`), `harness inspect` CLI, `ContextLimits` configuration
- **Not built:** live provider adapter, planner, executor/action protocol, orchestrator/state
  machine, verification engine, evidence ledger, failure recovery, context compaction, run
  report. `harness run` still only validates input and exits.

### M3 changes relative to the M2 snapshot

- New: `src/harness/repo/{__init__,classify,inventory,reader,facts,commands,profile,signals,discovery,report}.py`,
  `src/harness/context/{__init__,working_set,manager}.py`, `tests/repo_fixtures.py`, and 8 test
  modules (`test_repo_inventory`, `test_repo_profile`, `test_task_signals`, `test_discovery`,
  `test_working_set`, `test_context_manager`, `test_command_discovery`, `test_inspect_cli`).
- Modified: `src/harness/cli.py` (`inspect` subcommand), `src/harness/config.py` (`ContextLimits`,
  `load_context_limits`, key-free settings reader), `src/harness/tools/search.py`,
  `src/harness/tools/git.py`, `tests/test_search.py`, `arch.md` (§3, §6–§8, §11, §17, §20–§22),
  `README.md`, `.env.example`.
- M2 changes made in M3, both backwards compatible, both tested:
  - `search()` gained `allowed_paths` (Python API only; not a tool argument). In scoped mode
    ripgrep still walks the directory and results are filtered, because ripgrep searches
    explicitly named files even when they are binary or over `--max-filesize`. Passing them
    explicitly broke engine parity (observed, then fixed).
  - **M2 defect fixed:** the Python engine split lines with `str.splitlines()`, which also
    breaks on form feeds and U+2028, so line numbers could differ from ripgrep. It now splits
    on `\n` only (`test_line_numbers_split_on_newline_only`).
  - `tools/git.py` gained `git_work_tree_prefix` and `git_list_files` (read-only `ls-files`;
    raises instead of returning a truncated listing).

## Gap analysis (after M3)

REQUIREMENTS.md holds the per-item evidence. Items not listed are MISSING.

| Req | Item | Status |
|---|---|---|
| R1 | Accept task | PARTIAL |
| R1 | Inspect repository; determine relevant files | PARTIAL (components verified; not used by `run`) |
| R1 | Understand task; modify; verify | MISSING |
| R2 | Analyzer exists; targeted discovery; no blind loading | IMPLEMENTED AND VERIFIED |
| R2 | Deterministic search before model calls | PARTIAL (no orchestrator to order it yet) |
| R3 | Lifecycle (all) | MISSING |
| R4 | Working vs permanent context; snippets selected | IMPLEMENTED AND VERIFIED |
| R4 | Repeated context avoided | PARTIAL (dedup of identical items only) |
| R4 | Compaction | MISSING |
| R5 | Read, search, patch, command, git diff | IMPLEMENTED AND VERIFIED |
| R5 | Run tests | PARTIAL (commands discovered + runnable; not connected) |
| R6 | Failure recovery (all) | MISSING |
| R7 | Verification (all) | MISSING |
| R8 | Model/tool calls tracked | IMPLEMENTED AND VERIFIED |
| R8 | Context usage controlled; expensive ops avoided | PARTIAL |
| R8 | Targeted tests before full suite | MISSING |
| R9 | Makefile setup/test/clean | IMPLEMENTED AND VERIFIED; `make run` PARTIAL |
| R10 | Credentials (all) | IMPLEMENTED AND VERIFIED |
| R11 | Provider abstraction | IMPLEMENTED AND VERIFIED; text-only, configurable PARTIAL |
| R12 | Dependencies declared | IMPLEMENTED AND VERIFIED; others PARTIAL |

## Missing P0 components

1. ~~Scaffolding, Makefile, config~~ (M1)
2. ~~Model interface, fake model, accounting~~ (M2)
3. ~~Tool layer + registry~~ (M2)
4. ~~Repository intelligence~~ (M3)
5. Context: ~~working set, permanent/working/episodic stores~~ (M3); compaction and the
   turn-history context for model calls are still missing (R4, R8)
6. Orchestrator: `RunState`, state machine, budgets enforced, planner, executor/action protocol (R1, R3, R6)
7. Verification engine + evidence ledger + failure classification (R6, R7)
8. Run report / telemetry output (R8)
9. E2E fixture runs with the scripted model (R1, R7, R12)
10. Live provider adapter, once announced (R11)

## Dependency ordering

```text
repo intelligence + working set (done) ─┐
model interface + tools (done) ─────────┼─▶ 6 orchestrator/planner/executor ─▶ 8 report ─▶ 9 E2E ─▶ 10 live adapter
tools (done) ─▶ 7 verifier + ledger ────┘
```

## Next 3 implementation milestones

### M4 — Orchestrator, planner and executor (R1, R3, R6 partial)

- `RunState`, state machine (INIT → UNDERSTAND → DISCOVER → PLAN → EXECUTE → VERIFY → REPAIR →
  SUCCEEDED/FAILED/ABORTED) with `max_steps`/`max_repair_cycles` enforced
- DISCOVER = `discover_for_task` + baseline test run with the top test command
- Planner and executor over `ToolRegistry` + `MeteredModelClient`; text action protocol for
  models without native tool calls; `ContextManager` feeds prompts
- Done when: `ScriptedModel` drives fixture repos through every state, and loop/limit tests
  prove termination.

### M5 — Verification, evidence, recovery (R6, R7, R8)

- Layered verification (targeted tests, then full suite; baseline regression check; diff
  review via `git_diff`/`git_diff_stat`), evidence ledger, failure classes and signatures, replan
- Done when: E2E covers clean fix, fail → repair → pass, and stop-at-limit.

### M6 — CLI integration, run report, compaction, submission hardening (R4, R8, R9, R12)

- `make run` performs a real run; run directory with `events.jsonl`, `state.json`, `report.md`
- Deterministic compaction of old observations; `ToolLimits` wired to configuration; live adapter
  once the provider is announced; clean-environment rehearsal

## Completed

- M1 (2026-09-26, `34d0896`): scaffolding, Makefile, config, CLI.
- M2 (2026-09-26, uncommitted): model interface, tools, registry, metrics; Makefile `.pth` fix.
- M3 (2026-09-26, uncommitted): repository intelligence, working set, context manager, `inspect`;
  search scope extension and line-splitting fix.

## Currently implementing

Nothing. Awaiting go-ahead for M4.

## Verified — M3 evidence

All on macOS (Darwin 25.6.0), from the project root. Scripts: `<scratchpad>/verify_m3.sh`
(build/test matrix). Fresh copies come from `git ls-files -co --exclude-standard`.

| # | DoD item | Command / test | Result |
|---|---|---|---|
| 1 | M1/M2 tests pass | `make test`: config 14, cli 21, model 19, registry 14, file_tools 19, search 14 (9 M2 + 5 M3), patch 14, git_tools 9, commands 23, secrets 4 | OK |
| 2 | Git ignore behavior at intelligence layer | `GitInventoryTest` (7): exact inventory `(.gitignore, src/app.py, untracked_note.py)` from a repo containing gitignored, `.venv`, `node_modules`, `dist`, `__pycache__` files; `test_gitignored_files_are_never_candidates` | OK |
| 3 | Non-git fallback | `FilesystemInventoryTest`, `NonGitProfileTest`, `NonGitDiscoveryTest` | OK |
| 4 | RepoProfile | `tests.test_repo_profile` (7): Python/TS/Go/non-git/config recognition/empty | OK |
| 5 | Deterministic task signals | `tests.test_task_signals` (8), including the brief's example | OK |
| 6 | Ranked with reasons | `FixtureRankingTest` (4), `RankingRulesTest` (7): explicit path first, identifier beats keyword, reasons on every candidate | OK |
| 7 | Source/test influence | `PairingKeyTest`, `PairingBoostTest` (test file linked only by naming convention is boosted) | OK |
| 8 | Discovery bounded | `test_discovery_reads_few_files` (303 files: ≤ 8 candidate reads, < 5% read in total), `test_common_keywords_are_ignored` | OK |
| 9–10 | Evidence and WorkingSet bounded | `tests.test_working_set` (10): files, evidence count, chars for budgets 80–10 000, snippet lines, deterministic order | OK |
| 11 | ContextManager | `tests.test_context_manager` (10) | OK |
| 12–13 | No model, no key | `test_no_api_key_needed`, `test_profile_without_api_key`, inspect subprocess with `AI_API_KEY` removed | OK |
| 14 | Test commands with evidence | `tests.test_command_discovery` (9): unittest, pytest (explicit vs referenced), repo venv, npm/yarn + placeholder, Go, Cargo, Makefile, no command for uncertain repositories | OK |
| 15 | inspect CLI | `tests.test_inspect_cli` (7), plus manual runs below | OK |
| 16 | No repository mutation | `test_repository_is_not_modified`, `test_repository_unchanged`; manual inspect runs: sha256 of every file incl. `.git` identical before/after (python 51 files, typescript 42, go 40, large 934) | OK |
| 17 | Selective exploration | metrics below | OK |
| 18 | Clean/offline setup | working tree `make clean && make setup && make test` 0/0/0 (234 OK); fresh Python 3.10.19 0/0 (234 OK); fresh offline `PIP_NO_INDEX=1` 3.12 0/0 (234 OK, 1 offline warning); fresh without ripgrep 0/0 (234, 2 skipped) | OK |
| — | Credentials | credential-pattern grep over tracked + untracked files: no matches; only fixture `test-key-7f3a9c1e5b` | OK |

**Test count: 234** (146 M1+M2, 88 M3). Suite time ≈ 12 s.

### Discovery metrics from `harness inspect` (API key unset)

| Fixture | Top candidates | Metrics |
|---|---|---|
| Python, "Fix PaymentService refresh_token behavior" | service.py 120, test_service.py 59, tokens.py 27 | 10 files; 2 content searches → 3 files; read 3 config + 5 candidate files (1,019 B); 6 candidates; 2,328 / 24,000 chars; keyword search not needed |
| TypeScript, "token refresh fails when the access token has expired" | token.test.ts 69, token.ts 65, session.ts 16 | 7 files; 8 searches → 3 files; 1 + 3 files read (755 B); 3 candidates; 1,546 chars; keyword search run |
| Go, "ParseConfig returns an error for empty files" | parser.go 76, parser_test.go 59, cmd/app/main.go 20 | 5 files; 1 search → 3 files; 0 + 4 files read (390 B); 3 candidates; 1,431 chars; keywords skipped (definition found) |
| Large (303 files), "InvoiceParser.parse_total mishandles commas" | invoice_parser.py 133, test_invoice_parser.py 107 | 303 files; 3 searches → 2 files; 2 config + 1 candidate file read (328 B); 2 candidates; 1,007 chars |

Not verified: Linux, Python 3.11/3.13, the evaluator environment, real-world large repositories.

## Known issues / limitations

- Ranking is heuristic. In the TS fixture the test file scores slightly above the source
  (69 vs 65) because it mentions more task keywords; both are top 2 as required.
- Definition detection uses per-language regexes on matching lines; it can miss unusual
  definitions and occasionally treat an assignment as a definition. Python `ast` outlines remain
  planned (arch.md §6).
- Keyword stemming is crude (`retried` → `retr`). It only feeds case-insensitive substring
  search, where low weights limit the damage.
- Profile facts come from root-level configuration only; nested projects (monorepos) are listed
  as manifests but not interpreted.
- Test commands use the repository's `.venv/bin/python`/`venv/bin/python` when present, else a
  bare `python`, which may not exist (e.g. macOS has only `python3`). M4 must choose the
  interpreter.
- From M2: the command policy is a guardrail, not a sandbox; cosmetic `make` error echo.

## Notes for M4

- **Entry point:** `discover_for_task(repo, task, limits=config.context)` returns
  `DiscoveryResult(repo_profile, task_signals, candidates, working_set, metrics, warnings)`.
  Use `working_set.render()` as the planner's repository context (bounded, deterministic).
- **Shared context:** pass one `ToolContext` (`discover_for_task(..., ctx=ctx)`) so discovery and
  tools share root and limits. `RepoReader` and `DiscoveryMetrics` are separate from
  `ExecutionMetrics` by design.
- **Context manager:** `ContextManager(task, profile.summary(), config.context)`, then
  `load_working_set(ws)`, then add tool observations with `add_working_item` and priorities.
  Call `remove_source(path)` after `apply_patch` changes a file. Facts go through `record_fact`.
- **Test commands:** `profile.test_commands` is ordered, high confidence first, and each entry
  has a reason. None may exist: M4 must handle "no test command" explicitly (never invent one).
  The interpreter question above applies.
- **`ToolLimits` is still not wired to `Config`** (M2 note stands).
- **Inventory scope:** use it for any further searches by passing
  `allowed_paths=[f.path for f in inventory.files]`, so gitignored files stay out.
- **No M2 checkpoint commit exists.** Commit M2 and M3 (together or separately) before M4, so
  M4's diff can be reviewed on its own.

## Important architectural decisions

- (M3) Repository intelligence owns scope. The search primitive stays generic, and scoping goes
  through `allowed_paths`.
- (M3) Git inventory = `ls-files --cached` + `--others --exclude-standard`, minus noise
  directories; filesystem fallback outside git. `stat` only.
- (M3) Configuration facts are read with line-based/stdlib parsers (no `tomllib`), so results
  are the same on Python 3.10 and 3.14.
- (M3) Commands are proposed only with explicit configuration evidence and are never executed
  during analysis.
- (M3) Ranking is additive with documented weights (arch.md §7); every point carries a reason,
  and ties break by path.
- (M3) Progressive discovery: names → identifiers → keywords only if needed; common keywords
  dropped; one-hop imports.
- (M3) Working-set limits are enforced on the exact rendered text, and omissions are counted.
- (M3) `inspect` never loads the API key (`load_context_limits`).
- (M2) Pure-Python patch applier, no implicit shell, read-only git, single path boundary,
  provider-neutral model types, counting semantics (arch.md §9, §11, §13, §19, §25).
- (M1) Python ≥ 3.10, stdlib only at runtime, `unittest`, `PYTHONPATH=src` in make targets.

## Commands

```sh
export AI_API_KEY="..."
make setup        # idempotent, works offline
make run          # interactive; still validates and exits
make test         # 234 tests, offline, no key needed
make clean

PYTHONPATH=src .venv/bin/python -m harness inspect --repo PATH [--task "TEXT"] [--top N] [--show-context]
```

## Last successful test

2026-09-26: `make clean && make setup && make test` gave 234 tests OK on Python 3.14.3; also 234 OK
on 3.10.19, on 3.12.13 offline, and without ripgrep (2 skipped).

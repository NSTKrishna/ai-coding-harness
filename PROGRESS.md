# Implementation Progress

Last updated: 2026-09-26 (M1 complete and verified)

## Current milestone

**M1 — Runnable skeleton and submission contract: DONE, verified.**
M2 has not started and waits for explicit go-ahead.

## Current repository state

- Git repository on branch `main`.
- `arch.md` repaired: 26 sections (problem → definition of completion) with all code fences
  closed. Sections mark what is built **[M1]** and what is **[planned]**.
- Built in M1:

  ```text
  .gitignore  .env.example  Makefile  pyproject.toml  README.md
  src/harness/__init__.py  __main__.py  cli.py  config.py
  tests/__init__.py  test_config.py  test_cli.py
  ```

- **Not built yet:** model client, tools, repository intelligence, context manager,
  orchestrator, verifier, evidence ledger, telemetry. The CLI states this explicitly on every run
  and makes no model call.

## Gap analysis (after M1)

Everything not listed here is still **MISSING**. REQUIREMENTS.md holds the per-item evidence.

| Req | Item | Status |
|---|---|---|
| R1 | Accept software engineering task | PARTIAL (accepted + validated, not acted on) |
| R9 | Makefile exists; setup / test / clean work | IMPLEMENTED AND VERIFIED |
| R9 | `make run` works | PARTIAL (launches + validates; no agent yet) |
| R10 | Reads `AI_API_KEY`; no hardcoded credentials; clean `.env.example` | IMPLEMENTED AND VERIFIED |
| R11 | Model configurable | PARTIAL (config fields only; nothing consumes them) |
| R11 | Text-only; provider abstraction | MISSING |
| R12 | Dependencies declared | IMPLEMENTED AND VERIFIED |
| R12 | Clean environment setup tested | PARTIAL (fresh copies on dev macOS, Python 3.10 + offline 3.12) |
| R12 | No undocumented evaluator commands | PARTIAL (README documents flow; full flow not built) |
| R2–R8 | All items | MISSING |

Note: `max_repair_cycles` and `max_steps` exist as config values, but nothing enforces them yet,
so R6 "retry limit exists" stays MISSING.

## Missing P0 components

1. ~~Project scaffolding~~ (M1)
2. ~~Makefile~~ (M1)
3. ~~Config loader~~ (M1)
4. Model interface: `ModelClient.generate()`, fake model, adapter slot, call accounting (R11, R8)
5. Tool layer + registry: read_file, search, apply_patch, run_command, run_tests, git_status/git_diff (R5, R8)
6. Repository intelligence: file index, profile, ranked deterministic search (R2)
7. Context manager: permanent/working split, compaction, dedup (R4, R8)
8. Orchestrator state machine + RunState + budgets (R1, R3, R6)
9. Verification engine + evidence ledger + failure classification (R6, R7)
10. Run report / telemetry output (R8)
11. End-to-end fixture repos + fake-model E2E tests (R1, R7, R12)
12. First real provider adapter, once the organizers announce the model (R11)

## Dependency ordering

```text
config (done) ─▶ 4 model interface ─┐
                                    │
scaffold (done) ─▶ 5 tools ─▶ 6 repo intelligence ─▶ 7 context ─┐
                     │                                          │
                     └─▶ 9 verifier + ledger ◀──────────────────┤
                                  │                             │
                                  └────▶ 8 orchestrator ◀───────┘
                                              │
                                              └─▶ 10 report ─▶ 11 E2E ─▶ 12 live adapter
```

## Next 3 implementation milestones

### M2 — Model interface, tools, and accounting (R5, R8-tracking, R11)

- `harness/model/`: `ModelClient` protocol, `ModelRequest`/`ModelResponse`, `FakeModelClient`,
  adapter registry keyed by `config.model.provider` (no real adapter until announced)
- `harness/tools/`: registry with arg validation, read/write/exec categories, path confinement,
  output caps, timeouts, and API key scrubbed from child environments
- Tools: list_files, search (rg + Python fallback), read_file, apply_patch (unified diff +
  replace block, atomic, snapshots), run_command, run_tests, git_status, git_diff
- Call counters for model and tools
- Done when: every tool has unit tests against a temp git repo; confinement and env-scrub tests pass;
  counters asserted.

### M3 — Repository intelligence, context, orchestrator loop (R1–R4, R6)

- Repo profile, file index, candidate ranking, Python `ast` symbols
- Context manager with budgets, compaction and dedup
- RunState + state machine with step/repair limits enforced; action-protocol parser
- Done when: the fake model drives a fixture repo through all states, and a loop test proves termination.

### M4 — Verification, evidence, recovery, report (R6, R7, R8)

- Baseline tests, layered verification, regression check, diff review, criteria mapping
- Evidence ledger, failure classes + signatures, replan path
- Run directory with `events.jsonl`, `state.json`, `report.md`
- Done when: E2E tests cover clean fix, fail → repair → pass, and stop-at-limit.

## Completed

- M1 (2026-09-26): git init, arch.md repair, scaffolding, Makefile, config loader, CLI, tests.

## Currently implementing

Nothing. Awaiting go-ahead for M2.

## Verified — M1 evidence

All commands run from the project root on macOS (Darwin 25.6.0) unless stated otherwise.

| # | Check | Command | Result |
|---|---|---|---|
| 1 | Git repo | `git init && git branch -m main` | initialized |
| 2 | Clean | `make clean` | exit 0 |
| 3 | Setup + idempotency | `make setup` twice (`AI_API_KEY` unset) | exit 0 both; `harness 0.1.0 ready on Python 3.14.3` |
| 4 | Tests | `make test` | `Ran 35 tests … OK`, exit 0 |
| 5a | Interactive run | `printf "$REPO\nFix the date parser\nsee issue #12\n\n" \| AI_API_KEY=… make run` | exit 0, both prompts shown, `Task source: prompt` |
| 5b | Interactive on a real terminal | Python `pty.fork()` driving `make run`, typing after each prompt | exit 0, `Task source: prompt`, key not in output |
| 6 | Missing key | `env -u AI_API_KEY make run </dev/null` | exit 2, `error: AI_API_KEY is not set.` + how to set it; stdout empty (no prompt shown) |
| 7a | CLI args | `.venv/bin/python -m harness run --repo $REPO --task "Add retry"` | exit 0 |
| 7b | Task file + console script | `.venv/bin/harness run --repo $REPO --task-file issue.md` | exit 0, 3-line task |
| 7c | make convenience | `make run ARGS="--repo $REPO --task 'Add retry'"` | exit 0 |
| 8 | No secret in output | grep of the fake key across all captured stdout/stderr of 5a–7c | no occurrence |
| 9 | No credentials in tracked files | credential-pattern `grep -E` over `git ls-files` | no matches; only fixture `test-key-7f3a9c1e5b` in tests |
| 10 | Fresh copy, min Python | tracked files copied to scratch dir; `make setup PYTHON=python3.10`, test, run, run-without-key, clean | 0 / 0 / 0 / 2 / 0 (Python 3.10.19) |
| 11 | Fresh copy, offline | same with `PIP_NO_INDEX=1`, Python 3.12.13 | 0 / 0 / 0 / 2 / 0; setup warns that the console script is unavailable |

Not verified: Python 3.11 and 3.13, Linux, the real evaluator environment.

## Known failures / issues

- None failing.
- Cosmetic: when `make run` exits non-zero, make appends `make: *** [run] Error 2` after the
  harness's own error message.
- Cosmetic: `--task-file` is echoed as given (e.g. with `..`), not normalized.
- Test-method note: macOS `script -q` with piped stdin delivers immediate EOF to the child, so it
  cannot test interactive input; use `pty.fork()` instead (5b).

## Important architectural decisions

- Python **≥ 3.10** (was 3.11+; relaxed for portability, no 3.11-only features needed). Standard
  library only at runtime.
- Tests use stdlib `unittest`; `make test` needs no downloads.
- `make setup` exposes `src/` through a `.pth` file (offline-safe), then *tries* an editable
  install for the `harness` console script and only warns if that fails.
- `make run`/`make test` depend on a setup stamp file (`.venv/.harness-setup`) and run setup if
  it is missing.
- Config precedence: environment → `.env` in the working directory → defaults. Empty values count
  as unset. Only `AI_API_KEY` is required.
- Organizer-prescribed provider/model/base URL: marked block at the top of `config.py`, currently
  `None`. No provider has been assumed.
- The API key is excluded from `repr`, redacted from CLI output, and never included in error messages
  (`.env` parse errors report line numbers only).
- The config loads before any prompt, so a missing key fails before the user types anything.
- `harness` with no subcommand prints help and exits 2. Exit codes: 0 ok, 2 usage/config/input,
  130 interrupted (1 reserved for "verification failed").
- The task prompt is multi-line and ends at an empty line or end-of-input, so pasted issues work.
- State-machine orchestrator, patch-based editing, provider-independent model interface,
  deterministic search before model reasoning (unchanged; detailed in arch.md).
- The harness never commits in the target repository.

## Commands

```sh
export AI_API_KEY="..."
make setup        # idempotent; PYTHON=python3.x to choose the interpreter
make run          # interactive
make test
make clean

.venv/bin/python -m harness run --repo PATH --task "TEXT"
.venv/bin/python -m harness run --repo PATH --task-file FILE
```

## Last successful test

2026-09-26 — `make test`: 35 tests, OK (Python 3.14.3; also 3.10.19 and 3.12.13 in fresh copies).

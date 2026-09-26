# Hackathon Requirement Matrix

Status:
- [ ] not implemented
- [~] partially implemented
- [x] verified

---

## R1 — Autonomous software engineering harness

- [~] Accept software engineering task
- [ ] Understand task
- [ ] Inspect existing repository
- [ ] Determine relevant files
- [ ] Modify implementation
- [ ] Verify modifications

Evidence:
- M1 (2026-09-26): `harness run` accepts the task via `--task`, `--task-file` or an
  interactive prompt, and validates repo path + task (tests/test_cli.py, 21 tests;
  real-pty run of `make run`). Partial: the task is accepted and validated, not yet acted on.

---

## R2 — Repository navigation

- [ ] Repository analyzer exists
- [ ] Uses deterministic search before model calls
- [ ] Supports targeted file discovery
- [ ] Does not blindly load entire repository

Evidence:
TBD

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

- [ ] Working context separated from permanent context
- [ ] Relevant files/snippets selected
- [ ] Old observations can be summarized/compacted
- [ ] Repeated unnecessary context avoided

Evidence:
TBD

---

## R5 — Tool use

- [ ] Read file
- [ ] Search repository
- [ ] Apply patch
- [ ] Execute command
- [ ] Run tests
- [ ] Inspect git diff

Evidence:
TBD

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

- [ ] Model calls tracked
- [ ] Tool calls tracked
- [ ] Context usage controlled
- [ ] Targeted tests before full suite
- [ ] Expensive operations avoided when unnecessary

Evidence:
TBD

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

---

## R10 — API credential

- [x] reads AI_API_KEY
- [x] no hardcoded credentials
- [x] .env.example contains no real credential

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

- [ ] text-only
- [~] model configurable
- [ ] provider abstraction exists

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

---

# Final acceptance

Submission is ready ONLY when every mandatory requirement above is:

[x]

and has evidence.

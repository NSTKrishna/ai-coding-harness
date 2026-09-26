# Coding Harness

An autonomous software-engineering harness around a text-only foundation model.
Architecture: [arch.md](arch.md). Status: [PROGRESS.md](PROGRESS.md).

> Current state: milestone M6 (final). A run goes discovery → plan → baseline (targeted test
> first, then the discovered suite, before any edit) → one-action-per-step execution with
> deterministic context compaction → evidence-based verification → bounded repair with a
> repeated-failure stop rule → VERIFIED only on strong evidence. Every run writes artifacts
> (`harness runs`, `harness report <id>`). It is exercised end to end with a scripted model;
> no live model adapter exists yet (the provider has not been announced), so `make run`
> accepts input and then stops with a precise "provider not supported" message.

## Requirements

- Python 3.10 or newer (`python3` on PATH, or pass `PYTHON=python3.x` to `make setup`)
- `make`, `git`
- Optional: `rg` (ripgrep) for faster search; a pure-Python fallback is used when it is absent
- No other system packages, no Docker, no Node. No runtime Python dependencies.

## Quick start

```sh
export AI_API_KEY="..."
make setup
make run
```

`make run` prompts for the repository path and the task (end the task with an
empty line). It then needs a model adapter for `AI_MODEL_PROVIDER`; this build
has none, so it exits with code 2 and says so. Nothing is modified. You can also
pass the input directly:

```sh
make run ARGS='--repo /path/to/repo --task "Fix the failing date parser test"'
make run ARGS='--repo /path/to/repo --task-file issue.md'
PYTHONPATH=src .venv/bin/python -m harness run --repo /path/to/repo --task-file issue.md
```

`.venv/bin/harness` (installed when the editable install succeeds) and plain
`.venv/bin/python -m harness` also work, unless Python skips the venv's `.pth`
files (Python ≥ 3.13 skips files carrying the macOS "hidden" flag). Setting
`PYTHONPATH=src`, as the Makefile does, always works.

## Inspecting a repository (no API key needed)

```sh
PYTHONPATH=src .venv/bin/python -m harness inspect --repo /path/to/repo
PYTHONPATH=src .venv/bin/python -m harness inspect --repo /path/to/repo \
    --task "Fix PaymentService refresh_token behavior" [--top 10] [--show-context]
```

Shows the repository profile (languages, manifests, test/build commands with the
evidence for each) and, with a task, the ranked candidate files with reasons and
discovery metrics. Makes no model call and does not modify the repository.

## Run artifacts (no API key needed)

Each `harness run` writes `events.jsonl`, `summary.json`, `final_report.md` and (for git
repositories) `final.diff` to `.harness/runs/<run-id>/` in this checkout (or
`HARNESS_RUNS_DIR`); never into the target repository.

```sh
PYTHONPATH=src .venv/bin/python -m harness runs
PYTHONPATH=src .venv/bin/python -m harness report <run-id> [--json]
```

## Configuration

Set in the environment, or in a `.env` file in the directory you run from
(copy [.env.example](.env.example)). The environment wins over `.env`.

| Variable | Required | Default |
|---|---|---|
| `AI_API_KEY` | yes | — |
| `AI_MODEL_PROVIDER` | no | unset until organizers announce it |
| `AI_MODEL` | no | unset until organizers announce it |
| `AI_BASE_URL` | no | unset until organizers announce it |
| `HARNESS_MAX_STEPS` | no | 40 |
| `HARNESS_MAX_REPAIR_CYCLES` | no | 3 (0 = verify, never repair) |
| `HARNESS_MAX_REPEATED_FAILURE_CYCLES` | no | 2 |
| `HARNESS_TARGETED_TESTS` | no | true |
| `HARNESS_VERIFY_FULL_SUITE` | no | true |
| `HARNESS_CONTEXT_COMPACTION_THRESHOLD` | no | 60000 |
| `HARNESS_TELEMETRY` | no | true |
| `HARNESS_RUNS_DIR` | no | `.harness/runs` in this checkout |
| `HARNESS_COMMAND_TIMEOUT_SECONDS` | no | 300 |
| `HARNESS_MAX_MODEL_CALLS` | no | 60 |
| `HARNESS_MAX_TOOL_CALLS` | no | 80 |
| `HARNESS_MAX_VERIFICATION_COMMANDS` | no | 3 |
| `HARNESS_MAX_ACTIVE_FILES` | no | 8 |
| `HARNESS_MAX_CANDIDATES` | no | 25 |
| `HARNESS_MAX_EVIDENCE_ITEMS` | no | 24 |
| `HARNESS_MAX_SNIPPET_LINES` | no | 30 |
| `HARNESS_MAX_CONTEXT_CHARS` | no | 24000 |

## Make targets

| Target | What it does |
|---|---|
| `make setup` | Create `.venv` and make the package importable. Idempotent, works offline. |
| `make run` | Start the harness (interactive unless `ARGS` is given). |
| `make test` | Run the test suite (stdlib `unittest`; no network, no API key needed). |
| `make clean` | Remove `.venv`, caches and run artifacts. Leaves `.env` and sources alone. |

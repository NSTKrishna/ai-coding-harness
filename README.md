# Coding Harness

An autonomous software-engineering harness around a text-only foundation model.
Architecture: [arch.md](arch.md). Status: [PROGRESS.md](PROGRESS.md).

> Current state: milestone M1 (runnable skeleton). The CLI validates configuration
> and input, then exits. The agent itself is not implemented yet.

## Requirements

- Python 3.10 or newer (`python3` on PATH, or pass `PYTHON=python3.x` to `make setup`)
- `make`, `git`
- No other system packages, no Docker, no Node. No runtime Python dependencies.

## Quick start

```sh
export AI_API_KEY="..."
make setup
make run
```

`make run` prompts for the repository path and the task (end the task with an
empty line). You can also pass them directly:

```sh
.venv/bin/python -m harness run --repo /path/to/repo --task "Fix the failing date parser test"
.venv/bin/python -m harness run --repo /path/to/repo --task-file issue.md
make run ARGS='--repo /path/to/repo --task-file issue.md'   # optional convenience
```

If the editable install succeeded during `make setup`, `.venv/bin/harness run ...` works too.

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
| `HARNESS_MAX_REPAIR_CYCLES` | no | 3 |
| `HARNESS_COMMAND_TIMEOUT_SECONDS` | no | 300 |

## Make targets

| Target | What it does |
|---|---|
| `make setup` | Create `.venv` and make the package importable. Idempotent, works offline. |
| `make run` | Start the harness (interactive unless `ARGS` is given). |
| `make test` | Run the test suite (stdlib `unittest`; no network, no API key needed). |
| `make clean` | Remove `.venv`, caches and run artifacts. Leaves `.env` and sources alone. |

# Coding Harness

An autonomous software-engineering harness around a text-only foundation model.
Architecture: [arch.md](arch.md). Status: [PROGRESS.md](PROGRESS.md).

> Current state: milestone M6 (final). A run goes discovery → plan → baseline (targeted test
> first, then the discovered suite, before any edit) → one-action-per-step execution with
> deterministic context compaction → evidence-based verification → bounded repair with a
> repeated-failure stop rule → VERIFIED only on strong evidence. Every run writes artifacts
> (`harness runs`, `harness report <id>`). Models are reached through the provider-neutral
> `openai_compatible` adapter (stdlib HTTP, no SDK); it has been run live against Qwen3 and
> DeepSeek V3.x models on AWS Bedrock (see "Model configuration"). Without model settings,
> `make run` accepts input and then stops with a precise "no model configured" message.

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

`make run` shows a concise header (repository, task or GitHub issue, model) and,
on a real terminal, a live view of discovery/plan/baseline/execute/verify/repair
as it happens. Interactive prompts: `Repository ›`/`Task ›` accept a single line
(including a GitHub issue URL) immediately on Enter; type `:multi` and Enter for
a longer, blank-line-terminated task. `--verbose` shows configuration diagnostics
(adapter, base URL, limits, `.env` file — never the API key value); `--no-interactive`
disables the start confirmation and any animation, for scripts and CI. `Ctrl-C`
is graceful at every stage (no changes committed, or the modified files and run
id if execution had started). It needs `AI_MODEL_ADAPTER`, `AI_MODEL` and
`AI_BASE_URL` (see "Model configuration"); without them it exits with code 2,
says what is missing and modifies nothing. You can also pass the input directly:

GitHub issues: when the task is an issue URL, the harness fetches the issue's title,
body, labels and recent comments (with the `gh` CLI when installed, otherwise the public
API; `--no-fetch-issue` skips this) and warns if the issue is closed or belongs to a
different repository than the target's remotes. It then starts on a **new branch**
`harness/issue-<n>` cut from the freshly fetched default branch of `upstream` (or
`origin`): your local `main` is never modified, uncommitted tracked changes make it stay
on the current branch (or refuse, with `--branch`), and nothing is forced or reset. Use
`--branch` to do this for any task and `--no-branch` to stay where you are. The branch is
created only after the model client is configured, so a misconfigured run changes nothing.

```sh
make run ARGS='--repo /path/to/repo --task "Fix the failing date parser test"'
make run ARGS='--repo /path/to/repo --task https://github.com/owner/repo/issues/42'
make run ARGS='--repo /path/to/repo --task-file issue.md --branch'
make run ARGS='--repo /path/to/repo --task "..." --verbose'
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

## Model configuration

The core depends only on a provider-neutral `ModelClient`. `AI_MODEL_ADAPTER` picks the
transport (today `openai_compatible`: any OpenAI-compatible chat-completions endpoint);
`AI_MODEL` and `AI_BASE_URL` say which model and where. The model family never selects code.

Verified live on 2026-09-26 with an AWS Bedrock API key (region us-east-1):

```sh
export AI_API_KEY="bedrock-api-key-..."       # short-term Bedrock keys expire after <= 12 h
export AI_MODEL_ADAPTER=openai_compatible
export AI_BASE_URL=https://bedrock-mantle.us-east-1.api.aws/v1
export AI_MODEL=qwen.qwen3-coder-480b-a35b-instruct   # or deepseek.v3.2, deepseek.v3.1,
                                                      # qwen.qwen3-235b-a22b-2507, qwen.qwen3-coder-next, ...
make run ARGS='--repo /path/to/repo --task "..."'
```

The evaluator's own endpoint and model ids are not built in; set these variables for it.

## Configuration

Set in the environment, or in a `.env` file in the directory you run from
(copy [.env.example](.env.example)). The environment wins over `.env`.

| Variable | Required | Default |
|---|---|---|
| `AI_API_KEY` | yes | — |
| `AI_MODEL_ADAPTER` | for `run` | unset (built in: `openai_compatible`) |
| `AI_MODEL_PROVIDER` | no | unset; family label for reports only (e.g. `qwen`, `deepseek`) |
| `AI_MODEL` | for `run` | unset; the model id the endpoint expects |
| `AI_BASE_URL` | for `run` | unset; endpoint root, `/chat/completions` is appended |
| `AI_MODEL_TIMEOUT_SECONDS` | no | 120 |
| `AI_MODEL_MAX_RETRIES` | no | 2 (transient transport failures only) |
| `AI_MODEL_HEADERS` | no | none (JSON object of extra headers; never `Authorization`) |
| `AI_MODEL_STRUCTURED_OUTPUT` | no | `none` (`json_object` requests JSON mode for the plan) |
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

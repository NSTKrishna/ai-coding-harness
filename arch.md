# AI Coding Harness — Architecture

> This document is the technical source of truth for the project.
>
> It describes how the harness is structured, how components communicate,
> how a task moves through the system, and the important engineering decisions.
>
> Implementation should remain consistent with this document unless the
> architecture is intentionally revised. Revisions are deliberate edits to this
> file, recorded in PROGRESS.md.

Status markers used below: **[M1]** built in milestone 1, **[planned]** designed but not built.
What is actually implemented and verified is tracked in PROGRESS.md and REQUIREMENTS.md, not here.

---

# 1. Problem

A foundation language model alone is not a reliable autonomous software engineer.

Given a software-engineering task, a model still needs a surrounding system that can:

- understand the task
- inspect an unfamiliar repository
- locate relevant code
- select useful context
- plan changes
- execute tools
- edit code safely
- run tests
- inspect failures
- repair incorrect changes
- verify acceptance criteria
- stop when sufficient evidence exists
- operate within bounded time/model/tool budgets

This surrounding system is the **coding harness**.

Our system transforms:

```text
Software Engineering Task
          ↓
     Coding Harness
          ↓
   Foundation Model
      + Tools
      + Repository
          ↓
 Correct Verified Change
```

---

# 2. Design principles

1. **Evidence over assertion.** The harness declares success only from recorded
   evidence (test results, diffs, checks), never from the model saying "done".
2. **Deterministic first, model second.** Cheap deterministic work (file indexing,
   text search, manifest parsing, test discovery) runs before any model call and
   narrows what the model sees.
3. **Explicit state.** One `RunState` object holds everything about a run. The
   orchestrator is a state machine; every transition is logged.
4. **Everything is bounded.** Steps, repair cycles, model calls, context size,
   command time and wall-clock time all have limits. No unbounded loops.
5. **Small, reversible edits.** Changes are patches validated before they are
   applied, applied atomically, and snapshotted so they can be rolled back.
6. **Provider independence.** Orchestration never depends on a specific model
   vendor. The model is a text-in/text-out dependency behind an interface.
7. **Stdlib first, offline by default.** Python ≥ 3.10 standard library wherever
   reasonable. Only model calls need the network. No Docker, Node, system packages
   or downloaded models.
8. **The target repository is the user's.** The harness edits files inside the
   target repository root only, never commits, pushes or rewrites history.
9. **Testable without a model.** Every component can be tested with a scripted
   fake model and throwaway fixture repositories.

---

# 3. High-level architecture

```text
            ┌─────────┐   ┌──────────┐
  user ───▶ │   CLI   │──▶│  Config  │   (AI_API_KEY, model, limits)
            └────┬────┘   └──────────┘
                 │ TaskSpec + repo path
                 ▼
         ┌──────────────────────────────────────────────┐
         │               Orchestrator                    │
         │  state machine over RunState, budget checks   │
         └──┬─────────┬──────────┬──────────┬───────────┘
            │         │          │          │
            ▼         ▼          ▼          ▼
       Repository  Planner   Executor   Verification
       Intelligence   │          │        Engine
            │         │          │          │
            │         └────┬─────┘          │
            │              ▼                │
            │        Context Manager        │
            │              │                │
            │              ▼                │
            │        Model Client ──▶ provider adapter ──▶ model API
            │                               │
            └──────────▶ Tool Registry ◀────┘
                          │  read / search / patch / run / test / git
                          ▼
                   Target repository

  cross-cutting:  Evidence Ledger · Telemetry · Budget tracker · Secret redaction
```

| Component | Module (planned) | Responsibility |
|---|---|---|
| CLI | `harness/cli.py` **[M1]** | Parse arguments or prompt for input; build `TaskSpec`; start a run; exit codes |
| Config | `harness/config.py` **[M1]** | Load and validate settings; hold the secret without exposing it |
| Orchestrator | `harness/orchestrator/machine.py` | Drive the lifecycle state machine; enforce budgets |
| RunState | `harness/orchestrator/state.py` | Single source of run state; serializable |
| Repository intelligence | `harness/repo/` | Repo profile, file index, search, ranking, symbols |
| Context manager | `harness/context/` | Assemble model prompts within a size budget; compaction |
| Model client | `harness/model/` | Provider-independent `generate()`; adapters; fake model |
| Planner | `harness/orchestrator/planner.py` | Produce and revise a structured `Plan` |
| Executor | `harness/orchestrator/executor.py` | Run plan steps as model-action → tool → observation loops |
| Tool registry | `harness/tools/` | Validated, confined, counted tool execution |
| Verification engine | `harness/verify/verifier.py` | Decide PASS/FAIL/INCONCLUSIVE from evidence |
| Evidence ledger | `harness/verify/ledger.py` | Append-only record of what was observed |
| Failure recovery | `harness/verify/failures.py` | Classify failures and choose a recovery path |
| Telemetry | `harness/telemetry.py` | Counters, event log, run report |

Components communicate through plain dataclasses (`TaskSpec`, `RepoProfile`,
`Plan`, `ToolResult`, `ModelResponse`, `Evidence`, `FailureReport`). Only the
orchestrator mutates `RunState`; other components receive what they need and
return results.

---

# 4. Agent lifecycle / state machine

```text
 INIT ──▶ UNDERSTAND ──▶ DISCOVER ──▶ PLAN ──▶ EXECUTE ──▶ VERIFY ──pass──▶ SUCCEEDED
                             ▲          ▲         ▲           │
                             │          │         │         fail
                             │          │         │           ▼
                             │          └─replan──┴──fix── REPAIR
                             └──────need more context────────┘
                                                             │
                                    budget exhausted / loop  │
                                    detected / unrecoverable ▼
                                                           FAILED

 any state ──fatal error / interrupt──▶ ABORTED
```

| State | Does | Exits to |
|---|---|---|
| `INIT` | Validate config, repo path, task; create run directory; snapshot `git status` | `UNDERSTAND`, or `ABORTED` on invalid input |
| `UNDERSTAND` | Extract goal, acceptance criteria, identifiers, file paths, error text from the task | `DISCOVER` |
| `DISCOVER` | Build `RepoProfile`, rank candidate files, detect test command, run **baseline** tests | `PLAN` |
| `PLAN` | Model produces a `Plan` from permanent context + top candidates (read-only tools allowed) | `EXECUTE` |
| `EXECUTE` | Execute the current plan step via the executor loop; apply patches | `EXECUTE` (next step) or `VERIFY` |
| `VERIFY` | Run the verification engine (§14) | `SUCCEEDED` or `REPAIR` |
| `REPAIR` | Classify the failure (§16) and choose: fix in place, replan, or rediscover | `EXECUTE`, `PLAN`, `DISCOVER`, or `FAILED` |
| `SUCCEEDED` | Terminal. Write run report with evidence | — |
| `FAILED` | Terminal. Write run report explaining why, with evidence gathered | — |
| `ABORTED` | Terminal. Fatal environment/config error or user interrupt | — |

Loop shape:

```python
while state.phase not in TERMINAL:
    if budget.exhausted(state):
        state.transition(FAILED, reason=budget.reason)
        break
    handler = HANDLERS[state.phase]
    transition = handler(state)          # returns next phase + reason
    telemetry.record_transition(state.phase, transition)
    state.apply(transition)
```

Each handler is a plain function `RunState -> Transition`, which makes each
state unit-testable in isolation.

---

# 5. RunState

```python
@dataclass
class RunState:
    run_id: str
    phase: Phase
    task: TaskSpec                    # raw text, source (arg/file/prompt), criteria
    repo: RepoProfile | None          # filled in DISCOVER
    plan: Plan | None                 # filled in PLAN, revised on replan
    current_step: int
    patches: list[PatchRecord]        # applied edits + pre-edit snapshots
    baseline: TestRunSummary | None   # tests before any edit
    last_failure: FailureReport | None
    failure_signatures: list[str]     # for loop detection
    repair_cycles: int
    context: ContextStore
    ledger: EvidenceLedger
    budget: BudgetTracker
    transitions: list[TransitionRecord]
    outcome: Outcome | None           # SUCCEEDED / FAILED / ABORTED + reason
```

Rules:

- The orchestrator is the only writer of `phase`, `outcome` and `transitions`.
- `RunState` is JSON-serializable (via `dataclasses.asdict` plus small encoders)
  and written to the run directory at every transition, so a failed run can be
  inspected after the fact.
- It never contains the API key.

---

# 6. Repository intelligence

Produces a `RepoProfile` without loading file contents wholesale.

- **File index.** In a git repository, `git ls-files --cached --others --exclude-standard`
  (respects `.gitignore`). Otherwise a directory walk with default ignores
  (`.git`, `.venv`, `node_modules`, `__pycache__`, `dist`, `build`, binary
  extensions). Stores path, size, extension. No content.
- **Languages.** Counted by extension.
- **Build/test tooling.** Detected from manifests: `pyproject.toml`, `setup.cfg`,
  `pytest.ini`, `tox.ini`, `package.json` scripts, `go.mod`, `Cargo.toml`,
  `Makefile` targets. Produces an ordered list of candidate test commands.
- **Test layout.** Test directories and test-file ↔ source-file mapping by naming
  convention (`test_foo.py` ↔ `foo.py`, `foo.test.ts` ↔ `foo.ts`).
- **Symbols.** Python via the stdlib `ast` module (functions, classes, methods with
  line ranges). Other languages via conservative regexes. Built lazily, only for
  candidate files.
- **Search.** `ripgrep` when available (fast, respects ignores), with a pure-Python
  fallback that produces the same result format.

---

# 7. Progressive repository discovery

The model sees the repository in widening levels, and each level costs budget.

| Level | Content | Produced by |
|---|---|---|
| L0 | Repo profile summary: top-level tree (depth-limited), languages, manifests, test command | deterministic |
| L1 | Ranked candidate files (paths + reason + score) | deterministic |
| L2 | Snippets: windows around search hits, or whole functions/classes via symbols | deterministic |
| L3 | Full file content, only on explicit request and under a size cap | tool call |

**Candidate ranking (L1).** Terms are extracted from the task: identifiers
(`snake_case`, `CamelCase`, dotted names), quoted strings, file paths, error
messages and stack-trace frames. Each file gets a score from:

- exact path or filename mentioned in the task (highest)
- stack-trace frames pointing at it
- symbol definitions matching task identifiers
- number and specificity of search hits (rare terms weigh more, IDF-style)
- test ↔ source pairing with an already-ranked file
- penalties for generated, vendored or very large files

The model can ask for more (search, read) through tools; the harness never
dumps the whole repository into context.

---

# 8. Context management

Two separate stores:

- **Permanent context** — present in every model call and kept small:
  system instructions and action protocol, task text, acceptance criteria,
  L0 repo summary, current plan and step, budget remaining.
- **Working context** — items with `id`, `kind` (snippet, file, tool output,
  test result, summary), `source` (path + line range or command), `size`,
  `relevance`, `turn_added`, `pinned`, `content_hash`.

**Assembly.** Permanent context first, then working items by relevance and
recency until the character budget is reached. Sizes are counted in characters
(provider-independent). Tokens are estimated as `chars / 4` unless the adapter
reports real usage.

**Compaction.** When the working store exceeds its budget, the oldest unpinned
items are compacted, deterministically first:

- test output → failing test ids + first assertion/traceback lines + counts
- command output → exit code + head/tail lines
- file reads → path + line range + "available on request"

A model-written summary is used only when deterministic compaction is not enough,
and it counts as a model call.

**Deduplication and staleness.** Items are keyed by `content_hash`; re-reading an
unchanged region yields a short "unchanged since turn N" marker instead of the
content. When a patch touches a file, working items for that file are marked stale
and replaced on next read.

---

# 9. Model abstraction

```python
class ModelClient(Protocol):
    def generate(self, request: ModelRequest) -> ModelResponse: ...

@dataclass
class ModelRequest:
    messages: list[Message]          # role in {"system", "user", "assistant"}, text only
    max_output_tokens: int
    temperature: float = 0.0
    purpose: str = ""                # "plan", "execute", "repair", ... for telemetry

@dataclass
class ModelResponse:
    text: str
    input_tokens: int | None         # None when the provider does not report usage
    output_tokens: int | None
    finish_reason: str
    latency_seconds: float
```

- **Text only.** The harness does not depend on a provider's native tool-calling
  API. Tools are invoked through a text action protocol (below), so any
  text-only model works.
- **Adapters.** One adapter per provider, selected by `config.model.provider`.
  The first real adapter is written once organizers announce the provider and
  model; none is assumed now. Adapters use `urllib.request` so no SDK dependency
  is required.
- **Fake model.** `FakeModelClient` replays a scripted list of responses and
  records the requests it receives. All non-live tests use it.
- **Transient errors.** Timeouts, HTTP 429 and 5xx are retried with exponential
  backoff up to a limit; every attempt counts as a model call.

**Action protocol.** Each executor turn, the model replies with exactly one JSON
object in a fenced block:

```json
{"thought": "short reasoning", "action": "read_file", "args": {"path": "src/x.py", "start": 1, "end": 80}}
```

Special actions: `step_done`, `request_replan`, `finish` (a claim that verification
should run; not a success declaration). A reply that does not parse gets one
format-correction reprompt, counted as `MODEL_FORMAT_ERROR` if it recurs.

---

# 10. Planner

Input: permanent context + L1 candidates + L2 snippets for the top candidates.
Read-only tools are allowed while planning.

Output:

```python
@dataclass
class Plan:
    goal: str
    acceptance_criteria: list[Criterion]   # from task + defaults below
    steps: list[PlanStep]                  # intent, target files, how to verify
    test_strategy: TestStrategy            # targeted tests, full-suite command
```

Default criteria are added to every plan unless the task contradicts them:
tests that passed at baseline still pass; the change is confined to relevant
files; new behaviour is exercised by a test where the repo has tests.

Replanning happens on `request_replan`, on repeated failure signatures (§16),
or when repair determines the plan's file targets were wrong. A replan keeps the
evidence ledger and the failure history so the model does not repeat itself.

---

# 11. Tool system

```python
@dataclass
class ToolResult:
    ok: bool
    output: str          # already truncated to the tool's output cap
    truncated: bool
    metadata: dict       # e.g. exit_code, duration, files_changed

class Tool(Protocol):
    name: str
    category: Literal["read", "write", "exec"]
    def run(self, args: dict, ctx: ToolContext) -> ToolResult: ...
```

The registry validates arguments, enforces the category allowed in the current
phase (planning is read-only), applies confinement (§25), caps output, times
execution, and records every call in telemetry and the evidence ledger.

| Tool | Category | Purpose |
|---|---|---|
| `list_files` | read | Paths under a directory (from the file index) |
| `search` | read | Text/regex search with file glob, returns path:line:match |
| `read_file` | read | File content by line range, with size cap |
| `apply_patch` | write | Apply a unified diff or replace-block edit (§13) |
| `run_command` | exec | Run a command in the repo with timeout and scrubbed env |
| `run_tests` | exec | Run detected/targeted tests; returns parsed summary |
| `git_status` | read | `git status --porcelain` |
| `git_diff` | read | `git diff` and `git diff --stat` against the run's start state |

---

# 12. Executor

Runs one plan step as a bounded loop:

```text
assemble context ─▶ model.generate ─▶ parse action ─▶ registry.run(tool)
        ▲                                                   │
        └──────────── add observation to working context ◀──┘
```

The loop ends when the model emits `step_done`, `request_replan` or `finish`,
or when the per-step turn limit is hit. Repeating an identical action with
identical arguments three times in a row forces a `NO_PROGRESS` failure.

---

# 13. Editing strategy

- **Formats.** Unified diff is primary. A search/replace block (`path`, exact
  `old` text, `new` text) is the fallback because models often produce malformed
  diffs. Both go through the harness's own pure-Python applier; no dependency on
  the `patch` binary.
- **Validation before apply.** Every path resolves inside the repo root; the
  target exists (new files must be explicitly declared); every hunk / `old` block
  matches exactly once. Whitespace-tolerant matching is tried only if exact
  matching fails, and is recorded.
- **Atomicity.** All hunks of a patch apply or none do.
- **Snapshots.** Original contents of every touched file are kept in the
  `PatchRecord` for rollback.
- **Post-apply checks.** Python files are compiled (`compile()`) to catch syntax
  errors immediately; the result goes to the ledger.
- **No commits.** The harness never runs `git commit`, `git push`, `git reset --hard`,
  `git checkout -- .` or rewrites history. The result is left as working-tree changes.

---

# 14. Verification engine

Runs in layers; later layers run only if earlier ones pass.

1. **Static.** All patches applied; changed Python files compile; diff is non-empty.
2. **Targeted tests.** Tests named in the task, tests paired with changed files,
   and tests added by the run.
3. **Broader tests.** The full suite (or the widest affordable subset if the
   baseline showed the suite is too slow for the remaining budget).
4. **Regression check against baseline.** A test that passed at baseline and fails
   now is a regression. Failures that already existed at baseline are reported
   but do not by themselves fail the run.
5. **Diff review.** `git diff` is inspected for changes outside plan targets,
   deleted or skipped tests, leftover debug output, and files that should not change
   (lockfiles, CI config) unless the task asked for it.
6. **Acceptance criteria.** Each criterion is mapped to evidence. Deterministic
   mapping where possible (named test passes). Otherwise the model judges with the
   evidence in front of it and must cite ledger ids; such checks are marked
   lower confidence in the report.

Verdict: `PASS` (all layers pass, every criterion has evidence), `FAIL`
(with a `FailureReport`), or `INCONCLUSIVE` (e.g. no test command found). An
inconclusive verdict is never reported as success.

---

# 15. Evidence ledger

Append-only list of `Evidence` records:

```python
@dataclass
class Evidence:
    id: str                 # "E12"
    kind: str               # baseline | test_run | command | patch | diff | syntax_check | criterion_check
    summary: str            # one line, human-readable
    passed: bool | None
    artifact_path: str      # full output in the run directory
    criteria: list[str]     # criterion ids this supports
    after_patch: int        # index of the last patch applied when recorded
```

Success requires: every acceptance criterion linked to passing evidence, and the
test evidence recorded **after** the last patch. The final report lists the
evidence ids that justify the outcome.

---

# 16. Failure recovery

| Failure class | Detected by | Recovery |
|---|---|---|
| `PATCH_APPLY_FAILED` | applier | Re-read target region, retry edit (counts toward format retries) |
| `SYNTAX_ERROR` | post-apply compile | Fix in place |
| `TEST_FAILURE` | assertion failures in targeted tests | Fix in place with failure excerpt in context |
| `TEST_ERROR` | import/collection errors | Fix in place; if in untouched files, rediscover |
| `REGRESSION` | baseline comparison | Fix in place; if repeated, roll back the offending patch and replan |
| `TIMEOUT` | command timeout | Narrow the test selection; if the baseline also timed out, stop |
| `ENV_ERROR` | missing interpreter/command, permission errors | Stop (`FAILED`): editing code cannot fix the environment |
| `MODEL_FORMAT_ERROR` | action parser | Reprompt with the protocol; then fail the step |
| `NO_PROGRESS` | repeated identical actions or failure signatures | Replan once; then stop |
| `BUDGET_EXHAUSTED` | budget tracker | Stop |

**Failure signature** = hash of (class, sorted failing test ids, normalized
first error line). The same signature twice triggers a replan; three times stops
the run.

---

# 17. Retry / budget policy

| Limit | Config key | Default |
|---|---|---|
| Orchestrator steps (state transitions + executor turns) | `max_steps` **[M1]** | 40 |
| Repair cycles | `max_repair_cycles` **[M1]** | 3 |
| Command timeout (seconds) | `command_timeout_seconds` **[M1]** | 300 |
| Model calls | `max_model_calls` [planned] | 60 |
| Context size (characters) | `max_context_chars` [planned] | 60 000 |
| Per-tool output cap (characters) | `max_tool_output_chars` [planned] | 8 000 |
| Edit format retries per step | `max_format_retries` [planned] | 2 |
| Transient model API retries | `max_model_retries` [planned] | 3 |
| Wall-clock limit (seconds) | `max_wall_seconds` [planned] | 1 800 |

The budget is checked before every state handler and every executor turn.
Exhaustion ends the run in `FAILED` with a report of what was achieved.

**On failure** the working tree keeps the edits made so far and the report says
clearly that they are unverified. A `--rollback-on-failure` flag [planned]
restores all snapshots instead.

---

# 18. Efficiency strategy

- Deterministic discovery before the first model call; the model starts from a
  ranked shortlist, not the repository.
- Snippets before whole files; whole files only on request and under a cap.
- Targeted tests before the full suite; the full suite once, at the end.
- Per-run caches for file reads and searches, keyed by path + mtime.
- Tool output truncated and deterministically compacted.
- Deduplicated context (§8).
- One model call per executor turn; no speculative parallel calls.
- The baseline run tells the harness whether the full suite is affordable.

---

# 19. Telemetry

Counters kept in the `BudgetTracker` / telemetry module:

- model calls by purpose, input/output tokens (reported or estimated), latency
- tool calls by name and outcome, command durations
- test runs (targeted vs full), repair cycles, replans
- state transitions, wall-clock time

Outputs, written to the run directory (`.harness-runs/<run_id>/` under the
current working directory by default, configurable, never inside the target repo):

- `events.jsonl` — one structured event per transition, model call and tool call
- `state.json` — latest `RunState`
- `report.md` — human summary: outcome, evidence ids, diff stat, counters

All text written or printed passes through a redaction filter that replaces
the API key value with `***`.

---

# 20. CLI

Entry points: `python -m harness` and the `harness` console script (installed by
`make setup` when possible). `make run` calls `python -m harness run`.

```text
harness run [--repo PATH] [--task TEXT | --task-file FILE]
```

- **[M1]** Missing `--repo` or task → interactive prompts:
  `Repository path:` then `Task / GitHub issue:`. The task prompt accepts
  multi-line input terminated by an empty line or end-of-input (so pasted issues work).
- **[M1]** `--task` and `--task-file` are mutually exclusive.
- **[M1]** In M1, after validation the CLI prints the accepted configuration
  (secret redacted) and states that the agent is not implemented yet, then exits.
  It does not call a model.
- **[planned]** stdin task input, `make run` convenience variables, `--rollback-on-failure`,
  `--json` report output.

Exit codes:

| Code | Meaning |
|---|---|
| 0 | Success (M1: input and configuration accepted) |
| 1 | Run finished but verification failed [planned] |
| 2 | Usage, configuration or input error |
| 130 | Interrupted (Ctrl-C) |

---

# 21. Configuration

Precedence (highest first): CLI flags → process environment → `.env` file in the
current working directory → built-in defaults. `.env` never overrides a variable
already set in the environment.

| Variable | Field | Required | Default |
|---|---|---|---|
| `AI_API_KEY` | `api_key` | **yes** (the only secret) | — |
| `AI_MODEL_PROVIDER` | `model.provider` | no | unset (awaiting organizers) |
| `AI_MODEL` | `model.name` | no | unset (awaiting organizers) |
| `AI_BASE_URL` | `model.base_url` | no | unset (awaiting organizers) |
| `HARNESS_MAX_STEPS` | `limits.max_steps` | no | 40 |
| `HARNESS_MAX_REPAIR_CYCLES` | `limits.max_repair_cycles` | no | 3 |
| `HARNESS_COMMAND_TIMEOUT_SECONDS` | `limits.command_timeout_seconds` | no | 300 |

The organizer-prescribed provider/model/base URL are defaults in one clearly
marked block at the top of `config.py`. They stay unset until the hackathon
announces them; once a real adapter exists, a run that needs the model fails
early with a clear error if they are still unset.

The key is stored in a field excluded from `repr`, never logged, and scrubbed
from child-process environments (§25).

---

# 22. Repository structure

```text
.
├── Makefile                  [M1] setup / run / test / clean
├── pyproject.toml            [M1] metadata, Python ≥ 3.10, no runtime deps
├── .env.example              [M1] placeholders only
├── .gitignore                [M1]
├── README.md                 [M1] evaluator quick start
├── arch.md  PROGRESS.md  REQUIREMENTS.md
├── src/harness/
│   ├── __init__.py           [M1]
│   ├── __main__.py           [M1]
│   ├── cli.py                [M1]
│   ├── config.py             [M1]
│   ├── telemetry.py          [planned]
│   ├── model/                [planned] base.py, fake.py, adapters/
│   ├── tools/                [planned] registry.py, fs.py, search.py, patch.py, shell.py, git.py
│   ├── repo/                 [planned] profile.py, index.py, rank.py, symbols.py
│   ├── context/              [planned] store.py, compaction.py
│   ├── orchestrator/         [planned] state.py, machine.py, planner.py, executor.py, protocol.py
│   └── verify/               [planned] verifier.py, ledger.py, failures.py
└── tests/
    ├── test_config.py        [M1]
    ├── test_cli.py           [M1]
    ├── fixtures/             [planned] templates for small target repositories
    └── e2e/                  [planned] full-loop tests with the fake model
```

---

# 23. Testing architecture

- **Framework.** Stdlib `unittest`, run with
  `python -m unittest discover -s tests -t .`. No download is needed for `make test`.
  pytest may be added later only if it pays for itself, and then declared in
  `pyproject.toml` and installed by `make setup`.
- **Unit tests.** Pure functions and components with injected inputs
  (config takes an explicit environment mapping; CLI takes argv and streams).
- **Component tests.** Tools and repository intelligence against throwaway
  repositories created in a temporary directory at test time (`git init` there).
- **End-to-end tests.** The full state machine with `FakeModelClient` on a fixture
  repo containing a seeded bug, covering: a clean fix, a failed verification
  followed by a repair, and a loop that must stop at the retry limit.
- **Live smoke test.** Optional, skipped unless `AI_API_KEY` is set **and**
  `HARNESS_LIVE_TESTS=1`. Never part of the default `make test`.
- **Secret tests.** Tests assert the API key never appears in stdout, stderr,
  `repr(config)` or run artifacts.

---

# 24. Makefile contract

| Target | Contract |
|---|---|
| `make setup` | Creates `.venv` with `$(PYTHON)` (default `python3`, must be ≥ 3.10) and makes the package importable. Idempotent. Needs no network: the package is exposed through a `.pth` file. It then tries an editable install to get the `harness` console script; if that fails (e.g. offline), it warns and continues. |
| `make run` | Runs `python -m harness run` from the venv. Interactive when no arguments are given. Optional `ARGS="--repo … --task …"` passes flags through; never required. |
| `make test` | Runs the unittest suite from the venv. Needs no network and no API key. |
| `make clean` | Removes `.venv`, caches, build output and `.harness-runs`. Never touches `.env` or source files. |

The evaluator flow is exactly:

```sh
export AI_API_KEY="..."
make setup
make run
```

---

# 25. Security boundaries

- **Secret.** `AI_API_KEY` is read from the environment or `.env`, excluded from
  `repr`, redacted from all output and logs, and **removed from the environment of
  every child process** the harness starts, so code in the target repository
  (its tests, build scripts) cannot read it.
- **File-system confinement.** Every tool path is resolved (`Path.resolve()`,
  following symlinks) and must stay inside the target repo root. Writes to `.git/`
  are rejected.
- **Command execution.** `subprocess.run` with an argument list (no `shell=True`
  for harness-built commands), `cwd` = repo root, timeout, output cap, scrubbed env.
  Model-proposed commands are checked against a denylist (`git commit/push/reset --hard/clean -f`,
  `rm -rf` outside the repo, `sudo`, piping downloads into a shell).
- **Not a sandbox.** The harness does not isolate the network or the file system
  at the OS level; test code in the target repo runs with the user's permissions.
  This is documented, not hidden.
- **Prompt injection.** Repository contents, issue text and command output are data.
  They are placed in clearly delimited blocks, and the action protocol only accepts
  the defined tools; content cannot widen the tool set or the confinement.

---

# 26. Definition of completion

**A run is complete and successful** only when all hold:

1. The run ended in `SUCCEEDED` by the verification engine, not by model claim.
2. The diff is non-empty, confined to relevant files, and recorded in the ledger.
3. Targeted tests and the broader test run recorded after the last patch pass,
   with no regressions against baseline.
4. Every acceptance criterion links to passing evidence.
5. Budgets were not exceeded; the report shows the counters.

**The project is complete** only when:

1. Every item in REQUIREMENTS.md is `[x]` with evidence.
2. From a fresh clone, `make setup && make test && make run` work with only
   `AI_API_KEY` set, with no undocumented steps.
3. End-to-end tests with the fake model pass, including repair and retry-limit cases.
4. At least one live run with the organizer-prescribed model has been recorded
   (once the model is announced).
5. No credential-like string exists in tracked files.

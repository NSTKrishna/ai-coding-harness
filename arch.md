# AI Coding Harness — Architecture

> This document is the technical source of truth for the project.
>
> It describes how the harness is structured, how components communicate,
> how a task moves through the system, and the important engineering decisions.
>
> Implementation should remain consistent with this document unless the
> architecture is intentionally revised. Revisions are deliberate edits to this
> file, recorded in PROGRESS.md.

Status markers used below: **[M1]**…**[M4]** built in that milestone, **[planned]** designed but not built.
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
| Orchestrator | `harness/orchestrator/orchestrator.py` **[M4]** | Coordinate INTAKE → DISCOVER → PLAN → EXECUTE; share one ToolContext |
| RunState | `harness/orchestrator/state.py` **[M4]** | Single source of run state; enforced phase transitions |
| Repository intelligence | `harness/repo/` **[M3]** | Inventory, profile, command discovery, task signals, candidate ranking |
| Context manager | `harness/context/` **[M3]** | Bounded working set; permanent/working/episodic stores (compaction planned) |
| Model client | `harness/model/` **[M2]** | Provider-independent `generate()`; scripted fake model; call metering (adapters planned) |
| Planner | `harness/orchestrator/plan.py` **[M4]** | One model call → strict JSON `TaskPlan` (revision planned) |
| Executor | `harness/orchestrator/executor.py` **[M4]** | Bounded one-action-per-step model → tool → observation loop |
| Tool registry | `harness/tools/` **[M2]** | Validated, confined, counted tool execution |
| Verification engine | `harness/verify/verifier.py` | Decide PASS/FAIL/INCONCLUSIVE from evidence |
| Evidence ledger | `harness/verify/ledger.py` | Append-only record of what was observed |
| Failure recovery | `harness/verify/failures.py` | Classify failures and choose a recovery path |
| Metrics | `harness/metrics.py` **[M2]** | Model/tool/command counters and token usage |
| Telemetry | `harness/telemetry.py` [planned] | Event log and run report built on the metrics |

Components communicate through plain dataclasses (`TaskSpec`, `RepoProfile`,
`Plan`, `ToolResult`, `ModelResponse`, `Evidence`, `FailureReport`). Only the
orchestrator mutates `RunState`; other components receive what they need and
return results.

---

# 4. Agent lifecycle / state machine

**Built in M4** (`orchestrator/state.py`, `orchestrator/orchestrator.py`):

```text
 INTAKE ──▶ DISCOVER ──▶ PLAN ──▶ EXECUTE ──complete──▶ READY_FOR_VERIFICATION
   │            │          │         │
   │            │          │         ├─blocked──────────▶ BLOCKED
   │            │          │         ├─model/protocol───▶ MODEL_ERROR
   │            │          │         ├─tool crash───────▶ TOOL_ERROR
   │            │          ├─────────┴─budget───────────▶ BUDGET_EXHAUSTED
   │            │          └─bad plan / model failure───▶ MODEL_ERROR
   ├─invalid input──────────────────────────────────────▶ BLOCKED
   └────────────┴─ unexpected exception ────────────────▶ INTERNAL_ERROR
```

| Phase | Does | Exits to |
|---|---|---|
| `INTAKE` | Check task non-empty, create the run's single `ToolContext` (root, limits, metrics) | `DISCOVER`, `BLOCKED` |
| `DISCOVER` | M3 `discover_for_task(..., ctx=ctx)`; resolve Python interpreters of discovered commands (§6) | `PLAN` |
| `PLAN` | Budget check, then one planner model call → `TaskPlan` (§10) | `EXECUTE`, `MODEL_ERROR`, `BUDGET_EXHAUSTED` |
| `EXECUTE` | Executor loop (§12) | `READY_FOR_VERIFICATION` or an abnormal terminal |
| `READY_FOR_VERIFICATION` | Terminal in M4. The executor *believes* it is done; nothing has been verified | — (M5: → `VERIFYING`) |
| `BLOCKED`, `MODEL_ERROR`, `TOOL_ERROR`, `BUDGET_EXHAUSTED`, `INTERNAL_ERROR` | Terminal, with a structured `Failure(kind, message, details)` | — |

`RunState.transition()` enforces the table; any other move raises
`InvalidTransition`. `VERIFYING`, `VERIFIED` and `NEEDS_REPAIR` exist in the
enum for M5, but no M4 transition reaches them. The orchestrator catches every
exception, so a run always ends in a terminal phase instead of a traceback.

**Target lifecycle (M5+) [planned]:**

```text
 … EXECUTE ──▶ READY_FOR_VERIFICATION ──▶ VERIFYING ──pass──▶ VERIFIED
                    ▲                          │
                    └──── fix / replan ── NEEDS_REPAIR ◀──fail─┘
```

`VERIFYING` runs the verification engine (§14). `NEEDS_REPAIR` classifies the
failure (§16) and re-enters `EXECUTE` or `PLAN` within `max_repair_cycles`. A
baseline test run before editing is also planned for DISCOVER/M5.

---

# 5. RunState

**[M4]** `orchestrator/state.py`, a mutable dataclass owned by the orchestrator:

```python
RunState(
    run_id, task, repo_root, phase,
    discovery: DiscoveryResult,        # repo_profile / working_set are views of it
    plan: TaskPlan,
    observations: list[Observation],   # bounded (max_observations, oldest dropped, counted)
    action_history: list[ActionRecord],# step, kind, tool, argument summary, native|text
    modified_files: list[str],         # unique, in order
    steps: int,                        # executor iterations
    failure: Failure | None,           # kind, message, details
    terminal_reason: str | None,
    transitions: list[Transition],     # source, target, reason, seconds since start
    metrics: ExecutionMetrics,         # the run's shared counters (model/tool/command calls)
    started_at, elapsed_seconds,
)
Observation(step, tool, arguments_summary, success, outcome, result_summary,
            affected_paths, exit_code, timed_out, error_code, stale)
```

Rules:

- **Single source of truth.** `model_calls`/`tool_calls` are properties reading the
  shared `ExecutionMetrics` that `MeteredModelClient` and `ToolRegistry` update.
  RunState keeps no second counter. The planner, executor and context manager
  hold no copy of run state.
- **Facts only.** Observations record tool, bounded argument summary, tool
  success, outcome (`ok`, `tool_error`, `command_ok`, `command_failed`,
  `command_timed_out`), a bounded excerpt, affected paths, exit code and timeout.
  No model reasoning is stored. The only model text kept is the
  `complete`/`blocked` summary the protocol asks for (≤ 500 characters).
- **Staleness.** `invalidate_path(path, before_step)` marks earlier observations
  of a patched file stale; stale observations are rendered as a "read it again"
  marker, never as content.
- `summary()` returns a frozen `RunSummary` (status, reason, failure, plan,
  modified files, counts, recent observations). There is deliberately no
  verification field.
- It never contains the API key: tool results pass through the registry
  redactor, and model summaries through the same redactor.
- **[planned]** JSON serialization to the run directory at every transition (§19).

---

# 6. Repository intelligence

**[M3]** `harness/repo/`. Deterministic, model-free, read-only. Entry points:
`analyze_repository(root) -> RepoProfile` and
`discover_for_task(root, task, limits=ContextLimits) -> DiscoveryResult`.

**Scope is owned here, not by the search primitive.** The M2 `search_text`
primitive keeps its generic file selection (it ignores `.gitignore`, so its two
engines stay equivalent). Repository intelligence builds the inventory and
passes it to `search(..., allowed_paths=inventory)`; results are then the
primitive's results restricted to the inventory, identically for both engines.

- **Inventory** (`inventory.py`). Git repositories: `git ls-files --cached`
  (tracked) plus `git ls-files --others --exclude-standard` (untracked, not
  ignored), run read-only through the M2 git helpers. Other directories: the
  deterministic M2 walker. In both modes it also drops `IGNORED_DIRS` (`.git`,
  virtualenvs, `node_modules`, caches) even if git would list them, untracked
  files under build-output directories (`build dist target out coverage …`),
  symlinks, and tracked-but-deleted files. Capped at 50 000 files with a
  warning. Only `stat` is used. `FileRecord(path, size_bytes, language,
  category, tracked)`.
- **Classification** (`classify.py`). Language by extension/name. Category, in
  order: `ci`, `generated` (lockfiles, `*.min.js`, `*_pb2.py`, …), `other`
  (binary extensions), `manifest`, `test` (per-ecosystem naming or a
  `tests/test/__tests__/spec` directory), `configuration`, `documentation`,
  `source`.
- **Facts** (`facts.py`). Bounded, counted reads of root-level configuration
  only: `pyproject.toml` section headers (line-based, because `tomllib` is 3.11+
  and results must not depend on the Python version), `setup.cfg`/`tox.ini`
  sections (`configparser`), `package.json` (`json`), Makefile targets, pytest
  in `requirements*.txt`, JVM manifests, and the first 8 KB of up to 20 Python
  test files (unittest/pytest imports).
- **Profile** (`profile.py`). `RepoProfile`: root, git or not, file count and
  bytes, languages, categories, important (top-level) directories, manifests,
  build systems, test frameworks, CI files, lint/typecheck tools, docs
  entrypoints, source roots and test roots (top-level directories holding code
  of that category), test commands, build/lint/typecheck commands, warnings.
  Every indicator names its evidence file (and section). `summary()` is the
  compact text a planner gets.
- **Command discovery** (`commands.py`). `CommandCandidate(kind, argv,
  confidence, reason)`. Proposed only from explicit configuration, never from
  language alone, and never executed during analysis. Test rules in order:
  pytest configured (high); `package.json scripts.test` unless npm's placeholder
  (high, with pnpm/yarn from lockfiles); `go.mod` plus `*_test.go` (high);
  `Cargo.toml` (high); Makefile `test` target (high); pytest referenced without
  configuration (medium); unittest imports without pytest evidence (medium,
  with `-s <dir>` and `-t .` when that dir is a package); Maven/Gradle (medium,
  using wrappers when present). Build/lint/typecheck: `package.json` scripts,
  Makefile targets, `go build`, `cargo build`, and ruff/flake8/mypy/tsc/eslint
  when their configuration exists. Python commands use the repository's own
  `.venv/bin/python` or `venv/bin/python` when present, else `python`.
  Otherwise no command is reported.
- **Symbols [planned].** Python `ast` outlines. M3 detects definitions with
  per-language regexes on search hits instead (§7).

---

# 7. Progressive repository discovery

The model sees the repository in widening levels, and each level costs budget.

| Level | Content | Produced by |
|---|---|---|
| L0 | `RepoProfile.summary()` | deterministic **[M3]** |
| L1 | Ranked `CandidateFile`s with reasons | deterministic **[M3]** |
| L2 | Evidence snippets around the hits in the top files | deterministic **[M3]** |
| L3 | Full file content, only on explicit request and under a size cap | tool call (`read_file`/`read_range`, M2) |

**Task signals [M3]** (`signals.py`, regex heuristics, no NLP): explicit paths
(with or without directories; `:line` suffixes and URLs removed); identifiers
(CamelCase, camelCase, acronym-led like `HTTPClient`, snake_case,
SCREAMING_CASE, dotted names plus their last component, `backticked`,
`calls()`); name parts (lowercased pieces of identifiers); keywords (other words
of 3+ letters minus a noise list of task and code vocabulary); phrases (adjacent
keywords within one clause). First-appearance order, deduplicated.

**Discovery stages [M3]** (`discovery.py`), cheapest first:

1. explicit paths: exact path, path suffix, directory members (max 20), or bare
   file name; unknown paths produce a warning;
2. file and directory names against identifiers, name parts and keywords
   (inventory only, nothing read);
3. identifier content search: one regex per identifier covering its
   snake/camel/Pascal variants, word-bounded and case-sensitive, over the
   inventory (max 8 identifiers, 200 matches each). Hits on lines that look like
   definitions (`def`, `class`, `func`, `function`, `fn`, `type`, `const`,
   methods, module-level assignment) count as definitions;
4. keyword and phrase search (case-insensitive stem substring, and a
   `stem1\w*[\s_-]*stem2` phrase regex), run only when stage 3 found no
   definition and fewer than 3 files. A keyword found in at least
   max(20, 30%) of searchable files is ignored as non-discriminating, with a
   warning;
5. multi-signal bonus;
6. source ↔ test pairing for the top 10 files scoring ≥ 20, by base-name key
   (`test_foo.py`/`foo_test.py`/`foo.test.ts`/`foo.spec.ts`/`foo_test.go`/`FooTest.java`
   ↔ `foo`) within one language family, preferring the partner that shares the
   most directories;
7. one-hop import expansion from the top 5 files (first 300 lines, max 10
   imports each): Python absolute and relative imports, JS/TS relative imports,
   Go imports under the `go.mod` module, Java/Kotlin imports. No recursion;
8. documentation files: score halved.

**Weights** (a file's score is the sum of its reasons):

| Reason | Points |
|---|---|
| explicit path in task / path suffix | 100 |
| file name mentioned in task (no directory) | 80 |
| inside a directory named in the task | 30 |
| file name equals an identifier | 40 |
| defines an identifier | 30 (+2 per extra hit, max +6) |
| contains / test references an identifier | 20 (+2 per extra hit, max +6) |
| file name matches an identifier part (prefix) | 12 (8) |
| file name matches a keyword (prefix) | 10 (6) |
| directory name matches a part or keyword | 6 |
| contains a task phrase | 8 |
| contains a keyword | 4 (+1 per extra hit, max +3) |
| each distinct signal beyond the first | 5 (max 15) |
| test ↔ source partner of a top file | half the partner's score, max 20 |
| imported by a top file | 8 if the import line mentions a signal, else 3 |
| documentation file | score halved |

Ranking is score descending, then path. Every candidate carries its reasons,
matched signals and up to five content hits. Tests assert orderings, not raw
scores.

**Selectivity [M3].** `DiscoveryMetrics` reports inventory size, searchable
files, content searches, files matched, files read by the analyzer and by
discovery (bytes too), candidates, selected files, evidence items (kept and
omitted) and working-set size. On a 303-file fixture an anchored task reads 1
candidate file and 2 configuration files.

---

# 8. Context management

Three stores **[M3]** (`harness/context/manager.py`, `ContextManager`):

- **Permanent** — the original task and the repository summary. Never evicted.
  **[planned]** Also system instructions and action protocol, acceptance
  criteria, current plan and step, budget remaining.
- **Working** — `ContextItem(id, kind, source, content, priority, seq)`. Bounded
  by `ContextLimits.max_evidence_items` (count) and `max_context_chars`
  (characters). When full, the lowest-priority item is evicted first, oldest
  first among equals. An item larger than the whole budget is truncated with a
  marker. `load_working_set()` fills it from a `WorkingSet`.
  `remove_source(path)` drops a file's items (for use after an edit).
- **Episodic** — `EpisodicFact(kind, text, source, seq)`: short structured
  facts in order, deduplicated, capped at `max_facts` (oldest dropped).

**Working set [M3]** (`harness/context/working_set.py`). The repository context
a planner receives: repository summary, ranked candidates with up to 3 reasons
each, selected files (the top `max_active_files`), and evidence snippets
(`Evidence(path, line_start, line_end, snippet, reason, signal, value)`).
`build_working_set` enforces the limits on the exact rendered text:

1. evidence only from selected files, chosen by value (candidate score × kind
   weight: definition 1.0, reference 0.8, phrase 0.6, keyword 0.5, file head
   0.4) while the count ≤ `max_evidence_items` and the render ≤ `max_context_chars`;
2. each snippet ≤ `max_snippet_lines` lines (a window starting a third of the
   window above the hit), lines cut to 240 characters;
3. if the summary and candidate list alone do not fit, candidates are dropped
   from the bottom, then the summary is cut;
4. omissions are counted (`candidates_omitted`, `evidence_omitted`,
   `summary_truncated`), never silent.

Evidence is rendered in reading order (file rank, then line) with line numbers.

**Assembly.** Sizes are counted in characters (provider-independent). Tokens are
estimated as `chars / 4` unless the adapter reports real usage.

**Deduplication [M3].** The same content from the same source is stored once
(content hash). **[planned]** Re-reading an unchanged region yields a short
"unchanged since turn N" marker instead of the content.

**Compaction [planned].** When the working store exceeds its budget, M3 evicts.
Compaction replaces eviction for old observations, deterministically first:

- test output → failing test ids + first assertion/traceback lines + counts
- command output → exit code + head/tail lines
- file reads → path + line range + "available on request"

A model-written summary is used only when deterministic compaction is not enough,
and it counts as a model call.

---

# 9. Model abstraction

**[M2]** Types live in `harness/model/types.py`; all are frozen dataclasses.

```python
class ModelClient(Protocol):
    def generate(self, request: ModelRequest) -> ModelResponse: ...

ModelRequest(messages: tuple[Message, ...],
             tools: tuple[ToolDefinition, ...] = (),     # JSON-Schema tool metadata
             response_schema: Mapping | None = None,     # structured-output request
             max_output_tokens: int | None = None,
             temperature: float | None = None,
             purpose: str = "")                          # "plan", "execute", ... for accounting

Message(role: "system" | "user" | "assistant" | "tool", content: str,
        tool_calls: tuple[ToolCall, ...] = (),          # assistant only
        tool_call_id: str | None = None)                # tool only

ModelResponse(text: str,
              tool_calls: tuple[ToolCall, ...] = (),
              finish_reason: FinishReason,               # stop | length | tool_calls | content_filter | other
              usage: Usage,                              # input/output tokens, None when not reported
              provider: str | None, model: str | None,
              raw_finish_reason: str | None)             # provider's own label, diagnostics only

ToolCall(id, name, arguments: Mapping, raw_arguments: str | None, parse_error: str | None)
ModelError(message, retryable: bool)                     # adapters raise this
```

- **Provider neutrality.** Adapters translate provider payloads to and from these
  types. No provider object crosses the adapter boundary. The API key is never
  part of these types; an adapter receives it when constructed.
- **Tool calls, two routes.** If the prescribed model supports native tool
  calling, its adapter maps it to `ToolCall`. If not, the executor **[M4]**
  parses the text action protocol below into the same `ToolCall`. Either way the executor sees one shape. Malformed arguments are
  represented (`parse_error`), not raised, and the registry turns them into a
  structured `invalid_arguments` result.
- **Adapters.** **[M4]** `model/factory.create_model_client(settings, api_key)`
  selects a constructor from `ADAPTERS` by `config.model.provider` and raises
  `UnsupportedProviderError` for an unset or unknown provider. `ADAPTERS` is
  empty: the organizers have not announced the provider, model or endpoint, and
  none is assumed. **[planned]** Adapters will use `urllib.request` (no SDK).
- **Fake model [M2].** `ScriptedModel` returns queued items in order: a
  `ModelResponse`, an exception to raise, or a callable building a response from
  the request. It records every request (`requests`, `call_count`), raises
  `ScriptExhausted` when the script runs out, and fills deterministic fake usage
  (`chars // 4`) unless `fake_usage=False`. Helpers: `text_response`,
  `tool_call_response`, `malformed_tool_call_response`.
- **Metering [M2].** `MeteredModelClient(inner, metrics)` wraps any client and
  counts one model call per `generate()` attempt, failed ones included, plus
  reported token usage (§19).
- **Transient errors [planned].** Timeouts, HTTP 429 and 5xx are retried with
  exponential backoff up to a limit; every attempt counts as a model call.

**Action protocol [M4]** (`orchestrator/protocol.py`). Each executor step, the
model's whole reply is exactly one JSON object (optionally one code fence that is
the entire reply; no other markdown or prose is parsed):

```json
{"action": "tool", "tool": "read_range", "arguments": {"path": "src/x.py", "start_line": 1, "end_line": 80}}
{"action": "complete", "summary": "what was changed"}
{"action": "blocked", "reason": "why work cannot continue"}
```

Exactly these keys are allowed per form; summaries and reasons are non-empty and
at most 500 characters. A native tool call (`ModelResponse.tool_calls`) takes
precedence over the text, and exactly one is allowed. Both routes yield the same
`ToolCall` and go through `ToolRegistry.dispatch_call`. `complete` means only
"the executor believes it is done" (→ `READY_FOR_VERIFICATION`). Anything else
(prose, invalid JSON, unknown action, missing or extra fields) is a protocol
error that ends the run (`MODEL_ERROR`, `invalid_action`). There is no automatic
reprompt yet; **[planned]** one format-correction reprompt (§16).
---

# 10. Planner

**[M4]** `orchestrator/plan.py`. One model call; no tools; no retry.

Input (bounded): the system instructions; the task (capped at 8 000
characters); the task signals; `WorkingSet.render()` from M3 (bounded by
`ContextLimits`); and "Discovered commands", the M3 test/build/lint candidates
with resolved interpreters, reasons and confidence, or an explicit "none". The
planner never reads the repository itself. The request carries `PLAN_SCHEMA` as
`response_schema` for providers with structured output, and plain-text JSON
works for all others.

Output: exactly one JSON object (optionally one whole-response code fence):

```json
{"understanding": "...", "acceptance_criteria": ["..."], "hypotheses": [],
 "files_to_inspect": ["src/x.py"], "steps": [{"kind": "inspect|edit|test|other", "description": "..."}],
 "verification_candidates": ["<a discovered command, copied exactly>"], "risks": []}
```

Validation rejects: non-JSON or surrounding prose, missing or extra fields, wrong
types, empty `understanding`, no acceptance criteria, no steps, unknown step
kinds, more than 20 items, strings over 300 characters (understanding over
1 000), a tool call instead of JSON, and any verification candidate that the
harness did not discover. An unresolved spelling shown in the repository
summary (e.g. `python -m unittest …`) is accepted as an alias of the same
discovered command. Rejection ends the run in `MODEL_ERROR` with
`Failure(kind="invalid_plan")`; nothing is filled in or repaired.

Result: `TaskPlan(understanding, acceptance_criteria, hypotheses, files_to_inspect,
steps, verification_candidates: tuple[CommandCandidate], risks)`. When no command
was discovered, `verification_candidates` is empty and the planner input says so.
The verifier (M5) must handle that case.

**[planned]** Replanning on `request_replan`, repeated failure signatures (§16) or
wrong file targets, keeping evidence and failure history.

---

# 11. Tool system

**[M2]** In `harness/tools/`.

```python
Tool(name, description, parameters: JSON Schema object, handler(ctx, **args) -> data,
     category: "read" | "write" | "exec")

ToolResult(tool: str, success: bool, data: <typed dataclass> | None,
           error: ToolError(code, message, details) | None, duration_ms: int)

ToolContext(root: resolved Path, limits: ToolLimits, metrics: ExecutionMetrics)
```

- `success` means the tool did its job. A command that ran and exited non-zero is
  a successful *tool* call; the command outcome is in `CommandResult.ok`.
- Tools raise `ToolFailure(code, message, details)`; the registry converts it.
  Stable codes include `invalid_arguments`, `unknown_tool`, `tool_not_allowed`,
  `path_outside_repo`, `path_not_writable`, `not_found`, `is_directory`,
  `decode_error`, `binary_file`, `file_too_large`, `range_out_of_bounds`,
  `patch_invalid`, `patch_unsupported`, `patch_mismatch`, `patch_conflict`,
  `command_blocked`, `command_not_found`, `not_git_repo`, `git_failed`,
  `internal_error`.

**Registry (`ToolRegistry`).** Registers tools by unique name (duplicates raise),
exposes `definitions(categories)` as `ToolDefinition`s for a model request,
validates arguments against the schema subset the tools use (types, `required`,
`additionalProperties: false`, `items`, `minimum`), optionally restricts
dispatch to categories (planning = read-only), and applies a redactor
(`Config.redact`) to every string in results and errors. `dispatch()` never
raises for tool-level problems: unknown tools, bad arguments, `ToolFailure` and
unexpected exceptions all come back as structured errors. `dispatch_call(ToolCall)`
does the same for a model's tool call. `build_registry(ctx)` registers all tools.

| Tool | Category | Result data | Notes |
|---|---|---|---|
| `list_files` | read | `FileList(entries, truncated)` | Sorted walk; skips `IGNORED_DIRS`; symlinks listed, never followed; `max_depth`, `max_entries` |
| `find_files` | read | `FileList` | `fnmatch` glob: name match without `/`, path match with `/` |
| `read_file` | read | `FileContent(path, content, start_line, end_line, total_lines, truncated, size_bytes)` | UTF-8 only; binary and oversized files refused; output cut at a line boundary |
| `read_range` | read | `FileContent` | 1-based inclusive; end past EOF is clamped; start past EOF is an error with the file length |
| `search_text` | read | `SearchResult(query, matches[path, line, text], truncated, engine)` | ripgrep when installed, pure-Python fallback with identical file selection; literal by default. **[M3]** `search(..., allowed_paths=…)` (Python API, not a tool argument) restricts results to a caller-supplied inventory |
| `apply_patch` | write | `PatchResult(files[path, action, hunks, added, removed], warnings)` | §13 |
| `run_command` | exec | `CommandResult` | §25 policy, then bounded execution |
| `run_tests` | exec | `CommandResult` | Same as `run_command` for a caller-supplied test command; discovery is repository intelligence's job (§6) |
| `git_status` | read | `GitStatus(branch, entries[path, index, worktree, orig_path])` | Porcelain v1 |
| `git_diff` | read | `GitDiff(text, truncated, untracked_included)` | Untracked files included as new-file diffs by default; `paths`, `staged` |
| `git_diff_stat` | read | `GitDiffStat(files[path, added, deleted, untracked], insertions, deletions)` | `--numstat` plus untracked line counts |

`CommandResult(command, cwd, stdout, stderr, exit_code | None, timed_out,
duration_ms, truncated, stdout_bytes, stderr_bytes)`; `ok` = exit 0 and no timeout.

**Search engines.** Both engines include hidden files, ignore `.gitignore`, skip
`IGNORED_DIRS`, binary files and files over `max_file_bytes`, do not follow
symlinks, split lines on `\n` only, and sort by path then line; tests assert they
return identical results, with and without `allowed_paths`.
Only regex dialects differ (Rust `regex` vs Python `re`); a pattern ripgrep
rejects (e.g. look-around) is retried with the Python engine. Respecting
`.gitignore` is left to repository intelligence (§6).

**Git tools** run fixed harness-built commands with `GIT_OPTIONAL_LOCKS=0` (so
`status` does not even refresh the index), never stage, commit, reset or check
out, and fail with `not_git_repo` outside a work tree. The root may be a
subdirectory of the work tree; results are limited to and relative to it.

**Limits (`ToolLimits`, defaults).** `max_file_bytes` 2 000 000,
`max_read_chars` 50 000, `max_list_entries` 1 000, `max_search_results` 200,
`max_match_chars` 300, `max_output_bytes` 20 000 per stream,
`command_timeout_seconds` 300 (upper bound for any requested timeout),
`git_timeout_seconds` 60, `search_timeout_seconds` 60.

---

# 12. Executor

**[M4]** `orchestrator/executor.py`. A bounded DECIDE → ACT → OBSERVE → UPDATE loop.

```text
check budgets ─▶ build request ─▶ model.generate ─▶ parse_action ─▶ registry.dispatch_call ─▶ observe ─▶ update state
      ▲                                                                                                    │
      └────────────────────────────────────────────────────────────────────────────────────────────────────┘
```

- **One action per step.** Each iteration makes one model call and carries out at
  most one action. More than one native tool call in a response is a protocol
  error.
- **Budgets, checked before the operation that would exceed them.** Before the
  model call: `steps < max_steps` and `model_calls < max_model_calls`; before
  dispatch: `tool_calls < max_tool_calls`. Exhaustion ends the run in
  `BUDGET_EXHAUSTED` with `{budget, used, limit}` and makes no further model call.
  The planner call counts toward `max_model_calls`.
- **Actions.** `tool` → `ToolRegistry.dispatch_call` (the only way anything touches
  the repository); `complete` → `READY_FOR_VERIFICATION`; `blocked` → `BLOCKED`.
  Protocol violations → `MODEL_ERROR` (`invalid_action`); model call failures,
  including unexpected adapter exceptions → `MODEL_ERROR` (`model_call_failed`).
- **Tool results.** Every result becomes an `Observation`. Tool-level errors
  (unknown tool, invalid or unparseable arguments, path outside repository,
  blocked command, patch mismatch …) are observations and the loop continues.
  Only `internal_error` (a tool crashed) ends the run in `TOOL_ERROR`. A command
  that ran and failed is `success=True, outcome="command_failed"`, never a tool error.
- **After a successful `apply_patch`:** record `modified_files`; mark earlier
  observations of each changed file stale; `ContextManager.remove_source(path)`
  drops that file's working-set evidence. The model must read the file again.
- **Bounded request, rebuilt every step (no growing conversation):**
  `ContextManager.render()` (capped task, repository summary, current evidence),
  the plan, modified files, the last 20 action-history lines, the last 6
  observations (each ≤ 4 000 chars; command output keeps its tail), remaining
  budgets, and the tool list (also passed as `ModelRequest.tools` for native
  tool calling). Request size stops growing once the windows fill (tested).
  Window sizes are `ExecutorSettings`, not user configuration.
- **[planned]** Detecting repeated identical actions (`NO_PROGRESS`), repair and
  replanning (M5+).

---

# 13. Editing strategy

- **Formats.** **[M2]** Unified diff through the harness's own pure-Python applier
  (`tools/patch.py`); no dependency on the `patch` binary or on git. Supports
  modify, create (`--- /dev/null`), delete (`+++ /dev/null`), several files and
  hunks, `a/`/`b/` prefixes, and `\ No newline at end of file`. Renames, binary
  and mode-only patches are rejected with a clear error. **[planned]** A
  search/replace block (`path`, exact `old` text, `new` text) as a fallback format.
- **Matching [M2].** `@@` line counts are not trusted (models get them wrong);
  hunk bodies are read by prefix, and blank separator lines at the end of a hunk
  are dropped. A hunk's old lines must match exactly; the stated line number is
  only a hint, and the match closest to it wins. If nothing matches exactly, a
  match ignoring trailing whitespace is accepted and reported in `warnings`.
  Nothing fuzzier is attempted, and no model-based patch repair exists. Context
  lines keep the file's own text; line endings (LF/CRLF) and file mode are
  preserved.
- **Diagnostics [M2].** A mismatch reports the hunk, the closest match (or the
  stated position) and the first differing line, expected vs actual.
- **Validation and atomicity [M2].** Every path goes through `resolve_in_repo`
  with `for_write=True` (inside the root, not in `.git`, symlinks resolved). The
  whole patch is computed in memory; files are written only if every hunk of
  every file applies. Writes are atomic per file (temp file + `os.replace`); if a
  write fails midway, files already written are restored.
- **Snapshots [planned].** Original contents of every touched file are kept in a
  run-level `PatchRecord` for rollback across patches.
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
| Executor steps (one model decision each) | `max_steps` **[M1]**, enforced **[M4]** | 40 |
| Repair cycles | `max_repair_cycles` **[M1]** | 3 |
| Command timeout (seconds) | `command_timeout_seconds` **[M1]**, enforced by the command tools **[M2]**, wired from config into the run's `ToolLimits` **[M4]** | 300 |
| Model calls (planner + executor, failures included) | `max_model_calls` **[M4]** | 60 |
| Tool calls (every dispatch) | `max_tool_calls` **[M4]** | 80 |
| Context size (characters) | `max_context_chars` [planned] | 60 000 |
| Per-tool output caps | `ToolLimits` **[M2]** (§11); not yet wired to configuration | see §11 |
| Working-set files / candidates / evidence / snippet lines / chars | `ContextLimits` **[M3]** (§8, §21) | 8 / 25 / 24 / 30 / 24 000 |
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

**[M2] Counters** — `ExecutionMetrics` in `harness/metrics.py`, shared by the model
wrapper and the tool registry through `ToolContext.metrics`:

| Counter | Incremented |
|---|---|
| `model_calls`, `model_failures` | once per `MeteredModelClient.generate()` attempt; failures included |
| `input_tokens`, `output_tokens` | from reported usage; `model_calls_without_usage` counts calls that reported none |
| `tool_calls`, `tool_failures`, `tool_calls_by_name` | once per `ToolRegistry.dispatch()`, including unknown tools, invalid arguments and blocked commands |
| `command_calls` | once per caller-supplied command actually launched by `run_command`/`run_tests`; blocked commands and internal git/ripgrep processes are not counted here |

**[planned]** Also: model calls by purpose and latency, command durations, test
runs (targeted vs full), repair cycles, replans, state transitions, wall-clock time.

**[planned] Outputs**, written to the run directory (`.harness-runs/<run_id>/`
under the current working directory by default, configurable, never inside the
target repo):

- `events.jsonl` — one structured event per transition, model call and tool call
- `state.json` — latest `RunState`
- `report.md` — human summary: outcome, evidence ids, diff stat, counters

All text written or printed passes through a redaction filter that replaces
the API key value with `***` (the registry already does this for tool results).

---

# 20. CLI

Entry points: `python -m harness` and the `harness` console script (installed by
`make setup` when possible). `make run` calls `python -m harness run`.

```text
harness run [--repo PATH] [--task TEXT | --task-file FILE]
harness inspect --repo PATH [--task TEXT | --task-file FILE] [--top N] [--show-context]
```

- **[M3]** `inspect` prints the `RepoProfile`; with a task, also the task
  signals, the top N candidates with reasons (`*` = selected for the working
  set), working-set size and discovery metrics. `--show-context` prints the
  rendered working set. It never loads the API key (only `ContextLimits`), makes
  no model call and does not modify the repository. Output has no timestamps or
  timings, so it is deterministic. If `AI_API_KEY` is set, its value is still
  redacted from the output.

- **[M1]** Missing `--repo` or task → interactive prompts:
  `Repository path:` then `Task / GitHub issue:`. The task prompt accepts
  multi-line input terminated by an empty line or end-of-input (so pasted issues work).
- **[M1]** `--task` and `--task-file` are mutually exclusive.
- **[M4]** After validation the CLI prints the accepted configuration and input
  (secret redacted), then asks `model/factory.create_model_client` for the
  configured provider. `ADAPTERS` is empty in this build, so it stops with
  "No model provider is configured …" or "Configured model provider is not
  supported by this build: '<name>' …", states that no model was called and
  the repository was not modified, points to `harness inspect`, and exits 2.
  When an adapter exists, it runs the `Orchestrator` and prints the run
  summary (terminal phase, meaning, counts, modified files, acceptance
  criteria, verification candidates). It never claims verification.
  ScriptedModel runs go through the orchestrator API in tests, not a CLI flag.
- **[planned]** stdin task input, `make run` convenience variables, `--rollback-on-failure`,
  `--json` report output.

Exit codes:

| Code | Meaning |
|---|---|
| 0 | Run reached `READY_FOR_VERIFICATION` (M4; not a verification verdict) |
| 1 | Run ended in another terminal phase (M4); verification failed [planned M5] |
| 2 | Usage, configuration or input error, including an unsupported/unset model provider |
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
| `HARNESS_MAX_MODEL_CALLS` | `limits.max_model_calls` **[M4]** | no | 60 |
| `HARNESS_MAX_TOOL_CALLS` | `limits.max_tool_calls` **[M4]** | no | 80 |
| `HARNESS_MAX_ACTIVE_FILES` | `context.max_active_files` **[M3]** | no | 8 |
| `HARNESS_MAX_CANDIDATES` | `context.max_candidates` **[M3]** | no | 25 |
| `HARNESS_MAX_EVIDENCE_ITEMS` | `context.max_evidence_items` **[M3]** | no | 24 |
| `HARNESS_MAX_SNIPPET_LINES` | `context.max_snippet_lines` **[M3]** | no | 30 |
| `HARNESS_MAX_CONTEXT_CHARS` | `context.max_context_chars` **[M3]** | no | 24 000 |

`load_context_limits()` reads only the `HARNESS_MAX_*` context variables and
needs no API key (used by `inspect`); `load_config()` includes them as
`Config.context`.

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
│   ├── metrics.py            [M2] ExecutionMetrics
│   ├── telemetry.py          [planned]
│   ├── model/                [M2] types.py, client.py (ModelClient, MeteredModelClient), fake.py
│   │                         [M4] factory.py (create_model_client; ADAPTERS empty)
│   │   └── adapters/         [planned] one module per provider, once announced
│   ├── tools/                [M2] base.py, registry.py, paths.py, files.py, search.py,
│   │                              patch.py, commands.py, git.py
│   ├── repo/                 [M3] classify.py, inventory.py, reader.py, facts.py, commands.py,
│   │                              profile.py, signals.py, discovery.py, report.py
│   ├── context/              [M3] working_set.py, manager.py   (compaction.py planned)
│   ├── orchestrator/         [M4] state.py, protocol.py, plan.py, observe.py, executor.py,
│   │                              interpreter.py, orchestrator.py, report.py
│   └── verify/               [planned] verifier.py, ledger.py, failures.py
└── tests/
    ├── test_config.py        [M1]
    ├── test_cli.py           [M1]
    ├── helpers.py            [M2] temp repos with isolated git config
    ├── test_model.py  test_registry.py  test_file_tools.py  test_search.py   [M2]
    ├── test_patch.py  test_git_tools.py  test_commands.py  test_secrets.py   [M2]
    ├── repo_fixtures.py      [M3] Python/TS/Go/noisy/non-git/large miniature repositories
    ├── test_repo_inventory.py  test_repo_profile.py  test_task_signals.py  test_discovery.py   [M3]
    ├── test_working_set.py  test_context_manager.py  test_command_discovery.py  test_inspect_cli.py   [M3]
    ├── orchestration_helpers.py  [M4] buggy repository + scripted responses
    ├── test_run_state.py  test_planner.py  test_action_protocol.py  test_orchestrator.py   [M4]
    ├── test_interpreter.py  test_model_factory.py   [M4]
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
- **End-to-end tests.** The full state machine with `ScriptedModel` on a fixture
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
| `make setup` | Creates `.venv` with `$(PYTHON)` (default `python3`, must be ≥ 3.10). Idempotent. Needs no network. Writes a `.pth` file and tries an editable install (for the `harness` console script); if that fails (e.g. offline), it warns and continues. |
| `make run` | Runs `python -m harness run` from the venv with `PYTHONPATH=src`. Interactive when no arguments are given. Optional `ARGS="--repo … --task …"` passes flags through; never required. |
| `make test` | Runs the unittest suite from the venv with `PYTHONPATH=src`. Needs no network and no API key. |
| `make clean` | Removes `.venv`, caches, build output and `.harness-runs`. Never touches `.env` or source files. |

Targets set `PYTHONPATH=src` explicitly instead of relying on the `.pth` file:
Python ≥ 3.13 skips `.pth` files carrying the macOS "hidden" flag, which was
observed being set on `.venv` after creation (M2 fix). The `.pth` file and the
console script remain conveniences for running from the venv directly.

The evaluator flow is exactly:

```sh
export AI_API_KEY="..."
make setup
make run
```

---

# 25. Security boundaries

- **Secret.** `AI_API_KEY` is read from the environment or `.env`, excluded from
  `repr`, redacted from CLI output and **[M2]** from every string in tool results
  and errors (registry redactor), and **[M2] removed from the environment of every
  child process** (`SCRUBBED_ENV_VARS`), so code in the target repository (its
  tests, build scripts) cannot read it. The tool layer never holds the key value;
  it receives only the `Config.redact` function.
- **File-system confinement [M2].** `tools/paths.resolve_in_repo` is the single
  boundary function used by every tool: it rejects empty/NUL/`~` paths, resolves
  relative paths against the root (or a validated cwd), follows symlinks, and
  requires the result to be the root or inside it. Absolute paths are allowed only
  inside the root. `for_write=True` also rejects anything in `.git/`. Walkers never
  follow symlinks.
- **Command execution [M2].** No implicit shell: commands are an argv list or a
  string split with POSIX `shlex` rules; a string containing shell operators
  (`| && ; > < & ( )` or newline) is rejected, not misinterpreted. A shell is used
  only when asked for explicitly (`["bash", "-c", script]`), and that script is
  checked by the same policy. Execution: `cwd` validated inside the repo, stdin
  closed, output to temp files and truncated (head 40% / tail 60%), timeout capped
  by configuration, the whole process group killed on timeout, env scrubbed.
- **Command policy [M2]** — deliberately small, a guardrail not a sandbox:
  - blocked programs: `sudo su doas pkexec shutdown reboot halt poweroff init
    telinit mkfs mkfs.* fdisk sfdisk cfdisk parted wipefs diskutil`;
  - `rm rmdir unlink shred chmod chown chgrp` and `dd of=`: every path operand must
    resolve inside the repo, not be the root or `.git`, and contain no shell
    expansion (`$ * ? [ ~` backtick);
  - `git`: read-only subcommands only (`status diff log show ls-files ls-tree
    rev-parse rev-list grep blame describe shortlog cat-file show-ref name-rev
    merge-base version help`), no global options except `-C <dir in repo>`,
    `--no-pager`, `-P`, `--no-optional-locks`, `--literal-pathspecs`; no
    `--output` or `-O`;
  - wrappers are looked through: `env`, `nohup`, `nice`, `time`, `timeout`,
    `command`, `exec`, leading `VAR=value`; `xargs` feeding a guarded program is blocked;
  - `sh/bash/zsh/dash/ksh/fish -c` scripts are split on operators and each
    command is checked; `>` redirect targets must be inside the repo (or
    `/dev/null`); any blocked program name anywhere in the script (e.g. inside
    `$(...)`) blocks it.
- **Not a sandbox.** The policy cannot see what a program does internally
  (`python -c`, `make`, test code, script files). The harness does not isolate
  network or file system at the OS level; test code in the target repo runs with
  the user's permissions. Documented, not hidden.
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

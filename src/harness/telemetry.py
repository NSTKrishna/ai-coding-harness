"""Durable run artifacts: events.jsonl, summary.json, final_report.md, final.diff.

Artifact root (one deterministic rule, never inside the target repository):

1. ``HARNESS_RUNS_DIR`` / ``Config.runs_dir`` if set;
2. otherwise ``<harness checkout>/.harness/runs`` when running from a source
   checkout (the evaluator flow: ``make run`` in this repository);
3. otherwise ``$XDG_CACHE_HOME/coding-harness/runs`` (``~/.cache/...``).

If the chosen directory is the target repository or inside it, the cache
location (3) is used instead, so telemetry can never appear in the target's diff.
Each run writes to ``<root>/<run_id>/``.

Everything written is bounded (short metadata, capped excerpts, no prompts, no
model output other than the protocol's short summaries) and passes through the
run's redactor; the API key is never part of any structure written here.
"""

from __future__ import annotations

import dataclasses
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

MAX_META_CHARS = 300
MAX_META_ITEMS = 20
MAX_EVENTS = 5_000
MAX_TASK_CHARS = 8_000
MAX_DIFF_BYTES = 200_000
SUMMARY_SCHEMA = "harness-run-summary/1"


def _cache_runs_dir() -> Path:
    cache = os.environ.get("XDG_CACHE_HOME") or str(Path.home() / ".cache")
    return Path(cache) / "coding-harness" / "runs"


def default_runs_dir() -> Path:
    project = Path(__file__).resolve().parents[2]
    if (project / "pyproject.toml").is_file() and (project / "src" / "harness").is_dir():
        return project / ".harness" / "runs"
    return _cache_runs_dir()


def resolve_runs_dir(configured: Optional[Path], target_repo: Optional[Path] = None) -> tuple[Path, Optional[str]]:
    """Returns (directory, note). The note explains a relocation away from the target."""
    chosen = Path(configured or default_runs_dir()).expanduser().resolve()
    if target_repo is not None:
        target = Path(target_repo).expanduser().resolve()
        if chosen == target or target in chosen.parents:
            fallback = _cache_runs_dir().resolve()
            return fallback, f"runs directory {chosen} is inside the target repository; using {fallback}"
    return chosen, None


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _bounded(value: Any, redact: Callable[[str], str]) -> Any:
    if isinstance(value, str):
        value = redact(value)
        return value if len(value) <= MAX_META_CHARS else value[: MAX_META_CHARS - 3] + "..."
    if isinstance(value, (list, tuple)):
        return [_bounded(v, redact) for v in list(value)[:MAX_META_ITEMS]]
    if isinstance(value, dict):
        return {str(k): _bounded(v, redact) for k, v in list(value.items())[:MAX_META_ITEMS]}
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return _bounded(str(value), redact)


class RunRecorder:
    def __init__(self, runs_dir: Path, redact: Optional[Callable[[str], str]] = None) -> None:
        self.runs_dir = Path(runs_dir)
        self.redact = redact or (lambda text: text)
        self.run_dir: Optional[Path] = None
        self.state = None
        self._events = None
        self._seq = 0
        self.events_dropped = 0

    # lifecycle ------------------------------------------------------------------
    def start(self, state) -> None:
        self.state = state
        self.run_dir = self.runs_dir / state.run_id
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self._events = open(self.run_dir / "events.jsonl", "a", encoding="utf-8")
        state.event_sink = self._sink
        self.event("run_started", task=state.task.strip().splitlines()[0] if state.task.strip() else "",
                   repository=state.repo_root)

    def _sink(self, name: str, metadata: dict) -> None:
        self.event(name, **metadata)

    def event(self, name: str, **metadata) -> None:
        if self._events is None:
            return
        if self._seq >= MAX_EVENTS:
            self.events_dropped += 1
            return
        self._seq += 1
        record = {"ts": _now(), "seq": self._seq, "run_id": self.state.run_id,
                  "phase": self.state.phase.value, "event": name, **_bounded(metadata, self.redact)}
        self._events.write(json.dumps(record, sort_keys=True, default=str) + "\n")
        self._events.flush()

    def finish(self, state, ctx=None) -> Optional[Path]:
        if self.run_dir is None:
            return None
        from harness.orchestrator.report import render_markdown
        self.event("run_finished", final_status=state.phase.value, terminal_reason=state.terminal_reason or "",
                   model_calls=state.model_calls, tool_calls=state.tool_calls, steps=state.steps,
                   repair_cycles=state.repair_cycles, events_dropped=self.events_dropped)
        self._events.close()
        self._events = None
        state.event_sink = None
        diff_written = self._write_diff(state, ctx)
        summary = build_summary(state, diff_written)
        (self.run_dir / "summary.json").write_text(
            self.redact(json.dumps(summary, indent=2, sort_keys=True, default=str)) + "\n", encoding="utf-8")
        (self.run_dir / "final_report.md").write_text(self.redact(render_markdown(state)), encoding="utf-8")
        return self.run_dir

    def _write_diff(self, state, ctx) -> bool:
        profile = state.repo_profile
        changes = state.changes
        if ctx is None or profile is None or not profile.is_git or changes is None or not changes.net_changed():
            return False
        from harness.tools.base import ToolFailure
        from harness.tools.git import git_diff
        paths = list(changes.net_changed())
        try:
            diff = git_diff(ctx, paths=paths)
        except ToolFailure:
            return False
        baseline = state.baseline
        preexisting = sorted(baseline.initial.paths) if baseline is not None and baseline.initial.available else []
        header = [f"# Harness-attributed changes of run {state.run_id}: {', '.join(paths)}",
                  "# Only these paths are included. Changes that existed before the run are not part of it."]
        dirty_before = sorted(set(preexisting) & set(paths))
        if dirty_before:
            header.append("# NOTE: already modified before the run (their diff includes those earlier edits): "
                          + ", ".join(dirty_before))
        other = sorted(set(preexisting) - set(paths))
        if other:
            header.append("# Pre-existing changes NOT made by this run (not included): " + ", ".join(other))
        text = "\n".join(header) + "\n" + diff.text
        (self.run_dir / "final.diff").write_text(self.redact(text)[:MAX_DIFF_BYTES], encoding="utf-8")
        return True

    # wrappers -------------------------------------------------------------------
    def wrap_model(self, model):
        return RecordingModel(model, self)

    def wrap_registry(self, registry):
        return RecordingRegistry(registry, self)


class RecordingModel:
    """Records one start/finish event per model call; never records prompts or outputs."""

    def __init__(self, inner, recorder: RunRecorder) -> None:
        self.inner, self.recorder = inner, recorder

    def generate(self, request):
        size = sum(len(m.content) for m in request.messages)
        self.recorder.event("model_call_started", purpose=request.purpose, messages=len(request.messages),
                            request_chars=size, tools=len(request.tools))
        start = time.monotonic()
        try:
            response = self.inner.generate(request)
        except Exception as exc:
            self.recorder.event("model_call_failed", purpose=request.purpose, error=exc.__class__.__name__,
                                message=str(exc)[:200], duration_ms=int((time.monotonic() - start) * 1000))
            raise
        self.recorder.event("model_call_completed", purpose=request.purpose,
                            duration_ms=int((time.monotonic() - start) * 1000),
                            input_tokens=response.usage.input_tokens, output_tokens=response.usage.output_tokens,
                            tool_calls=len(response.tool_calls), finish_reason=response.finish_reason.value,
                            response_chars=len(response.text))
        return response


class RecordingRegistry:
    """Proxies a ToolRegistry, recording each dispatch (the registry still does the counting)."""

    def __init__(self, inner, recorder: RunRecorder) -> None:
        self.inner, self.recorder = inner, recorder

    def __getattr__(self, name):
        return getattr(self.inner, name)

    def definitions(self, *args, **kwargs):
        return self.inner.definitions(*args, **kwargs)

    def _record(self, name: str, arguments, run):
        from harness.model.types import ToolCall
        from harness.orchestrator.observe import summarize_arguments
        summary = summarize_arguments(ToolCall("-", name, dict(arguments or {})))
        self.recorder.event("tool_call_started", tool=name, arguments=summary)
        result = run()
        meta = {"tool": name, "success": result.success, "duration_ms": result.duration_ms}
        if result.error is not None:
            meta["error_code"] = result.error.code
        data = result.data
        if data is not None and hasattr(data, "exit_code") and hasattr(data, "timed_out"):
            meta.update(exit_code=data.exit_code, timed_out=data.timed_out)
        self.recorder.event("tool_call_completed", **meta)
        return result

    def dispatch(self, name, arguments=None, **kwargs):
        return self._record(name, arguments, lambda: self.inner.dispatch(name, arguments, **kwargs))

    def dispatch_call(self, call, **kwargs):
        args = call.arguments if call.parse_error is None else {}
        return self._record(call.name, args, lambda: self.inner.dispatch_call(call, **kwargs))


# --------------------------------------------------------------------------
# summary.json
# --------------------------------------------------------------------------

def _asdict(obj) -> Any:
    return dataclasses.asdict(obj) if dataclasses.is_dataclass(obj) else obj


def build_summary(state, diff_written: bool = False) -> dict:
    report = state.last_report
    profile = state.repo_profile
    evidence = state.evidence.items if state.evidence is not None else ()
    by_kind: dict[str, int] = {}
    for item in evidence:
        by_kind[item.kind.value] = by_kind.get(item.kind.value, 0) + 1
    baseline = state.baseline
    criteria = (
        [{"criterion": c.criterion, "status": c.status, "evidence_ids": list(c.evidence_ids), "notes": c.notes}
         for c in report.criteria_results] if report is not None else
        [{"criterion": c, "status": "UNKNOWN", "evidence_ids": [], "notes": "not verified"}
         for c in (state.plan.acceptance_criteria if state.plan is not None else ())]
    )
    discovery = state.discovery
    changed = list(state.changes.net_changed()) if state.changes is not None else list(state.modified_files)
    return {
        "schema": SUMMARY_SCHEMA,
        "run_id": state.run_id,
        "task": state.task[:MAX_TASK_CHARS],
        "final_status": state.phase.value,
        "terminal_reason": state.terminal_reason,
        "failure": _asdict(state.failure) if state.failure is not None else None,
        "started_at": datetime.fromtimestamp(state.started_at, timezone.utc).isoformat(timespec="seconds"),
        "elapsed_seconds": round(state.elapsed_seconds, 3),
        "repository": {"root": state.repo_root, "is_git": bool(profile and profile.is_git),
                       "files": profile.file_count if profile else None},
        "modified_files": changed,
        "model_calls": state.model_calls,
        "tool_calls": state.tool_calls,
        "verification_command_calls": sum(1 for i in evidence if i.command_id and i.result != "NOT_RUN"),
        "command_calls": state.metrics.command_calls,
        "tool_calls_by_name": dict(state.metrics.tool_calls_by_name),
        "tokens": {"input": state.metrics.input_tokens, "output": state.metrics.output_tokens,
                   "calls_without_usage": state.metrics.model_calls_without_usage,
                   "source": "as reported by the model client"},
        "steps": state.steps,
        "repair_cycles": state.repair_cycles,
        "repeated_failures": state.repeated_failures,
        "no_progress": state.no_progress,
        "targeting": ({"targets": [{"command": " ".join(t.argv), "derived_from": t.derived_from,
                                    "confidence": t.confidence, "parent_command_id": t.parent_command_id,
                                    "reason": t.reason} for t in state.targeting.targets],
                       "unavailable_reason": state.targeting.unavailable_reason}
                      if state.targeting is not None else None),
        "verification_commands": [{"id": c.id, "level": c.level, "purpose": c.purpose, "kind": c.kind,
                                   "command": c.text, "within_cap": c.within_cap, "parent_id": c.parent_id}
                                  for c in state.verification_commands],
        "baseline_summary": ({"available": baseline.available,
                              "commands": [{"id": r.command.id, "status": r.classification.status.value,
                                            "exit_code": r.classification.exit_code, "evidence_id": r.evidence_id}
                                           for r in baseline.runs],
                              "not_run": list(baseline.not_run)} if baseline is not None else None),
        "verification_summary": [
            {"round": r.round, "verdict": r.verdict.value,
             "failure_class": r.failure_class.value if r.failure_class else None, "summary": r.summary,
             "commands": [{"id": c.command.id, "level": c.command.level, "purpose": c.command.purpose,
                           "baseline_status": c.baseline.classification.status.value if c.baseline else None,
                           "post_status": c.post.classification.status.value, "comparison": c.comparison.value,
                           "evidence_ids": [e for e in (c.baseline.evidence_id if c.baseline else None,
                                                        c.post.evidence_id) if e],
                           "note": c.note} for c in r.command_results]}
            for r in state.verification_reports],
        "acceptance_criteria": criteria,
        "evidence_summary": {
            "count": len(evidence), "by_kind": by_kind,
            "items": [{"id": i.id, "kind": i.kind.value, "phase": i.phase, "result": i.result,
                       "command_id": i.command_id, "path": i.path, "description": i.description[:200],
                       "refs": list(i.refs)} for i in evidence],
        },
        "context_metrics": {
            "compactions": [dict(_asdict(r), observations_dropped=r.observations_dropped)
                            for r in state.compaction.records],
            "discovery": _asdict(discovery.metrics) if discovery is not None else None,
            "working_set_chars": discovery.working_set.estimated_chars if discovery is not None else None,
        },
        "unresolved_risks": list(report.risks) if report is not None else [],
        "completion_claims_not_evidence": [text for _, text in state.completion_claims],
        "artifacts": {"events": "events.jsonl", "report": "final_report.md",
                      "diff": "final.diff" if diff_written else None},
    }


# --------------------------------------------------------------------------
# reading runs back (harness runs / harness report)
# --------------------------------------------------------------------------

def list_runs(runs_dir: Path, limit: int = 20) -> list[dict]:
    if not runs_dir.is_dir():
        return []
    rows = []
    for summary_path in runs_dir.glob("*/summary.json"):
        try:
            data = json.loads(summary_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        rows.append({"run_id": data.get("run_id", summary_path.parent.name), "final_status": data.get("final_status"),
                     "started_at": data.get("started_at") or "", "task": (data.get("task") or "").strip().splitlines()[:1]})
    rows.sort(key=lambda r: (r["started_at"], r["run_id"]), reverse=True)
    return rows[:limit]

"""Turn a ToolResult into a bounded, factual Observation.

Excerpts are cut to ``max_chars`` (on top of the M2 tool limits). Command
output keeps the tail, where failures usually show. For command tools,
``success`` (the tool worked) and ``outcome`` (what the command did) are
recorded separately: a failing test run is ``success=True,
outcome="command_failed"``.
"""

from __future__ import annotations

import json
import re
from typing import Any, Mapping

from harness.model.types import ToolCall
from harness.orchestrator.state import Observation
from harness.tools.base import ToolResult
from harness.tools.commands import CommandResult
from harness.tools.files import FileContent, FileList
from harness.tools.git import GitDiff, GitDiffStat, GitStatus
from harness.tools.patch import PatchResult
from harness.tools.search import SearchResult

MAX_ARGUMENT_SUMMARY = 200
MAX_LISTED = 20


def _cut(text: str, limit: int, *, keep: str = "head") -> str:
    if len(text) <= limit:
        return text
    marker = f"\n[... {len(text) - limit} characters omitted ...]\n"
    room = max(limit - len(marker), 0)
    if keep == "tail":
        return marker.lstrip("\n") + text[-room:]
    head = text[:room]
    cut = head.rfind("\n")
    return (head[: cut + 1] if cut > room // 2 else head) + marker.rstrip("\n")


def summarize_arguments(call: ToolCall) -> str:
    args: Mapping[str, Any] = call.arguments
    if call.parse_error:
        return f"<unparseable arguments: {call.parse_error}>"[:MAX_ARGUMENT_SUMMARY]
    if call.name == "apply_patch" and isinstance(args.get("patch"), str):
        patch = args["patch"]
        files = re.findall(r"^\+\+\+ (?:b/)?(\S+)", patch, re.MULTILINE)
        return f"patch of {patch.count(chr(10)) + 1} lines for {', '.join(files) or '?'}"[:MAX_ARGUMENT_SUMMARY]
    try:
        text = json.dumps(dict(args), sort_keys=True)
    except (TypeError, ValueError):
        text = repr(dict(args))
    return text if len(text) <= MAX_ARGUMENT_SUMMARY else text[: MAX_ARGUMENT_SUMMARY - 3] + "..."


def observe(step: int, call: ToolCall, result: ToolResult, max_chars: int) -> Observation:
    args = summarize_arguments(call)
    if not result.success:
        error = result.error
        return Observation(step, call.name, args, success=False, outcome="tool_error",
                           result_summary=_cut(f"{error.code}: {error.message}", max_chars),
                           error_code=error.code)
    data = result.data
    if isinstance(data, CommandResult):
        outcome = "command_timed_out" if data.timed_out else "command_ok" if data.ok else "command_failed"
        half = max_chars // 2
        text = (f"$ {' '.join(data.command)}  (cwd {data.cwd}, exit_code={data.exit_code}, "
                f"timed_out={data.timed_out}, {data.duration_ms} ms)\n"
                f"--- stdout ---\n{_cut(data.stdout, half, keep='tail')}\n"
                f"--- stderr ---\n{_cut(data.stderr, half, keep='tail')}")
        return Observation(step, call.name, args, True, outcome, _cut(text, max_chars, keep="tail"),
                           exit_code=data.exit_code, timed_out=data.timed_out)
    if isinstance(data, FileContent):
        header = (f"{data.path} lines {data.start_line}-{data.end_line} of {data.total_lines}"
                  + (" (truncated by the tool; use read_range)" if data.truncated else "") + "\n")
        return Observation(step, call.name, args, True, "ok", _cut(header + data.content, max_chars),
                           affected_paths=(data.path,))
    if isinstance(data, PatchResult):
        lines = [f"{f.action} {f.path} (+{f.added} -{f.removed}, {f.hunks} hunk(s))" for f in data.files]
        lines += [f"warning: {w}" for w in data.warnings]
        return Observation(step, call.name, args, True, "ok", _cut("\n".join(lines), max_chars),
                           affected_paths=data.changed_files)
    if isinstance(data, SearchResult):
        lines = [f"{len(data.matches)} match(es){' (truncated)' if data.truncated else ''}"]
        lines += [f"{m.path}:{m.line}: {m.text}" for m in data.matches]
        paths = tuple(dict.fromkeys(m.path for m in data.matches))[:MAX_LISTED]
        return Observation(step, call.name, args, True, "ok", _cut("\n".join(lines), max_chars), affected_paths=paths)
    if isinstance(data, FileList):
        lines = [f"{len(data.entries)} entr{'y' if len(data.entries) == 1 else 'ies'}"
                 f"{' (truncated)' if data.truncated else ''}"] + [f"{e.kind} {e.path}" for e in data.entries]
        return Observation(step, call.name, args, True, "ok", _cut("\n".join(lines), max_chars))
    if isinstance(data, GitStatus):
        lines = [f"branch {data.branch or '(detached)'}; {'clean' if data.clean else f'{len(data.entries)} change(s)'}"]
        lines += [f"{e.index}{e.worktree} {e.path}" for e in data.entries]
        return Observation(step, call.name, args, True, "ok", _cut("\n".join(lines), max_chars))
    if isinstance(data, GitDiff):
        return Observation(step, call.name, args, True, "ok", _cut(data.text or "(no changes)", max_chars))
    if isinstance(data, GitDiffStat):
        lines = [data.summary] + [f"{f.path} +{f.added} -{f.deleted}" for f in data.files]
        return Observation(step, call.name, args, True, "ok", _cut("\n".join(lines), max_chars))
    return Observation(step, call.name, args, True, "ok", _cut(repr(data), max_chars))

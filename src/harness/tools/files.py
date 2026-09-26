"""Read-only file tools: list_files, find_files, read_file, read_range."""

from __future__ import annotations

import fnmatch
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from harness.tools.base import Tool, ToolContext, ToolFailure
from harness.tools.paths import iter_repo_files, relative_posix, resolve_in_repo

BINARY_SNIFF_BYTES = 8192


@dataclass(frozen=True)
class FileEntry:
    path: str   # repository-relative, forward slashes
    kind: str   # "file", "dir" or "symlink"
    size: Optional[int] = None  # bytes, files only


@dataclass(frozen=True)
class FileList:
    entries: tuple[FileEntry, ...]
    truncated: bool  # more entries existed than the limit allowed


@dataclass(frozen=True)
class FileContent:
    path: str
    content: str
    start_line: int      # 1-based, inclusive
    end_line: int        # 1-based, inclusive; 0 for an empty file
    total_lines: int
    truncated: bool      # output stopped before end_line requested/available
    size_bytes: int


def _directory(ctx: ToolContext, path: str) -> Path:
    resolved = resolve_in_repo(ctx.root, path)
    if not resolved.exists():
        raise ToolFailure("not_found", f"'{path}' does not exist")
    if not resolved.is_dir():
        raise ToolFailure("not_a_directory", f"'{path}' is not a directory")
    return resolved


def list_files(ctx: ToolContext, path: str = ".", max_depth: Optional[int] = None,
               max_entries: Optional[int] = None) -> FileList:
    start = _directory(ctx, path)
    limit = min(max_entries or ctx.limits.max_list_entries, ctx.limits.max_list_entries)
    entries: list[FileEntry] = []
    for item, kind in iter_repo_files(ctx.root, start, include_dirs=True, max_depth=max_depth):
        if len(entries) >= limit:
            return FileList(tuple(entries), truncated=True)
        size = item.stat().st_size if kind == "file" else None
        entries.append(FileEntry(relative_posix(ctx.root, item), kind, size))
    return FileList(tuple(entries), truncated=False)


def find_files(ctx: ToolContext, pattern: str, path: str = ".",
               max_results: Optional[int] = None) -> FileList:
    """Match files by glob (``fnmatch``, case-sensitive).

    A pattern without ``/`` matches the file name; with ``/`` it matches the
    repository-relative path. ``*`` also matches across ``/``.
    """
    if not pattern.strip():
        raise ToolFailure("invalid_arguments", "pattern must not be empty")
    start = _directory(ctx, path)
    limit = min(max_results or ctx.limits.max_list_entries, ctx.limits.max_list_entries)
    match_path = "/" in pattern
    entries: list[FileEntry] = []
    for item, kind in iter_repo_files(ctx.root, start):
        if kind != "file":
            continue
        rel = relative_posix(ctx.root, item)
        subject = rel if match_path else item.name
        if fnmatch.fnmatchcase(subject, pattern):
            if len(entries) >= limit:
                return FileList(tuple(entries), truncated=True)
            entries.append(FileEntry(rel, "file", item.stat().st_size))
    return FileList(tuple(entries), truncated=False)


def load_text_lines(ctx: ToolContext, path: str) -> tuple[Path, list[str], int]:
    """Read a UTF-8 text file inside the repository. Returns (path, lines, size).

    Lines keep their line endings. Shared by the read tools and the patcher.
    """
    resolved = resolve_in_repo(ctx.root, path)
    if not resolved.exists():
        raise ToolFailure("not_found", f"'{path}' does not exist")
    if resolved.is_dir():
        raise ToolFailure("is_directory", f"'{path}' is a directory; use list_files")
    if not resolved.is_file():
        raise ToolFailure("not_a_file", f"'{path}' is not a regular file")
    size = resolved.stat().st_size
    if size > ctx.limits.max_file_bytes:
        raise ToolFailure(
            "file_too_large",
            f"'{path}' is {size} bytes; the limit is {ctx.limits.max_file_bytes}",
            {"size_bytes": size},
        )
    data = resolved.read_bytes()
    if b"\x00" in data[:BINARY_SNIFF_BYTES]:
        raise ToolFailure("binary_file", f"'{path}' looks like a binary file")
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ToolFailure(
            "decode_error", f"'{path}' is not valid UTF-8 (byte {exc.start})", {"byte_offset": exc.start}
        ) from None
    return resolved, text.splitlines(keepends=True), size


def _content(ctx: ToolContext, resolved: Path, lines: list[str], size: int,
             start: int, end: int) -> FileContent:
    """Lines ``start..end`` (1-based, inclusive), cut at a line boundary to fit the char cap."""
    budget = ctx.limits.max_read_chars
    taken: list[str] = []
    used = 0
    truncated = False
    for line in lines[start - 1:end]:
        if used + len(line) > budget:
            if not taken:  # a single line longer than the whole budget
                taken.append(line[:budget])
            truncated = True
            break
        taken.append(line)
        used += len(line)
    return FileContent(
        path=relative_posix(ctx.root, resolved),
        content="".join(taken),
        start_line=start if taken else 0,
        end_line=start + len(taken) - 1 if taken else 0,
        total_lines=len(lines),
        truncated=truncated,
        size_bytes=size,
    )


def read_file(ctx: ToolContext, path: str) -> FileContent:
    resolved, lines, size = load_text_lines(ctx, path)
    return _content(ctx, resolved, lines, size, 1, len(lines))


def read_range(ctx: ToolContext, path: str, start_line: int, end_line: int) -> FileContent:
    """Lines ``start_line..end_line``. ``end_line`` past the end is clamped;
    ``start_line`` past the end is an error that reports the file length."""
    if start_line < 1 or end_line < 1:
        raise ToolFailure("invalid_arguments", "line numbers start at 1")
    if start_line > end_line:
        raise ToolFailure("invalid_arguments", f"start_line ({start_line}) is after end_line ({end_line})")
    resolved, lines, size = load_text_lines(ctx, path)
    if start_line > len(lines):
        raise ToolFailure(
            "range_out_of_bounds",
            f"'{path}' has {len(lines)} lines; start_line {start_line} is past the end",
            {"total_lines": len(lines)},
        )
    return _content(ctx, resolved, lines, size, start_line, min(end_line, len(lines)))


_PATH = {"type": "string", "description": "Repository-relative path."}
_DIR = {"type": "string", "description": "Repository-relative directory (default: repository root)."}

TOOLS = (
    Tool(
        name="list_files",
        description="List files and directories under a directory (sorted, skips .git, node_modules, "
                    "virtualenvs and caches; symlinks are listed but not followed).",
        parameters={
            "type": "object",
            "properties": {
                "path": _DIR,
                "max_depth": {"type": "integer", "minimum": 1, "description": "1 = direct children only."},
                "max_entries": {"type": "integer", "minimum": 1},
            },
            "additionalProperties": False,
        },
        handler=list_files,
        category="read",
    ),
    Tool(
        name="find_files",
        description="Find files by glob pattern. Without '/', the pattern matches file names "
                    "(e.g. 'test_*.py'); with '/', it matches repository-relative paths (e.g. 'src/*/models.py').",
        parameters={
            "type": "object",
            "properties": {
                "pattern": {"type": "string"},
                "path": _DIR,
                "max_results": {"type": "integer", "minimum": 1},
            },
            "required": ["pattern"],
            "additionalProperties": False,
        },
        handler=find_files,
        category="read",
    ),
    Tool(
        name="read_file",
        description="Read a UTF-8 text file. Long files are cut at a line boundary (truncated=true); "
                    "use read_range for the rest.",
        parameters={
            "type": "object",
            "properties": {"path": _PATH},
            "required": ["path"],
            "additionalProperties": False,
        },
        handler=read_file,
        category="read",
    ),
    Tool(
        name="read_range",
        description="Read lines start_line..end_line (1-based, inclusive) of a UTF-8 text file. "
                    "end_line past the end of the file is clamped.",
        parameters={
            "type": "object",
            "properties": {
                "path": _PATH,
                "start_line": {"type": "integer", "minimum": 1},
                "end_line": {"type": "integer", "minimum": 1},
            },
            "required": ["path", "start_line", "end_line"],
            "additionalProperties": False,
        },
        handler=read_range,
        category="read",
    ),
)

"""search_text: deterministic text search with ripgrep, or a pure-Python fallback.

Both engines search the same files: hidden files included, ``.gitignore``
*not* applied, ``IGNORED_DIRS`` skipped, symlinks not followed, files over
``max_file_bytes`` and binary files skipped, results sorted by path then line.
Literal search (the default) behaves identically. With ``regex=true`` the
dialects differ slightly (Rust ``regex`` vs Python ``re``).
"""

from __future__ import annotations

import fnmatch
import json
import re
import shutil
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Collection, Optional

from harness.tools.base import Tool, ToolContext, ToolFailure
from harness.tools.files import BINARY_SNIFF_BYTES
from harness.tools.paths import IGNORED_DIRS, iter_repo_files, relative_posix, resolve_in_repo


@dataclass(frozen=True)
class SearchMatch:
    path: str
    line: int   # 1-based
    text: str   # the matching line, without newline, cut to max_match_chars


@dataclass(frozen=True)
class SearchResult:
    query: str
    matches: tuple[SearchMatch, ...]
    truncated: bool   # stopped at max_results or at the time limit
    engine: str       # "ripgrep" or "python"


def search_text(ctx: ToolContext, query: str, path: str = ".", regex: bool = False,
                case_sensitive: bool = True, glob: Optional[str] = None,
                max_results: Optional[int] = None) -> SearchResult:
    return search(ctx, query, path=path, regex=regex, case_sensitive=case_sensitive,
                  glob=glob, max_results=max_results)


def search(ctx: ToolContext, query: str, *, path: str = ".", regex: bool = False,
           case_sensitive: bool = True, glob: Optional[str] = None,
           max_results: Optional[int] = None, engine: str = "auto",
           allowed_paths: Optional[Collection[str]] = None) -> SearchResult:
    """``engine`` is ``auto`` (ripgrep if installed), ``ripgrep`` or ``python``.

    ``allowed_paths`` (repository-relative, forward slashes) restricts the search
    to exactly those files; repository intelligence uses it to apply its own
    inventory (e.g. ``.gitignore``-aware). ``None`` keeps the default file
    selection described in the module docstring.
    """
    if not query:
        raise ToolFailure("invalid_arguments", "query must not be empty")
    target = resolve_in_repo(ctx.root, path)
    if not target.exists():
        raise ToolFailure("not_found", f"'{path}' does not exist")
    limit = min(max_results or ctx.limits.max_search_results, ctx.limits.max_search_results)
    if regex:
        try:
            re.compile(query)
        except re.error as exc:
            raise ToolFailure("invalid_arguments", f"invalid regular expression: {exc}") from None
    keep = _glob_filter(glob)
    scoped = None if allowed_paths is None else _scoped_files(ctx, target, allowed_paths)
    if scoped is not None and not scoped:
        return SearchResult(query=query, matches=(), truncated=False,
                            engine="python" if engine == "auto" else engine)

    if engine == "auto":
        engine = "ripgrep" if shutil.which("rg") else "python"
    if engine == "ripgrep":
        try:
            matches, truncated = _ripgrep(ctx, query, target, regex, case_sensitive, glob, keep, limit,
                                          scoped)
        except _RegexUnsupported:
            # Valid Python regex that Rust's regex rejects (e.g. look-around).
            engine = "python"
    if engine == "python":
        matches, truncated = _python(ctx, query, target, regex, case_sensitive, keep, limit, scoped)
    elif engine != "ripgrep":
        raise ValueError(f"unknown search engine {engine!r}")
    return SearchResult(query=query, matches=tuple(matches), truncated=truncated, engine=engine)


class _RegexUnsupported(Exception):
    pass


def _scoped_files(ctx: ToolContext, target: Path, allowed: Collection[str]) -> list[str]:
    """Allowed files under ``target``, sorted, excluding ``IGNORED_DIRS`` (same as the walkers)."""
    prefix = relative_posix(ctx.root, target)
    selected = []
    for rel in allowed:
        if IGNORED_DIRS.intersection(rel.split("/")):
            continue
        if prefix == "." or rel == prefix or rel.startswith(prefix + "/"):
            selected.append(rel)
    return sorted(set(selected))


def _glob_filter(glob: Optional[str]) -> Callable[[str], bool]:
    if not glob:
        return lambda rel: True
    if "/" in glob:
        return lambda rel: fnmatch.fnmatchcase(rel, glob)
    return lambda rel: fnmatch.fnmatchcase(rel.rsplit("/", 1)[-1], glob)


def _match(ctx: ToolContext, rel: str, line_number: int, text: str) -> SearchMatch:
    return SearchMatch(rel, line_number, text.rstrip("\r\n")[: ctx.limits.max_match_chars])


def _ripgrep(ctx, query, target, regex, case_sensitive, glob, keep, limit, scoped):
    argv = ["rg", "--json", "--sort", "path", "--no-config", "--no-ignore", "--hidden",
            "--no-follow", "--max-filesize", str(ctx.limits.max_file_bytes)]
    for name in sorted(IGNORED_DIRS):
        argv += ["--glob", f"!{name}"]
    if glob and "/" not in glob:
        argv += ["--glob", glob]
    if not regex:
        argv.append("--fixed-strings")
    if not case_sensitive:
        argv.append("--ignore-case")
    # Scoped searches still walk the directory: ripgrep searches explicitly named
    # files even when they are binary or over --max-filesize, which would break
    # parity with the Python engine. Results are filtered to the scope instead.
    argv += ["--regexp", query, "--", relative_posix(ctx.root, target)]
    if scoped is not None:
        allowed = set(scoped)
        glob_keep = keep
        keep = lambda rel: rel in allowed and glob_keep(rel)  # noqa: E731

    matches: list[SearchMatch] = []
    truncated = False
    with tempfile.TemporaryFile() as stderr:
        proc = subprocess.Popen(argv, cwd=ctx.root, stdin=subprocess.DEVNULL,
                                stdout=subprocess.PIPE, stderr=stderr)
        timer = threading.Timer(ctx.limits.search_timeout_seconds, proc.kill)
        timer.start()
        try:
            assert proc.stdout is not None
            for raw in proc.stdout:
                event = json.loads(raw)
                if event.get("type") != "match":
                    continue
                data = event["data"]
                rel = data["path"].get("text")
                text = data["lines"].get("text")
                if rel is None or text is None:  # non-UTF-8 path or line
                    continue
                rel = rel[2:] if rel.startswith("./") else rel
                if not keep(rel):
                    continue
                if len(matches) >= limit:
                    truncated = True
                    proc.kill()
                    break
                matches.append(_match(ctx, rel, data["line_number"], text))
        finally:
            timer.cancel()
            proc.stdout.close()
            code = proc.wait()
        if code < 0 and not truncated:  # killed by the timer
            truncated = True
        if code == 2 and not matches:
            stderr.seek(0)
            message = stderr.read(2000).decode("utf-8", "replace").strip()
            if regex and "regex parse error" in message:
                raise _RegexUnsupported(message)
            raise ToolFailure("search_failed", f"ripgrep failed: {message}")
    return matches, truncated


def _python(ctx, query, target, regex, case_sensitive, keep, limit, scoped=None):
    if regex:
        pattern = re.compile(query, 0 if case_sensitive else re.IGNORECASE)
        found = lambda line: pattern.search(line) is not None  # noqa: E731
    elif case_sensitive:
        found = lambda line: query in line  # noqa: E731
    else:
        folded = query.casefold()
        found = lambda line: folded in line.casefold()  # noqa: E731

    if scoped is not None:
        files = (ctx.root / rel for rel in scoped
                 if not (ctx.root / rel).is_symlink() and (ctx.root / rel).is_file())
    elif target.is_file():
        files = [target]
    else:
        files = (p for p, kind in iter_repo_files(ctx.root, target) if kind == "file")

    deadline = time.monotonic() + ctx.limits.search_timeout_seconds
    matches: list[SearchMatch] = []
    for file in files:
        if time.monotonic() > deadline:
            return matches, True
        rel = relative_posix(ctx.root, file)
        if not keep(rel):
            continue
        text = _readable_text(ctx, file)
        if text is None:
            continue
        for number, line in enumerate(_lines(text), start=1):
            if found(line):
                if len(matches) >= limit:
                    return matches, True
                matches.append(_match(ctx, rel, number, line))
    return matches, False


def _lines(text: str) -> list[str]:
    """Split on newline only, like ripgrep (``str.splitlines`` also splits on
    form feeds and Unicode separators, which would shift line numbers)."""
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    return lines


def _readable_text(ctx: ToolContext, file: Path) -> Optional[str]:
    try:
        if file.stat().st_size > ctx.limits.max_file_bytes:
            return None
        data = file.read_bytes()
    except OSError:
        return None
    if b"\x00" in data[:BINARY_SNIFF_BYTES]:
        return None
    return data.decode("utf-8", "replace")


TOOLS = (
    Tool(
        name="search_text",
        description="Search file contents. Literal text by default; set regex=true for a regular "
                    "expression. Returns path, line number and line text, sorted by path.",
        parameters={
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "path": {"type": "string", "description": "Directory or file to search (default: repository root)."},
                "regex": {"type": "boolean"},
                "case_sensitive": {"type": "boolean"},
                "glob": {"type": "string", "description": "Only files whose name matches, e.g. '*.py'."},
                "max_results": {"type": "integer", "minimum": 1},
            },
            "required": ["query"],
            "additionalProperties": False,
        },
        handler=search_text,
        category="read",
    ),
)

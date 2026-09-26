"""Read-only git inspection: git_status, git_diff, git_diff_stat.

These run fixed, harness-built git commands (never model-supplied ones) with
``GIT_OPTIONAL_LOCKS=0`` so that even ``git status`` does not refresh the
index. Nothing here stages, commits, resets or checks out.

All paths are relative to the selected repository root, which may be a
subdirectory of the git work tree; output is limited to that subtree.
Untracked files are included in diffs by default (as new-file diffs produced
with ``git diff --no-index``) because files created by the harness are
untracked until someone stages them.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from typing import Optional, Sequence

from harness.tools.base import Tool, ToolContext, ToolFailure
from harness.tools.commands import CommandResult, execute
from harness.tools.files import BINARY_SNIFF_BYTES
from harness.tools.paths import relative_posix, resolve_in_repo

_GIT_ENV = {
    "GIT_OPTIONAL_LOCKS": "0",
    "GIT_PAGER": "cat",
    "GIT_TERMINAL_PROMPT": "0",
    "LC_ALL": "C",
}
_GIT_FLAGS = ["-c", "core.quotepath=off", "-c", "color.ui=never", "--no-pager"]
_MAX_UNTRACKED_DIFFS = 200
# Cap for machine-readable output (status, ls-files, numstat), which must not be cut.
_MACHINE_OUTPUT_BYTES = 5_000_000


@dataclass(frozen=True)
class GitStatusEntry:
    path: str
    index: str        # X column of porcelain status ("?" for untracked)
    worktree: str     # Y column
    orig_path: Optional[str] = None  # renames/copies


@dataclass(frozen=True)
class GitStatus:
    branch: Optional[str]   # None when HEAD is detached
    entries: tuple[GitStatusEntry, ...]

    @property
    def clean(self) -> bool:
        return not self.entries


@dataclass(frozen=True)
class GitDiff:
    text: str
    truncated: bool
    untracked_included: tuple[str, ...]


@dataclass(frozen=True)
class DiffStatEntry:
    path: str
    added: Optional[int]     # None for binary files
    deleted: Optional[int]
    untracked: bool = False


@dataclass(frozen=True)
class GitDiffStat:
    files: tuple[DiffStatEntry, ...]
    insertions: int
    deletions: int

    @property
    def summary(self) -> str:
        n = len(self.files)
        return (f"{n} file{'s' if n != 1 else ''} changed, "
                f"{self.insertions} insertion{'s' if self.insertions != 1 else ''}(+), "
                f"{self.deletions} deletion{'s' if self.deletions != 1 else ''}(-)")


def _git(ctx: ToolContext, *args: str, ok_codes: Sequence[int] = (0,),
         max_bytes: Optional[int] = None) -> CommandResult:
    if shutil.which("git") is None:
        raise ToolFailure("git_not_found", "git is not installed or not on PATH")
    result = execute(["git", *_GIT_FLAGS, *args], root=ctx.root, timeout=ctx.limits.git_timeout_seconds,
                     max_output_bytes=max_bytes or ctx.limits.max_output_bytes, extra_env=_GIT_ENV)
    if result.timed_out:
        raise ToolFailure("timeout", f"git {args[0]} timed out after {ctx.limits.git_timeout_seconds}s")
    if result.exit_code not in ok_codes:
        raise ToolFailure("git_failed", f"git {args[0]} failed: {result.stderr.strip()[:500]}",
                          {"exit_code": result.exit_code})
    return result


def _require_repo(ctx: ToolContext) -> str:
    """Return the root's prefix inside the work tree ("" at the top level)."""
    if shutil.which("git") is None:
        raise ToolFailure("git_not_found", "git is not installed or not on PATH")
    probe = execute(["git", *_GIT_FLAGS, "rev-parse", "--show-prefix"], root=ctx.root,
                    timeout=ctx.limits.git_timeout_seconds, max_output_bytes=4096, extra_env=_GIT_ENV)
    if probe.exit_code != 0:
        raise ToolFailure("not_git_repo", f"{ctx.root} is not inside a git repository")
    return probe.stdout.strip()


def _pathspecs(ctx: ToolContext, paths: Optional[Sequence[str]]) -> list[str]:
    if not paths:
        return ["."]
    return [relative_posix(ctx.root, resolve_in_repo(ctx.root, p)) for p in paths]


def _untracked(ctx: ToolContext, specs: list[str]) -> list[str]:
    out = _git(ctx, "ls-files", "--others", "--exclude-standard", "-z", "--", *specs,
               max_bytes=_MACHINE_OUTPUT_BYTES).stdout
    return sorted(p for p in out.split("\0") if p)


def git_work_tree_prefix(ctx: ToolContext) -> Optional[str]:
    """The root's path inside its git work tree ("" at the top level), or ``None``
    when the root is not in a git repository or git is unavailable."""
    try:
        return _require_repo(ctx)
    except ToolFailure:
        return None


def git_list_files(ctx: ToolContext, *, tracked: bool) -> list[str]:
    """Root-relative paths git knows about, read-only.

    ``tracked=True``: ``git ls-files --cached``; ``tracked=False``: untracked files
    not excluded by ``.gitignore`` / ``info/exclude`` / global excludes
    (``git ls-files --others --exclude-standard``).
    """
    _require_repo(ctx)
    which = ["--cached"] if tracked else ["--others", "--exclude-standard"]
    result = _git(ctx, "ls-files", *which, "-z", "--", ".", max_bytes=_MACHINE_OUTPUT_BYTES)
    if result.truncated:
        raise ToolFailure("git_output_too_large",
                          f"git ls-files output exceeds {_MACHINE_OUTPUT_BYTES} bytes")
    return sorted(p for p in result.stdout.split("\0") if p)


def git_status(ctx: ToolContext) -> GitStatus:
    prefix = _require_repo(ctx)
    branch = _git(ctx, "symbolic-ref", "--short", "-q", "HEAD", ok_codes=(0, 1)).stdout.strip() or None
    raw = _git(ctx, "status", "--porcelain=v1", "-z", "--untracked-files=all", "--", ".",
               max_bytes=_MACHINE_OUTPUT_BYTES).stdout
    fields = raw.split("\0")
    entries: list[GitStatusEntry] = []
    i = 0
    while i < len(fields):
        record = fields[i]
        i += 1
        if len(record) < 4:
            continue
        x, y, path = record[0], record[1], record[3:]
        orig = None
        if x in "RC" or y in "RC":
            orig = fields[i] if i < len(fields) else None
            i += 1
        entries.append(GitStatusEntry(_strip(prefix, path), x, y, _strip(prefix, orig) if orig else None))
    return GitStatus(branch=branch, entries=tuple(entries))


def _strip(prefix: str, path: str) -> str:
    return path[len(prefix):] if prefix and path.startswith(prefix) else path


def git_diff(ctx: ToolContext, paths: Optional[Sequence[str]] = None, staged: bool = False,
             include_untracked: bool = True) -> GitDiff:
    _require_repo(ctx)
    specs = _pathspecs(ctx, paths)
    args = ["diff", "--no-ext-diff", "--no-textconv", "--no-renames", "--relative"]
    if staged:
        args.append("--cached")
    result = _git(ctx, *args, "--", *specs)
    parts = [result.stdout]
    truncated = result.truncated
    included: list[str] = []
    if include_untracked and not staged:
        untracked = _untracked(ctx, specs)
        for path in untracked[:_MAX_UNTRACKED_DIFFS]:
            new = _git(ctx, "diff", "--no-index", "--no-ext-diff", "--no-textconv", "--", "/dev/null", path,
                       ok_codes=(0, 1))
            parts.append(new.stdout)
            truncated = truncated or new.truncated
            included.append(path)
        truncated = truncated or len(untracked) > _MAX_UNTRACKED_DIFFS
    text = "".join(parts)
    limit = ctx.limits.max_output_bytes
    if len(text.encode()) > limit:
        text = text.encode()[:limit].decode("utf-8", "ignore") + "\n[... diff truncated ...]\n"
        truncated = True
    return GitDiff(text=text, truncated=truncated, untracked_included=tuple(included))


def git_diff_stat(ctx: ToolContext, staged: bool = False, include_untracked: bool = True) -> GitDiffStat:
    _require_repo(ctx)
    args = ["diff", "--numstat", "--no-ext-diff", "--no-textconv", "--no-renames", "--relative"]
    if staged:
        args.append("--cached")
    out = _git(ctx, *args, "--", ".", max_bytes=_MACHINE_OUTPUT_BYTES).stdout
    files: list[DiffStatEntry] = []
    for line in out.splitlines():
        added, deleted, path = line.split("\t", 2)
        files.append(DiffStatEntry(path, None if added == "-" else int(added),
                                   None if deleted == "-" else int(deleted)))
    if include_untracked and not staged:
        for path in _untracked(ctx, ["."]):
            files.append(_untracked_stat(ctx, path))
    return GitDiffStat(
        files=tuple(files),
        insertions=sum(f.added or 0 for f in files),
        deletions=sum(f.deleted or 0 for f in files),
    )


def _untracked_stat(ctx: ToolContext, path: str) -> DiffStatEntry:
    target = ctx.root / path
    try:
        data = target.read_bytes() if target.is_file() and not target.is_symlink() else b""
    except OSError:
        data = b""
    if b"\x00" in data[:BINARY_SNIFF_BYTES]:
        return DiffStatEntry(path, None, None, untracked=True)
    lines = data.count(b"\n") + (1 if data and not data.endswith(b"\n") else 0)
    return DiffStatEntry(path, lines, 0, untracked=True)


_NO_ARGS = {"type": "object", "properties": {}, "additionalProperties": False}

TOOLS = (
    Tool(
        name="git_status",
        description="Show the current branch and changed/untracked files (read-only).",
        parameters=_NO_ARGS,
        handler=git_status,
        category="read",
    ),
    Tool(
        name="git_diff",
        description="Show the unified diff of working-tree changes (read-only). Untracked files are "
                    "included as new-file diffs unless include_untracked=false.",
        parameters={
            "type": "object",
            "properties": {
                "paths": {"type": "array", "items": {"type": "string"},
                          "description": "Limit to these repository-relative paths."},
                "staged": {"type": "boolean", "description": "Diff the index instead of the working tree."},
                "include_untracked": {"type": "boolean"},
            },
            "additionalProperties": False,
        },
        handler=git_diff,
        category="read",
    ),
    Tool(
        name="git_diff_stat",
        description="Per-file added/deleted line counts for working-tree changes, plus totals (read-only).",
        parameters={
            "type": "object",
            "properties": {
                "staged": {"type": "boolean"},
                "include_untracked": {"type": "boolean"},
            },
            "additionalProperties": False,
        },
        handler=git_diff_stat,
        category="read",
    ),
)

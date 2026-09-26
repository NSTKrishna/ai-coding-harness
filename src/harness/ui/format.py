"""Pure formatting helpers for the CLI's presentation layer: GitHub issue URL
parsing, duration/path formatting, tool-activity headlines, and a read-only
repository status line (name, branch, clean/dirty) via ``git``.

Nothing here makes a network call, calls a model, or mutates the repository.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

GITHUB_ISSUE_RE = re.compile(r"^https://github\.com/([^/\s]+)/([^/\s]+)/issues/(\d+)/?$")


@dataclass(frozen=True)
class GitHubIssue:
    owner: str
    repo: str
    number: int
    url: str


def parse_github_issue(task: str) -> Optional[GitHubIssue]:
    """A GitHub issue URL is recognized only when it is the whole (single-line) task
    text; a URL merely mentioned inside a longer task is left as plain text."""
    stripped = task.strip()
    if not stripped or "\n" in stripped:
        return None
    match = GITHUB_ISSUE_RE.match(stripped)
    if not match:
        return None
    owner, repo, number = match.groups()
    return GitHubIssue(owner=owner, repo=repo, number=int(number), url=stripped)


def format_duration(seconds: float) -> str:
    total = max(0, int(round(seconds)))
    minutes, secs = divmod(total, 60)
    return f"{minutes}m {secs}s" if minutes else f"{secs}s"


def shorten_path(path: str, max_len: int) -> str:
    """Shorten for *display only*; never used for anything written to a report."""
    if max_len <= 0 or len(path) <= max_len:
        return path
    parts = [p for p in path.split("/") if p]
    if len(parts) <= 2:
        return path[: max(max_len - 3, 1)] + "..."
    head, tail = parts[0], parts[-1]
    shortened = f"{head}/.../{tail}"
    return shortened if len(shortened) <= max_len else ("..." + tail)[-max_len:]


_TOOL_VERBS = {
    "read_file": "read", "read_range": "read", "list_files": "list", "find_files": "find",
    "search_text": "search", "apply_patch": "edit", "edit_file": "edit", "write_file": "edit",
    "run_command": "run", "run_tests": "test",
    "git_status": "git", "git_diff": "git", "git_diff_stat": "git",
}


def _target_from_summary(tool: str, summary: str) -> str:
    text = summary
    prefix = f"{tool} "
    if text.startswith(prefix):
        text = text[len(prefix):]
    try:
        data = json.loads(text)
    except (ValueError, TypeError):
        data = None
    if isinstance(data, dict):
        for key in ("path", "pattern", "query", "command"):
            value = data.get(key)
            if isinstance(value, str):
                return value
            if isinstance(value, list) and value:
                return " ".join(str(v) for v in value)
        return ""
    return "" if text in ("{}", "") else text


COMMAND_TOOLS = frozenset({"run_command", "run_tests"})
GIT_TOOLS = frozenset({"git_status", "git_diff", "git_diff_stat"})


def tool_parts(tool: str, arguments_summary: str) -> tuple[str, str]:
    """(verb, target) for one tool call, e.g. ``("read", "src/foo.py")``."""
    target = _target_from_summary(tool, arguments_summary or "")
    if tool in ("edit_file", "write_file"):
        target = target.split(", ", 1)[0]                     # "src/x.py, 2 new line(s)" -> "src/x.py"
    elif tool == "apply_patch" and " for " in target:
        target = target.split(" for ", 1)[1]                  # "patch of 9 lines for a.py" -> "a.py"
    elif tool in COMMAND_TOOLS and target.startswith("/"):
        first, _, rest = target.partition(" ")                # "/opt/.../python3.14 -m x" -> "python3.14 -m x"
        target = (first.rsplit("/", 1)[-1] + (" " + rest if rest else ""))
    return _TOOL_VERBS.get(tool, tool), target


def tool_headline(tool: str, arguments_summary: str, detail: str = "", max_target: int = 60) -> str:
    """A short human line for one tool call, e.g. ``read  src/foo.py`` or
    ``edit  src/foo.py +12 -4``. Never reparses framework test output — ``detail``
    is only what the tool's own structured result already exposes."""
    verb, target = tool_parts(tool, arguments_summary)
    if len(target) > max_target:
        target = shorten_path(target, max_target)
    line = f"{verb}  {target}" if target else verb
    if detail:
        line += f"   {detail}"
    return line


@dataclass(frozen=True)
class RepoHeader:
    name: str
    is_git: bool
    branch: Optional[str] = None
    clean: Optional[bool] = None
    modified: int = 0

    def status_line(self) -> str:
        if not self.is_git:
            return "not a git repository"
        if self.clean is None:
            return f"git · {self.branch or '(unknown)'}"
        if self.clean:
            return f"git · {self.branch or '(detached)'} · clean"
        return f"git · {self.branch or '(detached)'} · {self.modified} modified"


def repo_header(repo: Path) -> RepoHeader:
    """Read-only ``git`` inspection for the pre-run header. Never fails the run:
    any git error just yields ``is_git=False``/``clean=None``."""
    name = repo.name or str(repo)
    if shutil.which("git") is None:
        return RepoHeader(name=name, is_git=False)

    def run(*args: str):
        try:
            return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, timeout=10)
        except (OSError, subprocess.TimeoutExpired):
            return None

    inside = run("rev-parse", "--is-inside-work-tree")
    if inside is None or inside.returncode != 0 or inside.stdout.strip() != "true":
        return RepoHeader(name=name, is_git=False)

    branch_result = run("rev-parse", "--abbrev-ref", "HEAD")
    branch = branch_result.stdout.strip() if branch_result and branch_result.returncode == 0 else None

    status_result = run("status", "--porcelain")
    if status_result is None or status_result.returncode != 0:
        return RepoHeader(name=name, is_git=True, branch=branch)
    entries = [line for line in status_result.stdout.splitlines() if line.strip()]
    return RepoHeader(name=name, is_git=True, branch=branch, clean=not entries, modified=len(entries))

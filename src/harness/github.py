"""GitHub issue intake: turn an issue URL into task text the model can act on.

Never a hard dependency. Uses the authenticated ``gh`` CLI when it is installed
(no extra credentials handled here), otherwise the public REST API over stdlib
``urllib``. Any failure (offline, rate limit, private repo) returns ``None`` and
the caller keeps the bare URL as the task. Content is bounded before it reaches
the planner.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from harness.ui.format import GitHubIssue

MAX_BODY_CHARS = 6_000
MAX_COMMENTS = 5
MAX_COMMENT_CHARS = 600
TIMEOUT_SECONDS = 15


@dataclass(frozen=True)
class IssueDetails:
    title: str
    body: str
    state: str                      # "OPEN" / "CLOSED"
    state_reason: str = ""          # e.g. "COMPLETED", "DUPLICATE", "NOT_PLANNED"
    labels: tuple[str, ...] = ()
    comments: tuple[str, ...] = field(default_factory=tuple)   # "author: text", bounded
    source: str = ""                # "gh" or "api"


def fetch_issue(issue: GitHubIssue) -> Optional[IssueDetails]:
    return _via_gh(issue) or _via_api(issue)


def _via_gh(issue: GitHubIssue) -> Optional[IssueDetails]:
    if shutil.which("gh") is None:
        return None
    try:
        result = subprocess.run(
            ["gh", "issue", "view", str(issue.number), "-R", f"{issue.owner}/{issue.repo}",
             "--json", "title,body,state,stateReason,labels,comments"],
            capture_output=True, text=True, timeout=TIMEOUT_SECONDS)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError:
        return None
    comments = tuple(
        f"{(c.get('author') or {}).get('login', '?')}: {_clip(c.get('body') or '', MAX_COMMENT_CHARS)}"
        for c in (data.get("comments") or [])[:MAX_COMMENTS])
    return IssueDetails(
        title=data.get("title") or "", body=_clip(data.get("body") or "", MAX_BODY_CHARS),
        state=(data.get("state") or "").upper(), state_reason=(data.get("stateReason") or "").upper(),
        labels=tuple(l.get("name", "") for l in data.get("labels") or []), comments=comments, source="gh")


def _via_api(issue: GitHubIssue) -> Optional[IssueDetails]:
    url = f"https://api.github.com/repos/{issue.owner}/{issue.repo}/issues/{issue.number}"
    request = urllib.request.Request(url, headers={"Accept": "application/vnd.github+json",
                                                   "User-Agent": "coding-harness"})
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
            data = json.loads(response.read().decode("utf-8", errors="replace"))
    except (urllib.error.URLError, OSError, ValueError):
        return None
    if not isinstance(data, dict) or "title" not in data:
        return None
    return IssueDetails(
        title=data.get("title") or "", body=_clip(data.get("body") or "", MAX_BODY_CHARS),
        state=(data.get("state") or "").upper(), state_reason=(data.get("state_reason") or "").upper(),
        labels=tuple(l.get("name", "") for l in data.get("labels") or [] if isinstance(l, dict)), source="api")


def _clip(text: str, limit: int) -> str:
    text = text.strip()
    return text if len(text) <= limit else text[: limit - 20].rstrip() + "\n[... truncated ...]"


def compose_task(issue: GitHubIssue, details: IssueDetails) -> str:
    """The task text handed to the harness: the issue itself, not just its URL."""
    lines = [f"Resolve GitHub issue {issue.owner}/{issue.repo}#{issue.number}: {details.title}",
             f"URL: {issue.url}"]
    if details.labels:
        lines.append("Labels: " + ", ".join(details.labels))
    if details.state and details.state != "OPEN":
        reason = f" ({details.state_reason.lower().replace('_', ' ')})" if details.state_reason else ""
        lines.append(f"Note: this issue is {details.state.lower()}{reason}.")
    lines += ["", details.body or "(no description)"]
    if details.comments:
        lines += ["", "Discussion:"] + [f"- {c}" for c in details.comments]
    return "\n".join(lines)


_REMOTE_RE = re.compile(r"github\.com[:/]([^/\s]+)/([^/\s]+?)(?:\.git)?/?$")


def remote_repos(repo: Path) -> dict[str, str]:
    """``{remote_name: "owner/repo"}`` for GitHub remotes of a local repository (read-only)."""
    if shutil.which("git") is None:
        return {}
    try:
        result = subprocess.run(["git", "-C", str(repo), "remote", "-v"], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return {}
    remotes: dict[str, str] = {}
    for line in result.stdout.splitlines():
        parts = line.split()
        if len(parts) >= 2:
            match = _REMOTE_RE.search(parts[1])
            if match:
                remotes.setdefault(parts[0], f"{match.group(1)}/{match.group(2)}".lower())
    return remotes


def issue_matches_repo(issue: GitHubIssue, repo: Path) -> Optional[bool]:
    """True/False when the repository has GitHub remotes; None when it cannot tell."""
    remotes = remote_repos(repo)
    if not remotes:
        return None
    return f"{issue.owner}/{issue.repo}".lower() in remotes.values()

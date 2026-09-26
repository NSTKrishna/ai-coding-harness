"""Pre-run workspace preparation: start the run on a fresh branch cut from the
latest upstream default branch.

Deterministic and run by the CLI before the orchestrator starts; the model's
tools stay read-only for git. Safety rules:

- refuses when tracked files have uncommitted changes (nothing is stashed,
  reset or discarded);
- fetches ``<remote>/<default>`` and branches from it with ``git switch -c``,
  so the local default branch (e.g. ``main``) is never modified — equivalent to
  "pull latest main, then branch" without touching a possibly diverged local main;
- never forces: an existing branch name gets a numeric suffix instead;
- if the fetch fails (offline), the last fetched ``<remote>/<default>`` is used
  and a warning says so.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

FETCH_TIMEOUT_SECONDS = 120
GIT_TIMEOUT_SECONDS = 30


class WorkspaceError(Exception):
    """The branch cannot be prepared. The message is safe to print."""


@dataclass(frozen=True)
class BranchPlan:
    remote: str
    base: str                 # default branch name on the remote, e.g. "main"
    branch: str               # new branch to create
    original: Optional[str]   # branch checked out before the run (None if detached)

    @property
    def base_ref(self) -> str:
        return f"{self.remote}/{self.base}"


@dataclass(frozen=True)
class BranchResult:
    plan: BranchPlan
    commit: str               # short sha the new branch starts at
    fetched: bool             # False: fetch failed, last known remote ref was used
    warning: Optional[str] = None


def _git(repo: Path, *args: str, timeout: int = GIT_TIMEOUT_SECONDS) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, timeout=timeout,
                              env=_env())
    except subprocess.TimeoutExpired:
        raise WorkspaceError(f"git {args[0]} timed out after {timeout}s") from None
    except OSError as exc:
        raise WorkspaceError(f"git could not be run: {exc.__class__.__name__}") from None


def _env() -> dict:
    import os
    env = dict(os.environ)
    env.update({"GIT_TERMINAL_PROMPT": "0", "LC_ALL": "C"})
    env.pop("AI_API_KEY", None)
    return env


def branch_name_for(task: str, issue_number: Optional[int] = None) -> str:
    if issue_number is not None:
        return f"harness/issue-{issue_number}"
    words = re.findall(r"[a-z0-9]+", task.lower())[:6]
    slug = "-".join(words)[:40].strip("-") or "task"
    return f"harness/{slug}"


def plan_branch(repo: Path, name: str) -> BranchPlan:
    if shutil.which("git") is None:
        raise WorkspaceError("git is not installed")
    inside = _git(repo, "rev-parse", "--is-inside-work-tree")
    if inside.returncode != 0 or inside.stdout.strip() != "true":
        raise WorkspaceError("not a git repository, so no branch can be created")

    dirty = _git(repo, "status", "--porcelain", "--untracked-files=no")
    if dirty.stdout.strip():
        count = len(dirty.stdout.strip().splitlines())
        raise WorkspaceError(f"working tree has {count} uncommitted change(s); commit or stash them first, "
                             "or run with --no-branch")

    remotes = _git(repo, "remote").stdout.split()
    remote = "upstream" if "upstream" in remotes else "origin" if "origin" in remotes else None
    if remote is None:
        raise WorkspaceError("no 'upstream' or 'origin' remote to branch from")

    base = _default_branch(repo, remote)
    current = _git(repo, "symbolic-ref", "--quiet", "--short", "HEAD")
    original = current.stdout.strip() if current.returncode == 0 else None
    return BranchPlan(remote=remote, base=base, branch=_unique(repo, name), original=original)


def _default_branch(repo: Path, remote: str) -> str:
    head = _git(repo, "symbolic-ref", "--quiet", "--short", f"refs/remotes/{remote}/HEAD")
    if head.returncode == 0 and head.stdout.strip().startswith(f"{remote}/"):
        return head.stdout.strip()[len(remote) + 1:]
    for candidate in ("main", "master"):
        if _git(repo, "rev-parse", "--verify", "--quiet", f"refs/remotes/{remote}/{candidate}").returncode == 0:
            return candidate
    remote_head = _git(repo, "ls-remote", "--symref", remote, "HEAD", timeout=FETCH_TIMEOUT_SECONDS)
    match = re.search(r"ref: refs/heads/(\S+)\s+HEAD", remote_head.stdout)
    if match:
        return match.group(1)
    raise WorkspaceError(f"cannot determine the default branch of remote '{remote}'")


def _unique(repo: Path, name: str) -> str:
    candidate, n = name, 2
    while _git(repo, "rev-parse", "--verify", "--quiet", f"refs/heads/{candidate}").returncode == 0:
        candidate, n = f"{name}-{n}", n + 1
    return candidate


def prepare_branch(repo: Path, plan: BranchPlan) -> BranchResult:
    fetch = _git(repo, "fetch", "--quiet", plan.remote, plan.base, timeout=FETCH_TIMEOUT_SECONDS)
    fetched = fetch.returncode == 0
    warning = None
    if not fetched:
        exists = _git(repo, "rev-parse", "--verify", "--quiet", f"refs/remotes/{plan.base_ref}")
        if exists.returncode != 0:
            raise WorkspaceError(f"could not fetch {plan.base_ref} and no local copy of it exists")
        warning = f"could not fetch {plan.base_ref} (offline?); used the last fetched copy"

    switch = _git(repo, "switch", "--quiet", "--no-track", "-c", plan.branch, plan.base_ref)
    if switch.returncode != 0:
        detail = (switch.stderr.strip().splitlines() or ["unknown error"])[-1]
        raise WorkspaceError(f"could not create branch {plan.branch}: {detail}")
    commit = _git(repo, "rev-parse", "--short", "HEAD").stdout.strip()
    return BranchResult(plan=plan, commit=commit, fetched=fetched, warning=warning)

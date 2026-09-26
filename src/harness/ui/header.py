"""The pre-run screen: what will run, where, with which model, on which branch.

Concise by default; ``--verbose`` adds a "Details" block (adapter, base URL,
limits, env file — never the key value). The plain form is used for non-TTY
output (deterministic, test-friendly); the boxed form for interactive terminals.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional, TextIO

from harness.config import Config
from harness.ui.format import GitHubIssue, parse_github_issue, repo_header
from harness.ui.theme import Theme

NOT_CONFIGURED = "not configured (set AI_MODEL_ADAPTER, AI_MODEL, AI_BASE_URL)"


@dataclass
class RunInfo:
    repo: Path
    task: str                                   # what the user entered (URL or text)
    issue: Optional[GitHubIssue] = None
    details: Any = None                         # github.IssueDetails when fetched
    branch: Any = None                          # workspace.BranchPlan when a branch will be created
    notes: list[str] = field(default_factory=list)      # warnings shown before starting

    @classmethod
    def for_task(cls, repo: Path, task: str) -> "RunInfo":
        return cls(repo=repo, task=task, issue=parse_github_issue(task))


def _model_line(config: Config) -> str:
    model = config.model
    if not model.name:
        return NOT_CONFIGURED
    label = model.provider or model.adapter or "model"
    return f"{label} · {model.name}"


def _issue_state(details) -> str:
    if details is None or not details.state or details.state == "OPEN":
        return ""
    reason = f" ({details.state_reason.lower().replace('_', ' ')})" if details.state_reason else ""
    return f"{details.state.lower()}{reason}"


def render(config: Config, info: RunInfo, *, theme: Theme, verbose: bool = False) -> str:
    """Plain, append-only header (non-TTY)."""
    lines = ["Harness", ""]
    header = repo_header(info.repo)
    lines += ["Repository", f"  {header.name}", f"  {header.status_line()}", ""]

    if info.issue is not None:
        lines += ["Issue", f"  {info.issue.owner}/{info.issue.repo} #{info.issue.number}"]
        if info.details is not None and info.details.title:
            lines.append(f"  {info.details.title}")
        lines += [f"  {info.issue.url}", ""]
    else:
        task_lines = info.task.strip().splitlines() or [""]
        first = task_lines[0]
        if len(first) > 100:
            first = first[:97] + "..."
        more = f"  (+{len(task_lines) - 1} more line(s))" if len(task_lines) > 1 else ""
        lines += ["Task", f"  {first}{more}", ""]

    if info.branch is not None:
        lines += ["Branch", f"  new {info.branch.branch} from {info.branch.base_ref} (fetched first)", ""]

    lines += ["Model", f"  {_model_line(config)}"]
    lines += [f"! {note}" for note in info.notes]
    if verbose:
        lines += _details(config, info.repo)
    return "\n".join(lines) + "\n"


def render_panel(config: Config, info: RunInfo, *, theme: Theme, verbose: bool = False) -> str:
    """Boxed header for interactive terminals."""
    from harness.ui.panel import panel, wrapped

    label = lambda text: (f"{text:<12}", "dim")  # noqa: E731
    header = repo_header(info.repo)
    status = header.status_line()
    rows = [[label("Repository"), (header.name, "bold"), ("  " + status, "dim")]]

    if info.issue is not None:
        rows.append([label("Issue"), (f"{info.issue.owner}/{info.issue.repo} #{info.issue.number}", "accent")])
        if info.details is not None and info.details.title:
            rows += [[(" " * 12, None), *seg] for seg in wrapped(info.details.title, theme.panel_width - 16)]
        state = _issue_state(info.details)
        tags = ", ".join(info.details.labels) if info.details is not None and info.details.labels else ""
        meta = " · ".join(x for x in (state, tags) if x)
        if meta:
            rows.append([(" " * 12, None), (meta, "yellow" if state else "dim")])
    else:
        task_lines = info.task.strip().splitlines() or [""]
        more = f"  +{len(task_lines) - 1} more line(s)" if len(task_lines) > 1 else ""
        rows.append([label("Task"), (task_lines[0], None), (more, "dim")])

    if info.branch is not None:
        rows.append([label("Branch"), (info.branch.branch, "green"),
                     (f"  from {info.branch.base_ref} · fetched before start", "dim")])
    rows.append([label("Model"), (_model_line(config), "accent2" if config.model.name else "yellow")])
    if verbose:
        rows.append([("", None)])
        for line in _details(config, info.repo)[2:]:
            rows.append([(line.strip(), "dim")])

    out = panel(theme, rows, title="Run")
    for note in info.notes:
        out.append(" " + theme.paint(theme.symbol("warning"), "yellow") + " " + note)
    return "\n".join(out) + "\n"


def _details(config: Config, repo: Path) -> list[str]:
    model, limits = config.model, config.limits
    return [
        "", "Details",
        f"  Repository path:  {repo}",
        f"  API key:          set (redacted in all output)",
        f"  Model adapter:    {model.adapter or '-'}",
        f"  Model provider:   {model.provider or '-'}",
        f"  Base URL:         {model.base_url or '-'}",
        f"  Timeout/retries:  {model.timeout_seconds}s, max {model.max_retries} retries",
        f"  Limits:           max_steps={limits.max_steps}, max_repair_cycles={limits.max_repair_cycles}, "
        f"command_timeout_seconds={limits.command_timeout_seconds}",
        f"  .env file:        {config.env_file or 'none loaded'}",
        f"  Telemetry:        {'enabled' if config.telemetry_enabled else 'disabled'}",
    ]


CONFIRM_PROMPT = "Press Enter to start · Ctrl-C to cancel "
CONFIRM_PROMPT_ASCII = "Press Enter to start (or 'n' to cancel), Ctrl-C to cancel "


def confirm(stdin: TextIO, stdout: TextIO, *, theme: Theme) -> bool:
    """Returns False only if the user explicitly declines; EOF/Ctrl-C-safe."""
    if theme.unicode:
        prompt = (" " + theme.paint("↵", "accent") + " " + theme.paint("Enter", "bold") + " to start"
                  + theme.paint("  ·  ", "dim") + theme.paint("n", "bold") + " or "
                  + theme.paint("Ctrl-C", "bold") + " to cancel ")
    else:
        prompt = CONFIRM_PROMPT_ASCII
    stdout.write("\n" + prompt)
    stdout.flush()
    line = stdin.readline()
    if not line:
        stdout.write("\n")
        return True
    return line.strip().lower() not in ("n", "no")

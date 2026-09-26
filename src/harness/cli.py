"""Command-line entry point.

M1 scope: load configuration, accept a repository path and a task (from
flags or interactive prompts), validate them, report, and exit. No model is
called and the repository is not modified.
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Optional, TextIO

from harness import __version__
from harness.config import Config, ConfigError, load_config

EXIT_OK = 0
EXIT_USAGE = 2
EXIT_INTERRUPTED = 130

NOT_SET = "not set (awaiting organizer announcement)"


class InputError(Exception):
    """Invalid repository or task input. The message is safe to print."""


@dataclass(frozen=True)
class TaskInput:
    repo: Path
    task: str
    source: str  # "argument", "file <path>" or "prompt"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="harness",
        description="Autonomous coding harness.",
    )
    parser.add_argument("--version", action="version", version=f"harness {__version__}")
    subcommands = parser.add_subparsers(dest="command", metavar="COMMAND")

    run = subcommands.add_parser(
        "run",
        help="run the harness on a repository and task",
        description="Run the harness. Missing --repo or task input is prompted for interactively.",
    )
    run.add_argument("--repo", metavar="PATH", help="path to the target repository")
    task = run.add_mutually_exclusive_group()
    task.add_argument("--task", metavar="TEXT", help="task description or GitHub issue text")
    task.add_argument("--task-file", metavar="FILE", help="file containing the task description")
    return parser


def main(
    argv: Optional[list[str]] = None,
    *,
    environ: Optional[Mapping[str, str]] = None,
    dotenv_path: Optional[Path] = None,
    stdin: Optional[TextIO] = None,
    stdout: Optional[TextIO] = None,
    stderr: Optional[TextIO] = None,
) -> int:
    stdin = stdin or sys.stdin
    stdout = stdout or sys.stdout
    stderr = stderr or sys.stderr

    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command is None:
        parser.print_help(stderr)
        return EXIT_USAGE

    try:
        config = load_config(environ=environ, dotenv_path=dotenv_path)
    except ConfigError as exc:
        print(f"error: {exc}", file=stderr)
        return EXIT_USAGE

    try:
        task_input = _collect_input(args, stdin, stdout)
    except InputError as exc:
        print(config.redact(f"error: {exc}"), file=stderr)
        return EXIT_USAGE
    except KeyboardInterrupt:
        print("\nInterrupted.", file=stderr)
        return EXIT_INTERRUPTED

    stdout.write(config.redact(_report(config, task_input)))
    stdout.flush()
    return EXIT_OK


def _collect_input(args: argparse.Namespace, stdin: TextIO, stdout: TextIO) -> TaskInput:
    raw_repo = args.repo if args.repo is not None else _prompt_line("Repository path: ", stdin, stdout)
    repo = _validate_repo(raw_repo)

    if args.task is not None:
        task, source = args.task, "argument"
    elif args.task_file is not None:
        task, source = _read_task_file(args.task_file), f"file {args.task_file}"
    else:
        task, source = _prompt_task(stdin, stdout), "prompt"

    task = task.strip()
    if not task:
        raise InputError("task must not be empty")
    return TaskInput(repo=repo, task=task, source=source)


def _prompt_line(prompt: str, stdin: TextIO, stdout: TextIO) -> str:
    stdout.write(prompt)
    stdout.flush()
    line = stdin.readline()
    if not line:
        stdout.write("\n")
        raise InputError(f"no input received for '{prompt.strip().rstrip(':')}'")
    return line.strip()


def _prompt_task(stdin: TextIO, stdout: TextIO) -> str:
    stdout.write("Task / GitHub issue (finish with an empty line):\n")
    stdout.flush()
    lines = []
    while True:
        line = stdin.readline()
        if not line or not line.strip():
            break
        lines.append(line.rstrip("\n"))
    return "\n".join(lines)


def _validate_repo(raw: str) -> Path:
    if not raw.strip():
        raise InputError("repository path must not be empty")
    path = Path(raw.strip()).expanduser()
    if not path.exists():
        raise InputError(f"repository path does not exist: {path}")
    if not path.is_dir():
        raise InputError(f"repository path is not a directory: {path}")
    return path.resolve()


def _read_task_file(raw: str) -> str:
    path = Path(raw).expanduser()
    if not path.is_file():
        raise InputError(f"task file does not exist: {path}")
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise InputError(f"cannot read task file {path}: {exc.__class__.__name__}") from None


def _git_status_label(repo: Path) -> str:
    if shutil.which("git") is None:
        return "unknown (git not found on PATH)"
    try:
        result = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "--is-inside-work-tree"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return "unknown (git check failed)"
    if result.returncode == 0 and result.stdout.strip() == "true":
        return "git repository"
    return "not a git repository (diff-based verification will be limited)"


def _report(config: Config, task_input: TaskInput) -> str:
    model = config.model
    limits = config.limits
    task_lines = task_input.task.splitlines()
    first_line = task_lines[0]
    if len(first_line) > 80:
        first_line = first_line[:77] + "..."

    rows = [
        "Configuration accepted.",
        f"  API key:         set (redacted)",
        f"  Model provider:  {model.provider or NOT_SET}",
        f"  Model:           {model.name or NOT_SET}",
        f"  Base URL:        {model.base_url or NOT_SET}",
        f"  Limits:          max_steps={limits.max_steps}, "
        f"max_repair_cycles={limits.max_repair_cycles}, "
        f"command_timeout_seconds={limits.command_timeout_seconds}",
        f"  .env file:       {config.env_file or 'none loaded'}",
        "",
        "Input accepted.",
        f"  Repository:      {task_input.repo} ({_git_status_label(task_input.repo)})",
        f"  Task source:     {task_input.source}",
        f"  Task:            {first_line} ({len(task_lines)} line(s), {len(task_input.task)} chars)",
        "",
        "Harness skeleton ready. The agent is not implemented yet (milestone M1):",
        "no model was called and the repository was not modified.",
    ]
    return "\n".join(rows) + "\n"

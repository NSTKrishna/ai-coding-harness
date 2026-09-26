"""Command-line entry point.

M1 scope: load configuration, accept a repository path and a task (from
flags or interactive prompts), validate them, report, and exit. No model is
called and the repository is not modified.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Optional, TextIO

from harness import __version__
from harness.config import API_KEY_VAR, Config, ConfigError, load_config, load_context_limits

EXIT_OK = 0
EXIT_NOT_VERIFIED = 1       # the run ended in any terminal phase other than VERIFIED
EXIT_USAGE = 2              # usage, configuration (including an unsupported provider) or input error
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

    inspect = subcommands.add_parser(
        "inspect",
        help="profile a repository and, given a task, rank relevant files (no model, no API key)",
        description="Show the repository profile. With --task or --task-file, also show task signals, "
                    "ranked candidate files with reasons, and discovery metrics. Makes no model call "
                    "and does not modify the repository.",
    )
    inspect.add_argument("--repo", metavar="PATH", required=True, help="path to the repository")
    inspect_task = inspect.add_mutually_exclusive_group()
    inspect_task.add_argument("--task", metavar="TEXT", help="task description")
    inspect_task.add_argument("--task-file", metavar="FILE", help="file containing the task description")
    inspect.add_argument("--top", metavar="N", type=int, default=10, help="candidates to show (default 10)")
    inspect.add_argument("--show-context", action="store_true",
                         help="also print the rendered working set a planner would receive")

    runs = subcommands.add_parser("runs", help="list recent runs from their saved artifacts (no model, no API key)")
    runs.add_argument("--limit", metavar="N", type=int, default=20, help="runs to show (default 20)")
    report = subcommands.add_parser("report", help="show a saved run report (no model, no API key)")
    report.add_argument("run_id", help="run id as listed by 'harness runs'")
    report.add_argument("--json", action="store_true", help="print summary.json instead of final_report.md")
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
    if args.command == "inspect":
        return _inspect(args, environ=environ, dotenv_path=dotenv_path, stdout=stdout, stderr=stderr)
    if args.command in ("runs", "report"):
        return _artifacts(args, environ=environ, dotenv_path=dotenv_path, stdout=stdout, stderr=stderr)

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
    return _execute(config, task_input, stdout, stderr)


def _execute(config: Config, task_input: TaskInput, stdout: TextIO, stderr: TextIO) -> int:
    """Model execution. Needs a live adapter for the configured provider; none exists in this build."""
    from harness.model.factory import UnsupportedProviderError, create_model_client
    from harness.orchestrator import Orchestrator, Phase
    from harness.orchestrator.report import format_run

    try:
        model = create_model_client(config.model, config.api_key)
    except UnsupportedProviderError as exc:
        print(config.redact(
            f"error: {exc}\nNo model was called and the repository was not modified. "
            f"Deterministic analysis is available without a model: "
            f"harness inspect --repo {task_input.repo} --task \"...\""), file=stderr)
        return EXIT_USAGE

    recorder = None
    if config.telemetry_enabled:
        from harness.telemetry import RunRecorder, resolve_runs_dir
        runs_dir, note = resolve_runs_dir(config.runs_dir, task_input.repo)
        if note:
            print(f"note: {note}", file=stderr)
        recorder = RunRecorder(runs_dir, redact=config.redact)
    orchestrator = Orchestrator(model, limits=config.limits, context_limits=config.context,
                                redact=config.redact, recorder=recorder)
    try:
        state = orchestrator.run(task_input.repo, task_input.task)
    except KeyboardInterrupt:
        print("\nInterrupted.", file=stderr)
        return EXIT_INTERRUPTED
    stdout.write(config.redact(format_run(state)))
    if recorder is not None and recorder.run_dir is not None:
        stdout.write(f"Run artifacts: {recorder.run_dir}\n")
    stdout.flush()
    return EXIT_OK if state.phase == Phase.VERIFIED else EXIT_NOT_VERIFIED


def _inspect(args: argparse.Namespace, *, environ: Optional[Mapping[str, str]], dotenv_path: Optional[Path],
             stdout: TextIO, stderr: TextIO) -> int:
    """Repository intelligence only. Deliberately does not load the API key."""
    from harness.repo.discovery import discover_for_task
    from harness.repo.profile import analyze_repository
    from harness.repo.report import format_discovery, format_profile

    secret = (os.environ if environ is None else environ).get(API_KEY_VAR, "")

    def redact(text: str) -> str:
        return text.replace(secret, "***") if secret else text

    try:
        repo = _validate_repo(args.repo)
        limits = load_context_limits(environ=environ, dotenv_path=dotenv_path)
        task = None
        if args.task is not None:
            task = args.task
        elif args.task_file is not None:
            task = _read_task_file(args.task_file)
        if task is not None and not task.strip():
            raise InputError("task must not be empty")
        if args.top < 1:
            raise InputError("--top must be at least 1")
    except (InputError, ConfigError) as exc:
        print(redact(f"error: {exc}"), file=stderr)
        return EXIT_USAGE

    if task is None:
        stdout.write(redact(format_profile(analyze_repository(repo))))
    else:
        result = discover_for_task(repo, task, limits=limits)
        stdout.write(redact(format_discovery(result, top=args.top)))
        if args.show_context:
            stdout.write(redact("\n--- working set ---\n" + result.working_set.render()))
    stdout.flush()
    return EXIT_OK


def _artifacts(args: argparse.Namespace, *, environ: Optional[Mapping[str, str]], dotenv_path: Optional[Path],
               stdout: TextIO, stderr: TextIO) -> int:
    """`harness runs` / `harness report`: read saved artifacts only (no key, no model, no repository)."""
    import json
    import re

    from harness.config import load_runtime_settings
    from harness.telemetry import list_runs, resolve_runs_dir

    try:
        _, configured = load_runtime_settings(environ=environ, dotenv_path=dotenv_path)
    except ConfigError as exc:
        print(f"error: {exc}", file=stderr)
        return EXIT_USAGE
    runs_dir, _ = resolve_runs_dir(configured)
    if args.command == "runs":
        if args.limit < 1:
            print("error: --limit must be at least 1", file=stderr)
            return EXIT_USAGE
        rows = list_runs(runs_dir, args.limit)
        if not rows:
            stdout.write(f"No runs recorded in {runs_dir}\n")
            return EXIT_OK
        stdout.write(f"Runs in {runs_dir} (newest first):\n")
        for r in rows:
            task = (r["task"][0] if r["task"] else "")[:70]
            stdout.write(f"  {r['run_id']}  {str(r['final_status']):<17} {r['started_at']}  {task}\n")
        return EXIT_OK
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", args.run_id):
        print(f"error: invalid run id {args.run_id!r}", file=stderr)
        return EXIT_USAGE
    path = runs_dir / args.run_id / ("summary.json" if args.json else "final_report.md")
    if not path.is_file():
        print(f"error: no saved {'summary' if args.json else 'report'} for run {args.run_id} in {runs_dir}", file=stderr)
        return EXIT_USAGE
    text = path.read_text(encoding="utf-8")
    if args.json:
        text = json.dumps(json.loads(text), indent=2, sort_keys=True) + "\n"
    stdout.write(text)
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
        f"  Model adapter:   {model.adapter or NOT_SET}",
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
    ]
    return "\n".join(rows) + "\n"

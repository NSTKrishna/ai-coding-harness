"""Command-line entry point.

Concise by default: the pre-run header shows only what's needed to confirm
the run (repository, task/issue, model) and the live view shows phase
progress and tool activity without dumping internals. ``--verbose`` adds
configuration diagnostics (adapter, base URL, limits, env file — never the
key value); the API key itself is never printed in any mode.
"""

from __future__ import annotations

import argparse
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Optional, TextIO

from harness import __version__
from harness.config import API_KEY_VAR, Config, ConfigError, load_config, load_context_limits
from harness.ui import header as ui_header
from harness.ui import input as ui_input
from harness.ui.theme import Theme, is_tty

EXIT_OK = 0
EXIT_NOT_VERIFIED = 1       # the run ended in any terminal phase other than VERIFIED
EXIT_USAGE = 2              # usage, configuration (including an unsupported provider) or input error
EXIT_INTERRUPTED = 130

InputError = ui_input.InputError


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
    run.add_argument("--verbose", action="store_true",
                     help="show configuration diagnostics (adapter, base URL, limits, env file)")
    run.add_argument("--no-interactive", action="store_true",
                     help="never animate or ask for confirmation, even on a terminal (deterministic output)")
    branch = run.add_mutually_exclusive_group()
    branch.add_argument("--branch", action="store_true",
                        help="start on a new branch cut from the freshly fetched upstream default branch "
                             "(default for GitHub issue tasks)")
    branch.add_argument("--no-branch", action="store_true", help="run on the currently checked-out branch")
    run.add_argument("--no-fetch-issue", action="store_true",
                     help="do not fetch a GitHub issue's text; pass only its URL to the model")

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

    theme = Theme.detect(stdout, environ)
    interactive = is_tty(stdout) and not args.no_interactive
    if interactive:
        from harness.ui.panel import banner
        stdout.write(banner(theme, __version__))
        stdout.flush()

    try:
        task_input = _collect_input(args, stdin, stdout, theme, boxed=interactive)
        info = _prepare_info(args, task_input, stdout, theme, interactive)
    except InputError as exc:
        print(config.redact(f"error: {exc}"), file=stderr)
        return EXIT_USAGE
    except KeyboardInterrupt:
        from harness.ui.final_screen import cancelled
        stdout.write("\n" + cancelled(None, theme=theme))
        stdout.flush()
        return EXIT_INTERRUPTED

    render = ui_header.render_panel if interactive else ui_header.render
    stdout.write(config.redact(render(config, info, theme=theme, verbose=args.verbose)))
    stdout.flush()

    if interactive:
        try:
            proceed = ui_header.confirm(stdin, stdout, theme=theme)
        except KeyboardInterrupt:
            proceed = False
        if not proceed:
            stdout.write("\nCancelled.\n")
            stdout.flush()
            return EXIT_INTERRUPTED
        stdout.write("\n")

    return _execute(config, task_input, info, stdout, stderr, theme=theme, interactive=interactive)


def _prepare_info(args: argparse.Namespace, task_input: "TaskInput", stdout: TextIO, theme: Theme,
                  interactive: bool) -> ui_header.RunInfo:
    """Issue text + branch plan, resolved before the confirmation so the header shows them."""
    from harness import workspace

    info = ui_header.RunInfo.for_task(task_input.repo, task_input.task)
    if info.issue is not None and not args.no_fetch_issue:
        from harness import github
        if interactive:
            stdout.write(" " + theme.paint(theme.spinner[0], "accent") + theme.paint(
                f" Fetching issue #{info.issue.number}\u2026", "dim") + "\r")
            stdout.flush()
        info.details = github.fetch_issue(info.issue)
        if interactive:
            stdout.write("\r\x1b[2K")
        if info.details is None:
            info.notes.append("Could not fetch the issue text (offline, rate-limited or private); "
                              "the model will only see the URL.")
        else:
            if info.details.state and info.details.state != "OPEN":
                reason = info.details.state_reason.lower().replace("_", " ")
                info.notes.append(f"This issue is {info.details.state.lower()}"
                                  + (f" ({reason})" if reason else "") + ".")
            if github.issue_matches_repo(info.issue, task_input.repo) is False:
                remotes = ", ".join(sorted(set(github.remote_repos(task_input.repo).values())))
                info.notes.append(f"Issue is from {info.issue.owner}/{info.issue.repo}, but this repository's "
                                  f"remotes are {remotes}. Is this the right repository?")

    mode = "on" if args.branch else "off" if args.no_branch else ("auto" if info.issue is not None else "off")
    if mode != "off":
        name = workspace.branch_name_for(task_input.task, info.issue.number if info.issue else None)
        try:
            info.branch = workspace.plan_branch(task_input.repo, name)
        except workspace.WorkspaceError as exc:
            if mode == "on":
                raise InputError(f"cannot create a branch: {exc}") from None
            info.notes.append(f"Staying on the current branch: {exc}.")
    return info


def _run_task(info: ui_header.RunInfo) -> str:
    if info.issue is not None and info.details is not None:
        from harness import github
        return github.compose_task(info.issue, info.details)
    return info.task


def _execute(config: Config, task_input: "TaskInput", info: ui_header.RunInfo, stdout: TextIO, stderr: TextIO,
             *, theme: Theme, interactive: bool) -> int:
    """Model execution. Nothing in the repository changes before the model client exists."""
    import threading

    from harness import workspace
    from harness.model.factory import UnsupportedProviderError, create_model_client
    from harness.orchestrator import Orchestrator, Phase
    from harness.ui.format import repo_header
    from harness.ui.renderer import RunContext, build_renderer

    try:
        model = create_model_client(config.model, config.api_key)
    except UnsupportedProviderError as exc:
        print(config.redact(
            f"error: {exc}\nNo model was called and the repository was not modified. "
            f"Deterministic analysis is available without a model: "
            f"harness inspect --repo {task_input.repo} --task \"...\""), file=stderr)
        return EXIT_USAGE

    def write(text: str) -> None:
        if text:
            stdout.write(config.redact(text))
            stdout.flush()

    branch_result = None
    if info.branch is not None:
        try:
            branch_result = workspace.prepare_branch(task_input.repo, info.branch)
        except workspace.WorkspaceError as exc:
            print(f"error: {exc}\nNo model was called and the repository was not modified.", file=stderr)
            return EXIT_USAGE

    context = RunContext(repo_name=task_input.repo.name,
                         branch=branch_result.plan.branch if branch_result else (repo_header(task_input.repo).branch or ""),
                         model=(config.model.name or "").split("/")[-1])
    renderer = build_renderer(interactive=interactive, theme=theme, context=context)
    if branch_result is not None:
        write(renderer.note("success", f"branch {branch_result.plan.branch} from {branch_result.plan.base_ref} "
                                       f"@ {branch_result.commit}"))
        if branch_result.warning:
            write(renderer.note("warning", branch_result.warning))

    def on_event(name: str, metadata: dict, phase: str) -> None:
        with renderer.lock:
            write(renderer.handle(name, metadata, phase))

    if config.telemetry_enabled:
        from harness.telemetry import RunRecorder, resolve_runs_dir
        runs_dir, note = resolve_runs_dir(config.runs_dir, task_input.repo)
        if note:
            print(f"note: {note}", file=stderr)
        recorder = RunRecorder(runs_dir, redact=config.redact, listener=on_event)
    else:
        from harness.telemetry import LiveObserver
        recorder = LiveObserver(on_event, redact=config.redact)

    stop = threading.Event()

    def ticker() -> None:
        while not stop.wait(0.1):
            with renderer.lock:
                write(renderer.tick())

    thread = threading.Thread(target=ticker, name="harness-ui", daemon=True) if interactive else None
    if thread is not None:
        thread.start()

    orchestrator = Orchestrator(model, limits=config.limits, context_limits=config.context,
                                redact=config.redact, recorder=recorder)
    try:
        state = orchestrator.run(task_input.repo, _run_task(info))
    except KeyboardInterrupt:
        stop.set()
        with renderer.lock:
            write(renderer.cancelled(recorder.state, recorder.run_dir))
            if branch_result is not None and branch_result.plan.original:
                write(f"  Branch {branch_result.plan.branch} is checked out; return with: "
                      f"git switch {branch_result.plan.original}\n")
        return EXIT_INTERRUPTED
    finally:
        stop.set()
        if thread is not None:
            thread.join(timeout=1)

    extra = []
    if branch_result is not None:
        lines = [f"{branch_result.plan.branch} (from {branch_result.plan.base_ref} @ {branch_result.commit})"]
        if branch_result.plan.original:
            lines.append(f"previous branch: git switch {branch_result.plan.original}")
        extra.append(("Branch", lines))
    with renderer.lock:
        write(renderer.finish(state, recorder.run_dir, extra_sections=extra))
    return EXIT_OK if state.phase in (Phase.VERIFIED, Phase.UNVERIFIED) else EXIT_NOT_VERIFIED


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
        stdout.write(f"  {'RUN':<14}{'STATUS':<17}{'REPOSITORY':<18}{'AGE':>6}  TASK\n")
        for r in rows:
            task = (r["task"][0] if r["task"] else "")[:60]
            repo_name = Path(r["repository"]).name if r.get("repository") else "-"
            age = _relative_age(r["started_at"])
            stdout.write(f"  {r['run_id']:<14}{str(r['final_status']):<17}{repo_name:<18}{age:>6}  {task}\n")
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


def _relative_age(started_at: str) -> str:
    if not started_at:
        return "-"
    from datetime import datetime, timezone
    try:
        dt = datetime.fromisoformat(started_at)
    except ValueError:
        return "-"
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    seconds = max(0, int((datetime.now(timezone.utc) - dt).total_seconds()))
    if seconds < 60:
        return f"{seconds}s"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes}m"
    hours = minutes // 60
    if hours < 24:
        return f"{hours}h"
    return f"{hours // 24}d"


def _collect_input(args: argparse.Namespace, stdin: TextIO, stdout: TextIO, theme: Theme,
                   *, boxed: bool = False) -> TaskInput:
    if args.repo is not None:
        raw_repo = args.repo
    else:
        default = ui_input.default_repository(Path.cwd())
        if boxed:
            hint = f"path \u00b7 Enter for current directory ({Path.cwd().name})" if default else "path"
            raw_repo = ui_input.boxed_prompt("Repository", stdin, stdout, theme, hint=hint, default=default)
        else:
            raw_repo = ui_input.prompt_line("Repository", stdin, stdout, arrow=theme.arrow, default=default)
    repo = _validate_repo(raw_repo)

    if args.task is not None:
        task, source = args.task, "argument"
    elif args.task_file is not None:
        task, source = _read_task_file(args.task_file), f"file {args.task_file}"
    elif boxed:
        task, source = ui_input.boxed_task(stdin, stdout, theme), "prompt"
    else:
        task, source = ui_input.prompt_task(stdin, stdout, arrow=theme.arrow), "prompt"

    task = task.strip()
    if not task:
        raise InputError("task must not be empty")
    return TaskInput(repo=repo, task=task, source=source)


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


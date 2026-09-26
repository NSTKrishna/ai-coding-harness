"""Bounded command execution: run_command and run_tests.

Commands never go through a shell implicitly. A command is either an argv
list or a string split with POSIX ``shlex`` rules; a string containing shell
operators (``|  &&  ;  >  <  &  ( )`` or a newline) is rejected rather than
misinterpreted. Shell features are available only by asking for a shell
explicitly, e.g. ``["bash", "-c", "pytest -q | tail -5"]``; that script is
then inspected by the same policy.

The policy is a guardrail against obvious damage, not a sandbox. It cannot
see what a program does internally (``python -c ...``, ``make``, test code).
"""

from __future__ import annotations

import os
import re
import shlex
import signal
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import IO, Mapping, Optional, Sequence, Union

from harness.config import API_KEY_VAR
from harness.tools.base import Tool, ToolContext, ToolFailure
from harness.tools.paths import relative_posix, resolve_in_repo

# Removed from every child process environment.
SCRUBBED_ENV_VARS = frozenset({API_KEY_VAR})

# Set for run_command / run_tests. Python must not write bytecode caches: a .pyc
# written by one run (e.g. the baseline) can be reused for a same-size source edit
# made within the same second (pyc validation uses whole-second mtime + size), so a
# later test run would execute stale code. It also keeps __pycache__ out of the
# target repository.
COMMAND_ENV = {"PYTHONDONTWRITEBYTECODE": "1"}

SHELL_OPERATOR_CHARS = "();<>|&\n"

# Programs that administer or destroy the machine. Matched by basename.
BLOCKED_PROGRAMS = frozenset({
    "sudo", "su", "doas", "pkexec",
    "shutdown", "reboot", "halt", "poweroff", "init", "telinit",
    "mkfs", "fdisk", "sfdisk", "cfdisk", "parted", "wipefs", "diskutil",
})
BLOCKED_PROGRAM_PREFIXES = ("mkfs.",)

# Programs whose path operands must stay inside the repository (and not be the
# repository root or anything in .git).
PATH_GUARDED_PROGRAMS = frozenset({"rm", "rmdir", "unlink", "shred", "chmod", "chown", "chgrp"})

SHELLS = frozenset({"sh", "bash", "zsh", "dash", "ksh", "fish"})
_ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")

# git is limited to read-only inspection.
GIT_READ_ONLY = frozenset({
    "status", "diff", "log", "show", "ls-files", "ls-tree", "rev-parse", "rev-list",
    "grep", "blame", "describe", "shortlog", "cat-file", "show-ref", "name-rev",
    "merge-base", "version", "help",
})
GIT_ALLOWED_GLOBAL_FLAGS = frozenset({"--no-pager", "-P", "--no-optional-locks", "--literal-pathspecs"})

Command = Union[str, Sequence[str]]


@dataclass(frozen=True)
class CommandResult:
    command: tuple[str, ...]
    cwd: str                    # repository-relative
    stdout: str
    stderr: str
    exit_code: Optional[int]    # None when the command timed out
    timed_out: bool
    duration_ms: int
    truncated: bool             # stdout or stderr was cut
    stdout_bytes: int           # full size before truncation
    stderr_bytes: int

    @property
    def ok(self) -> bool:
        return self.exit_code == 0 and not self.timed_out


# --------------------------------------------------------------------------
# Parsing
# --------------------------------------------------------------------------

def shell_tokens(text: str) -> list[str]:
    """Split like a POSIX shell would into words and operator tokens."""
    lexer = shlex.shlex(text, posix=True, punctuation_chars=SHELL_OPERATOR_CHARS)
    lexer.whitespace = " \t\r"
    lexer.whitespace_split = True
    try:
        return list(lexer)
    except ValueError as exc:  # e.g. unbalanced quotes
        raise ToolFailure("invalid_arguments", f"cannot parse command: {exc}") from None


def _is_operator(token: str) -> bool:
    return bool(token) and all(ch in SHELL_OPERATOR_CHARS for ch in token)


def parse_command(command: Command) -> list[str]:
    if isinstance(command, str):
        tokens = shell_tokens(command)
        operators = sorted({t for t in tokens if _is_operator(t)})
        if operators:
            shown = ", ".join(repr(o) for o in operators)
            raise ToolFailure(
                "invalid_arguments",
                f"shell operators ({shown}) are not interpreted. Run one command, or ask for a "
                f"shell explicitly: [\"bash\", \"-c\", \"<script>\"]",
            )
        argv = tokens
    else:
        argv = list(command)
        if not all(isinstance(a, str) for a in argv):
            raise ToolFailure("invalid_arguments", "command list items must be strings")
    if not argv or not argv[0].strip():
        raise ToolFailure("invalid_arguments", "command must not be empty")
    return argv


# --------------------------------------------------------------------------
# Policy
# --------------------------------------------------------------------------

def check_command(argv: Sequence[str], root: Path, cwd: Path) -> None:
    """Raise ``ToolFailure('command_blocked')`` if ``argv`` is obviously dangerous."""
    argv = _strip_wrappers(list(argv))
    if not argv:
        return
    program = os.path.basename(argv[0])

    if program in BLOCKED_PROGRAMS or program.startswith(BLOCKED_PROGRAM_PREFIXES):
        _block(f"'{program}' is not allowed: it administers or can destroy the machine")
    if program in PATH_GUARDED_PROGRAMS:
        _check_path_operands(program, argv[1:], root, cwd)
    elif program == "dd":
        for arg in argv[1:]:
            if arg.startswith("of="):
                _check_operand("dd", arg[3:], root, cwd)
    elif program == "git":
        _check_git(argv[1:], root, cwd)
    elif program in SHELLS:
        script = _shell_script(argv[1:])
        if script is not None:
            _check_shell_script(script, root, cwd)
    elif program == "xargs":
        rest = [a for a in argv[1:] if not a.startswith("-")]
        target = os.path.basename(rest[0]) if rest else ""
        if target in BLOCKED_PROGRAMS or target in PATH_GUARDED_PROGRAMS or target in SHELLS:
            _block(f"'xargs {target}' is not allowed: its operands cannot be checked")


def _block(reason: str) -> None:
    raise ToolFailure("command_blocked", reason)


def _strip_wrappers(argv: list[str]) -> list[str]:
    """Drop env/nohup/nice/time/timeout/command/exec prefixes to find the real program."""
    while argv:
        program = os.path.basename(argv[0])
        rest = argv[1:]
        if _ASSIGNMENT.match(argv[0]):  # FOO=1 cmd (shell scripts)
            argv = rest
        elif program in ("nohup", "time", "command", "exec"):
            argv = rest
        elif program == "env":
            while rest and ("=" in rest[0] and not rest[0].startswith("-") or rest[0] in ("-i", "-", "--ignore-environment")):
                rest = rest[1:]
            if rest and rest[0] in ("-u", "--unset"):
                rest = rest[2:]
            if rest and rest[0].startswith("-"):
                _block(f"env option '{rest[0]}' is not allowed")
            argv = rest
        elif program == "nice":
            if rest and rest[0] == "-n":
                rest = rest[2:]
            elif rest and rest[0].startswith("-"):
                rest = rest[1:]
            argv = rest
        elif program == "timeout":
            while rest and rest[0].startswith("-"):
                rest = rest[2:] if rest[0] in ("-s", "-k", "--signal", "--kill-after") else rest[1:]
            argv = rest[1:]  # drop DURATION
        else:
            return argv
    return argv


def _check_path_operands(program: str, args: Sequence[str], root: Path, cwd: Path) -> None:
    operands: list[str] = []
    options_done = False
    for arg in args:
        if not options_done and arg == "--":
            options_done = True
        elif not options_done and arg.startswith("-") and arg != "-":
            continue
        else:
            operands.append(arg)
    if program in ("chmod", "chown", "chgrp"):
        operands = operands[1:]  # the mode / owner operand
    for operand in operands:
        _check_operand(program, operand, root, cwd)


def _check_operand(program: str, operand: str, root: Path, cwd: Path) -> None:
    if any(ch in operand for ch in "$`*?[") or operand.startswith("~"):
        _block(f"'{program} {operand}': operands with shell expansion cannot be checked")
    try:
        target = resolve_in_repo(root, operand, base=cwd)
    except ToolFailure:
        _block(f"'{program} {operand}': target is outside the repository")
    if target == root or ".git" in target.relative_to(root).parts:
        _block(f"'{program} {operand}': refusing to touch the repository root or .git")


def _check_git(args: Sequence[str], root: Path, cwd: Path) -> None:
    args = list(args)
    while args and args[0].startswith("-"):
        flag = args.pop(0)
        if flag == "-C" and args:
            git_dir = args.pop(0)
            try:
                cwd = resolve_in_repo(root, git_dir, base=cwd)
            except ToolFailure:
                _block(f"'git -C {git_dir}': directory is outside the repository")
        elif flag not in GIT_ALLOWED_GLOBAL_FLAGS:
            _block(f"git option '{flag}' is not allowed")
    if not args:
        return
    sub = args[0]
    if sub not in GIT_READ_ONLY:
        _block(
            f"'git {sub}' is not allowed: the harness only runs read-only git commands "
            f"({', '.join(sorted(GIT_READ_ONLY))})"
        )
    for arg in args[1:]:
        if arg.startswith("--output") or arg in ("-O", "--open-files-in-pager") or arg.startswith("--open-files-in-pager="):
            _block(f"'git {sub} {arg}' is not allowed: it writes files or runs a program")


def _shell_script(args: Sequence[str]) -> Optional[str]:
    """Return the -c script of a shell invocation, if any (handles -c, -ec, -lc ...)."""
    for i, arg in enumerate(args):
        if arg.startswith("-") and not arg.startswith("--") and "c" in arg[1:]:
            return args[i + 1] if i + 1 < len(args) else ""
        if not arg.startswith("-"):
            return None  # a script file: its contents are not inspected
    return None



def _check_shell_script(script: str, root: Path, cwd: Path) -> None:
    tokens = shell_tokens(script)
    # Belt and braces for constructs the splitter does not follow ($(...), backticks).
    words = {w.strip("`$(){};'\"") for t in tokens for w in t.split()}
    blocked = sorted(w for w in words if os.path.basename(w) in BLOCKED_PROGRAMS)
    if blocked:
        _block(f"shell script uses '{blocked[0]}', which is not allowed")

    segment: list[str] = []
    redirect = False
    for token in tokens + [";"]:
        if _is_operator(token):
            redirect = ">" in token
            if segment:
                check_command(segment, root, cwd)
                segment = []
            continue
        if redirect:
            if token != "/dev/null":
                _check_operand("redirect >", token, root, cwd)
            redirect = False
            continue
        segment.append(token)


# --------------------------------------------------------------------------
# Execution
# --------------------------------------------------------------------------

def execute(argv: Sequence[str], *, root: Path, cwd: Optional[Path] = None, timeout: float,
            max_output_bytes: int, extra_env: Optional[Mapping[str, str]] = None) -> CommandResult:
    """Run ``argv`` without a shell. No policy check: callers decide that.

    stdin is closed, output goes to temporary files (so huge output cannot
    exhaust memory), and on timeout the whole process group is killed.
    """
    workdir = cwd or root
    env = {k: v for k, v in os.environ.items() if k not in SCRUBBED_ENV_VARS}
    env.update(extra_env or {})
    posix = os.name == "posix"

    with tempfile.TemporaryFile() as out, tempfile.TemporaryFile() as err:
        start = time.monotonic()
        try:
            proc = subprocess.Popen(list(argv), cwd=workdir, stdin=subprocess.DEVNULL,
                                    stdout=out, stderr=err, env=env, start_new_session=posix)
        except FileNotFoundError:
            raise ToolFailure("command_not_found", f"command not found: {argv[0]}") from None
        except PermissionError:
            raise ToolFailure("command_not_executable", f"permission denied: {argv[0]}") from None
        timed_out = False
        try:
            exit_code: Optional[int] = proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            _kill(proc, posix)
            proc.wait()
            exit_code = None
        except BaseException:
            _kill(proc, posix)
            proc.wait()
            raise
        duration_ms = int((time.monotonic() - start) * 1000)
        stdout, stdout_bytes, cut_out = read_bounded(out, max_output_bytes)
        stderr, stderr_bytes, cut_err = read_bounded(err, max_output_bytes)

    return CommandResult(
        command=tuple(argv), cwd=relative_posix(root, workdir), stdout=stdout, stderr=stderr,
        exit_code=exit_code, timed_out=timed_out, duration_ms=duration_ms,
        truncated=cut_out or cut_err, stdout_bytes=stdout_bytes, stderr_bytes=stderr_bytes,
    )


def _kill(proc: subprocess.Popen, posix: bool) -> None:
    try:
        if posix:
            os.killpg(proc.pid, signal.SIGKILL)
        else:
            proc.kill()
    except (ProcessLookupError, PermissionError):
        pass


def read_bounded(stream: IO[bytes], limit: int) -> tuple[str, int, bool]:
    """Return (text, total_bytes, truncated). Oversized output keeps the first
    40% and the last 60% (failures usually show at the end), cut at line breaks."""
    stream.seek(0, os.SEEK_END)
    size = stream.tell()
    stream.seek(0)
    if size <= limit:
        return stream.read().decode("utf-8", "replace"), size, False

    head_len = int(limit * 0.4)
    tail_len = limit - head_len
    head = stream.read(head_len)
    stream.seek(size - tail_len)
    tail = stream.read(tail_len)
    cut = head.rfind(b"\n")
    if cut > head_len // 2:
        head = head[: cut + 1]
    cut = tail.find(b"\n")
    if 0 <= cut < tail_len // 2:
        tail = tail[cut + 1:]
    omitted = size - len(head) - len(tail)
    marker = f"\n[... {omitted} bytes omitted ...]\n".encode()
    return (head + marker + tail).decode("utf-8", "replace"), size, True


# --------------------------------------------------------------------------
# Tools
# --------------------------------------------------------------------------

def _run(ctx: ToolContext, command: Command, cwd: Optional[str], timeout_seconds: Optional[int]) -> CommandResult:
    argv = parse_command(command)
    workdir = resolve_in_repo(ctx.root, cwd) if cwd else ctx.root
    if not workdir.is_dir():
        raise ToolFailure("not_a_directory", f"cwd '{cwd}' is not a directory")
    check_command(argv, ctx.root, workdir)
    limit = ctx.limits.command_timeout_seconds
    timeout = min(timeout_seconds or limit, limit)
    ctx.metrics.record_command()
    return execute(argv, root=ctx.root, cwd=workdir, timeout=timeout,
                   max_output_bytes=ctx.limits.max_output_bytes, extra_env=COMMAND_ENV)


def run_command(ctx: ToolContext, command: Command, cwd: Optional[str] = None,
                timeout_seconds: Optional[int] = None) -> CommandResult:
    return _run(ctx, command, cwd, timeout_seconds)


def run_tests(ctx: ToolContext, command: Command, cwd: Optional[str] = None,
              timeout_seconds: Optional[int] = None) -> CommandResult:
    """Run a caller-supplied test command. No test discovery happens here."""
    return _run(ctx, command, cwd, timeout_seconds)


_COMMAND_PARAMETERS = {
    "type": "object",
    "properties": {
        "command": {
            "type": ["string", "array"],
            "items": {"type": "string"},
            "description": "argv list, or a string split with POSIX shell quoting rules. Shell operators "
                           "are not interpreted; use [\"bash\", \"-c\", \"...\"] when a shell is needed.",
        },
        "cwd": {"type": "string", "description": "Repository-relative working directory (default: root)."},
        "timeout_seconds": {"type": "integer", "minimum": 1,
                            "description": "Capped at the configured command timeout."},
    },
    "required": ["command"],
    "additionalProperties": False,
}

TOOLS = (
    Tool(
        name="run_command",
        description="Run a command in the repository with a timeout. Returns stdout, stderr (bounded), "
                    "exit code and duration. Destructive system commands and state-changing git "
                    "commands are refused.",
        parameters=_COMMAND_PARAMETERS,
        handler=run_command,
        category="exec",
    ),
    Tool(
        name="run_tests",
        description="Run the given test command (e.g. 'python -m pytest -q tests/test_x.py') and "
                    "return its full structured result. Failing tests are a normal result, not a tool error.",
        parameters=_COMMAND_PARAMETERS,
        handler=run_tests,
        category="exec",
    ),
)

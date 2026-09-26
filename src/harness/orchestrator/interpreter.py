"""Deterministic Python interpreter resolution for discovered commands.

Repository intelligence (M3) reports Python commands as ``python -m ...`` or,
when the repository has its own virtualenv, ``.venv/bin/python -m ...``. A bare
``python`` may not exist (macOS ships ``python3`` only), so before a command is
offered for execution, its interpreter is resolved in this order:

1. the target repository's virtualenv (``.venv``/``venv``; ``bin/python`` or
   ``Scripts/python.exe``), as a repository-relative path;
2. the interpreter running the harness (``sys.executable``);
3. ``python3`` or ``python`` found on PATH.

Only commands whose program is ``python``/``python3`` (or a repository venv
interpreter) are touched. Everything else is returned unchanged.
"""

from __future__ import annotations

import shutil
import sys
from dataclasses import dataclass, replace
from pathlib import Path, PurePosixPath
from typing import Callable, Optional

from harness.repo.commands import CommandCandidate

VENV_INTERPRETERS = (".venv/bin/python", "venv/bin/python", ".venv/Scripts/python.exe", "venv/Scripts/python.exe")
_PYTHON_NAMES = {"python", "python3"}


@dataclass(frozen=True)
class Interpreter:
    path: str
    source: str   # "repository virtualenv", "harness interpreter" or "PATH"


def resolve_python(repo_root: Path, *, executable: Optional[str] = None,
                   which: Callable[[str], Optional[str]] = shutil.which) -> Optional[Interpreter]:
    for rel in VENV_INTERPRETERS:
        if (repo_root / rel).is_file():
            return Interpreter(rel, "repository virtualenv")
    current = sys.executable if executable is None else executable
    if current:
        return Interpreter(current, "harness interpreter")
    for name in ("python3", "python"):
        found = which(name)
        if found:
            return Interpreter(found, "PATH")
    return None


def is_python_program(program: str) -> bool:
    return PurePosixPath(program).name in _PYTHON_NAMES or program in VENV_INTERPRETERS


def resolve_command(candidate: CommandCandidate, repo_root: Path, *, executable: Optional[str] = None,
                    which: Callable[[str], Optional[str]] = shutil.which) -> CommandCandidate:
    if not candidate.argv or not is_python_program(candidate.argv[0]):
        return candidate
    interpreter = resolve_python(repo_root, executable=executable, which=which)
    if interpreter is None:
        return replace(candidate, reason=candidate.reason + "; no Python interpreter could be resolved")
    if interpreter.path == candidate.argv[0]:
        return candidate
    return replace(candidate, argv=(interpreter.path, *candidate.argv[1:]),
                   reason=f"{candidate.reason}; interpreter resolved to {interpreter.source}")

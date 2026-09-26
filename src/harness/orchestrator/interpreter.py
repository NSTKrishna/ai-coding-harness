"""Deterministic Python interpreter resolution for discovered commands.

Repository intelligence (M3) reports Python commands as ``python -m ...`` or,
when the repository has its own virtualenv, ``.venv/bin/python -m ...``. A bare
``python`` may not exist (macOS ships ``python3`` only), so before a command is
offered for execution, its interpreter is resolved in this order:

1. the target repository's virtualenv (``.venv``/``venv``; ``bin/python`` or
   ``Scripts/python.exe``), as a repository-relative path;
2. for ``python -m <module>`` with a third-party runner (e.g. pytest): the first of
   the harness interpreter, PATH ``python3``, PATH ``python`` that can import the
   module (a bounded ``-c "import <module>"`` probe; the harness's own venv has no
   packages, so a PATH interpreter with pytest installed is preferred over it);
3. the interpreter running the harness (``sys.executable``);
4. ``python3`` or ``python`` found on PATH.

Only commands whose program is ``python``/``python3`` (or a repository venv
interpreter) are touched. Everything else is returned unchanged.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from dataclasses import dataclass, replace
from pathlib import Path, PurePosixPath
from typing import Callable, Optional

from harness.repo.commands import CommandCandidate

VENV_INTERPRETERS = (".venv/bin/python", "venv/bin/python", ".venv/Scripts/python.exe", "venv/Scripts/python.exe")
_PYTHON_NAMES = {"python", "python3"}
STDLIB_RUNNERS = frozenset({"unittest", "doctest"})   # importable by any interpreter: no probe needed
PROBE_TIMEOUT_SECONDS = 15
_probe_cache: dict[tuple[str, str], bool] = {}


def can_import(interpreter: str, module: str) -> bool:
    """Whether ``interpreter`` can import ``module`` (cached; AI_API_KEY and PYTHONPATH not passed on)."""
    key = (interpreter, module)
    if key not in _probe_cache:
        env = {k: v for k, v in os.environ.items() if k not in ("AI_API_KEY", "PYTHONPATH", "PYTHONHOME")}
        try:
            done = subprocess.run([interpreter, "-c", f"import {module}"], stdin=subprocess.DEVNULL,
                                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env,
                                  timeout=PROBE_TIMEOUT_SECONDS)
            _probe_cache[key] = done.returncode == 0
        except (OSError, subprocess.SubprocessError):
            _probe_cache[key] = False
    return _probe_cache[key]


@dataclass(frozen=True)
class Interpreter:
    path: str
    source: str   # "repository virtualenv", "harness interpreter" or "PATH"


def resolve_python(repo_root: Path, *, executable: Optional[str] = None,
                   which: Callable[[str], Optional[str]] = shutil.which, module: Optional[str] = None,
                   probe: Callable[[str, str], bool] = can_import) -> Optional[Interpreter]:
    for rel in VENV_INTERPRETERS:
        if (repo_root / rel).is_file():
            return Interpreter(rel, "repository virtualenv")
    current = sys.executable if executable is None else executable
    if module and module.split(".")[0] not in STDLIB_RUNNERS:
        candidates = [(current, "harness interpreter")] + [(which(n), "PATH") for n in ("python3", "python")]
        seen = set()
        for path, source in candidates:
            if path and path not in seen:
                seen.add(path)
                if probe(path, module.split(".")[0]):
                    return Interpreter(path, f"{source} (can import {module.split('.')[0]})")
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
                    which: Callable[[str], Optional[str]] = shutil.which,
                    probe: Callable[[str, str], bool] = can_import) -> CommandCandidate:
    if not candidate.argv or not is_python_program(candidate.argv[0]):
        return candidate
    argv = candidate.argv
    module = argv[2] if len(argv) >= 3 and argv[1] == "-m" else None
    interpreter = resolve_python(repo_root, executable=executable, which=which, module=module, probe=probe)
    if interpreter is None:
        return replace(candidate, reason=candidate.reason + "; no Python interpreter could be resolved")
    if interpreter.path == candidate.argv[0]:
        return candidate
    return replace(candidate, argv=(interpreter.path, *candidate.argv[1:]),
                   reason=f"{candidate.reason}; interpreter resolved to {interpreter.source}")

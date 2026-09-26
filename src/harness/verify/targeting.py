"""Targeted test derivation: run the smallest useful verification first.

Derived before the baseline, deterministically, from evidence already in hand:
the ranked discovery candidates (test files linked to the task), the plan's
``files_to_inspect``, task identifiers, and the framework of a discovered suite
command. Only three shapes are produced, and only when the framework is certain:

- unittest (``python -m unittest discover`` with the repository root as top level,
  every directory a package): ``python -m unittest pkg.test_mod[.Class.test_method]``
- pytest (``python -m pytest`` / ``pytest``): ``python -m pytest path/test_x.py[::Class::test_fn]``
- go (``go test ./...``): ``go test ./pkg [-run '^(TestA|TestB)$']``

Method/function-level selection needs a test whose name contains a task term;
otherwise the file/package is targeted. Anything else yields no target and a
reason; the broad suite is then used alone. Nothing is invented.
"""

from __future__ import annotations

import ast
import posixpath
import re
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Optional, Sequence

from harness.repo.signals import keyword_stem, normalize_name
from harness.tools.base import ToolContext, ToolFailure
from harness.tools.files import load_text_lines

MIN_CANDIDATE_SCORE = 20      # a test file must be linked to the task at least this strongly
MAX_SELECTED_TESTS = 3


@dataclass(frozen=True)
class TargetedCommand:
    argv: tuple[str, ...]
    derived_from: str             # the evidence it came from
    confidence: str               # "high" (named tests) or "medium" (file / package)
    parent_command_id: str        # the broad suite it narrows
    reason: str
    framework: str


@dataclass(frozen=True)
class TargetingResult:
    targets: tuple[TargetedCommand, ...]
    unavailable_reason: Optional[str]     # set when no target could be derived
    files_read: tuple[str, ...] = ()


def framework_of(argv: Sequence[str]) -> Optional[str]:
    if "-m" in argv:
        i = list(argv).index("-m")
        if i + 1 < len(argv) and argv[i + 1] in ("unittest", "pytest"):
            return argv[i + 1]
    if argv and PurePosixPath(argv[0]).name == "pytest":
        return "pytest"
    if tuple(argv[:3]) == ("go", "test", "./..."):
        return "go"
    return None


def _terms(signals) -> list[str]:
    terms = [normalize_name(i) for i in signals.identifiers] + list(signals.name_parts) + \
            [keyword_stem(k) for k in signals.keywords]
    return [t for t in dict.fromkeys(terms) if len(t) >= 4]


def _matches(name: str, terms: Sequence[str]) -> bool:
    n = normalize_name(name)
    return any(t in n for t in terms)


def _python_tests(source: str) -> tuple[list[tuple[str, ...]], list[tuple[str, ...]]]:
    """(unittest-style (Class, method) pairs, pytest-style node paths) found in a module."""
    tree = ast.parse(source)
    class_methods, functions = [], []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith("test"):
            functions.append((node.name,))
        elif isinstance(node, ast.ClassDef):
            for item in node.body:
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)) and item.name.startswith("test"):
                    class_methods.append((node.name, item.name))
    return class_methods, functions


def _unittest_top(argv: Sequence[str]) -> Optional[str]:
    """Top-level directory the suite imports from, if it is the repository root."""
    args = list(argv)
    if "discover" not in args:
        return None
    top = args[args.index("-t") + 1] if "-t" in args and args.index("-t") + 1 < len(args) else None
    start = args[args.index("-s") + 1] if "-s" in args and args.index("-s") + 1 < len(args) else "."
    top = top or start          # unittest uses the start directory as top level when -t is absent
    return "." if top in (".", "./") else None


def derive_targets(ctx: ToolContext, discovery, plan, suite_commands) -> TargetingResult:
    """``suite_commands``: VerificationCommand objects of kind "test" (the broad suites)."""
    suites = [c for c in suite_commands if c.kind == "test" and framework_of(c.argv)]
    if not suite_commands:
        return TargetingResult((), "no test suite was discovered")
    if not suites:
        return TargetingResult((), "framework selector could not be derived safely for "
                               + ", ".join(c.text for c in suite_commands if c.kind == "test"))
    suite = suites[0]
    framework = framework_of(suite.argv)
    language = "Go" if framework == "go" else "Python"

    ranked = [c.path for c in discovery.candidates
              if c.category == "test" and c.score >= MIN_CANDIDATE_SCORE
              and (c.path.endswith("_test.go") if language == "Go" else c.path.endswith(".py"))]
    planned = [p for p in (plan.files_to_inspect if plan is not None else ())
               if p in {c.path for c in discovery.candidates if c.category == "test"}]
    files = list(dict.fromkeys(ranked + planned))
    if not files:
        return TargetingResult((), "no discovered test file is linked to the task strongly enough")
    test_file = files[0]
    derived = f"discovery candidate {test_file}"

    try:
        _, lines, _ = load_text_lines(ctx, test_file)
    except ToolFailure as exc:
        return TargetingResult((), f"{test_file} could not be read: {exc.code}")
    source = "".join(lines)
    terms = _terms(discovery.task_signals)

    if framework == "go":
        tests = re.findall(r"^func (Test\w+)\(", source, re.MULTILINE)
        picked = [t for t in tests if _matches(t, terms)][:MAX_SELECTED_TESTS]
        package = "./" + posixpath.dirname(test_file) if posixpath.dirname(test_file) else "."
        argv = ["go", "test", package]
        if picked:
            argv += ["-run", "^(" + "|".join(re.escape(t) for t in picked) + ")$"]
        target = TargetedCommand(tuple(argv), derived, "high" if picked else "medium", suite.id,
                                 f"{'tests ' + ', '.join(picked) if picked else 'package ' + package} "
                                 f"(from {test_file})", framework)
        return TargetingResult((target,), None, (test_file,))

    try:
        class_methods, functions = _python_tests(source)
    except SyntaxError:
        return TargetingResult((), f"{test_file} does not parse; no selector derived", (test_file,))

    if framework == "unittest":
        if _unittest_top(suite.argv) != ".":
            return TargetingResult((), "unittest suite does not import tests from the repository root; "
                                   "module names cannot be derived safely", (test_file,))
        parents = PurePosixPath(test_file).parts[:-1]
        for i in range(1, len(parents) + 1):
            if not (Path(ctx.root, *parents[:i]) / "__init__.py").is_file():
                return TargetingResult((), f"{'/'.join(parents[:i])} is not a package; module name of "
                                       f"{test_file} cannot be derived safely", (test_file,))
        module = test_file[:-3].replace("/", ".")
        picked = [f"{module}.{cls}.{meth}" for cls, meth in class_methods if _matches(meth, terms)][:MAX_SELECTED_TESTS]
        selectors = picked or [module]
        prefix = list(suite.argv[: list(suite.argv).index("unittest") + 1])
        target = TargetedCommand(tuple(prefix + selectors), derived, "high" if picked else "medium", suite.id,
                                 f"{'tests ' + ', '.join(picked) if picked else 'module ' + module}", framework)
        return TargetingResult((target,), None, (test_file,))

    # pytest
    nodes = [f"{test_file}::{name}" for (name,) in functions if _matches(name, terms)]
    nodes += [f"{test_file}::{cls}::{meth}" for cls, meth in class_methods
              if cls.startswith("Test") and _matches(meth, terms)]
    nodes = nodes[:MAX_SELECTED_TESTS]
    prefix = list(suite.argv[: list(suite.argv).index("pytest") + 1]) if "pytest" in suite.argv else list(suite.argv[:1])
    target = TargetedCommand(tuple(prefix + (nodes or [test_file])), derived, "high" if nodes else "medium", suite.id,
                             f"{'tests ' + ', '.join(nodes) if nodes else 'file ' + test_file}", framework)
    return TargetingResult((target,), None, (test_file,))

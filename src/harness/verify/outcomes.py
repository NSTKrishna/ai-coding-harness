"""Deterministic classification of verification command results.

Nothing here asks a model whether a command passed. Heuristics are deliberately
small and conservative:

- ENVIRONMENT_ERROR only for clear signs that the command could not run as
  intended (executable missing, exit 126/127, the test framework itself not
  importable, missing npm script / make target). A missing module that the
  code under test imports is a code failure, not an environment problem.
- A failure fingerprint is the set of failing test ids that common runners
  print (unittest, pytest, go test, cargo, jest/vitest file lines), plus a hash
  of the normalized output tail. Comparison prefers test ids; without ids it
  compares hashes.
"""

from __future__ import annotations

import enum
import hashlib
import re
from dataclasses import dataclass
from typing import Optional, Sequence

from harness.tools.base import ToolResult
from harness.tools.commands import CommandResult


class CommandStatus(str, enum.Enum):
    PASS = "PASS"
    TEST_FAILURE = "TEST_FAILURE"
    BUILD_FAILURE = "BUILD_FAILURE"
    LINT_FAILURE = "LINT_FAILURE"
    TYPECHECK_FAILURE = "TYPECHECK_FAILURE"
    ENVIRONMENT_ERROR = "ENVIRONMENT_ERROR"
    TIMEOUT = "TIMEOUT"
    TOOL_ERROR = "TOOL_ERROR"
    NOT_RUN = "NOT_RUN"


FAILURE_STATUS_BY_KIND = {
    "test": CommandStatus.TEST_FAILURE,
    "build": CommandStatus.BUILD_FAILURE,
    "lint": CommandStatus.LINT_FAILURE,
    "typecheck": CommandStatus.TYPECHECK_FAILURE,
    "format": CommandStatus.LINT_FAILURE,
}
FAILED = frozenset(FAILURE_STATUS_BY_KIND.values())


class Comparison(str, enum.Enum):
    UNCHANGED_PASS = "UNCHANGED_PASS"        # pass -> pass
    FIXED = "FIXED"                          # fail -> pass
    IMPROVED = "IMPROVED"                    # fail -> fail, strictly fewer failing tests, none new
    REGRESSED = "REGRESSED"                  # pass -> fail (or timeout)
    UNCHANGED_FAILURE = "UNCHANGED_FAILURE"  # fail -> same failure
    CHANGED_FAILURE = "CHANGED_FAILURE"      # fail -> different or additional failure
    ENVIRONMENT = "ENVIRONMENT"              # could not run as intended (either side)
    NO_BASELINE = "NO_BASELINE"              # no usable baseline result to compare with
    NOT_COMPARABLE = "NOT_COMPARABLE"        # post result unusable (not run, tool error, both timed out)


class FailureClass(str, enum.Enum):
    TASK_TEST_FAILURE = "TASK_TEST_FAILURE"
    REGRESSION = "REGRESSION"
    BUILD_FAILURE = "BUILD_FAILURE"
    LINT_FAILURE = "LINT_FAILURE"
    TYPECHECK_FAILURE = "TYPECHECK_FAILURE"
    COMMAND_TIMEOUT = "COMMAND_TIMEOUT"
    ENVIRONMENT_ERROR = "ENVIRONMENT_ERROR"
    NO_VERIFICATION_EVIDENCE = "NO_VERIFICATION_EVIDENCE"
    DIFF_PROBLEM = "DIFF_PROBLEM"


REPAIRABLE = frozenset({
    FailureClass.TASK_TEST_FAILURE, FailureClass.REGRESSION, FailureClass.BUILD_FAILURE,
    FailureClass.LINT_FAILURE, FailureClass.TYPECHECK_FAILURE, FailureClass.COMMAND_TIMEOUT,
    FailureClass.DIFF_PROBLEM,
})
# When several repairable failures exist, the report leads with the first of these.
REPAIR_PRIORITY = (
    FailureClass.DIFF_PROBLEM,   # nothing changed although edits were planned: the root cause of the rest
    FailureClass.REGRESSION, FailureClass.TASK_TEST_FAILURE, FailureClass.BUILD_FAILURE,
    FailureClass.TYPECHECK_FAILURE, FailureClass.LINT_FAILURE, FailureClass.COMMAND_TIMEOUT,
)
CHECK_FAILURE_CLASS = {
    "build": FailureClass.BUILD_FAILURE, "lint": FailureClass.LINT_FAILURE,
    "typecheck": FailureClass.TYPECHECK_FAILURE, "format": FailureClass.LINT_FAILURE,
}

# Test runners / tools whose absence means "cannot verify here", not "the code is broken".
FRAMEWORK_MODULES = frozenset({"pytest", "_pytest", "nose", "nose2", "unittest2", "hypothesis", "coverage",
                               "tox", "nox", "mypy", "ruff", "flake8", "pylint", "black"})
_MISSING_MODULE = re.compile(r"No module named '?([\w.]+)'?")
_ENVIRONMENT_PATTERNS = tuple(re.compile(p, re.IGNORECASE | re.MULTILINE) for p in (
    r"command not found",
    r"is not recognized as an internal or external command",
    r"^\S*sh: \S+: not found",
    r"^env: .*: No such file or directory",
    r"npm (?:ERR!|error) Missing script",
    r"No rule to make target",
    r"\*\*\* \[[^\]]+\] Error 127",          # make: a recipe command could not be executed
    r"^make(?:\[\d+\])?: \S+: No such file or directory",   # also as a sub-make ("make[1]: ...")
    r"error: no such (?:sub)?command",
    r"Cannot find module '(?:jest|vitest|mocha|typescript|eslint|ts-node)'",
    r"No such file or directory: '[^']*python[\d.]*'",
))

_FAILING_TEST_PATTERNS = (
    re.compile(r"^(?:FAIL|ERROR): (\w+) \(([\w.]+)\)", re.MULTILINE),                  # unittest
    re.compile(r"^(?:FAILED|ERROR) (\S+::\S+)", re.MULTILINE),                          # pytest summary
    re.compile(r"^\s*--- FAIL: (\S+)", re.MULTILINE),                                   # go test
    re.compile(r"^test (\S+) \.\.\. FAILED", re.MULTILINE),                             # cargo
    re.compile(r"^\s*(?:FAIL)\s+(\S+\.(?:test|spec)\.[jt]sx?)", re.MULTILINE),           # jest / vitest file
)
_TESTS_RUN_PATTERNS = (
    re.compile(r"^Ran (\d+) tests?", re.MULTILINE),                                     # unittest
    re.compile(r"test result: \w+\. (\d+) passed; (\d+) failed", re.MULTILINE),         # cargo
    re.compile(r"Tests:\s+.*?(\d+) total", re.MULTILINE),                               # jest
)
_PYTEST_COUNTS = re.compile(r"(\d+) (passed|failed|error)")
EXCERPT_CHARS = 1_500


@dataclass(frozen=True)
class Fingerprint:
    failing_tests: frozenset[str]
    output_hash: str
    tests_run: Optional[int]


@dataclass(frozen=True)
class Classification:
    status: CommandStatus
    exit_code: Optional[int]
    fingerprint: Optional[Fingerprint]
    excerpt: str          # bounded tail of the output, for evidence and repair context
    reason: str


def _tail(text: str, limit: int = EXCERPT_CHARS) -> str:
    return text if len(text) <= limit else "[...]" + text[-limit:]


def failing_tests(output: str) -> frozenset[str]:
    found = set()
    for pattern in _FAILING_TEST_PATTERNS:
        for m in pattern.finditer(output):
            if pattern is _FAILING_TEST_PATTERNS[0]:
                name, where = m.group(1), m.group(2)
                found.add(where if where.endswith("." + name) else f"{where}.{name}")  # 3.10 vs 3.11+ format
            else:
                found.add(m.group(1))
    return frozenset(found)


def tests_run(output: str) -> Optional[int]:
    for pattern in _TESTS_RUN_PATTERNS:
        m = pattern.search(output)
        if m:
            return sum(int(g) for g in m.groups())
    counts = _PYTEST_COUNTS.findall(output)
    return sum(int(n) for n, _ in counts) if counts else None


def output_hash(output: str) -> str:
    lines = [l.strip() for l in output.splitlines() if l.strip()][-30:]
    normalized = "\n".join(re.sub(r"\d+", "#", re.sub(r"0x[0-9a-fA-F]+", "0x", l)) for l in lines)
    return hashlib.sha1(normalized.encode("utf-8")).hexdigest()[:12]


def fingerprint(output: str) -> Fingerprint:
    return Fingerprint(failing_tests(output), output_hash(output), tests_run(output))


def _environment_reason(argv: Sequence[str], output: str, exit_code: Optional[int]) -> Optional[str]:
    if exit_code in (126, 127):
        return f"exit code {exit_code}: command could not be executed"
    runner = argv[2] if len(argv) >= 3 and argv[1] == "-m" else None
    for m in _MISSING_MODULE.finditer(output):
        module = m.group(1).split(".")[0]
        if module in FRAMEWORK_MODULES or module == runner:
            return f"required tool/module '{module}' is not installed for this interpreter"
    for pattern in _ENVIRONMENT_PATTERNS:
        m = pattern.search(output)
        if m:
            return f"environment problem: {m.group(0).strip()[:120]}"
    return None


def classify(kind: str, argv: Sequence[str], result: ToolResult) -> Classification:
    if not result.success:
        code = result.error.code
        if code in ("command_not_found", "command_not_executable"):
            return Classification(CommandStatus.ENVIRONMENT_ERROR, None, None, result.error.message,
                                  f"environment problem: {result.error.message}")
        return Classification(CommandStatus.TOOL_ERROR, None, None, result.error.message,
                              f"tool error {code}: {result.error.message}")
    data: CommandResult = result.data
    output = f"{data.stdout}\n{data.stderr}"
    excerpt = _tail(output.strip())
    if data.timed_out:
        return Classification(CommandStatus.TIMEOUT, None, fingerprint(output), excerpt, "command timed out")
    if data.exit_code == 0:
        return Classification(CommandStatus.PASS, 0, fingerprint(output), excerpt, "exit code 0")
    env = _environment_reason(argv, output, data.exit_code)
    if env:
        return Classification(CommandStatus.ENVIRONMENT_ERROR, data.exit_code, fingerprint(output), excerpt, env)
    status = FAILURE_STATUS_BY_KIND.get(kind, CommandStatus.TEST_FAILURE)
    return Classification(status, data.exit_code, fingerprint(output), excerpt, f"exit code {data.exit_code}")


def compare(baseline: Optional[Classification], post: Classification) -> Comparison:
    if post.status in (CommandStatus.NOT_RUN, CommandStatus.TOOL_ERROR):
        return Comparison.NOT_COMPARABLE
    if post.status == CommandStatus.ENVIRONMENT_ERROR or (
            baseline is not None and baseline.status == CommandStatus.ENVIRONMENT_ERROR):
        return Comparison.ENVIRONMENT
    if baseline is None or baseline.status in (CommandStatus.NOT_RUN, CommandStatus.TOOL_ERROR):
        return Comparison.NO_BASELINE
    base_ok = baseline.status == CommandStatus.PASS
    if post.status == CommandStatus.PASS:
        return Comparison.UNCHANGED_PASS if base_ok else Comparison.FIXED
    if base_ok:
        return Comparison.REGRESSED
    if post.status == CommandStatus.TIMEOUT or baseline.status == CommandStatus.TIMEOUT:
        return Comparison.NOT_COMPARABLE if post.status == baseline.status else Comparison.CHANGED_FAILURE
    before, after = baseline.fingerprint, post.fingerprint
    if before.failing_tests and after.failing_tests:
        if after.failing_tests == before.failing_tests:
            return Comparison.UNCHANGED_FAILURE
        if after.failing_tests < before.failing_tests:
            return Comparison.IMPROVED
        return Comparison.CHANGED_FAILURE
    return Comparison.UNCHANGED_FAILURE if after.output_hash == before.output_hash else Comparison.CHANGED_FAILURE

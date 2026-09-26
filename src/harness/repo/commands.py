"""Test / build / lint / typecheck command discovery from explicit configuration.

A command is proposed only when a configuration file supports it; nothing is
guessed from language alone, and nothing is executed here. Each candidate
carries its evidence. Order: high confidence first, then the rule order below.

Test rules (in order):
1. pytest configured (pytest.ini, [tool.pytest...], [tool:pytest], tox [pytest])  -> high
2. package.json ``scripts.test`` (not npm's placeholder)                          -> high
3. go.mod with ``*_test.go`` files                                                -> high
4. Cargo.toml                                                                     -> high
5. Makefile ``test`` target                                                       -> high
6. pytest referenced without config (conftest.py, requirements, imports)          -> medium
7. Python tests importing unittest, no pytest evidence                            -> medium
8. Maven / Gradle                                                                 -> medium
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import PurePosixPath

from harness.repo.facts import (
    ProjectFacts,
    package_dependencies,
    package_manager,
    package_scripts,
    pyproject_has,
    pytest_config,
)
from harness.repo.inventory import Inventory

NPM_PLACEHOLDER_TEST = "no test specified"
BUILD_SCRIPT_KINDS = {
    "build": "build", "compile": "build",
    "lint": "lint",
    "typecheck": "typecheck", "type-check": "typecheck", "types": "typecheck", "tsc": "typecheck",
    "check-types": "typecheck",
    "format": "format", "fmt": "format",
}
MAKE_TARGET_KINDS = {
    "build": "build", "all": "build", "lint": "lint", "typecheck": "typecheck", "type-check": "typecheck",
    "mypy": "typecheck", "format": "format", "fmt": "format",
}


@dataclass(frozen=True)
class CommandCandidate:
    kind: str                 # "test", "build", "lint", "typecheck" or "format"
    argv: tuple[str, ...]
    confidence: str           # "high" or "medium"
    reason: str


def discover_commands(inventory: Inventory, facts: ProjectFacts) -> tuple[list[CommandCandidate], list[CommandCandidate]]:
    return _test_commands(inventory, facts), _build_commands(facts)


def _test_commands(inventory: Inventory, facts: ProjectFacts) -> list[CommandCandidate]:
    rf = facts.root_files
    py = facts.python_interpreter
    found: list[CommandCandidate] = []

    def add(argv, confidence, reason):
        found.append(CommandCandidate("test", tuple(argv), confidence, reason))

    configured = pytest_config(facts)
    if configured:
        add([py, "-m", "pytest"], "high", f"pytest is configured in {configured}{facts.python_interpreter_note}")

    scripts = package_scripts(facts)
    test_script = scripts.get("test")
    if test_script and NPM_PLACEHOLDER_TEST not in test_script:
        manager = package_manager(facts)
        add([manager, "test"], "high", f"package.json scripts.test = {test_script!r}")

    if "go.mod" in rf and any(p.endswith("_test.go") for p in facts.all_paths):
        add(["go", "test", "./..."], "high", "go.mod present and *_test.go files exist")

    if "Cargo.toml" in rf:
        add(["cargo", "test"], "high", "Cargo.toml present")

    if "test" in facts.make_targets:
        add(["make", "test"], "high", f"{facts.make_file} defines a 'test' target")

    if not configured and facts.python_tests:
        weak = ("conftest.py" if any(PurePosixPath(p).name == "conftest.py" for p in facts.all_paths) else None) \
            or facts.requirements_mention_pytest \
            or (f"{facts.pytest_importers[0]} imports pytest" if facts.pytest_importers else None)
        if weak:
            add([py, "-m", "pytest"], "medium", f"pytest referenced by {weak} (no pytest configuration)"
                + facts.python_interpreter_note)
        elif facts.unittest_importers:
            argv, where = _unittest_argv(py, facts, inventory)
            add(argv, "medium", f"{len(facts.unittest_importers)} test file(s) import unittest "
                f"(e.g. {facts.unittest_importers[0]}){where}; no pytest evidence" + facts.python_interpreter_note)

    if "pom.xml" in rf:
        wrapper = "mvnw" in rf
        add(["./mvnw" if wrapper else "mvn", "test"], "medium",
            "pom.xml present" + (" with the mvnw wrapper" if wrapper else ""))
    if "build.gradle" in rf or "build.gradle.kts" in rf:
        wrapper = "gradlew" in rf
        add(["./gradlew" if wrapper else "gradle", "test"], "medium",
            "Gradle build file present" + (" with the gradlew wrapper" if wrapper else ""))

    return sorted(found, key=lambda c: 0 if c.confidence == "high" else 1)  # stable: keeps rule order


def _unittest_argv(py: str, facts: ProjectFacts, inventory: Inventory) -> tuple[list[str], str]:
    tops = {p.split("/", 1)[0] if "/" in p else "." for p in facts.python_tests}
    if len(tops) == 1 and "." not in tops:
        top = next(iter(tops))
        if inventory.get(f"{top}/__init__.py") is not None:
            return [py, "-m", "unittest", "discover", "-s", top, "-t", "."], f"; tests live in {top}/ (a package)"
        return [py, "-m", "unittest", "discover", "-s", top], f"; tests live in {top}/"
    return [py, "-m", "unittest", "discover"], ""


def _build_commands(facts: ProjectFacts) -> list[CommandCandidate]:
    rf = facts.root_files
    found: list[CommandCandidate] = []
    kinds_seen: set[str] = set()

    def add(kind, argv, confidence, reason):
        found.append(CommandCandidate(kind, tuple(argv), confidence, reason))
        kinds_seen.add(kind)

    manager = package_manager(facts)
    for name, script in sorted(package_scripts(facts).items()):
        kind = BUILD_SCRIPT_KINDS.get(name)
        if kind:
            add(kind, [manager, "run", name], "high", f"package.json scripts.{name} = {script!r}")
    for target in facts.make_targets:
        kind = MAKE_TARGET_KINDS.get(target)
        if kind:
            add(kind, ["make", target], "high", f"{facts.make_file} defines a '{target}' target")

    if "go.mod" in rf:
        add("build", ["go", "build", "./..."], "medium", "go.mod present")
    if "Cargo.toml" in rf:
        add("build", ["cargo", "build"], "medium", "Cargo.toml present")
    if "clippy.toml" in rf:
        add("lint", ["cargo", "clippy"], "medium", "clippy.toml present")

    ruff = next((f for f in ("ruff.toml", ".ruff.toml") if f in rf), None) or \
        ("pyproject.toml [tool.ruff]" if pyproject_has(facts, "ruff") else None)
    if ruff:
        add("lint", ["ruff", "check", "."], "medium", f"ruff is configured in {ruff}")
    flake8 = ".flake8" if ".flake8" in rf else "setup.cfg [flake8]" if "flake8" in facts.setup_cfg_sections \
        else "tox.ini [flake8]" if "flake8" in facts.tox_sections else None
    if flake8:
        add("lint", ["flake8"], "medium", f"flake8 is configured in {flake8}")
    mypy = next((f for f in ("mypy.ini", ".mypy.ini") if f in rf), None) or \
        ("pyproject.toml [tool.mypy]" if pyproject_has(facts, "mypy") else None) or \
        ("setup.cfg [mypy]" if "mypy" in facts.setup_cfg_sections else None)
    if mypy:
        add("typecheck", ["mypy", "."], "medium", f"mypy is configured in {mypy}")

    deps = package_dependencies(facts)
    if "tsconfig.json" in rf and "typescript" in deps and "typecheck" not in kinds_seen:
        add("typecheck", ["npx", "tsc", "--noEmit"], "medium", "tsconfig.json and typescript dependency present")
    eslint = next((f for f in sorted(rf) if f.startswith((".eslintrc", "eslint.config."))), None)
    if eslint and "eslint" in deps and "lint" not in kinds_seen:
        add("lint", ["npx", "eslint", "."], "medium", f"eslint configured in {eslint}")

    return sorted(found, key=lambda c: 0 if c.confidence == "high" else 1)

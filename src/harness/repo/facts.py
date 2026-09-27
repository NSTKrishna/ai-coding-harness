"""Facts read from a repository's configuration files (bounded, counted reads).

Shared by the profile and command discovery. Only root-level configuration is
interpreted; nested projects are listed as manifests but not read.
"""

from __future__ import annotations

import configparser
import json
import re
import shlex
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import Optional

from harness.repo.inventory import Inventory
from harness.repo.reader import RepoReader

CONFIG_READ_BYTES = 65_536
PACKAGE_JSON_READ_BYTES = 262_144
MAX_TEST_SNIFF_FILES = 20
TEST_SNIFF_BYTES = 8_192
JVM_MANIFESTS = ("pom.xml", "build.gradle", "build.gradle.kts")

CI_WORKFLOW_DIR = ".github/workflows/"
MAX_CI_FILES = 20
CI_READ_BYTES = 32_768
# Read the workflows most likely to hold the test command first: a large repository
# can have dozens, and an alphabetical cut can miss the only one that matters.
CI_NAME_HINTS = ("test", "ci", "check", "build", "lint", "unit", "verify")

_TOML_SECTION = re.compile(r"^\s*\[\[?\s*([^\]\s]+)\s*\]\]?", re.MULTILINE)
_MAKE_TARGET = re.compile(r"^([A-Za-z0-9][A-Za-z0-9_.-]*)\s*:(?!=)", re.MULTILINE)

# A CI step's command line, whether written inline after "run:" or as a line of a
# "run: |" block scalar. Only the runners the harness can also discover itself.
_CI_RUN_PREFIX = re.compile(r"^\s*(?:-\s*)?(?:run\s*:\s*)?(?P<cmd>\S.*)$")
_CI_RUN_KEY = re.compile(r"^(?P<indent>\s*)(?:-\s+)?run\s*:(?P<rest>.*)$")
_CI_TEST_START = re.compile(
    r"^(?:go\s+test"
    r"|(?:python[0-9.]*\s+-m\s+)?pytest"
    r"|(?:python[0-9.]*\s+-m\s+)unittest"
    r"|(?:npm|yarn|pnpm)\s+(?:run\s+)?test"
    r"|cargo\s+test"
    r"|(?:\./)?(?:mvnw|mvn|gradlew|gradle)\s+test"
    r")\b")
# Flags that serve CI reporting and only slow down or reshape an unattended run.
_CI_ONLY_FLAGS = ("-json", "-race", "--race")
_CI_ONLY_PREFIXES = ("-coverprofile=", "-covermode=", "--cov=", "--cov-report=", "--cov-branch",
                     "--junitxml=", "--junit-xml=", "-coverpkg=")


@dataclass
class ProjectFacts:
    root_files: set[str] = field(default_factory=set)
    all_paths: frozenset[str] = frozenset()
    pyproject_sections: set[str] = field(default_factory=set)
    pyproject_text: str = ""
    setup_cfg_sections: set[str] = field(default_factory=set)
    tox_sections: set[str] = field(default_factory=set)
    package_json: Optional[dict] = None
    make_file: Optional[str] = None
    make_targets: tuple[str, ...] = ()
    ci_test_commands: tuple[tuple[tuple[str, ...], str], ...] = ()   # (argv, workflow path)
    node_modules_present: bool = True     # only meaningful when package.json exists
    requirements_mention_pytest: Optional[str] = None   # the requirements file that lists pytest
    python_tests: tuple[str, ...] = ()
    unittest_importers: tuple[str, ...] = ()
    pytest_importers: tuple[str, ...] = ()
    jvm_manifest_text: dict[str, str] = field(default_factory=dict)  # lowercased heads
    python_interpreter: str = "python"
    python_interpreter_note: str = ""


def _ini_sections(text: str) -> set[str]:
    parser = configparser.ConfigParser(strict=False, interpolation=None)
    try:
        parser.read_string(text)
    except configparser.Error:
        return set(re.findall(r"^\s*\[([^\]]+)\]", text, re.MULTILINE))
    return set(parser.sections())


def _ci_command(line: str) -> Optional[tuple[str, ...]]:
    """One command line -> a runnable test argv, or None (CI-only flags dropped)."""
    if "${{" in line or "$(" in line or "`" in line:
        return None
    match = _CI_RUN_PREFIX.match(line)
    if not match:
        return None
    for segment in re.split(r"&&|\|\||;", match.group("cmd").strip()):
        segment = re.split(r"\s+\d*>{1,2}|\s*\|", segment)[0].strip()
        if not _CI_TEST_START.match(segment):
            continue
        try:
            argv = shlex.split(segment)
        except ValueError:
            return None
        argv = [a for a in argv if a not in _CI_ONLY_FLAGS and not a.startswith(_CI_ONLY_PREFIXES)]
        return tuple(argv) if len(argv) >= 2 else None
    return None


def _ci_run_blocks(text: str) -> list[list[str]]:
    """The command lines of each workflow ``run:`` step, skipping any step that runs elsewhere.

    A step is dropped whole when it changes directory (``cd`` on any line of a
    ``run: |`` block, or a ``working-directory:`` key), because a CommandCandidate
    carries no working directory and the command would run at the repository root.
    """
    lines = text.splitlines()
    blocks: list[list[str]] = []
    for i, line in enumerate(lines):
        match = _CI_RUN_KEY.match(line)
        if not match:
            continue
        indent, inline = len(match.group("indent")), match.group("rest").strip()
        body = [inline] if inline not in ("", "|", ">", "|-", ">-", "|+") else []
        for follow in lines[i + 1:]:
            if not follow.strip():
                continue
            if len(follow) - len(follow.lstrip()) <= indent:
                break
            body.append(follow.strip())
        if any(b == "cd" or b.startswith(("cd ", "pushd ")) or " cd " in b for b in body):
            continue
        if _step_changes_directory(lines, i, indent):
            continue
        blocks.append(body)
    return blocks


def _step_changes_directory(lines: list[str], run_index: int, indent: int) -> bool:
    """True if the step holding this ``run:`` sets working-directory."""
    for line in reversed(lines[max(run_index - 12, 0):run_index]):
        if not line.strip():
            continue
        line_indent = len(line) - len(line.lstrip())
        if line_indent < indent or (line_indent == indent and line.lstrip().startswith("- ")
                                    and not _CI_RUN_KEY.match(line)):
            return line.lstrip().lstrip("- ").startswith("working-directory")
        if line_indent == indent and line.lstrip().startswith("working-directory"):
            return True
    return False


def _ci_test_commands(facts: ProjectFacts, reader: RepoReader) -> tuple[tuple[tuple[str, ...], str], ...]:
    """Test commands the project's own CI runs, in workflow order, de-duplicated."""
    found: list[tuple[tuple[str, ...], str]] = []
    seen: set[tuple[str, ...]] = set()
    paths = sorted((p for p in facts.all_paths
                    if p.startswith(CI_WORKFLOW_DIR) and p.endswith((".yml", ".yaml"))),
                   key=lambda p: (not any(h in p.rsplit("/", 1)[-1].lower() for h in CI_NAME_HINTS), p))
    for path in paths[:MAX_CI_FILES]:
        for body in _ci_run_blocks(reader.head(path, CI_READ_BYTES) or ""):
            for line in body:
                argv = _ci_command(line)
                if argv is not None and argv not in seen:
                    seen.add(argv)
                    found.append((argv, path))
    return tuple(found)


def gather_facts(inventory: Inventory, reader: RepoReader) -> ProjectFacts:
    facts = ProjectFacts()
    facts.all_paths = frozenset(inventory.paths)
    facts.root_files = {p for p in inventory.paths if "/" not in p}
    rf = facts.root_files

    if "pyproject.toml" in rf:
        facts.pyproject_text = reader.head("pyproject.toml", CONFIG_READ_BYTES) or ""
        # Line-based on purpose: tomllib is 3.11+, and results must not depend on the Python version.
        facts.pyproject_sections = {m.group(1) for m in _TOML_SECTION.finditer(facts.pyproject_text)}
    if "setup.cfg" in rf:
        facts.setup_cfg_sections = _ini_sections(reader.head("setup.cfg", CONFIG_READ_BYTES) or "")
    if "tox.ini" in rf:
        facts.tox_sections = _ini_sections(reader.head("tox.ini", CONFIG_READ_BYTES) or "")
    if "package.json" in rf:
        try:
            data = json.loads(reader.head("package.json", PACKAGE_JSON_READ_BYTES) or "")
            facts.package_json = data if isinstance(data, dict) else None
        except json.JSONDecodeError:
            facts.package_json = None
    for name in ("Makefile", "GNUmakefile", "makefile"):
        if name in rf:
            text = reader.head(name, CONFIG_READ_BYTES) or ""
            facts.make_file = name
            facts.make_targets = tuple(dict.fromkeys(
                t for t in _MAKE_TARGET.findall(text) if not t.startswith(".")))
            break
    for rel in sorted(rf):
        if re.match(r"^requirements.*\.(txt|in)$", rel):
            if re.search(r"^\s*pytest\b", reader.head(rel, CONFIG_READ_BYTES) or "", re.MULTILINE):
                facts.requirements_mention_pytest = rel
                break
    for manifest in JVM_MANIFESTS:
        if manifest in rf:
            facts.jvm_manifest_text[manifest] = (reader.head(manifest, CONFIG_READ_BYTES) or "").lower()

    facts.ci_test_commands = _ci_test_commands(facts, reader)
    if "package.json" in rf:
        # Not from the inventory: node_modules is skipped when walking the repository.
        facts.node_modules_present = (reader.ctx.root / "node_modules").is_dir()

    facts.python_tests = tuple(f.path for f in inventory.files
                               if f.category == "test" and f.language == "Python"
                               and PurePosixPath(f.path).name != "conftest.py")
    unittest_users, pytest_users = [], []
    for rel in facts.python_tests[:MAX_TEST_SNIFF_FILES]:
        head = reader.head(rel, TEST_SNIFF_BYTES) or ""
        if re.search(r"^\s*(import unittest|from unittest\b)", head, re.MULTILINE):
            unittest_users.append(rel)
        if re.search(r"^\s*(import pytest|from pytest\b)", head, re.MULTILINE):
            pytest_users.append(rel)
    facts.unittest_importers = tuple(unittest_users)
    facts.pytest_importers = tuple(pytest_users)

    for candidate in (".venv/bin/python", "venv/bin/python"):
        if (inventory.root / candidate).is_file():
            facts.python_interpreter = candidate
            facts.python_interpreter_note = f"; interpreter: the repository's {candidate}"
            break
    return facts


def package_dependencies(facts: ProjectFacts) -> set[str]:
    data = facts.package_json or {}
    deps: set[str] = set()
    for key in ("dependencies", "devDependencies", "peerDependencies"):
        if isinstance(data.get(key), dict):
            deps.update(data[key])
    return deps


def package_scripts(facts: ProjectFacts) -> dict[str, str]:
    scripts = (facts.package_json or {}).get("scripts")
    if not isinstance(scripts, dict):
        return {}
    return {k: v for k, v in scripts.items() if isinstance(v, str)}


def package_manager(facts: ProjectFacts) -> str:
    if "pnpm-lock.yaml" in facts.root_files:
        return "pnpm"
    if "yarn.lock" in facts.root_files:
        return "yarn"
    return "npm"


def pytest_config(facts: ProjectFacts) -> Optional[str]:
    """Where pytest is explicitly configured, if anywhere."""
    if "pytest.ini" in facts.root_files:
        return "pytest.ini"
    if any(s.startswith("tool.pytest") for s in facts.pyproject_sections):
        return "pyproject.toml [tool.pytest.ini_options]"
    if "tool:pytest" in facts.setup_cfg_sections:
        return "setup.cfg [tool:pytest]"
    if "pytest" in facts.tox_sections:
        return "tox.ini [pytest]"
    return None


def pyproject_has(facts: ProjectFacts, tool: str) -> bool:
    return any(s == f"tool.{tool}" or s.startswith(f"tool.{tool}.") for s in facts.pyproject_sections)

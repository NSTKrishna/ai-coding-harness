"""RepositoryAnalyzer and RepoProfile: facts about a repository.

The profile reports what the file inventory and configuration files show. It
does not infer intent, run anything, or modify the repository.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Optional

from harness.repo.classify import CODE_LANGUAGES
from harness.repo.commands import CommandCandidate, discover_commands
from harness.repo.facts import ProjectFacts, gather_facts, package_dependencies, package_manager, pyproject_has, pytest_config
from harness.repo.inventory import Inventory, build_inventory
from harness.repo.reader import RepoReader
from harness.tools.base import ToolContext

MAX_LISTED = 10   # entries per profile list
MAX_ROOTS = 5


@dataclass(frozen=True)
class Indicator:
    name: str
    evidence: str   # the file (and section) that shows it


@dataclass(frozen=True)
class RepoProfile:
    root: str
    is_git: bool
    inventory_source: str
    file_count: int
    total_bytes: int
    languages: tuple[tuple[str, int], ...]         # code languages by file count
    categories: tuple[tuple[str, int], ...]
    important_dirs: tuple[tuple[str, int], ...]    # top-level directories by file count
    manifests: tuple[str, ...]
    build_systems: tuple[Indicator, ...]
    test_frameworks: tuple[Indicator, ...]
    ci: tuple[str, ...]
    lint_typecheck: tuple[Indicator, ...]
    docs: tuple[str, ...]
    source_roots: tuple[str, ...]
    test_roots: tuple[str, ...]
    test_commands: tuple[CommandCandidate, ...]
    build_commands: tuple[CommandCandidate, ...]   # build, lint, typecheck, format
    warnings: tuple[str, ...] = ()

    def summary(self) -> str:
        """Compact, deterministic text for a planner prompt."""
        def names(items):
            return ", ".join(i.name for i in items) or "none found"
        lines = [
            f"Repository: {self.file_count} files ({'git' if self.is_git else 'not a git repository'})",
            "Languages: " + (", ".join(f"{name} ({n})" for name, n in self.languages) or "none detected"),
            "Source roots: " + (", ".join(self.source_roots) or "none detected"),
            "Test roots: " + (", ".join(self.test_roots) or "none detected"),
            "Manifests: " + (", ".join(self.manifests) or "none"),
            f"Build systems: {names(self.build_systems)}",
            f"Test frameworks: {names(self.test_frameworks)}",
            f"Lint/typecheck: {names(self.lint_typecheck)}",
            "CI: " + (", ".join(self.ci) or "none"),
            "Test commands: " + ("; ".join(" ".join(c.argv) for c in self.test_commands) or "none identified"),
        ]
        return "\n".join(lines)


def _build_systems(facts: ProjectFacts) -> list[Indicator]:
    rf = facts.root_files
    found = []
    if "pyproject.toml" in rf:
        backend = re.search(r'build-backend\s*=\s*"([^"]+)"', facts.pyproject_text)
        found.append(Indicator("Python packaging" + (f" ({backend.group(1)})" if backend else ""), "pyproject.toml"))
    if "setup.py" in rf:
        found.append(Indicator("setuptools", "setup.py"))
    if "package.json" in rf:
        found.append(Indicator(package_manager(facts), "package.json"))
    for name, label in (("go.mod", "Go modules"), ("Cargo.toml", "Cargo"), ("pom.xml", "Maven"),
                        ("build.gradle", "Gradle"), ("build.gradle.kts", "Gradle"), ("CMakeLists.txt", "CMake"),
                        ("Makefile", "Make"), ("GNUmakefile", "Make"), ("Dockerfile", "Docker")):
        if name in rf:
            found.append(Indicator(label, name))
    return found


def _test_frameworks(facts: ProjectFacts) -> list[Indicator]:
    rf = facts.root_files
    found = []
    conftest = next((p for p in sorted(facts.all_paths) if PurePosixPath(p).name == "conftest.py"), None)
    pytest_evidence = pytest_config(facts) or conftest or facts.requirements_mention_pytest or (
        f"{facts.pytest_importers[0]} imports pytest" if facts.pytest_importers else None)
    if pytest_evidence:
        found.append(Indicator("pytest", pytest_evidence))
    if facts.unittest_importers:
        found.append(Indicator("unittest", f"{facts.unittest_importers[0]} imports unittest"))
    deps = package_dependencies(facts)
    for name in ("jest", "vitest", "mocha", "ava", "jasmine"):
        if name in deps:
            found.append(Indicator(name, f"package.json dependency '{name}'"))
    if "go.mod" in rf and any(p.endswith("_test.go") for p in facts.all_paths):
        found.append(Indicator("go test", "go.mod and *_test.go files"))
    if "Cargo.toml" in rf:
        found.append(Indicator("cargo test", "Cargo.toml"))
    for manifest, text in sorted(facts.jvm_manifest_text.items()):
        if "junit" in text:
            found.append(Indicator("JUnit", f"{manifest} mentions junit"))
    return found


def _lint_typecheck(facts: ProjectFacts) -> list[Indicator]:
    rf = facts.root_files
    found = []

    def add(name: str, evidence: Optional[str]) -> None:
        if evidence:
            found.append(Indicator(name, evidence))

    def first(names) -> Optional[str]:
        return next((n for n in names if n in rf), None)

    add("ruff", first(("ruff.toml", ".ruff.toml")) or ("pyproject.toml [tool.ruff]" if pyproject_has(facts, "ruff") else None))
    add("flake8", ".flake8" if ".flake8" in rf else "setup.cfg [flake8]" if "flake8" in facts.setup_cfg_sections
        else "tox.ini [flake8]" if "flake8" in facts.tox_sections else None)
    add("pylint", ".pylintrc" if ".pylintrc" in rf else "pyproject.toml [tool.pylint]" if pyproject_has(facts, "pylint") else None)
    add("mypy", first(("mypy.ini", ".mypy.ini")) or ("pyproject.toml [tool.mypy]" if pyproject_has(facts, "mypy") else None)
        or ("setup.cfg [mypy]" if "mypy" in facts.setup_cfg_sections else None))
    add("black", "pyproject.toml [tool.black]" if pyproject_has(facts, "black") else None)
    add("eslint", next((f for f in sorted(rf) if f.startswith((".eslintrc", "eslint.config."))), None))
    add("prettier", next((f for f in sorted(rf) if f.startswith((".prettierrc", "prettier.config."))), None))
    add("TypeScript compiler", "tsconfig.json" if "tsconfig.json" in rf else None)
    add("golangci-lint", first((".golangci.yml", ".golangci.yaml")))
    add("clippy", "clippy.toml" if "clippy.toml" in rf else None)
    add("rustfmt", first(("rustfmt.toml", ".rustfmt.toml")))
    return found


def _roots(inventory: Inventory, category: str) -> list[str]:
    """Top-level directories (or "." for the root) holding code files of ``category``."""
    counts: Counter = Counter()
    for f in inventory.files:
        if f.category == category and f.language in CODE_LANGUAGES:
            counts[f.path.split("/", 1)[0] if "/" in f.path else "."] += 1
    return [d for d, _ in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))][:MAX_ROOTS]


def _top(counter: Counter, limit: int = MAX_LISTED) -> tuple[tuple[str, int], ...]:
    return tuple(sorted(counter.items(), key=lambda kv: (-kv[1], kv[0]))[:limit])


class RepositoryAnalyzer:
    """Builds the inventory, facts and profile for one repository root."""

    def __init__(self, ctx: ToolContext, reader: Optional[RepoReader] = None) -> None:
        self.ctx = ctx
        self.reader = reader or RepoReader(ctx)
        self.inventory: Optional[Inventory] = None
        self.facts: Optional[ProjectFacts] = None

    def analyze(self) -> RepoProfile:
        inventory = build_inventory(self.ctx)
        facts = gather_facts(inventory, self.reader)
        self.inventory, self.facts = inventory, facts

        by_category: dict[str, list[str]] = {}
        for f in inventory.files:
            by_category.setdefault(f.category, []).append(f.path)
        tests, builds = discover_commands(inventory, facts)
        docs = [p for p in inventory.paths
                if re.match(r"^(README|CONTRIBUTING)(\..*)?$", p, re.IGNORECASE)
                or p.lower() in ("docs/index.md", "docs/readme.md", "docs/index.rst")]
        return RepoProfile(
            root=str(inventory.root),
            is_git=inventory.is_git,
            inventory_source=inventory.source,
            file_count=len(inventory.files),
            total_bytes=sum(f.size_bytes for f in inventory.files),
            languages=_top(Counter(f.language for f in inventory.files if f.language in CODE_LANGUAGES)),
            categories=_top(Counter(f.category for f in inventory.files), limit=100),
            important_dirs=_top(Counter(f.path.split("/", 1)[0] for f in inventory.files if "/" in f.path)),
            manifests=tuple(by_category.get("manifest", ()))[:MAX_LISTED],
            build_systems=tuple(_build_systems(facts)),
            test_frameworks=tuple(_test_frameworks(facts)),
            ci=tuple(by_category.get("ci", ()))[:MAX_LISTED],
            lint_typecheck=tuple(_lint_typecheck(facts)),
            docs=tuple(docs)[:MAX_LISTED],
            source_roots=tuple(_roots(inventory, "source")),
            test_roots=tuple(_roots(inventory, "test")),
            test_commands=tuple(tests),
            build_commands=tuple(builds),
            warnings=inventory.warnings,
        )


def analyze_repository(repo: Path | str) -> RepoProfile:
    """Profile a repository. No model, no API key, no writes."""
    return RepositoryAnalyzer(ToolContext.create(repo)).analyze()

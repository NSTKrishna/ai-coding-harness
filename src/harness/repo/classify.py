"""Cheap, name-based file classification: language and category.

Nothing here reads file contents. It is meant to be useful, not exact.
"""

from __future__ import annotations

import re
from pathlib import PurePosixPath
from typing import Optional

LANGUAGES_BY_EXTENSION = {
    ".py": "Python", ".pyi": "Python",
    ".ts": "TypeScript", ".mts": "TypeScript", ".cts": "TypeScript", ".tsx": "TypeScript/TSX",
    ".js": "JavaScript", ".mjs": "JavaScript", ".cjs": "JavaScript", ".jsx": "JavaScript/JSX",
    ".go": "Go", ".rs": "Rust", ".java": "Java", ".kt": "Kotlin", ".kts": "Kotlin",
    ".scala": "Scala", ".rb": "Ruby", ".php": "PHP", ".cs": "C#", ".swift": "Swift",
    ".c": "C", ".h": "C/C++ header", ".cc": "C++", ".cpp": "C++", ".cxx": "C++", ".hpp": "C++",
    ".m": "Objective-C", ".sh": "Shell", ".bash": "Shell", ".zsh": "Shell",
    ".sql": "SQL", ".html": "HTML", ".css": "CSS", ".scss": "SCSS", ".vue": "Vue", ".svelte": "Svelte",
    ".md": "Markdown", ".rst": "reStructuredText", ".txt": "Text",
    ".json": "JSON", ".yaml": "YAML", ".yml": "YAML", ".toml": "TOML", ".ini": "INI", ".cfg": "INI",
    ".xml": "XML", ".gradle": "Gradle",
}
LANGUAGES_BY_NAME = {"Makefile": "Makefile", "GNUmakefile": "Makefile", "Dockerfile": "Dockerfile"}

# Languages whose files count as source code (for "source" / "test" categories).
CODE_LANGUAGES = frozenset({
    "Python", "TypeScript", "TypeScript/TSX", "JavaScript", "JavaScript/JSX", "Go", "Rust", "Java",
    "Kotlin", "Scala", "Ruby", "PHP", "C#", "Swift", "C", "C/C++ header", "C++", "Objective-C",
    "Shell", "SQL", "Vue", "Svelte",
})

MANIFEST_NAMES = frozenset({
    "pyproject.toml", "setup.py", "setup.cfg", "Pipfile", "environment.yml",
    "package.json", "go.mod", "Cargo.toml", "pom.xml", "build.gradle", "build.gradle.kts",
    "settings.gradle", "settings.gradle.kts", "Gemfile", "composer.json", "CMakeLists.txt",
})
LOCKFILE_NAMES = frozenset({
    "package-lock.json", "yarn.lock", "pnpm-lock.yaml", "poetry.lock", "Pipfile.lock", "uv.lock",
    "Cargo.lock", "go.sum", "Gemfile.lock", "composer.lock",
})
CONFIG_NAMES = frozenset({
    "Makefile", "GNUmakefile", "Dockerfile", "docker-compose.yml", "docker-compose.yaml",
    "pytest.ini", "tox.ini", "mypy.ini", ".mypy.ini", ".flake8", ".pylintrc", "ruff.toml", ".ruff.toml",
    "tsconfig.json", "jsconfig.json", ".editorconfig", ".gitignore", ".gitattributes",
    ".golangci.yml", ".golangci.yaml", "clippy.toml", "rustfmt.toml", ".rustfmt.toml",
    "biome.json", ".pre-commit-config.yaml", "noxfile.py", "conftest.py",
})
CONFIG_PREFIXES = (".eslintrc", "eslint.config.", ".prettierrc", "prettier.config.", "jest.config.",
                   "vitest.config.", "babel.config.", ".babelrc", "webpack.config.", "vite.config.")
DOC_NAMES = re.compile(r"^(README|CHANGELOG|CHANGES|CONTRIBUTING|LICENSE|COPYING|NOTICE|AUTHORS)(\..*)?$",
                       re.IGNORECASE)
CI_FILES = frozenset({".gitlab-ci.yml", ".travis.yml", "azure-pipelines.yml", "Jenkinsfile",
                      "bitbucket-pipelines.yml", "appveyor.yml"})
CI_DIRS = (".github/workflows/", ".circleci/", ".buildkite/")
BINARY_EXTENSIONS = frozenset({
    ".png", ".jpg", ".jpeg", ".gif", ".bmp", ".ico", ".webp", ".pdf", ".zip", ".gz", ".tgz", ".bz2",
    ".xz", ".7z", ".tar", ".jar", ".war", ".class", ".so", ".dylib", ".dll", ".exe", ".o", ".a",
    ".pyc", ".pyo", ".whl", ".woff", ".woff2", ".ttf", ".otf", ".eot", ".mp3", ".mp4", ".mov",
    ".wav", ".sqlite", ".db", ".bin", ".pkl", ".npy", ".parquet",
})
GENERATED_SUFFIXES = (".min.js", ".min.css", ".map", "_pb2.py", "_pb2_grpc.py", ".pb.go", ".generated.ts")

TEST_DIR_NAMES = frozenset({"tests", "test", "__tests__", "spec", "specs", "testing"})

CATEGORIES = ("source", "test", "manifest", "documentation", "configuration", "ci", "generated", "other")


def language_of(path: str) -> Optional[str]:
    p = PurePosixPath(path)
    if p.name in LANGUAGES_BY_NAME:
        return LANGUAGES_BY_NAME[p.name]
    return LANGUAGES_BY_EXTENSION.get(p.suffix.lower())


def is_test_path(path: str, language: Optional[str] = None) -> bool:
    """Test file by the usual naming conventions of each ecosystem."""
    p = PurePosixPath(path)
    language = language if language is not None else language_of(path)
    if language not in CODE_LANGUAGES:
        return False
    name, stem = p.name, p.stem
    if language == "Python" and (name.startswith("test_") or stem.endswith("_test") or name == "conftest.py"):
        return True
    if ".test." in name or ".spec." in name:
        return True
    if language == "Go" and name.endswith("_test.go"):
        return True
    if language in ("Java", "Kotlin", "Scala") and re.search(r"(Test|Tests|Spec|IT)$", stem):
        return True
    return bool(TEST_DIR_NAMES.intersection(p.parts[:-1])) or "src/test/" in f"{p.parent.as_posix()}/"


def category_of(path: str, language: Optional[str] = None) -> str:
    p = PurePosixPath(path)
    name = p.name
    posix = p.as_posix()
    language = language if language is not None else language_of(path)
    if name in CI_FILES or posix.startswith(CI_DIRS):
        return "ci"
    if name in LOCKFILE_NAMES or name.endswith(GENERATED_SUFFIXES):
        return "generated"
    if p.suffix.lower() in BINARY_EXTENSIONS:
        return "other"
    if name in MANIFEST_NAMES or re.match(r"^requirements.*\.(txt|in)$", name):
        return "manifest"
    if is_test_path(posix, language):
        return "test"
    if name in CONFIG_NAMES or name.startswith(CONFIG_PREFIXES):
        return "configuration"
    if DOC_NAMES.match(name) or language in ("Markdown", "reStructuredText") or "docs" in p.parts[:-1]:
        return "documentation"
    if language in CODE_LANGUAGES:
        return "source"
    if language in ("JSON", "YAML", "TOML", "INI", "XML", "Gradle") or name.startswith("."):
        return "configuration"
    if language == "Text":
        return "documentation"
    return "other"

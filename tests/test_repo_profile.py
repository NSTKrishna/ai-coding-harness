import json
import shutil
import tempfile
import unittest
from pathlib import Path

from harness.repo import analyze_repository

from tests.repo_fixtures import GO_REPO, NON_GIT_REPO, PYTHON_REPO, TS_REPO, git_repo, write_files


def names(indicators):
    return [i.name for i in indicators]


class ProfileCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.base = Path(tmp.name).resolve()


@unittest.skipUnless(shutil.which("git"), "git not installed")
class GitProfileTest(ProfileCase):
    def test_python_repository(self):
        p = analyze_repository(git_repo(self.base / "py", PYTHON_REPO))
        self.assertTrue(p.is_git)
        self.assertEqual(p.file_count, 10)
        self.assertEqual(p.languages, (("Python", 7),))
        self.assertEqual(p.manifests, ("pyproject.toml",))
        self.assertEqual(p.source_roots, ("src",))
        self.assertEqual(p.test_roots, ("tests",))
        self.assertEqual(p.ci, (".github/workflows/ci.yml",))
        self.assertEqual(p.docs, ("README.md",))
        self.assertIn("pytest", names(p.test_frameworks))
        self.assertEqual(p.test_frameworks[0].evidence, "pyproject.toml [tool.pytest.ini_options]")
        self.assertEqual(names(p.lint_typecheck), ["ruff"])
        self.assertEqual(p.important_dirs, (("src", 5), ("tests", 2), (".github", 1)))

    def test_typescript_repository(self):
        p = analyze_repository(git_repo(self.base / "ts", TS_REPO))
        self.assertEqual(dict(p.languages), {"TypeScript": 4, "TypeScript/TSX": 1})
        self.assertIn("npm", names(p.build_systems))
        self.assertIn("vitest", names(p.test_frameworks))
        self.assertIn("TypeScript compiler", names(p.lint_typecheck))
        self.assertEqual(p.source_roots, ("src",))
        self.assertEqual(p.test_roots, ("src",))  # colocated tests

    def test_go_repository(self):
        p = analyze_repository(git_repo(self.base / "go", GO_REPO))
        self.assertEqual(p.languages, (("Go", 4),))
        self.assertIn("Go modules", names(p.build_systems))
        self.assertIn("go test", names(p.test_frameworks))
        self.assertEqual(set(p.source_roots), {"internal", "cmd"})

    def test_summary_is_deterministic_text(self):
        root = git_repo(self.base / "py", PYTHON_REPO)
        self.assertEqual(analyze_repository(root).summary(), analyze_repository(root).summary())
        self.assertIn("Test commands: python -m pytest", analyze_repository(root).summary())


class NonGitProfileTest(ProfileCase):
    def test_filesystem_fallback_profile(self):
        write_files(self.base / "plain", NON_GIT_REPO)
        p = analyze_repository(self.base / "plain")
        self.assertFalse(p.is_git)
        self.assertEqual(p.inventory_source, "filesystem")
        self.assertEqual(p.file_count, 6)
        self.assertEqual(p.source_roots, ("plain",))
        self.assertEqual(p.test_roots, ("tests",))
        self.assertEqual(set(p.manifests), {"requirements.txt", "setup.py"})
        self.assertIn("setuptools", names(p.build_systems))
        self.assertIn("unittest", names(p.test_frameworks))

    def test_config_recognition(self):
        write_files(self.base / "cfg", {
            "setup.cfg": "[flake8]\nmax-line-length = 100\n\n[mypy]\nstrict = true\n",
            "Dockerfile": "FROM python:3.12\n",
            "Cargo.toml": "[package]\nname = 'x'\n",
            "src/main.rs": "fn main() {}\n",
            "pom.xml": "<project><dependency>junit</dependency></project>\n",
            ".eslintrc.json": "{}\n",
            ".golangci.yml": "linters: {}\n",
        })
        p = analyze_repository(self.base / "cfg")
        self.assertEqual(names(p.build_systems), ["Cargo", "Maven", "Docker"])
        self.assertEqual(names(p.lint_typecheck), ["flake8", "mypy", "eslint", "golangci-lint"])
        self.assertIn("JUnit", names(p.test_frameworks))
        self.assertIn("cargo test", names(p.test_frameworks))

    def test_empty_directory(self):
        (self.base / "empty").mkdir()
        p = analyze_repository(self.base / "empty")
        self.assertEqual((p.file_count, p.languages, p.test_commands), (0, (), ()))


if __name__ == "__main__":
    unittest.main()

import json
import shutil
import tempfile
import unittest
from pathlib import Path

from harness.repo import analyze_repository

from tests.repo_fixtures import GO_REPO, PYTHON_REPO, TS_REPO, git_repo, write_files


def argvs(commands):
    return [" ".join(c.argv) for c in commands]


class CommandDiscoveryTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.base = Path(tmp.name).resolve()

    def profile(self, files, name="r"):
        write_files(self.base / name, files)
        return analyze_repository(self.base / name)

    def test_unittest(self):
        p = self.profile({"pkg/core.py": "x = 1\n", "tests/__init__.py": "",
                          "tests/test_core.py": "import unittest\n\nclass T(unittest.TestCase):\n    pass\n"})
        self.assertEqual(argvs(p.test_commands), ["python -m unittest discover -s tests -t ."])
        self.assertEqual(p.test_commands[0].confidence, "medium")
        self.assertIn("import unittest", p.test_commands[0].reason)

    def test_pytest_only_when_explicit(self):
        configured = self.profile({"pytest.ini": "[pytest]\n", "tests/test_a.py": "def test_a(): pass\n"}, "a")
        self.assertEqual(argvs(configured.test_commands), ["python -m pytest"])
        self.assertEqual(configured.test_commands[0].confidence, "high")
        referenced = self.profile({"requirements-dev.txt": "pytest==8.0\n", "tests/test_a.py": "def test_a(): pass\n"}, "b")
        self.assertEqual(argvs(referenced.test_commands), ["python -m pytest"])
        self.assertEqual(referenced.test_commands[0].confidence, "medium")
        self.assertIn("requirements-dev.txt", referenced.test_commands[0].reason)

    def test_repository_virtualenv_interpreter(self):
        p = self.profile({"pytest.ini": "[pytest]\n", ".venv/bin/python": "#!/bin/sh\n"})
        self.assertEqual(p.test_commands[0].argv, (".venv/bin/python", "-m", "pytest"))

    def test_npm_script_and_placeholder(self):
        real = self.profile({"package.json": json.dumps({"scripts": {"test": "jest", "lint": "eslint ."}}),
                             "yarn.lock": ""}, "a")
        self.assertEqual(argvs(real.test_commands), ["yarn test"])
        self.assertEqual(argvs(real.build_commands), ["yarn run lint"])
        placeholder = self.profile({"package.json": json.dumps(
            {"scripts": {"test": 'echo "Error: no test specified" && exit 1'}})}, "b")
        self.assertEqual(placeholder.test_commands, ())

    def test_go_and_cargo(self):
        go = self.profile({"go.mod": "module x\n", "a.go": "package a\n", "a_test.go": "package a\n"}, "go")
        self.assertEqual(argvs(go.test_commands), ["go test ./..."])
        self.assertEqual(argvs(go.build_commands), ["go build ./..."])
        cargo = self.profile({"Cargo.toml": "[package]\nname='x'\n", "src/lib.rs": ""}, "rs")
        self.assertEqual(argvs(cargo.test_commands), ["cargo test"])

    def test_makefile_targets(self):
        p = self.profile({"Makefile": ".PHONY: test\ntest:\n\tpytest\nlint:\n\truff .\nbuild: deps\n\tx\nVAR := 1\n"})
        self.assertEqual(argvs(p.test_commands), ["make test"])
        self.assertEqual(argvs(p.build_commands), ["make lint", "make build"])  # Makefile order

    def test_uncertain_repositories_get_no_command(self):
        cases = {
            "plain_python": {"app.py": "print(1)\n"},                       # code but no tests or config
            "tests_without_framework": {"tests/test_x.py": "def test_x():\n    assert True\n"},
            "js_without_scripts": {"package.json": "{}", "index.js": ""},
            "go_without_tests": {"main.go": "package main\n"},              # no go.mod
        }
        for name, files in cases.items():
            with self.subTest(case=name):
                self.assertEqual(self.profile(files, name).test_commands, ())

    def test_lint_and_typecheck_from_config(self):
        p = self.profile({"pyproject.toml": "[tool.ruff]\n[tool.mypy]\nstrict = true\n", "a.py": ""})
        self.assertEqual(argvs(p.build_commands), ["ruff check .", "mypy ."])
        self.assertTrue(all(c.reason for c in p.build_commands))

    @unittest.skipUnless(shutil.which("git"), "git not installed")
    def test_fixture_commands(self):
        for fixture, tests, builds in (
            (PYTHON_REPO, ["python -m pytest"], ["ruff check ."]),
            (TS_REPO, ["npm test"], ["npm run build", "npm run lint", "npx tsc --noEmit"]),
            (GO_REPO, ["go test ./..."], ["go build ./..."]),
        ):
            p = analyze_repository(git_repo(self.base / f"g{len(fixture)}", fixture))
            with self.subTest(tests=tests):
                self.assertEqual(argvs(p.test_commands), tests)
                self.assertEqual(argvs(p.build_commands), builds)


if __name__ == "__main__":
    unittest.main()

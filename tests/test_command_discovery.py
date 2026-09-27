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

    # CI-derived commands (what the maintainers actually keep green)
    # ----------------------------------------------------------------
    GO_FILES = {"go.mod": "module x\n", "a.go": "package a\n", "a_test.go": "package a\n"}

    def workflow(self, body, name="r"):
        return self.profile({**self.GO_FILES, ".github/workflows/ci.yml": body}, name)

    def test_ci_command_beats_the_template_and_loses_ci_only_flags(self):
        p = self.workflow("jobs:\n  t:\n    steps:\n      - name: Unit\n        run: go test -json --short "
                          "./server/... -race -coverprofile=c.txt -covermode=atomic > out.json\n")
        self.assertEqual(argvs(p.test_commands)[0], "go test --short ./server/...")
        self.assertEqual(p.test_commands[0].confidence, "high")
        self.assertIn(".github/workflows/ci.yml", p.test_commands[0].reason)
        self.assertIn("go test -short ./...", argvs(p.test_commands))   # template kept as a fallback

    def test_ci_block_scalar_and_pipe(self):
        p = self.workflow("jobs:\n  t:\n    steps:\n      - run: |\n          set +e\n"
                          "          go test --short ./mesheryctl/... | tee r.txt\n")
        self.assertEqual(argvs(p.test_commands)[0], "go test --short ./mesheryctl/...")

    def test_ci_steps_that_run_elsewhere_are_refused(self):
        """A CommandCandidate has no working directory, so a relocated command must not be adopted."""
        cd_in_block = self.workflow("jobs:\n  t:\n    steps:\n      - run: |\n          cd server/policies\n"
                                    "          go test -v ./... | tee r.txt\n", "a")
        working_dir = self.workflow("jobs:\n  t:\n    steps:\n      - name: Unit\n"
                                    "        working-directory: ./mesheryctl\n        run: go test ./...\n", "b")
        inline_cd = self.workflow("jobs:\n  t:\n    steps:\n      - run: cd sub && go test ./...\n", "c")
        for p in (cd_in_block, working_dir, inline_cd):
            self.assertEqual(argvs(p.test_commands), ["go test -short ./..."])   # template only

    def test_ci_lines_that_cannot_be_resolved_are_skipped(self):
        expression = self.workflow("jobs:\n  t:\n    steps:\n      - run: go test ${{ matrix.flags }} ./...\n", "a")
        not_a_command = self.workflow("jobs:\n  t:\n    steps:\n      - name: go test everything\n"
                                      "        run: go build ./...\n", "b")
        for p in (expression, not_a_command):
            self.assertEqual(argvs(p.test_commands), ["go test -short ./..."])

    def test_ci_commands_are_deduplicated_and_ordered(self):
        p = self.workflow("jobs:\n  t:\n    steps:\n      - run: go test --short ./server/...\n"
                          "      - run: go test --short ./mesheryctl/...\n"
                          "      - run: go test --short ./server/...\n")
        self.assertEqual(argvs(p.test_commands)[:2],
                         ["go test --short ./server/...", "go test --short ./mesheryctl/..."])
        self.assertEqual(len(argvs(p.test_commands)), 3)          # + the template fallback

    def test_workflows_with_test_like_names_are_read_first(self):
        """A repository can hold dozens of workflows; an alphabetical cut must not hide the test one."""
        files = {**self.GO_FILES}
        for i in range(25):
            files[f".github/workflows/aaa-{i:02d}.yml"] = "jobs:\n  x:\n    steps:\n      - run: echo hi\n"
        files[".github/workflows/go-testing-ci.yml"] = ("jobs:\n  t:\n    steps:\n"
                                                        "      - run: go test --short ./server/...\n")
        p = self.profile(files, "many")
        self.assertEqual(argvs(p.test_commands)[0], "go test --short ./server/...")

    def test_go_and_cargo(self):
        go = self.profile({"go.mod": "module x\n", "a.go": "package a\n", "a_test.go": "package a\n"}, "go")
        self.assertEqual(argvs(go.test_commands), ["go test -short ./..."])
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
            (GO_REPO, ["go test -short ./..."], ["go build ./..."]),
        ):
            p = analyze_repository(git_repo(self.base / f"g{len(fixture)}", fixture))
            with self.subTest(tests=tests):
                self.assertEqual(argvs(p.test_commands), tests)
                self.assertEqual(argvs(p.build_commands), builds)


if __name__ == "__main__":
    unittest.main()


class SetupCommandTest(unittest.TestCase):
    """Dependencies the repository needs before any of its own commands can run.

    Without this, every script in package.json exits 127 ("command not found") and the
    whole run is spent on an environment problem instead of the task.
    """

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.base = Path(tmp.name).resolve()
        self.count = 0

    def setup_for(self, files, installed=False):
        self.count += 1
        root = self.base / f"r{self.count}"
        write_files(root, {"package.json": json.dumps({"scripts": {"build": "next build"}}), **files})
        if installed:
            (root / "node_modules" / ".bin").mkdir(parents=True)
        return [" ".join(c.argv) for c in analyze_repository(root).setup_commands]

    def test_the_lockfile_chooses_the_install(self):
        self.assertEqual(self.setup_for({"package-lock.json": "{}"}), ["npm ci"])
        self.assertEqual(self.setup_for({"yarn.lock": ""}), ["yarn install --frozen-lockfile"])
        self.assertEqual(self.setup_for({"pnpm-lock.yaml": ""}), ["pnpm install --frozen-lockfile"])
        self.assertEqual(self.setup_for({}), ["npm install"])          # no lockfile: cannot use ci

    def test_pnpm_and_yarn_win_over_a_stray_package_lock(self):
        self.assertEqual(self.setup_for({"pnpm-lock.yaml": "", "package-lock.json": "{}"}),
                         ["pnpm install --frozen-lockfile"])

    def test_nothing_to_do_when_the_dependencies_are_already_installed(self):
        for files in ({"package-lock.json": "{}"}, {"yarn.lock": ""}, {}):
            self.assertEqual(self.setup_for(files, installed=True), [])

    def test_a_repository_without_package_json_needs_no_setup(self):
        self.count += 1
        root = self.base / f"r{self.count}"
        write_files(root, {"go.mod": "module x\n", "a_test.go": "package a\n"})
        self.assertEqual(analyze_repository(root).setup_commands, ())

    def test_the_planner_is_told_setup_is_needed(self):
        self.count += 1
        root = self.base / f"r{self.count}"
        write_files(root, {"package.json": json.dumps({"scripts": {"build": "next build"}}),
                           "package-lock.json": "{}"})
        self.assertIn("Setup needed first: npm ci", analyze_repository(root).summary())

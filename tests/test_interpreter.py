import os
import sys
import tempfile
import unittest
from pathlib import Path

from harness.orchestrator.interpreter import resolve_command, resolve_python
from harness.repo.commands import CommandCandidate


def cmd(*argv):
    return CommandCandidate("test", tuple(argv), "medium", "evidence")


class InterpreterResolutionTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()

    def make_venv(self, rel=".venv/bin/python"):
        path = self.root / rel
        path.parent.mkdir(parents=True)
        path.write_text("#!/bin/sh\n")
        os.chmod(path, 0o755)

    def test_repository_virtualenv_first(self):
        self.make_venv()
        resolved = resolve_command(cmd("python", "-m", "pytest"), self.root, executable="/opt/py")
        self.assertEqual(resolved.argv, (".venv/bin/python", "-m", "pytest"))
        self.assertIn("repository virtualenv", resolved.reason)

    def test_venv_directory_without_dot(self):
        self.make_venv("venv/bin/python")
        self.assertEqual(resolve_python(self.root, executable="/opt/py").path, "venv/bin/python")

    def test_harness_interpreter_fallback(self):
        resolved = resolve_command(cmd("python", "-m", "unittest", "discover"), self.root)
        self.assertEqual(resolved.argv, (sys.executable, "-m", "unittest", "discover"))
        self.assertIn("harness interpreter", resolved.reason)

    def test_path_fallback_when_no_harness_interpreter(self):
        found = {"python3": "/usr/bin/python3"}
        interpreter = resolve_python(self.root, executable="", which=found.get)
        self.assertEqual((interpreter.path, interpreter.source), ("/usr/bin/python3", "PATH"))

    def test_nothing_found_leaves_command_and_says_so(self):
        resolved = resolve_command(cmd("python", "-m", "pytest"), self.root, executable="", which=lambda _: None)
        self.assertEqual(resolved.argv, ("python", "-m", "pytest"))
        self.assertIn("no Python interpreter could be resolved", resolved.reason)

    def test_never_assumes_bare_python(self):
        for program in ("python", "python3"):
            with self.subTest(program=program):
                resolved = resolve_command(cmd(program, "-m", "pytest"), self.root, executable="/opt/py")
                self.assertEqual(resolved.argv[0], "/opt/py")

    def test_third_party_runner_prefers_an_interpreter_that_can_import_it(self):
        found = {"python3": "/usr/bin/python3", "python": "/usr/bin/python"}
        probed = []

        def probe(path, module):
            probed.append((path, module))
            return path == "/usr/bin/python3"

        resolved = resolve_command(cmd("python", "-m", "pytest", "-q"), self.root, executable="/opt/harness-venv/python",
                                   which=found.get, probe=probe)
        self.assertEqual(resolved.argv, ("/usr/bin/python3", "-m", "pytest", "-q"))
        self.assertIn("can import pytest", resolved.reason)
        self.assertEqual(probed, [("/opt/harness-venv/python", "pytest"), ("/usr/bin/python3", "pytest")])

    def test_no_interpreter_can_import_the_runner_keeps_the_harness_interpreter(self):
        resolved = resolve_command(cmd("python", "-m", "pytest"), self.root, executable="/opt/py",
                                   which={"python3": "/usr/bin/python3"}.get, probe=lambda p, m: False)
        self.assertEqual(resolved.argv[0], "/opt/py")   # verification then reports ENVIRONMENT_ERROR precisely

    def test_stdlib_runner_and_repository_venv_are_not_probed(self):
        def probe(path, module):
            raise AssertionError("must not probe")

        self.assertEqual(resolve_command(cmd("python", "-m", "unittest"), self.root, executable="/opt/py",
                                         probe=probe).argv[0], "/opt/py")
        self.make_venv()
        self.assertEqual(resolve_command(cmd("python", "-m", "pytest"), self.root, probe=probe).argv[0],
                         ".venv/bin/python")

    def test_real_probe(self):
        from harness.orchestrator.interpreter import can_import
        self.assertTrue(can_import(sys.executable, "json"))
        self.assertFalse(can_import(sys.executable, "no_such_module_xyz_123"))
        self.assertFalse(can_import("/nonexistent/python", "json"))

    def test_other_commands_untouched(self):
        for argv in (("npm", "test"), ("make", "test"), ("go", "test", "./..."), ("pythonic-tool", "run")):
            with self.subTest(argv=argv):
                original = cmd(*argv)
                self.assertIs(resolve_command(original, self.root, executable="/opt/py"), original)

    def test_already_resolved_venv_command_unchanged(self):
        self.make_venv()
        original = cmd(".venv/bin/python", "-m", "pytest")
        self.assertIs(resolve_command(original, self.root, executable="/opt/py"), original)


if __name__ == "__main__":
    unittest.main()

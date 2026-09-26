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

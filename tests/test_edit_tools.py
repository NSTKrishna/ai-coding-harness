"""edit_file / write_file: exact replacement and whole-file writes, same PatchResult as apply_patch."""

import hashlib
import sys
import unittest

from harness.config import Limits
from harness.model import ScriptedModel
from harness.orchestrator import Orchestrator, Phase
from harness.tools import ToolContext, build_registry
from harness.tools.patch import PatchResult

from tests.helpers import RepoTestCase
from tests.orchestration_helpers import TASK, buggy_repo, complete, plan_response, tool

CALC = "def add(a, b):\n    return a - b\n\n\ndef mul(a, b):\n    return a * b\n"


def sha(text):
    return hashlib.sha256(text.encode()).hexdigest()


class EditFileTest(RepoTestCase):
    def setUp(self):
        super().setUp()
        self.write("calc.py", CALC)
        self.registry = build_registry(ToolContext.create(self.repo))

    def edit(self, **args):
        return self.registry.dispatch("edit_file", {"path": "calc.py", **args})

    def read(self, rel="calc.py"):
        return (self.repo / rel).read_text()

    def test_exact_unique_replacement(self):
        result = self.edit(old_text="return a - b", new_text="return a + b")
        self.assertTrue(result.success, result.error)
        self.assertEqual(self.read(), CALC.replace("a - b", "a + b"))
        self.assertIsInstance(result.data, PatchResult)
        (f,) = result.data.files
        self.assertEqual((f.path, f.action, f.added, f.removed), ("calc.py", "modified", 1, 1))
        self.assertEqual((f.before_sha256, f.after_sha256), (sha(CALC), sha(self.read())))

    def test_not_found_and_ambiguous_change_nothing(self):
        missing = self.edit(old_text="return a / b", new_text="x")
        self.assertEqual(missing.error.code, "edit_mismatch")
        ambiguous = self.edit(old_text="(a, b):", new_text="(a, b, c):")
        self.assertEqual(ambiguous.error.code, "edit_ambiguous")
        self.assertIn("2 times", ambiguous.error.message)
        self.assertEqual(self.read(), CALC)

    def test_indentation_hint_and_replace_all(self):
        hint = self.edit(old_text="return a - b\n", new_text="return 0\n")   # missing indentation still found?
        self.assertTrue(hint.success)   # "return a - b\n" is a substring of "    return a - b\n"
        self.write("calc.py", CALC)
        wrong_indent = self.edit(old_text="  def mul(a, b):", new_text="def mul(a, b, c):")
        self.assertEqual(wrong_indent.error.code, "edit_mismatch")
        self.assertIn("line(s) [5]", wrong_indent.error.message)
        every = self.edit(old_text="(a, b)", new_text="(x, y)", replace_all=True)
        self.assertTrue(every.success)
        self.assertEqual(self.read().count("(x, y)"), 2)

    def test_crlf_file(self):
        self.write("win.py", "a = 1\r\nb = 2\r\n")
        result = self.registry.dispatch("edit_file", {"path": "win.py", "old_text": "a = 1\nb = 2", "new_text": "a = 3\nb = 4"})
        self.assertTrue(result.success, result.error)
        self.assertEqual((self.repo / "win.py").read_bytes(), b"a = 3\r\nb = 4\r\n")

    def test_path_boundary(self):
        result = self.registry.dispatch("edit_file", {"path": "../outside.py", "old_text": "a", "new_text": "b"})
        self.assertFalse(result.success)


class WriteFileTest(RepoTestCase):
    def setUp(self):
        super().setUp()
        self.write("calc.py", CALC)
        self.registry = build_registry(ToolContext.create(self.repo))

    def test_create_and_overwrite(self):
        created = self.registry.dispatch("write_file", {"path": "pkg/new.py", "content": "X = 1"})
        self.assertTrue(created.success, created.error)
        self.assertEqual((self.repo / "pkg/new.py").read_text(), "X = 1\n")
        (f,) = created.data.files
        self.assertEqual((f.action, f.before_sha256), ("created", None))
        replaced = self.registry.dispatch("write_file", {"path": "calc.py", "content": "Y = 2\n"})
        self.assertEqual(replaced.data.files[0].action, "modified")
        self.assertEqual(replaced.data.files[0].before_sha256, sha(CALC))
        same = self.registry.dispatch("write_file", {"path": "calc.py", "content": "Y = 2\n"})
        self.assertFalse(same.success)

    def test_path_boundary(self):
        self.assertFalse(self.registry.dispatch("write_file", {"path": "/tmp/x.py", "content": "a"}).success)
        self.assertFalse(self.registry.dispatch("write_file", {"path": ".git/config", "content": "a"}).success)


class EditToolsInARunTest(unittest.TestCase):
    """Edits made with edit_file are verified and ledgered exactly like apply_patch edits."""

    def run_script(self, *script):
        import tempfile
        from pathlib import Path
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = buggy_repo(Path(tmp.name).resolve())
        state = Orchestrator(ScriptedModel(list(script)), limits=Limits(max_repair_cycles=0)).run(root, TASK)
        return root, state

    def test_edit_file_fix_is_verified(self):
        root, state = self.run_script(plan_response(),
                                      tool("edit_file", path="src/math_utils.py", old_text="return x + 2",
                                           new_text="return x + 1"), complete())
        self.assertEqual(state.phase, Phase.VERIFIED, state.terminal_reason)
        self.assertEqual(state.last_report.changed_by_run, ("src/math_utils.py",))

    def test_rewriting_the_test_with_write_file_is_tampering(self):
        root, state = self.run_script(plan_response(),
                                      tool("write_file", path="tests/test_math_utils.py",
                                           content="import unittest\n\n\nclass T(unittest.TestCase):\n"
                                                   "    def test_add_one(self):\n        pass\n"), complete())
        self.assertNotEqual(state.phase, Phase.VERIFIED)
        self.assertEqual(state.last_report.tampered_tests, ("tests/test_math_utils.py",))


if __name__ == "__main__":
    unittest.main()

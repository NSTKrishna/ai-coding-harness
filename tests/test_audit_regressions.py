"""Adversarial regression tests for the AUDIT.md findings (B1-B4, H3, M2).

Each scenario reproduces a false VERIFIED (or a false failure) that the audit
demonstrated, and pins the corrected behaviour. They also check that the
legitimate variant still verifies, so the fix is not "never verify".
"""

import os
import shutil
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from harness.config import Limits
from harness.model import ScriptedModel
from harness.orchestrator import Orchestrator, Phase

from tests.orchestration_helpers import (
    BUGGY_REPO,
    FIX_PATCH,
    NO_COMMAND_REPO,
    TASK,
    complete,
    plan_response,
    tool,
)
from tests.repo_fixtures import git_repo

EDIT_TEST_PATCH = (
    "--- a/tests/test_math_utils.py\n+++ b/tests/test_math_utils.py\n@@ -7,2 +7,2 @@\n"
    "     def test_add_one(self):\n-        self.assertEqual(add_one(1), 2)\n+        self.assertEqual(add_one(1), 3)\n")
DELETE_TEST_PATCH = "--- a/tests/test_math_utils.py\n+++ /dev/null\n@@ -1,8 +0,0 @@\n" + "".join(
    "-" + line + "\n" for line in BUGGY_REPO["tests/test_math_utils.py"].rstrip("\n").split("\n"))
NEW_TEST_FILE_PATCH = (
    "--- /dev/null\n+++ b/tests/test_more.py\n@@ -0,0 +1,7 @@\n+import unittest\n+\n"
    "+from src.math_utils import add_one\n+\n+\n+class MoreTest(unittest.TestCase):\n"
    "+    def test_add_one_zero(self):\n+        self.assertEqual(add_one(0), 1)\n")


@unittest.skipUnless(shutil.which("git"), "git not installed")
class AuditScenarioCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.base = Path(tmp.name).resolve()
        self.count = 0

    def run_scenario(self, files, script, limits=None, prep=None):
        self.count += 1
        root = git_repo(self.base / f"r{self.count}", files)
        if prep:
            prep(root)
        state = Orchestrator(ScriptedModel(script), limits=limits or Limits()).run(root, TASK)
        return root, state

    def assertNotVerified(self, state):
        self.assertNotEqual(state.phase, Phase.VERIFIED, state.terminal_reason)
        for report in state.verification_reports:
            self.assertNotIn("PASS", [c.status for c in report.criteria_results
                                      if "structural" not in c.notes], report.criteria_results)


class B1EditedTestTest(AuditScenarioCase):
    def test_editing_the_failing_test_is_not_verified(self):
        root, state = self.run_scenario(BUGGY_REPO, [plan_response(), tool("apply_patch", patch=EDIT_TEST_PATCH),
                                                     complete()], limits=Limits(max_repair_cycles=0))
        self.assertNotVerified(state)
        report = state.last_report
        self.assertEqual(report.tampered_tests, ("tests/test_math_utils.py",))
        self.assertTrue(any("modified or deleted pre-existing test file" in r for r in report.risks))
        self.assertTrue(all(c.positive != "strong" for c in report.command_results))

    def test_a_new_test_file_with_a_real_fix_still_verifies(self):
        root, state = self.run_scenario(BUGGY_REPO, [plan_response(), tool("apply_patch", patch=FIX_PATCH),
                                                     tool("apply_patch", patch=NEW_TEST_FILE_PATCH), complete()])
        self.assertEqual(state.phase, Phase.VERIFIED, state.terminal_reason)
        self.assertEqual(state.last_report.tampered_tests, ())


class B2DeletedTestTest(AuditScenarioCase):
    def test_deleting_the_failing_test_is_not_verified(self):
        _, state = self.run_scenario(BUGGY_REPO, [plan_response(), tool("apply_patch", patch=DELETE_TEST_PATCH),
                                                  complete()],
                                     limits=Limits(targeted_tests=False, max_repair_cycles=0))
        self.assertNotVerified(state)

    @unittest.skipUnless(shutil.which("python3.11") or shutil.which("python3.10"), "needs Python <= 3.11")
    def test_deleting_the_failing_test_with_an_interpreter_that_exits_0(self):
        # unittest on <= 3.11 reports "Ran 0 tests ... OK" with exit 0 when the only test file is gone
        interpreter = shutil.which("python3.11") or shutil.which("python3.10")

        def venv(root):
            (root / ".venv" / "bin").mkdir(parents=True)
            os.symlink(interpreter, root / ".venv" / "bin" / "python")

        _, state = self.run_scenario(BUGGY_REPO, [plan_response(), tool("apply_patch", patch=DELETE_TEST_PATCH),
                                                  complete()],
                                     limits=Limits(targeted_tests=False, max_repair_cycles=0), prep=venv)
        self.assertNotVerified(state)

    def test_deleting_a_test_file_by_command_is_detected(self):
        remove = [sys.executable, "-c", "import os; os.remove('tests/test_math_utils.py')"]
        _, state = self.run_scenario(BUGGY_REPO, [plan_response(), tool("run_command", command=remove), complete()],
                                     limits=Limits(targeted_tests=False, max_repair_cycles=0))
        self.assertNotVerified(state)
        self.assertEqual(state.last_report.tampered_tests, ("tests/test_math_utils.py",))

    def test_fewer_tests_than_baseline_is_not_strong(self):
        from harness.verify.engine import VerificationEngine
        from tests.test_verification_outcomes import PY_TEST, run
        from harness.verify.outcomes import classify
        from harness.verify.engine import CommandRun
        from harness.verify.commands import VerificationCommand
        cmd = VerificationCommand("V1", "test", tuple(PY_TEST), "suite", "discovered", True)
        base = CommandRun(cmd, "baseline", classify("test", PY_TEST, run(1, stderr="FAIL: test_a (t.T)\nRan 3 tests")), "E1")
        post = CommandRun(cmd, "post-1", classify("test", PY_TEST, run(0, stderr="Ran 1 test\nOK")), "E2")
        self.assertIn("fewer tests ran", VerificationEngine._strength_caveat(base, post, ()))


class B3StructuralTest(AuditScenarioCase):
    def test_structural_criteria_without_a_change_are_not_verified(self):
        _, state = self.run_scenario(NO_COMMAND_REPO, [
            plan_response(acceptance_criteria=["src/math_utils.py exists"],
                          steps=[{"kind": "inspect", "description": "Look"}]),
            complete()])
        self.assertNotVerified(state)
        self.assertEqual([c.status for c in state.last_report.criteria_results], ["UNKNOWN"])

    def test_structural_criteria_for_a_created_file_verify(self):
        create = "--- /dev/null\n+++ b/src/extra.py\n@@ -0,0 +1 @@\n+VALUE = 1\n"
        _, state = self.run_scenario(NO_COMMAND_REPO, [
            plan_response(acceptance_criteria=["src/extra.py exists"]),
            tool("apply_patch", patch=create), complete()])
        self.assertEqual(state.phase, Phase.VERIFIED, state.terminal_reason)


class B4UnrelatedCheckTest(AuditScenarioCase):
    def test_unrelated_lint_fix_does_not_verify_the_task(self):
        files = {k: v for k, v in BUGGY_REPO.items() if k != "tests/__init__.py"}
        files["Makefile"] = ("lint:\n\t@python3 -c \"import sys; "
                             "sys.exit('TODO' in open('src/math_utils.py').read())\"\n")
        files["src/math_utils.py"] = "# TODO tidy\n" + files["src/math_utils.py"]
        lint_only = "--- a/src/math_utils.py\n+++ b/src/math_utils.py\n@@ -1,2 +1 @@\n-# TODO tidy\n def add_one(x):\n"
        _, state = self.run_scenario(files, [
            plan_response(acceptance_criteria=["the increment helper returns the next integer"]),
            tool("apply_patch", patch=lint_only), complete()], limits=Limits(max_repair_cycles=0))
        self.assertNotVerified(state)
        checks = [c for c in state.last_report.command_results if c.command.kind != "test"]
        self.assertTrue(checks and all(c.positive != "strong" for c in checks))


class H3PreexistingFailureTest(AuditScenarioCase):
    def test_correct_fix_is_not_blamed_for_a_preexisting_failure_sharing_a_name(self):
        files = dict(BUGGY_REPO)
        files["tests/test_math_utils.py"] = BUGGY_REPO["tests/test_math_utils.py"] + (
            "\n    def test_add_one_logs_nothing(self):\n        self.assertTrue(False, 'pre-existing, unrelated')\n")
        _, state = self.run_scenario(files, [plan_response(), tool("apply_patch", patch=FIX_PATCH), complete()],
                                     limits=Limits(max_repair_cycles=0))
        self.assertEqual(state.phase, Phase.VERIFIED, state.terminal_reason)
        self.assertEqual([c.status for c in state.last_report.criteria_results], ["PASS"])

    def test_task_test_still_failing_as_before_fails_the_criterion(self):
        noop = "--- a/src/math_utils.py\n+++ b/src/math_utils.py\n@@ -4,2 +4,2 @@\n \n-def double(x):\n+def double(x):  # touched\n"
        _, state = self.run_scenario(BUGGY_REPO, [plan_response(), tool("apply_patch", patch=noop), complete()],
                                     limits=replace(Limits(), max_repair_cycles=0))
        self.assertNotEqual(state.phase, Phase.VERIFIED)
        self.assertEqual([c.status for c in state.last_report.criteria_results], ["FAIL"])


class M2TimeoutBaselineTest(unittest.TestCase):
    def test_timeout_then_pass_is_weak(self):
        from harness.verify.engine import CommandRun, VerificationEngine
        from harness.verify.commands import VerificationCommand
        from harness.verify.outcomes import Classification, CommandStatus
        cmd = VerificationCommand("V1", "test", ("x",), "suite", "discovered", True)
        base = CommandRun(cmd, "baseline", Classification(CommandStatus.TIMEOUT, None, None, "", "timed out"), "E1")
        post = CommandRun(cmd, "post-1", Classification(CommandStatus.PASS, 0, None, "", "exit code 0"), "E2")
        self.assertIn("timed out", VerificationEngine._strength_caveat(base, post, ()))


if __name__ == "__main__":
    unittest.main()

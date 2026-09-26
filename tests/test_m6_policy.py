"""M6: repeated-failure / no-progress stop rules and the stricter VERIFIED policy."""

import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from harness.config import Limits
from harness.model import ScriptedModel
from harness.orchestrator import Orchestrator, Phase
from harness.verify import Comparison, FailureClass, Verdict

from tests.orchestration_helpers import (
    BUGGY_REPO, FIX_PATCH, NO_COMMAND_REPO, PASSING_REPO, REPAIR_PATCH, SECOND_WRONG_PATCH, TASK, UNITTEST_CMD,
    WRONG_PATCH, complete, plan_response, tool,
)
from tests.repo_fixtures import git_repo

SEL = dict(verification_candidates=[UNITTEST_CMD])
THIRD_WRONG_PATCH = ("--- a/src/math_utils.py\n+++ b/src/math_utils.py\n"
                     "@@ -1,2 +1,2 @@\n def add_one(x):\n-    return x + 4\n+    return x + 5\n")
TWO_TEST_REPO = {**BUGGY_REPO, "tests/test_math_utils.py": (
    "import unittest\n\nfrom src.math_utils import add_one, double\n\n\n"
    "class MathTest(unittest.TestCase):\n"
    "    def test_add_one(self):\n        self.assertEqual(add_one(1), 2)\n\n"
    "    def test_double(self):\n        self.assertEqual(double(3), 6)\n")}
FIX_ONE_BREAK_DOUBLE = ("--- a/src/math_utils.py\n+++ b/src/math_utils.py\n"
                        "@@ -1,6 +1,6 @@\n def add_one(x):\n-    return x + 3\n+    return x + 1\n \n \n"
                        " def double(x):\n-    return x * 2\n+    return x * 3\n")
FIX_DOUBLE = ("--- a/src/math_utils.py\n+++ b/src/math_utils.py\n"
              "@@ -5,2 +5,2 @@\n def double(x):\n-    return x * 3\n+    return x * 2\n")
DOCSTRING_PATCH = ("--- a/src/math_utils.py\n+++ b/src/math_utils.py\n"
                   "@@ -5,2 +5,3 @@\n def double(x):\n+    \"\"\"Return twice x.\"\"\"\n     return x * 2\n")
NEW_TEST_PATCH = ("--- /dev/null\n+++ b/tests/test_extra.py\n@@ -0,0 +1,7 @@\n+import unittest\n+\n"
                  "+from src.math_utils import double\n+\n+\n+class ExtraTest(unittest.TestCase):\n"
                  "+    def test_double_zero(self):\n+        self.assertEqual(double(0), 0)\n")


@unittest.skipUnless(shutil.which("git"), "git not installed")
class PolicyCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.base = Path(tmp.name).resolve()
        patcher = mock.patch.dict(os.environ, {}, clear=False)
        patcher.start()
        self.addCleanup(patcher.stop)
        os.environ.pop("AI_API_KEY", None)

    def run_task(self, files, *script, limits=None, task=TASK):
        self.root = git_repo(self.base / "repo", files)
        self.model = ScriptedModel(list(script))
        self.state = Orchestrator(self.model, limits=limits).run(self.root, task)
        return self.state


class NoProgressTest(PolicyCase):
    def test_repair_without_change_stops_immediately(self):
        spare = [tool("apply_patch", patch=REPAIR_PATCH), complete()] * 3
        state = self.run_task(BUGGY_REPO, plan_response(**SEL), tool("apply_patch", patch=WRONG_PATCH), complete(),
                              complete("tried again"), *spare, limits=Limits(max_repair_cycles=5))
        self.assertEqual(state.phase, Phase.UNVERIFIED)
        self.assertEqual(state.failure.kind, "repeated_failure_no_progress")
        self.assertTrue(state.terminal_reason.startswith("repeated_failure_no_progress"))
        self.assertTrue(state.no_progress)
        self.assertEqual(state.failure.details["no_progress"], True)
        self.assertEqual((state.repair_cycles, state.model_calls), (1, 4))    # no model call after the decision
        self.assertEqual(self.model.remaining, len(spare))

    def test_same_failure_after_repeated_repairs_stops_before_repair_budget(self):
        spare = [tool("apply_patch", patch=REPAIR_PATCH), complete()] * 3
        state = self.run_task(BUGGY_REPO, plan_response(**SEL), tool("apply_patch", patch=WRONG_PATCH), complete(),
                              tool("apply_patch", patch=SECOND_WRONG_PATCH), complete(),
                              tool("apply_patch", patch=THIRD_WRONG_PATCH), complete(), *spare,
                              limits=Limits(max_repair_cycles=5, max_repeated_failure_cycles=2))
        self.assertEqual(state.phase, Phase.UNVERIFIED)
        self.assertEqual(state.failure.kind, "repeated_failure_no_progress")
        self.assertEqual((state.repeated_failures, state.no_progress), (2, False))
        self.assertEqual((state.repair_cycles, state.model_calls, len(state.verification_reports)), (2, 7, 3))
        self.assertEqual(self.model.remaining, len(spare))                      # 3 repair cycles never started

    def test_a_different_failure_resets_the_count(self):
        state = self.run_task(TWO_TEST_REPO, plan_response(**SEL), tool("apply_patch", patch=WRONG_PATCH), complete(),
                              tool("apply_patch", patch=FIX_ONE_BREAK_DOUBLE), complete(),
                              tool("apply_patch", patch=FIX_DOUBLE), complete(),
                              limits=Limits(max_repair_cycles=3, max_repeated_failure_cycles=1))
        verdicts = [(r.verdict, r.failure_class) for r in state.verification_reports]
        self.assertEqual(verdicts, [(Verdict.NEEDS_REPAIR, FailureClass.TASK_TEST_FAILURE),
                                    (Verdict.NEEDS_REPAIR, FailureClass.REGRESSION), (Verdict.VERIFIED, None)])
        self.assertEqual(state.phase, Phase.VERIFIED)
        self.assertEqual(state.repeated_failures, 0)

    def test_identical_executor_actions_are_stopped(self):
        state = self.run_task(BUGGY_REPO, plan_response(), *[tool("read_file", path="src/math_utils.py")] * 6)
        self.assertEqual(state.phase, Phase.BLOCKED)
        self.assertEqual(state.failure.kind, "no_progress")
        self.assertEqual(state.failure.details["repeats"], 4)
        self.assertEqual((state.steps, state.model_calls), (4, 5))


class EvidencePolicyTest(PolicyCase):
    def test_unchanged_pass_alone_is_not_verified(self):
        state = self.run_task(PASSING_REPO, plan_response(**SEL, acceptance_criteria=["double has a docstring"]),
                              tool("apply_patch", patch=DOCSTRING_PATCH), complete(),
                              task="Add a docstring to double.")
        report = state.last_report
        self.assertEqual({r.comparison for r in report.command_results}, {Comparison.UNCHANGED_PASS})
        self.assertEqual((state.phase, report.verdict), (Phase.UNVERIFIED, Verdict.UNVERIFIED))
        self.assertIn("only weak evidence", report.summary)

    def test_fail_to_pass_is_verified(self):
        state = self.run_task(BUGGY_REPO, plan_response(), tool("apply_patch", patch=FIX_PATCH), complete())
        self.assertEqual(state.phase, Phase.VERIFIED)
        self.assertTrue(all(r.positive == "strong" for r in state.last_report.command_results))

    def test_new_passing_test_alone_is_weak(self):
        state = self.run_task(PASSING_REPO, plan_response(**SEL), tool("apply_patch", patch=NEW_TEST_PATCH), complete(),
                              task="Add a regression test for double(0).")
        suite = next(r for r in state.last_report.command_results if r.command.level == "suite")
        self.assertEqual(suite.positive, "weak")
        self.assertIn("failure before the change was not observed", suite.note)
        self.assertEqual(state.phase, Phase.UNVERIFIED)

    def test_structural_task_is_verified_by_file_evidence(self):
        patch = "--- /dev/null\n+++ b/config/settings.toml\n@@ -0,0 +1,2 @@\n+[app]\n+debug = false\n"
        state = self.run_task(NO_COMMAND_REPO, plan_response(acceptance_criteria=["config/settings.toml exists"]),
                              tool("apply_patch", patch=patch), complete(), task="Create config/settings.toml.")
        self.assertEqual(state.phase, Phase.VERIFIED)
        self.assertIn("structural evidence", state.last_report.summary)
        (criterion,) = state.last_report.criteria_results
        self.assertEqual(criterion.status, "PASS")


if __name__ == "__main__":
    unittest.main()

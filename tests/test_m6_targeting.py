"""M6 targeted test selection: derivation per framework, safe fallback, escalation and budgets."""

import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from harness.config import Limits
from harness.model import ScriptedModel
from harness.orchestrator import Orchestrator, Phase
from harness.repo import discover_for_task
from harness.tools import ToolContext
from harness.verify import CommandStatus, Comparison
from harness.verify.commands import VerificationCommand
from harness.verify.targeting import derive_targets, framework_of

from tests.orchestration_helpers import (
    BUGGY_REPO, FIX_PATCH, TASK, WRONG_PATCH, complete, plan_response, tool,
)
from tests.repo_fixtures import git_repo, write_files

PY = sys.executable


def suite(argv, id="V1"):
    return VerificationCommand(id, "test", tuple(argv), "suite", "discovered", within_cap=True)


class TargetCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.base = Path(tmp.name).resolve()

    def derive(self, files, task, suite_argv, plan=None, name="r"):
        root = self.base / name
        write_files(root, files)
        ctx = ToolContext.create(root)
        discovery = discover_for_task(root, task, ctx=ctx)
        return derive_targets(ctx, discovery, plan, [suite(suite_argv)])


class DerivationTest(TargetCase):
    def test_unittest_method_level(self):
        result = self.derive(BUGGY_REPO, TASK, [PY, "-m", "unittest", "discover", "-s", "tests", "-t", "."])
        (target,) = result.targets
        self.assertEqual(target.argv, (PY, "-m", "unittest", "tests.test_math_utils.AddOneTest.test_add_one"))
        self.assertEqual((target.confidence, target.parent_command_id, target.framework), ("high", "V1", "unittest"))
        self.assertIn("tests/test_math_utils.py", target.derived_from)

    def test_unittest_module_level_when_no_test_name_matches(self):
        files = {**BUGGY_REPO, "tests/test_math_utils.py": BUGGY_REPO["tests/test_math_utils.py"].replace(
            "test_add_one", "test_increment")}
        result = self.derive(files, "Fix math_utils add_one", [PY, "-m", "unittest", "discover", "-s", "tests", "-t", "."])
        (target,) = result.targets
        self.assertEqual(target.argv[-1], "tests.test_math_utils")
        self.assertEqual(target.confidence, "medium")

    def test_unittest_without_packages_is_not_guessed(self):
        files = {k: v for k, v in BUGGY_REPO.items() if k != "tests/__init__.py"}
        result = self.derive(files, TASK, [PY, "-m", "unittest", "discover", "-s", "tests", "-t", "."])
        self.assertEqual(result.targets, ())
        self.assertIn("is not a package", result.unavailable_reason)
        result = self.derive(BUGGY_REPO, TASK, [PY, "-m", "unittest", "discover", "-s", "tests"], name="r2")
        self.assertEqual(result.targets, ())
        self.assertIn("cannot be derived safely", result.unavailable_reason)

    def test_pytest_function_and_class_selectors(self):
        files = {
            "pytest.ini": "[pytest]\n",
            "parser/__init__.py": "",
            "parser/core.py": "def parse_value(text):\n    return text\n",
            "tests/test_core.py": ("from parser.core import parse_value\n\n\ndef test_parse_value_empty():\n"
                                   "    assert parse_value('') == ''\n\n\ndef test_other():\n    pass\n\n\n"
                                   "class TestParseValue:\n    def test_parse_value_spaces(self):\n        pass\n"),
        }
        result = self.derive(files, "parse_value fails on empty input", [PY, "-m", "pytest"])
        (target,) = result.targets
        self.assertEqual(target.argv, (PY, "-m", "pytest", "tests/test_core.py::test_parse_value_empty",
                                       "tests/test_core.py::TestParseValue::test_parse_value_spaces"))
        self.assertEqual(target.framework, "pytest")

    def test_go_package_and_run_selector(self):
        files = {
            "go.mod": "module example.com/cfg\n\ngo 1.21\n",
            "internal/parser/parser.go": "package parser\n\nfunc ParseConfig() {}\n",
            "internal/parser/parser_test.go": ("package parser\n\nimport \"testing\"\n\n"
                                               "func TestParseConfig(t *testing.T) {}\n\nfunc TestOther(t *testing.T) {}\n"),
        }
        result = self.derive(files, "ParseConfig breaks on empty files", ["go", "test", "./..."])
        (target,) = result.targets
        self.assertEqual(target.argv, ("go", "test", "./internal/parser", "-run", "^(TestParseConfig)$"))

    def test_unsupported_frameworks_return_no_target(self):
        for argv in (["npm", "test"], ["make", "test"], ["cargo", "test"]):
            with self.subTest(argv=argv):
                self.assertIsNone(framework_of(argv))
                result = self.derive(BUGGY_REPO, TASK, argv, name="u" + argv[0])
                self.assertEqual(result.targets, ())
                self.assertIn("framework selector could not be derived safely", result.unavailable_reason)

    def test_no_linked_test_file(self):
        result = self.derive(BUGGY_REPO, "Improve documentation wording",
                             [PY, "-m", "unittest", "discover", "-s", "tests", "-t", "."])
        self.assertEqual(result.targets, ())
        self.assertIn("no discovered test file is linked", result.unavailable_reason)


@unittest.skipUnless(shutil.which("git"), "git not installed")
class EscalationTest(TargetCase):
    def setUp(self):
        super().setUp()
        patcher = mock.patch.dict(os.environ, {}, clear=False)
        patcher.start()
        self.addCleanup(patcher.stop)
        os.environ.pop("AI_API_KEY", None)

    def run_task(self, *script, limits=None, files=BUGGY_REPO):
        self.root = git_repo(self.base / "repo", files)
        self.model = ScriptedModel(list(script))
        self.state = Orchestrator(self.model, limits=limits).run(self.root, TASK)
        return self.state

    def test_targeted_runs_before_the_suite_and_both_are_compared(self):
        state = self.run_task(plan_response(), tool("apply_patch", patch=FIX_PATCH), complete())
        t1, v1 = state.verification_commands
        self.assertEqual((t1.id, t1.level, t1.purpose, t1.parent_id), ("T1", "targeted", "task", "V1"))
        self.assertEqual((v1.id, v1.level, v1.purpose), ("V1", "suite", "suite"))
        order = [(i.phase, i.command_id) for i in state.evidence.items if i.command_id]
        self.assertEqual(order, [("baseline", "T1"), ("baseline", "V1"), ("post-1", "T1"), ("post-1", "V1")])
        results = {r.command.id: r.comparison for r in state.last_report.command_results}
        self.assertEqual(results, {"T1": Comparison.FIXED, "V1": Comparison.FIXED})
        self.assertEqual(state.phase, Phase.VERIFIED)
        self.assertEqual(state.tool_calls, 7 + 1 + 5)   # baseline 3+2+2, patch, round 2 tests + 3 snapshot

    def test_suite_is_skipped_while_the_target_still_fails(self):
        state = self.run_task(plan_response(), tool("apply_patch", patch=WRONG_PATCH), complete(),
                              limits=Limits(max_repair_cycles=0))
        (only,) = state.last_report.command_results
        self.assertEqual((only.command.id, only.post.classification.status), ("T1", CommandStatus.TEST_FAILURE))
        skipped = [i for i in state.evidence.items if i.command_id == "V1" and i.phase == "post-1"]
        self.assertEqual(skipped[0].result, "NOT_RUN")
        self.assertIn("did not pass", skipped[0].description)
        self.assertEqual(state.metrics.command_calls, 3)    # T1+V1 baseline, T1 post

    def test_broad_suite_skipped_when_targeted_evidence_suffices_by_policy(self):
        state = self.run_task(plan_response(), tool("apply_patch", patch=FIX_PATCH), complete(),
                              limits=Limits(verify_full_suite=False))
        self.assertEqual(state.phase, Phase.VERIFIED)
        self.assertEqual([r.command.id for r in state.last_report.command_results], ["T1"])
        self.assertTrue(any("V1 not run: skipped: targeted evidence is sufficient" in r for r in state.last_report.risks))
        self.assertEqual(state.metrics.command_calls, 3)

    def test_fallback_is_explicit_when_no_target_can_be_derived(self):
        files = {k: v for k, v in BUGGY_REPO.items() if k != "tests/__init__.py"}
        state = self.run_task(plan_response(), tool("apply_patch", patch=FIX_PATCH), complete(), files=files)
        self.assertEqual([c.level for c in state.verification_commands], ["suite"])
        self.assertIn("cannot be derived safely", state.targeting.unavailable_reason)

    def test_targeting_can_be_disabled(self):
        state = self.run_task(plan_response(), tool("apply_patch", patch=FIX_PATCH), complete(),
                              limits=Limits(targeted_tests=False))
        self.assertIsNone(state.targeting)
        self.assertEqual([c.id for c in state.verification_commands], ["V1"])


if __name__ == "__main__":
    unittest.main()

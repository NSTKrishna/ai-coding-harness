"""M5 end-to-end: baseline -> execute -> verify -> (repair -> verify) with ScriptedModel, offline, no key."""

import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from harness.config import Limits
from harness.model import ScriptedModel
from harness.orchestrator import Orchestrator, Phase
from harness.orchestrator.report import format_run
from harness.verify import CommandStatus, Comparison, EvidenceKind, FailureClass, Verdict

from tests.orchestration_helpers import (
    BREAK_DOUBLE_PATCH, BUGGY_REPO, ENVIRONMENT_REPO, FIX_DOUBLE_PATCH, FIX_PATCH, LEGACY_TEST, NO_COMMAND_REPO,
    PASSING_REPO, REPAIR_PATCH, SECOND_WRONG_PATCH, TASK, UNITTEST_CMD, WRONG_PATCH, complete, plan_response, tool,
)
from tests.repo_fixtures import git_repo

BASELINE_TOOLS = 6   # 3 initial snapshot + 1 test run + 2 after-baseline snapshot
VERIFY_TOOLS = 4     # 1 test run + 3 snapshot
SELECTED = dict(verification_candidates=[UNITTEST_CMD])


@unittest.skipUnless(shutil.which("git"), "git not installed")
class VerificationCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.base = Path(tmp.name).resolve()
        patcher = mock.patch.dict(os.environ, {}, clear=False)
        patcher.start()
        self.addCleanup(patcher.stop)
        os.environ.pop("AI_API_KEY", None)

    def run_task(self, files, *script, limits=None, task=TASK, name="repo", untracked=None):
        self.root = git_repo(self.base / name, files, untracked)
        self.model = ScriptedModel(list(script))
        self.state = Orchestrator(self.model, limits=limits).run(self.root, task)
        return self.state

    def items(self, kind=None, phase=None):
        return [i for i in self.state.evidence.items
                if (kind is None or i.kind == kind) and (phase is None or i.phase == phase)]


class FirstTrySuccessTest(VerificationCase):
    def test_baseline_fail_then_post_pass_is_verified(self):
        state = self.run_task(BUGGY_REPO, plan_response(), tool("read_file", path="src/math_utils.py"),
                              tool("apply_patch", patch=FIX_PATCH), complete())
        self.assertEqual(state.phase, Phase.VERIFIED)
        (report,) = state.verification_reports
        (result,) = report.command_results
        self.assertEqual((result.baseline.classification.status, result.post.classification.status, result.comparison),
                         (CommandStatus.TEST_FAILURE, CommandStatus.PASS, Comparison.FIXED))
        self.assertEqual(result.positive, "strong")
        baseline_item, post_item = self.items(EvidenceKind.TEST)
        self.assertEqual((baseline_item.phase, baseline_item.result, baseline_item.exit_code), ("baseline", "TEST_FAILURE", 1))
        self.assertEqual((post_item.phase, post_item.result, post_item.exit_code), ("post-1", "PASS", 0))
        (criterion,) = report.criteria_results
        self.assertEqual(criterion.status, "PASS")
        self.assertIn(baseline_item.id, criterion.evidence_ids)
        self.assertIn(post_item.id, criterion.evidence_ids)
        self.assertEqual((state.model_calls, state.tool_calls, state.repair_cycles),
                         (4, BASELINE_TOOLS + 2 + VERIFY_TOOLS, 0))

    def test_baseline_runs_before_the_first_edit(self):
        self.run_task(BUGGY_REPO, plan_response(), tool("apply_patch", patch=FIX_PATCH), complete())
        first_executor_request = self.model.requests[1].messages[1].content
        self.assertIn("Baseline (before any edit):", first_executor_request)
        self.assertIn("TEST_FAILURE", first_executor_request)
        self.assertIn("AssertionError: 3 != 2", first_executor_request)
        order = [t.target for t in self.state.transitions]
        self.assertLess(order.index(Phase.BASELINING), order.index(Phase.EXECUTE))

    def test_completion_claim_alone_is_not_evidence(self):
        # the model claims success without changing anything
        state = self.run_task(BUGGY_REPO, plan_response(), complete("all fixed"), limits=Limits(max_repair_cycles=0))
        self.assertNotEqual(state.phase, Phase.VERIFIED)
        self.assertEqual(state.verification_reports[0].verdict, Verdict.NEEDS_REPAIR)
        self.assertEqual(state.verification_reports[0].failure_class, FailureClass.DIFF_PROBLEM)


class RepairTest(VerificationCase):
    def test_failed_verification_is_repaired_and_reverified(self):
        state = self.run_task(
            BUGGY_REPO, plan_response(**SELECTED),
            tool("read_file", path="src/math_utils.py"), tool("apply_patch", patch=WRONG_PATCH), complete("fixed"),
            # repair cycle 1
            tool("read_file", path="src/math_utils.py"), tool("apply_patch", patch=REPAIR_PATCH), complete("fixed now"),
        )
        self.assertEqual(state.phase, Phase.VERIFIED)
        self.assertEqual(state.repair_cycles, 1)
        first, second = state.verification_reports
        self.assertEqual((first.verdict, first.failure_class), (Verdict.NEEDS_REPAIR, FailureClass.TASK_TEST_FAILURE))
        self.assertEqual(first.command_results[0].comparison, Comparison.UNCHANGED_FAILURE)
        self.assertEqual((second.verdict, second.command_results[0].comparison), (Verdict.VERIFIED, Comparison.FIXED))
        self.assertEqual([t.target for t in state.transitions][4:], [
            Phase.READY_FOR_VERIFICATION, Phase.VERIFYING, Phase.NEEDS_REPAIR, Phase.REPAIRING,
            Phase.READY_FOR_VERIFICATION, Phase.VERIFYING, Phase.VERIFIED])
        # causal history in the ledger: baseline fail, first verification fail, second verification pass
        self.assertEqual([(i.phase, i.result) for i in self.items(EvidenceKind.TEST)],
                         [("baseline", "TEST_FAILURE"), ("post-1", "TEST_FAILURE"), ("post-2", "PASS")])
        self.assertEqual(state.modified_files, ["src/math_utils.py"])
        (record,) = state.changes.records
        self.assertEqual((record.touched_in, record.patches), (["execute", "repair-1"], 2))
        self.assertEqual((state.model_calls, state.tool_calls),
                         (7, BASELINE_TOOLS + 2 + VERIFY_TOOLS + 1 + 2 + VERIFY_TOOLS))   # +1: fresh re-read

    def test_repair_request_has_fresh_content_and_failure_evidence(self):
        self.run_task(BUGGY_REPO, plan_response(**SELECTED),
                      tool("read_file", path="src/math_utils.py"), tool("apply_patch", patch=WRONG_PATCH), complete(),
                      tool("apply_patch", patch=REPAIR_PATCH), complete())
        repair_request = self.model.requests[4].messages[1].content
        self.assertIn("# Repair (cycle 1 of at most 3)", repair_request)
        self.assertIn("TASK_TEST_FAILURE", repair_request)
        self.assertIn("AssertionError: 4 != 2", repair_request)             # current failure output
        working = repair_request.split("## Working context")[1].split("# Plan")[0]
        self.assertIn("### src/math_utils.py (current)\ndef add_one(x):\n    return x + 3", working)   # fresh
        self.assertNotIn("return x + 2", working)                          # no stale pre-edit snippet
        self.assertIn("-    return x + 2\n+    return x + 3", repair_request)   # the diff shows the change
        self.assertIn("[stale: src/math_utils.py changed after this step; read it again]", repair_request)
        self.assertIn("[verification] round 1 after repair cycle 0: NEEDS_REPAIR TASK_TEST_FAILURE", repair_request)
        self.assertIn("[repair_attempt] cycle 1 started for TASK_TEST_FAILURE", repair_request)
        self.assertLess(len(repair_request), 40_000)

    def test_suite_fallback_repairs_only_when_a_criterion_is_linked(self):
        # no command selected by the plan: the discovered suite is a fallback, but the still-failing test
        # maps to the acceptance criterion "add_one(1) returns 2", so the task is known to FAIL
        state = self.run_task(BUGGY_REPO, plan_response(), tool("apply_patch", patch=WRONG_PATCH), complete(),
                              limits=Limits(max_repair_cycles=0))
        report = state.verification_reports[0]
        self.assertEqual(report.command_results[0].command.purpose, "suite")
        self.assertEqual(report.criteria_results[0].status, "FAIL")
        self.assertEqual(report.failure_class, FailureClass.TASK_TEST_FAILURE)


class RegressionTest(VerificationCase):
    def test_regression_is_repaired_then_verified(self):
        task = "Add a docstring to double without changing its behaviour."
        state = self.run_task(PASSING_REPO, plan_response(**SELECTED, acceptance_criteria=["double has a docstring"]),
                              tool("apply_patch", patch=BREAK_DOUBLE_PATCH), complete(),
                              tool("apply_patch", patch=FIX_DOUBLE_PATCH), complete(), task=task)
        first, second = state.verification_reports
        self.assertEqual(first.command_results[0].comparison, Comparison.REGRESSED)
        self.assertEqual((first.verdict, first.failure_class), (Verdict.NEEDS_REPAIR, FailureClass.REGRESSION))
        self.assertEqual(second.command_results[0].comparison, Comparison.UNCHANGED_PASS)
        self.assertEqual(state.phase, Phase.VERIFIED)
        self.assertEqual(second.command_results[0].positive, "weak")
        self.assertTrue(any("may not exercise the change" in r for r in second.risks))
        self.assertEqual(second.criteria_results[0].status, "UNKNOWN")


class PreExistingFailureTest(VerificationCase):
    def test_unrelated_failure_is_reported_not_blamed(self):
        files = {**BUGGY_REPO, "tests/test_legacy.py": LEGACY_TEST}
        state = self.run_task(files, plan_response(), tool("apply_patch", patch=FIX_PATCH), complete())
        (report,) = state.verification_reports
        (result,) = report.command_results
        self.assertEqual(result.comparison, Comparison.IMPROVED)
        self.assertIsNone(result.finding)
        self.assertEqual(state.phase, Phase.VERIFIED)
        self.assertTrue(any("pre-existing" in r and "test_legacy_format" in r for r in report.risks))
        self.assertNotIn(FailureClass.REGRESSION, [r.finding for r in report.command_results])
        self.assertIn("test_legacy_format", format_run(state))


class EnvironmentAndEvidenceTest(VerificationCase):
    def test_environment_error_blocks_without_repair(self):
        state = self.run_task(ENVIRONMENT_REPO, plan_response(), tool("apply_patch", patch=FIX_PATCH), complete())
        self.assertEqual(state.phase, Phase.BLOCKED)
        self.assertEqual(state.failure.kind, "environment_error")
        (report,) = state.verification_reports
        self.assertEqual(report.command_results[0].comparison, Comparison.ENVIRONMENT)
        self.assertEqual((state.model_calls, state.repair_cycles), (3, 0))    # no repair model call
        self.assertEqual({i.result for i in self.items(EvidenceKind.ENVIRONMENT)}, {"ENVIRONMENT_ERROR"})

    def test_no_verification_command_is_unverified(self):
        state = self.run_task(NO_COMMAND_REPO, plan_response(), tool("apply_patch", patch=FIX_PATCH), complete())
        self.assertEqual(state.phase, Phase.UNVERIFIED)
        self.assertEqual(state.verification_commands, ())
        self.assertEqual(self.items(EvidenceKind.BASELINE)[0].result, "NOT_AVAILABLE")
        self.assertEqual(state.verification_reports[0].failure_class, FailureClass.NO_VERIFICATION_EVIDENCE)
        self.assertIn("return x + 1", (self.root / "src" / "math_utils.py").read_text())   # edit kept, not verified
        self.assertEqual(state.tool_calls, 3 + 1 + 3)   # initial snapshot, patch, final snapshot
        self.assertNotIn("[pass]", format_run(state))

    def test_dirty_repository_is_preserved_and_not_attributed(self):
        files = {**BUGGY_REPO, "README.md": "original\n"}
        root = git_repo(self.base / "dirty", files, {"notes.txt": "user's own notes\n"})
        (root / "README.md").write_text("edited by the user\n")
        self.model = ScriptedModel([plan_response(), tool("apply_patch", patch=FIX_PATCH), complete()])
        state = self.state = Orchestrator(self.model).run(root, TASK)
        self.assertEqual(state.phase, Phase.VERIFIED)
        report = state.verification_reports[0]
        self.assertEqual(report.changed_by_run, ("src/math_utils.py",))
        self.assertEqual(set(report.preexisting_changes), {"README.md", "notes.txt"})
        self.assertEqual(self.items(EvidenceKind.SNAPSHOT, "initial")[0].result, "DIRTY")
        self.assertEqual((root / "notes.txt").read_text(), "user's own notes\n")       # nothing reset or cleaned
        self.assertEqual((root / "README.md").read_text(), "edited by the user\n")

    def test_non_git_repository_verifies_without_git_snapshots(self):
        from tests.repo_fixtures import write_files
        root = self.base / "plain"
        write_files(root, BUGGY_REPO)
        self.model = ScriptedModel([plan_response(), tool("apply_patch", patch=FIX_PATCH), complete()])
        state = self.state = Orchestrator(self.model).run(root, TASK)
        self.assertEqual(state.phase, Phase.VERIFIED)
        self.assertEqual({i.result for i in self.items(EvidenceKind.SNAPSHOT)}, {"UNAVAILABLE"})
        self.assertEqual(state.metrics.tool_calls_by_name.get("git_status", 0), 0)
        self.assertEqual(state.tool_calls, 1 + 1 + 1)          # baseline test, patch, verification test

    def test_evidence_refers_to_observed_events(self):
        self.run_task(BUGGY_REPO, plan_response(**SELECTED),
                      tool("apply_patch", patch=WRONG_PATCH), complete(),
                      tool("apply_patch", patch=REPAIR_PATCH), complete())
        items = self.state.evidence.items
        self.assertEqual([i.id for i in items], [f"E{n}" for n in range(1, len(items) + 1)])
        command_items = [i for i in items if i.command_id is not None and i.result != "NOT_RUN"]
        self.assertEqual(len(command_items), self.state.metrics.tool_calls_by_name["run_tests"])
        for item in items:
            self.assertNotEqual(item.source, "model")
            for ref in item.refs:
                self.assertIsNotNone(self.state.evidence.get(ref))
        for report in self.state.verification_reports:
            for crit in report.criteria_results:
                for eid in crit.evidence_ids:
                    self.assertIsNotNone(self.state.evidence.get(eid))


class LimitTest(VerificationCase):
    def test_zero_repair_cycles_means_no_repair_call(self):
        state = self.run_task(BUGGY_REPO, plan_response(**SELECTED), tool("apply_patch", patch=WRONG_PATCH), complete(),
                              tool("apply_patch", patch=REPAIR_PATCH), complete(), limits=Limits(max_repair_cycles=0))
        self.assertEqual(state.phase, Phase.BUDGET_EXHAUSTED)
        self.assertEqual(state.failure.details, {"budget": "repair_cycles", "used": 0, "limit": 0, "phase": "repair"})
        self.assertEqual((state.model_calls, state.repair_cycles, len(state.verification_reports)), (3, 0, 1))
        self.assertEqual(self.model.remaining, 2)

    def test_one_repair_cycle_then_limit(self):
        state = self.run_task(BUGGY_REPO, plan_response(**SELECTED), tool("apply_patch", patch=WRONG_PATCH), complete(),
                              tool("apply_patch", patch=SECOND_WRONG_PATCH), complete(),
                              tool("apply_patch", patch=REPAIR_PATCH), complete(), limits=Limits(max_repair_cycles=1))
        self.assertEqual(state.phase, Phase.BUDGET_EXHAUSTED)
        self.assertEqual(state.failure.details, {"budget": "repair_cycles", "used": 1, "limit": 1, "phase": "repair"})
        self.assertEqual((state.model_calls, state.repair_cycles, len(state.verification_reports)), (5, 1, 2))
        self.assertEqual([r.verdict for r in state.verification_reports], [Verdict.NEEDS_REPAIR] * 2)
        self.assertEqual(self.model.remaining, 2)

    def test_no_model_budget_left_for_repair_but_verification_still_runs(self):
        state = self.run_task(BUGGY_REPO, plan_response(**SELECTED), tool("apply_patch", patch=WRONG_PATCH), complete(),
                              limits=Limits(max_model_calls=3))
        self.assertEqual(len(state.verification_reports), 1)      # verification needs no model call
        self.assertEqual(state.phase, Phase.BUDGET_EXHAUSTED)
        self.assertEqual(state.failure.details, {"budget": "model_calls", "used": 3, "limit": 3, "phase": "repair"})
        self.assertEqual((self.model.call_count, state.repair_cycles), (3, 0))

    def test_verification_command_cap(self):
        files = {**BUGGY_REPO, "Makefile": "build:\n\t@echo built\n"}
        state = self.run_task(files, plan_response(**SELECTED), tool("apply_patch", patch=FIX_PATCH), complete(),
                              limits=Limits(max_verification_commands=1))
        v1, v2 = state.verification_commands
        self.assertEqual((v1.purpose, v2.kind, v2.purpose, v2.within_cap), ("task", "build", "check", False))
        self.assertEqual([i.result for i in state.evidence.items if i.command_id == "V2"], ["NOT_RUN", "NOT_RUN"])
        self.assertEqual(state.metrics.command_calls, 2)          # only V1, at baseline and post-change
        self.assertTrue(any("V2 not run" in r for r in state.verification_reports[0].risks))
        self.assertEqual(state.phase, Phase.VERIFIED)             # decided by V1 alone

    def test_report_text(self):
        state = self.run_task(BUGGY_REPO, plan_response(**SELECTED),
                              tool("apply_patch", patch=WRONG_PATCH), complete(),
                              tool("apply_patch", patch=REPAIR_PATCH), complete())
        text = format_run(state)
        self.assertIn("TASK RESULT: VERIFIED", text)
        self.assertIn("Verification round 1: NEEDS_REPAIR (TASK_TEST_FAILURE)", text)
        self.assertIn("Verification round 2: VERIFIED", text)
        self.assertIn("Repair cycles: 1", text)
        self.assertIn("Executor completion claim (not evidence)", text)


if __name__ == "__main__":
    unittest.main()

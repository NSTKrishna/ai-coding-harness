"""End-to-end runs with ScriptedModel: task -> discovery -> plan -> baseline -> actions -> verification.

M4 tests of the execution mechanics. Since M5 every completed execution is verified, so:
- tool-call totals include the fixed baseline/verification overhead of the buggy fixture
  (``BASELINE_TOOLS``, ``VERIFY_TOOLS``);
- scripts that complete without the fix use ``NO_REPAIR`` (max_repair_cycles=0) so the run stops
  right after verification instead of asking the scripted model for a repair.
M5-specific behaviour is tested in tests/test_verification*.py.
"""

import json
from dataclasses import replace
import os
import shutil
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from harness.config import ContextLimits, Limits
from harness.model import ModelError, ScriptedModel, malformed_tool_call_response, text_response, tool_call_response
from harness.model.types import ModelResponse, ToolCall
from harness.orchestrator import Orchestrator, Phase
from harness.orchestrator import orchestrator as orchestrator_module
from harness.orchestrator.executor import Executor
from harness.orchestrator.orchestrator import tool_limits_from

from tests.orchestration_helpers import FIX_PATCH, TASK, blocked, buggy_repo, complete, plan_response, tool
from tests.repo_fixtures import snapshot

PY = sys.executable
TEST_COMMAND = [PY, "-m", "unittest", "discover", "-s", "tests", "-t", "."]
# buggy fixture (git, one discovered test command):
BASELINE_TOOLS = 6   # initial git_status + git_diff_stat + git_diff, run_tests, after-baseline git_status + git_diff_stat
VERIFY_TOOLS = 4     # run_tests, git_status + git_diff_stat + git_diff
NO_REPAIR = Limits(max_repair_cycles=0)


@unittest.skipUnless(shutil.which("git"), "git not installed")
class OrchestratorCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = buggy_repo(Path(tmp.name).resolve())
        patcher = mock.patch.dict(os.environ, {}, clear=False)
        patcher.start()
        self.addCleanup(patcher.stop)
        os.environ.pop("AI_API_KEY", None)   # scripted runs must not need a key

    def run_script(self, *script, limits=None, task=TASK, **kwargs):
        # These tests pin the M5 verification command set (one suite, no targeted command) so the
        # exact overhead constants hold; targeted tests are covered in tests/test_m6_*.py.
        limits = replace(limits or Limits(), targeted_tests=False)
        self.model = ScriptedModel(list(script))
        self.state = Orchestrator(self.model, limits=limits, **kwargs).run(self.root, task)
        return self.state


class ReadPatchIntegrationTest(OrchestratorCase):
    def test_read_then_patch_then_complete(self):
        state = self.run_script(
            plan_response(),
            tool("read_file", path="src/math_utils.py"),
            tool("apply_patch", patch=FIX_PATCH),
            complete("add_one returns x + 1"),
        )
        # The executor stops at READY_FOR_VERIFICATION; the verifier (not the model) decides.
        self.assertEqual([t.target for t in state.transitions],
                         [Phase.DISCOVER, Phase.PLAN, Phase.BASELINING, Phase.EXECUTE,
                          Phase.READY_FOR_VERIFICATION, Phase.VERIFYING, Phase.VERIFIED])
        self.assertEqual(state.completion_claims, [(3, "add_one returns x + 1")])
        self.assertNotIn("add_one returns x + 1", state.terminal_reason)   # the claim is not the verdict
        self.assertEqual(state.modified_files, ["src/math_utils.py"])
        self.assertIn("return x + 1", (self.root / "src" / "math_utils.py").read_text())
        self.assertEqual((state.steps, state.model_calls, state.tool_calls),
                         (3, 4, BASELINE_TOOLS + 2 + VERIFY_TOOLS))
        self.assertEqual([(a.kind, a.tool) for a in state.action_history],
                         [("tool", "read_file"), ("tool", "apply_patch"), ("complete", None)])
        read, patch = state.observations
        self.assertTrue(read.stale)                     # pre-edit content invalidated
        self.assertFalse(patch.stale)
        self.assertEqual(patch.affected_paths, ("src/math_utils.py",))
        self.assertEqual(state.plan.acceptance_criteria, ("add_one(1) returns 2",))

    def test_each_observation_feeds_the_next_request(self):
        self.run_script(plan_response(), tool("read_file", path="src/math_utils.py"),
                        tool("apply_patch", patch=FIX_PATCH), complete())
        second, third = self.model.requests[2].messages[1].content, self.model.requests[3].messages[1].content
        self.assertIn("return x + 2", second)            # read result shown before patching
        self.assertIn("step 1: read_file", second)
        self.assertIn("[stale: src/math_utils.py changed after this step; read it again]", third)
        self.assertNotIn("    return x + 2\n", third.split("# Recent observations")[1])

    def test_stale_working_set_evidence_is_removed_after_patch(self):
        self.run_script(plan_response(), tool("apply_patch", patch=FIX_PATCH), complete())
        before, after = self.model.requests[1].messages[1].content, self.model.requests[2].messages[1].content
        self.assertIn("### src/math_utils.py:", before)
        self.assertNotIn("### src/math_utils.py:", after.split("# Plan")[0])

    def test_one_action_per_model_call(self):
        state = self.run_script(plan_response(), *[tool("read_file", path="src/math_utils.py")] * 3, complete(),
                                limits=NO_REPAIR)
        self.assertEqual(state.steps, 4)
        self.assertEqual(state.model_calls, 1 + state.steps)
        self.assertEqual(len(state.observations), 3)
        self.assertEqual(state.metrics.tool_calls_by_name["read_file"], 3)


class CommandObservationTest(OrchestratorCase):
    def test_failing_tests_are_a_successful_tool_call_with_a_failed_command(self):
        state = self.run_script(plan_response(), tool("run_tests", command=TEST_COMMAND), complete("not fixed yet"),
                                limits=NO_REPAIR)
        (obs,) = state.observations
        self.assertTrue(obs.success)                   # tool invocation succeeded
        self.assertEqual(obs.outcome, "command_failed")  # test command failed
        self.assertEqual(obs.exit_code, 1)
        self.assertFalse(obs.timed_out)
        self.assertIsNone(obs.error_code)
        self.assertIn("AssertionError: 3 != 2", obs.result_summary)
        self.assertNotEqual(state.phase, Phase.TOOL_ERROR)           # a failing command is not a tool failure
        self.assertEqual(state.repair_cycles, 0)
        self.assertEqual(state.metrics.command_calls, 3)             # baseline + executor + verification
        self.assertIn("tool ok, command_failed, exit_code=1", self.model.requests[2].messages[1].content)

    def test_command_timeout_comes_from_configuration(self):
        self.assertEqual(tool_limits_from(Limits(command_timeout_seconds=7)).command_timeout_seconds, 7)
        start = time.monotonic()
        state = self.run_script(plan_response(),
                                tool("run_command", command=[PY, "-c", "import time; time.sleep(30)"], timeout_seconds=999),
                                complete(), limits=Limits(command_timeout_seconds=1))
        (obs,) = state.observations
        self.assertEqual((obs.outcome, obs.timed_out), ("command_timed_out", True))
        self.assertLess(time.monotonic() - start, 15)


class BudgetTest(OrchestratorCase):
    def test_step_budget(self):
        state = self.run_script(plan_response(), *[tool("read_file", path="src/math_utils.py")] * 5,
                                limits=Limits(max_steps=3))
        self.assertEqual(state.phase, Phase.BUDGET_EXHAUSTED)
        self.assertEqual(state.failure.details, {"budget": "steps", "used": 3, "limit": 3})
        self.assertEqual((state.steps, state.model_calls, state.tool_calls), (3, 4, BASELINE_TOOLS + 3))
        self.assertEqual(self.model.remaining, 2)          # no extra model call to announce it

    def test_model_call_budget(self):
        state = self.run_script(plan_response(), *[tool("read_file", path="src/math_utils.py")] * 5,
                                limits=Limits(max_model_calls=3))
        self.assertEqual(state.phase, Phase.BUDGET_EXHAUSTED)
        self.assertEqual(state.failure.details, {"budget": "model_calls", "used": 3, "limit": 3})
        self.assertEqual((state.model_calls, state.steps, state.tool_calls), (3, 2, BASELINE_TOOLS + 2))
        self.assertEqual(self.model.call_count, 3)

    def test_tool_call_budget_checked_before_dispatch(self):
        limit = BASELINE_TOOLS + 2
        state = self.run_script(plan_response(), *[tool("read_file", path="src/math_utils.py")] * 5,
                                limits=Limits(max_tool_calls=limit))
        self.assertEqual(state.phase, Phase.BUDGET_EXHAUSTED)
        self.assertEqual(state.failure.details, {"budget": "tool_calls", "used": limit, "limit": limit})
        self.assertEqual((state.tool_calls, state.model_calls, state.steps), (limit, 4, 3))
        self.assertEqual(len(state.observations), 2)

    def test_exact_tool_budget_for_execution_and_verification(self):
        limit = BASELINE_TOOLS + 2 + VERIFY_TOOLS
        state = self.run_script(plan_response(), tool("read_file", path="src/math_utils.py"),
                                tool("apply_patch", patch=FIX_PATCH), complete(), limits=Limits(max_tool_calls=limit))
        self.assertEqual(state.phase, Phase.VERIFIED)
        self.assertEqual(state.tool_calls, limit)

    def test_no_tool_budget_left_for_verification_is_never_verified(self):
        limit = BASELINE_TOOLS + 2
        state = self.run_script(plan_response(), tool("read_file", path="src/math_utils.py"),
                                tool("apply_patch", patch=FIX_PATCH), complete(), limits=Limits(max_tool_calls=limit))
        self.assertEqual(state.phase, Phase.BUDGET_EXHAUSTED)
        self.assertEqual(state.failure.details, {"budget": "tool_calls", "used": limit, "limit": limit,
                                                 "phase": "verification"})
        self.assertEqual(state.verification_reports, [])

    def test_planner_alone_can_use_the_whole_model_budget(self):
        state = self.run_script(plan_response(), complete(), limits=Limits(max_model_calls=1))
        self.assertEqual(state.phase, Phase.BUDGET_EXHAUSTED)
        self.assertEqual((state.model_calls, state.steps), (1, 0))
        self.assertIsNotNone(state.plan)

    def test_no_model_budget_means_no_planner_call(self):
        state = self.run_script(plan_response(), limits=Limits(max_model_calls=0))
        self.assertEqual(state.phase, Phase.BUDGET_EXHAUSTED)
        self.assertEqual(self.model.call_count, 0)

    def test_execution_requests_stay_bounded(self):
        # distinct calls: 30 identical calls would (correctly) be stopped by the M6 no-progress rule.
        # The plan is inspect-only, so the stalled-edit rule does not apply either: this test is
        # about context staying bounded over a long run, not about progress.
        reads = [tool("read_range", path="src/math_utils.py", start_line=1, end_line=n) for n in range(1, 31)]
        state = self.run_script(plan_response(steps=[{"kind": "inspect", "description": "Read the file"}]),
                                *reads, complete(), limits=Limits(max_steps=40, max_repair_cycles=0))
        sizes = [len(r.messages[1].content) for r in self.model.requests[1:]]
        self.assertEqual(state.steps, 31)
        self.assertLess(max(sizes[22:]) - min(sizes[22:]), 200)   # windows saturated: no growth


class MalformedOutputTest(OrchestratorCase):
    def assert_terminal(self, state, phase, kind):
        self.assertEqual(state.phase, phase)
        self.assertEqual(state.failure.kind, kind)

    def test_malformed_plan(self):
        bad = text_response("I will fix add_one.")
        state = self.run_script(bad, bad)                     # rejected, corrected once, rejected again
        self.assert_terminal(state, Phase.MODEL_ERROR, "invalid_plan")
        self.assertEqual((state.steps, state.tool_calls, state.model_calls), (0, 0, 2))
        retry = self.model.requests[1].messages
        self.assertEqual([m.role for m in retry[-2:]], ["assistant", "user"])
        self.assertIn("That plan was rejected", retry[-1].content)

    def test_rejected_plan_is_corrected_once(self):
        state = self.run_script(text_response("I will fix add_one."), plan_response(),
                                tool("apply_patch", patch=FIX_PATCH), complete(), limits=NO_REPAIR)
        self.assertEqual(state.phase, Phase.VERIFIED, state.terminal_reason)

    def test_plan_missing_field_and_wrong_type(self):
        from tests.orchestration_helpers import plan_dict
        partial = {k: v for k, v in plan_dict().items() if k != "steps"}
        state = self.run_script(text_response(json.dumps(partial)), text_response(json.dumps(partial)))
        self.assert_terminal(state, Phase.MODEL_ERROR, "invalid_plan")
        self.assertIn("steps", state.failure.message)
        state = self.run_script(plan_response(files_to_inspect="src/math_utils.py"),
                                plan_response(files_to_inspect="src/math_utils.py"))
        self.assert_terminal(state, Phase.MODEL_ERROR, "invalid_plan")

    def test_malformed_action_json(self):
        bad = text_response('{"action": "tool", "tool": "read_file",')
        state = self.run_script(plan_response(), bad, bad, bad)   # 2 answered with feedback, the 3rd is terminal
        self.assert_terminal(state, Phase.MODEL_ERROR, "invalid_action")
        self.assertEqual(state.tool_calls, BASELINE_TOOLS)       # nothing dispatched for the bad actions
        self.assertEqual([o.outcome for o in state.observations], ["invalid_action", "invalid_action"])
        self.assertEqual(state.model_calls, 4)

    def test_unknown_action(self):
        bad = text_response('{"action": "shell", "command": "rm -rf /"}')
        state = self.run_script(plan_response(), bad, bad, bad)
        self.assert_terminal(state, Phase.MODEL_ERROR, "invalid_action")
        self.assertIn("unknown action 'shell'", state.failure.message)

    def test_identical_repeat_is_annotated_and_still_stopped(self):
        read = tool("read_file", path="src/math_utils.py")
        state = self.run_script(plan_response(), read, read, read, read)
        self.assert_terminal(state, Phase.BLOCKED, "no_progress")
        notes = [o for o in state.observations if "[harness] Identical to the result of step" in o.result_summary]
        self.assertEqual(len(notes), 3)                            # every repeat after the first read
        self.assertIn("take a different action", self.model.requests[3].messages[1].content)

    def test_passing_tests_after_a_change_suggest_completing(self):
        state = self.run_script(plan_response(), tool("apply_patch", patch=FIX_PATCH),
                                tool("run_tests", command=TEST_COMMAND), complete(), limits=NO_REPAIR)
        self.assertEqual(state.phase, Phase.VERIFIED)
        self.assertIn("If the task is done, call complete", self.model.requests[3].messages[1].content)

    def test_low_step_budget_is_announced(self):
        read = lambda n: tool("read_range", path="src/math_utils.py", start_line=1, end_line=n)
        self.run_script(plan_response(), read(1), read(2), complete(), limits=Limits(max_steps=6, max_repair_cycles=0))
        self.assertNotIn("Few steps remain", self.model.requests[1].messages[1].content)    # 6 steps left
        self.assertIn("Few steps remain", self.model.requests[2].messages[1].content)       # 5 steps left

    @staticmethod
    def distinct_reads(count):
        """`count` read_range calls that each return something different (so no rule but the one
        under test can fire). src/math_utils.py has 6 lines, tests/test_math_utils.py has 8."""
        spans = [("src/math_utils.py", a, b) for a in range(1, 6) for b in range(a, 7)]
        spans += [("tests/test_math_utils.py", a, b) for a in range(1, 8) for b in range(a, 9)]
        assert count <= len(spans), count
        return [tool("read_range", path=p, start_line=a, end_line=b) for p, a, b in spans[:count]]

    def test_a_cycle_of_read_only_calls_is_stopped(self):
        """A,B,C,A,B,C never repeats consecutively, so only the window rule can see it."""
        cycle = [tool("read_file", path="src/math_utils.py"),
                 tool("git_status"),
                 tool("list_files", path="src")] * 4
        state = self.run_script(plan_response(), *cycle, complete(), limits=Limits(max_repair_cycles=0))
        self.assert_terminal(state, Phase.BLOCKED, "no_progress")
        self.assertTrue(state.no_progress)
        self.assertTrue(state.failure.details.get("cycle"))
        self.assertIn("repeated earlier calls", state.terminal_reason)
        self.assertEqual(state.steps, 11)            # 3 new calls, then a full window of repeats
        self.assertEqual(state.modified_files, [])

    def test_varied_exploration_is_not_mistaken_for_a_cycle(self):
        """One genuinely new observation per window is enough: a wide search is real work."""
        state = self.run_script(plan_response(), *self.distinct_reads(15),
                                tool("apply_patch", patch=FIX_PATCH), complete(), limits=NO_REPAIR)
        self.assertEqual(state.phase, Phase.VERIFIED, state.terminal_reason)

    def test_a_plan_with_edit_steps_that_never_edits_is_stopped(self):
        state = self.run_script(plan_response(), *self.distinct_reads(30), complete(),
                                limits=Limits(max_steps=40, max_repair_cycles=0))
        self.assert_terminal(state, Phase.BLOCKED, "no_progress")
        self.assertTrue(state.failure.details.get("no_edit"))
        self.assertEqual(state.steps, 20)            # the allowance, not the 40-step budget
        warned = [i for i, r in enumerate(self.model.requests) if "No successful edit yet." in r.messages[1].content]
        self.assertTrue(warned, "the model should be warned before being stopped")
        self.assertLess(warned[0], state.steps, "the warning must arrive before the stop, not with it")

    def test_a_stall_after_real_edits_is_verified_not_discarded(self):
        """Edits already on disk are real work. Stopping the loop must not throw away the
        chance to judge them: a live run deleted the right files, then flailed, and was BLOCKED."""
        state = self.run_script(plan_response(), tool("apply_patch", patch=FIX_PATCH),
                                *([tool("git_status"), tool("list_files", path="src")] * 6),
                                limits=NO_REPAIR)
        self.assertTrue(state.no_progress)
        self.assertIsNone(state.failure)                       # a stall, not a failure
        self.assertEqual(state.phase, Phase.VERIFIED, state.terminal_reason)   # judged on evidence
        self.assertEqual(state.modified_files, ["src/math_utils.py"])
        self.assertTrue(any("verifying the changes already made" in t.reason
                            for t in state.transitions if t.target == Phase.READY_FOR_VERIFICATION))

    def test_the_stalled_edit_rule_ignores_an_inspect_only_plan(self):
        """Nothing was asked to change, so making no change is not a stall."""
        state = self.run_script(plan_response(steps=[{"kind": "inspect", "description": "Look"}]),
                                *self.distinct_reads(25), complete(),
                                limits=Limits(max_steps=40, max_repair_cycles=0))
        self.assertEqual(state.steps, 26)                 # every step ran; nothing was cut short
        self.assertNotEqual(state.failure.kind, "no_progress")
        self.assertFalse(state.no_progress)

    def test_invalid_reply_is_fed_back_and_the_run_recovers(self):
        state = self.run_script(plan_response(), text_response("I will now fix the bug."),
                                tool("apply_patch", patch=FIX_PATCH), complete(), limits=NO_REPAIR)
        self.assertEqual(state.phase, Phase.VERIFIED, state.terminal_reason)
        feedback = self.model.requests[2].messages[1].content    # the step after the invalid reply
        self.assertIn("Your reply was not a valid action", feedback)
        self.assertEqual([a.kind for a in state.action_history], ["invalid", "tool", "complete"])

    def test_native_malformed_arguments_are_a_structured_tool_error(self):
        state = self.run_script(plan_response(), malformed_tool_call_response("read_file", '{"path": '), complete(),
                                limits=NO_REPAIR)
        self.assertNotIn(state.phase, (Phase.MODEL_ERROR, Phase.TOOL_ERROR))
        (obs,) = state.observations
        self.assertEqual((obs.success, obs.outcome, obs.error_code), (False, "tool_error", "invalid_arguments"))
        self.assertEqual(state.action_history[0].source, "native")

    def test_unknown_tool_is_a_structured_tool_error(self):
        state = self.run_script(plan_response(), tool("delete_everything"), complete(), limits=NO_REPAIR)
        (obs,) = state.observations
        self.assertEqual((obs.success, obs.error_code), (False, "unknown_tool"))
        self.assertNotIn(state.phase, (Phase.MODEL_ERROR, Phase.TOOL_ERROR))

    def test_model_errors_end_cleanly(self):
        state = self.run_script(plan_response(), ModelError("HTTP 503", retryable=True))
        self.assert_terminal(state, Phase.MODEL_ERROR, "model_call_failed")
        self.assertTrue(state.failure.details["retryable"])
        state = self.run_script(RuntimeError("adapter bug"))           # during planning
        self.assert_terminal(state, Phase.MODEL_ERROR, "model_call_failed")
        self.assertIn("RuntimeError: adapter bug", state.failure.message)
        state = self.run_script(plan_response(), RuntimeError("adapter bug"))
        self.assert_terminal(state, Phase.MODEL_ERROR, "model_call_failed")

    def test_blocked(self):
        state = self.run_script(plan_response(), blocked("tests directory is missing"))
        self.assert_terminal(state, Phase.BLOCKED, "blocked_by_model")
        self.assertEqual(state.terminal_reason, "tests directory is missing")

    def test_native_and_text_tool_calls_both_dispatch(self):
        state = self.run_script(plan_response(), tool_call_response("read_file", {"path": "src/math_utils.py"}),
                                tool("read_file", path="src/math_utils.py"), complete())
        self.assertEqual([a.source for a in state.action_history[:2]], ["native", "text"])
        from harness.orchestrator.executor import REPEAT_NOTE
        first, second = state.observations[0].result_summary, state.observations[1].result_summary
        self.assertEqual(first, second.split(REPEAT_NOTE)[0])      # same result by either route
        self.assertIn(REPEAT_NOTE, second)


class WiringTest(OrchestratorCase):
    def test_discovery_runs_before_first_model_call_and_shares_the_tool_context(self):
        order, contexts = [], {}
        real_discover, real_registry = orchestrator_module.discover_for_task, orchestrator_module.build_registry

        def discover(*args, **kwargs):
            order.append("discover")
            contexts["discovery"] = kwargs["ctx"]
            return real_discover(*args, **kwargs)

        def registry(ctx, **kwargs):
            contexts["registry"] = ctx
            return real_registry(ctx, **kwargs)

        def planner(request):
            order.append("plan")
            self.assertIn("## Candidate files", request.messages[1].content)
            return plan_response()

        with mock.patch.object(orchestrator_module, "discover_for_task", discover), \
                mock.patch.object(orchestrator_module, "build_registry", registry):
            state = self.run_script(planner, complete())
        self.assertEqual(order, ["discover", "plan"])
        self.assertIs(contexts["discovery"], contexts["registry"])
        self.assertIs(contexts["discovery"].metrics, state.metrics)
        self.assertEqual(contexts["discovery"].root, self.root)

    def test_planner_uses_the_m3_working_set(self):
        state = self.run_script(plan_response(), complete())
        self.assertIn(state.working_set.render(), self.model.requests[0].messages[1].content)

    def test_no_repository_mutation_before_execution(self):
        before = snapshot(self.root)
        checks = []

        def planner(request):
            checks.append(snapshot(self.root) == before)
            return plan_response()

        def first_action(request):
            checks.append(snapshot(self.root) == before)
            return complete()

        state = self.run_script(planner, first_action, limits=NO_REPAIR)
        self.assertEqual(checks, [True, True])   # discovery, planning and the baseline test run changed nothing
        self.assertEqual(snapshot(self.root), before)
        self.assertEqual(state.metrics.command_calls, 2)   # baseline + verification ran the tests

    def test_planner_sees_resolved_interpreter_not_bare_python(self):
        state = self.run_script(plan_response(), complete())
        planner_input = self.model.requests[0].messages[1].content
        self.assertIn(f"- {PY} -m unittest discover -s tests -t .", planner_input)
        self.assertNotIn("- python -m unittest", planner_input)
        self.assertTrue(state.repo_profile.test_commands)

    def test_unresolved_spelling_from_summary_maps_to_resolved_command(self):
        state = self.run_script(plan_response(verification_candidates=["python -m unittest discover -s tests -t ."]),
                                complete(), limits=NO_REPAIR)
        self.assertEqual(state.plan.verification_candidates[0].argv[0], PY)

    def test_invented_verification_command_is_rejected(self):
        invented = plan_response(verification_candidates=["pytest -q"])
        state = self.run_script(invented, invented)
        self.assertEqual(state.failure.kind, "invalid_plan")

    def test_chosen_verification_command_is_carried_in_the_plan(self):
        command = f"{PY} -m unittest discover -s tests -t ."
        state = self.run_script(plan_response(verification_candidates=[command]), complete(), limits=NO_REPAIR)
        self.assertEqual([" ".join(c.argv) for c in state.plan.verification_candidates], [command])

    def test_invalid_input(self):
        self.assertEqual(self.run_script(plan_response(), task="   ").phase, Phase.BLOCKED)
        state = Orchestrator(ScriptedModel([])).run(self.root / "missing", TASK)
        self.assertEqual((state.phase, state.failure.kind), (Phase.BLOCKED, "invalid_input"))

    def test_tool_internal_error_is_terminal(self):
        from harness.tools import Tool, ToolContext, ToolRegistry
        from harness.context import ContextManager
        from harness.orchestrator.state import RunState

        def crash(ctx):
            raise RuntimeError("bug")

        ctx = ToolContext.create(self.root)
        registry = ToolRegistry(ctx)
        registry.register(Tool("crash", "crashes", {"type": "object", "properties": {}}, crash, "read"))
        state = RunState(task=TASK, repo_root=str(self.root), metrics=ctx.metrics)
        state.phase = Phase.EXECUTE
        from harness.model import MeteredModelClient
        model = MeteredModelClient(ScriptedModel([tool("crash")]), ctx.metrics)
        Executor(model, registry, ContextManager(TASK, "summary"), Limits()).execute(state)
        self.assertEqual((state.phase, state.failure.kind), (Phase.TOOL_ERROR, "tool_internal_error"))


if __name__ == "__main__":
    unittest.main()

"""End-to-end M4 runs with ScriptedModel: task -> discovery -> plan -> actions -> READY_FOR_VERIFICATION."""

import json
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
        self.assertEqual(state.phase, Phase.READY_FOR_VERIFICATION)
        self.assertNotEqual(state.phase, Phase.VERIFIED)
        self.assertEqual(state.terminal_reason, "add_one returns x + 1")
        self.assertEqual([t.target for t in state.transitions],
                         [Phase.DISCOVER, Phase.PLAN, Phase.EXECUTE, Phase.READY_FOR_VERIFICATION])
        self.assertEqual(state.modified_files, ["src/math_utils.py"])
        self.assertIn("return x + 1", (self.root / "src" / "math_utils.py").read_text())
        self.assertEqual((state.steps, state.model_calls, state.tool_calls), (3, 4, 2))
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
        state = self.run_script(plan_response(), *[tool("read_file", path="src/math_utils.py")] * 3, complete())
        self.assertEqual(state.steps, 4)
        self.assertEqual(state.model_calls, 1 + state.steps)
        self.assertEqual(state.tool_calls, 3)


class CommandObservationTest(OrchestratorCase):
    def test_failing_tests_are_a_successful_tool_call_with_a_failed_command(self):
        state = self.run_script(plan_response(), tool("run_tests", command=TEST_COMMAND), complete("not fixed yet"))
        (obs,) = state.observations
        self.assertTrue(obs.success)                   # tool invocation succeeded
        self.assertEqual(obs.outcome, "command_failed")  # test command failed
        self.assertEqual(obs.exit_code, 1)
        self.assertFalse(obs.timed_out)
        self.assertIsNone(obs.error_code)
        self.assertIn("AssertionError: 3 != 2", obs.result_summary)
        self.assertEqual(state.phase, Phase.READY_FOR_VERIFICATION)   # not TOOL_ERROR, no repair attempted
        self.assertEqual(state.metrics.command_calls, 1)
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
        self.assertEqual((state.steps, state.model_calls, state.tool_calls), (3, 4, 3))
        self.assertEqual(self.model.remaining, 2)          # no extra model call to announce it

    def test_model_call_budget(self):
        state = self.run_script(plan_response(), *[tool("read_file", path="src/math_utils.py")] * 5,
                                limits=Limits(max_model_calls=3))
        self.assertEqual(state.phase, Phase.BUDGET_EXHAUSTED)
        self.assertEqual(state.failure.details, {"budget": "model_calls", "used": 3, "limit": 3})
        self.assertEqual((state.model_calls, state.steps, state.tool_calls), (3, 2, 2))
        self.assertEqual(self.model.call_count, 3)

    def test_tool_call_budget_checked_before_dispatch(self):
        state = self.run_script(plan_response(), *[tool("read_file", path="src/math_utils.py")] * 5,
                                limits=Limits(max_tool_calls=2))
        self.assertEqual(state.phase, Phase.BUDGET_EXHAUSTED)
        self.assertEqual(state.failure.details, {"budget": "tool_calls", "used": 2, "limit": 2})
        self.assertEqual((state.tool_calls, state.model_calls, state.steps), (2, 4, 3))
        self.assertEqual(len(state.observations), 2)

    def test_completing_exactly_at_the_tool_budget_is_fine(self):
        state = self.run_script(plan_response(), tool("read_file", path="src/math_utils.py"),
                                tool("apply_patch", patch=FIX_PATCH), complete(), limits=Limits(max_tool_calls=2))
        self.assertEqual(state.phase, Phase.READY_FOR_VERIFICATION)
        self.assertEqual(state.tool_calls, 2)

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
        state = self.run_script(plan_response(), *[tool("read_file", path="src/math_utils.py")] * 30, complete(),
                                limits=Limits(max_steps=40))
        sizes = [len(r.messages[1].content) for r in self.model.requests[1:]]
        self.assertEqual(state.phase, Phase.READY_FOR_VERIFICATION)
        self.assertLess(max(sizes[22:]) - min(sizes[22:]), 200)   # windows saturated: no growth


class MalformedOutputTest(OrchestratorCase):
    def assert_terminal(self, state, phase, kind):
        self.assertEqual(state.phase, phase)
        self.assertEqual(state.failure.kind, kind)

    def test_malformed_plan(self):
        state = self.run_script(text_response("I will fix add_one."))
        self.assert_terminal(state, Phase.MODEL_ERROR, "invalid_plan")
        self.assertEqual((state.steps, state.tool_calls), (0, 0))

    def test_plan_missing_field_and_wrong_type(self):
        from tests.orchestration_helpers import plan_dict
        partial = {k: v for k, v in plan_dict().items() if k != "steps"}
        state = self.run_script(text_response(json.dumps(partial)))
        self.assert_terminal(state, Phase.MODEL_ERROR, "invalid_plan")
        self.assertIn("steps", state.failure.message)
        state = self.run_script(plan_response(files_to_inspect="src/math_utils.py"))
        self.assert_terminal(state, Phase.MODEL_ERROR, "invalid_plan")

    def test_malformed_action_json(self):
        state = self.run_script(plan_response(), text_response('{"action": "tool", "tool": "read_file",'))
        self.assert_terminal(state, Phase.MODEL_ERROR, "invalid_action")
        self.assertEqual(state.tool_calls, 0)

    def test_unknown_action(self):
        state = self.run_script(plan_response(), text_response('{"action": "shell", "command": "rm -rf /"}'))
        self.assert_terminal(state, Phase.MODEL_ERROR, "invalid_action")
        self.assertIn("unknown action 'shell'", state.failure.message)

    def test_native_malformed_arguments_are_a_structured_tool_error(self):
        state = self.run_script(plan_response(), malformed_tool_call_response("read_file", '{"path": '), complete())
        self.assertEqual(state.phase, Phase.READY_FOR_VERIFICATION)
        (obs,) = state.observations
        self.assertEqual((obs.success, obs.outcome, obs.error_code), (False, "tool_error", "invalid_arguments"))
        self.assertEqual(state.action_history[0].source, "native")

    def test_unknown_tool_is_a_structured_tool_error(self):
        state = self.run_script(plan_response(), tool("delete_everything"), complete())
        (obs,) = state.observations
        self.assertEqual((obs.success, obs.error_code), (False, "unknown_tool"))
        self.assertEqual(state.phase, Phase.READY_FOR_VERIFICATION)

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
        self.assertEqual([o.result_summary for o in state.observations][0], state.observations[1].result_summary)


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

        state = self.run_script(planner, first_action)
        self.assertEqual(checks, [True, True])
        self.assertEqual(snapshot(self.root), before)
        self.assertEqual(state.phase, Phase.READY_FOR_VERIFICATION)

    def test_planner_sees_resolved_interpreter_not_bare_python(self):
        state = self.run_script(plan_response(), complete())
        planner_input = self.model.requests[0].messages[1].content
        self.assertIn(f"- {PY} -m unittest discover -s tests -t .", planner_input)
        self.assertNotIn("- python -m unittest", planner_input)
        self.assertTrue(state.repo_profile.test_commands)

    def test_unresolved_spelling_from_summary_maps_to_resolved_command(self):
        state = self.run_script(plan_response(verification_candidates=["python -m unittest discover -s tests -t ."]),
                                complete())
        self.assertEqual(state.phase, Phase.READY_FOR_VERIFICATION)
        self.assertEqual(state.plan.verification_candidates[0].argv[0], PY)

    def test_invented_verification_command_is_rejected(self):
        state = self.run_script(plan_response(verification_candidates=["pytest -q"]))
        self.assertEqual(state.failure.kind, "invalid_plan")

    def test_chosen_verification_command_is_carried_in_the_plan(self):
        command = f"{PY} -m unittest discover -s tests -t ."
        state = self.run_script(plan_response(verification_candidates=[command]), complete())
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

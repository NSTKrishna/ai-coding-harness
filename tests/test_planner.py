import json
import shutil
import tempfile
import unittest
from pathlib import Path

from harness.config import ContextLimits
from harness.model import ScriptedModel, text_response, tool_call_response
from harness.orchestrator.plan import (
    MAX_ITEM_CHARS,
    PlanError,
    Planner,
    TaskPlan,
    build_planner_request,
    parse_plan,
    plan_to_json,
)
from harness.repo import discover_for_task
from harness.repo.commands import CommandCandidate

from tests.orchestration_helpers import TASK, buggy_repo, plan_dict
from tests.repo_fixtures import large_repo

UNITTEST = CommandCandidate("test", ("/usr/bin/python3", "-m", "unittest"), "medium", "unittest imports")


class ParsePlanTest(unittest.TestCase):
    def test_valid_plan(self):
        plan = parse_plan(json.dumps(plan_dict(verification_candidates=["/usr/bin/python3 -m unittest"])), [UNITTEST])
        self.assertIsInstance(plan, TaskPlan)
        self.assertEqual(plan.acceptance_criteria, ("add_one(1) returns 2",))
        self.assertEqual([s.kind for s in plan.steps], ["inspect", "edit"])
        self.assertEqual(plan.verification_candidates, (UNITTEST,))
        self.assertEqual(json.loads(plan_to_json(plan))["acceptance_criteria"], ["add_one(1) returns 2"])

    def test_fenced_json_is_accepted(self):
        plan = parse_plan("```json\n" + json.dumps(plan_dict()) + "\n```", [])
        self.assertEqual(plan.understanding, "add_one adds 2 instead of 1.")

    def test_single_plan_inside_prose_is_accepted(self):
        # live models often write a sentence before (or after) the plan object
        plan = parse_plan("Plan:\n" + json.dumps(plan_dict()) + "\nLet me know.", [])
        self.assertEqual(plan.understanding, "add_one adds 2 instead of 1.")

    def test_rejections(self):
        cases = {
            "not json": ("Here is my plan: fix it", "not valid JSON"),
            "json array": ("[1, 2]", "expected a JSON object"),
            "two plans": ("Plan A:\n" + json.dumps(plan_dict()) + "\nPlan B:\n" + json.dumps(plan_dict(risks=["x"])),
                          "2 JSON objects"),
            "missing field": (json.dumps({k: v for k, v in plan_dict().items() if k != "risks"}), "missing required field(s): risks"),
            "extra field": (json.dumps({**plan_dict(), "thoughts": "x"}), "unexpected field(s): thoughts"),
            "wrong type": (json.dumps(plan_dict(acceptance_criteria="add_one works")), "'acceptance_criteria' must be a list"),
            "no criteria": (json.dumps(plan_dict(acceptance_criteria=[])), "at least 1"),
            "no steps": (json.dumps(plan_dict(steps=[])), "'steps' needs"),
            "bad step kind": (json.dumps(plan_dict(steps=[{"kind": "think", "description": "x"}])), "steps[0].kind"),
            "step extra key": (json.dumps(plan_dict(steps=[{"kind": "edit", "description": "x", "why": "y"}])), "steps[0]"),
            "item too long": (json.dumps(plan_dict(risks=["x" * (MAX_ITEM_CHARS + 1)])), "longer than"),
            "non-string item": (json.dumps(plan_dict(hypotheses=[3])), "'hypotheses[0]' must be a string"),
            "invented command": (json.dumps(plan_dict(verification_candidates=["pytest"])), "'pytest' was not discovered"),
        }
        for name, (text, message) in cases.items():
            with self.subTest(case=name):
                with self.assertRaises(PlanError) as ctx:
                    parse_plan(text, [UNITTEST])
                self.assertIn(message, str(ctx.exception))


@unittest.skipUnless(shutil.which("git"), "git not installed")
class PlannerContextTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.base = Path(tmp.name).resolve()

    def test_planner_receives_bounded_working_set_not_the_repository(self):
        root = large_repo(self.base / "big", filler=300)
        limits = ContextLimits(max_context_chars=3_000)
        discovery = discover_for_task(root, "InvoiceParser.parse_total mishandles commas", limits=limits)
        request = build_planner_request("InvoiceParser.parse_total mishandles commas", discovery, [])
        user = request.messages[1].content
        self.assertIn(discovery.working_set.render(), user)
        self.assertLessEqual(len(discovery.working_set.render()), 3_000)
        self.assertLess(len(user), 3_000 + 1_000)
        self.assertIn("src/billing/invoice_parser.py", user)
        self.assertNotIn("handler_150", user)          # unrelated file contents never appear
        self.assertEqual(request.purpose, "plan")
        self.assertIsNotNone(request.response_schema)

    def test_missing_commands_are_stated_not_invented(self):
        root = buggy_repo(self.base)
        (root / "tests" / "test_math_utils.py").write_text("def test_x():\n    assert True\n")  # no unittest import
        discovery = discover_for_task(root, TASK)
        self.assertEqual(discovery.test_commands, ())
        request = build_planner_request(TASK, discovery, [])
        self.assertIn("none: no test, build or lint command", request.messages[1].content)
        self.assertNotIn("pytest", request.messages[1].content)

    def test_planner_rejects_tool_calls_and_keeps_criteria(self):
        root = buggy_repo(self.base)
        discovery = discover_for_task(root, TASK)
        with self.assertRaises(PlanError):
            Planner(ScriptedModel([tool_call_response("read_file", {"path": "x"})] * 2)).create_plan(TASK, discovery, [])
        plan = Planner(ScriptedModel([text_response(json.dumps(plan_dict()))])).create_plan(TASK, discovery, [])
        self.assertEqual(plan.acceptance_criteria, ("add_one(1) returns 2",))


if __name__ == "__main__":
    unittest.main()

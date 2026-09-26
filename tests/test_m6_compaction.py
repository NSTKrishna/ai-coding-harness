"""M6 context compaction: a long execution + repair history stays under the threshold, deterministically,
without model calls, and without losing the task, criteria, plan, current failure or repair memory."""

import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from harness.config import ContextLimits
from harness.context import ContextManager
from harness.context.facts import HistoryFact, RepairAttemptFact
from harness.model import ScriptedModel
from harness.orchestrator import Orchestrator, Phase

from tests.orchestration_helpers import (
    BUGGY_REPO, REPAIR_PATCH, TASK, UNITTEST_CMD, WRONG_PATCH, complete, plan_response, tool,
)
from tests.repo_fixtures import git_repo

TEST = [sys.executable, "-m", "unittest", "discover", "-s", "tests", "-t", "."]
THRESHOLD = 12_000


def long_script():
    script = [plan_response(verification_candidates=[UNITTEST_CMD])]
    for n in range(1, 9):   # a deliberately long, noisy history: failing test runs and file reads
        script += [tool("run_tests", command=TEST), tool("read_range", path="src/math_utils.py", start_line=1, end_line=n)]
    script += [tool("apply_patch", patch=WRONG_PATCH), complete()]
    script += [tool("read_range", path="tests/test_math_utils.py", start_line=1, end_line=8),
               tool("apply_patch", patch=REPAIR_PATCH), complete()]
    return script


@unittest.skipUnless(shutil.which("git"), "git not installed")
class CompactionIntegrationTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        tmp = tempfile.TemporaryDirectory()
        cls._tmp = tmp
        base = Path(tmp.name).resolve()
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("AI_API_KEY", None)
            cls.plain_model = ScriptedModel(long_script())
            cls.plain = Orchestrator(cls.plain_model).run(git_repo(base / "plain", BUGGY_REPO), TASK)
            cls.model = ScriptedModel(long_script())
            cls.state = Orchestrator(cls.model, context_limits=ContextLimits(compaction_threshold_chars=THRESHOLD)).run(
                git_repo(base / "compact", BUGGY_REPO), TASK)

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def sizes(self, model):
        return [len(r.messages[1].content) for r in model.requests[1:]]

    def test_history_exceeds_threshold_without_compaction(self):
        self.assertEqual(self.plain.compaction.records, [])
        self.assertGreater(max(self.sizes(self.plain_model)), THRESHOLD)

    def test_compaction_keeps_every_request_under_the_threshold(self):
        self.assertTrue(self.state.compaction.records)
        self.assertLessEqual(max(self.sizes(self.model)), THRESHOLD)
        self.assertTrue(all(r.reached_limit and r.chars_after <= THRESHOLD for r in self.state.compaction.records))
        first = self.state.compaction.records[0]
        self.assertGreater(first.chars_before, THRESHOLD)
        self.assertEqual(first.stages[0], "drop_stale")                 # cheapest stage first
        heaviest = max(self.state.compaction.records, key=lambda r: len(r.stages))
        self.assertIn("observations_to_facts", heaviest.stages)          # escalates only when needed
        self.assertGreater(heaviest.observations_compacted, 0)

    def test_same_outcome_and_no_extra_model_calls(self):
        self.assertEqual((self.state.phase, self.plain.phase), (Phase.VERIFIED, Phase.VERIFIED))
        self.assertEqual(self.state.model_calls, self.plain.model_calls)
        self.assertEqual(self.state.model_calls, 1 + self.state.steps)

    def test_essential_context_survives(self):
        snap = self.state.context_snapshot          # the last repair request
        self.assertEqual(snap.task, TASK)
        self.assertEqual(snap.acceptance_criteria, ("add_one(1) returns 2",))
        self.assertIn("Return x + 1", snap.plan_steps)
        self.assertIn("# Repair (cycle 1", snap.verification)                       # current failure retained
        self.assertIn("AssertionError: 4 != 2", snap.verification)
        self.assertIn("-    return x + 2\n+    return x + 3", snap.verification)   # failing diff retained...
        self.assertIn("Diff at verification round 1 (excerpt):", snap.verification)  # ...labelled by its round
        self.assertNotIn("Current diff", snap.verification)                          # never presented as current
        self.assertIn("NOTE: you have patched src/math_utils.py since verification round 1", snap.verification)
        kinds = {k for k, _ in snap.facts}
        self.assertTrue({"verification", "repair_attempt", "failure"} <= kinds)     # repair memory retained
        self.assertTrue(any(k == "failure" and "TASK_TEST_FAILURE" in t for k, t in snap.facts))  # not forgotten

    def test_stale_and_old_observations_are_compacted(self):
        snap = self.state.context_snapshot
        states = {how for _, _, how in snap.observations}
        self.assertIn("compacted-to-fact", states)
        repair_requests = [r.messages[1].content for r in self.model.requests if "# Repair (cycle" in r.messages[1].content]
        self.assertIn("### src/math_utils.py (current)\ndef add_one(x):\n    return x + 3", repair_requests[0])  # fresh
        self.assertNotIn("(current)", repair_requests[-1])     # invalidated again by the repair patch
        self.assertNotIn("NOTE: you have patched", repair_requests[0])   # nothing patched yet in this cycle
        for text in repair_requests:
            working = text.split("## Working context")[1].split("# Plan")[0] if "## Working context" in text else ""
            self.assertNotIn("return x + 2", working)                                 # never stale content
        record_totals = sum(r.observations_dropped for r in self.state.compaction.records)
        self.assertGreater(record_totals, 0)


class ProtectedFactTest(unittest.TestCase):
    def test_protected_facts_survive_caps_and_history_drops(self):
        cm = ContextManager("task", "repo", max_facts=5)
        cm.record(RepairAttemptFact(1, "TASK_TEST_FAILURE", "t.test_x", ("a.py",), "started"))
        for step in range(10):
            cm.record(HistoryFact(step, "read_file", "{}", "ok", ""))
        self.assertEqual([f.kind for f in cm.facts()][0], "repair_attempt")
        self.assertEqual(len(cm.facts()), 5)
        self.assertEqual(cm.drop_facts("history"), 4)
        self.assertEqual([f.kind for f in cm.facts()], ["repair_attempt"])
        with self.assertRaises(ValueError):
            cm.drop_facts("repair_attempt")
        self.assertEqual(cm.facts()[0].data["files_changed"], ("a.py",))


if __name__ == "__main__":
    unittest.main()

import os
import unittest
from unittest import mock

from harness.config import ContextLimits
from harness.context import ContextManager
from harness.context.working_set import CandidateSummary, Evidence, build_working_set


def manager(**limits):
    return ContextManager("Fix the parser", "Repository: 3 files", ContextLimits(**limits))


class ContextManagerTest(unittest.TestCase):
    def test_permanent_context_is_retained(self):
        cm = manager(max_evidence_items=2, max_context_chars=50)
        for i in range(10):
            cm.add_working_item("observation", f"cmd{i}", "x" * 30)
        self.assertEqual(cm.permanent.task, "Fix the parser")
        self.assertEqual(cm.permanent.repo_summary, "Repository: 3 files")
        self.assertIn("Fix the parser", cm.render())

    def test_working_context_is_bounded_by_count_and_chars(self):
        cm = manager(max_evidence_items=3, max_context_chars=100)
        for i in range(10):
            cm.add_working_item("observation", f"src{i}", f"{i}" * 30, priority=i)
        self.assertLessEqual(len(cm.working_items), 3)
        self.assertLessEqual(cm.working_chars, 100)
        self.assertEqual([i.source for i in cm.working_items], ["src7", "src8", "src9"])  # lowest priority evicted
        self.assertEqual(cm.evicted, 7)

    def test_equal_priority_evicts_oldest(self):
        cm = manager(max_evidence_items=2)
        for i in range(4):
            cm.add_working_item("observation", f"s{i}", f"content {i}")
        self.assertEqual([i.source for i in cm.working_items], ["s2", "s3"])

    def test_low_priority_item_rejected_when_full(self):
        cm = manager(max_evidence_items=1)
        cm.add_working_item("evidence", "a", "important", priority=10)
        self.assertIsNone(cm.add_working_item("evidence", "b", "minor", priority=1))
        self.assertEqual([i.source for i in cm.working_items], ["a"])

    def test_oversized_item_is_truncated_to_the_limit(self):
        cm = manager(max_context_chars=200)
        cm.add_working_item("observation", "big", "y" * 5_000)
        self.assertLessEqual(cm.working_chars, 200)
        self.assertIn("truncated", cm.working_items[0].content)

    def test_repeated_content_is_stored_once(self):
        cm = manager()
        first = cm.add_working_item("evidence", "a.py:1-5", "same text")
        second = cm.add_working_item("evidence", "a.py:1-5", "same text")
        self.assertEqual(first, second)
        self.assertEqual(len(cm.working_items), 1)

    def test_remove_source(self):
        cm = manager()
        cm.add_working_item("evidence", "a.py:1-5", "one")
        cm.add_working_item("evidence", "a.py:20-25", "two")
        cm.add_working_item("evidence", "b.py:1-5", "three")
        self.assertEqual(cm.remove_source("a.py"), 2)
        self.assertEqual([i.source for i in cm.working_items], ["b.py:1-5"])

    def test_episodic_facts(self):
        cm = ContextManager("t", "s", max_facts=3)
        cm.record_fact("baseline", "3 tests failing", source="run_tests")
        cm.record_fact("baseline", "3 tests failing")  # duplicate ignored
        cm.record_fact("decision", "edit parser.py")
        self.assertEqual([f.text for f in cm.facts()], ["3 tests failing", "edit parser.py"])
        self.assertEqual([f.text for f in cm.facts("decision")], ["edit parser.py"])
        for i in range(3):
            cm.record_fact("note", f"n{i}")
        self.assertEqual(len(cm.facts()), 3)
        self.assertEqual(cm.facts_dropped, 2)
        self.assertIn("[note] n2", cm.render())

    def test_loads_working_set_evidence(self):
        ws = build_working_set("s", [CandidateSummary("a.py", 50, ("r",))],
                               [Evidence("a.py", 3, 4, "x = 1\ny = 2", "defines x", "x", 50.0)], ContextLimits())
        cm = manager()
        cm.load_working_set(ws)
        self.assertEqual([(i.source, i.content) for i in cm.working_items], [("a.py:3-4", "x = 1\ny = 2")])

    def test_no_model_or_key_needed(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("AI_API_KEY", None)
            cm = manager()
            cm.add_working_item("observation", "x", "y")
            self.assertTrue(cm.render())


if __name__ == "__main__":
    unittest.main()

import shutil
import tempfile
import unittest
from pathlib import Path

from harness.config import ContextLimits
from harness.context.working_set import MAX_LINE_CHARS, CandidateSummary, Evidence, build_working_set, make_snippet
from harness.repo import discover_for_task

from tests.repo_fixtures import PYTHON_REPO, git_repo, large_repo


def ev(path, start, value, lines=5, text="x"):
    return Evidence(path, start, start + lines - 1, "\n".join([text] * lines), f"reason {path}:{start}", "sig", value)


CANDIDATES = [CandidateSummary(f"f{i}.py", 100 - i, (f"reason {i}",)) for i in range(10)]
POOL = [ev(f"f{i}.py", 1 + 10 * j, value=100 - i - j) for i in range(10) for j in range(4)]


class BuildWorkingSetTest(unittest.TestCase):
    def test_max_active_files(self):
        ws = build_working_set("summary", CANDIDATES, POOL, ContextLimits(max_active_files=3))
        self.assertEqual(ws.selected_files, ("f0.py", "f1.py", "f2.py"))
        self.assertTrue(all(e.path in ws.selected_files for e in ws.evidence))

    def test_max_evidence_items_keeps_highest_value(self):
        ws = build_working_set("summary", CANDIDATES, POOL, ContextLimits(max_evidence_items=4))
        self.assertEqual(len(ws.evidence), 4)
        # values are 100 - file - snippet: the top four are 100, 99 (f0 #2), 99 (f1 #1), 98
        self.assertEqual(sorted(e.value for e in ws.evidence), [98, 99, 99, 100])
        self.assertEqual(ws.evidence_omitted, 8 * 4 - 4)  # 8 active files x 4 snippets

    def test_max_context_chars_is_never_exceeded(self):
        for budget in (80, 200, 500, 1_000, 3_000, 10_000):
            with self.subTest(budget=budget):
                ws = build_working_set("S" * 400, CANDIDATES, POOL, ContextLimits(max_context_chars=budget))
                self.assertLessEqual(len(ws.render()), budget)
                self.assertEqual(ws.estimated_chars, len(ws.render()))

    def test_tight_budget_drops_candidates_then_cuts_summary(self):
        ws = build_working_set("S" * 400, CANDIDATES, POOL, ContextLimits(max_context_chars=150))
        self.assertEqual(ws.candidates, ())
        self.assertEqual(ws.candidates_omitted, 10)
        self.assertTrue(ws.summary_truncated)
        self.assertEqual(ws.evidence, ())

    def test_max_candidates_listed(self):
        ws = build_working_set("s", CANDIDATES, [], ContextLimits(max_candidates=4))
        self.assertEqual(len(ws.candidates), 4)
        self.assertIn("6 more not shown", ws.render())

    def test_oversized_snippets_are_rejected(self):
        ws = build_working_set("s", CANDIDATES[:1], [ev("f0.py", 1, 100, lines=50)], ContextLimits(max_snippet_lines=30))
        self.assertEqual(ws.evidence, ())

    def test_deterministic_reading_order(self):
        ws = build_working_set("s", CANDIDATES, list(reversed(POOL)), ContextLimits())
        again = build_working_set("s", CANDIDATES, POOL, ContextLimits())
        self.assertEqual(ws.evidence, again.evidence)
        order = [(ws.selected_files.index(e.path), e.line_start) for e in ws.evidence]
        self.assertEqual(order, sorted(order))

    def test_snippet_lines_are_cut(self):
        snippet = make_snippet(["a" * 1000 + "\n", "short\n"], 1, 2)
        first, second = snippet.split("\n")
        self.assertEqual(len(first), MAX_LINE_CHARS)
        self.assertEqual(second, "short")


@unittest.skipUnless(shutil.which("git"), "git not installed")
class DiscoveryWorkingSetTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.base = Path(tmp.name).resolve()

    def test_snippets_bounded_and_centred_on_evidence(self):
        root = git_repo(self.base / "py", PYTHON_REPO)
        limits = ContextLimits(max_snippet_lines=4)
        ws = discover_for_task(root, "Fix PaymentService refresh_token behavior", limits=limits).working_set
        self.assertTrue(ws.evidence)
        for e in ws.evidence:
            self.assertLessEqual(e.line_end - e.line_start + 1, 4)
            self.assertLessEqual(len(e.snippet.split("\n")), 4)
        service = [e for e in ws.evidence if e.path == "src/payment/service.py"]
        self.assertTrue(any(e.line_start <= 4 <= e.line_end for e in service))  # class PaymentService

    def test_real_discovery_respects_limits(self):
        root = large_repo(self.base / "big", filler=80)
        limits = ContextLimits(max_active_files=2, max_evidence_items=3, max_context_chars=1_500, max_candidates=5)
        result = discover_for_task(root, "InvoiceParser parse_total handler_1 handler_2 handler_3", limits=limits)
        ws = result.working_set
        self.assertLessEqual(len(ws.selected_files), 2)
        self.assertLessEqual(len(ws.evidence), 3)
        self.assertLessEqual(len(ws.candidates), 5)
        self.assertLessEqual(len(ws.render()), 1_500)
        self.assertEqual(result.metrics.working_set_chars, len(ws.render()))


if __name__ == "__main__":
    unittest.main()

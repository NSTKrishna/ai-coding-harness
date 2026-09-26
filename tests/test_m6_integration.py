"""M6 final integration (offline, ScriptedModel) and the large-repository scale check."""

import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from harness.config import ContextLimits
from harness.model import ScriptedModel
from harness.orchestrator import Orchestrator, Phase
from harness.telemetry import RunRecorder
from harness.verify import Comparison, EvidenceKind

from tests.helpers import FAKE_KEY
from tests.orchestration_helpers import BUGGY_REPO, FIX_PATCH, TASK, complete, plan_response, tool
from tests.repo_fixtures import git_repo


@unittest.skipUnless(shutil.which("git"), "git not installed")
class FinalIntegrationTest(unittest.TestCase):
    """TASK -> discovery -> plan -> targeted test derived -> baseline targeted FAIL -> edit ->
    targeted PASS -> broader suite PASS -> VERIFIED -> artifacts."""

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        base = Path(cls._tmp.name).resolve()
        with mock.patch.dict(os.environ, {"AI_API_KEY": FAKE_KEY}):
            cls.root = git_repo(base / "repo", BUGGY_REPO)
            cls.model = ScriptedModel([plan_response(), tool("read_file", path="src/math_utils.py"),
                                       tool("apply_patch", patch=FIX_PATCH), complete("add_one returns x + 1")])
            recorder = RunRecorder(base / "runs")
            cls.state = Orchestrator(cls.model, recorder=recorder).run(cls.root, TASK)
            cls.run_dir = recorder.run_dir

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def test_verified_with_targeted_then_broad_evidence(self):
        s = self.state
        self.assertEqual(s.phase, Phase.VERIFIED)
        t1, v1 = s.verification_commands
        self.assertEqual((t1.level, v1.level), ("targeted", "suite"))
        self.assertIn("tests.test_math_utils.AddOneTest.test_add_one", t1.text)
        tests = [(i.phase, i.command_id, i.result) for i in s.evidence.of_kind(EvidenceKind.TEST)]
        self.assertEqual(tests, [("baseline", "T1", "TEST_FAILURE"), ("baseline", "V1", "TEST_FAILURE"),
                                 ("post-1", "T1", "PASS"), ("post-1", "V1", "PASS")])
        comparisons = {r.command.id: r.comparison for r in s.last_report.command_results}
        self.assertEqual(comparisons, {"T1": Comparison.FIXED, "V1": Comparison.FIXED})
        (criterion,) = s.last_report.criteria_results
        self.assertEqual(criterion.status, "PASS")
        for eid in criterion.evidence_ids:
            self.assertIsNotNone(s.evidence.get(eid))

    def test_counters_and_bounded_context(self):
        s = self.state
        self.assertEqual((s.model_calls, s.steps, s.repair_cycles), (4, 3, 0))
        self.assertEqual(s.tool_calls, 7 + 2 + 5)     # baseline, read + patch, verification round
        limit = ContextLimits().compaction_threshold_chars
        self.assertTrue(all(len(r.messages[1].content) <= limit for r in self.model.requests))

    def test_artifacts_valid_and_secret_free(self):
        files = {p.name: p.read_text(encoding="utf-8") for p in self.run_dir.iterdir()}
        summary = json.loads(files["summary.json"])
        self.assertEqual(summary["final_status"], "VERIFIED")
        self.assertEqual(summary["targeting"]["targets"][0]["parent_command_id"], "V1")
        self.assertEqual([c["comparison"] for c in summary["verification_summary"][0]["commands"]], ["FIXED", "FIXED"])
        for line in files["events.jsonl"].splitlines():
            json.loads(line)
        report = files["final_report.md"]
        self.assertTrue(report.startswith("# TASK RESULT: VERIFIED"))
        self.assertIn("## Targeted verification", report)
        self.assertIn("## Resource usage", report)
        for text in files.values():
            self.assertNotIn(FAKE_KEY, text)


def scale_repo(root: Path, filler: int = 1_000) -> Path:
    files = dict(BUGGY_REPO)
    files["src/filler/__init__.py"] = ""
    for i in range(filler):
        files[f"src/filler/mod_{i:04d}.py"] = f"def handler_{i}(value):\n    return value + {i}\n"
    return git_repo(root, files)


@unittest.skipUnless(shutil.which("git"), "git not installed")
class ScaleTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("AI_API_KEY", None)
            cls.root = scale_repo(Path(cls._tmp.name).resolve() / "big")
            cls.model = ScriptedModel([plan_response(), tool("apply_patch", patch=FIX_PATCH), complete()])
            cls.state = Orchestrator(cls.model).run(cls.root, TASK)

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def test_inventory_sees_everything_but_reads_selectively(self):
        m = self.state.discovery.metrics
        self.assertGreaterEqual(m.inventory_files, 1_000)
        self.assertLessEqual(m.discovery_files_read, 10)
        self.assertLess(m.analysis_files_read + m.discovery_files_read, 0.02 * m.inventory_files)
        total = sum(p.stat().st_size for p in self.root.rglob("*.py"))
        self.assertLess(m.bytes_read, 0.05 * total)                       # no whole-repository read

    def test_working_set_and_requests_stay_bounded(self):
        limits = ContextLimits()
        ws = self.state.working_set
        self.assertLessEqual(ws.estimated_chars, limits.max_context_chars)
        self.assertLessEqual(len(ws.selected_files), limits.max_active_files)
        self.assertNotIn("handler_500", self.model.requests[0].messages[1].content)
        self.assertTrue(all(len(r.messages[1].content) <= limits.compaction_threshold_chars for r in self.model.requests))

    def test_still_verified(self):
        self.assertEqual(self.state.phase, Phase.VERIFIED)


if __name__ == "__main__":
    unittest.main()

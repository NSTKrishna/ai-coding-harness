"""M6 run artifacts (events.jsonl, summary.json, final_report.md, final.diff) and the runs/report CLI."""

import io
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from harness.cli import EXIT_OK, EXIT_USAGE, main
from harness.model import ScriptedModel
from harness.orchestrator import Orchestrator, Phase
from harness.telemetry import MAX_META_CHARS, RunRecorder, resolve_runs_dir

from tests.helpers import FAKE_KEY
from tests.orchestration_helpers import (
    BUGGY_REPO, FIX_PATCH, NO_COMMAND_REPO, TASK, complete, plan_response, tool,
)
from tests.repo_fixtures import git_repo, snapshot

NO_DOTENV = Path("/nonexistent/harness/.env")


@unittest.skipUnless(shutil.which("git"), "git not installed")
class TelemetryCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.base = Path(tmp.name).resolve()
        self.runs = self.base / "runs"
        patcher = mock.patch.dict(os.environ, {"AI_API_KEY": FAKE_KEY})   # present, must never be written
        patcher.start()
        self.addCleanup(patcher.stop)

    def run_task(self, files, *script, name="repo", untracked=None):
        self.root = git_repo(self.base / name, files, untracked)
        recorder = RunRecorder(self.runs, redact=lambda t: t.replace(FAKE_KEY, "***"))
        self.state = Orchestrator(ScriptedModel(list(script)), recorder=recorder).run(self.root, TASK)
        self.run_dir = recorder.run_dir
        return self.state

    def artifacts(self):
        return {p.name: p.read_text(encoding="utf-8") for p in self.run_dir.iterdir()}


class ArtifactTest(TelemetryCase):
    def setUp(self):
        super().setUp()
        (self.base / "notes").mkdir()
        self.run_task(BUGGY_REPO, plan_response(), tool("read_file", path="src/math_utils.py"),
                      tool("apply_patch", patch=FIX_PATCH), complete(), untracked={"scratch.txt": "user notes\n"})

    def test_files_exist_and_parse(self):
        files = self.artifacts()
        self.assertEqual(set(files), {"events.jsonl", "summary.json", "final_report.md", "final.diff"})
        events = [json.loads(line) for line in files["events.jsonl"].splitlines()]
        summary = json.loads(files["summary.json"])
        self.assertEqual(events[0]["event"], "run_started")
        self.assertEqual(events[-1]["event"], "run_finished")
        self.assertEqual([e["seq"] for e in events], list(range(1, len(events) + 1)))
        for e in events:
            self.assertEqual(e["run_id"], self.state.run_id)
            self.assertTrue({"ts", "phase", "event"} <= set(e))
        kinds = {e["event"] for e in events}
        self.assertTrue({"phase_changed", "discovery_completed", "model_call_started", "model_call_completed",
                         "tool_call_started", "tool_call_completed", "baseline_completed",
                         "verification_completed"} <= kinds)
        self.assertTrue(files["final_report.md"].startswith("# TASK RESULT: VERIFIED"))
        self.assertEqual(summary["final_status"], "VERIFIED")

    def test_counters_match_run_state(self):
        summary = json.loads(self.artifacts()["summary.json"])
        s = self.state
        self.assertEqual((summary["model_calls"], summary["tool_calls"], summary["steps"], summary["repair_cycles"]),
                         (s.model_calls, s.tool_calls, s.steps, s.repair_cycles))
        events = [json.loads(l) for l in self.artifacts()["events.jsonl"].splitlines()]
        self.assertEqual(sum(e["event"] == "model_call_completed" for e in events), s.model_calls)
        self.assertEqual(sum(e["event"] == "tool_call_completed" for e in events), s.tool_calls)
        self.assertEqual(summary["modified_files"], ["src/math_utils.py"])
        self.assertEqual(summary["verification_command_calls"], 4)       # T1 + V1, baseline and post

    def test_evidence_references_are_valid(self):
        summary = json.loads(self.artifacts()["summary.json"])
        ids = {i["id"] for i in summary["evidence_summary"]["items"]}
        referenced = {e for c in summary["acceptance_criteria"] for e in c["evidence_ids"]}
        referenced |= {e for r in summary["verification_summary"] for c in r["commands"] for e in c["evidence_ids"]}
        self.assertTrue(referenced)
        self.assertLessEqual(referenced, ids)
        self.assertEqual(summary["acceptance_criteria"][0]["status"], "PASS")

    def test_no_secret_anywhere_and_bounded(self):
        for name, text in self.artifacts().items():
            with self.subTest(file=name):
                self.assertNotIn(FAKE_KEY, text)
        for line in self.artifacts()["events.jsonl"].splitlines():
            for value in json.loads(line).values():
                if isinstance(value, str):
                    self.assertLessEqual(len(value), MAX_META_CHARS)
        self.assertLess(sum(len(t) for t in self.artifacts().values()), 200_000)
        self.assertNotIn("# Plan", self.artifacts()["events.jsonl"])          # no prompts recorded

    def test_target_repository_only_has_the_intended_change(self):
        status = subprocess.run(["git", "status", "--porcelain"], cwd=self.root, capture_output=True, text=True).stdout
        self.assertEqual(sorted(status.splitlines()), [" M src/math_utils.py", "?? scratch.txt"])
        diff = self.artifacts()["final.diff"]
        self.assertIn("Harness-attributed changes", diff)
        self.assertIn("+    return x + 1", diff)
        self.assertIn("Pre-existing changes NOT made by this run (not included): scratch.txt", diff)
        self.assertNotIn("user notes", diff)


class RunsDirTest(TelemetryCase):
    def test_runs_dir_inside_target_is_relocated(self):
        root = git_repo(self.base / "target", NO_COMMAND_REPO)
        chosen, note = resolve_runs_dir(root / ".harness" / "runs", root)
        self.assertNotIn(root, chosen.parents)
        self.assertIn("inside the target repository", note)
        chosen, note = resolve_runs_dir(self.runs, root)
        self.assertEqual((chosen, note), (self.runs.resolve(), None))

    def test_no_artifacts_without_a_recorder(self):
        root = git_repo(self.base / "quiet", BUGGY_REPO)
        before = snapshot(root)
        Orchestrator(ScriptedModel([plan_response(), complete()])).run(root, TASK)
        self.assertFalse(self.runs.exists())
        self.assertEqual(snapshot(root), before)


class RunsCliTest(TelemetryCase):
    def cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        code = main(list(argv), environ={"HARNESS_RUNS_DIR": str(self.runs)}, dotenv_path=NO_DOTENV,
                    stdout=out, stderr=err)
        return code, out.getvalue(), err.getvalue()

    def test_runs_and_report_without_api_key(self):
        self.run_task(BUGGY_REPO, plan_response(), tool("apply_patch", patch=FIX_PATCH), complete())
        before = snapshot(self.root)
        with mock.patch("harness.model.factory.create_model_client") as factory:
            code, out, err = self.cli("runs")
            self.assertEqual(code, EXIT_OK, err)
            self.assertIn(self.state.run_id, out)
            self.assertIn("VERIFIED", out)
            code, out, err = self.cli("report", self.state.run_id)
            self.assertEqual(code, EXIT_OK, err)
            self.assertTrue(out.startswith("# TASK RESULT: VERIFIED"))
            code, out, _ = self.cli("report", self.state.run_id, "--json")
            self.assertEqual(json.loads(out)["run_id"], self.state.run_id)
            factory.assert_not_called()
        self.assertEqual(snapshot(self.root), before)

    def test_errors(self):
        self.assertEqual(self.cli("report", "../../etc")[0], EXIT_USAGE)
        self.assertEqual(self.cli("report", "missing123")[0], EXIT_USAGE)
        code, out, _ = self.cli("runs")
        self.assertEqual(code, EXIT_OK)
        self.assertIn("No runs recorded", out)


if __name__ == "__main__":
    unittest.main()

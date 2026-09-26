import io
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from harness.cli import EXIT_OK, EXIT_USAGE, main

from tests.helpers import FAKE_KEY
from tests.repo_fixtures import PYTHON_REPO, git_repo, snapshot

PROJECT_ROOT = Path(__file__).resolve().parent.parent
NO_DOTENV = Path("/nonexistent/harness/.env")


@unittest.skipUnless(shutil.which("git"), "git not installed")
class InspectCliTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = git_repo(Path(tmp.name).resolve() / "py", PYTHON_REPO)

    def run_cli(self, argv, environ=None):
        out, err = io.StringIO(), io.StringIO()
        code = main(argv, environ={} if environ is None else environ, dotenv_path=NO_DOTENV,
                    stdout=out, stderr=err)
        return code, out.getvalue(), err.getvalue()

    def test_profile_without_api_key(self):
        code, out, err = self.run_cli(["inspect", "--repo", str(self.root)])
        self.assertEqual(code, EXIT_OK, err)
        self.assertIn(f"Repository: {self.root}", out)
        self.assertIn("python -m pytest  [high] pytest is configured in pyproject.toml [tool.pytest.ini_options]", out)
        self.assertNotIn("Candidate files", out)

    def test_task_discovery_output(self):
        code, out, err = self.run_cli(["inspect", "--repo", str(self.root), "--task",
                                       "Fix PaymentService refresh_token behavior", "--top", "3"])
        self.assertEqual(code, EXIT_OK, err)
        self.assertIn("identifiers:  PaymentService, refresh_token", out)
        self.assertIn("Candidate files (top 3 of", out)
        self.assertIn(" * 1. src/payment/service.py", out)
        self.assertIn("- defines PaymentService (line 4)", out)
        self.assertIn("Discovery: 10 files in inventory", out)

    def test_output_is_deterministic(self):
        args = ["inspect", "--repo", str(self.root), "--task", "Fix PaymentService refresh_token behavior",
                "--show-context"]
        self.assertEqual(self.run_cli(args)[1], self.run_cli(args)[1])
        self.assertIn("--- working set ---", self.run_cli(args)[1])

    def test_repository_unchanged(self):
        before = snapshot(self.root)
        self.run_cli(["inspect", "--repo", str(self.root)])
        self.run_cli(["inspect", "--repo", str(self.root), "--task", "Fix PaymentService", "--show-context"])
        self.assertEqual(snapshot(self.root), before)

    def test_errors(self):
        self.assertEqual(self.run_cli(["inspect", "--repo", str(self.root / "missing")])[0], EXIT_USAGE)
        self.assertEqual(self.run_cli(["inspect", "--repo", str(self.root), "--task", "  "])[0], EXIT_USAGE)
        code, _, err = self.run_cli(["inspect", "--repo", str(self.root)], environ={"HARNESS_MAX_CONTEXT_CHARS": "0"})
        self.assertEqual(code, EXIT_USAGE)
        self.assertIn("HARNESS_MAX_CONTEXT_CHARS", err)

    def test_key_in_repository_content_is_redacted(self):
        (self.root / "src" / "payment" / "leak.py").write_text(f"PaymentService_KEY = '{FAKE_KEY}'\n")
        _, out, _ = self.run_cli(["inspect", "--repo", str(self.root), "--task", "PaymentService", "--show-context"],
                                 environ={"AI_API_KEY": FAKE_KEY})
        self.assertNotIn(FAKE_KEY, out)

    def test_module_entry_point_without_key(self):
        env = {k: v for k, v in os.environ.items() if not k.startswith(("AI_", "HARNESS_"))}
        env["PYTHONPATH"] = str(PROJECT_ROOT / "src")
        result = subprocess.run([sys.executable, "-m", "harness", "inspect", "--repo", str(self.root),
                                 "--task", "Fix PaymentService refresh_token behavior"],
                                capture_output=True, text=True, cwd=self.root.parent, env=env, timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("src/payment/service.py", result.stdout)


if __name__ == "__main__":
    unittest.main()

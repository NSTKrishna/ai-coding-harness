import contextlib
import io
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from harness import __version__
from harness.cli import EXIT_OK, EXIT_USAGE, main

PROJECT_ROOT = Path(__file__).resolve().parent.parent
FAKE_KEY = "test-key-7f3a9c1e5b"
NO_DOTENV = Path("/nonexistent/harness/.env")


class CliTestCase(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.tmp = Path(directory.name)
        self.repo = self.tmp / "repo"
        self.repo.mkdir()

    def run_cli(self, argv, stdin_text="", environ=None):
        stdout, stderr = io.StringIO(), io.StringIO()
        code = main(
            argv,
            environ={"AI_API_KEY": FAKE_KEY} if environ is None else environ,
            dotenv_path=NO_DOTENV,
            stdin=io.StringIO(stdin_text),
            stdout=stdout,
            stderr=stderr,
        )
        out, err = stdout.getvalue(), stderr.getvalue()
        self.assertNotIn(FAKE_KEY, out)
        self.assertNotIn(FAKE_KEY, err)
        return code, out, err

    def assert_input_accepted(self, code, out, err):
        """Input is accepted and reported; execution then stops because this build has no
        live model adapter (M4). Nothing is claimed to have run."""
        self.assertIn("Input accepted.", out, err)
        self.assertEqual(code, EXIT_USAGE, err)
        self.assertIn("No model provider is configured", err)
        self.assertIn("No model was called and the repository was not modified.", err)


class RunArgumentsTest(CliTestCase):
    def test_repo_and_task_arguments(self):
        code, out, err = self.run_cli(["run", "--repo", str(self.repo), "--task", "Fix the parser bug"])
        self.assert_input_accepted(code, out, err)
        self.assertIn("Configuration accepted.", out)
        self.assertIn("API key:         set (redacted)", out)
        self.assertIn(str(self.repo.resolve()), out)
        self.assertIn("Task source:     argument", out)
        self.assertIn("Fix the parser bug", out)
        self.assertNotIn("Repository path:", out)

    def test_task_file_argument(self):
        task_file = self.tmp / "issue.md"
        task_file.write_text("Title line\n\nBody line\n", encoding="utf-8")
        code, out, err = self.run_cli(["run", "--repo", str(self.repo), "--task-file", str(task_file)])
        self.assert_input_accepted(code, out, err)
        self.assertIn(f"Task source:     file {task_file}", out)
        self.assertIn("Title line (3 line(s)", out)

    def test_task_and_task_file_are_mutually_exclusive(self):
        with self.assertRaises(SystemExit) as ctx, contextlib.redirect_stderr(io.StringIO()):
            main(["run", "--repo", str(self.repo), "--task", "x", "--task-file", "y"],
                 environ={"AI_API_KEY": FAKE_KEY}, dotenv_path=NO_DOTENV)
        self.assertEqual(ctx.exception.code, EXIT_USAGE)

    def test_missing_api_key_fails_before_prompting(self):
        code, out, err = self.run_cli(["run"], stdin_text=f"{self.repo}\ntask\n\n", environ={})
        self.assertEqual(code, EXIT_USAGE)
        self.assertIn("AI_API_KEY is not set", err)
        self.assertEqual(out, "")

    def test_nonexistent_repo(self):
        code, _, err = self.run_cli(["run", "--repo", str(self.tmp / "missing"), "--task", "x"])
        self.assertEqual(code, EXIT_USAGE)
        self.assertIn("repository path does not exist", err)

    def test_repo_that_is_a_file(self):
        file_path = self.tmp / "file.txt"
        file_path.write_text("x", encoding="utf-8")
        code, _, err = self.run_cli(["run", "--repo", str(file_path), "--task", "x"])
        self.assertEqual(code, EXIT_USAGE)
        self.assertIn("not a directory", err)

    def test_blank_task(self):
        code, _, err = self.run_cli(["run", "--repo", str(self.repo), "--task", "   "])
        self.assertEqual(code, EXIT_USAGE)
        self.assertIn("task must not be empty", err)

    def test_missing_task_file(self):
        code, _, err = self.run_cli(["run", "--repo", str(self.repo), "--task-file", str(self.tmp / "nope.md")])
        self.assertEqual(code, EXIT_USAGE)
        self.assertIn("task file does not exist", err)

    def test_empty_task_file(self):
        task_file = self.tmp / "empty.md"
        task_file.write_text("\n\n", encoding="utf-8")
        code, _, err = self.run_cli(["run", "--repo", str(self.repo), "--task-file", str(task_file)])
        self.assertEqual(code, EXIT_USAGE)
        self.assertIn("task must not be empty", err)

    def test_no_subcommand_prints_help(self):
        code, _, err = self.run_cli([])
        self.assertEqual(code, EXIT_USAGE)
        self.assertIn("usage: harness", err)

    def test_api_key_in_task_text_is_redacted(self):
        code, out, err = self.run_cli(["run", "--repo", str(self.repo), "--task", f"leak {FAKE_KEY}"])
        self.assert_input_accepted(code, out, err)
        self.assertIn("leak ***", out)

    @unittest.skipUnless(shutil.which("git"), "git not installed")
    def test_reports_git_repository(self):
        subprocess.run(["git", "init", "-q", str(self.repo)], check=True)
        code, out, err = self.run_cli(["run", "--repo", str(self.repo), "--task", "x"])
        self.assert_input_accepted(code, out, err)
        self.assertIn("(git repository)", out)

    def test_reports_non_git_directory(self):
        code, out, err = self.run_cli(["run", "--repo", str(self.repo), "--task", "x"])
        self.assert_input_accepted(code, out, err)
        self.assertIn("not a git repository", out)


class InteractiveTest(CliTestCase):
    def test_prompts_for_repo_and_multiline_task(self):
        stdin_text = f"{self.repo}\nFirst line of issue\nSecond line\n\nignored after blank\n"
        code, out, err = self.run_cli(["run"], stdin_text=stdin_text)
        self.assert_input_accepted(code, out, err)
        self.assertIn("Repository path: ", out)
        self.assertIn("Task / GitHub issue", out)
        self.assertIn("Task source:     prompt", out)
        self.assertIn("First line of issue (2 line(s)", out)

    def test_prompts_only_for_what_is_missing(self):
        code, out, err = self.run_cli(["run", "--repo", str(self.repo)], stdin_text="Do the thing\n")
        self.assert_input_accepted(code, out, err)
        self.assertNotIn("Repository path:", out)
        self.assertIn("Task / GitHub issue", out)

    def test_end_of_input_without_repo(self):
        code, _, err = self.run_cli(["run"], stdin_text="")
        self.assertEqual(code, EXIT_USAGE)
        self.assertIn("no input received for 'Repository path'", err)

    def test_end_of_input_without_task(self):
        code, _, err = self.run_cli(["run"], stdin_text=f"{self.repo}\n")
        self.assertEqual(code, EXIT_USAGE)
        self.assertIn("task must not be empty", err)


class ModuleEntryPointTest(CliTestCase):
    """Runs ``python -m harness`` as a real subprocess."""

    def run_module(self, *args, env_overrides=None, stdin_text=""):
        env = {k: v for k, v in os.environ.items() if not k.startswith(("AI_", "HARNESS_"))}
        env["PYTHONPATH"] = str(PROJECT_ROOT / "src")
        env.update(env_overrides or {})
        result = subprocess.run(
            [sys.executable, "-m", "harness", *args],
            input=stdin_text,
            capture_output=True,
            text=True,
            cwd=self.tmp,  # no .env here
            env=env,
            timeout=60,
        )
        self.assertNotIn(FAKE_KEY, result.stdout)
        self.assertNotIn(FAKE_KEY, result.stderr)
        return result

    def test_version(self):
        result = self.run_module("--version")
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout.strip(), f"harness {__version__}")

    def test_run_with_arguments(self):
        result = self.run_module("run", "--repo", str(self.repo), "--task", "x",
                                 env_overrides={"AI_API_KEY": FAKE_KEY})
        self.assertEqual(result.returncode, EXIT_USAGE, result.stderr)
        self.assertIn("Input accepted.", result.stdout)
        self.assertIn("No model provider is configured", result.stderr)

    def test_run_interactive_via_stdin(self):
        result = self.run_module("run", env_overrides={"AI_API_KEY": FAKE_KEY},
                                 stdin_text=f"{self.repo}\nFix it\n\n")
        self.assertEqual(result.returncode, EXIT_USAGE, result.stderr)
        self.assertIn("Task source:     prompt", result.stdout)
        self.assertIn("No model provider is configured", result.stderr)

    def test_run_without_api_key(self):
        result = self.run_module("run", "--repo", str(self.repo), "--task", "x")
        self.assertEqual(result.returncode, EXIT_USAGE)
        self.assertIn("AI_API_KEY is not set", result.stderr)


class RunExecutionTest(CliTestCase):
    """`harness run` with a model injected in place of the (absent) live adapter."""

    def setUp(self):
        super().setUp()
        from tests.orchestration_helpers import FIX_PATCH, buggy_repo, complete, plan_response, tool
        self.repo = buggy_repo(self.tmp)
        self.script = [plan_response(), tool("apply_patch", patch=FIX_PATCH), complete("fixed add_one")]

    def run_with_model(self, script, provider="acme"):
        from unittest import mock
        from harness.model import ScriptedModel
        self.runs_dir = self.tmp / "runs"      # never the project's own .harness/ during tests
        with mock.patch("harness.model.factory.create_model_client", return_value=ScriptedModel(script)):
            return self.run_cli(["run", "--repo", str(self.repo), "--task", "Fix add_one"],
                                environ={"AI_API_KEY": FAKE_KEY, "AI_MODEL_PROVIDER": provider,
                                         "HARNESS_RUNS_DIR": str(self.runs_dir)})

    def test_run_is_verified_only_by_evidence(self):
        code, out, err = self.run_with_model(self.script)
        self.assertEqual(code, EXIT_OK, err)
        self.assertIn("TASK RESULT: VERIFIED", out)
        self.assertIn("Files changed by this run: src/math_utils.py", out)
        self.assertIn("TEST_FAILURE", out)       # baseline failed
        self.assertIn("FIXED", out)              # and the same command passes afterwards
        self.assertIn("Executor completion claim (not evidence): fixed add_one", out)
        (run_dir,) = [d.resolve() for d in self.runs_dir.iterdir()]
        self.assertIn(f"Run artifacts: {run_dir}", out)
        self.assertEqual({p.name for p in run_dir.iterdir()},
                         {"events.jsonl", "summary.json", "final_report.md", "final.diff"})

    def test_unverified_run_exit_code_and_wording(self):
        from harness.model import text_response
        from tests.orchestration_helpers import complete, plan_response
        (self.repo / "tests" / "test_math_utils.py").write_text("def test_x():\n    pass\n")  # no command discovered
        code, out, _ = self.run_with_model([plan_response(), *self.script[1:]])
        self.assertEqual(code, 1)
        self.assertIn("TASK RESULT: UNVERIFIED", out)
        self.assertIn("correctness could NOT be established", out)
        self.assertNotIn("[pass]", out)

    def test_incomplete_run_exit_code(self):
        from harness.model import text_response
        code, out, _ = self.run_with_model([text_response("not a plan")])
        self.assertEqual(code, 1)
        self.assertIn("MODEL_ERROR", out)
        self.assertIn("invalid_plan", out)

    def test_unsupported_provider_is_reported_precisely(self):
        code, out, err = self.run_cli(["run", "--repo", str(self.repo), "--task", "Fix add_one"],
                                      environ={"AI_API_KEY": FAKE_KEY, "AI_MODEL_PROVIDER": "acme"})
        self.assertEqual(code, EXIT_USAGE)
        self.assertIn("Configured model provider is not supported by this build: 'acme'", err)
        self.assertIn("harness inspect", err)
        self.assertIn("return x + 2", (self.repo / "src" / "math_utils.py").read_text())


if __name__ == "__main__":
    unittest.main()

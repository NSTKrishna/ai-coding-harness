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


class RunArgumentsTest(CliTestCase):
    def test_repo_and_task_arguments(self):
        code, out, err = self.run_cli(["run", "--repo", str(self.repo), "--task", "Fix the parser bug"])
        self.assertEqual(code, EXIT_OK, err)
        self.assertIn("Configuration accepted.", out)
        self.assertIn("API key:         set (redacted)", out)
        self.assertIn(str(self.repo.resolve()), out)
        self.assertIn("Task source:     argument", out)
        self.assertIn("Fix the parser bug", out)
        self.assertIn("not implemented yet", out)
        self.assertNotIn("Repository path:", out)

    def test_task_file_argument(self):
        task_file = self.tmp / "issue.md"
        task_file.write_text("Title line\n\nBody line\n", encoding="utf-8")
        code, out, err = self.run_cli(["run", "--repo", str(self.repo), "--task-file", str(task_file)])
        self.assertEqual(code, EXIT_OK, err)
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
        self.assertEqual(code, EXIT_OK, err)
        self.assertIn("leak ***", out)

    @unittest.skipUnless(shutil.which("git"), "git not installed")
    def test_reports_git_repository(self):
        subprocess.run(["git", "init", "-q", str(self.repo)], check=True)
        code, out, err = self.run_cli(["run", "--repo", str(self.repo), "--task", "x"])
        self.assertEqual(code, EXIT_OK, err)
        self.assertIn("(git repository)", out)

    def test_reports_non_git_directory(self):
        code, out, err = self.run_cli(["run", "--repo", str(self.repo), "--task", "x"])
        self.assertEqual(code, EXIT_OK, err)
        self.assertIn("not a git repository", out)


class InteractiveTest(CliTestCase):
    def test_prompts_for_repo_and_multiline_task(self):
        stdin_text = f"{self.repo}\nFirst line of issue\nSecond line\n\nignored after blank\n"
        code, out, err = self.run_cli(["run"], stdin_text=stdin_text)
        self.assertEqual(code, EXIT_OK, err)
        self.assertIn("Repository path: ", out)
        self.assertIn("Task / GitHub issue", out)
        self.assertIn("Task source:     prompt", out)
        self.assertIn("First line of issue (2 line(s)", out)

    def test_prompts_only_for_what_is_missing(self):
        code, out, err = self.run_cli(["run", "--repo", str(self.repo)], stdin_text="Do the thing\n")
        self.assertEqual(code, EXIT_OK, err)
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
        self.assertEqual(result.returncode, EXIT_OK, result.stderr)
        self.assertIn("Harness skeleton ready.", result.stdout)

    def test_run_interactive_via_stdin(self):
        result = self.run_module("run", env_overrides={"AI_API_KEY": FAKE_KEY},
                                 stdin_text=f"{self.repo}\nFix it\n\n")
        self.assertEqual(result.returncode, EXIT_OK, result.stderr)
        self.assertIn("Task source:     prompt", result.stdout)

    def test_run_without_api_key(self):
        result = self.run_module("run", "--repo", str(self.repo), "--task", "x")
        self.assertEqual(result.returncode, EXIT_USAGE)
        self.assertIn("AI_API_KEY is not set", result.stderr)


if __name__ == "__main__":
    unittest.main()

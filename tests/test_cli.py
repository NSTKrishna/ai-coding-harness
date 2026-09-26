import contextlib
import io
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from harness import __version__
from harness.cli import EXIT_INTERRUPTED, EXIT_OK, EXIT_USAGE, main

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
        fetch = mock.patch("harness.github.fetch_issue", return_value=None)   # tests never touch the network
        self.fetch_issue = fetch.start()
        self.addCleanup(fetch.stop)

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
        """Input is accepted and shown in the header; execution then stops because this
        build has no live model adapter (M4). Nothing is claimed to have run."""
        self.assertIn("Repository", out, err)
        self.assertIn("Model", out, err)
        self.assertEqual(code, EXIT_USAGE, err)
        self.assertIn("No model provider is configured", err)
        self.assertIn("No model was called and the repository was not modified.", err)


class RunArgumentsTest(CliTestCase):
    def test_repo_and_task_arguments(self):
        code, out, err = self.run_cli(["run", "--repo", str(self.repo), "--task", "Fix the parser bug"])
        self.assert_input_accepted(code, out, err)
        self.assertIn(self.repo.name, out)
        self.assertNotIn(str(self.repo.resolve()), out)   # full path hidden by default
        self.assertIn("Fix the parser bug", out)
        self.assertNotIn("Repository >", out)   # not prompted: --repo was given

    def test_repo_and_task_verbose_shows_full_path_and_diagnostics(self):
        code, out, err = self.run_cli(["run", "--repo", str(self.repo), "--task", "x", "--verbose"])
        self.assertEqual(code, EXIT_USAGE, err)
        self.assertIn(str(self.repo.resolve()), out)
        self.assertIn("Details", out)
        self.assertIn("Model adapter:", out)
        self.assertIn("Base URL:", out)
        self.assertNotIn(FAKE_KEY, out)

    def test_task_file_argument(self):
        task_file = self.tmp / "issue.md"
        task_file.write_text("Title line\n\nBody line\n", encoding="utf-8")
        code, out, err = self.run_cli(["run", "--repo", str(self.repo), "--task-file", str(task_file)])
        self.assert_input_accepted(code, out, err)
        self.assertIn("Title line", out)
        self.assertIn("more line(s)", out)

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
        self.assertIn("clean", out)
        self.assertNotIn("not a git repository", out)

    def test_reports_non_git_directory(self):
        code, out, err = self.run_cli(["run", "--repo", str(self.repo), "--task", "x"])
        self.assert_input_accepted(code, out, err)
        self.assertIn("not a git repository", out)

    def test_github_issue_url_is_shown_as_an_issue(self):
        url = "https://github.com/rajatrsrivastav/ossgrid/issues/7"
        code, out, err = self.run_cli(["run", "--repo", str(self.repo), "--task", url])
        self.assert_input_accepted(code, out, err)
        self.assertIn("Issue", out)
        self.assertIn("rajatrsrivastav/ossgrid #7", out)
        self.assertIn(url, out)
        self.assertNotIn("Task\n", out)   # the Issue block replaces the generic Task block

    def test_no_interactive_flag_never_prompts_for_confirmation(self):
        # stdout is already non-tty (StringIO) here, so this mainly documents the flag
        # is accepted; the real effect is exercised by the confirmation tests below.
        code, out, err = self.run_cli(["run", "--repo", str(self.repo), "--task", "x", "--no-interactive"])
        self.assert_input_accepted(code, out, err)


class InteractiveTest(CliTestCase):
    def test_single_line_task_submits_immediately(self):
        code, out, err = self.run_cli(["run"], stdin_text=f"{self.repo}\nFix the thing\n")
        self.assert_input_accepted(code, out, err)
        self.assertIn("Repository", out)
        self.assertIn("Task >", out)
        self.assertIn("Fix the thing", out)

    def test_github_issue_url_submits_immediately(self):
        url = "https://github.com/octocat/hello-world/issues/42"
        code, out, err = self.run_cli(["run"], stdin_text=f"{self.repo}\n{url}\n")
        self.assert_input_accepted(code, out, err)
        self.assertIn("octocat/hello-world #42", out)

    def test_explicit_multiline_task(self):
        stdin_text = f"{self.repo}\n:multi\nFirst line of issue\nSecond line\n\n"
        code, out, err = self.run_cli(["run"], stdin_text=stdin_text)
        self.assert_input_accepted(code, out, err)
        self.assertIn("Multiline task", out)
        self.assertIn("First line of issue", out)
        self.assertIn("more line(s)", out)

    def test_blank_line_task_is_rejected(self):
        code, _, err = self.run_cli(["run"], stdin_text=f"{self.repo}\n\n")
        self.assertEqual(code, EXIT_USAGE)
        self.assertIn("task must not be empty", err)

    def test_prompts_only_for_what_is_missing(self):
        code, out, err = self.run_cli(["run", "--repo", str(self.repo)], stdin_text="Do the thing\n")
        self.assert_input_accepted(code, out, err)
        self.assertNotIn("Repository >", out)
        self.assertIn("Task >", out)

    def test_end_of_input_without_repo(self):
        code, _, err = self.run_cli(["run"], stdin_text="")
        self.assertEqual(code, EXIT_USAGE)
        self.assertIn("no input received for 'Repository'", err)

    def test_end_of_input_without_task(self):
        code, _, err = self.run_cli(["run"], stdin_text=f"{self.repo}\n")
        self.assertEqual(code, EXIT_USAGE)
        self.assertIn("no input received for 'Task'", err)


class ConfirmationTest(CliTestCase):
    """The interactive start confirmation only applies when stdout is a TTY."""

    def run_with_tty_stdout(self, stdin_text):
        stdout, stderr = io.StringIO(), io.StringIO()
        stdout.isatty = lambda: True
        with mock.patch("harness.model.factory.create_model_client", side_effect=AssertionError("model must not be called")):
            code = main(["run", "--repo", str(self.repo), "--task", "x"],
                       environ={"AI_API_KEY": FAKE_KEY}, dotenv_path=NO_DOTENV,
                       stdin=io.StringIO(stdin_text), stdout=stdout, stderr=stderr)
        return code, stdout.getvalue(), stderr.getvalue()

    def test_declining_confirmation_cancels_before_any_model_call(self):
        code, out, err = self.run_with_tty_stdout("n\n")
        self.assertEqual(code, EXIT_INTERRUPTED, err)
        self.assertIn("Cancelled", out)

    def test_ctrl_c_during_confirmation_is_graceful(self):
        stdout, stderr = io.StringIO(), io.StringIO()
        stdout.isatty = lambda: True
        stdin = io.StringIO("")
        stdin.readline = mock.Mock(side_effect=KeyboardInterrupt)
        with mock.patch("harness.model.factory.create_model_client", side_effect=AssertionError("must not be called")):
            code = main(["run", "--repo", str(self.repo), "--task", "x"],
                       environ={"AI_API_KEY": FAKE_KEY}, dotenv_path=NO_DOTENV,
                       stdin=stdin, stdout=stdout, stderr=stderr)
        self.assertEqual(code, EXIT_INTERRUPTED)
        self.assertIn("Cancelled", stdout.getvalue())


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
        self.assertIn("Repository", result.stdout)
        self.assertIn("No model provider is configured", result.stderr)

    def test_run_interactive_via_stdin(self):
        result = self.run_module("run", env_overrides={"AI_API_KEY": FAKE_KEY},
                                 stdin_text=f"{self.repo}\nFix it\n")
        self.assertEqual(result.returncode, EXIT_USAGE, result.stderr)
        self.assertIn("Fix it", result.stdout)
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
        code, out, _ = self.run_with_model([text_response("not a plan")] * 2)
        self.assertEqual(code, 1)
        self.assertIn("MODEL_ERROR", out)
        self.assertIn("invalid_plan", out)

    def test_openai_compatible_without_model_or_endpoint_is_reported(self):
        code, out, err = self.run_cli(["run", "--repo", str(self.repo), "--task", "Fix add_one"],
                                      environ={"AI_API_KEY": FAKE_KEY, "AI_MODEL_ADAPTER": "openai_compatible"})
        self.assertEqual(code, EXIT_USAGE)
        self.assertIn("AI_MODEL and AI_BASE_URL", err)
        self.assertNotIn(FAKE_KEY, out + err)
        self.assertIn("return x + 2", (self.repo / "src" / "math_utils.py").read_text())

    def test_unsupported_adapter_is_reported_precisely(self):
        code, out, err = self.run_cli(["run", "--repo", str(self.repo), "--task", "Fix add_one"],
                                      environ={"AI_API_KEY": FAKE_KEY, "AI_MODEL_ADAPTER": "acme"})
        self.assertEqual(code, EXIT_USAGE)
        self.assertIn("Configured model adapter is not supported by this build: 'acme'", err)
        self.assertNotIn(FAKE_KEY, out + err)
        self.assertIn("harness inspect", err)
        self.assertIn("return x + 2", (self.repo / "src" / "math_utils.py").read_text())

    def test_ctrl_c_during_execution_is_graceful(self):
        from unittest import mock as _mock

        from harness.model import ScriptedModel

        class InterruptingModel(ScriptedModel):
            def generate(self, request):
                raise KeyboardInterrupt

        self.runs_dir = self.tmp / "runs"
        with _mock.patch("harness.model.factory.create_model_client", return_value=InterruptingModel(self.script)):
            code, out, err = self.run_cli(["run", "--repo", str(self.repo), "--task", "Fix add_one"],
                                          environ={"AI_API_KEY": FAKE_KEY, "HARNESS_RUNS_DIR": str(self.runs_dir)})
        self.assertEqual(code, EXIT_INTERRUPTED, err)
        self.assertIn("Run cancelled", out)
        self.assertIn("No changes were committed.", out)
        (run_dir,) = [d.resolve() for d in self.runs_dir.iterdir()]
        self.assertIn(str(run_dir), out)


@unittest.skipUnless(shutil.which("git"), "git not installed")
class IssueAndBranchTest(CliTestCase):
    """GitHub issue intake + fresh-branch workflow, end to end with a scripted model."""

    URL = "https://github.com/acme/calc/issues/7"

    def setUp(self):
        super().setUp()
        from harness.github import IssueDetails
        from tests.orchestration_helpers import FIX_PATCH, buggy_repo, complete, plan_response, tool
        from tests.repo_fixtures import git
        upstream = buggy_repo(self.tmp)
        self.work = self.tmp / "work"
        subprocess.run(["git", "clone", "-q", "-o", "upstream", str(upstream), str(self.work)], check=True)
        git(self.work, "remote", "add", "origin", "https://github.com/acme/calc.git")
        git(self.work, "switch", "-q", "-c", "feature/old")
        self.script = [plan_response(), tool("apply_patch", patch=FIX_PATCH), complete("fixed add_one")]
        self.fetch_issue.return_value = IssueDetails(
            title="add_one returns the wrong value", body="add_one(1) should be 2.", state="OPEN")

    def run_issue(self, *extra, script=None):
        from harness.model import ScriptedModel
        self.runs_dir = self.tmp / "runs"
        model = ScriptedModel(script or self.script)
        with mock.patch("harness.model.factory.create_model_client", return_value=model):
            code, out, err = self.run_cli(["run", "--repo", str(self.work), "--task", self.URL, *extra],
                                          environ={"AI_API_KEY": FAKE_KEY, "HARNESS_RUNS_DIR": str(self.runs_dir)})
        return code, out, err, model

    def branch(self):
        return subprocess.run(["git", "-C", str(self.work), "branch", "--show-current"],
                              capture_output=True, text=True, check=True).stdout.strip()

    def test_issue_run_creates_branch_from_upstream_and_passes_issue_text(self):
        code, out, err, _ = self.run_issue()
        self.assertEqual(code, EXIT_OK, err)
        self.assertEqual(self.branch(), "harness/issue-7")
        self.assertIn("add_one returns the wrong value", out)          # title in the header
        self.assertIn("new harness/issue-7 from upstream/main", out)
        self.assertIn("previous branch: git switch feature/old", out)
        import json
        (run_dir,) = list(self.runs_dir.iterdir())
        task = json.loads((run_dir / "summary.json").read_text())["task"]
        self.assertIn("add_one(1) should be 2.", task)                  # the model saw the issue, not just a URL
        self.assertIn(self.URL, task)

    def test_no_branch_keeps_current_branch(self):
        code, out, err, _ = self.run_issue("--no-branch")
        self.assertEqual(code, EXIT_OK, err)
        self.assertEqual(self.branch(), "feature/old")

    def test_dirty_tree_stays_on_branch_with_a_note(self):
        (self.work / "README.md").write_text("local edit\n")
        from tests.repo_fixtures import git
        git(self.work, "add", "README.md")
        code, out, err, _ = self.run_issue()
        self.assertEqual(self.branch(), "feature/old")
        self.assertIn("Staying on the current branch", out)

    def test_explicit_branch_on_dirty_tree_is_a_usage_error_and_changes_nothing(self):
        (self.work / "README.md").write_text("local edit\n")
        from tests.repo_fixtures import git
        git(self.work, "add", "README.md")
        code, out, err, model = self.run_issue("--branch")
        self.assertEqual(code, EXIT_USAGE)
        self.assertIn("cannot create a branch", err)
        self.assertEqual(self.branch(), "feature/old")

    def test_branch_is_not_created_when_the_model_is_not_configured(self):
        code, out, err = self.run_cli(["run", "--repo", str(self.work), "--task", self.URL])
        self.assertEqual(code, EXIT_USAGE)
        self.assertEqual(self.branch(), "feature/old")

    def test_closed_issue_and_repository_mismatch_are_warned(self):
        from harness.github import IssueDetails
        self.fetch_issue.return_value = IssueDetails(title="t", body="b", state="CLOSED", state_reason="DUPLICATE")
        url = "https://github.com/other/project/issues/1"
        code, out, err = self.run_cli(["run", "--repo", str(self.work), "--task", url, "--no-branch"])
        self.assertIn("This issue is closed (duplicate).", out)
        self.assertIn("Issue is from other/project", out)

    def test_fetch_failure_falls_back_to_the_url(self):
        self.fetch_issue.return_value = None
        code, out, err, _ = self.run_issue("--no-branch")
        self.assertIn("Could not fetch the issue text", out)

    def test_no_fetch_issue_flag_skips_fetching(self):
        self.run_issue("--no-fetch-issue", "--no-branch")
        self.fetch_issue.assert_not_called()


if __name__ == "__main__":
    unittest.main()

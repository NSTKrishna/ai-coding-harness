import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from harness.workspace import WorkspaceError, branch_name_for, plan_branch, prepare_branch
from tests.repo_fixtures import git, git_repo

FILES = {"README.md": "hello\n", "src/app.py": "print('hi')\n"}


def sha(repo: Path, ref: str) -> str:
    return subprocess.run(["git", "-C", str(repo), "rev-parse", ref], capture_output=True, text=True,
                          check=True).stdout.strip()


def current(repo: Path) -> str:
    return subprocess.run(["git", "-C", str(repo), "branch", "--show-current"], capture_output=True, text=True,
                          check=True).stdout.strip()


@unittest.skipUnless(shutil.which("git"), "git not installed")
class WorkspaceTest(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.tmp = Path(directory.name)
        self.upstream = git_repo(self.tmp / "upstream", FILES)
        subprocess.run(["git", "clone", "-q", "-o", "upstream", str(self.upstream), str(self.tmp / "work")],
                       check=True)
        self.work = self.tmp / "work"
        git(self.work, "config", "user.email", "t@example.com")
        git(self.work, "config", "user.name", "t")
        git(self.work, "switch", "-q", "-c", "feature/old")

    def test_plan_uses_upstream_default_branch_and_records_original(self):
        plan = plan_branch(self.work, "harness/issue-7")
        self.assertEqual((plan.remote, plan.base, plan.branch, plan.original),
                         ("upstream", "main", "harness/issue-7", "feature/old"))

    def test_prepare_branches_from_freshly_fetched_upstream_without_touching_local_main(self):
        local_main = sha(self.work, "main")
        (self.upstream / "NEW.md").write_text("new upstream commit\n")
        git(self.upstream, "add", "-A")
        git(self.upstream, "commit", "-q", "-m", "upstream moved on")
        latest = sha(self.upstream, "main")

        result = prepare_branch(self.work, plan_branch(self.work, "harness/issue-7"))

        self.assertTrue(result.fetched)
        self.assertIsNone(result.warning)
        self.assertEqual(current(self.work), "harness/issue-7")
        self.assertEqual(sha(self.work, "HEAD"), latest)            # the latest upstream commit
        self.assertEqual(sha(self.work, "main"), local_main)        # local main untouched
        self.assertTrue((self.work / "NEW.md").is_file())

    def test_dirty_tracked_file_is_refused_and_nothing_changes(self):
        (self.work / "README.md").write_text("local edit\n")
        with self.assertRaises(WorkspaceError) as ctx:
            plan_branch(self.work, "harness/x")
        self.assertIn("uncommitted change", str(ctx.exception))
        self.assertEqual(current(self.work), "feature/old")
        self.assertEqual((self.work / "README.md").read_text(), "local edit\n")

    def test_untracked_files_do_not_block(self):
        (self.work / "scratch.txt").write_text("untracked\n")
        result = prepare_branch(self.work, plan_branch(self.work, "harness/x"))
        self.assertEqual(current(self.work), result.plan.branch)
        self.assertTrue((self.work / "scratch.txt").is_file())

    def test_existing_branch_name_gets_a_suffix(self):
        git(self.work, "branch", "harness/issue-7")
        self.assertEqual(plan_branch(self.work, "harness/issue-7").branch, "harness/issue-7-2")

    def test_offline_fetch_falls_back_to_last_fetched_ref(self):
        shutil.rmtree(self.upstream)
        result = prepare_branch(self.work, plan_branch(self.work, "harness/offline"))
        self.assertFalse(result.fetched)
        self.assertIn("last fetched copy", result.warning)
        self.assertEqual(current(self.work), "harness/offline")

    def test_origin_is_used_when_there_is_no_upstream(self):
        git(self.work, "remote", "rename", "upstream", "origin")
        self.assertEqual(plan_branch(self.work, "harness/x").remote, "origin")

    def test_no_remote_is_an_error(self):
        git(self.work, "remote", "remove", "upstream")
        with self.assertRaises(WorkspaceError):
            plan_branch(self.work, "harness/x")

    def test_non_git_directory_is_an_error(self):
        plain = self.tmp / "plain"
        plain.mkdir()
        with self.assertRaises(WorkspaceError):
            plan_branch(plain, "harness/x")


class BranchNameTest(unittest.TestCase):
    def test_issue_number(self):
        self.assertEqual(branch_name_for("https://github.com/a/b/issues/22091", 22091), "harness/issue-22091")

    def test_slug_from_task(self):
        self.assertEqual(branch_name_for("Fix the parser: handle empty input!"), "harness/fix-the-parser-handle-empty-input")

    def test_empty_task(self):
        self.assertEqual(branch_name_for("!!!"), "harness/task")


if __name__ == "__main__":
    unittest.main()

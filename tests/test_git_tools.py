import shutil
import unittest

from harness.tools import ToolContext, build_registry

from tests.helpers import RepoTestCase, git


@unittest.skipUnless(shutil.which("git"), "git not installed")
class GitToolsTest(RepoTestCase):
    def setUp(self):
        super().setUp()
        self.write("app.py", "a = 1\nb = 2\n")
        self.write("sub/mod.py", "x = 1\n")
        self.init_git()
        self.ctx = ToolContext.create(self.repo)
        self.registry = build_registry(self.ctx)

    def call(self, name, **args):
        result = self.registry.dispatch(name, args)
        self.assertTrue(result.success, result.error)
        return result.data

    def change_worktree(self):
        self.write("app.py", "a = 1\nb = 3\nc = 4\n")
        self.write("new_file.py", "print('new')\nprint('two')\n")

    def test_status_clean(self):
        status = self.call("git_status")
        self.assertTrue(status.clean)
        self.assertEqual(status.branch, "main")

    def test_status_reports_modified_and_untracked(self):
        self.change_worktree()
        entries = {(e.path, e.index, e.worktree) for e in self.call("git_status").entries}
        self.assertEqual(entries, {("app.py", " ", "M"), ("new_file.py", "?", "?")})

    def test_diff_includes_modifications_and_untracked_files(self):
        self.change_worktree()
        result = self.call("git_diff")
        self.assertIn("-b = 2\n+b = 3\n+c = 4\n", result.text)
        self.assertIn("+++ b/new_file.py", result.text)
        self.assertEqual(result.untracked_included, ("new_file.py",))
        self.assertFalse(result.truncated)
        tracked_only = self.call("git_diff", include_untracked=False)
        self.assertNotIn("new_file.py", tracked_only.text)

    def test_diff_paths_filter_and_boundary(self):
        self.change_worktree()
        self.write("sub/mod.py", "x = 2\n")
        only_sub = self.call("git_diff", paths=["sub"])
        self.assertIn("sub/mod.py", only_sub.text)
        self.assertNotIn("app.py", only_sub.text)
        outside = self.registry.dispatch("git_diff", {"paths": ["../outside"]})
        self.assertEqual(outside.error.code, "path_outside_repo")

    def test_diff_stat(self):
        self.change_worktree()
        stat = self.call("git_diff_stat")
        by_path = {f.path: (f.added, f.deleted, f.untracked) for f in stat.files}
        self.assertEqual(by_path, {"app.py": (2, 1, False), "new_file.py": (2, 0, True)})
        self.assertEqual((stat.insertions, stat.deletions), (4, 1))
        self.assertEqual(stat.summary, "2 files changed, 4 insertions(+), 1 deletion(-)")

    def test_root_may_be_a_subdirectory_of_the_work_tree(self):
        self.change_worktree()
        self.write("sub/mod.py", "x = 2\n")
        registry = build_registry(ToolContext.create(self.repo / "sub"))
        status = registry.dispatch("git_status").data
        self.assertEqual([e.path for e in status.entries], ["mod.py"])
        stat = registry.dispatch("git_diff_stat").data
        self.assertEqual([f.path for f in stat.files], ["mod.py"])

    def test_inspection_never_changes_repository_state(self):
        self.change_worktree()
        index_before = (self.repo / ".git" / "index").read_bytes()
        head_before = git(self.repo, "rev-parse", "HEAD")
        for name in ("git_status", "git_diff", "git_diff_stat"):
            self.call(name)
        self.assertEqual((self.repo / ".git" / "index").read_bytes(), index_before)
        self.assertEqual(git(self.repo, "rev-parse", "HEAD"), head_before)
        self.assertEqual(git(self.repo, "diff", "--cached", "--name-only"), "")  # nothing staged

    def test_staged_diff(self):
        self.change_worktree()
        git(self.repo, "add", "app.py")  # staged by the test, not the harness
        staged = self.call("git_diff", staged=True)
        self.assertIn("+c = 4", staged.text)
        self.assertEqual(staged.untracked_included, ())


class NotAGitRepoTest(RepoTestCase):
    def test_clear_failure_outside_git(self):
        self.write("a.txt", "x\n")
        registry = build_registry(ToolContext.create(self.repo))
        for name in ("git_status", "git_diff", "git_diff_stat"):
            with self.subTest(tool=name):
                result = registry.dispatch(name)
                self.assertFalse(result.success)
                self.assertEqual(result.error.code, "not_git_repo")
                self.assertIn("not inside a git repository", result.error.message)


if __name__ == "__main__":
    unittest.main()

import io
import subprocess
import unittest
from pathlib import Path
from unittest import mock

from harness.ui.format import (
    GitHubIssue,
    format_duration,
    parse_github_issue,
    repo_header,
    shorten_path,
    tool_headline,
)


class GitHubIssueTest(unittest.TestCase):
    def test_recognizes_a_bare_issue_url(self):
        issue = parse_github_issue("https://github.com/rajatrsrivastav/ossgrid/issues/7")
        self.assertEqual(issue, GitHubIssue("rajatrsrivastav", "ossgrid", 7, "https://github.com/rajatrsrivastav/ossgrid/issues/7"))

    def test_trailing_slash_is_accepted(self):
        issue = parse_github_issue("https://github.com/a/b/issues/3/")
        self.assertEqual((issue.owner, issue.repo, issue.number), ("a", "b", 3))

    def test_surrounding_whitespace_is_ignored(self):
        issue = parse_github_issue("  https://github.com/a/b/issues/3  \n")
        self.assertIsNotNone(issue)

    def test_plain_task_is_not_an_issue(self):
        self.assertIsNone(parse_github_issue("Fix the parser bug"))

    def test_url_mentioned_inside_a_longer_task_is_not_an_issue(self):
        self.assertIsNone(parse_github_issue("see https://github.com/a/b/issues/3 for context"))

    def test_multiline_task_is_never_an_issue(self):
        self.assertIsNone(parse_github_issue("https://github.com/a/b/issues/3\nmore text"))

    def test_non_issue_github_url_is_not_an_issue(self):
        self.assertIsNone(parse_github_issue("https://github.com/a/b/pull/3"))

    def test_empty_task_is_not_an_issue(self):
        self.assertIsNone(parse_github_issue("   "))


class DurationTest(unittest.TestCase):
    def test_seconds_only(self):
        self.assertEqual(format_duration(42), "42s")

    def test_minutes_and_seconds(self):
        self.assertEqual(format_duration(84), "1m 24s")

    def test_zero(self):
        self.assertEqual(format_duration(0), "0s")

    def test_negative_is_clamped(self):
        self.assertEqual(format_duration(-5), "0s")


class ShortenPathTest(unittest.TestCase):
    def test_short_path_is_unchanged(self):
        self.assertEqual(shorten_path("src/a.py", 40), "src/a.py")

    def test_long_path_keeps_head_and_tail(self):
        result = shorten_path("src/components/deeply/nested/dir/Projects.tsx", 30)
        self.assertLessEqual(len(result), 30)
        self.assertTrue(result.startswith("src/"))
        self.assertTrue(result.endswith("Projects.tsx"))

    def test_never_longer_than_requested(self):
        for width in (5, 10, 20, 60):
            self.assertLessEqual(len(shorten_path("a/b/c/d/e/f/g/h/i/j/k.py", width)), max(width, 1))


class ToolHeadlineTest(unittest.TestCase):
    def test_read_file_shows_path(self):
        self.assertEqual(tool_headline("read_file", '{"path": "src/foo.py"}'), "read  src/foo.py")

    def test_search_shows_pattern_and_detail(self):
        line = tool_headline("search_text", '{"pattern": "refreshToken"}', detail="8 match(es)")
        self.assertEqual(line, "search  refreshToken   8 match(es)")

    def test_run_command_shows_command(self):
        line = tool_headline("run_command", '{"command": ["pytest", "-q"]}')
        self.assertEqual(line, "run  pytest -q")

    def test_edit_file_strips_tool_name_prefix(self):
        line = tool_headline("edit_file", "edit_file src/x.py, 5 new line(s)")
        self.assertNotIn("edit_file edit_file", line)
        self.assertTrue(line.startswith("edit  "))

    def test_no_target_falls_back_to_bare_verb(self):
        self.assertEqual(tool_headline("git_diff", "{}"), "git")

    def test_unknown_tool_uses_its_own_name_as_verb(self):
        line = tool_headline("mystery_tool", "{}")
        self.assertTrue(line == "mystery_tool" or line.startswith("mystery_tool"))


class ToolPartsTest(unittest.TestCase):
    def test_edit_target_is_just_the_path(self):
        from harness.ui.format import tool_parts
        self.assertEqual(tool_parts("edit_file", "edit_file src/x.py, 2 new line(s)"), ("edit", "src/x.py"))

    def test_patch_target_lists_files(self):
        from harness.ui.format import tool_parts
        self.assertEqual(tool_parts("apply_patch", "patch of 9 lines for a.py, b.py"), ("edit", "a.py, b.py"))

    def test_absolute_interpreter_is_shortened_to_its_name(self):
        from harness.ui.format import tool_parts
        verb, target = tool_parts("run_tests", '{"command": ["/opt/homebrew/bin/python3.14", "-m", "unittest"]}')
        self.assertEqual((verb, target), ("test", "python3.14 -m unittest"))


class RepoHeaderTest(unittest.TestCase):
    def test_non_git_directory(self):
        with mock.patch("shutil.which", return_value=None):
            header = repo_header(Path("/some/repo"))
        self.assertFalse(header.is_git)
        self.assertEqual(header.status_line(), "not a git repository")

    def test_git_missing_returncode_is_not_git(self):
        with mock.patch("shutil.which", return_value="/usr/bin/git"), \
             mock.patch("subprocess.run", return_value=subprocess.CompletedProcess([], 1, "", "")):
            header = repo_header(Path("/some/repo"))
        self.assertFalse(header.is_git)

    def test_clean_git_repo(self):
        def fake_run(argv, **kwargs):
            if "--is-inside-work-tree" in argv:
                return subprocess.CompletedProcess(argv, 0, "true\n", "")
            if "--abbrev-ref" in argv:
                return subprocess.CompletedProcess(argv, 0, "main\n", "")
            return subprocess.CompletedProcess(argv, 0, "", "")

        with mock.patch("shutil.which", return_value="/usr/bin/git"), mock.patch("subprocess.run", side_effect=fake_run):
            header = repo_header(Path("/some/repo"))
        self.assertTrue(header.is_git)
        self.assertEqual(header.branch, "main")
        self.assertTrue(header.clean)
        self.assertEqual(header.status_line(), "git · main · clean")

    def test_dirty_git_repo(self):
        def fake_run(argv, **kwargs):
            if "--is-inside-work-tree" in argv:
                return subprocess.CompletedProcess(argv, 0, "true\n", "")
            if "--abbrev-ref" in argv:
                return subprocess.CompletedProcess(argv, 0, "feature/foo\n", "")
            return subprocess.CompletedProcess(argv, 0, " M a.py\n?? b.py\n", "")

        with mock.patch("shutil.which", return_value="/usr/bin/git"), mock.patch("subprocess.run", side_effect=fake_run):
            header = repo_header(Path("/some/repo"))
        self.assertEqual(header.modified, 2)
        self.assertIn("2 modified", header.status_line())


if __name__ == "__main__":
    unittest.main()

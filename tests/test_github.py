import io
import json
import shutil
import subprocess
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

from harness import github
from harness.github import IssueDetails, compose_task, fetch_issue, issue_matches_repo, remote_repos
from harness.ui.format import parse_github_issue
from tests.repo_fixtures import git, git_repo

ISSUE = parse_github_issue("https://github.com/meshery/meshery/issues/22091")
GH_JSON = {"title": "Broken image link", "body": "The link points to /images/x.svg.", "state": "CLOSED",
           "stateReason": "DUPLICATE", "labels": [{"name": "kind/bug"}],
           "comments": [{"author": {"login": "alice"}, "body": "can I take this?"}]}


class FetchTest(unittest.TestCase):
    def test_gh_is_used_when_available(self):
        done = subprocess.CompletedProcess([], 0, json.dumps(GH_JSON), "")
        with mock.patch("shutil.which", return_value="/usr/bin/gh"), \
             mock.patch("subprocess.run", return_value=done) as run:
            details = fetch_issue(ISSUE)
        self.assertEqual(run.call_args[0][0][:4], ["gh", "issue", "view", "22091"])
        self.assertEqual((details.title, details.state, details.state_reason, details.labels, details.source),
                         ("Broken image link", "CLOSED", "DUPLICATE", ("kind/bug",), "gh"))
        self.assertEqual(details.comments, ("alice: can I take this?",))

    def test_public_api_is_the_fallback(self):
        body = json.dumps({"title": "T", "body": "B", "state": "open", "labels": [{"name": "bug"}]}).encode()
        response = mock.MagicMock()
        response.__enter__.return_value.read.return_value = body
        with mock.patch("shutil.which", return_value=None), \
             mock.patch("urllib.request.urlopen", return_value=response):
            details = fetch_issue(ISSUE)
        self.assertEqual((details.title, details.state, details.source), ("T", "OPEN", "api"))

    def test_every_failure_returns_none(self):
        failed = subprocess.CompletedProcess([], 1, "", "not found")
        with mock.patch("shutil.which", return_value="/usr/bin/gh"), \
             mock.patch("subprocess.run", return_value=failed), \
             mock.patch("urllib.request.urlopen", side_effect=urllib.error.URLError("offline")):
            self.assertIsNone(fetch_issue(ISSUE))

    def test_rate_limit_response_returns_none(self):
        response = mock.MagicMock()
        response.__enter__.return_value.read.return_value = b'{"message": "API rate limit exceeded"}'
        with mock.patch("shutil.which", return_value=None), \
             mock.patch("urllib.request.urlopen", return_value=response):
            self.assertIsNone(fetch_issue(ISSUE))

    def test_long_body_is_bounded(self):
        data = dict(GH_JSON, body="x" * 50_000)
        with mock.patch("shutil.which", return_value="/usr/bin/gh"), \
             mock.patch("subprocess.run", return_value=subprocess.CompletedProcess([], 0, json.dumps(data), "")):
            details = fetch_issue(ISSUE)
        self.assertLessEqual(len(details.body), github.MAX_BODY_CHARS)


class ComposeTest(unittest.TestCase):
    def test_task_contains_title_body_url_and_closed_note(self):
        details = IssueDetails(title="Broken image link", body="Details here.", state="CLOSED",
                               state_reason="DUPLICATE", labels=("kind/bug",), comments=("alice: hi",))
        task = compose_task(ISSUE, details)
        self.assertTrue(task.startswith("Resolve GitHub issue meshery/meshery#22091: Broken image link"))
        for part in ("URL: https://github.com/meshery/meshery/issues/22091", "Labels: kind/bug",
                     "this issue is closed (duplicate)", "Details here.", "- alice: hi"):
            self.assertIn(part, task)

    def test_open_issue_has_no_state_note(self):
        task = compose_task(ISSUE, IssueDetails(title="T", body="B", state="OPEN"))
        self.assertNotIn("Note:", task)


@unittest.skipUnless(shutil.which("git"), "git not installed")
class RemoteTest(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.repo = git_repo(Path(directory.name) / "r", {"a.txt": "a\n"})

    def test_https_and_ssh_remotes_are_parsed(self):
        git(self.repo, "remote", "add", "origin", "https://github.com/NSTKrishna/meshery.git")
        git(self.repo, "remote", "add", "upstream", "git@github.com:meshery/meshery.git")
        self.assertEqual(remote_repos(self.repo), {"origin": "nstkrishna/meshery", "upstream": "meshery/meshery"})
        self.assertTrue(issue_matches_repo(ISSUE, self.repo))

    def test_mismatch_is_reported(self):
        git(self.repo, "remote", "add", "origin", "https://github.com/someone/else.git")
        self.assertFalse(issue_matches_repo(ISSUE, self.repo))

    def test_no_remotes_is_unknown(self):
        self.assertIsNone(issue_matches_repo(ISSUE, self.repo))


if __name__ == "__main__":
    unittest.main()

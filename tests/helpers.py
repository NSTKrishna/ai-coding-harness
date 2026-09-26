"""Shared test fixtures: throwaway repositories with isolated git config."""

import os
import subprocess
import tempfile
import unittest
from pathlib import Path

FAKE_KEY = "test-key-7f3a9c1e5b"

# Keep fixture git commands independent of the developer's git config
# (signing, hooks, default branch...).
GIT_ENV = {
    **os.environ,
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_AUTHOR_NAME": "Test",
    "GIT_AUTHOR_EMAIL": "test@example.invalid",
    "GIT_COMMITTER_NAME": "Test",
    "GIT_COMMITTER_EMAIL": "test@example.invalid",
}


def git(repo: Path, *args: str) -> str:
    result = subprocess.run(["git", *args], cwd=repo, env=GIT_ENV, check=True,
                            capture_output=True, text=True)
    return result.stdout


class RepoTestCase(unittest.TestCase):
    """Provides ``self.repo`` (resolved temp dir) and ``self.outside`` (a sibling dir)."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        base = Path(tmp.name).resolve()
        self.repo = base / "repo"
        self.repo.mkdir()
        self.outside = base / "outside"
        self.outside.mkdir()
        (self.outside / "secret.txt").write_text("outside data\n", encoding="utf-8")

    def write(self, rel: str, content: str, *, mode: str = "w") -> Path:
        path = self.repo / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        if mode == "wb":
            path.write_bytes(content)  # type: ignore[arg-type]
        else:
            path.write_text(content, encoding="utf-8", newline="")
        return path

    def init_git(self, commit: bool = True) -> None:
        git(self.repo, "init", "-q")
        git(self.repo, "symbolic-ref", "HEAD", "refs/heads/main")
        if commit:
            git(self.repo, "add", "-A")
            git(self.repo, "commit", "-q", "--allow-empty", "-m", "initial")

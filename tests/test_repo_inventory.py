import shutil
import tempfile
import unittest
from pathlib import Path

from harness.repo import build_inventory
from harness.repo.classify import category_of, is_test_path, language_of
from harness.tools import ToolContext

from tests.repo_fixtures import NOISY_TRACKED, NOISY_UNTRACKED, NON_GIT_REPO, git_repo, write_files


class RepoCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.base = Path(tmp.name).resolve()


@unittest.skipUnless(shutil.which("git"), "git not installed")
class GitInventoryTest(RepoCase):
    def setUp(self):
        super().setUp()
        self.root = git_repo(self.base / "noisy", NOISY_TRACKED, NOISY_UNTRACKED)
        self.inventory = build_inventory(ToolContext.create(self.root))

    def test_uses_git_view(self):
        self.assertEqual(self.inventory.source, "git")
        self.assertTrue(self.inventory.is_git)

    def test_exact_inventory(self):
        self.assertEqual(self.inventory.paths, (".gitignore", "src/app.py", "untracked_note.py"))

    def test_tracked_and_untracked_flags(self):
        self.assertTrue(self.inventory.get("src/app.py").tracked)
        self.assertFalse(self.inventory.get("untracked_note.py").tracked)

    def test_gitignored_files_excluded(self):
        for path in ("generated/out.py", "debug.log", "build/lib/app.py"):
            self.assertTrue((self.root / path).exists())
            self.assertIsNone(self.inventory.get(path), path)

    def test_noise_directories_excluded_even_when_not_gitignored(self):
        for path in (".venv/lib/site.py", "node_modules/pkg/index.js", "dist/bundle.js",
                     "src/__pycache__/app.cpython-312.pyc"):
            self.assertTrue((self.root / path).exists())
            self.assertIsNone(self.inventory.get(path), path)
        self.assertFalse(any(p.startswith(".git/") for p in self.inventory.paths))

    def test_tracked_file_in_build_dir_is_kept(self):
        root = git_repo(self.base / "tracked_build", {"build/config.py": "X = 1\n", ".gitignore": ""})
        self.assertEqual(build_inventory(ToolContext.create(root)).paths, (".gitignore", "build/config.py"))

    def test_deleted_tracked_file_and_symlinks_skipped(self):
        (self.root / "src" / "app.py").unlink()
        (self.root / "link.py").symlink_to(self.root / "untracked_note.py")
        paths = build_inventory(ToolContext.create(self.root)).paths
        self.assertNotIn("src/app.py", paths)
        self.assertNotIn("link.py", paths)


class FilesystemInventoryTest(RepoCase):
    def test_fallback_outside_git(self):
        write_files(self.base / "plain", NON_GIT_REPO)
        inventory = build_inventory(ToolContext.create(self.base / "plain"))
        self.assertEqual(inventory.source, "filesystem")
        self.assertEqual(inventory.paths, ("docs/guide.md", "plain/__init__.py", "plain/core.py",
                                           "requirements.txt", "setup.py", "tests/test_core.py"))
        self.assertIsNone(inventory.get("plain/core.py").tracked)

    def test_limit_is_reported(self):
        write_files(self.base / "many", {f"f{i}.py": "x = 1\n" for i in range(12)})
        from harness.repo.inventory import build_inventory as build
        inventory = build(ToolContext.create(self.base / "many"), max_files=5)
        self.assertEqual(len(inventory.files), 5)
        self.assertTrue(inventory.truncated)
        self.assertIn("stopped at 5 files", inventory.warnings[0])


class ClassificationTest(unittest.TestCase):
    def test_languages(self):
        cases = {"a.py": "Python", "a.ts": "TypeScript", "a.tsx": "TypeScript/TSX", "a.js": "JavaScript",
                 "a.go": "Go", "a.rs": "Rust", "a.java": "Java", "Makefile": "Makefile",
                 "Dockerfile": "Dockerfile", "README.md": "Markdown", "x.unknownext": None}
        for path, language in cases.items():
            with self.subTest(path=path):
                self.assertEqual(language_of(path), language)

    def test_test_files(self):
        for path in ("tests/test_x.py", "pkg/x_test.py", "src/a.test.ts", "src/a.spec.js", "pkg/a_test.go",
                     "src/test/java/FooTest.java", "__tests__/a.js", "conftest.py"):
            with self.subTest(path=path):
                self.assertTrue(is_test_path(path))
        for path in ("src/testing_utils.md", "src/contest.py", "src/a.ts", "tests/data.json"):
            with self.subTest(path=path):
                self.assertFalse(is_test_path(path))

    def test_categories(self):
        cases = {
            "src/app.py": "source", "tests/test_app.py": "test", "pyproject.toml": "manifest",
            "requirements-dev.txt": "manifest", "package.json": "manifest", "go.mod": "manifest",
            "README.md": "documentation", "docs/guide.md": "documentation", "tsconfig.json": "configuration",
            ".github/workflows/ci.yml": "ci", ".gitlab-ci.yml": "ci", "package-lock.json": "generated",
            "app.min.js": "generated", "logo.png": "other", "Makefile": "configuration",
        }
        for path, category in cases.items():
            with self.subTest(path=path):
                self.assertEqual(category_of(path), category)


if __name__ == "__main__":
    unittest.main()

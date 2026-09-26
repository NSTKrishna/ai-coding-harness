import os
import unittest

from harness.tools import ToolContext, ToolLimits, build_registry
from harness.tools.paths import resolve_in_repo
from harness.tools.base import ToolFailure

from tests.helpers import RepoTestCase


class PathBoundaryTest(RepoTestCase):
    """The single repository-boundary function."""

    def assert_rejected(self, path, code="path_outside_repo", **kwargs):
        with self.assertRaises(ToolFailure) as ctx:
            resolve_in_repo(self.repo, path, **kwargs)
        self.assertEqual(ctx.exception.code, code)
        return ctx.exception

    def test_relative_inside_is_accepted(self):
        self.assertEqual(resolve_in_repo(self.repo, "src/a.py"), self.repo / "src" / "a.py")
        self.assertEqual(resolve_in_repo(self.repo, "src/../b.py"), self.repo / "b.py")
        self.assertEqual(resolve_in_repo(self.repo, "."), self.repo)

    def test_traversal_is_rejected(self):
        error = self.assert_rejected("../../../../etc/passwd")
        self.assertIn("outside the repository", error.message)
        self.assert_rejected("../outside/secret.txt")
        self.assert_rejected("src/../../outside")

    def test_absolute_paths(self):
        self.assert_rejected("/etc/passwd")
        self.assert_rejected(str(self.outside / "secret.txt"))
        self.assertEqual(resolve_in_repo(self.repo, str(self.repo / "a.py")), self.repo / "a.py")

    def test_symlink_escaping_repository_is_rejected(self):
        os.symlink(self.outside, self.repo / "link_dir")
        os.symlink(self.outside / "secret.txt", self.repo / "link_file")
        self.assert_rejected("link_dir/secret.txt")
        self.assert_rejected("link_file")

    def test_symlink_inside_repository_is_allowed(self):
        self.write("real.txt", "x")
        os.symlink(self.repo / "real.txt", self.repo / "alias.txt")
        self.assertEqual(resolve_in_repo(self.repo, "alias.txt"), self.repo / "real.txt")

    def test_other_invalid_inputs(self):
        self.assert_rejected("", code="invalid_path")
        self.assert_rejected("a\x00b", code="invalid_path")
        self.assert_rejected("~/.ssh/id_rsa")

    def test_git_directory_is_not_writable(self):
        self.assert_rejected(".git/config", code="path_not_writable", for_write=True)
        resolve_in_repo(self.repo, ".git/config")  # reading is fine


class FileToolsTest(RepoTestCase):
    def setUp(self):
        super().setUp()
        self.write("README.md", "# Title\n")
        self.write("src/app.py", "".join(f"line {i}\n" for i in range(1, 11)))
        self.write("src/util/helpers.py", "def helper():\n    pass\n")
        self.write("tests/test_app.py", "import app\n")
        self.write("node_modules/pkg/index.js", "ignored\n")
        self.write(".git/HEAD", "ref\n")
        self.ctx = ToolContext.create(self.repo)
        self.registry = build_registry(self.ctx)

    def call(self, name, **args):
        return self.registry.dispatch(name, args)

    def test_list_files(self):
        result = self.call("list_files")
        paths = [e.path for e in result.data.entries]
        self.assertEqual(paths, ["README.md", "src", "tests", "src/app.py", "src/util",
                                 "src/util/helpers.py", "tests/test_app.py"])
        self.assertFalse(result.data.truncated)
        kinds = {e.path: e.kind for e in result.data.entries}
        self.assertEqual((kinds["src"], kinds["README.md"]), ("dir", "file"))

    def test_list_files_depth_and_limit(self):
        top = self.call("list_files", max_depth=1).data
        self.assertEqual([e.path for e in top.entries], ["README.md", "src", "tests"])
        limited = self.call("list_files", max_entries=2).data
        self.assertEqual(len(limited.entries), 2)
        self.assertTrue(limited.truncated)

    def test_list_files_outside_repo(self):
        self.assertEqual(self.call("list_files", path="..").error.code, "path_outside_repo")

    def test_find_files_by_name_and_path(self):
        by_name = [e.path for e in self.call("find_files", pattern="*.py").data.entries]
        self.assertEqual(by_name, ["src/app.py", "src/util/helpers.py", "tests/test_app.py"])
        by_path = [e.path for e in self.call("find_files", pattern="src/*.py").data.entries]
        self.assertEqual(by_path, ["src/app.py", "src/util/helpers.py"])
        in_dir = [e.path for e in self.call("find_files", pattern="test_*.py", path="tests").data.entries]
        self.assertEqual(in_dir, ["tests/test_app.py"])

    def test_read_file(self):
        data = self.call("read_file", path="src/app.py").data
        self.assertTrue(data.content.startswith("line 1\n"))
        self.assertEqual((data.start_line, data.end_line, data.total_lines), (1, 10, 10))
        self.assertFalse(data.truncated)

    def test_read_range(self):
        data = self.call("read_range", path="src/app.py", start_line=3, end_line=5).data
        self.assertEqual(data.content, "line 3\nline 4\nline 5\n")
        self.assertEqual((data.start_line, data.end_line), (3, 5))

    def test_read_range_past_end_is_clamped(self):
        data = self.call("read_range", path="src/app.py", start_line=9, end_line=500).data
        self.assertEqual(data.content, "line 9\nline 10\n")
        self.assertEqual(data.end_line, 10)

    def test_read_range_validation(self):
        self.assertEqual(self.call("read_range", path="src/app.py", start_line=5, end_line=2).error.code,
                         "invalid_arguments")
        self.assertEqual(self.call("read_range", path="src/app.py", start_line=0, end_line=2).error.code,
                         "invalid_arguments")
        error = self.call("read_range", path="src/app.py", start_line=50, end_line=60).error
        self.assertEqual(error.code, "range_out_of_bounds")
        self.assertIn("has 10 lines", error.message)

    def test_read_errors(self):
        self.assertEqual(self.call("read_file", path="missing.py").error.code, "not_found")
        self.assertEqual(self.call("read_file", path="src").error.code, "is_directory")
        self.assertEqual(self.call("read_file", path="../outside/secret.txt").error.code, "path_outside_repo")
        self.write("latin1.txt", b"caf\xe9\n", mode="wb")
        self.assertEqual(self.call("read_file", path="latin1.txt").error.code, "decode_error")
        self.write("image.bin", b"\x89PNG\x00\x00data", mode="wb")
        self.assertEqual(self.call("read_file", path="image.bin").error.code, "binary_file")

    def test_read_through_escaping_symlink_is_rejected(self):
        os.symlink(self.outside / "secret.txt", self.repo / "sneaky.txt")
        result = self.call("read_file", path="sneaky.txt")
        self.assertEqual(result.error.code, "path_outside_repo")
        self.assertIsNone(result.data)

    def test_output_size_limit(self):
        ctx = ToolContext.create(self.repo, limits=ToolLimits(max_read_chars=20))
        registry = build_registry(ctx)
        data = registry.dispatch("read_file", {"path": "src/app.py"}).data
        self.assertEqual(data.content, "line 1\nline 2\n")  # whole lines only; a third would make 21 chars
        self.assertTrue(data.truncated)
        self.assertEqual(data.end_line, 2)

    def test_file_size_limit(self):
        ctx = ToolContext.create(self.repo, limits=ToolLimits(max_file_bytes=10))
        error = build_registry(ctx).dispatch("read_file", {"path": "src/app.py"}).error
        self.assertEqual(error.code, "file_too_large")


if __name__ == "__main__":
    unittest.main()

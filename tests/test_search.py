import os
import shutil
import io
import unittest
from unittest import mock

from harness.tools import ToolContext, ToolLimits, build_registry
from harness.tools.search import search

from tests.helpers import RepoTestCase

HAS_RG = shutil.which("rg") is not None
ENGINES = ["python"] + (["ripgrep"] if HAS_RG else [])


class SearchTest(RepoTestCase):
    def setUp(self):
        super().setUp()
        self.write("src/app.py", "def parse_date(s):\n    return s\n\nPARSE = parse_date\n")
        self.write("src/other.py", "x = 'Parse_Date'\n")
        self.write("docs/notes.md", "mention parse_date here\n")
        self.write(".github/workflow.yml", "run: parse_date\n")
        self.write("node_modules/lib.js", "parse_date\n")
        self.write("blob.bin", b"parse_date\x00\x01", mode="wb")
        self.ctx = ToolContext.create(self.repo)

    def run_search(self, engine, query, **kwargs):
        return search(self.ctx, query, engine=engine, **kwargs)

    def test_literal_search_structured_results(self):
        for engine in ENGINES:
            with self.subTest(engine=engine):
                result = self.run_search(engine, "parse_date")
                self.assertEqual(result.engine, engine)
                self.assertEqual(
                    [(m.path, m.line, m.text) for m in result.matches],
                    [(".github/workflow.yml", 1, "run: parse_date"),
                     ("docs/notes.md", 1, "mention parse_date here"),
                     ("src/app.py", 1, "def parse_date(s):"),
                     ("src/app.py", 4, "PARSE = parse_date")],
                )
                self.assertFalse(result.truncated)

    @unittest.skipUnless(HAS_RG, "ripgrep not installed")
    def test_engines_agree(self):
        for query, kwargs in [("parse_date", {}), ("PARSE", {"case_sensitive": False}),
                              (r"def \w+\(", {"regex": True}), ("parse", {"glob": "*.py"}),
                              ("parse", {"path": "src"}), ("return", {"path": "src/app.py"})]:
            with self.subTest(query=query, **kwargs):
                python = self.run_search("python", query, **kwargs).matches
                ripgrep = self.run_search("ripgrep", query, **kwargs).matches
                self.assertEqual(python, ripgrep)

    def test_options(self):
        for engine in ENGINES:
            with self.subTest(engine=engine):
                insensitive = self.run_search(engine, "parse_date", case_sensitive=False)
                self.assertIn("src/other.py", {m.path for m in insensitive.matches})
                py_only = self.run_search(engine, "parse_date", glob="*.py")
                self.assertEqual({m.path for m in py_only.matches}, {"src/app.py"})
                regex = self.run_search(engine, r"^def \w+", regex=True)
                self.assertEqual([(m.path, m.line) for m in regex.matches], [("src/app.py", 1)])

    def test_result_limit(self):
        for engine in ENGINES:
            with self.subTest(engine=engine):
                result = self.run_search(engine, "parse_date", max_results=2)
                self.assertEqual(len(result.matches), 2)
                self.assertTrue(result.truncated)

    def test_long_lines_are_cut(self):
        self.write("long.txt", "needle " + "x" * 5000 + "\n")
        ctx = ToolContext.create(self.repo, limits=ToolLimits(max_match_chars=50))
        for engine in ENGINES:
            with self.subTest(engine=engine):
                match = search(ctx, "needle", engine=engine).matches[0]
                self.assertEqual(len(match.text), 50)

    def test_escaping_symlinks_are_not_followed(self):
        (self.outside / "leak.py").write_text("parse_date = 'outside'\n", encoding="utf-8")
        os.symlink(self.outside, self.repo / "linked")
        for engine in ENGINES:
            with self.subTest(engine=engine):
                paths = {m.path for m in self.run_search(engine, "parse_date").matches}
                self.assertFalse(any(p.startswith("linked") for p in paths))

    def test_a_ripgrep_stream_cut_mid_line_is_partial_not_a_crash(self):
        """Killing ripgrep (timeout, or the match limit) can cut its last line mid-write.
        A large repository hit this as an uncaught JSONDecodeError that ended the whole run."""
        import json as _json
        import subprocess as _subprocess
        from unittest import mock

        good = _json.dumps({"type": "match", "data": {
            "path": {"text": "src/app.py"}, "lines": {"text": "def parse_date(s):"}, "line_number": 1}})
        stream = io.BytesIO((good + "\n" + good[:40]).encode())        # second line truncated

        class FakeProc:
            stdout = stream
            returncode = -9
            def kill(self): pass
            def wait(self): return -9

        with mock.patch.object(_subprocess, "Popen", return_value=FakeProc()):
            result = self.run_search("ripgrep", "parse_date")
        self.assertEqual([(m.path, m.line) for m in result.matches], [("src/app.py", 1)])
        self.assertTrue(result.truncated)                              # reported, not silently complete

    @unittest.skipUnless(HAS_RG, "ripgrep not installed")
    def test_regex_unsupported_by_ripgrep_falls_back_to_python(self):
        result = self.run_search("ripgrep", r"parse_(?=date)", regex=True)
        self.assertEqual(result.engine, "python")
        self.assertTrue(result.matches)

    def test_tool_uses_python_when_ripgrep_is_missing(self):
        registry = build_registry(self.ctx)
        with mock.patch("harness.tools.search.shutil.which", return_value=None):
            result = registry.dispatch("search_text", {"query": "parse_date"})
        self.assertTrue(result.success)
        self.assertEqual(result.data.engine, "python")
        self.assertEqual(len(result.data.matches), 4)

    def test_errors_are_structured(self):
        registry = build_registry(self.ctx)
        self.assertTrue(registry.dispatch("search_text", {"query": "("}).success)  # literal by default
        self.assertEqual(registry.dispatch("search_text", {"query": "(", "regex": True}).error.code,
                         "invalid_arguments")
        self.assertEqual(registry.dispatch("search_text", {"query": "x", "path": "../outside"}).error.code,
                         "path_outside_repo")
        self.assertEqual(registry.dispatch("search_text", {"query": ""}).error.code, "invalid_arguments")


class ScopedSearchTest(RepoTestCase):
    """``allowed_paths`` (used by repository intelligence) keeps both engines identical."""

    def setUp(self):
        super().setUp()
        self.write("a.py", "needle\n")
        self.write("sub/b.py", "needle\n")
        self.write("sub/c.py", "needle\n")
        self.write("big.txt", "needle\n" + "y" * 500)
        self.write("bin.dat", b"needle\x00\x01", mode="wb")
        self.write("ff.txt", "a\x0cneedle\nneedle\u2028x\nneedle\n")
        self.ctx = ToolContext.create(self.repo, limits=ToolLimits(max_file_bytes=200))

    def test_scope_restricts_results_identically(self):
        allowed = ["a.py", "sub/c.py", "big.txt", "bin.dat", "ff.txt", "missing.py"]
        for engine in ENGINES:
            with self.subTest(engine=engine):
                result = search(self.ctx, "needle", engine=engine, allowed_paths=allowed)
                self.assertEqual([(m.path, m.line) for m in result.matches],
                                 [("a.py", 1), ("ff.txt", 1), ("ff.txt", 2), ("ff.txt", 3), ("sub/c.py", 1)])

    def test_scope_combines_with_path(self):
        for engine in ENGINES:
            with self.subTest(engine=engine):
                result = search(self.ctx, "needle", path="sub", engine=engine, allowed_paths=["a.py", "sub/b.py"])
                self.assertEqual([m.path for m in result.matches], ["sub/b.py"])

    def test_empty_scope(self):
        self.assertEqual(search(self.ctx, "needle", allowed_paths=[]).matches, ())

    def test_default_behaviour_unchanged(self):
        for engine in ENGINES:
            with self.subTest(engine=engine):
                paths = {m.path for m in search(self.ctx, "needle", engine=engine).matches}
                self.assertEqual(paths, {"a.py", "sub/b.py", "sub/c.py", "ff.txt"})

    def test_line_numbers_split_on_newline_only(self):
        for engine in ENGINES:
            with self.subTest(engine=engine):
                lines = [m.line for m in search(self.ctx, "needle", path="ff.txt", engine=engine).matches]
                self.assertEqual(lines, [1, 2, 3])


if __name__ == "__main__":
    unittest.main()

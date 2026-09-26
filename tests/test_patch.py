import os
import stat
import textwrap
import unittest

from harness.tools import ToolContext, build_registry

from tests.helpers import RepoTestCase

CALC = "def add(a, b):\n    return a - b\n\n\ndef mul(a, b):\n    return a * b\n"


def diff(text):
    return textwrap.dedent(text).lstrip("\n")


class PatchTest(RepoTestCase):
    def setUp(self):
        super().setUp()
        self.write("calc.py", CALC)
        self.ctx = ToolContext.create(self.repo)
        self.registry = build_registry(self.ctx)

    def apply(self, patch):
        return self.registry.dispatch("apply_patch", {"patch": patch})

    def read(self, rel):
        return (self.repo / rel).read_text(encoding="utf-8")

    def test_minimal_change(self):
        result = self.apply(diff("""
            --- a/calc.py
            +++ b/calc.py
            @@ -1,2 +1,2 @@
             def add(a, b):
            -    return a - b
            +    return a + b
        """))
        self.assertTrue(result.success, result.error)
        self.assertEqual(self.read("calc.py"), CALC.replace("a - b", "a + b"))
        self.assertEqual(result.data.changed_files, ("calc.py",))
        f = result.data.files[0]
        self.assertEqual((f.action, f.hunks, f.added, f.removed), ("modified", 1, 1, 1))
        self.assertEqual(result.data.warnings, ())

    def test_wrong_line_numbers_are_tolerated_via_context(self):
        result = self.apply(diff("""
            --- a/calc.py
            +++ b/calc.py
            @@ -40,2 +40,2 @@ def mul
             def mul(a, b):
            -    return a * b
            +    return b * a
        """))
        self.assertTrue(result.success, result.error)
        self.assertIn("return b * a", self.read("calc.py"))

    def test_multiple_hunks_and_insertion(self):
        result = self.apply(diff("""
            --- a/calc.py
            +++ b/calc.py
            @@ -0,0 +1,1 @@
            +\"\"\"Calculator.\"\"\"
            @@ -5,2 +6,3 @@
             def mul(a, b):
            +    # multiply
                 return a * b
        """))
        self.assertTrue(result.success, result.error)
        self.assertEqual(self.read("calc.py"),
                         '"""Calculator."""\n' + CALC.replace("def mul(a, b):\n", "def mul(a, b):\n    # multiply\n"))

    def test_context_mismatch_fails_cleanly_with_diagnostics(self):
        result = self.apply(diff("""
            --- a/calc.py
            +++ b/calc.py
            @@ -1,2 +1,2 @@
             def add(x, y):
            -    return x - y
            +    return x + y
        """))
        self.assertFalse(result.success)
        self.assertEqual(result.error.code, "patch_mismatch")
        self.assertIn("hunk 1", result.error.message)
        self.assertIn("expected 'def add(x, y):'", result.error.message)
        self.assertEqual(result.error.details["file"], "calc.py")
        self.assertEqual(self.read("calc.py"), CALC)

    def test_all_or_nothing_across_files(self):
        self.write("other.py", "x = 1\n")
        result = self.apply(diff("""
            --- a/other.py
            +++ b/other.py
            @@ -1 +1 @@
            -x = 1
            +x = 2
            --- a/calc.py
            +++ b/calc.py
            @@ -1,2 +1,2 @@
             def nothing_like_this():
            -    pass
            +    return 1
        """))
        self.assertFalse(result.success)
        self.assertEqual(self.read("other.py"), "x = 1\n")
        self.assertEqual(self.read("calc.py"), CALC)

    def test_create_and_delete_files(self):
        self.write("old.txt", "bye\n")
        result = self.apply(diff("""
            --- /dev/null
            +++ b/pkg/new_module.py
            @@ -0,0 +1,2 @@
            +def hello():
            +    return "hi"
            --- a/old.txt
            +++ /dev/null
            @@ -1 +0,0 @@
            -bye
        """))
        self.assertTrue(result.success, result.error)
        self.assertEqual(self.read("pkg/new_module.py"), 'def hello():\n    return "hi"\n')
        self.assertFalse((self.repo / "old.txt").exists())
        actions = {f.path: f.action for f in result.data.files}
        self.assertEqual(actions, {"pkg/new_module.py": "created", "old.txt": "deleted"})

    def test_create_existing_file_is_a_conflict(self):
        result = self.apply("--- /dev/null\n+++ b/calc.py\n@@ -0,0 +1 @@\n+x\n")
        self.assertEqual(result.error.code, "patch_conflict")

    def test_outside_repository_is_rejected(self):
        for target in ("../outside/secret.txt", str(self.outside / "secret.txt"), "../../../../etc/passwd"):
            with self.subTest(target=target):
                result = self.apply(f"--- a/{target}\n+++ b/{target}\n@@ -1 +1 @@\n-outside data\n+pwned\n")
                self.assertFalse(result.success)
                self.assertEqual(result.error.code, "path_outside_repo")
        self.assertEqual((self.outside / "secret.txt").read_text(encoding="utf-8"), "outside data\n")

    def test_symlink_escape_and_git_dir_are_rejected(self):
        os.symlink(self.outside, self.repo / "link")
        result = self.apply("--- a/link/secret.txt\n+++ b/link/secret.txt\n@@ -1 +1 @@\n-outside data\n+pwned\n")
        self.assertEqual(result.error.code, "path_outside_repo")
        created = self.apply("--- /dev/null\n+++ b/link/new.txt\n@@ -0,0 +1 @@\n+x\n")
        self.assertEqual(created.error.code, "path_outside_repo")
        self.assertFalse((self.outside / "new.txt").exists())
        git = self.apply("--- /dev/null\n+++ b/.git/hooks/pre-commit\n@@ -0,0 +1 @@\n+evil\n")
        self.assertEqual(git.error.code, "path_not_writable")

    def test_preserves_crlf_and_missing_final_newline(self):
        self.write("win.txt", "one\r\ntwo\r\nthree")
        result = self.apply(diff("""
            --- a/win.txt
            +++ b/win.txt
            @@ -2,2 +2,2 @@
             two
            -three
            \\ No newline at end of file
            +THREE
            \\ No newline at end of file
        """))
        self.assertTrue(result.success, result.error)
        self.assertEqual((self.repo / "win.txt").read_bytes(), b"one\r\ntwo\r\nTHREE")

    def test_trailing_whitespace_tolerance_is_reported(self):
        self.write("ws.py", "value = 1   \nother = 2\n")
        result = self.apply("--- a/ws.py\n+++ b/ws.py\n@@ -1,2 +1,2 @@\n value = 1\n-other = 2\n+other = 3\n")
        self.assertTrue(result.success, result.error)
        self.assertEqual(self.read("ws.py"), "value = 1   \nother = 3\n")  # context keeps file's text
        self.assertIn("trailing whitespace", result.data.warnings[0])

    def test_blank_separator_lines_between_files(self):
        self.write("b.txt", "b\n")
        result = self.apply("--- a/calc.py\n+++ b/calc.py\n@@ -1 +1 @@\n-def add(a, b):\n+def add(a, b):  # sum\n"
                            "\n\n--- a/b.txt\n+++ b/b.txt\n@@ -1 +1 @@\n-b\n+B\n")
        self.assertTrue(result.success, result.error)
        self.assertEqual(self.read("b.txt"), "B\n")

    def test_file_mode_is_preserved(self):
        script = self.write("run.sh", "#!/bin/sh\necho a\n")
        script.chmod(0o755)
        self.apply("--- a/run.sh\n+++ b/run.sh\n@@ -2 +2 @@\n-echo a\n+echo b\n")
        self.assertTrue(stat.S_IMODE(script.stat().st_mode) & stat.S_IXUSR)

    def test_unsupported_and_invalid_patches(self):
        cases = [
            ("not a diff at all", "patch_invalid"),
            ("diff --git a/x b/y\nsimilarity index 100%\nrename from x\nrename to y\n", "patch_unsupported"),
            ("--- a/calc.py\n+++ b/calc.py\n@@ -1 +1 @@\n def add(a, b):\n", "patch_invalid"),
            ("--- a/missing.py\n+++ b/missing.py\n@@ -1 +1 @@\n-a\n+b\n", "not_found"),
        ]
        for patch, code in cases:
            with self.subTest(code=code):
                result = self.apply(patch)
                self.assertFalse(result.success)
                self.assertEqual(result.error.code, code)
        self.assertEqual(self.read("calc.py"), CALC)


if __name__ == "__main__":
    unittest.main()

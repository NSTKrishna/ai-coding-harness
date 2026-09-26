import io
import unittest
from pathlib import Path

from harness.ui.input import InputError, default_repository, prompt_line, prompt_multiline, prompt_task


class PromptLineTest(unittest.TestCase):
    def test_enter_submits_immediately(self):
        out = io.StringIO()
        text = prompt_line("Repository", io.StringIO("/tmp/repo\n"), out)
        self.assertEqual(text, "/tmp/repo")

    def test_empty_line_uses_default(self):
        out = io.StringIO()
        text = prompt_line("Repository", io.StringIO("\n"), out, default=".")
        self.assertEqual(text, ".")

    def test_eof_raises(self):
        with self.assertRaises(InputError):
            prompt_line("Repository", io.StringIO(""), io.StringIO())

    def test_default_shown_in_prompt(self):
        out = io.StringIO()
        prompt_line("Repository", io.StringIO("x\n"), out, default=".")
        self.assertIn("[.]", out.getvalue())


class PromptTaskTest(unittest.TestCase):
    def test_single_line_task_submits_on_enter(self):
        out = io.StringIO()
        task = prompt_task(io.StringIO("Fix the parser bug\n"), out)
        self.assertEqual(task, "Fix the parser bug")

    def test_github_issue_url_submits_on_enter(self):
        out = io.StringIO()
        url = "https://github.com/owner/repo/issues/7"
        task = prompt_task(io.StringIO(f"{url}\n"), out)
        self.assertEqual(task, url)

    def test_multi_sentinel_switches_to_multiline(self):
        out = io.StringIO()
        stdin = io.StringIO(":multi\nFirst line\nSecond line\n\n")
        task = prompt_task(stdin, out)
        self.assertEqual(task, "First line\nSecond line")
        self.assertIn("Multiline task", out.getvalue())

    def test_eof_raises(self):
        with self.assertRaises(InputError):
            prompt_task(io.StringIO(""), io.StringIO())

    def test_blank_line_is_returned_as_empty_string(self):
        # validated by the caller (cli.py), not this module
        self.assertEqual(prompt_task(io.StringIO("\n"), io.StringIO()), "")


class PromptMultilineTest(unittest.TestCase):
    def test_blank_line_ends_input(self):
        stdin = io.StringIO("line one\nline two\n\nunread after blank\n")
        text = prompt_multiline(stdin, io.StringIO())
        self.assertEqual(text, "line one\nline two")

    def test_eof_also_ends_input(self):
        stdin = io.StringIO("only line")
        text = prompt_multiline(stdin, io.StringIO())
        self.assertEqual(text, "only line")

    def test_no_input_gives_empty_string(self):
        self.assertEqual(prompt_multiline(io.StringIO(""), io.StringIO()), "")


class DefaultRepositoryTest(unittest.TestCase):
    def test_existing_directory_defaults_to_dot(self):
        self.assertEqual(default_repository(Path(".")), ".")

    def test_nonexistent_path_has_no_default(self):
        self.assertEqual(default_repository(Path("/nonexistent/should/not/exist/xyz")), "")


if __name__ == "__main__":
    unittest.main()

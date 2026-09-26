import shutil
import sys
import time
import unittest

from harness.tools import ToolContext, ToolFailure, ToolLimits, build_registry
from harness.tools.commands import check_command, execute, parse_command

from tests.helpers import RepoTestCase

PY = sys.executable


class ParseCommandTest(unittest.TestCase):
    def test_string_uses_posix_quoting(self):
        self.assertEqual(parse_command('pytest -k "a or b" tests/x.py::T'),
                         ["pytest", "-k", "a or b", "tests/x.py::T"])

    def test_list_is_used_as_is(self):
        self.assertEqual(parse_command(["bash", "-c", "a | b"]), ["bash", "-c", "a | b"])

    def test_shell_operators_are_rejected_not_misinterpreted(self):
        for command in ("pytest | tail", "make && make test", "echo x > out.txt", "a; b", "sleep 1 &", "a\nb"):
            with self.subTest(command=command):
                with self.assertRaises(ToolFailure) as ctx:
                    parse_command(command)
                self.assertIn("bash", ctx.exception.message)

    def test_invalid(self):
        for command in ("", "   ", [], ["", "x"], 'echo "unbalanced'):
            with self.subTest(command=command):
                with self.assertRaises(ToolFailure):
                    parse_command(command)


class PolicyTest(RepoTestCase):
    def blocked(self, command):
        with self.assertRaises(ToolFailure) as ctx:
            check_command(parse_command(command), self.repo, self.repo)
        self.assertEqual(ctx.exception.code, "command_blocked")
        return ctx.exception.message

    def allowed(self, command):
        check_command(parse_command(command), self.repo, self.repo)

    def test_destructive_system_commands_are_blocked(self):
        for command in ("sudo rm -rf /", "shutdown -h now", "reboot", "mkfs /dev/sda", "mkfs.ext4 /dev/sda1",
                        "/usr/bin/sudo ls", "env FOO=1 sudo ls", "timeout 5 reboot", "nohup halt",
                        "nice -n 5 poweroff", "dd if=/dev/zero of=/dev/sda", "doas ls", "su root"):
            with self.subTest(command=command):
                self.blocked(command)

    def test_rm_is_confined_to_the_repository(self):
        for command in ("rm -rf /", "rm -rf ..", "rm -rf .", "rm -rf .git", f"rm {self.outside}/secret.txt",
                        "rm -rf ~", "rm -rf *", "chmod -R 000 /", "rmdir /tmp"):
            with self.subTest(command=command):
                self.blocked(command)
        self.allowed("rm -rf build dist")
        self.allowed("chmod +x scripts/run.sh")

    def test_git_is_limited_to_read_only_commands(self):
        for command in ("git commit -m x", "git push", "git reset --hard", "git checkout -- .", "git clean -fdx",
                        "git add -A", "git stash", "git -C / status", "git -c core.pager=sh status",
                        "git --git-dir=/tmp/x status", "git diff --output=/tmp/x"):
            with self.subTest(command=command):
                self.blocked(command)
        for command in ("git status", "git diff --stat", "git log --oneline -5", "git show HEAD",
                        "git --no-pager log", "git -C . status", "git ls-files", "git grep foo"):
            with self.subTest(command=command):
                self.allowed(command)

    def test_explicit_shell_scripts_are_inspected(self):
        for script in ("echo hi; sudo ls", "echo hi\nrm -rf /", "x=$(sudo id)", "echo `reboot`",
                       "echo data > /etc/hosts", "ls | xargs rm", "FOO=1 git push"):
            with self.subTest(script=script):
                self.blocked(["bash", "-c", script])
        self.blocked(["sh", "-ec", "sudo ls"])
        self.allowed(["bash", "-c", "python -m pytest -q 2>&1 | tail -20"])
        self.allowed(["bash", "-lc", "make test > build/test.log"])

    def test_normal_development_commands_are_allowed(self):
        for command in ("python -m pytest -q", "python -m unittest discover", "pytest tests/test_x.py",
                        "make test", "npm test", "npm run build", "go test ./...", "cargo test",
                        "node script.js", "ls -la", "cat README.md"):
            with self.subTest(command=command):
                self.allowed(command)


class ExecuteTest(RepoTestCase):
    def setUp(self):
        super().setUp()
        self.ctx = ToolContext.create(self.repo, limits=ToolLimits(command_timeout_seconds=30))
        self.registry = build_registry(self.ctx)

    def run_cmd(self, command, tool="run_command", **args):
        result = self.registry.dispatch(tool, {"command": command, **args})
        self.assertTrue(result.success, result.error)
        return result.data

    def test_success(self):
        data = self.run_cmd([PY, "-c", "print('hello')"])
        self.assertEqual((data.stdout, data.stderr, data.exit_code), ("hello\n", "", 0))
        self.assertTrue(data.ok)
        self.assertFalse(data.timed_out)
        self.assertGreaterEqual(data.duration_ms, 0)
        self.assertEqual(data.cwd, ".")

    def test_non_zero_exit_and_stderr(self):
        data = self.run_cmd([PY, "-c", "import sys; print('out'); sys.stderr.write('bad thing\\n'); sys.exit(3)"])
        self.assertEqual(data.exit_code, 3)
        self.assertFalse(data.ok)
        self.assertEqual(data.stdout, "out\n")
        self.assertEqual(data.stderr, "bad thing\n")

    def test_runs_in_repository_root_and_subdirectory(self):
        (self.repo / "pkg").mkdir()
        self.assertEqual(self.run_cmd([PY, "-c", "import os; print(os.getcwd())"]).stdout.strip(), str(self.repo))
        sub = self.run_cmd([PY, "-c", "import os; print(os.getcwd())"], cwd="pkg")
        self.assertEqual(sub.stdout.strip(), str(self.repo / "pkg"))
        self.assertEqual(sub.cwd, "pkg")

    def test_cwd_cannot_leave_the_repository(self):
        import os
        os.symlink(self.outside, self.repo / "escape")
        for cwd in ("..", "../..", str(self.outside), "/", "escape"):
            with self.subTest(cwd=cwd):
                result = self.registry.dispatch("run_command", {"command": "ls", "cwd": cwd})
                self.assertEqual(result.error.code, "path_outside_repo")
        self.assertEqual(self.ctx.metrics.command_calls, 0)

    def test_timeout(self):
        start = time.monotonic()
        data = self.run_cmd([PY, "-c", "import time; time.sleep(30)"], timeout_seconds=1)
        self.assertTrue(data.timed_out)
        self.assertIsNone(data.exit_code)
        self.assertFalse(data.ok)
        self.assertLess(time.monotonic() - start, 10)

    def test_timeout_is_capped_by_configuration(self):
        ctx = ToolContext.create(self.repo, limits=ToolLimits(command_timeout_seconds=1))
        data = build_registry(ctx).dispatch(
            "run_command", {"command": [PY, "-c", "import time; time.sleep(30)"], "timeout_seconds": 999}).data
        self.assertTrue(data.timed_out)

    @unittest.skipUnless(shutil.which("bash"), "bash not installed")
    def test_timeout_kills_child_processes(self):
        marker = self.repo / "marker"
        execute(["bash", "-c", f"sleep 1.5; touch '{marker}'"], root=self.repo, timeout=0.3,
                max_output_bytes=1000)
        time.sleep(2)
        self.assertFalse(marker.exists())

    def test_output_is_bounded_keeping_head_and_tail(self):
        ctx = ToolContext.create(self.repo, limits=ToolLimits(max_output_bytes=1000))
        script = "for i in range(20000): print(f'line {i}')"
        data = build_registry(ctx).dispatch("run_command", {"command": [PY, "-c", script]}).data
        self.assertTrue(data.truncated)
        self.assertTrue(data.stdout.startswith("line 0\n"))
        self.assertTrue(data.stdout.endswith("line 19999\n"))
        self.assertIn("bytes omitted", data.stdout)
        self.assertLess(len(data.stdout), 1100)
        self.assertGreater(data.stdout_bytes, 100_000)

    def test_same_size_edit_in_the_same_second_is_not_hidden_by_bytecode_cache(self):
        (self.repo / "mod.py").write_text("VALUE = 2\n")
        check = [PY, "-c", "import mod; print(mod.VALUE)"]
        self.assertEqual(self.run_cmd(check).stdout, "2\n")
        (self.repo / "mod.py").write_text("VALUE = 1\n")          # same size, same second
        self.assertEqual(self.run_cmd(check).stdout, "1\n")
        self.assertFalse((self.repo / "__pycache__").exists())

    def test_command_not_found_is_structured(self):
        result = self.registry.dispatch("run_command", {"command": "definitely-not-a-real-command-xyz"})
        self.assertEqual(result.error.code, "command_not_found")

    def test_blocked_command_is_structured_and_not_launched(self):
        result = self.registry.dispatch("run_command", {"command": "sudo ls"})
        self.assertFalse(result.success)
        self.assertEqual(result.error.code, "command_blocked")
        self.assertEqual((self.ctx.metrics.tool_calls, self.ctx.metrics.command_calls), (1, 0))

    def test_counters(self):
        self.run_cmd([PY, "-c", "pass"])
        self.run_cmd([PY, "-c", "import sys; sys.exit(1)"])
        self.run_cmd([PY, "-c", "pass"], tool="run_tests")
        self.registry.dispatch("run_command", {"command": "reboot"})
        self.registry.dispatch("run_command", {"command": 42})
        m = self.ctx.metrics
        self.assertEqual(m.command_calls, 3)
        self.assertEqual(m.tool_calls, 5)
        self.assertEqual(m.tool_failures, 2)


class RunTestsToolTest(RepoTestCase):
    def setUp(self):
        super().setUp()
        self.write("calc.py", "def add(a, b):\n    return a - b\n")
        self.write("tests/__init__.py", "")
        self.write("tests/test_calc.py",
                   "import unittest\nimport calc\n\n"
                   "class T(unittest.TestCase):\n"
                   "    def test_add(self):\n        self.assertEqual(calc.add(2, 3), 5)\n")
        self.ctx = ToolContext.create(self.repo)
        self.registry = build_registry(self.ctx)
        self.command = [PY, "-m", "unittest", "discover", "-s", "tests", "-t", "."]

    def test_failing_tests_are_a_structured_result(self):
        result = self.registry.dispatch("run_tests", {"command": self.command})
        self.assertTrue(result.success)
        data = result.data
        self.assertNotEqual(data.exit_code, 0)
        self.assertFalse(data.ok)
        self.assertIn("AssertionError: -1 != 5", data.stderr)
        self.assertIn("FAILED (failures=1)", data.stderr)

    def test_passing_tests(self):
        (self.repo / "calc.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
        data = self.registry.dispatch("run_tests", {"command": self.command}).data
        self.assertTrue(data.ok)
        self.assertIn("OK", data.stderr)
        self.assertEqual(self.ctx.metrics.command_calls, 1)

    def test_run_tests_applies_the_same_policy(self):
        result = self.registry.dispatch("run_tests", {"command": "sudo make test"})
        self.assertEqual(result.error.code, "command_blocked")


if __name__ == "__main__":
    unittest.main()

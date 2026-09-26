"""Deterministic command classification, fingerprints, comparisons and ledgers (no model)."""

import unittest

from harness.tools.base import ToolError, ToolResult
from harness.tools.commands import CommandResult
from harness.tools.patch import PatchedFile
from harness.verify.ledger import ChangeLedger, EvidenceKind, EvidenceLedger
from harness.verify.outcomes import (
    CommandStatus,
    Comparison,
    classify,
    compare,
    failing_tests,
    tests_run,
)

PY_TEST = ("python", "-m", "unittest")


def run(exit_code=0, stdout="", stderr="", timed_out=False, argv=PY_TEST):
    data = CommandResult(tuple(argv), ".", stdout, stderr, None if timed_out else exit_code, timed_out, 5, False,
                         len(stdout), len(stderr))
    return ToolResult("run_tests", True, data)


def tool_error(code, message="x"):
    return ToolResult("run_tests", False, error=ToolError(code, message))


UNITTEST_311 = "FAIL: test_add_one (tests.test_m.AddOneTest.test_add_one)\n---\nRan 3 tests in 0.001s\nFAILED (failures=1)"
UNITTEST_310 = "FAIL: test_add_one (tests.test_m.AddOneTest)\n---\nRan 3 tests in 0.001s\nFAILED (failures=1)"


class ClassifyTest(unittest.TestCase):
    def test_pass_and_failures_by_kind(self):
        self.assertEqual(classify("test", PY_TEST, run(0)).status, CommandStatus.PASS)
        self.assertEqual(classify("test", PY_TEST, run(1, stderr=UNITTEST_311)).status, CommandStatus.TEST_FAILURE)
        self.assertEqual(classify("build", ("make", "build"), run(2)).status, CommandStatus.BUILD_FAILURE)
        self.assertEqual(classify("lint", ("ruff", "check", "."), run(1)).status, CommandStatus.LINT_FAILURE)
        self.assertEqual(classify("typecheck", ("mypy", "."), run(1)).status, CommandStatus.TYPECHECK_FAILURE)

    def test_environment_errors(self):
        cases = [
            (("python", "-m", "pytest"), run(1, stderr="/usr/bin/python: No module named pytest")),
            (("python", "-m", "nose2"), run(1, stderr="No module named nose2")),
            (("npm", "test"), run(1, stderr="npm ERR! Missing script: \"test\"")),
            (("make", "test"), run(2, stderr="make: nonexistent_tool: No such file or directory\nmake: *** [test] Error 1")),
            (("make", "test"), run(2, stderr="make[1]: nonexistent_tool: No such file or directory\nmake[1]: *** [test] Error 1")),
            (("sh", "-c", "x"), run(127, stderr="sh: x: command not found")),
            (("tool",), tool_error("command_not_found", "command not found: tool")),
        ]
        for argv, result in cases:
            with self.subTest(argv=argv):
                self.assertEqual(classify("test", argv, result).status, CommandStatus.ENVIRONMENT_ERROR)

    def test_missing_project_module_is_a_code_failure(self):
        result = run(1, stderr="ModuleNotFoundError: No module named 'src.parser'")
        self.assertEqual(classify("test", PY_TEST, result).status, CommandStatus.TEST_FAILURE)

    def test_timeout_and_tool_error(self):
        self.assertEqual(classify("test", PY_TEST, run(timed_out=True)).status, CommandStatus.TIMEOUT)
        self.assertEqual(classify("test", PY_TEST, tool_error("command_blocked")).status, CommandStatus.TOOL_ERROR)

    def test_failing_test_ids_are_the_same_across_python_versions(self):
        self.assertEqual(failing_tests(UNITTEST_311), failing_tests(UNITTEST_310))
        self.assertEqual(failing_tests(UNITTEST_311), {"tests.test_m.AddOneTest.test_add_one"})

    def test_other_runners(self):
        self.assertEqual(failing_tests("FAILED tests/test_a.py::test_x - AssertionError\nERROR tests/test_b.py::test_y"),
                         {"tests/test_a.py::test_x", "tests/test_b.py::test_y"})
        self.assertEqual(failing_tests("--- FAIL: TestParse (0.00s)"), {"TestParse"})
        self.assertEqual(failing_tests("test parser::tests::empty ... FAILED"), {"parser::tests::empty"})
        self.assertEqual(tests_run("Ran 12 tests in 0.1s"), 12)
        self.assertEqual(tests_run("=== 3 failed, 10 passed in 0.2s ==="), 13)


class CompareTest(unittest.TestCase):
    def c(self, result, kind="test"):
        return classify(kind, PY_TEST, result)

    def test_cases(self):
        passing = self.c(run(0, stderr="Ran 3 tests"))
        failing = self.c(run(1, stderr=UNITTEST_311))
        same_failure_other_message = self.c(run(1, stderr=UNITTEST_311.replace("FAILED", "FAILED ")))
        two = self.c(run(1, stderr="FAIL: test_a (m.T.test_a)\nFAIL: test_b (m.T.test_b)\nRan 3 tests"))
        one = self.c(run(1, stderr="FAIL: test_b (m.T.test_b)\nRan 3 tests"))
        other = self.c(run(1, stderr="FAIL: test_c (m.T.test_c)\nRan 3 tests"))
        env = self.c(run(1, stderr="No module named unittest"))
        cases = [
            (passing, passing, Comparison.UNCHANGED_PASS),
            (failing, passing, Comparison.FIXED),
            (passing, failing, Comparison.REGRESSED),
            (failing, same_failure_other_message, Comparison.UNCHANGED_FAILURE),
            (two, one, Comparison.IMPROVED),
            (one, two, Comparison.CHANGED_FAILURE),
            (one, other, Comparison.CHANGED_FAILURE),
            (env, passing, Comparison.ENVIRONMENT),
            (passing, env, Comparison.ENVIRONMENT),
            (passing, self.c(run(timed_out=True)), Comparison.REGRESSED),
            (self.c(run(timed_out=True)), self.c(run(timed_out=True)), Comparison.NOT_COMPARABLE),
            (None, passing, Comparison.NO_BASELINE),
            (passing, self.c(tool_error("command_blocked")), Comparison.NOT_COMPARABLE),
        ]
        for baseline, post, expected in cases:
            with self.subTest(expected=expected):
                self.assertEqual(compare(baseline, post), expected)

    def test_failures_without_test_ids_compare_normalized_output(self):
        a = self.c(run(1, stderr="error at line 12, took 0.51s"))
        b = self.c(run(1, stderr="error at line 12, took 0.93s"))      # only numbers differ
        c = self.c(run(1, stderr="a different error entirely"))
        self.assertEqual(compare(a, b), Comparison.UNCHANGED_FAILURE)
        self.assertNotEqual(a.fingerprint.output_hash, c.fingerprint.output_hash)
        # AUDIT M3: without test ids a changed hash is noise (ordering, messages), not a regression
        self.assertEqual(compare(a, c), Comparison.UNCHANGED_FAILURE)

    def test_identified_failures_appearing_or_disappearing_is_a_change(self):
        no_ids = self.c(run(1, stderr="Traceback: ImportError: cannot import name 'x'"))
        with_ids = self.c(run(1, stderr="FAIL: test_a (tests.test_m.T)\nRan 1 test"))
        self.assertTrue(with_ids.fingerprint.failing_tests)
        self.assertEqual(compare(no_ids, with_ids), Comparison.CHANGED_FAILURE)
        self.assertEqual(compare(with_ids, no_ids), Comparison.CHANGED_FAILURE)


class LedgerTest(unittest.TestCase):
    def test_ids_are_sequential_and_assessments_must_reference_real_items(self):
        ledger = EvidenceLedger()
        first = ledger.record_command("baseline", "V1", "test", "python -m unittest", classify("test", PY_TEST, run(1)))
        second = ledger.record_baseline_unavailable("none")
        self.assertEqual((first.id, second.id), ("E1", "E2"))
        self.assertEqual(ledger.get("E1"), first)
        self.assertIsNone(ledger.get("E99"))
        assessment = ledger.record_assessment("post-1", "criterion", "PASS", ["E1"], "note")
        self.assertEqual((assessment.id, assessment.refs), ("E3", ("E1",)))
        with self.assertRaises(ValueError):
            ledger.record_assessment("post-1", "criterion", "PASS", ["E42"], "fabricated")
        self.assertEqual([i.kind for i in ledger.items],
                         [EvidenceKind.TEST, EvidenceKind.BASELINE, EvidenceKind.ACCEPTANCE_ASSESSMENT])

    def test_environment_results_are_environment_evidence(self):
        ledger = EvidenceLedger()
        item = ledger.record_command("baseline", "V1", "test", "python -m pytest",
                                     classify("test", ("python", "-m", "pytest"), run(1, stderr="No module named pytest")))
        self.assertEqual((item.kind, item.result), (EvidenceKind.ENVIRONMENT, "ENVIRONMENT_ERROR"))

    def test_change_ledger(self):
        changes = ChangeLedger()
        changes.record_patch([PatchedFile("a.py", "modified", 1, 1, 1, "h0", "h1")], "execute")
        changes.record_patch([PatchedFile("a.py", "modified", 1, 1, 1, "h1", "h2"),
                              PatchedFile("b.py", "created", 1, 3, 0, None, "hb")], "repair-1")
        changes.record_patch([PatchedFile("c.py", "modified", 1, 1, 1, "c0", "c1")], "repair-1")
        changes.record_patch([PatchedFile("c.py", "modified", 1, 1, 1, "c1", "c0")], "repair-1")   # reverted
        a = changes.records[0]
        self.assertEqual((a.before_sha256, a.current_sha256, a.touched_in, a.patches), ("h0", "h2", ["execute", "repair-1"], 2))
        self.assertEqual(changes.net_changed(), ("a.py", "b.py"))


if __name__ == "__main__":
    unittest.main()

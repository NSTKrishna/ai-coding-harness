"""Helpers for M4 tests: a small buggy repository and scripted model responses."""

import json
from pathlib import Path

from harness.model import text_response
from tests.repo_fixtures import git_repo

BUGGY_REPO = {
    "src/__init__.py": "",
    "src/math_utils.py": "def add_one(x):\n    return x + 2\n\n\ndef double(x):\n    return x * 2\n",
    "tests/__init__.py": "",
    "tests/test_math_utils.py": (
        "import unittest\n\nfrom src.math_utils import add_one\n\n\n"
        "class AddOneTest(unittest.TestCase):\n"
        "    def test_add_one(self):\n        self.assertEqual(add_one(1), 2)\n"
    ),
}
TASK = "Fix add_one so add_one(1) returns 2."

FIX_PATCH = (
    "--- a/src/math_utils.py\n+++ b/src/math_utils.py\n"
    "@@ -1,2 +1,2 @@\n def add_one(x):\n-    return x + 2\n+    return x + 1\n"
)


def buggy_repo(base: Path) -> Path:
    return git_repo(base / "buggy", BUGGY_REPO)


def plan_dict(**overrides) -> dict:
    plan = {
        "understanding": "add_one adds 2 instead of 1.",
        "acceptance_criteria": ["add_one(1) returns 2"],
        "hypotheses": ["src/math_utils.py returns x + 2"],
        "files_to_inspect": ["src/math_utils.py"],
        "steps": [{"kind": "inspect", "description": "Read add_one in src/math_utils.py"},
                  {"kind": "edit", "description": "Return x + 1"}],
        "verification_candidates": [],
        "risks": [],
    }
    plan.update(overrides)
    return plan


def plan_response(**overrides):
    return text_response(json.dumps(plan_dict(**overrides)))


def tool(name: str, **arguments):
    return text_response(json.dumps({"action": "tool", "tool": name, "arguments": arguments}))


def complete(summary: str = "add_one fixed"):
    return text_response(json.dumps({"action": "complete", "summary": summary}))


def blocked(reason: str = "cannot continue"):
    return text_response(json.dumps({"action": "blocked", "reason": reason}))


# ---------------------------------------------------------------------------
# M5 fixtures
# ---------------------------------------------------------------------------

import sys as _sys

UNITTEST_CMD = f"{_sys.executable} -m unittest discover -s tests -t ."

WRONG_PATCH = (   # compiles, still wrong: add_one(1) == 4
    "--- a/src/math_utils.py\n+++ b/src/math_utils.py\n"
    "@@ -1,2 +1,2 @@\n def add_one(x):\n-    return x + 2\n+    return x + 3\n"
)
REPAIR_PATCH = (
    "--- a/src/math_utils.py\n+++ b/src/math_utils.py\n"
    "@@ -1,2 +1,2 @@\n def add_one(x):\n-    return x + 3\n+    return x + 1\n"
)
SECOND_WRONG_PATCH = (
    "--- a/src/math_utils.py\n+++ b/src/math_utils.py\n"
    "@@ -1,2 +1,2 @@\n def add_one(x):\n-    return x + 3\n+    return x + 4\n"
)

# A repository whose tests pass; the task only asks for a docstring.
PASSING_REPO = {
    **BUGGY_REPO,
    "src/math_utils.py": "def add_one(x):\n    return x + 1\n\n\ndef double(x):\n    return x * 2\n",
    "tests/test_math_utils.py": (
        "import unittest\n\nfrom src.math_utils import add_one, double\n\n\n"
        "class MathTest(unittest.TestCase):\n"
        "    def test_add_one(self):\n        self.assertEqual(add_one(1), 2)\n\n"
        "    def test_double(self):\n        self.assertEqual(double(3), 6)\n"
    ),
}
BREAK_DOUBLE_PATCH = (
    "--- a/src/math_utils.py\n+++ b/src/math_utils.py\n"
    "@@ -5,2 +5,3 @@\n def double(x):\n-    return x * 2\n+    \"\"\"Return twice x.\"\"\"\n+    return x * 3\n"
)
FIX_DOUBLE_PATCH = (
    "--- a/src/math_utils.py\n+++ b/src/math_utils.py\n"
    "@@ -6,2 +6,2 @@\n     \"\"\"Return twice x.\"\"\"\n-    return x * 3\n+    return x * 2\n"
)

# Plain assert tests (no unittest import, no pytest config): nothing to discover.
NO_COMMAND_REPO = {
    "src/__init__.py": "",
    "src/math_utils.py": BUGGY_REPO["src/math_utils.py"],
    "tests/test_math_utils.py": "from src.math_utils import add_one\n\n\ndef test_add_one():\n    assert add_one(1) == 2\n",
}
# The only discovered command needs a tool that does not exist.
ENVIRONMENT_REPO = {**NO_COMMAND_REPO, "Makefile": "test:\n\tnonexistent_tool_xyz_123 --run tests\n"}

# A test command that outlives any sane timeout (meshery's `go test ./...` in miniature).
SLOW_REPO = {**BUGGY_REPO,   # the interpreter path is quoted: a temp dir may contain spaces
             "Makefile": f'test:\n\t"{_sys.executable}" -c \'import time; time.sleep(30)\'\n'}
SLOW_CMD = "make test"

LEGACY_TEST = (
    "import unittest\n\n\nclass LegacyTest(unittest.TestCase):\n"
    "    def test_legacy_format(self):\n        self.assertEqual('a-b', 'a_b')   # fails before and after\n"
)

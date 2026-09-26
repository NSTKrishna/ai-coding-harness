"""The same harness scenario with ScriptedModel and with OpenAICompatibleClient.

The orchestrator is used unchanged: only the ``ModelClient`` differs. The adapter
runs over a mocked transport (text-JSON and native tool-call forms) and, for the
CLI path, over a local HTTP endpoint through the real factory and configuration.
No live provider, endpoint or model id is assumed.
"""

import ast
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

from harness.config import Limits
from harness.model import ScriptedModel, text_response
from harness.model.adapters.openai_compatible import HttpResponse, OpenAICompatibleClient
from harness.orchestrator import Orchestrator, Phase

from tests.orchestration_helpers import FIX_PATCH, TASK, buggy_repo, plan_dict
from tests.test_openai_compatible import KEY, MODEL, FakeTransport, LocalServer, chat

SRC = Path(__file__).resolve().parent.parent / "src" / "harness"
LIMITS = replace(Limits(), targeted_tests=False)
ENV_PROBE = [sys.executable, "-c", "import os; print('KEY_IN_CHILD=' + str('AI_API_KEY' in os.environ))"]

# a neutral script: ("plan", dict) | ("tools", [(name, args), ...]) | ("complete", text)
SCENARIO = [
    ("plan", plan_dict()),
    ("tools", [("read_file", {"path": "src/math_utils.py"})]),
    ("tools", [("run_command", {"command": ENV_PROBE}), ("apply_patch", {"patch": FIX_PATCH})]),
    ("complete", "fixed add_one"),
]
USAGE = {"prompt_tokens": 50, "completion_tokens": 10}


def scripted(scenario):
    out = []
    for kind, value in scenario:
        if kind == "plan":
            out.append(text_response(json.dumps(value)))
        elif kind == "tools":
            out.extend(text_response(json.dumps({"action": "tool", "tool": n, "arguments": a})) for n, a in value)
        else:
            out.append(text_response(json.dumps({"action": "complete", "summary": value})))
    return out


def chat_bodies(scenario, native):
    """The same scenario as OpenAI-compatible chat completions (native: one response carries all calls)."""
    out = []
    for i, (kind, value) in enumerate(scenario):
        if kind == "plan":
            out.append(chat(json.dumps(value), usage=USAGE))
        elif kind == "tools" and native:
            out.append(chat(None, finish="tool_calls", usage=USAGE, tool_calls=[
                {"id": f"call_{i}_{j}", "type": "function", "function": {"name": n, "arguments": json.dumps(a)}}
                for j, (n, a) in enumerate(value)]))
        elif kind == "tools":
            out.extend(chat(f"```json\n{json.dumps({'action': 'tool', 'tool': n, 'arguments': a})}\n```", usage=USAGE)
                       for n, a in value)
        else:
            out.append(chat(json.dumps({"action": "complete", "summary": value}), usage=USAGE))
    return out


@unittest.skipUnless(shutil.which("git"), "git not installed")
class SameScenarioTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name).resolve()
        patcher = mock.patch.dict(os.environ, {"AI_API_KEY": KEY})   # present in the parent process
        patcher.start()
        self.addCleanup(patcher.stop)
        self.runs = 0

    def run_with(self, model):
        self.runs += 1
        root = buggy_repo(self.tmp / f"r{self.runs}")
        return root, Orchestrator(model, limits=LIMITS).run(root, TASK)

    @staticmethod
    def outcome(root, state):
        return {
            "phase": state.phase,
            "changed": sorted(state.modified_files),
            "tools": [(a.kind, a.tool) for a in state.action_history],
            "fixed": "return x + 1" in (root / "src" / "math_utils.py").read_text(),
        }

    def assert_key_absent(self, state):
        self.assertNotIn(KEY, repr(state.summary()))
        self.assertNotIn(KEY, repr(state.observations))
        self.assertNotIn(KEY, repr(state.action_history))
        probe = [o for o in state.observations if o.tool == "run_command"]
        self.assertTrue(probe and "KEY_IN_CHILD=False" in repr(probe[0]), repr(probe))

    def test_scripted_and_adapter_produce_the_same_run(self):
        root, scripted_state = self.run_with(ScriptedModel(scripted(SCENARIO)))
        expected = self.outcome(root, scripted_state)
        self.assertEqual(expected["phase"], Phase.VERIFIED)
        self.assertTrue(expected["fixed"])
        self.assert_key_absent(scripted_state)

        for native in (False, True):
            with self.subTest(native=native):
                transport = FakeTransport(*[HttpResponse(200, {}, json.dumps(b).encode())
                                            for b in chat_bodies(SCENARIO, native)])
                adapter = OpenAICompatibleClient(api_key=KEY, model=MODEL, base_url="http://mock.invalid/v1",
                                                 transport=transport)
                root, state = self.run_with(adapter)
                self.assertEqual(self.outcome(root, state), expected)
                self.assertEqual(transport.items, [])            # every scripted response was consumed
                m = state.metrics
                self.assertEqual((m.provider_attempts, m.input_tokens), (m.model_calls, 50 * m.model_calls))
                self.assert_key_absent(state)
                sent = [r["body"] for r in transport.requests]
                self.assertTrue(all(b["model"] == MODEL for b in sent))
                self.assertTrue(any(b.get("tools") for b in sent))   # the executor offers native tools
                sources = {a.source for a in state.action_history if a.kind == "tool"}
                self.assertEqual(sources, {"native" if native else "text"})


@unittest.skipUnless(shutil.which("git"), "git not installed")
class CliOverHttpTest(unittest.TestCase):
    """`harness run`: env config -> factory -> adapter -> local HTTP endpoint -> artifacts."""

    def test_cli_run_over_local_endpoint(self):
        from harness.cli import main

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        base = Path(tmp.name).resolve()
        repo = buggy_repo(base)
        runs = base / "runs"
        out, err = io.StringIO(), io.StringIO()
        with LocalServer(chat_bodies(SCENARIO, native=True)) as server, \
                mock.patch.dict(os.environ, {"AI_API_KEY": KEY}):
            code = main(["run", "--repo", str(repo), "--task", TASK],
                        environ={"AI_API_KEY": KEY, "AI_MODEL_ADAPTER": "openai_compatible",
                                 "AI_MODEL_PROVIDER": "family-label", "AI_MODEL": MODEL,
                                 "AI_BASE_URL": server.base_url, "HARNESS_RUNS_DIR": str(runs)},
                        dotenv_path=Path("/nonexistent/.env"), stdin=io.StringIO(""), stdout=out, stderr=err)
        text = out.getvalue() + err.getvalue()
        self.assertEqual(code, 0, text)
        self.assertIn("TASK RESULT: VERIFIED", text)
        self.assertNotIn(KEY, text)
        self.assertTrue(server.seen)
        self.assertTrue(all(s["path"] == "/custom/v1/chat/completions" for s in server.seen))
        self.assertTrue(all(s["headers"]["Authorization"] == f"Bearer {KEY}" for s in server.seen))
        self.assertTrue(all(s["body"]["model"] == MODEL for s in server.seen))
        (run_dir,) = list(runs.iterdir())
        self.assertEqual({p.name for p in run_dir.iterdir()},
                         {"events.jsonl", "summary.json", "final_report.md", "final.diff"})
        for artifact in run_dir.iterdir():
            self.assertNotIn(KEY, artifact.read_text(), artifact.name)
        self.assertEqual(json.loads((run_dir / "summary.json").read_text())["final_status"], "VERIFIED")


class ArchitectureTest(unittest.TestCase):
    """Core modules depend on ModelClient and the neutral types only, never on adapters."""

    CORE_PACKAGES = ("orchestrator", "verify", "context", "tools", "discovery", "telemetry")
    CORE_FILES = ("metrics.py", "config.py", "telemetry.py", "model/client.py", "model/types.py", "model/fake.py",
                  "model/__init__.py")

    def test_core_does_not_import_adapters(self):
        files = [p for pkg in self.CORE_PACKAGES if (SRC / pkg).is_dir() for p in (SRC / pkg).rglob("*.py")]
        files += [SRC / f for f in self.CORE_FILES if (SRC / f).exists()]
        self.assertGreater(len(files), 10)
        offenders = []
        for path in files:
            for node in ast.walk(ast.parse(path.read_text())):
                if isinstance(node, ast.Import):
                    names = [a.name for a in node.names]
                elif isinstance(node, ast.ImportFrom):
                    names = [node.module or ""]
                else:
                    continue
                for name in names:
                    if "adapters" in name or name == "harness.model.factory" or name.split(".")[0] in ("openai", "anthropic"):
                        offenders.append(f"{path.relative_to(SRC)} imports {name}")
        self.assertEqual(offenders, [])

    def test_no_model_family_dispatch(self):
        """No code branches on a model family: family names may appear in comments/docstrings only."""
        offenders = []
        for path in SRC.rglob("*.py"):
            tree = ast.parse(path.read_text())
            docstrings = {id(n.body[0].value) for n in ast.walk(tree)
                          if isinstance(n, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
                          and n.body and isinstance(n.body[0], ast.Expr) and isinstance(n.body[0].value, ast.Constant)}
            for node in ast.walk(tree):
                if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in docstrings:
                    if any(f in node.value.lower() for f in ("deepseek", "qwen")):
                        offenders.append(f"{path.relative_to(SRC)}:{node.lineno}")
                if isinstance(node, ast.Name) and any(f in node.id.lower() for f in ("deepseek", "qwen")):
                    offenders.append(f"{path.relative_to(SRC)}:{node.lineno}")
        self.assertEqual(offenders, [])
        self.assertEqual(sorted(p.name for p in (SRC / "model" / "adapters").glob("*.py")),
                         ["__init__.py", "openai_compatible.py"])


if __name__ == "__main__":
    unittest.main()

"""The API key must not reach tool output, child processes, or model structures."""

import dataclasses
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

from harness.config import load_config
from harness.metrics import ExecutionMetrics
from harness.model import Message, MeteredModelClient, ModelRequest, ScriptedModel, text_response
from harness.tools import ToolContext, build_registry

from tests.helpers import FAKE_KEY, RepoTestCase

NO_DOTENV = Path("/nonexistent/harness/.env")


class SecretHandlingTest(RepoTestCase):
    def setUp(self):
        super().setUp()
        patcher = mock.patch.dict(os.environ, {"AI_API_KEY": FAKE_KEY})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.config = load_config(dotenv_path=NO_DOTENV)
        self.ctx = ToolContext.create(self.repo)
        self.registry = build_registry(self.ctx, redactor=self.config.redact)

    def test_child_processes_do_not_inherit_the_key(self):
        script = "import os; print(os.environ.get('AI_API_KEY')); print(sorted(os.environ))"
        data = self.registry.dispatch("run_command", {"command": [sys.executable, "-c", script]}).data
        self.assertTrue(data.stdout.startswith("None\n"))
        self.assertNotIn("AI_API_KEY", data.stdout)
        self.assertNotIn(FAKE_KEY, data.stdout + data.stderr)

    def test_key_in_repository_content_is_redacted_from_results(self):
        self.write("leaky.env", f"TOKEN={FAKE_KEY}\n")
        read = self.registry.dispatch("read_file", {"path": "leaky.env"})
        self.assertEqual(read.data.content, "TOKEN=***\n")
        found = self.registry.dispatch("search_text", {"query": "TOKEN"})
        self.assertEqual(found.data.matches[0].text, "TOKEN=***")
        shown = self.registry.dispatch("run_command", {"command": [sys.executable, "-c",
                                                                   "print(open('leaky.env').read())"]})
        self.assertNotIn(FAKE_KEY, shown.data.stdout)

    def test_key_is_redacted_from_error_messages(self):
        result = self.registry.dispatch("read_file", {"path": f"missing-{FAKE_KEY}.txt"})
        self.assertFalse(result.success)
        self.assertNotIn(FAKE_KEY, result.error.message)
        self.assertIn("***", result.error.message)

    def test_model_layer_never_holds_the_key(self):
        metrics = ExecutionMetrics()
        model = MeteredModelClient(ScriptedModel([text_response("done")]), metrics)
        response = model.generate(ModelRequest(messages=(Message("user", "hi"),)))
        for obj in (response, model.inner.requests[0], metrics):
            text = repr(obj) + repr(dataclasses.asdict(obj))
            self.assertNotIn(FAKE_KEY, text)


if __name__ == "__main__":
    unittest.main()

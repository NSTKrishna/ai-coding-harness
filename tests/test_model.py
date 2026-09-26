import dataclasses
import json
import os
import unittest
from unittest import mock

from harness.metrics import ExecutionMetrics
from harness.model import (
    FinishReason,
    Message,
    MeteredModelClient,
    ModelError,
    ModelRequest,
    ModelResponse,
    ScriptedModel,
    ScriptExhausted,
    ToolDefinition,
    Usage,
    malformed_tool_call_response,
    text_response,
    tool_call_response,
)


def request(text="hello", **kwargs):
    return ModelRequest(messages=(Message("user", text),), **kwargs)


class ScriptedModelTest(unittest.TestCase):
    def test_returns_scripted_responses_in_order(self):
        model = ScriptedModel([text_response("one"), text_response("two"), text_response("three")])
        self.assertEqual([model.generate(request()).text for _ in range(3)], ["one", "two", "three"])

    def test_records_requests_and_call_count(self):
        model = ScriptedModel([text_response("a"), text_response("b")])
        model.generate(request("first"))
        model.generate(request("second"))
        self.assertEqual(model.call_count, 2)
        self.assertEqual([r.messages[0].content for r in model.requests], ["first", "second"])
        self.assertEqual(model.remaining, 0)

    def test_exhausted_script_raises_clear_error(self):
        model = ScriptedModel([text_response("only")])
        model.generate(request())
        with self.assertRaises(ScriptExhausted):
            model.generate(request())
        self.assertEqual(model.call_count, 2)

    def test_scripted_exception_is_raised(self):
        model = ScriptedModel([ModelError("rate limited", retryable=True), text_response("after")])
        with self.assertRaises(ModelError) as ctx:
            model.generate(request())
        self.assertTrue(ctx.exception.retryable)
        self.assertEqual(model.generate(request()).text, "after")

    def test_callable_item_builds_response_from_request(self):
        model = ScriptedModel([lambda req: text_response(req.messages[-1].content.upper())])
        self.assertEqual(model.generate(request("echo")).text, "ECHO")

    def test_queue_appends(self):
        model = ScriptedModel().queue(text_response("x")).queue(text_response("y"))
        self.assertEqual(model.remaining, 2)

    def test_tool_call_response(self):
        model = ScriptedModel([tool_call_response("read_file", {"path": "a.py"}, call_id="c7")])
        response = model.generate(request())
        self.assertEqual(response.finish_reason, FinishReason.TOOL_CALLS)
        self.assertEqual(len(response.tool_calls), 1)
        call = response.tool_calls[0]
        self.assertEqual((call.id, call.name, dict(call.arguments)), ("c7", "read_file", {"path": "a.py"}))
        self.assertIsNone(call.parse_error)

    def test_malformed_tool_call_is_represented_not_raised(self):
        model = ScriptedModel([malformed_tool_call_response("read_file", '{"path": ')])
        call = model.generate(request()).tool_calls[0]
        self.assertEqual(dict(call.arguments), {})
        self.assertEqual(call.raw_arguments, '{"path": ')
        self.assertIn("not valid JSON", call.parse_error)

    def test_response_is_normalized_with_provider_and_model(self):
        response = ScriptedModel([text_response("hi")], model="m1").generate(request())
        self.assertIsInstance(response, ModelResponse)
        self.assertEqual((response.provider, response.model), ("fake", "m1"))
        self.assertEqual(response.finish_reason, FinishReason.STOP)

    def test_response_contains_only_neutral_serializable_data(self):
        response = ScriptedModel([tool_call_response("t", {"x": 1})]).generate(request())
        encoded = json.dumps(dataclasses.asdict(response), default=str)
        self.assertIn('"name": "t"', encoded)
        self.assertEqual(
            {f.name for f in dataclasses.fields(ModelResponse)},
            {"text", "tool_calls", "finish_reason", "usage", "provider", "model", "raw_finish_reason", "metadata"},
        )

    def test_fake_usage_is_deterministic(self):
        req = request("x" * 40)
        first = ScriptedModel([text_response("y" * 8)]).generate(req)
        second = ScriptedModel([text_response("y" * 8)]).generate(req)
        self.assertEqual(first.usage, Usage(input_tokens=10, output_tokens=2))
        self.assertEqual(first.usage, second.usage)

    def test_explicit_usage_is_kept_and_fake_usage_can_be_disabled(self):
        explicit = ScriptedModel([text_response("y", usage=Usage(7, 3))]).generate(request())
        self.assertEqual(explicit.usage, Usage(7, 3))
        none = ScriptedModel([text_response("y")], fake_usage=False).generate(request())
        self.assertEqual(none.usage, Usage())

    def test_works_without_api_key(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("AI_API_KEY", None)
            self.assertEqual(ScriptedModel([text_response("ok")]).generate(request()).text, "ok")


class ModelRequestTest(unittest.TestCase):
    def test_carries_tools_and_response_schema(self):
        tool = ToolDefinition("read_file", "Read a file", {"type": "object", "properties": {}})
        req = request(tools=[tool], response_schema={"type": "object"}, purpose="plan")
        self.assertEqual(req.tools, (tool,))
        self.assertEqual(req.response_schema, {"type": "object"})

    def test_rejects_empty_messages(self):
        with self.assertRaises(ValueError):
            ModelRequest(messages=())

    def test_message_validation(self):
        with self.assertRaises(ValueError):
            Message("robot", "x")
        with self.assertRaises(ValueError):
            Message("tool", "result")  # no tool_call_id
        Message("tool", "result", tool_call_id="c1")


class MeteredModelClientTest(unittest.TestCase):
    def test_counts_calls_and_usage(self):
        metrics = ExecutionMetrics()
        client = MeteredModelClient(ScriptedModel([text_response("a", usage=Usage(10, 2)),
                                                   text_response("b", usage=Usage(5, 1))]), metrics)
        client.generate(request())
        client.generate(request())
        self.assertEqual((metrics.model_calls, metrics.input_tokens, metrics.output_tokens), (2, 15, 3))
        self.assertEqual(metrics.model_failures, 0)

    def test_failed_call_counts_once(self):
        metrics = ExecutionMetrics()
        client = MeteredModelClient(ScriptedModel([ModelError("boom")]), metrics)
        with self.assertRaises(ModelError):
            client.generate(request())
        self.assertEqual((metrics.model_calls, metrics.model_failures), (1, 1))

    def test_missing_usage_is_tracked_separately(self):
        metrics = ExecutionMetrics()
        client = MeteredModelClient(ScriptedModel([text_response("a")], fake_usage=False), metrics)
        client.generate(request())
        self.assertEqual((metrics.model_calls, metrics.model_calls_without_usage, metrics.input_tokens), (1, 1, 0))


if __name__ == "__main__":
    unittest.main()

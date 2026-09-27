import json
import unittest

from harness.model import ModelRequest, Message, ScriptedModel, tool_call_response, malformed_tool_call_response
from harness.tools import ALL_TOOLS, Tool, ToolContext, ToolFailure, ToolRegistry, build_registry

from tests.helpers import RepoTestCase


def echo(ctx, text, times=1):
    return text * times


def fail(ctx):
    raise ToolFailure("custom_code", "it failed on purpose", {"hint": "details kept"})


def crash(ctx):
    raise RuntimeError("unexpected bug")


ECHO = Tool(
    name="echo",
    description="Echo text.",
    parameters={
        "type": "object",
        "properties": {"text": {"type": "string"}, "times": {"type": "integer", "minimum": 1}},
        "required": ["text"],
        "additionalProperties": False,
    },
    handler=echo,
    category="read",
)
NO_ARGS = {"type": "object", "properties": {}, "additionalProperties": False}
FAIL = Tool("fail", "Always fails.", NO_ARGS, fail, "exec")
CRASH = Tool("crash", "Raises.", NO_ARGS, crash, "write")


class RegistryTest(RepoTestCase):
    def setUp(self):
        super().setUp()
        self.ctx = ToolContext.create(self.repo)
        self.registry = ToolRegistry(self.ctx)
        self.registry.register_all([ECHO, FAIL, CRASH])

    def test_register_and_list(self):
        self.assertEqual(self.registry.names, ("crash", "echo", "fail"))

    def test_duplicate_name_rejected(self):
        with self.assertRaises(ValueError):
            self.registry.register(ECHO)

    def test_invalid_category_rejected(self):
        with self.assertRaises(ValueError):
            Tool("x", "x", NO_ARGS, echo, "dangerous")

    def test_dispatch_success(self):
        result = self.registry.dispatch("echo", {"text": "ab", "times": 2})
        self.assertTrue(result.success)
        self.assertEqual(result.data, "abab")
        self.assertIsNone(result.error)
        self.assertGreaterEqual(result.duration_ms, 0)

    def test_tool_failure_is_structured(self):
        result = self.registry.dispatch("fail")
        self.assertFalse(result.success)
        self.assertEqual(result.error.code, "custom_code")
        self.assertEqual(result.error.message, "it failed on purpose")
        self.assertEqual(result.error.details, {"hint": "details kept"})

    def test_unexpected_exception_is_structured(self):
        result = self.registry.dispatch("crash")
        self.assertFalse(result.success)
        self.assertEqual(result.error.code, "internal_error")
        self.assertIn("RuntimeError: unexpected bug", result.error.message)

    def test_unknown_tool(self):
        result = self.registry.dispatch("nope")
        self.assertEqual(result.error.code, "unknown_tool")
        self.assertIn("echo", result.error.message)

    def test_argument_validation(self):
        cases = [
            ({}, "missing required argument(s): text"),
            ({"text": "a", "extra": 1}, "unknown argument(s): extra"),
            ({"text": 5}, "must be string"),
            ({"text": "a", "times": True}, "must be integer"),
            ({"text": "a", "times": 0}, "must be >= 1"),
        ]
        for args, message in cases:
            with self.subTest(args=args):
                result = self.registry.dispatch("echo", args)
                self.assertEqual(result.error.code, "invalid_arguments")
                self.assertIn(message, result.error.message)

    def test_every_dispatch_counts_once(self):
        self.registry.dispatch("echo", {"text": "a"})
        self.registry.dispatch("echo", {})          # invalid arguments
        self.registry.dispatch("fail")
        self.registry.dispatch("crash")
        self.registry.dispatch("nope")
        m = self.ctx.metrics
        self.assertEqual(m.tool_calls, 5)
        self.assertEqual(m.tool_failures, 4)
        self.assertEqual(m.tool_calls_by_name, {"echo": 2, "fail": 1, "crash": 1, "nope": 1})

    def test_category_restriction(self):
        result = self.registry.dispatch("fail", categories=["read"])
        self.assertEqual(result.error.code, "tool_not_allowed")
        self.assertEqual([d.name for d in self.registry.definitions(["read"])], ["echo"])

    def test_definitions_are_json_schema_ready(self):
        definitions = self.registry.definitions()
        self.assertEqual([d.name for d in definitions], ["crash", "echo", "fail"])
        json.dumps([{"name": d.name, "description": d.description, "parameters": d.parameters}
                    for d in definitions])


class BuiltinRegistryTest(RepoTestCase):
    def test_all_builtin_tools_registered_with_valid_schemas(self):
        registry = build_registry(ToolContext.create(self.repo))
        self.assertEqual(set(registry.names), {
            "list_files", "find_files", "read_file", "read_range", "search_text", "apply_patch", "edit_file", "write_file",
            "delete_file",
            "run_command", "run_tests", "git_status", "git_diff", "git_diff_stat",
        })
        for tool in ALL_TOOLS:
            with self.subTest(tool=tool.name):
                self.assertEqual(tool.parameters["type"], "object")
                self.assertFalse(tool.parameters.get("additionalProperties", True))
                self.assertTrue(tool.description)
                json.dumps(tool.parameters)

    def test_model_tool_call_round_trip(self):
        """A scripted model's tool call is dispatched through the registry."""
        self.write("notes.txt", "hello\n")
        registry = build_registry(ToolContext.create(self.repo))
        model = ScriptedModel([tool_call_response("read_file", {"path": "notes.txt"})])
        response = model.generate(ModelRequest(messages=(Message("user", "read it"),),
                                               tools=registry.definitions()))
        result = registry.dispatch_call(response.tool_calls[0])
        self.assertTrue(result.success)
        self.assertEqual(result.data.content, "hello\n")
        self.assertEqual(len(model.requests[0].tools), len(ALL_TOOLS))

    def test_malformed_model_tool_call_becomes_structured_error(self):
        ctx = ToolContext.create(self.repo)
        registry = build_registry(ctx)
        call = malformed_tool_call_response("read_file", "{not json").tool_calls[0]
        result = registry.dispatch_call(call)
        self.assertFalse(result.success)
        self.assertEqual(result.error.code, "invalid_arguments")
        self.assertEqual(ctx.metrics.tool_calls, 1)


if __name__ == "__main__":
    unittest.main()

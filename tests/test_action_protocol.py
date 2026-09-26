import json
import unittest

from harness.model import ModelResponse, ToolCall, malformed_tool_call_response, text_response, tool_call_response
from harness.orchestrator.protocol import ProtocolError, extract_json_object, parse_action


class ActionProtocolTest(unittest.TestCase):
    def test_text_tool_action_becomes_tool_call(self):
        response = text_response(json.dumps({"action": "tool", "tool": "read_file", "arguments": {"path": "a.py"}}))
        action = parse_action(response, step=4)
        self.assertEqual(action.kind, "tool")
        self.assertEqual(action.source, "text")
        self.assertIsInstance(action.call, ToolCall)
        self.assertEqual((action.call.id, action.call.name, dict(action.call.arguments)), ("step-4", "read_file", {"path": "a.py"}))

    def test_native_tool_call_takes_the_same_path(self):
        native = parse_action(tool_call_response("read_file", {"path": "a.py"}), step=1)
        text = parse_action(text_response(json.dumps({"action": "tool", "tool": "read_file", "arguments": {"path": "a.py"}})), step=1)
        self.assertEqual(native.source, "native")
        self.assertEqual((native.call.name, dict(native.call.arguments)), (text.call.name, dict(text.call.arguments)))
        self.assertIsInstance(native.call, ToolCall)

    def test_native_call_with_malformed_arguments_is_passed_on_for_a_structured_tool_error(self):
        action = parse_action(malformed_tool_call_response("read_file", '{"path": '), step=1)
        self.assertEqual(action.kind, "tool")
        self.assertIsNotNone(action.call.parse_error)

    def test_complete_and_blocked(self):
        done = parse_action(text_response('{"action": "complete", "summary": "changed x"}'), 1)
        self.assertEqual((done.kind, done.text), ("complete", "changed x"))
        stop = parse_action(text_response('```json\n{"action": "blocked", "reason": "no such file"}\n```'), 1)
        self.assertEqual((stop.kind, stop.text), ("blocked", "no such file"))

    def test_rejections(self):
        cases = {
            "prose": ("I think maybe run pytest now", "not valid JSON"),
            "empty": ("", "empty"),
            "array": ("[]", "expected a JSON object"),
            "no action": ('{"tool": "read_file"}', "missing 'action'"),
            "unknown action": ('{"action": "shell", "command": "ls"}', "unknown action 'shell'"),
            "tool without arguments": ('{"action": "tool", "tool": "read_file"}', "missing field(s) arguments"),
            "arguments not object": ('{"action": "tool", "tool": "x", "arguments": [1]}', "must be a JSON object"),
            "extra field": ('{"action": "complete", "summary": "x", "thoughts": "y"}', "unexpected field(s) thoughts"),
            "empty summary": ('{"action": "complete", "summary": "  "}', "non-empty string"),
            "long reason": (json.dumps({"action": "blocked", "reason": "x" * 600}), "longer than"),
            "two json objects": ('{"action": "complete", "summary": "a"}\n{"action": "complete", "summary": "b"}', "not valid JSON"),
        }
        for name, (text, message) in cases.items():
            with self.subTest(case=name):
                with self.assertRaises(ProtocolError) as ctx:
                    parse_action(text_response(text), 1)
                self.assertIn(message, str(ctx.exception))

    def test_multiple_native_tool_calls_rejected(self):
        calls = (ToolCall("1", "read_file", {"path": "a"}), ToolCall("2", "read_file", {"path": "b"}))
        with self.assertRaises(ProtocolError) as ctx:
            parse_action(ModelResponse(text="", tool_calls=calls), 1)
        self.assertIn("exactly one tool call", str(ctx.exception))

    def test_extract_json_object_only_accepts_whole_body_fences(self):
        self.assertEqual(extract_json_object('```\n{"a": 1}\n```'), {"a": 1})
        with self.assertRaises(ProtocolError):
            extract_json_object('Sure!\n```json\n{"a": 1}\n```')


if __name__ == "__main__":
    unittest.main()

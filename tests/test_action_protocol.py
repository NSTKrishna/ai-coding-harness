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
            "two json objects": ('{"action": "complete", "summary": "a"}\n{"action": "complete", "summary": "b"}',
                                 "2 JSON objects; expected exactly one"),
            "two fenced objects": ('A:\n```json\n{"action": "complete", "summary": "a"}\n```\nB:\n```json\n'
                                   '{"action": "blocked", "reason": "b"}\n```', "2 JSON objects"),
            "tool name as action without tool list": ('{"action": "read_file", "path": "a"}', "unknown action 'read_file'"),
        }
        for name, (text, message) in cases.items():
            with self.subTest(case=name):
                with self.assertRaises(ProtocolError) as ctx:
                    parse_action(text_response(text), 1)
                self.assertIn(message, str(ctx.exception))

    def test_multiple_native_tool_calls_are_kept_in_order(self):
        calls = (ToolCall("1", "read_file", {"path": "a"}), ToolCall("2", "read_file", {"path": "b"}))
        action = parse_action(ModelResponse(text="", tool_calls=calls), 1)
        self.assertEqual((action.kind, action.source, action.calls, action.call), ("tool", "native", calls, calls[0]))

    def test_too_many_native_tool_calls_rejected(self):
        from harness.orchestrator.protocol import MAX_TOOL_CALLS_PER_STEP
        calls = tuple(ToolCall(str(i), "read_file", {"path": "a"}) for i in range(MAX_TOOL_CALLS_PER_STEP + 1))
        with self.assertRaises(ProtocolError) as ctx:
            parse_action(ModelResponse(text="", tool_calls=calls), 1)
        self.assertIn(f"at most {MAX_TOOL_CALLS_PER_STEP} tool calls", str(ctx.exception))

    def test_extract_json_object_finds_the_single_object_in_prose(self):
        # live Qwen/DeepSeek output often wraps the one JSON object in a sentence or a fence
        self.assertEqual(extract_json_object('```\n{"a": 1}\n```'), {"a": 1})
        self.assertEqual(extract_json_object('Sure!\n```json\n{"a": 1}\n```'), {"a": 1})
        self.assertEqual(extract_json_object('I will read it now. {"a": {"b": [1, "}"]}} Done.'), {"a": {"b": [1, "}"]}})
        self.assertEqual(extract_json_object('Same twice {"a": 1} and {"a": 1}'), {"a": 1})
        for text in ('no json here', '{"a": 1} {"b": 2}', 'x {"a": ', '```json\n{"a": 1}\n```\n```json\n{"b": 1}\n```'):
            with self.subTest(text=text), self.assertRaises(ProtocolError):
                extract_json_object(text)

    def test_raw_newline_inside_a_string_is_accepted(self):
        text = '{"action": "tool", "tool": "write_file", "arguments": {"path": "a.py", "content": "x = 1\ny = 2\n"}}'
        self.assertIn("\n", text)                                   # a literal newline, not an escape
        action = parse_action(text_response(text), 1)
        self.assertEqual(action.call.arguments["content"], "x = 1\ny = 2\n")
        wrapped = parse_action(text_response("Writing it:\n" + text), 1)
        self.assertEqual(wrapped.call.arguments["content"], "x = 1\ny = 2\n")

    def test_tolerated_action_shapes(self):
        names = frozenset({"read_file", "run_tests"})
        a = parse_action(text_response('{"action": "read_file", "arguments": {"path": "a.py"}}'), 2, names)
        b = parse_action(text_response('{"action": "read_file", "path": "a.py"}'), 2, names)
        c = parse_action(text_response('{"tool": "read_file", "arguments": {"path": "a.py"}}'), 2, names)
        for action in (a, b, c):
            self.assertEqual((action.kind, action.call.name, dict(action.call.arguments), action.source),
                             ("tool", "read_file", {"path": "a.py"}, "text"))

    def test_native_control_tools(self):
        from harness.orchestrator.protocol import CONTROL_TOOLS
        self.assertEqual([t.name for t in CONTROL_TOOLS], ["complete", "blocked"])
        done = parse_action(tool_call_response("complete", {"summary": "fixed it"}), 1)
        self.assertEqual((done.kind, done.text, done.source), ("complete", "fixed it", "native"))
        stop = parse_action(tool_call_response("blocked", {}), 1)
        self.assertEqual(stop.kind, "blocked")
        self.assertIn("without a reason", stop.text)
        mixed = parse_action(ModelResponse(text="", tool_calls=(ToolCall("1", "read_file", {"path": "a"}),
                                                                 ToolCall("2", "complete", {"summary": "x"}))), 1)
        self.assertEqual((mixed.kind, [c.name for c in mixed.calls]), ("tool", ["read_file"]))


if __name__ == "__main__":
    unittest.main()

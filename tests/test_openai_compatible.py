"""Contract tests for the OpenAI-compatible adapter.

No live provider is used. The transport is either a recording fake (translation,
retries, error normalization) or a local ``http.server`` (real urllib path:
custom base URL, Authorization header, timeout). Model ids and URLs here are
placeholders; nothing assumes a particular provider's endpoint or model name.
"""

import http.server
import json
import threading
import time
import unittest

from harness.model.adapters.openai_compatible import (
    MAX_RETRY_AFTER_SECONDS,
    HttpResponse,
    OpenAICompatibleClient,
    TransportError,
)
from harness.model.types import (
    FinishReason,
    Message,
    ModelError,
    ModelRequest,
    ModelResponse,
    ToolCall,
    ToolDefinition,
    Usage,
)

KEY = "sk-contract-test-9d8c7b6a"
MODEL = "configured-model-id"
BASE = "http://gateway.invalid/v1"
READ_FILE = ToolDefinition("read_file", "Read a file", {"type": "object", "properties": {"path": {"type": "string"}},
                                                         "required": ["path"]})


def completion(message=None, finish="stop", usage=None, **extra):
    body = {"id": "resp-1", "model": MODEL,
            "choices": [{"index": 0, "message": {"role": "assistant", "content": "", **(message or {})},
                         "finish_reason": finish}], **extra}
    if usage is not None:
        body["usage"] = usage
    return HttpResponse(200, {"Content-Type": "application/json"}, json.dumps(body).encode())


def status(code, body=b"", headers=None):
    return HttpResponse(code, headers or {}, body if isinstance(body, bytes) else json.dumps(body).encode())


class FakeTransport:
    """Returns (or raises) queued items and records every request."""

    def __init__(self, *items):
        self.items = list(items)
        self.requests = []

    def __call__(self, url, headers, body, timeout):
        self.requests.append({"url": url, "headers": dict(headers), "body": json.loads(body), "timeout": timeout})
        item = self.items.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def client(transport, **kw):
    sleeps = []
    kw.setdefault("max_retries", 2)
    c = OpenAICompatibleClient(api_key=KEY, model=MODEL, base_url=BASE, transport=transport,
                               sleep=sleeps.append, **kw)
    return c, sleeps


def request(**kw):
    return ModelRequest(messages=(Message("system", "sys"), Message("user", "hi")), **kw)


class RequestTranslationTest(unittest.TestCase):
    def test_request_translation(self):
        t = FakeTransport(completion({"content": "ok"}))
        c, _ = client(t, timeout_seconds=17)
        history = (
            Message("system", "sys"),
            Message("user", "hi"),
            Message("assistant", "", tool_calls=(ToolCall("c1", "read_file", {"path": "a.py"}),)),
            Message("tool", "file text", tool_call_id="c1"),
        )
        c.generate(ModelRequest(messages=history, tools=(READ_FILE,), max_output_tokens=321, temperature=0.0))
        (sent,) = t.requests
        self.assertEqual(sent["url"], BASE + "/chat/completions")
        self.assertEqual(sent["timeout"], 17)
        body = sent["body"]
        self.assertEqual(body["model"], MODEL)
        self.assertEqual(body["max_tokens"], 321)
        self.assertEqual(body["temperature"], 0.0)
        self.assertEqual(body["messages"][:2], [{"role": "system", "content": "sys"}, {"role": "user", "content": "hi"}])
        self.assertEqual(body["messages"][2], {"role": "assistant", "content": None, "tool_calls": [
            {"id": "c1", "type": "function", "function": {"name": "read_file", "arguments": '{"path": "a.py"}'}}]})
        self.assertEqual(body["messages"][3], {"role": "tool", "tool_call_id": "c1", "content": "file text"})
        self.assertEqual(body["tools"], [{"type": "function", "function": {
            "name": "read_file", "description": "Read a file", "parameters": dict(READ_FILE.parameters)}}])
        self.assertNotIn("response_format", body)
        self.assertEqual(sent["headers"]["Authorization"], f"Bearer {KEY}")
        self.assertNotIn(KEY, json.dumps(body))           # the key is a header, never payload

    def test_optional_fields_are_omitted(self):
        t = FakeTransport(completion({"content": "ok"}))
        client(t)[0].generate(request())
        self.assertEqual(set(t.requests[0]["body"]), {"model", "messages"})

    def test_structured_output_is_opt_in(self):
        schema = {"type": "object"}
        for mode, expected in (("none", None), ("json_object", {"type": "json_object"})):
            t = FakeTransport(completion({"content": "{}"}))
            client(t, structured_output=mode)[0].generate(request(response_schema=schema))
            self.assertEqual(t.requests[0]["body"].get("response_format"), expected)

    def test_custom_base_url_and_extra_headers(self):
        for base, url in (("http://localhost:9/api/", "http://localhost:9/api/chat/completions"),
                          ("https://gw.invalid/x/chat/completions", "https://gw.invalid/x/chat/completions")):
            t = FakeTransport(completion({"content": "ok"}))
            OpenAICompatibleClient(api_key=KEY, model=MODEL, base_url=base, transport=t,
                                   headers=(("X-Team", "t1"),)).generate(request())
            self.assertEqual(t.requests[0]["url"], url)
            self.assertEqual(t.requests[0]["headers"]["X-Team"], "t1")

    def test_model_name_comes_from_config(self):
        for model in ("family-a/model-1", "model-two"):
            t = FakeTransport(completion({"content": "ok"}))
            OpenAICompatibleClient(api_key=KEY, model=model, base_url=BASE, transport=t).generate(request())
            self.assertEqual(t.requests[0]["body"]["model"], model)

    def test_extra_header_cannot_replace_authorization(self):
        t = FakeTransport(completion({"content": "ok"}))
        client(t, headers=(("Authorization", "Bearer other"),))[0].generate(request())
        self.assertEqual(t.requests[0]["headers"]["Authorization"], f"Bearer {KEY}")


class ResponseTranslationTest(unittest.TestCase):
    def generate(self, *items, **kw):
        c, sleeps = client(FakeTransport(*items), **kw)
        return c.generate(request(tools=(READ_FILE,))), sleeps

    def test_content_response(self):
        response, _ = self.generate(completion({"content": "hello", "reasoning_content": "hidden chain"}))
        self.assertIsInstance(response, ModelResponse)
        self.assertEqual((response.text, response.tool_calls, response.finish_reason), ("hello", (), FinishReason.STOP))
        self.assertEqual(response.model, MODEL)
        self.assertEqual(response.metadata, {"provider_attempts": 1, "response_id": "resp-1"})
        self.assertNotIn("hidden chain", repr(response))        # hidden reasoning is dropped

    def test_content_parts(self):
        response, _ = self.generate(completion({"content": [{"type": "text", "text": "a"}, {"type": "text", "text": "b"}]}))
        self.assertEqual(response.text, "ab")

    def test_native_tool_call(self):
        response, _ = self.generate(completion({"content": None, "tool_calls": [
            {"id": "call_9", "type": "function", "function": {"name": "read_file", "arguments": '{"path": "a.py"}'}}]},
            finish="tool_calls"))
        self.assertEqual(response.tool_calls, (ToolCall("call_9", "read_file", {"path": "a.py"}, '{"path": "a.py"}'),))
        self.assertEqual(response.finish_reason, FinishReason.TOOL_CALLS)
        self.assertEqual(response.text, "")

    def test_multiple_tool_calls(self):
        response, _ = self.generate(completion({"tool_calls": [
            {"id": "a", "type": "function", "function": {"name": "read_file", "arguments": '{"path": "x"}'}},
            {"id": "b", "type": "function", "function": {"name": "read_file", "arguments": '{"path": "y"}'}},
            {"type": "function", "function": {"name": "read_file", "arguments": {"path": "z"}}},
        ]}, finish="tool_calls"))
        self.assertEqual([(c.id, c.arguments["path"]) for c in response.tool_calls],
                         [("a", "x"), ("b", "y"), ("call_2", "z")])
        self.assertTrue(all(c.parse_error is None for c in response.tool_calls))

    def test_malformed_tool_arguments_are_reported_not_raised(self):
        response, _ = self.generate(completion({"tool_calls": [
            {"id": "a", "type": "function", "function": {"name": "read_file", "arguments": '{"path": '}},
            {"id": "b", "type": "function", "function": {"name": "read_file", "arguments": '["x"]'}},
        ]}, finish="tool_calls"))
        bad_json, not_object = response.tool_calls
        self.assertIn("not valid JSON", bad_json.parse_error)
        self.assertEqual((bad_json.arguments, bad_json.raw_arguments), ({}, '{"path": '))
        self.assertIn("JSON object", not_object.parse_error)

    def test_tool_calls_written_as_text_markup(self):
        run_cmd = ToolDefinition("run_command", "Run", {"type": "object", "properties": {
            "command": {"type": ["string", "array"]}, "timeout_seconds": {"type": "integer"}}})
        patch = ToolDefinition("apply_patch", "Patch", {"type": "object", "properties": {"patch": {"type": "string"}}})
        text = ("<function=apply_patch>\n<parameter=patch>\n--- a/x.py\n+++ b/x.py\n@@ -1 +1 @@\n-a\n+b\n"
                "</parameter>\n</function>\n</tool_call>\n"
                '<function=run_command><parameter=command>["python", "-m", "unittest"]</parameter>'
                "<parameter=timeout_seconds>30</parameter></function>"
                '<tool_call>{"name": "run_command", "arguments": {"command": "ls"}}</tool_call>'
                "<function=not_offered><parameter=x>1</parameter></function>")
        c, _ = client(FakeTransport(completion({"content": text})))
        response = c.generate(ModelRequest(messages=(Message("user", "go"),), tools=(run_cmd, patch)))
        self.assertEqual([(t.name, dict(t.arguments)) for t in response.tool_calls], [
            ("apply_patch", {"patch": "--- a/x.py\n+++ b/x.py\n@@ -1 +1 @@\n-a\n+b"}),
            ("run_command", {"command": ["python", "-m", "unittest"], "timeout_seconds": 30}),
            ("run_command", {"command": "ls"})])
        self.assertEqual(response.finish_reason, FinishReason.TOOL_CALLS)
        self.assertEqual(response.metadata["tool_calls_from_text"], 3)
        self.assertNotIn("apply_patch", response.text)

    def test_text_markup_is_not_parsed_without_offered_tools_or_with_native_calls(self):
        text = "<function=read_file><parameter=path>a.py</parameter></function>"
        plain, _ = client(FakeTransport(completion({"content": text})))
        self.assertEqual(plain.generate(request()).tool_calls, ())            # no tools offered
        native = completion({"content": text, "tool_calls": [
            {"id": "n", "type": "function", "function": {"name": "read_file", "arguments": '{"path": "b.py"}'}}]})
        c, _ = client(FakeTransport(native))
        (call,) = c.generate(request(tools=(READ_FILE,))).tool_calls
        self.assertEqual(dict(call.arguments), {"path": "b.py"})                # structured calls win

    def test_raw_newlines_in_arguments_are_accepted_and_bad_json_is_located(self):
        response, _ = self.generate(completion({"tool_calls": [
            {"id": "a", "type": "function", "function": {"name": "read_file", "arguments": '{"path": "a\nb"}'}},
            {"id": "b", "type": "function", "function": {"name": "read_file",
                                                         "arguments": '{"path": "say "hi" now"}'}}]}, finish="tool_calls"))
        raw_newline, unescaped = response.tool_calls
        self.assertEqual(dict(raw_newline.arguments), {"path": "a\nb"})
        self.assertIn("near: ...", unescaped.parse_error)
        self.assertIn('say "hi', unescaped.parse_error)

    def test_finish_reasons(self):
        cases = {"stop": FinishReason.STOP, "length": FinishReason.LENGTH, "tool_calls": FinishReason.TOOL_CALLS,
                 "content_filter": FinishReason.CONTENT_FILTER, "something_new": FinishReason.OTHER,
                 None: FinishReason.OTHER}
        for raw, expected in cases.items():
            with self.subTest(raw=raw):
                response, _ = self.generate(completion({"content": "x"}, finish=raw))
                self.assertEqual(response.finish_reason, expected)
                self.assertEqual(response.raw_finish_reason, raw)

    def test_usage(self):
        response, _ = self.generate(completion({"content": "x"}, usage={
            "prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120,
            "prompt_tokens_details": {"cached_tokens": 64}, "completion_tokens_details": {"reasoning_tokens": 5}}))
        self.assertEqual(response.usage, Usage(100, 20, 64, 5))
        alt, _ = self.generate(completion({"content": "x"}, usage={
            "prompt_tokens": 10, "completion_tokens": 2, "prompt_cache_hit_tokens": 8}))
        self.assertEqual(alt.usage, Usage(10, 2, 8, None))

    def test_missing_usage_is_not_invented(self):
        response, _ = self.generate(completion({"content": "x"}))
        self.assertEqual(response.usage, Usage())
        garbage, _ = self.generate(completion({"content": "x"}, usage={"prompt_tokens": "many", "completion_tokens": -1}))
        self.assertEqual(garbage.usage, Usage())

    def test_invalid_body_is_not_retried(self):
        for body in (b"not json", b'{"choices": []}', b"[]"):
            with self.subTest(body=body):
                t = FakeTransport(HttpResponse(200, {}, body))
                with self.assertRaises(ModelError) as ctx:
                    client(t)[0].generate(request())
                self.assertEqual((ctx.exception.kind, ctx.exception.retryable, len(t.requests)),
                                 ("invalid_response", False, 1))


class ErrorAndRetryTest(unittest.TestCase):
    def test_api_error_normalization(self):
        cases = {400: ("bad_request", False), 401: ("auth", False), 403: ("auth", False), 404: ("bad_request", False),
                 422: ("bad_request", False), 429: ("rate_limit", True), 500: ("server_error", True),
                 503: ("server_error", True)}
        for code, (kind, retryable) in cases.items():
            with self.subTest(code=code):
                t = FakeTransport(status(code, {"error": {"message": f"problem {code}"}}))
                with self.assertRaises(ModelError) as ctx:
                    client(t, max_retries=0)[0].generate(request())
                err = ctx.exception
                self.assertEqual((err.kind, err.retryable, err.status_code, err.attempts), (kind, retryable, code, 1))
                self.assertIn(f"HTTP {code}", str(err))
                self.assertIn(f"problem {code}", str(err))

    def test_non_retryable_errors_are_not_retried(self):
        t = FakeTransport(status(400, {"error": {"message": "bad"}}), completion({"content": "never"}))
        with self.assertRaises(ModelError):
            client(t, max_retries=5)[0].generate(request())
        self.assertEqual(len(t.requests), 1)

    def test_timeout(self):
        t = FakeTransport(TransportError("timeout", "request timed out after 3s"))
        with self.assertRaises(ModelError) as ctx:
            client(t, max_retries=0)[0].generate(request())
        self.assertEqual((ctx.exception.kind, ctx.exception.retryable, ctx.exception.attempts), ("timeout", True, 1))

    def test_transient_failures_are_retried(self):
        t = FakeTransport(TransportError("connection", "connection failed: ConnectionRefusedError"),
                          status(503, b"overloaded"), status(429, b"", {"Retry-After": "2"}),
                          completion({"content": "ok"}))
        c, sleeps = client(t, max_retries=3)
        response = c.generate(request())
        self.assertEqual(response.text, "ok")
        self.assertEqual(response.metadata["provider_attempts"], 4)
        self.assertEqual(len(t.requests), 4)
        self.assertEqual(sleeps, [0.5, 1.0, 2.0])               # backoff, backoff, Retry-After

    def test_retry_after_is_capped(self):
        t = FakeTransport(status(429, b"", {"retry-after": "9999"}), completion({"content": "ok"}))
        c, sleeps = client(t)
        c.generate(request())
        self.assertEqual(sleeps, [MAX_RETRY_AFTER_SECONDS])

    def test_retry_cap(self):
        t = FakeTransport(*[status(500, b"")] * 10)
        c, sleeps = client(t, max_retries=2)
        with self.assertRaises(ModelError) as ctx:
            c.generate(request())
        self.assertEqual((len(t.requests), ctx.exception.attempts, len(sleeps)), (3, 3, 2))
        self.assertTrue(ctx.exception.retryable)

    def test_metrics_count_logical_calls_and_provider_attempts_separately(self):
        from harness.metrics import ExecutionMetrics
        from harness.model.client import MeteredModelClient
        metrics = ExecutionMetrics()
        t = FakeTransport(status(502, b""), completion({"content": "ok"}, usage={"prompt_tokens": 3, "completion_tokens": 1}),
                          *[status(500, b"")] * 3)
        metered = MeteredModelClient(client(t, max_retries=2)[0], metrics)
        metered.generate(request())
        with self.assertRaises(ModelError):
            metered.generate(request())
        self.assertEqual((metrics.model_calls, metrics.provider_attempts, metrics.model_failures), (2, 5, 1))
        self.assertEqual((metrics.input_tokens, metrics.output_tokens), (3, 1))


class KeyConfidentialityTest(unittest.TestCase):
    def test_key_never_in_exceptions_or_responses(self):
        echoes = [
            status(401, {"error": {"message": f"Incorrect API key provided: {KEY}"}}),
            status(400, f"raw echo Authorization: Bearer {KEY}".encode()),
            status(500, {"message": KEY}),
        ]
        for item in echoes:
            with self.subTest(status=item.status):
                with self.assertRaises(ModelError) as ctx:
                    client(FakeTransport(item), max_retries=0)[0].generate(request())
                text = f"{ctx.exception} {ctx.exception!r} {vars(ctx.exception)}"
                self.assertNotIn(KEY, text)
                self.assertNotIn("Authorization", str(ctx.exception).replace("raw echo Authorization", ""))
        response = client(FakeTransport(completion({"content": "ok"})))[0].generate(request())
        self.assertNotIn(KEY, repr(response))
        c = client(FakeTransport())[0]
        self.assertNotIn(KEY, repr(c))
        self.assertNotIn(KEY, str(vars(c).get("model")))

    def test_key_is_required(self):
        with self.assertRaises(ValueError):
            OpenAICompatibleClient(api_key="", model=MODEL, base_url=BASE)


# ---------------------------------------------------------------------------
# a real local HTTP endpoint (default urllib transport)
# ---------------------------------------------------------------------------

class LocalServer:
    """A throwaway OpenAI-compatible endpoint on 127.0.0.1 that answers from a queue."""

    def __init__(self, responses=(), delay=0.0):
        self.responses = list(responses)     # dicts (200 JSON) or (status, body) tuples
        self.delay = delay
        self.seen = []
        outer = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                length = int(self.headers.get("Content-Length", 0))
                outer.seen.append({"path": self.path, "headers": dict(self.headers.items()),
                                   "body": json.loads(self.rfile.read(length))})
                if outer.delay:
                    time.sleep(outer.delay)
                item = outer.responses.pop(0) if outer.responses else (500, {"error": "script exhausted"})
                code, body = item if isinstance(item, tuple) else (200, item)
                data = json.dumps(body).encode()
                try:
                    self.send_response(code)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                except (BrokenPipeError, ConnectionResetError):
                    pass

            def log_message(self, *args):
                pass

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    @property
    def base_url(self):
        return f"http://127.0.0.1:{self.server.server_address[1]}/custom/v1"

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.server.shutdown()
        self.server.server_close()


def chat(content="", tool_calls=None, finish="stop", usage=None):
    message = {"role": "assistant", "content": content}
    if tool_calls:
        message["tool_calls"] = tool_calls
    body = {"id": "x", "model": MODEL, "choices": [{"index": 0, "message": message, "finish_reason": finish}]}
    if usage:
        body["usage"] = usage
    return body


class LocalEndpointTest(unittest.TestCase):
    def test_round_trip_over_http(self):
        with LocalServer([chat("hello", usage={"prompt_tokens": 5, "completion_tokens": 1})]) as server:
            c = OpenAICompatibleClient(api_key=KEY, model=MODEL, base_url=server.base_url)
            response = c.generate(request())
        self.assertEqual((response.text, response.usage.input_tokens), ("hello", 5))
        (seen,) = server.seen
        self.assertEqual(seen["path"], "/custom/v1/chat/completions")
        self.assertEqual(seen["headers"]["Authorization"], f"Bearer {KEY}")
        self.assertEqual(seen["body"]["model"], MODEL)

    def test_http_error_over_http_does_not_leak_headers(self):
        with LocalServer([(401, {"error": {"message": "invalid key"}})]) as server:
            c = OpenAICompatibleClient(api_key=KEY, model=MODEL, base_url=server.base_url, max_retries=0)
            with self.assertRaises(ModelError) as ctx:
                c.generate(request())
        self.assertEqual((ctx.exception.status_code, ctx.exception.kind), (401, "auth"))
        self.assertNotIn(KEY, str(ctx.exception))
        self.assertNotIn("Bearer", str(ctx.exception))

    def test_timeout_over_http(self):
        with LocalServer([chat("late")], delay=1.5) as server:
            c = OpenAICompatibleClient(api_key=KEY, model=MODEL, base_url=server.base_url, timeout_seconds=0.3,
                                       max_retries=0)
            started = time.monotonic()
            with self.assertRaises(ModelError) as ctx:
                c.generate(request())
            self.assertLess(time.monotonic() - started, 1.4)
        self.assertEqual((ctx.exception.kind, ctx.exception.retryable), ("timeout", True))

    def test_connection_refused(self):
        with LocalServer() as server:
            base = server.base_url
        c = OpenAICompatibleClient(api_key=KEY, model=MODEL, base_url=base, max_retries=1, sleep=lambda s: None)
        with self.assertRaises(ModelError) as ctx:
            c.generate(request())
        self.assertEqual((ctx.exception.kind, ctx.exception.attempts), ("connection", 2))
        self.assertNotIn(KEY, str(ctx.exception))


if __name__ == "__main__":
    unittest.main()

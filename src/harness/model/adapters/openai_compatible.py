"""OpenAICompatibleClient: ``ModelClient`` over an OpenAI-compatible chat-completions API.

Transport and protocol translation only:

    ModelRequest -> {"model", "messages", "tools", ...} -> POST {base_url}/chat/completions
    -> {"choices": [{"message": ..., "finish_reason": ...}], "usage": ...} -> ModelResponse

Nothing here knows about DeepSeek, Qwen or any other model family: the endpoint,
model id and optional extra headers come from configuration, so the same class
serves any OpenAI-compatible deployment (provider API, gateway, local server).

- stdlib only (``urllib``), so installation never needs the network;
- bounded retries for transient transport failures only (connection errors,
  timeouts, HTTP 408/409/429/500/502/503/504), honouring ``Retry-After``;
  invalid output, rejected plans and tool-argument problems are *not* retried
  here — higher layers own those;
- the API key is used only to build the Authorization header; it never appears
  in ``ModelRequest``, ``ModelResponse``, metadata or error messages (provider
  error text is redacted before it is used);
- hidden reasoning fields some providers return (e.g. ``reasoning_content``) are
  dropped, never stored;
- tool calls that an endpoint returns as text markup instead of structured
  ``tool_calls`` (``<function=NAME><parameter=K>V</parameter></function>`` or
  ``<tool_call>{"name": ..., "arguments": {...}}</tool_call>``) are converted to
  ``ToolCall`` - only for tools offered in the request, and only when the response
  has no structured tool calls. This is protocol normalization, not model-specific
  logic: whichever model emits these forms gets the same treatment.
"""

from __future__ import annotations

import json
import re
import socket
import ssl
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Optional, Sequence

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

RETRYABLE_STATUS = frozenset({408, 409, 429, 500, 502, 503, 504})
MAX_BACKOFF_SECONDS = 8.0
MAX_RETRY_AFTER_SECONDS = 30.0
MAX_ERROR_TEXT = 300
FINISH_REASONS = {
    "stop": FinishReason.STOP, "end_turn": FinishReason.STOP, "eos": FinishReason.STOP,
    "length": FinishReason.LENGTH, "max_tokens": FinishReason.LENGTH,
    "tool_calls": FinishReason.TOOL_CALLS, "function_call": FinishReason.TOOL_CALLS,
    "content_filter": FinishReason.CONTENT_FILTER,
}
_XML_CALL = re.compile(r"<function=([\w.-]+)>(.*?)</function>", re.DOTALL)
_XML_PARAM = re.compile(r"<parameter=([\w.-]+)>\n?(.*?)\n?</parameter>", re.DOTALL)
_JSON_CALL = re.compile(r"<tool_call>\s*(\{.*?\})\s*</tool_call>", re.DOTALL)
_MARKUP_LEFTOVER = re.compile(r"</?tool_call>")


@dataclass(frozen=True)
class HttpResponse:
    status: int
    headers: Mapping[str, str]
    body: bytes


class TransportError(Exception):
    """Raised by a transport for failures before an HTTP status exists."""

    def __init__(self, kind: str, message: str) -> None:   # kind: "timeout" or "connection"
        super().__init__(message)
        self.kind = kind


Transport = Callable[[str, Mapping[str, str], bytes, float], HttpResponse]


def urllib_transport(url: str, headers: Mapping[str, str], body: bytes, timeout: float) -> HttpResponse:
    request = urllib.request.Request(url, data=body, headers=dict(headers), method="POST")
    try:
        with urllib.request.urlopen(request, timeout=timeout, context=ssl.create_default_context()) as response:
            return HttpResponse(response.status, dict(response.headers.items()), response.read())
    except urllib.error.HTTPError as exc:          # has a status: let the client classify it
        try:
            body = exc.read() if exc.fp is not None else b""
        finally:
            exc.close()
        return HttpResponse(exc.code, dict(exc.headers.items()) if exc.headers else {}, body)
    except (socket.timeout, TimeoutError) as exc:
        raise TransportError("timeout", f"request timed out after {timeout}s") from exc
    except urllib.error.URLError as exc:
        reason = exc.reason
        if isinstance(reason, (socket.timeout, TimeoutError)):
            raise TransportError("timeout", f"request timed out after {timeout}s") from exc
        raise TransportError("connection", f"connection failed: {reason.__class__.__name__}") from exc
    except (ConnectionError, OSError) as exc:
        raise TransportError("connection", f"connection failed: {exc.__class__.__name__}") from exc


class OpenAICompatibleClient:
    """``ModelClient`` for OpenAI-compatible chat-completions endpoints."""

    def __init__(self, *, api_key: str, model: str, base_url: str, timeout_seconds: float = 120,
                 max_retries: int = 2, headers: Sequence[tuple[str, str]] = (), provider: Optional[str] = None,
                 structured_output: str = "none", transport: Optional[Transport] = None,
                 sleep: Callable[[float], None] = time.sleep) -> None:
        if not api_key:
            raise ValueError("api_key is required")
        if not model or not base_url:
            raise ValueError("model and base_url are required")
        self._api_key = api_key
        self.model = model
        self.provider = provider
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds
        self.max_retries = max(0, max_retries)
        self.extra_headers = tuple(headers)
        self.structured_output = structured_output
        self._transport = transport or urllib_transport
        self._sleep = sleep

    def __repr__(self) -> str:   # never show the key
        return f"OpenAICompatibleClient(model={self.model!r}, base_url={self.base_url!r})"

    @property
    def url(self) -> str:
        return self.base_url if self.base_url.endswith("/chat/completions") else self.base_url + "/chat/completions"

    # request ----------------------------------------------------------------
    def build_payload(self, request: ModelRequest) -> dict:
        payload: dict[str, Any] = {"model": self.model, "messages": [_message(m) for m in request.messages]}
        if request.tools:
            payload["tools"] = [_tool(t) for t in request.tools]
        if request.max_output_tokens is not None:
            payload["max_tokens"] = request.max_output_tokens
        if request.temperature is not None:
            payload["temperature"] = request.temperature
        if request.response_schema is not None and self.structured_output == "json_object":
            payload["response_format"] = {"type": "json_object"}
        return payload

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        headers.update((k, v) for k, v in self.extra_headers if k.lower() != "authorization")
        headers["Authorization"] = f"Bearer {self._api_key}"
        return headers

    # call ---------------------------------------------------------------------
    def generate(self, request: ModelRequest) -> ModelResponse:
        body = json.dumps(self.build_payload(request)).encode("utf-8")
        attempts = 0
        while True:
            attempts += 1
            try:
                response = self._transport(self.url, self._headers(), body, self.timeout_seconds)
            except TransportError as exc:
                error = ModelError(f"model endpoint {exc.kind}: {exc}", retryable=True, kind=exc.kind, attempts=attempts)
                retry_after = None
            else:
                if 200 <= response.status < 300:
                    return self._parse(response.body, attempts, request.tools)
                error = self._http_error(response, attempts)
                retry_after = _retry_after(response.headers)
            if not error.retryable or attempts > self.max_retries:
                raise error
            self._sleep(retry_after if retry_after is not None else min(0.5 * 2 ** (attempts - 1), MAX_BACKOFF_SECONDS))

    def _redact(self, text: str) -> str:
        return text.replace(self._api_key, "***")

    def _http_error(self, response: HttpResponse, attempts: int) -> ModelError:
        status = response.status
        detail = ""
        try:
            data = json.loads(response.body.decode("utf-8", "replace") or "{}")
            err = data.get("error") if isinstance(data, dict) else None
            detail = (err.get("message") if isinstance(err, dict) else err) or data.get("message") or ""
        except (ValueError, AttributeError):
            detail = response.body.decode("utf-8", "replace")
        detail = self._redact(str(detail))[:MAX_ERROR_TEXT]
        kind = ("rate_limit" if status == 429 else "auth" if status in (401, 403)
                else "server_error" if status >= 500 else "timeout" if status == 408 else "bad_request")
        return ModelError(f"model endpoint returned HTTP {status} ({kind})" + (f": {detail}" if detail else ""),
                          retryable=status in RETRYABLE_STATUS, kind=kind, status_code=status, attempts=attempts)

    # response -----------------------------------------------------------------
    def _parse(self, raw: bytes, attempts: int, offered: Sequence[ToolDefinition] = ()) -> ModelResponse:
        try:
            data = json.loads(raw.decode("utf-8"))
            choice = data["choices"][0]
            message = choice.get("message") or {}
        except (ValueError, KeyError, IndexError, TypeError, AttributeError):
            raise ModelError("model endpoint returned a response that is not a chat completion",
                             kind="invalid_response", attempts=attempts) from None
        raw_finish = choice.get("finish_reason")
        tool_calls = tuple(_tool_call(i, c) for i, c in enumerate(message.get("tool_calls") or ()))
        text = _text(message.get("content"))
        metadata: dict[str, Any] = {"provider_attempts": attempts}
        if not tool_calls and offered and text:
            tool_calls, text = _text_tool_calls(text, offered)
            if tool_calls:
                metadata["tool_calls_from_text"] = len(tool_calls)
        finish = FINISH_REASONS.get(raw_finish or "", FinishReason.OTHER)
        if tool_calls and finish in (FinishReason.OTHER, FinishReason.STOP):
            finish = FinishReason.TOOL_CALLS
        if isinstance(data.get("id"), str):
            metadata["response_id"] = data["id"]
        return ModelResponse(
            text=text,
            tool_calls=tool_calls,
            finish_reason=finish,
            usage=_usage(data.get("usage")),
            provider=self.provider,
            model=data.get("model") if isinstance(data.get("model"), str) else self.model,
            raw_finish_reason=raw_finish if isinstance(raw_finish, str) else None,
            metadata=metadata,
        )


# --------------------------------------------------------------------------
# translation helpers
# --------------------------------------------------------------------------

def _message(m: Message) -> dict:
    if m.role == "tool":
        return {"role": "tool", "tool_call_id": m.tool_call_id, "content": m.content}
    out: dict[str, Any] = {"role": m.role, "content": m.content}
    if m.tool_calls:
        out["content"] = m.content or None
        out["tool_calls"] = [{"id": c.id, "type": "function", "function": {
            "name": c.name, "arguments": c.raw_arguments or json.dumps(dict(c.arguments), sort_keys=True)}}
            for c in m.tool_calls]
    return out


def _tool(t: ToolDefinition) -> dict:
    return {"type": "function", "function": {"name": t.name, "description": t.description,
                                             "parameters": dict(t.parameters)}}


def _tool_call(index: int, raw: Any) -> ToolCall:
    raw = raw if isinstance(raw, dict) else {}
    fn = raw.get("function") if isinstance(raw.get("function"), dict) else {}
    call_id = raw.get("id") if isinstance(raw.get("id"), str) and raw.get("id") else f"call_{index}"
    name = fn.get("name") if isinstance(fn.get("name"), str) else ""
    arguments = fn.get("arguments")
    if isinstance(arguments, dict):                     # some servers send an object, not a string
        return ToolCall(call_id, name, arguments, json.dumps(arguments, sort_keys=True))
    text = arguments if isinstance(arguments, str) else ""
    if not name:
        return ToolCall(call_id, name, {}, text, "tool call has no function name")
    try:
        # strict=False: accept raw newlines/tabs inside strings, which models often emit in code arguments
        parsed = json.loads(text, strict=False) if text.strip() else {}
    except json.JSONDecodeError as exc:
        near = text[max(exc.pos - 40, 0): exc.pos + 20].replace("\n", "\\n")
        return ToolCall(call_id, name, {}, text, f"arguments are not valid JSON: {exc.msg} at position {exc.pos} "
                                                 f"(near: ...{near}...); escape double quotes inside strings as \\\"")
    if not isinstance(parsed, dict):
        return ToolCall(call_id, name, {}, text, "arguments must be a JSON object")
    return ToolCall(call_id, name, parsed, text)


def _text_tool_calls(text: str, offered: Sequence[ToolDefinition]) -> tuple[tuple[ToolCall, ...], str]:
    """Tool calls written as text markup -> ToolCall (offered tools only). Returns (calls, remaining text)."""
    schemas = {t.name: (t.parameters or {}).get("properties", {}) for t in offered}
    calls: list[ToolCall] = []
    for m in _XML_CALL.finditer(text):
        name = m.group(1)
        if name not in schemas:
            continue
        arguments = {k: _coerce(v, schemas[name].get(k, {})) for k, v in _XML_PARAM.findall(m.group(2))}
        calls.append(ToolCall(f"text_call_{len(calls)}", name, arguments, json.dumps(arguments, sort_keys=True)))
    for m in _JSON_CALL.finditer(text):
        try:
            data = json.loads(m.group(1))
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict) and data.get("name") in schemas:
            arguments = data.get("arguments", data.get("parameters", {}))
            if isinstance(arguments, str):
                try:
                    arguments = json.loads(arguments)
                except json.JSONDecodeError:
                    arguments = None
            if isinstance(arguments, dict):
                calls.append(ToolCall(f"text_call_{len(calls)}", data["name"], arguments,
                                      json.dumps(arguments, sort_keys=True)))
    if not calls:
        return (), text
    remaining = _MARKUP_LEFTOVER.sub("", _JSON_CALL.sub("", _XML_CALL.sub("", text))).strip()
    return tuple(calls), remaining


def _coerce(value: str, schema: Mapping[str, Any]) -> Any:
    """A text parameter value -> the JSON type its schema asks for (strings stay strings)."""
    types = schema.get("type")
    types = set(types) if isinstance(types, list) else {types} if types else set()
    if types == {"string"}:
        return value
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return value
    return parsed if not types or _json_type(parsed) in types else value


def _json_type(value: Any) -> str:
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    return {float: "number", str: "string", list: "array", dict: "object"}.get(type(value), "null")


def _text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):   # content parts
        return "".join(p.get("text", "") for p in content if isinstance(p, dict) and p.get("type") in (None, "text"))
    return ""


def _int(value: Any) -> Optional[int]:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def _usage(raw: Any) -> Usage:
    if not isinstance(raw, dict):
        return Usage()
    prompt_details = raw.get("prompt_tokens_details") if isinstance(raw.get("prompt_tokens_details"), dict) else {}
    completion_details = (raw.get("completion_tokens_details")
                          if isinstance(raw.get("completion_tokens_details"), dict) else {})
    cached = _int(prompt_details.get("cached_tokens"))
    if cached is None:
        cached = _int(raw.get("prompt_cache_hit_tokens"))     # a common alternative field name
    return Usage(input_tokens=_int(raw.get("prompt_tokens")), output_tokens=_int(raw.get("completion_tokens")),
                 cached_input_tokens=cached, reasoning_tokens=_int(completion_details.get("reasoning_tokens")))


def _retry_after(headers: Mapping[str, str]) -> Optional[float]:
    for key, value in headers.items():
        if key.lower() == "retry-after":
            try:
                return max(0.0, min(float(value), MAX_RETRY_AFTER_SECONDS))
            except ValueError:
                return None
    return None

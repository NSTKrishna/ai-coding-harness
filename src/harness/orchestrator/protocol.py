"""Provider-neutral JSON protocol for model output.

A response must contain exactly one JSON object. The whole body (optionally one
fenced block) is tried first; otherwise the single JSON object found in the text
(in a fenced block or inline, e.g. after a sentence of prose) is used. Zero or
several candidate objects are rejected - the parser never guesses between them.

Executor actions (one per response):

    {"action": "tool", "tool": "<name>", "arguments": {...}}
    {"action": "complete", "summary": "<what was done>"}
    {"action": "blocked", "reason": "<why work cannot continue>"}

Tolerated variants (common in real model output, unambiguous given the tool
list): ``{"action": "<tool name>", "arguments": {...}}`` (arguments may also be
given as the remaining top-level fields) and ``{"tool": "<name>", "arguments":
{...}}`` without "action". With native tool calling, "complete" and "blocked" are
offered as the control tools ``CONTROL_TOOLS``.

Native tool calls (``ModelResponse.tool_calls``) are used instead of the text
when present; several in one response (parallel tool calls) are run in order,
up to ``MAX_TOOL_CALLS_PER_STEP``. Both routes produce the same ``ToolCall``,
which goes to ``ToolRegistry.dispatch_call``.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Optional

from harness.model.types import ModelResponse, ToolCall, ToolDefinition

MAX_TEXT_FIELD = 500
MAX_TOOL_CALLS_PER_STEP = 8
_FENCE = re.compile(r"^```(?:json)?[ \t]*\n(.*)\n```$", re.DOTALL)
_ANY_FENCE = re.compile(r"```(?:json)?[ \t]*\n(.*?)\n?```", re.DOTALL)

# Native-tool equivalents of the "complete" / "blocked" actions (not registry tools: never dispatched).
CONTROL_TOOLS = (
    ToolDefinition("complete", "Finish: the changes are done. Verification runs afterwards.",
                   {"type": "object", "properties": {"summary": {"type": "string",
                                                                 "description": "Short description of the changes made."}},
                    "required": ["summary"], "additionalProperties": False}),
    ToolDefinition("blocked", "Stop: the work cannot continue.",
                   {"type": "object", "properties": {"reason": {"type": "string",
                                                                "description": "Why the work cannot continue."}},
                    "required": ["reason"], "additionalProperties": False}),
)
CONTROL_NAMES = frozenset(t.name for t in CONTROL_TOOLS)


class ProtocolError(Exception):
    """Model output that does not follow the protocol. The message is safe to show."""


def extract_json_object(text: str) -> dict:
    body = (text or "").strip()
    fenced = _FENCE.match(body)
    if fenced:
        body = fenced.group(1).strip()
    if not body:
        raise ProtocolError("response is empty; expected one JSON object")
    try:
        value = json.loads(body, strict=False)   # strict=False: raw newlines/tabs inside strings (common in code)
    except json.JSONDecodeError as exc:
        embedded = _embedded_objects(body)
        if len(embedded) == 1:
            return embedded[0]
        if len(embedded) > 1:
            raise ProtocolError(f"response contains {len(embedded)} JSON objects; expected exactly one") from None
        raise ProtocolError(f"response is not valid JSON: {exc.msg} at line {exc.lineno} column {exc.colno}") from None
    if not isinstance(value, dict):
        raise ProtocolError(f"expected a JSON object, got {type(value).__name__}")
    return value


def _embedded_objects(text: str) -> list[dict]:
    """Distinct top-level JSON objects inside surrounding text: fenced blocks first, else inline."""
    found: list[dict] = []
    for block in _ANY_FENCE.findall(text):
        try:
            value = json.loads(block.strip(), strict=False)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict) and value not in found:
            found.append(value)
    if found:
        return found
    decoder, i = json.JSONDecoder(strict=False), 0
    while True:
        i = text.find("{", i)
        if i < 0:
            return found
        try:
            value, end = decoder.raw_decode(text, i)
        except json.JSONDecodeError:
            i += 1
            continue
        if isinstance(value, dict) and value not in found:
            found.append(value)
        i = end


@dataclass(frozen=True)
class Action:
    kind: str                         # "tool", "complete" or "blocked"
    call: Optional[ToolCall] = None   # for "tool": the first (or only) call
    calls: tuple[ToolCall, ...] = ()  # for "tool": every call, in order (native responses may carry several)
    text: str = ""                    # summary (complete) or reason (blocked)
    source: str = "text"              # "native" or "text"


def _require_keys(obj: dict, required: set, what: str) -> None:
    missing = sorted(required - set(obj))
    extra = sorted(set(obj) - required)
    if missing:
        raise ProtocolError(f"{what}: missing field(s) {', '.join(missing)}")
    if extra:
        raise ProtocolError(f"{what}: unexpected field(s) {', '.join(extra)}")


def _short_text(value: Any, field_name: str, what: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ProtocolError(f"{what}: '{field_name}' must be a non-empty string")
    if len(value) > MAX_TEXT_FIELD:
        raise ProtocolError(f"{what}: '{field_name}' is longer than {MAX_TEXT_FIELD} characters")
    return value.strip()


def _control(call: ToolCall) -> Action:
    field_name = "summary" if call.name == "complete" else "reason"
    value = call.arguments.get(field_name) if not call.parse_error else None
    if not isinstance(value, str) or not value.strip():
        value = f"({call.name} called without a {field_name})"
    return Action(kind=call.name, text=value.strip()[:MAX_TEXT_FIELD], source="native")


def parse_action(response: ModelResponse, step: int, tool_names: Optional[frozenset] = None) -> Action:
    """``tool_names`` (the registry's tools) enables the tolerated ``{"action": "<tool>"}`` form."""
    if response.tool_calls:
        calls = tuple(response.tool_calls)
        tools = tuple(c for c in calls if c.name not in CONTROL_NAMES)
        if not tools:                       # only control calls: the first one decides
            return _control(calls[0])
        if len(tools) > MAX_TOOL_CALLS_PER_STEP:
            raise ProtocolError(f"at most {MAX_TOOL_CALLS_PER_STEP} tool calls per step, got {len(tools)}")
        # control calls mixed with real tool calls are dropped: the tools run first, the model decides again
        return Action(kind="tool", call=tools[0], calls=tools, source="native")

    obj = extract_json_object(response.text)
    kind = obj.get("action")
    if kind is None and isinstance(obj.get("tool"), str) and isinstance(obj.get("arguments"), dict):
        obj = {"action": "tool", "tool": obj["tool"], "arguments": obj["arguments"]}
        kind = "tool"
    if tool_names and isinstance(kind, str) and kind in tool_names:
        arguments = obj.get("arguments")
        if not isinstance(arguments, dict):
            arguments = {k: v for k, v in obj.items() if k != "action"}
        obj, kind = {"action": "tool", "tool": obj["action"], "arguments": arguments}, "tool"
    if kind == "tool":
        _require_keys(obj, {"action", "tool", "arguments"}, "tool action")
        name = obj["tool"]
        if not isinstance(name, str) or not name.strip():
            raise ProtocolError("tool action: 'tool' must be a non-empty string")
        if not isinstance(obj["arguments"], dict):
            raise ProtocolError("tool action: 'arguments' must be a JSON object")
        call = ToolCall(id=f"step-{step}", name=name, arguments=obj["arguments"],
                        raw_arguments=json.dumps(obj["arguments"], sort_keys=True))
        return Action(kind="tool", call=call, calls=(call,), source="text")
    if kind == "complete":
        _require_keys(obj, {"action", "summary"}, "complete action")
        return Action(kind="complete", text=_short_text(obj["summary"], "summary", "complete action"))
    if kind == "blocked":
        _require_keys(obj, {"action", "reason"}, "blocked action")
        return Action(kind="blocked", text=_short_text(obj["reason"], "reason", "blocked action"))
    if kind is None:
        raise ProtocolError("missing 'action' field")
    raise ProtocolError(f"unknown action {kind!r}; expected 'tool', 'complete' or 'blocked'")

"""Strict, provider-neutral JSON protocol for model output.

A response body must be exactly one JSON object, optionally wrapped in one
fenced code block (```json ... ``` or ``` ... ```) that makes up the whole
response. Nothing else is parsed: no prose, no "the first JSON-looking thing".

Executor actions (one per response):

    {"action": "tool", "tool": "<name>", "arguments": {...}}
    {"action": "complete", "summary": "<what was done>"}
    {"action": "blocked", "reason": "<why work cannot continue>"}

A native tool call (``ModelResponse.tool_calls``) is used instead of the text
when present. Both routes produce the same ``ToolCall``, which goes to
``ToolRegistry.dispatch_call``.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Optional

from harness.model.types import ModelResponse, ToolCall

MAX_TEXT_FIELD = 500
_FENCE = re.compile(r"^```(?:json)?[ \t]*\n(.*)\n```$", re.DOTALL)


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
        value = json.loads(body)
    except json.JSONDecodeError as exc:
        raise ProtocolError(f"response is not valid JSON: {exc.msg} at line {exc.lineno} column {exc.colno}") from None
    if not isinstance(value, dict):
        raise ProtocolError(f"expected a JSON object, got {type(value).__name__}")
    return value


@dataclass(frozen=True)
class Action:
    kind: str                         # "tool", "complete" or "blocked"
    call: Optional[ToolCall] = None   # for "tool"
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


def parse_action(response: ModelResponse, step: int) -> Action:
    if response.tool_calls:
        if len(response.tool_calls) != 1:
            raise ProtocolError(f"expected exactly one tool call per step, got {len(response.tool_calls)}")
        return Action(kind="tool", call=response.tool_calls[0], source="native")

    obj = extract_json_object(response.text)
    kind = obj.get("action")
    if kind == "tool":
        _require_keys(obj, {"action", "tool", "arguments"}, "tool action")
        name = obj["tool"]
        if not isinstance(name, str) or not name.strip():
            raise ProtocolError("tool action: 'tool' must be a non-empty string")
        if not isinstance(obj["arguments"], dict):
            raise ProtocolError("tool action: 'arguments' must be a JSON object")
        call = ToolCall(id=f"step-{step}", name=name, arguments=obj["arguments"],
                        raw_arguments=json.dumps(obj["arguments"], sort_keys=True))
        return Action(kind="tool", call=call, source="text")
    if kind == "complete":
        _require_keys(obj, {"action", "summary"}, "complete action")
        return Action(kind="complete", text=_short_text(obj["summary"], "summary", "complete action"))
    if kind == "blocked":
        _require_keys(obj, {"action", "reason"}, "blocked action")
        return Action(kind="blocked", text=_short_text(obj["reason"], "reason", "blocked action"))
    if kind is None:
        raise ProtocolError("missing 'action' field")
    raise ProtocolError(f"unknown action {kind!r}; expected 'tool', 'complete' or 'blocked'")

"""Deterministic, offline model for tests."""

from __future__ import annotations

import dataclasses
import json
from typing import Any, Callable, Iterable, Mapping, Optional, Union

from harness.model.types import (
    FinishReason,
    ModelError,
    ModelRequest,
    ModelResponse,
    ToolCall,
    Usage,
)

ScriptItem = Union[ModelResponse, Exception, Callable[[ModelRequest], ModelResponse]]


class ScriptExhausted(ModelError):
    """The test asked the fake model for more responses than were scripted."""


class ScriptedModel:
    """Returns queued responses in order and records every request.

    Queue items can be a ``ModelResponse``, an exception to raise, or a
    callable that builds a response from the request. A response without usage
    gets deterministic fake usage (``chars // 4`` for input and output) unless
    ``fake_usage=False``, which simulates a provider that reports no usage.
    """

    provider = "fake"

    def __init__(self, script: Iterable[ScriptItem] = (), *, model: str = "scripted",
                 fake_usage: bool = True) -> None:
        self.model = model
        self.fake_usage = fake_usage
        self._queue: list[ScriptItem] = list(script)
        self.requests: list[ModelRequest] = []

    @property
    def call_count(self) -> int:
        return len(self.requests)

    @property
    def remaining(self) -> int:
        return len(self._queue)

    def queue(self, *items: ScriptItem) -> "ScriptedModel":
        self._queue.extend(items)
        return self

    def generate(self, request: ModelRequest) -> ModelResponse:
        self.requests.append(request)
        if not self._queue:
            raise ScriptExhausted(f"no scripted response left for call {self.call_count}")
        item = self._queue.pop(0)
        if isinstance(item, Exception):
            raise item
        response = item(request) if callable(item) else item
        if self.fake_usage and response.usage == Usage():
            response = dataclasses.replace(response, usage=_fake_usage(request, response))
        return dataclasses.replace(
            response, provider=response.provider or self.provider, model=response.model or self.model
        )


def text_response(text: str, *, finish_reason: FinishReason = FinishReason.STOP,
                  usage: Optional[Usage] = None) -> ModelResponse:
    return ModelResponse(text=text, finish_reason=finish_reason, usage=usage or Usage())


def tool_call_response(name: str, arguments: Mapping[str, Any], *, call_id: str = "call_1",
                       text: str = "") -> ModelResponse:
    call = ToolCall(id=call_id, name=name, arguments=dict(arguments),
                    raw_arguments=json.dumps(arguments, sort_keys=True))
    return ModelResponse(text=text, tool_calls=(call,), finish_reason=FinishReason.TOOL_CALLS)


def malformed_tool_call_response(name: str, raw_arguments: str, *,
                                 call_id: str = "call_1") -> ModelResponse:
    """A tool call whose arguments are not valid JSON, as a provider may return."""
    try:
        json.loads(raw_arguments)
        raise ValueError("raw_arguments is valid JSON; use tool_call_response instead")
    except json.JSONDecodeError as exc:
        error = f"arguments are not valid JSON: {exc.msg} at position {exc.pos}"
    call = ToolCall(id=call_id, name=name, raw_arguments=raw_arguments, parse_error=error)
    return ModelResponse(text="", tool_calls=(call,), finish_reason=FinishReason.TOOL_CALLS)


def _fake_usage(request: ModelRequest, response: ModelResponse) -> Usage:
    input_chars = sum(len(m.content) for m in request.messages)
    output_chars = len(response.text) + sum(len(c.raw_arguments or "") for c in response.tool_calls)
    return Usage(input_tokens=input_chars // 4, output_tokens=output_chars // 4)

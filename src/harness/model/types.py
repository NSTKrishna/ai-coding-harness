"""Provider-neutral request/response types.

Provider adapters translate to and from these types. Nothing outside an
adapter sees a provider's own request or response objects. The API key is
never part of these types; adapters receive it when they are constructed.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from typing import Any, Mapping, Optional

ROLES = ("system", "user", "assistant", "tool")


class FinishReason(str, enum.Enum):
    STOP = "stop"                  # natural end of the answer
    LENGTH = "length"              # hit the output token limit
    TOOL_CALLS = "tool_calls"      # the model asked for tools to be run
    CONTENT_FILTER = "content_filter"
    OTHER = "other"                # anything an adapter cannot map


@dataclass(frozen=True)
class ToolDefinition:
    """A tool the model may call. ``parameters`` is a JSON Schema object."""

    name: str
    description: str
    parameters: Mapping[str, Any]


@dataclass(frozen=True)
class ToolCall:
    """A tool invocation requested by the model.

    ``parse_error`` is set when the provider returned arguments that are not a
    JSON object; ``arguments`` is then empty and ``raw_arguments`` holds the text.
    """

    id: str
    name: str
    arguments: Mapping[str, Any] = field(default_factory=dict)
    raw_arguments: Optional[str] = None
    parse_error: Optional[str] = None


@dataclass(frozen=True)
class Message:
    role: str
    content: str
    tool_calls: tuple[ToolCall, ...] = ()   # assistant messages only
    tool_call_id: Optional[str] = None      # tool messages only: which call this answers

    def __post_init__(self) -> None:
        if self.role not in ROLES:
            raise ValueError(f"unknown message role {self.role!r}; expected one of {ROLES}")
        if self.role == "tool" and not self.tool_call_id:
            raise ValueError("tool messages need tool_call_id")
        if self.tool_calls and self.role != "assistant":
            raise ValueError("only assistant messages carry tool_calls")


@dataclass(frozen=True)
class ModelRequest:
    messages: tuple[Message, ...]
    tools: tuple[ToolDefinition, ...] = ()
    response_schema: Optional[Mapping[str, Any]] = None  # JSON Schema for structured output
    max_output_tokens: Optional[int] = None
    temperature: Optional[float] = None
    purpose: str = ""  # e.g. "plan", "execute"; used for accounting only

    def __post_init__(self) -> None:
        if not self.messages:
            raise ValueError("a model request needs at least one message")
        object.__setattr__(self, "messages", tuple(self.messages))
        object.__setattr__(self, "tools", tuple(self.tools))


@dataclass(frozen=True)
class Usage:
    input_tokens: Optional[int] = None
    output_tokens: Optional[int] = None


@dataclass(frozen=True)
class ModelResponse:
    text: str
    tool_calls: tuple[ToolCall, ...] = ()
    finish_reason: FinishReason = FinishReason.STOP
    usage: Usage = Usage()
    provider: Optional[str] = None
    model: Optional[str] = None
    raw_finish_reason: Optional[str] = None  # provider's own label, for diagnostics


class ModelError(Exception):
    """A model call failed. ``retryable`` marks transient failures (timeouts, 429, 5xx)."""

    def __init__(self, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.retryable = retryable

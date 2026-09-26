"""ToolRegistry: named tools, argument validation, dispatch, accounting.

Every ``dispatch`` returns a ``ToolResult`` and never raises for tool-level
problems: unknown tools, invalid arguments, ``ToolFailure`` and unexpected
exceptions all become structured errors. Every dispatch counts as one tool
call in the context's metrics, successful or not.
"""

from __future__ import annotations

import dataclasses
import time
from typing import Any, Callable, Iterable, Mapping, Optional

from harness.model.types import ToolCall, ToolDefinition
from harness.tools.base import Tool, ToolContext, ToolError, ToolFailure, ToolResult

Redactor = Callable[[str], str]

_JSON_TYPES: dict[str, Callable[[Any], bool]] = {
    "string": lambda v: isinstance(v, str),
    "integer": lambda v: isinstance(v, int) and not isinstance(v, bool),
    "number": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool),
    "boolean": lambda v: isinstance(v, bool),
    "array": lambda v: isinstance(v, (list, tuple)),
    "object": lambda v: isinstance(v, Mapping),
}


class ToolRegistry:
    def __init__(self, ctx: ToolContext, *, redactor: Optional[Redactor] = None) -> None:
        """``redactor`` (e.g. ``Config.redact``) is applied to every string in
        results and errors, so the registry never needs the secret itself."""
        self.ctx = ctx
        self._redact = redactor or (lambda text: text)
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool) -> None:
        if tool.name in self._tools:
            raise ValueError(f"tool {tool.name!r} is already registered")
        self._tools[tool.name] = tool

    def register_all(self, tools: Iterable[Tool]) -> None:
        for tool in tools:
            self.register(tool)

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(sorted(self._tools))

    def definitions(self, categories: Optional[Iterable[str]] = None) -> tuple[ToolDefinition, ...]:
        """Tool metadata for a model request, optionally limited by category."""
        allowed = set(categories) if categories is not None else None
        return tuple(t.definition() for name, t in sorted(self._tools.items())
                     if allowed is None or t.category in allowed)

    def dispatch(self, name: str, arguments: Optional[Mapping[str, Any]] = None, *,
                 categories: Optional[Iterable[str]] = None) -> ToolResult:
        start = time.monotonic()
        try:
            tool = self._tools.get(name)
            if tool is None:
                raise ToolFailure("unknown_tool", f"no tool named {name!r}; available: {', '.join(self.names)}")
            if categories is not None and tool.category not in set(categories):
                raise ToolFailure("tool_not_allowed", f"tool {name!r} ({tool.category}) is not allowed here")
            args = dict(arguments or {})
            validate_arguments(tool.parameters, args)
            data = tool.handler(self.ctx, **args)
            result = ToolResult(tool=name, success=True, data=_redact(data, self._redact))
        except ToolFailure as failure:
            error = ToolError(failure.code, failure.message, failure.details)
            result = ToolResult(tool=name, success=False, error=_redact(error, self._redact))
        except Exception as exc:  # a bug in a tool must not take the harness down
            error = ToolError("internal_error", f"{exc.__class__.__name__}: {exc}")
            result = ToolResult(tool=name, success=False, error=_redact(error, self._redact))
        result = dataclasses.replace(result, duration_ms=int((time.monotonic() - start) * 1000))
        self.ctx.metrics.record_tool_call(name, success=result.success)
        return result

    def dispatch_call(self, call: ToolCall, *, categories: Optional[Iterable[str]] = None) -> ToolResult:
        """Dispatch a model's ``ToolCall``; malformed arguments become a structured error."""
        if call.parse_error is not None:
            start = time.monotonic()
            error = ToolError("invalid_arguments", f"tool call arguments could not be parsed: {call.parse_error}")
            result = ToolResult(tool=call.name, success=False, error=_redact(error, self._redact),
                                duration_ms=int((time.monotonic() - start) * 1000))
            self.ctx.metrics.record_tool_call(call.name, success=False)
            return result
        return self.dispatch(call.name, call.arguments, categories=categories)


def validate_arguments(schema: Mapping[str, Any], args: Mapping[str, Any]) -> None:
    """Validate against the JSON Schema subset the tools use: object with typed
    properties, ``required``, ``additionalProperties: false``, ``items``, ``minimum``."""
    properties: Mapping[str, Any] = schema.get("properties", {})
    missing = [k for k in schema.get("required", ()) if k not in args]
    if missing:
        raise ToolFailure("invalid_arguments", f"missing required argument(s): {', '.join(missing)}")
    if schema.get("additionalProperties") is False:
        unknown = sorted(set(args) - set(properties))
        if unknown:
            raise ToolFailure("invalid_arguments",
                              f"unknown argument(s): {', '.join(unknown)}; allowed: {', '.join(properties) or 'none'}")
    for key, value in args.items():
        spec = properties.get(key)
        if spec is not None:
            _check_value(key, spec, value)


def _check_value(key: str, spec: Mapping[str, Any], value: Any) -> None:
    types = spec.get("type")
    if types is not None:
        types = [types] if isinstance(types, str) else list(types)
        matched = next((t for t in types if _JSON_TYPES[t](value)), None)
        if matched is None:
            raise ToolFailure("invalid_arguments",
                              f"argument {key!r} must be {' or '.join(types)}, got {type(value).__name__}")
        if matched == "array" and "items" in spec:
            for i, item in enumerate(value):
                _check_value(f"{key}[{i}]", spec["items"], item)
    if "minimum" in spec and value < spec["minimum"]:
        raise ToolFailure("invalid_arguments", f"argument {key!r} must be >= {spec['minimum']}")


def _redact(value: Any, redact: Redactor) -> Any:
    if isinstance(value, str):
        return redact(value)
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        changes = {f.name: _redact(getattr(value, f.name), redact)
                   for f in dataclasses.fields(value) if f.init}
        return dataclasses.replace(value, **changes)
    if isinstance(value, tuple):
        return tuple(_redact(v, redact) for v in value)
    if isinstance(value, list):
        return [_redact(v, redact) for v in value]
    if isinstance(value, Mapping):
        return {k: _redact(v, redact) for k, v in value.items()}
    return value

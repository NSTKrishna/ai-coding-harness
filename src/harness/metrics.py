"""Minimal execution counters shared by the model and tool layers.

Counting rules:

- ``model_calls``: one per ``generate()`` attempt made through
  ``MeteredModelClient``, including attempts that raise.
- ``tool_calls``: one per ``ToolRegistry.dispatch()``, including calls that fail
  validation, are blocked, or error.
- ``command_calls``: one per caller-supplied command actually launched by the
  ``run_command`` / ``run_tests`` tools. Blocked commands are not launched and
  are not counted here. Internal subprocesses (git inspection, ripgrep) are not
  counted here either; they count through their tool call.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


@dataclass
class ExecutionMetrics:
    model_calls: int = 0
    model_failures: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    model_calls_without_usage: int = 0
    tool_calls: int = 0
    tool_failures: int = 0
    tool_calls_by_name: dict[str, int] = field(default_factory=dict)
    command_calls: int = 0

    def record_model_call(
        self,
        *,
        failed: bool = False,
        input_tokens: Optional[int] = None,
        output_tokens: Optional[int] = None,
    ) -> None:
        self.model_calls += 1
        if failed:
            self.model_failures += 1
            return
        if input_tokens is None and output_tokens is None:
            self.model_calls_without_usage += 1
        self.input_tokens += input_tokens or 0
        self.output_tokens += output_tokens or 0

    def record_tool_call(self, name: str, *, success: bool) -> None:
        self.tool_calls += 1
        self.tool_calls_by_name[name] = self.tool_calls_by_name.get(name, 0) + 1
        if not success:
            self.tool_failures += 1

    def record_command(self) -> None:
        self.command_calls += 1

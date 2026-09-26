"""Shared tool types: context, limits, normalized results and failures."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping, Optional

from harness.metrics import ExecutionMetrics
from harness.model.types import ToolDefinition


class ToolFailure(Exception):
    """Raised inside a tool; the registry turns it into a structured ``ToolError``.

    ``code`` is a stable machine-readable identifier (e.g. ``path_outside_repo``).
    """

    def __init__(self, code: str, message: str, details: Optional[Mapping[str, Any]] = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = dict(details or {})


@dataclass(frozen=True)
class ToolError:
    code: str
    message: str
    details: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ToolResult:
    """Outcome of one tool call.

    ``success`` means the tool did its job. For command tools, a command that
    ran and exited non-zero is still a successful *tool* call; the command's
    own outcome is in ``data`` (``CommandResult.ok``).
    """

    tool: str
    success: bool
    data: Any = None
    error: Optional[ToolError] = None
    duration_ms: int = 0


@dataclass(frozen=True)
class ToolLimits:
    max_file_bytes: int = 2_000_000       # files larger than this are not read
    max_read_chars: int = 50_000          # read_file / read_range output cap
    max_list_entries: int = 1_000
    max_search_results: int = 200
    max_match_chars: int = 300            # per search match line
    max_output_bytes: int = 20_000        # per stdout/stderr stream, and git diff text
    command_timeout_seconds: int = 300    # upper bound for run_command / run_tests
    git_timeout_seconds: int = 60
    search_timeout_seconds: int = 60


@dataclass
class ToolContext:
    """Everything a tool needs: the resolved repository root, limits, counters."""

    root: Path
    limits: ToolLimits = field(default_factory=ToolLimits)
    metrics: ExecutionMetrics = field(default_factory=ExecutionMetrics)

    @classmethod
    def create(cls, repo: Path | str, *, limits: Optional[ToolLimits] = None,
               metrics: Optional[ExecutionMetrics] = None) -> "ToolContext":
        root = Path(repo).expanduser().resolve()
        if not root.is_dir():
            raise ValueError(f"repository root is not a directory: {root}")
        return cls(root=root, limits=limits or ToolLimits(), metrics=metrics or ExecutionMetrics())


CATEGORIES = ("read", "write", "exec")


@dataclass(frozen=True)
class Tool:
    """A named operation. ``handler(ctx, **arguments)`` returns the result data
    or raises ``ToolFailure``. ``parameters`` is a JSON Schema object."""

    name: str
    description: str
    parameters: Mapping[str, Any]
    handler: Callable[..., Any]
    category: str

    def __post_init__(self) -> None:
        if self.category not in CATEGORIES:
            raise ValueError(f"tool {self.name!r}: category must be one of {CATEGORIES}")

    def definition(self) -> ToolDefinition:
        return ToolDefinition(name=self.name, description=self.description, parameters=self.parameters)

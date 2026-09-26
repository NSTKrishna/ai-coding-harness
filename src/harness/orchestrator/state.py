"""RunState: the single source of truth for one harness run.

Only observable facts are stored (tool names, argument summaries, outcomes,
bounded result excerpts). Model free text is not stored, apart from the short
``complete``/``blocked`` summary the protocol asks for (capped). Counts of model
and tool calls are read from the shared ``ExecutionMetrics``; RunState keeps no
counters of its own for them.
"""

from __future__ import annotations

import dataclasses
import enum
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Optional

from harness.context.compaction import CompactionState
from harness.metrics import ExecutionMetrics


class Phase(str, enum.Enum):
    INTAKE = "INTAKE"
    DISCOVER = "DISCOVER"
    PLAN = "PLAN"
    BASELINING = "BASELINING"
    EXECUTE = "EXECUTE"
    READY_FOR_VERIFICATION = "READY_FOR_VERIFICATION"
    VERIFYING = "VERIFYING"
    NEEDS_REPAIR = "NEEDS_REPAIR"
    REPAIRING = "REPAIRING"
    # terminals
    VERIFIED = "VERIFIED"
    UNVERIFIED = "UNVERIFIED"
    BLOCKED = "BLOCKED"
    MODEL_ERROR = "MODEL_ERROR"
    TOOL_ERROR = "TOOL_ERROR"
    BUDGET_EXHAUSTED = "BUDGET_EXHAUSTED"
    INTERNAL_ERROR = "INTERNAL_ERROR"


_ABNORMAL = {Phase.BLOCKED, Phase.MODEL_ERROR, Phase.TOOL_ERROR, Phase.BUDGET_EXHAUSTED, Phase.INTERNAL_ERROR}

# Every path to VERIFIED goes through VERIFYING; NEEDS_REPAIR can only lead to REPAIRING
# (or stop), and REPAIRING can only lead back to READY_FOR_VERIFICATION (or stop).
TRANSITIONS: dict[Phase, frozenset[Phase]] = {
    Phase.INTAKE: frozenset({Phase.DISCOVER, Phase.BLOCKED, Phase.INTERNAL_ERROR}),
    Phase.DISCOVER: frozenset({Phase.PLAN, Phase.INTERNAL_ERROR}),
    Phase.PLAN: frozenset({Phase.BASELINING, Phase.MODEL_ERROR, Phase.BUDGET_EXHAUSTED, Phase.INTERNAL_ERROR}),
    Phase.BASELINING: frozenset({Phase.EXECUTE, Phase.BUDGET_EXHAUSTED, Phase.INTERNAL_ERROR}),
    Phase.EXECUTE: frozenset({Phase.READY_FOR_VERIFICATION} | _ABNORMAL),
    Phase.READY_FOR_VERIFICATION: frozenset({Phase.VERIFYING, Phase.INTERNAL_ERROR}),
    Phase.VERIFYING: frozenset({Phase.VERIFIED, Phase.UNVERIFIED, Phase.BLOCKED, Phase.NEEDS_REPAIR,
                                Phase.BUDGET_EXHAUSTED, Phase.INTERNAL_ERROR}),
    Phase.NEEDS_REPAIR: frozenset({Phase.REPAIRING, Phase.BUDGET_EXHAUSTED, Phase.INTERNAL_ERROR}),
    Phase.REPAIRING: frozenset({Phase.READY_FOR_VERIFICATION} | _ABNORMAL),
}
TERMINAL = frozenset({Phase.VERIFIED, Phase.UNVERIFIED} | _ABNORMAL)
EXECUTING = frozenset({Phase.EXECUTE, Phase.REPAIRING})

MAX_SUMMARY_CHARS = 500


class InvalidTransition(Exception):
    pass


@dataclass(frozen=True)
class Failure:
    kind: str          # e.g. "invalid_plan", "invalid_action", "model_call_failed", "budget", "tool_internal_error"
    message: str
    details: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Transition:
    source: Phase
    target: Phase
    reason: str
    at: float          # seconds since run start


@dataclass(frozen=True)
class Observation:
    step: int
    tool: str
    arguments_summary: str
    success: bool                    # the tool did its job (ToolResult.success)
    outcome: str                     # "ok", "tool_error", "command_ok", "command_failed", "command_timed_out"
    result_summary: str              # bounded excerpt
    affected_paths: tuple[str, ...] = ()
    exit_code: Optional[int] = None
    timed_out: Optional[bool] = None
    error_code: Optional[str] = None
    stale: bool = False              # a later patch changed one of affected_paths


@dataclass(frozen=True)
class ActionRecord:
    step: int
    kind: str                        # "tool", "complete" or "blocked"
    tool: Optional[str] = None
    arguments_summary: str = ""
    source: str = ""                 # "native" or "text" for tool actions


@dataclass
class RunState:
    task: str
    repo_root: str
    metrics: ExecutionMetrics = field(default_factory=ExecutionMetrics)
    max_observations: int = 200
    run_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    phase: Phase = Phase.INTAKE
    discovery: Any = None            # repo.discovery.DiscoveryResult
    plan: Any = None                 # orchestrator.plan.TaskPlan
    observations: list[Observation] = field(default_factory=list)
    observations_dropped: int = 0
    action_history: list[ActionRecord] = field(default_factory=list)
    modified_files: list[str] = field(default_factory=list)
    steps: int = 0
    completion_claims: list[tuple[int, str]] = field(default_factory=list)   # (step, summary); never evidence
    # verification (M5)
    verification_commands: tuple = ()      # verify.commands.VerificationCommand
    baseline: Any = None                   # verify.engine.BaselineResult
    verification_reports: list = field(default_factory=list)   # verify.engine.VerificationReport per round
    evidence: Any = None                   # verify.ledger.EvidenceLedger
    changes: Any = None                    # verify.ledger.ChangeLedger
    repair_cycles: int = 0
    repair_context: Any = None             # verify.recovery.RepairContext while REPAIRING
    repeated_failures: int = 0             # consecutive rounds with the same failure fingerprint (M6)
    no_progress: bool = False              # a repair changed nothing and the failure stayed (M6)
    targeting: Any = None                  # verify.targeting.TargetingResult (M6)
    compaction: CompactionState = field(default_factory=CompactionState)   # M6
    event_sink: Optional[Callable[[str, dict], None]] = field(default=None, repr=False)   # telemetry (M6)
    context_snapshot: Any = field(default=None, repr=False)   # debug/test: ContextSnapshot of the last request
    failure: Optional[Failure] = None
    terminal_reason: Optional[str] = None
    transitions: list[Transition] = field(default_factory=list)
    started_at: float = field(default_factory=time.time)
    _clock_start: float = field(default_factory=time.monotonic, repr=False)
    _clock_end: Optional[float] = field(default=None, repr=False)

    # derived views --------------------------------------------------------------
    @property
    def repo_profile(self):
        return self.discovery.repo_profile if self.discovery is not None else None

    @property
    def working_set(self):
        return self.discovery.working_set if self.discovery is not None else None

    @property
    def model_calls(self) -> int:
        return self.metrics.model_calls

    @property
    def tool_calls(self) -> int:
        return self.metrics.tool_calls

    @property
    def elapsed_seconds(self) -> float:
        end = self._clock_end if self._clock_end is not None else time.monotonic()
        return end - self._clock_start

    @property
    def is_terminal(self) -> bool:
        return self.phase in TERMINAL

    @property
    def last_report(self):
        return self.verification_reports[-1] if self.verification_reports else None

    def verification_brief(self) -> str:
        """Baseline results (and the repair context while repairing) for the executor's request."""
        parts = []
        if self.baseline is not None:
            parts.append(self.baseline.render())
        if self.phase == Phase.REPAIRING and self.repair_context is not None:
            parts.append(self.repair_context.render())
        return "\n\n".join(parts)

    # mutation ------------------------------------------------------------------
    def transition(self, target: Phase, reason: str = "", failure: Optional[Failure] = None) -> None:
        allowed = TRANSITIONS.get(self.phase, frozenset())
        if target not in allowed:
            raise InvalidTransition(f"{self.phase.value} -> {target.value} is not allowed")
        self.transitions.append(Transition(self.phase, target, reason, round(self.elapsed_seconds, 3)))
        source, self.phase = self.phase, target
        if failure is not None:
            self.failure = failure
        if target in TERMINAL:
            self.terminal_reason = reason[:MAX_SUMMARY_CHARS]
            self._clock_end = time.monotonic()
        self.emit("phase_changed", source=source.value, target=target.value, reason=reason[:200])

    def emit(self, event: str, **metadata) -> None:
        """Forward a compact event to telemetry, if a recorder is attached."""
        if self.event_sink is not None:
            self.event_sink(event, metadata)

    def add_observation(self, observation: Observation) -> None:
        self.observations.append(observation)
        overflow = len(self.observations) - self.max_observations
        if overflow > 0:
            del self.observations[:overflow]
            self.observations_dropped += overflow

    def record_modified(self, paths) -> None:
        for path in paths:
            if path not in self.modified_files:
                self.modified_files.append(path)

    def invalidate_path(self, path: str, before_step: int) -> int:
        """Mark earlier observations about ``path`` stale. Returns how many were marked."""
        marked = 0
        for i, obs in enumerate(self.observations):
            if obs.step < before_step and path in obs.affected_paths and not obs.stale:
                self.observations[i] = dataclasses.replace(obs, stale=True)
                marked += 1
        return marked

    def recent_observations(self, n: int) -> list[Observation]:
        return self.observations[-n:] if n > 0 else []

    def summary(self) -> "RunSummary":
        return RunSummary(
            run_id=self.run_id,
            terminal_status=self.phase,
            terminal_reason=self.terminal_reason,
            failure=self.failure,
            plan=self.plan,
            modified_files=tuple(self.modified_files),
            model_calls=self.metrics.model_calls,
            tool_calls=self.metrics.tool_calls,
            command_calls=self.metrics.command_calls,
            steps=self.steps,
            recent_observations=tuple(self.recent_observations(5)),
            elapsed_seconds=round(self.elapsed_seconds, 3),
            repair_cycles=self.repair_cycles,
            verification=self.last_report,
        )


@dataclass(frozen=True)
class RunSummary:
    """What a caller needs after a run. The verdict is the terminal status plus the last
    VerificationReport; a completion claim by the model is never part of it."""
    run_id: str
    terminal_status: Phase
    terminal_reason: Optional[str]
    failure: Optional[Failure]
    plan: Any
    modified_files: tuple[str, ...]
    model_calls: int
    tool_calls: int
    command_calls: int
    steps: int
    recent_observations: tuple[Observation, ...]
    elapsed_seconds: float
    repair_cycles: int = 0
    verification: Any = None      # last VerificationReport, if verification ran

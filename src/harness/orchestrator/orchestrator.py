"""Orchestrator: INTAKE -> DISCOVER -> PLAN -> EXECUTE -> READY_FOR_VERIFICATION.

It coordinates and owns no subsystem logic: discovery is M3's
``discover_for_task``, tools are M2's registry, planning and execution are the
Planner and Executor. One ``ToolContext`` (root, limits, metrics) is shared by
discovery, the tools and the metered model, so every count comes from one
``ExecutionMetrics``. No exception escapes ``run``: failures end in a terminal
phase with a structured ``Failure``.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Callable, Optional

from harness.config import ContextLimits, Limits
from harness.context.manager import ContextManager
from harness.model.client import MeteredModelClient, ModelClient
from harness.model.types import ModelError
from harness.orchestrator.executor import Executor, ExecutorSettings
from harness.orchestrator.interpreter import resolve_command
from harness.orchestrator.plan import Planner, PlanError, cap_task
from harness.orchestrator.state import Failure, Phase, RunState
from harness.repo.discovery import discover_for_task
from harness.tools import build_registry
from harness.tools.base import ToolContext, ToolLimits


def tool_limits_from(limits: Limits, base: Optional[ToolLimits] = None) -> ToolLimits:
    """Configuration wins for the command timeout; other tool limits keep their defaults."""
    return replace(base or ToolLimits(), command_timeout_seconds=limits.command_timeout_seconds)


class Orchestrator:
    def __init__(self, model: ModelClient, *, limits: Optional[Limits] = None,
                 context_limits: Optional[ContextLimits] = None, tool_limits: Optional[ToolLimits] = None,
                 executor_settings: Optional[ExecutorSettings] = None,
                 redact: Optional[Callable[[str], str]] = None) -> None:
        self.model = model
        self.limits = limits or Limits()
        self.context_limits = context_limits or ContextLimits()
        self.tool_limits = tool_limits_from(self.limits, tool_limits)
        self.executor_settings = executor_settings or ExecutorSettings()
        self.redact = redact

    def run(self, repo: Path | str, task: str) -> RunState:
        state = RunState(task=task, repo_root=str(repo))
        try:
            self._run(state, repo, task)
        except Exception as exc:  # last line of defence: never leak a traceback to the caller
            if not state.is_terminal:
                message = f"{exc.__class__.__name__}: {exc}"
                message = self.redact(message) if self.redact else message
                state.transition(Phase.INTERNAL_ERROR, f"internal error during {state.phase.value}",
                                 Failure("internal_error", message, {"phase": state.phase.value}))
        return state

    def _run(self, state: RunState, repo, task: str) -> None:
        # INTAKE ---------------------------------------------------------------
        if not task or not task.strip():
            state.transition(Phase.BLOCKED, "task is empty", Failure("invalid_input", "task is empty"))
            return
        try:
            ctx = ToolContext.create(repo, limits=self.tool_limits, metrics=state.metrics)
        except ValueError as exc:
            state.transition(Phase.BLOCKED, str(exc), Failure("invalid_input", str(exc)))
            return
        state.repo_root = str(ctx.root)
        model = MeteredModelClient(self.model, ctx.metrics)
        registry = build_registry(ctx, redactor=self.redact)

        # DISCOVER (deterministic, before any model call) -------------------------
        state.transition(Phase.DISCOVER, "intake accepted")
        discovery = discover_for_task(ctx.root, task, limits=self.context_limits, ctx=ctx)
        state.discovery = discovery
        discovered = (*discovery.repo_profile.test_commands, *discovery.repo_profile.build_commands)
        commands = [resolve_command(c, ctx.root) for c in discovered]
        # The repository summary shows M3's unresolved spelling; accept it as the same command.
        aliases = {" ".join(o.argv): r for o, r in zip(discovered, commands) if o.argv != r.argv}

        # PLAN -------------------------------------------------------------------
        state.transition(Phase.PLAN, f"discovery found {len(discovery.candidates)} candidate file(s)")
        if ctx.metrics.model_calls >= self.limits.max_model_calls:
            state.transition(Phase.BUDGET_EXHAUSTED, "model_calls budget exhausted before planning",
                             Failure("budget", "model_calls budget exhausted",
                                     {"budget": "model_calls", "used": ctx.metrics.model_calls,
                                      "limit": self.limits.max_model_calls}))
            return
        try:
            state.plan = Planner(model).create_plan(task, discovery, commands, aliases)
        except PlanError as exc:
            state.transition(Phase.MODEL_ERROR, f"planning failed: {exc}", Failure("invalid_plan", str(exc)))
            return
        except ModelError as exc:
            message = self.redact(str(exc)) if self.redact else str(exc)
            state.transition(Phase.MODEL_ERROR, f"planner model call failed: {message}",
                             Failure("model_call_failed", message, {"retryable": exc.retryable}))
            return

        # EXECUTE ----------------------------------------------------------------
        state.transition(Phase.EXECUTE, f"plan with {len(state.plan.steps)} step(s)")
        context = ContextManager(cap_task(task), discovery.repo_profile.summary(), self.context_limits)
        context.load_working_set(discovery.working_set)
        Executor(model, registry, context, self.limits, self.executor_settings, redact=self.redact).execute(state)

"""Executor: a bounded DECIDE -> ACT -> OBSERVE -> UPDATE loop.

Each iteration (a "step") makes one model call and carries out at most one
action. Budgets are checked before the operation that would exceed them:

- before a model call: ``steps < max_steps`` and ``model_calls < max_model_calls``;
- before a tool dispatch: ``tool_calls < max_tool_calls``.

Exhausting a budget ends the run in ``BUDGET_EXHAUSTED`` without another model
call. ``complete`` ends it in ``READY_FOR_VERIFICATION``, never in a verified
state; the summary is kept as a completion *claim*, not evidence. The same loop
runs the initial execution (EXECUTE) and every repair (REPAIRING); only the
request context differs. Each request is rebuilt from bounded state.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Callable, Optional

from harness.config import Limits
from harness.context.compaction import ContextRenderer, ContextSnapshot
from harness.context.manager import ContextManager
from harness.model.types import Message, ModelError, ModelRequest
from harness.orchestrator.observe import observe, summarize_arguments
from harness.orchestrator.protocol import ProtocolError, parse_action
from harness.orchestrator.state import EXECUTING, ActionRecord, Failure, Phase, RunState
from harness.tools.registry import ToolRegistry

EXECUTOR_MAX_OUTPUT_TOKENS = 4_000

EXECUTOR_INSTRUCTIONS = """You carry out a planned change in a software repository, one action at a time.
You never touch the repository yourself: every read, search, edit, command or git inspection is a tool
call that the harness runs and reports back to you.
Reply with exactly one JSON object and nothing else, one of:
  {"action": "tool", "tool": "<tool name>", "arguments": {...}}
  {"action": "complete", "summary": "<short description of the changes made>"}
  {"action": "blocked", "reason": "<why the work cannot continue>"}
(If your interface supports native tool calls, you may use one instead of the "tool" form.)
Use "complete" when you believe the changes are done; verification happens afterwards.
Edit files only with apply_patch (unified diff). Re-read a file after editing it before patching it again."""


@dataclass(frozen=True)
class ExecutorSettings:
    observation_window: int = 6          # most recent observations shown in full
    max_observation_chars: int = 4_000   # per observation excerpt
    history_window: int = 20             # one-line action history entries shown
    max_identical_actions: int = 4       # same tool call with the same result N times in a row -> no progress


class Executor:
    def __init__(self, model, registry: ToolRegistry, context: ContextManager, limits: Limits,
                 settings: Optional[ExecutorSettings] = None, redact: Optional[Callable[[str], str]] = None) -> None:
        self.model = model                 # MeteredModelClient sharing the run's metrics
        self.registry = registry
        self.context = context
        self.limits = limits
        self.settings = settings or ExecutorSettings()
        self.redact = redact or (lambda text: text)
        threshold = context.limits.compaction_threshold_chars
        self.renderer = ContextRenderer(context, self.settings, threshold)
        self.last_snapshot: Optional[ContextSnapshot] = None   # debug/test view of the last request

    def execute(self, state: RunState) -> None:
        """Run while ``state.phase`` is EXECUTE or REPAIRING."""
        metrics = state.metrics
        while state.phase in EXECUTING:
            if state.steps >= self.limits.max_steps:
                self._exhausted(state, "steps", state.steps, self.limits.max_steps)
                return
            if metrics.model_calls >= self.limits.max_model_calls:
                self._exhausted(state, "model_calls", metrics.model_calls, self.limits.max_model_calls)
                return

            state.steps += 1
            step = state.steps
            try:
                response = self.model.generate(self.build_request(state))
            except ModelError as exc:
                state.transition(Phase.MODEL_ERROR, f"model call failed at step {step}: {self.redact(str(exc))}",
                                 Failure("model_call_failed", self.redact(str(exc)), {"step": step, "retryable": exc.retryable}))
                return
            except Exception as exc:  # an adapter bug must not escape as a traceback
                state.transition(Phase.MODEL_ERROR, f"model call raised {exc.__class__.__name__} at step {step}",
                                 Failure("model_call_failed", self.redact(f"{exc.__class__.__name__}: {exc}"), {"step": step}))
                return

            try:
                action = parse_action(response, step)
            except ProtocolError as exc:
                state.transition(Phase.MODEL_ERROR, f"invalid action at step {step}: {exc}",
                                 Failure("invalid_action", str(exc), {"step": step}))
                return

            if action.kind == "complete":
                state.action_history.append(ActionRecord(step, "complete"))
                state.completion_claims.append((step, self.redact(action.text)))
                state.transition(Phase.READY_FOR_VERIFICATION, "executor reported completion (a claim, not evidence)")
                return
            if action.kind == "blocked":
                state.action_history.append(ActionRecord(step, "blocked"))
                state.transition(Phase.BLOCKED, self.redact(action.text),
                                 Failure("blocked_by_model", self.redact(action.text), {"step": step}))
                return

            call = action.call
            state.action_history.append(ActionRecord(step, "tool", call.name, self.redact(summarize_arguments(call)), action.source))
            if metrics.tool_calls >= self.limits.max_tool_calls:
                self._exhausted(state, "tool_calls", metrics.tool_calls, self.limits.max_tool_calls)
                return
            result = self.registry.dispatch_call(call)
            if not result.success and result.error.code == "internal_error":
                state.transition(Phase.TOOL_ERROR, f"tool {call.name} failed internally at step {step}",
                                 Failure("tool_internal_error", result.error.message, {"step": step, "tool": call.name}))
                return
            observation = observe(step, call, result, self.settings.max_observation_chars)
            state.add_observation(observation)
            repeats = self._identical_tail(state)
            if repeats >= self.settings.max_identical_actions:
                state.no_progress = True
                state.transition(Phase.BLOCKED, f"no progress: {call.name} returned the same result {repeats} times in a row",
                                 Failure("no_progress", f"{call.name} {observation.arguments_summary} repeated {repeats} "
                                         f"times with an identical result",
                                         {"no_progress": True, "tool": call.name, "repeats": repeats, "step": step}))
                return
            if call.name == "apply_patch" and result.success:
                self._after_patch(state, step, observation.affected_paths)
                if state.changes is not None:
                    label = "execute" if state.phase == Phase.EXECUTE else f"repair-{state.repair_cycles}"
                    state.changes.record_patch(result.data.files, label)

    # ------------------------------------------------------------------------
    @staticmethod
    def _identical_tail(state: RunState) -> int:
        """How many of the latest observations are the same call with the same result."""
        obs = state.observations
        if not obs:
            return 0
        key = (obs[-1].tool, obs[-1].arguments_summary, obs[-1].outcome, obs[-1].result_summary)
        count = 0
        for o in reversed(obs):
            if (o.tool, o.arguments_summary, o.outcome, o.result_summary) != key:
                break
            count += 1
        return count

    def _after_patch(self, state: RunState, step: int, paths) -> None:
        state.record_modified(paths)
        for path in paths:
            state.invalidate_path(path, before_step=step)
            self.context.remove_source(path)

    def _exhausted(self, state: RunState, budget: str, used: int, limit: int) -> None:
        state.transition(Phase.BUDGET_EXHAUSTED, f"{budget} budget exhausted ({used}/{limit})",
                         Failure("budget", f"{budget} budget exhausted", {"budget": budget, "used": used, "limit": limit}))

    def build_request(self, state: RunState) -> ModelRequest:
        """Bounded context: task, plan, repository evidence (minus stale files), verification and
        repair state, short history, the last few observations, remaining budgets and the tool list.
        Compacted deterministically when it exceeds the threshold (``context.compaction``)."""
        m = state.metrics
        tools = self.registry.definitions()
        tool_lines = [f"- {t.name}: {t.description}\n  arguments: {json.dumps(t.parameters['properties'], sort_keys=True)}"
                      for t in tools]
        remaining = (f"steps {self.limits.max_steps - state.steps + 1} (including this one), "
                     f"model calls {self.limits.max_model_calls - m.model_calls}, "
                     f"tool calls {self.limits.max_tool_calls - m.tool_calls}")
        text, snapshot, record = self.renderer.render(
            state, [f"# Remaining budget\n{remaining}", "# Tools\n" + "\n".join(tool_lines)])
        self.last_snapshot = state.context_snapshot = snapshot
        if record is not None:
            state.emit("context_compacted", step=record.step, chars_before=record.chars_before,
                       chars_after=record.chars_after, stages=list(record.stages),
                       observations_dropped=record.observations_dropped, facts_retained=record.facts_retained,
                       reached_limit=record.reached_limit)
        return ModelRequest(
            messages=(Message("system", EXECUTOR_INSTRUCTIONS), Message("user", text)),
            tools=tools,
            max_output_tokens=EXECUTOR_MAX_OUTPUT_TOKENS,
            temperature=0.0,
            purpose="execute",
        )

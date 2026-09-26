"""Plain-text run summary. Reports the terminal phase; it never claims verification."""

from __future__ import annotations

from harness.orchestrator.state import Phase, RunState

_MEANING = {
    Phase.READY_FOR_VERIFICATION: "execution finished; the changes have NOT been verified yet",
    Phase.BLOCKED: "the run stopped: the task could not proceed",
    Phase.MODEL_ERROR: "the run stopped: model call failed or its output broke the protocol",
    Phase.TOOL_ERROR: "the run stopped: a tool failed internally",
    Phase.BUDGET_EXHAUSTED: "the run stopped: a budget was used up",
    Phase.INTERNAL_ERROR: "the run stopped: internal harness error",
}


def format_run(state: RunState) -> str:
    lines = [
        "",
        f"Run {state.run_id}: {state.phase.value}",
        f"  Meaning:        {_MEANING.get(state.phase, state.phase.value)}",
        f"  Reason:         {state.terminal_reason or '-'}",
    ]
    if state.failure is not None:
        lines.append(f"  Failure:        {state.failure.kind}: {state.failure.message}")
    lines += [
        f"  Steps:          {state.steps}",
        f"  Model calls:    {state.model_calls}",
        f"  Tool calls:     {state.tool_calls} ({state.metrics.command_calls} command(s) run)",
        f"  Modified files: {', '.join(state.modified_files) or 'none'}",
        f"  Elapsed:        {state.elapsed_seconds:.1f} s",
    ]
    if state.plan is not None:
        lines.append("  Acceptance criteria:")
        lines += [f"    - {c}" for c in state.plan.acceptance_criteria]
        lines.append("  Verification candidates: " + ("; ".join(" ".join(c.argv) for c in state.plan.verification_candidates)
                                                      or "none"))
    recent = state.recent_observations(5)
    if recent:
        lines.append("  Recent observations:")
        for o in recent:
            lines.append(f"    step {o.step}: {o.tool} -> {'tool ok' if o.success else 'tool error'}, {o.outcome}"
                         + (f", exit_code={o.exit_code}" if o.exit_code is not None else ""))
    return "\n".join(lines) + "\n"

"""Plain-text run result. Success language only for VERIFIED; the model's completion
claim is shown as a claim, never as evidence."""

from __future__ import annotations

from harness.orchestrator.state import Phase, RunState

RULE = "=" * 60

_MEANING = {
    Phase.VERIFIED: "the change is supported by observed verification evidence",
    Phase.UNVERIFIED: "correctness could NOT be established from the available evidence",
    Phase.BLOCKED: "the run stopped: the task or its verification cannot proceed in this environment",
    Phase.MODEL_ERROR: "the run stopped: a model call failed or its output broke the protocol",
    Phase.TOOL_ERROR: "the run stopped: a tool failed internally",
    Phase.BUDGET_EXHAUSTED: "the run stopped: a budget was used up (not verified)",
    Phase.INTERNAL_ERROR: "the run stopped: internal harness error",
}


def _mark(ok: bool) -> str:
    return "[pass]" if ok else "[FAIL]"


def format_run(state: RunState) -> str:
    lines = ["", RULE, f"TASK RESULT: {state.phase.value}", RULE,
             f"Meaning: {_MEANING.get(state.phase, state.phase.value)}",
             f"Reason:  {state.terminal_reason or '-'}"]
    if state.failure is not None and state.phase != Phase.VERIFIED:
        lines.append(f"Failure: {state.failure.kind}: {state.failure.message}")
    lines += ["", "Task: " + (state.task.strip().splitlines() or [""])[0][:120]]

    changed = state.changes.net_changed() if state.changes is not None else tuple(state.modified_files)
    lines.append("Files changed by this run: " + (", ".join(changed) or "none"))

    baseline = state.baseline
    if baseline is not None:
        lines.append("")
        if not baseline.available:
            lines.append("Baseline: BASELINE_NOT_AVAILABLE (no verification command was discovered)")
        else:
            lines.append("Baseline (before any edit):")
            for run in baseline.runs:
                c = run.classification
                lines.append(f"  {_mark(c.status.value == 'PASS')} {run.command.id} {run.command.text}: "
                             f"{c.status.value} [{run.evidence_id}]")

    for report in state.verification_reports:
        lines += ["", f"Verification round {report.round}: {report.verdict.value}"
                  + (f" ({report.failure_class.value})" if report.failure_class else "")]
        for r in report.command_results:
            c = r.post.classification
            lines.append(f"  {_mark(c.status.value == 'PASS')} {r.command.id} {r.command.text}: {c.status.value}, "
                         f"{r.comparison.value} [{r.post.evidence_id}] - {r.note}")
        if report.criteria_results:
            lines.append("  Acceptance criteria:")
            for crit in report.criteria_results:
                lines.append(f"    [{crit.status}] {crit.criterion}  (evidence: {', '.join(crit.evidence_ids)}; {crit.notes})")
        regressions = [r for r in report.command_results if r.finding is not None]
        lines.append("  Regression/failure checks: " + ("; ".join(f"{r.command.id} {r.finding.value}" for r in regressions)
                                                     or "no new failures detected"))
        for risk in report.risks:
            lines.append(f"  Risk: {risk}")

    if state.completion_claims:
        lines += ["", f"Executor completion claim (not evidence): {state.completion_claims[-1][1]}"]
    lines += [
        "",
        "Resources:",
        f"  Model calls:   {state.model_calls}",
        f"  Tool calls:    {state.tool_calls} ({state.metrics.command_calls} command(s) run)",
        f"  Steps:         {state.steps}",
        f"  Repair cycles: {state.repair_cycles}",
        f"  Evidence:      {len(state.evidence.items) if state.evidence is not None else 0} item(s)",
        f"  Elapsed:       {state.elapsed_seconds:.1f} s",
        RULE,
    ]
    return "\n".join(lines) + "\n"

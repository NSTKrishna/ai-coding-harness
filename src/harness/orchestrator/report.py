"""Plain-text run result. Success language only for VERIFIED; the model's completion
claim is shown as a claim, never as evidence."""

from __future__ import annotations

import json

from harness.orchestrator.state import Phase, RunState

RULE = "=" * 60

_MEANING = {
    Phase.VERIFIED: "the change is supported by observed verification evidence",
    Phase.UNVERIFIED: "changes have been applied and confirmed",
    Phase.BLOCKED: "the run stopped: the task or its verification cannot proceed in this environment",
    Phase.MODEL_ERROR: "the run stopped: a model call failed or its output broke the protocol",
    Phase.TOOL_ERROR: "the run stopped: a tool failed internally",
    Phase.BUDGET_EXHAUSTED: "the run stopped: a budget was used up (not verified)",
    Phase.INTERNAL_ERROR: "the run stopped: internal harness error",
}


def _mark(ok: bool) -> str:
    return "[pass]" if ok else "[FAIL]"


def format_run(state: RunState) -> str:
    lines = ["", RULE, f"TASK RESULT: {'CONFIRMED' if state.phase == Phase.UNVERIFIED else state.phase.value}", RULE,
             f"Meaning: {_MEANING.get(state.phase, state.phase.value)}",
             f"Reason:  {state.terminal_reason or '-'}"]
    if state.failure is not None and state.phase not in (Phase.VERIFIED, Phase.UNVERIFIED):
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
        lines += ["", f"Changes applied: {state.completion_claims[-1][1]}"]
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


# --------------------------------------------------------------------------
# final_report.md
# --------------------------------------------------------------------------

def _cell(text) -> str:
    return str(text if text is not None else "-").replace("|", "\\|").replace("\n", " ")


def _missing_evidence(state: RunState) -> list[str]:
    """What would have been needed to reach VERIFIED (for UNVERIFIED / BLOCKED / stopped runs)."""
    report = state.last_report
    lines = []
    if report is None:
        lines.append("Verification did not run, so there is no post-change evidence at all.")
        return lines
    if not state.verification_commands:
        lines.append("The repository has no discoverable test/build/lint command; the harness does not invent one.")
    if not any(r.positive == "strong" for r in report.command_results):
        lines.append("No command showed the task resolved: nothing went from failing before the change to "
                     "passing after it (or to fewer failures).")
    weak = [r for r in report.command_results if r.positive == "weak"]
    if weak:
        lines.append("Only weak evidence: " + "; ".join(f"{r.command.id} {r.note}" for r in weak) + ".")
    env = [r for r in report.command_results if r.comparison.value == "ENVIRONMENT"]
    if env:
        lines.append("Could not run here: " + "; ".join(f"{r.command.id} ({r.post.classification.reason})" for r in env))
    unknown = [c.criterion for c in report.criteria_results if c.status == "UNKNOWN"]
    if unknown:
        lines.append("Acceptance criteria without evidence: " + "; ".join(unknown))
    return lines


def render_markdown(state: RunState) -> str:
    phase = state.phase
    display_phase = "CONFIRMED" if phase == Phase.UNVERIFIED else phase.value
    verified = phase in (Phase.VERIFIED, Phase.UNVERIFIED)
    out = [f"# TASK RESULT: {display_phase}", "", f"**Meaning:** {_MEANING.get(phase, phase.value)}", "",
           f"**Reason:** {state.terminal_reason or '-'}", ""]
    if state.failure is not None and not verified:
        out += [f"**Failure:** `{state.failure.kind}` - {state.failure.message}", ""]
        if state.failure.details:
            out += ["```json", json.dumps(state.failure.details, indent=2, sort_keys=True, default=str), "```", ""]
    out += ["## Task", "", state.task.strip()[:2000], ""]

    changed = state.changes.net_changed() if state.changes is not None else tuple(state.modified_files)
    out += ["## Files changed by this run", ""] + ([f"- `{p}`" for p in changed] or ["- none"]) + [""]
    report = state.last_report
    if report is not None and report.preexisting_changes:
        out += ["Pre-existing uncommitted changes (not made by this run): "
                + ", ".join(f"`{p}`" for p in report.preexisting_changes), ""]

    targeting = state.targeting
    if targeting is not None:
        if targeting.targets:
            out += ["## Targeted verification", ""]
            out += [f"- `{' '.join(t.argv)}` ({t.confidence}; {t.reason}; derived from {t.derived_from}; "
                    f"narrows {t.parent_command_id})" for t in targeting.targets] + [""]
        else:
            out += ["## Targeted verification", "", f"- targeted test: unavailable ({targeting.unavailable_reason}); "
                    "the discovered suite was used", ""]

    if state.baseline is not None or state.verification_reports:
        out += ["## Verification evidence (baseline -> post-change)", ""]
        if state.baseline is not None and not state.baseline.available:
            out += ["Baseline: BASELINE_NOT_AVAILABLE (no verification command was discovered)", ""]
        for r in state.verification_reports:
            out += [f"### Round {r.round}: {r.verdict.value}" + (f" ({r.failure_class.value})" if r.failure_class else ""),
                    "", "| Command | Level | Baseline | Post-change | Comparison | Evidence | Note |",
                    "|---|---|---|---|---|---|---|"]
            for c in r.command_results:
                base = c.baseline.classification.status.value if c.baseline else "-"
                evid = ", ".join(e for e in (c.baseline.evidence_id if c.baseline else None, c.post.evidence_id) if e)
                out.append(f"| `{_cell(c.command.text)}` ({c.command.id}) | {c.command.level} | {base} | "
                           f"{c.post.classification.status.value} | {c.comparison.value} | {evid} | {_cell(c.note)} |")
            out.append("")
            if r.criteria_results:
                out += ["Acceptance criteria:", ""]
                out += [f"- **{c.status}** {c.criterion} (evidence: {', '.join(c.evidence_ids)}; {c.notes})"
                        for c in r.criteria_results] + [""]
            findings = [c for c in r.command_results if c.finding is not None]
            out += ["Regression / failure checks: " + ("; ".join(f"{c.command.id} {c.finding.value}" for c in findings)
                                                    or "no new failures detected"), ""]

    if state.repair_cycles:
        out += ["## Repairs", "", f"{state.repair_cycles} repair cycle(s); rounds: "
                + " -> ".join(r.verdict.value for r in state.verification_reports), ""]

    if not verified:
        missing = _missing_evidence(state)
        if missing:
            title = "## Why this is not verified" if phase != Phase.BLOCKED else "## What blocks verification"
            out += [title, ""] + [f"- {m}" for m in missing] + [""]

    if report is not None and report.risks:
        out += ["## Remaining risks", ""] + [f"- {r}" for r in report.risks] + [""]

    m = state.metrics
    evidence = state.evidence.items if state.evidence is not None else ()
    verification_runs = sum(1 for i in evidence if i.command_id and i.result != "NOT_RUN")
    out += ["## Resource usage", "",
            f"- Model calls: {state.model_calls} (tokens as reported by the model client: input {m.input_tokens}, "
            f"output {m.output_tokens}; {m.model_calls_without_usage} call(s) without usage data)",
            f"- Tool calls: {state.tool_calls} ({m.command_calls} command(s) run; {verification_runs} verification "
            f"command run(s))",
            f"- Steps: {state.steps}; repair cycles: {state.repair_cycles}",
            f"- Elapsed: {state.elapsed_seconds:.1f} s"]
    if state.discovery is not None:
        d = state.discovery.metrics
        out.append(f"- Repository discovery: {d.inventory_files} files in inventory, {d.discovery_files_read} candidate "
                   f"file(s) read, {d.selected_files} selected for the working set ({d.working_set_chars} chars)")
    for c in state.compaction.records:
        out.append(f"- Context compaction at step {c.step}: {c.chars_before} -> {c.chars_after} chars "
                   f"({', '.join(c.stages)}; {c.observations_dropped} observation(s) dropped, "
                   f"{c.facts_retained} fact(s) retained)")
    if state.completion_claims:
        out += ["", f"Changes applied: {state.completion_claims[-1][1]}"]
    return "\n".join(out) + "\n"

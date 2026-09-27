"""The final-result panel for the interactive (TTY) renderer.

Read-only consumer of ``RunState``, exactly like ``orchestrator.report``
(which the non-TTY/plain path keeps using verbatim, unchanged) — this module
adds a prettier presentation, not a second source of truth. Success wording
is used only for VERIFIED; every other terminal phase gets neutral or
cautious language.
"""

from __future__ import annotations

from typing import Optional, Sequence

from harness.orchestrator.state import Phase, RunState
from harness.ui.format import format_duration
from harness.ui.panel import panel, wrapped
from harness.ui.theme import Theme

_TITLE = {
    Phase.VERIFIED: ("success", "VERIFIED", "green"),
    Phase.UNVERIFIED: ("success", "CONFIRMED", "green"),
    Phase.BLOCKED: ("failure", "BLOCKED", "red"),
    Phase.BUDGET_EXHAUSTED: ("warning", "BUDGET EXHAUSTED", "yellow"),
    Phase.MODEL_ERROR: ("failure", "MODEL ERROR", "red"),
    Phase.TOOL_ERROR: ("failure", "TOOL ERROR", "red"),
    Phase.INTERNAL_ERROR: ("failure", "INTERNAL ERROR", "red"),
}

_LEAD = {
    Phase.BLOCKED: "No repair was attempted because this looks like an environment "
                   "issue, not a code issue.",
    Phase.BUDGET_EXHAUSTED: "The run stopped before verification could complete.",
}
# BLOCKED has several causes and they need different explanations: blaming the environment
# for a model that simply stopped editing sends the reader to the wrong place entirely.
_BLOCKED_LEAD = {
    "no_progress": "The run stopped making progress, so it was ended early rather than "
                   "spending the rest of its budget.",
    "blocked_by_model": "The model reported it could not continue.",
}


def _lead(state: RunState) -> Optional[str]:
    if state.phase == Phase.BLOCKED and state.failure is not None:
        return _BLOCKED_LEAD.get(state.failure.kind, _LEAD[Phase.BLOCKED])
    return _LEAD.get(state.phase)

Sections = Optional[Sequence[tuple[str, Sequence[str]]]]


def render_panel(state: RunState, *, theme: Theme, report_path=None, extra_sections: Sections = None,
                 elapsed: Optional[float] = None) -> str:
    kind, label, color = _TITLE.get(state.phase, ("warning", state.phase.value, "yellow"))
    width = theme.panel_width
    inner = width - 4
    rows: list = [[(f"{theme.symbol(kind)} {label}", color)], [("", None)]]

    if state.phase in (Phase.VERIFIED, Phase.UNVERIFIED) and state.completion_claims:
        rows += wrapped(state.completion_claims[-1][1], inner)
    else:
        intro = []
        lead = _lead(state)
        if lead is not None:
            intro.append(lead)
        if state.failure is not None and state.phase != Phase.UNVERIFIED and state.failure.message not in intro:
            intro.append(state.failure.message)
        for text in intro:
            rows += wrapped(text, inner)
        if state.completion_claims:
            rows += [[("", None)]]
            rows += wrapped(state.completion_claims[-1][1], inner)

    def section(title: str) -> None:
        rows.append([("", None)])
        rows.append([(title, "bold")])

    changed = state.changes.net_changed() if state.changes is not None else tuple(state.modified_files)
    section("Changed")
    rows += [[("  " + path, None)] for path in changed] or [[("  no files changed", "dim")]]

    if state.baseline is not None or state.verification_reports:
        section("Verification")
        if state.baseline is not None and not state.baseline.available:
            rows.append([("  " + theme.symbol("warning") + " no test command discovered", "yellow")])
        elif state.baseline is not None:
            for run in state.baseline.runs:
                status = run.classification.status.value
                rows.append([("  before  ", "dim"), (f"{run.command.id} ", None),
                             (status, "green" if status == "PASS" else "red")])
        for report in state.verification_reports:
            for r in report.command_results:
                status = r.post.classification.status.value
                rows.append([(f"  round {report.round} ", "dim"), (f"{r.command.id} ", None),
                             (status, "green" if status == "PASS" else "red"), (f"  {r.comparison.value}", "dim")])

    for title, lines in extra_sections or ():
        section(title)
        rows += [[("  " + line, None)] for line in lines]

    section("Resources")
    stats = [f"{state.model_calls} model calls", f"{state.tool_calls} tool calls"]
    if state.repair_cycles:
        stats.append(f"{state.repair_cycles} repair cycle(s)")
    stats.append(format_duration(elapsed if elapsed is not None else state.elapsed_seconds))
    m = state.metrics
    if m.input_tokens or m.output_tokens:
        stats.append(f"{m.input_tokens:,} in / {m.output_tokens:,} out tokens")
    rows.append([("  " + f" {theme.bullet} ".join(stats), "dim")])

    out = panel(theme, rows, border=color, width=width)
    if report_path is not None:
        out += ["", " " + theme.paint("Report ", "dim") + str(report_path)]
    return "\n".join(out) + "\n"


def render(state: RunState, *, theme: Theme, report_path=None) -> str:
    return render_panel(state, theme=theme, report_path=report_path)


def cancelled(state: Optional[RunState], run_dir=None, *, theme: Theme) -> str:
    """Graceful ``Ctrl-C`` message. ``state``/``run_dir`` come from whatever the
    recorder/observer captured before the interrupt (may be partial or None)."""
    lines = [" " + theme.paint(f"{theme.symbol('warning')} Run cancelled", "yellow"), ""]
    changed = ()
    if state is not None:
        changed = state.changes.net_changed() if state.changes is not None else tuple(state.modified_files)
    if changed:
        lines.append("  Modified files:")
        lines += [f"    {path}" for path in changed]
        lines += ["", "  Changes were left in the working tree."]
    else:
        lines.append("  No changes were committed.")
    if state is not None:
        lines.append(theme.paint(f"  Run id: {state.run_id}", "dim"))
    if run_dir is not None:
        lines.append(theme.paint(f"  Run artifacts: {run_dir}", "dim"))
    return "\n".join(lines) + "\n"

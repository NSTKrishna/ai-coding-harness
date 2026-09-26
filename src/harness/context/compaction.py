"""Deterministic compaction of the executor's request. No model call is ever made.

The request is rendered from bounded state. When it exceeds
``ContextLimits.compaction_threshold_chars``, stages are applied in this order,
re-rendering after each, until it fits:

1. stale file contents (a later patch changed the file) are removed;
2. repeated identical observations are shown once;
3. older command outputs keep only a short tail;
4. observations older than the most recent ``KEEP_RECENT`` become one-line
   ``HistoryFact``s in episodic memory (and are no longer rendered in full);
5. the action-history window shrinks;
6. discovery evidence snippets (low-value by now) are dropped, lowest priority first;
7. the oldest history facts are dropped.

Never dropped: the task, the plan and its acceptance criteria, the verification
and repair section (baseline, current failure, diff, prior attempts), current
file contents re-read for repair, and protected facts (verification, repair
attempts, failures, changes, decisions). Stages 4 and 6–7 change stored context
and therefore persist; the rendering switches of stages 1–3 and 5 stay on for the
rest of the run once compaction has started.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Optional

from harness.context.facts import HistoryFact

KEEP_RECENT = 2                 # observations always shown in full
TRIMMED_EXCERPT = 400           # chars kept of older command outputs
COMPACT_HISTORY_WINDOW = 5


@dataclass(frozen=True)
class CompactionRecord:
    step: int
    chars_before: int
    chars_after: int
    threshold: int
    reached_limit: bool
    stages: tuple[str, ...]
    stale_sources_removed: int
    duplicates_removed: int
    excerpts_trimmed: int
    observations_compacted: int
    evidence_items_dropped: int
    history_facts_dropped: int
    facts_retained: int

    @property
    def observations_dropped(self) -> int:
        return self.stale_sources_removed + self.duplicates_removed + self.observations_compacted


@dataclass
class CompactionState:
    """Lives on RunState; records what has been compacted so later steps stay compact."""
    active: bool = False
    compacted_steps: set[int] = field(default_factory=set)
    drop_stale: bool = False
    dedupe: bool = False
    trim_commands: bool = False
    history_window: Optional[int] = None
    records: list[CompactionRecord] = field(default_factory=list)


@dataclass(frozen=True)
class ContextSnapshot:
    """Structured view of what the model receives (debug/test only; no reasoning)."""
    chars: int
    task: str
    acceptance_criteria: tuple[str, ...]
    plan_steps: tuple[str, ...]
    verification: str
    working_items: tuple[tuple[str, str], ...]           # (kind, source)
    facts: tuple[tuple[str, str], ...]                   # (kind, text)
    observations: tuple[tuple[int, str, str], ...]       # (step, tool, how it is shown)
    action_history: tuple[str, ...]


def _digest(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:12]


class ContextRenderer:
    def __init__(self, context, settings, threshold: int) -> None:
        self.context = context          # ContextManager
        self.settings = settings        # ExecutorSettings
        self.threshold = threshold

    # rendering ------------------------------------------------------------------
    def _observations(self, state, cs: CompactionState, counts: dict) -> tuple[list[str], list[tuple[int, str, str]]]:
        window = [o for o in state.recent_observations(self.settings.observation_window)
                  if o.step not in cs.compacted_steps]
        latest_command = max((o.step for o in window if o.exit_code is not None or o.timed_out), default=None)
        seen_later: set[str] = set()
        shown: list[tuple] = []
        for o in reversed(window):
            key = _digest(f"{o.tool}|{o.arguments_summary}|{o.outcome}|{o.result_summary}")
            if cs.dedupe and key in seen_later:
                shown.append((o, "duplicate"))
                counts["duplicates"] += 1
                continue
            seen_later.add(key)
            shown.append((o, "full"))
        shown.reverse()

        lines, view = [], []
        for o, how in shown:
            header = (f"### step {o.step}: {o.tool} {o.arguments_summary} -> "
                      f"{'tool ok' if o.success else 'tool error'}, {o.outcome}"
                      + (f", exit_code={o.exit_code}" if o.exit_code is not None else ""))
            if how == "duplicate":
                view.append((o.step, o.tool, "duplicate-removed"))
                continue
            if o.stale:
                if cs.drop_stale:
                    lines.append(f"{header} [stale content removed: {', '.join(o.affected_paths)} changed later]")
                    view.append((o.step, o.tool, "stale-removed"))
                    counts["stale"] += 1
                    continue
                lines += [header, f"[stale: {', '.join(o.affected_paths)} changed after this step; read it again]"]
                view.append((o.step, o.tool, "stale-marker"))
                continue
            body = o.result_summary
            if cs.trim_commands and o.exit_code is not None and o.step != latest_command and len(body) > TRIMMED_EXCERPT:
                body = "[... older output trimmed ...]\n" + body[-TRIMMED_EXCERPT:]
                counts["trimmed"] += 1
                view.append((o.step, o.tool, "trimmed"))
            else:
                view.append((o.step, o.tool, "full"))
            lines += [header, body]
        for step in sorted(cs.compacted_steps):
            view.append((step, next((o.tool for o in state.observations if o.step == step), "?"), "compacted-to-fact"))
        return lines, view

    def _render(self, state, cs: CompactionState, tail_sections: list[str]) -> tuple[str, ContextSnapshot, dict]:
        counts = {"duplicates": 0, "stale": 0, "trimmed": 0}
        window = cs.history_window or self.settings.history_window
        history = state.action_history[-window:]
        history_lines = [f"step {a.step}: {a.kind}" + (f" {a.tool} {a.arguments_summary}" if a.tool else "")
                         for a in history]
        obs_lines, obs_view = self._observations(state, cs, counts)
        verification = state.verification_brief() or "no baseline information"
        sections = [
            self.context.render().rstrip("\n"),
            "# Plan\n" + (state.plan.render() if state.plan is not None else "none"),
            "# Verification\n" + verification,
            "# Files modified so far\n" + (", ".join(state.modified_files) or "none"),
            "# Action history\n" + ("\n".join(history_lines) or "none yet")
            + (f"\n(older steps: see facts)" if len(state.action_history) > window else ""),
            "# Recent observations\n" + ("\n".join(obs_lines) or "none yet"),
            *tail_sections,
        ]
        text = "\n\n".join(sections)
        plan = state.plan
        snapshot = ContextSnapshot(
            chars=len(text),
            task=self.context.permanent.task,
            acceptance_criteria=tuple(plan.acceptance_criteria) if plan is not None else (),
            plan_steps=tuple(s.description for s in plan.steps) if plan is not None else (),
            verification=verification,
            working_items=tuple((i.kind, i.source) for i in self.context.working_items),
            facts=tuple((f.kind, f.text) for f in self.context.facts()),
            observations=tuple(obs_view),
            action_history=tuple(history_lines),
        )
        return text, snapshot, counts

    # compaction -------------------------------------------------------------------
    def render(self, state, tail_sections: list[str]) -> tuple[str, ContextSnapshot, Optional[CompactionRecord]]:
        cs: CompactionState = state.compaction
        text, snap, counts = self._render(state, cs, tail_sections)
        if len(text) <= self.threshold:
            return text, snap, None

        before = len(text)
        stages: list[str] = []
        compacted = evidence_dropped = history_dropped = 0

        def fits() -> bool:
            nonlocal text, snap, counts
            text, snap, counts = self._render(state, cs, tail_sections)
            return len(text) <= self.threshold

        cs.active = True
        steps = [
            ("drop_stale", lambda: setattr(cs, "drop_stale", True)),
            ("dedupe", lambda: setattr(cs, "dedupe", True)),
            ("trim_command_output", lambda: setattr(cs, "trim_commands", True)),
        ]
        done = False
        for name, apply in steps:
            apply()
            stages.append(name)
            if fits():
                done = True
                break
        if not done:
            stages.append("observations_to_facts")
            visible = [o for o in state.observations if o.step not in cs.compacted_steps]
            for o in visible[:-KEEP_RECENT] if len(visible) > KEEP_RECENT else []:
                note = "stale" if o.stale else (o.error_code or "")
                self.context.record(HistoryFact(o.step, o.tool, o.arguments_summary[:120], o.outcome, note))
                cs.compacted_steps.add(o.step)
                compacted += 1
            done = fits()
        if not done:
            stages.append("shrink_action_history")
            cs.history_window = COMPACT_HISTORY_WINDOW
            done = fits()
        while not done and any(i.kind == "evidence" for i in self.context.working_items):
            if "drop_discovery_evidence" not in stages:
                stages.append("drop_discovery_evidence")
            remaining = sum(1 for i in self.context.working_items if i.kind == "evidence")
            evidence_dropped += self.context.drop_working("evidence", keep=remaining - 1)
            done = fits()
        while not done and any(f.kind == "history" for f in self.context.facts()):
            if "drop_history_facts" not in stages:
                stages.append("drop_history_facts")
            remaining = sum(1 for f in self.context.facts() if f.kind == "history")
            history_dropped += self.context.drop_facts("history", keep_last=remaining // 2)
            done = fits()

        record = CompactionRecord(
            step=state.steps, chars_before=before, chars_after=len(text), threshold=self.threshold,
            reached_limit=len(text) <= self.threshold, stages=tuple(stages),
            stale_sources_removed=counts["stale"], duplicates_removed=counts["duplicates"],
            excerpts_trimmed=counts["trimmed"], observations_compacted=compacted,
            evidence_items_dropped=evidence_dropped, history_facts_dropped=history_dropped,
            facts_retained=len(self.context.facts()),
        )
        cs.records.append(record)
        return text, snap, record

"""Renderers turn the telemetry event stream (see ``telemetry.RunRecorder``/
``LiveObserver``) into terminal output. They consume events and ``RunState``
only — never a source of agent state, never imported by the orchestrator.

``PlainRenderer`` is deterministic and append-only (no ANSI, no animation):
used whenever stdout is not a TTY, ``TERM=dumb``, or ``--no-interactive`` is
given. Its final screen reuses ``orchestrator.report.format_run`` verbatim —
already a solid, tested, deterministic report; not reinvented here.

``InteractiveRenderer`` (TTY only) prints completed work once into scrollback,
grouped per phase, and redraws only a small bottom region (spinner,
state-machine breadcrumb, footer) in place — history stays scrollable.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Any, Mapping, Optional

from harness.ui.format import COMMAND_TOOLS, GIT_TOOLS, format_duration, tool_headline, tool_parts
from harness.ui.theme import CLEAR_TO_END, Theme, cursor_up, truncate

_MILESTONES = [
    ("DISCOVER", "Discover"),
    ("PLAN", "Plan"),
    ("BASELINING", "Baseline"),
    ("EXECUTE", "Execute"),
    ("VERIFYING", "Verify"),
]
_REPAIR_PHASES = {"NEEDS_REPAIR", "REPAIRING"}
_TERMINAL_OK = {"VERIFIED"}
_TERMINAL_BAD = {"BLOCKED", "MODEL_ERROR", "TOOL_ERROR", "BUDGET_EXHAUSTED", "INTERNAL_ERROR"}


class PhaseTracker:
    """Tracks the state-machine breadcrumb from ``phase_changed`` events (source/target
    strings only — no dependency on the ``Phase`` enum, so this stays a pure string
    translator of whatever telemetry already emits)."""

    def __init__(self) -> None:
        self.status = {label: "pending" for _, label in _MILESTONES}
        self.repair_seen = False
        self.repair_status = "pending"
        self._current: Optional[str] = None

    def apply(self, source: str, target: str) -> None:
        if self._current == "Repair":
            self.repair_status = "success"
        elif self._current in self.status:
            self.status[self._current] = "success"

        if target in _REPAIR_PHASES:
            self.repair_seen = True
            self.repair_status = "active"
            self._current = "Repair"
            return
        for phase_name, label in _MILESTONES:
            if target == phase_name:
                self.status[label] = "active"
                self._current = label
                return
        if target in _TERMINAL_BAD:
            if self._current == "Repair":
                self.repair_status = "failure"
            elif self._current in self.status:
                self.status[self._current] = "failure"
            self._current = None
        elif target in _TERMINAL_OK:
            self._current = None

    def breadcrumb(self, theme: Theme) -> str:
        parts = []
        for phase_name, label in _MILESTONES:
            parts.append(f"{theme.symbol(self.status[label])} {label}")
            if label == "Execute" and self.repair_seen:
                parts.append(f"{theme.symbol(self.repair_status)} Repair")
        return f" {theme.step_sep} ".join(parts)


class EventFeed:
    """Turns one telemetry event into zero or more human-readable lines.

    Stateful only to correlate a tool/model call's ``*_started`` event with its
    ``*_completed``/``*_failed`` event, which is safe because the executor issues
    one call at a time (never concurrent).
    """

    def __init__(self, theme: Theme) -> None:
        self.theme = theme
        self._tool: Optional[tuple[str, str]] = None
        self._model: Optional[str] = None

    def lines_for(self, name: str, metadata: Mapping[str, Any], phase: str) -> list[str]:
        t = self.theme
        if name == "discovery_completed":
            return [f"{t.symbol('success')} Discovering repository",
                    f"    {metadata.get('inventory_files', 0)} files {t.bullet} inspected "
                    f"{metadata.get('files_read', 0)} {t.bullet} selected {metadata.get('selected_files', 0)}"]
        if name == "baseline_completed":
            available = metadata.get("available")
            commands = metadata.get("commands") or []
            sym = t.symbol("success" if available else "warning")
            detail = "; ".join(str(c) for c in commands) if commands else "no verification command discovered"
            return [f"{sym} Baseline", f"    {detail}"]
        if name == "model_call_started":
            label = self._model_label(metadata.get("purpose"), phase)
            self._model = label
            return [f"{t.symbol('active')} {label}…"]
        if name == "model_call_completed":
            label = self._model or "Model call"
            self._model = None
            return [f"{t.symbol('success')} {label} done"]
        if name == "model_call_failed":
            label = self._model or "Model call"
            self._model = None
            return [f"{t.symbol('failure')} {label} failed: {metadata.get('error', 'error')}"]
        if name == "tool_call_started":
            tool = metadata.get("tool", "")
            headline = tool_headline(tool, metadata.get("arguments", ""))
            self._tool = (tool, headline)
            return [f"{t.symbol('active')} {headline}"]
        if name == "tool_call_completed":
            tool = metadata.get("tool", "")
            headline = self._tool[1] if self._tool and self._tool[0] == tool else tool_headline(tool, "")
            self._tool = None
            ok = bool(metadata.get("success"))
            detail = metadata.get("detail") or ""
            sym = t.symbol("success" if ok else "failure")
            line = f"{sym} {headline}"
            if detail:
                line += f"   {detail}"
            return [line]
        if name == "verification_completed":
            verdict = metadata.get("verdict", "")
            display = "CONFIRMED" if verdict == "UNVERIFIED" else verdict
            kind = "success" if verdict in ("VERIFIED", "UNVERIFIED") else "warning" if verdict == "NEEDS_REPAIR" else "failure"
            return [f"{t.symbol(kind)} Verification round {metadata.get('round')}: {display}"]
        if name == "repair_started":
            return [f"{t.symbol('active')} Repairing {t.bullet} cycle {metadata.get('cycle')} "
                    f"({metadata.get('failure_class', '')})"]
        if name == "context_compacted":
            return [f"{t.symbol('info')} Context compacted ({metadata.get('chars_before')} "
                    f"{t.step_sep} {metadata.get('chars_after')} chars)"]
        return []

    @staticmethod
    def _model_label(purpose: Optional[str], phase: str) -> str:
        if purpose == "plan":
            return "Planning"
        if phase == "REPAIRING":
            return "Repairing"
        return "Deciding next action"


class Renderer:
    lock: "threading.RLock"

    def handle(self, name: str, metadata: Mapping[str, Any], phase: str) -> Optional[str]:
        raise NotImplementedError

    def finish(self, state, report_path=None, extra_sections=None) -> str:
        raise NotImplementedError

    def tick(self) -> str:
        return ""

    def note(self, symbol_kind: str, text: str) -> str:
        raise NotImplementedError

    def cancelled(self, state, run_dir=None) -> str:
        raise NotImplementedError


class PlainRenderer(Renderer):
    """Deterministic, append-only. Safe for non-TTY stdout, CI, and tests."""

    def __init__(self, theme: Theme) -> None:
        self.theme = theme
        self.feed = EventFeed(theme)
        self.lock = threading.RLock()

    def handle(self, name: str, metadata: Mapping[str, Any], phase: str) -> Optional[str]:
        lines = self.feed.lines_for(name, metadata, phase)
        return ("\n".join(lines) + "\n") if lines else None

    def finish(self, state, report_path=None, extra_sections=None) -> str:
        from harness.orchestrator.report import format_run
        text = format_run(state)
        for title, lines in extra_sections or ():
            text += f"{title}: " + "; ".join(lines) + "\n"
        if report_path is not None:
            text += f"Run artifacts: {report_path}\n"
        return text

    def note(self, symbol_kind: str, text: str) -> str:
        return f"{self.theme.symbol(symbol_kind)} {text}\n"

    def cancelled(self, state, run_dir=None) -> str:
        from harness.ui.final_screen import cancelled
        return cancelled(state, run_dir, theme=self.theme)


@dataclass(frozen=True)
class RunContext:
    """Static facts for the live footer (never agent state)."""
    repo_name: str = ""
    branch: str = ""
    model: str = ""


_SECTION_FOR = {"DISCOVER": "Discover", "PLAN": "Plan", "BASELINING": "Baseline", "EXECUTE": "Execute",
                "VERIFYING": "Verify", "NEEDS_REPAIR": "Repair", "REPAIRING": "Repair"}
_TERMINALS = _TERMINAL_OK | _TERMINAL_BAD
_HARNESS_PHASES = {"BASELINING", "VERIFYING", "READY_FOR_VERIFICATION"}


def _k(n: Optional[int]) -> str:
    if n is None:
        return "?"
    return f"{n / 1000:.1f}k" if n >= 1000 else str(n)


class InteractiveRenderer(Renderer):
    """TTY-only live view (Gemini-CLI style).

    Completed work is printed once into scrollback, grouped in one rail-framed
    section per phase; only a small bottom region (spinner + breadcrumb + footer)
    is redrawn in place with cursor-up + clear-to-end, so history stays scrollable
    and copyable. ``tick()`` advances the spinner; the CLI calls it from a timer
    thread while holding ``lock``.
    """

    def __init__(self, theme: Theme, context: Optional[RunContext] = None) -> None:
        self.theme = theme
        self.context = context or RunContext()
        self.phases = PhaseTracker()
        self.lock = threading.RLock()
        self._printed_height = 0
        self._section: Optional[str] = None
        self._verify_rounds = 0
        self._activity = "Starting…"
        self._activity_since = time.monotonic()
        self._frame = 0
        self._tool: Optional[tuple[str, str, str]] = None      # (tool, verb, target)
        self._model_label: Optional[str] = None
        self._model_calls = 0
        self._tool_calls = 0
        self._tokens_in = 0
        self._tokens_out = 0
        self._tokens_seen = False
        self._started = time.monotonic()

    # -- public ------------------------------------------------------------------
    def handle(self, name: str, metadata: Mapping[str, Any], phase: str) -> Optional[str]:
        with self.lock:
            history = self._history(name, metadata, phase)
            text = self._clear()
            if history:
                text += "\n".join(history) + "\n"
            return text + self._region()

    def tick(self) -> str:
        with self.lock:
            self._frame += 1
            return self._clear() + self._region()

    def note(self, symbol_kind: str, text: str) -> str:
        """A one-off history line outside the event stream (e.g. branch created)."""
        with self.lock:
            line = " " + self.theme.paint(self.theme.symbol(symbol_kind), _PAINT_FOR.get(symbol_kind, "dim")) + " " + text
            return self._clear() + line + "\n" + self._region()

    def finish(self, state, report_path=None, extra_sections=None) -> str:
        from harness.ui.final_screen import render_panel
        with self.lock:
            text = self._clear() + self._close_section()
            return text + "\n" + render_panel(state, theme=self.theme, report_path=report_path,
                                               extra_sections=extra_sections, elapsed=time.monotonic() - self._started)

    def cancelled(self, state, run_dir=None) -> str:
        from harness.ui.final_screen import cancelled
        with self.lock:
            return self._clear() + self._close_section() + "\n" + cancelled(state, run_dir, theme=self.theme)

    # -- history -----------------------------------------------------------------
    def _rail(self, body: str) -> str:
        return " " + self.theme.paint(self.theme.box("v"), "dim") + " " + body

    def _open_section(self, title: str) -> list[str]:
        t, h = self.theme, self.theme.box("h")
        width = t.panel_width
        lines = [] if self._section is None else [self._close_section().rstrip("\n")]
        self._section = title
        label = f" {title} "
        return [l for l in lines if l] + [" " + t.paint(t.box("tl") + h, "dim") + t.paint(label, "bold")
                                          + t.paint(h * max(width - 4 - len(label), 0), "dim")]

    def _close_section(self) -> str:
        if self._section is None:
            return ""
        self._section = None
        t = self.theme
        return " " + t.paint(t.box("bl") + t.box("h") * (t.panel_width - 2), "dim") + "\n"

    def _line(self, kind: str, body: str, detail: str = "") -> str:
        t = self.theme
        room = t.panel_width - 6
        body = truncate(body, max(room - (len(detail) + 2 if detail else 0), 10))
        pad = max(room - len(body) - len(detail), 2) if detail else 0
        return self._rail(t.paint(t.symbol(kind), _PAINT_FOR.get(kind, "dim")) + " " + body
                          + (" " * pad + t.paint(detail, "dim") if detail else ""))

    def _history(self, name: str, m: Mapping[str, Any], phase: str) -> list[str]:
        t = self.theme
        out: list[str] = []
        if name == "phase_changed":
            target = str(m.get("target", ""))
            self.phases.apply(str(m.get("source", "")), target)
            section = _SECTION_FOR.get(target)
            if section == "Verify":
                self._verify_rounds += 1
                section = "Verify" if self._verify_rounds == 1 else f"Verify · round {self._verify_rounds}"
            if section and section != self._section:
                out += self._open_section(section)
                self._activity = {"Discover": "Indexing repository", "Plan": "Planning", "Baseline":
                                  "Running baseline checks", "Execute": "Working", "Repair": "Repairing"}.get(
                                      section.split(" ")[0], "Verifying")
                self._activity_since = time.monotonic()
            elif target in _TERMINALS:
                closing = self._close_section()
                if closing:
                    out.append(closing.rstrip("\n"))
            return out
        if name == "discovery_completed":
            out.append(self._line("success", f"{m.get('inventory_files', 0):,} files indexed "
                                  f"{t.bullet} {m.get('files_read', 0)} read {t.bullet} "
                                  f"{m.get('selected_files', 0)} selected for context"))
        elif name == "baseline_completed":
            commands = [str(c) for c in (m.get("commands") or [])]
            if not m.get("available") or not commands:
                out.append(self._line("warning", "no test command discovered \u2014 changes cannot be proven by tests"))
            else:
                results = [c.partition("=") for c in commands]
                passing = all(status == "PASS" for _, _, status in results)
                summary = f" {t.bullet} ".join(f"{cid} {status}" for cid, _, status in results)
                out.append(self._line("info", "before any edit: " + summary,
                                      "all passing" if passing else "failing now"))
        elif name == "model_call_started":
            self._model_label = EventFeed._model_label(m.get("purpose"), phase)
            self._activity, self._activity_since = self._model_label, time.monotonic()
        elif name == "model_call_completed":
            self._model_calls += 1
            tin, tout = m.get("input_tokens"), m.get("output_tokens")
            if tin is not None or tout is not None:
                self._tokens_seen = True
                self._tokens_in += tin or 0
                self._tokens_out += tout or 0
            if m.get("purpose") == "plan":
                dur = f"{(m.get('duration_ms') or 0) / 1000:.1f}s"
                usage = f" {t.bullet} {_k(tin)} in / {_k(tout)} out" if tin is not None else ""
                out.append(self._line("info", "plan ready", dur + usage))
            self._activity, self._activity_since = "Working", time.monotonic()
        elif name == "model_call_failed":
            self._model_calls += 1
            out.append(self._line("failure", f"model call failed: {m.get('error', 'error')}"))
        elif name == "tool_call_started":
            tool = str(m.get("tool", ""))
            verb, target = tool_parts(tool, str(m.get("arguments", "")))
            self._tool = (tool, verb, target)
            self._activity = f"{verb} {target}".strip()
            self._activity_since = time.monotonic()
        elif name == "tool_call_completed":
            self._tool_calls += 1
            tool = str(m.get("tool", ""))
            verb, target = (self._tool[1], self._tool[2]) if self._tool and self._tool[0] == tool \
                else tool_parts(tool, "")
            self._tool = None
            self._activity, self._activity_since = "Thinking", time.monotonic()
            if tool in GIT_TOOLS and phase in _HARNESS_PHASES:
                return out                     # the harness's own diff snapshots, not the agent's work
            ok = bool(m.get("success"))
            if ok and tool in COMMAND_TOOLS and m.get("exit_code") is not None:
                ok = m.get("exit_code") == 0 and not m.get("timed_out")
            detail = str(m.get("detail") or m.get("error_code") or "")
            dur = m.get("duration_ms")
            if dur and dur >= 1000:
                detail = (detail + "  " if detail else "") + f"{dur / 1000:.1f}s"
            out.append(self._line("success" if ok else "failure", f"{verb:<7}{target}", detail))
        elif name == "verification_completed":
            verdict = str(m.get("verdict", ""))
            display = "CONFIRMED" if verdict == "UNVERIFIED" else verdict
            kind = "success" if verdict in ("VERIFIED", "UNVERIFIED") else "warning" if verdict in ("NEEDS_REPAIR",) \
                else "failure"
            out.append(self._line(kind, f"verdict: {display}"))
            summary = str(m.get("summary") or "")
            if summary and verdict not in ("VERIFIED", "UNVERIFIED"):
                out.append(self._rail("  " + t.paint(truncate(summary, t.panel_width - 10), "dim")))
        elif name == "repair_started":
            out.append(self._line("active", f"repair cycle {m.get('cycle')}", str(m.get("failure_class", ""))))
        elif name == "context_compacted":
            out.append(self._line("info", "context compacted",
                                  f"{m.get('chars_before')} {t.step_sep} {m.get('chars_after')} chars"))
        return out

    # -- live region -------------------------------------------------------------
    def _clear(self) -> str:
        text = cursor_up(self._printed_height) + "\r" + CLEAR_TO_END if self._printed_height else "\r" + CLEAR_TO_END
        self._printed_height = 0
        return text

    def _region(self) -> str:
        t = self.theme
        spin = t.spinner[self._frame % len(t.spinner)]
        waited = int(time.monotonic() - self._activity_since)
        activity = truncate(self._activity, t.panel_width - 30)
        line1 = (" " + t.paint(spin, "accent") + " " + t.paint(activity + "…" if t.unicode else activity + "...",
                                                                  "bold")
                 + t.paint(f" ({waited}s · ctrl-c to cancel)" if t.unicode else f" ({waited}s, ctrl-c to cancel)",
                           "dim"))
        line2 = "   " + self.phases.breadcrumb(t)
        parts = [p for p in (self.context.repo_name,
                             f"({self.context.branch})" if self.context.branch else "") if p]
        left = " ".join(parts)
        stats = [self.context.model, f"{self._model_calls} model", f"{self._tool_calls} tools"]
        if self._tokens_seen:
            stats.append(f"{_k(self._tokens_in)} in/{_k(self._tokens_out)} out")
        stats.append(format_duration(time.monotonic() - self._started))
        right = f" {t.bullet} ".join(s for s in stats if s)
        line3 = "   " + t.paint(truncate(left + ("   " if left else "") + right, t.width - 4), "dim")
        self._printed_height = 3
        return "\n".join((line1, line2, line3)) + "\n"


_PAINT_FOR = {"success": "green", "failure": "red", "warning": "yellow", "active": "accent",
              "info": "accent2", "pending": "dim", "skipped": "dim"}


def build_renderer(*, interactive: bool, theme: Theme, context: Optional[RunContext] = None) -> Renderer:
    return InteractiveRenderer(theme, context) if interactive else PlainRenderer(theme)

"""RecoveryController: from a NEEDS_REPAIR report to a bounded repair context.

The repair itself is run by the ordinary M4 Executor (same ToolRegistry, same
action protocol); only the context differs. Limits are checked before anything
is spent:

- ``repair_cycles < max_repair_cycles`` (0 = verify but never repair);
- a model call must still be available;
- fresh reads of changed files are ordinary tool calls within ``max_tool_calls``.

Changed files are re-read through ``read_file`` and put into working context,
replacing any stale evidence, so the model repairs the current content.

Repeated-failure stop rule (M6): after a repair, if verification reports the same
failure signature (class + failing command + failing test ids, or the output hash
when no ids are printed) as the previous round, ``repeated_failures`` grows. The
loop stops, without another model call, when the repair changed no file
(``no_progress``) or when ``repeated_failures`` reaches
``max_repeated_failure_cycles``. A different signature resets the count.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from harness.context.facts import ChangeFact, DecisionFact, FailureFact, RepairAttemptFact, VerificationFact
from harness.verify.engine import Verdict, VerificationReport
from harness.verify.outcomes import REPAIRABLE

MAX_FRESH_FILES = 3
MAX_OUTPUT_EXCERPT = 1_500
MAX_DIFF_EXCERPT = 2_000
MAX_FACTS_SHOWN = 10
FRESH_PRIORITY = 1e9       # current file contents outrank discovery evidence in working context


@dataclass(frozen=True)
class RepairContext:
    cycle: int
    max_cycles: int
    verdict_summary: str
    failure_class: str
    failing_command: Optional[str]
    baseline_status: Optional[str]
    post_status: Optional[str]
    failure_output: str
    failed_criteria: tuple[str, ...]
    modified_files: tuple[str, ...]
    fresh_files: tuple[str, ...]
    diff_excerpt: str
    prior_attempts: tuple[str, ...]
    repeated_failure: bool

    def render(self) -> str:
        lines = [f"# Repair (cycle {self.cycle} of at most {self.max_cycles})",
                 "Verification of your previous changes FAILED. Fix the problem below, then reply complete.",
                 "Verification will run again automatically; 'complete' is not evidence.",
                 f"Failure: {self.failure_class}: {self.verdict_summary}"]
        if self.failing_command:
            lines.append(f"Failing command: {self.failing_command} (before change: {self.baseline_status}, "
                         f"after change: {self.post_status})")
        if self.failure_output:
            lines += ["Output (tail):", self.failure_output]
        if self.failed_criteria:
            lines += ["Acceptance criteria failing:"] + [f"- {c}" for c in self.failed_criteria]
        lines.append("Files changed by this run: " + (", ".join(self.modified_files) or "none"))
        if self.fresh_files:
            lines.append("Current contents re-read for: " + ", ".join(self.fresh_files)
                         + " (see working context; earlier reads are stale)")
        if self.diff_excerpt:
            lines += ["Current diff (excerpt):", self.diff_excerpt]
        if self.prior_attempts:
            lines += ["Previous verification/repair history:"] + [f"- {a}" for a in self.prior_attempts]
        if self.repeated_failure:
            lines.append("WARNING: the last repair left exactly the same failure. Do not repeat it; try a different fix.")
        return "\n".join(lines)


class RepairLimit(Exception):
    def __init__(self, budget: str, used: int, limit: int) -> None:
        super().__init__(f"{budget} budget exhausted ({used}/{limit})")
        self.budget, self.used, self.limit = budget, used, limit


class RecoveryController:
    def __init__(self, registry, state, limits, context) -> None:
        self.registry = registry
        self.state = state
        self.limits = limits
        self.context = context   # the run's ContextManager

    def record_verification(self, report: VerificationReport) -> None:
        """Typed episodic facts for every verification round (observed outcomes only)."""
        failing = report.primary_failure
        fp = _failure_text(report)
        self.context.record(VerificationFact(
            report.round, self.state.repair_cycles, report.verdict.value,
            report.failure_class.value if report.failure_class else None,
            failing.command.id if failing else None, fp, tuple(report.changed_by_run)))
        if self.state.repair_cycles and report.round > 1:
            self.context.record(RepairAttemptFact(
                self.state.repair_cycles, _previous_class(self.state), "", tuple(report.changed_by_run),
                f"verification round {report.round}: {report.verdict.value}"
                + (f" {report.failure_class.value}" if report.failure_class else "")))
        if report.verdict == Verdict.NEEDS_REPAIR:
            self.context.record(FailureFact(fp or report.summary[:120], report.failure_class.value,
                                            failing.command.id if failing else None, report.round))
        for record in self.state.changes.records if self.state.changes is not None else ():
            if record.before_sha256 != record.current_sha256:
                self.context.record(ChangeFact(record.path, tuple(record.touched_in), record.patches))

    def check_progress(self, report: VerificationReport) -> Optional[dict]:
        """After a repair: returns stop details if the same failure keeps recurring."""
        reports = self.state.verification_reports
        if report.verdict != Verdict.NEEDS_REPAIR or len(reports) < 2 or reports[-2].verdict != Verdict.NEEDS_REPAIR:
            self.state.repeated_failures = 0
            return None
        previous = reports[-2]
        signature = _signature(report)
        if signature != _signature(previous):
            self.state.repeated_failures = 0
            return None
        self.state.repeated_failures += 1
        unchanged = dict(previous.change_state) == dict(report.change_state)
        if unchanged:
            self.state.no_progress = True
        if not unchanged and self.state.repeated_failures < self.limits.max_repeated_failure_cycles:
            return None
        why = ("the last repair changed no file and the failure is identical" if unchanged else
               f"the same failure followed {self.state.repeated_failures} consecutive repair(s)")
        details = {"no_progress": unchanged, "repeated_failures": self.state.repeated_failures,
                   "limit": self.limits.max_repeated_failure_cycles, "signature": signature,
                   "repair_cycles": self.state.repair_cycles}
        self.context.record(DecisionFact("stop repair loop", why))
        return {"reason": f"repeated_failure_no_progress: {why}", "details": details}

    def prepare(self, report: VerificationReport) -> RepairContext:
        """Raises ``RepairLimit`` before spending anything if a repair may not start."""
        if report.failure_class not in REPAIRABLE:
            raise ValueError(f"{report.failure_class} is not repairable by code")
        if self.state.repair_cycles >= self.limits.max_repair_cycles:
            raise RepairLimit("repair_cycles", self.state.repair_cycles, self.limits.max_repair_cycles)
        m = self.state.metrics
        if m.model_calls >= self.limits.max_model_calls:
            raise RepairLimit("model_calls", m.model_calls, self.limits.max_model_calls)

        self.state.repair_cycles += 1
        cycle = self.state.repair_cycles
        fresh = []
        for path in report.changed_by_run[:MAX_FRESH_FILES]:
            if m.tool_calls >= self.limits.max_tool_calls:
                raise RepairLimit("tool_calls", m.tool_calls, self.limits.max_tool_calls)
            result = self.registry.dispatch("read_file", {"path": path})
            self.context.remove_source(path)
            if result.success:
                self.context.add_working_item("current", path, result.data.content, priority=FRESH_PRIORITY)
                fresh.append(path)

        failing = report.primary_failure
        failed_criteria = tuple(c.criterion for c in report.criteria_results if c.status == "FAIL")
        previous = self.state.verification_reports[-2] if len(self.state.verification_reports) >= 2 else None
        repeated = bool(previous and previous.failure_class == report.failure_class
                        and _fingerprint(previous) == _fingerprint(report) and _fingerprint(report))
        self.context.record(RepairAttemptFact(cycle, report.failure_class.value, _failure_text(report),
                                              tuple(report.changed_by_run), "started"))
        facts = [f"[{f.kind}] {f.text}" for f in self.context.facts()
                 if f.kind in ("verification", "repair_attempt", "failure", "decision")]
        diff_text = ""
        if report.diff_evidence_id:
            diff_text = self.state.evidence.get(report.diff_evidence_id).excerpt[:MAX_DIFF_EXCERPT]
        return RepairContext(
            cycle=cycle, max_cycles=self.limits.max_repair_cycles,
            verdict_summary=report.summary, failure_class=report.failure_class.value,
            failing_command=failing.command.text if failing else None,
            baseline_status=failing.baseline.classification.status.value if failing and failing.baseline else None,
            post_status=failing.post.classification.status.value if failing else None,
            failure_output=(failing.post.classification.excerpt[-MAX_OUTPUT_EXCERPT:] if failing else ""),
            failed_criteria=failed_criteria, modified_files=tuple(report.changed_by_run), fresh_files=tuple(fresh),
            diff_excerpt=diff_text, prior_attempts=tuple(facts[-MAX_FACTS_SHOWN:]), repeated_failure=repeated,
        )


def _failure_text(report: VerificationReport) -> str:
    failing = report.primary_failure
    if failing is None or failing.post.classification.fingerprint is None:
        return ""
    f = failing.post.classification.fingerprint
    return ",".join(sorted(f.failing_tests)) or f"output {f.output_hash}"


def _signature(report: VerificationReport) -> str:
    """Failure class + failing command + failing test ids (or output hash); criteria when no command."""
    failing = report.primary_failure
    base = report.failure_class.value if report.failure_class else ""
    if failing is not None:
        return f"{base}|{failing.command.id}|{_failure_text(report)}"
    return base + "|" + ",".join(sorted(c.criterion for c in report.criteria_results if c.status == "FAIL"))


def _previous_class(state) -> str:
    reports = state.verification_reports
    prior = reports[-2] if len(reports) >= 2 else None
    return prior.failure_class.value if prior is not None and prior.failure_class else ""


def _fingerprint(report: VerificationReport) -> str:
    failing = report.primary_failure
    if failing is None or failing.post.classification.fingerprint is None:
        return ""
    f = failing.post.classification.fingerprint
    return failing.command.id + ":" + (",".join(sorted(f.failing_tests)) or f.output_hash)

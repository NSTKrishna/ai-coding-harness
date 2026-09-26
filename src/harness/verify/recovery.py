"""RecoveryController: from a NEEDS_REPAIR report to a bounded repair context.

The repair itself is run by the ordinary M4 Executor (same ToolRegistry, same
action protocol); only the context differs. Limits are checked before anything
is spent:

- ``repair_cycles < max_repair_cycles`` (0 = verify but never repair);
- a model call must still be available;
- fresh reads of changed files are ordinary tool calls within ``max_tool_calls``.

Changed files are re-read through ``read_file`` and put into working context,
replacing any stale evidence, so the model repairs the current content.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from harness.verify.engine import VerificationReport
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
        """Episodic fact for every verification round (no reasoning, only observed outcome)."""
        failing = report.primary_failure
        fp = ""
        if failing is not None and failing.post.classification.fingerprint is not None:
            f = failing.post.classification.fingerprint
            fp = ",".join(sorted(f.failing_tests)) or f.output_hash
        self.context.record_fact(
            "verification",
            f"round {report.round} after repair cycle {self.state.repair_cycles}: {report.verdict.value}"
            + (f" {report.failure_class.value}" if report.failure_class else "")
            + (f" [{failing.command.id} fingerprint {fp}]" if failing else "")
            + f"; changed: {', '.join(report.changed_by_run) or 'none'}")

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
        self.context.record_fact(
            "repair_attempt",
            f"cycle {cycle} started for {report.failure_class.value}"
            + (f" ({failing.command.id} {failing.command.text})" if failing else "")
            + f"; files so far: {', '.join(report.changed_by_run) or 'none'}")
        facts = [f"[{f.kind}] {f.text}" for f in self.context.facts() if f.kind in ("verification", "repair_attempt")]
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


def _fingerprint(report: VerificationReport) -> str:
    failing = report.primary_failure
    if failing is None or failing.post.classification.fingerprint is None:
        return ""
    f = failing.post.classification.fingerprint
    return failing.command.id + ":" + (",".join(sorted(f.failing_tests)) or f.output_hash)

"""VerificationEngine: baseline before editing, then evidence-based verdicts.

Every command and git inspection is a normal ``ToolRegistry.dispatch`` and counts
toward ``max_tool_calls``; if the budget runs out mid-verification,
``VerificationBudgetExhausted`` is raised and the run cannot be VERIFIED.

Verdict policy (documented in arch.md §14):

1. NEEDS_REPAIR if any repairable failure was found (regression, task test still
   failing, new build/lint/typecheck failure, new timeout, a criterion known to
   FAIL, or no change made although the plan has edit steps).
2. VERIFIED if none of the above and there is positive evidence:
   - strong: a command went fail -> pass, fail -> fewer failures (no new ones), or
     passes with more tests than at baseline;
   - weak (accepted, reported as a risk): a test command the plan selected passes
     before and after, and this run changed files.
3. BLOCKED if verification commands exist but none could run for environment
   reasons (verification is impossible here).
4. UNVERIFIED otherwise (no command, only unrelated/pre-existing results, only
   commands that passed both times without being selected, …).
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from harness.repo.signals import extract_task_signals, normalize_name
from harness.tools.paths import resolve_in_repo
from harness.tools.base import ToolFailure
from harness.verify.commands import VerificationCommand
from harness.verify.ledger import ChangeLedger, EvidenceLedger
from harness.verify.outcomes import (
    CHECK_FAILURE_CLASS,
    REPAIR_PRIORITY,
    Classification,
    CommandStatus,
    Comparison,
    FailureClass,
    classify,
    compare,
)

MAX_DIFF_EXCERPT = 3_000
MAX_SNAPSHOT_EXCERPT = 1_000


def _evidence_order(evidence_id: str) -> int:
    return int(evidence_id[1:])


class Verdict(str, enum.Enum):
    VERIFIED = "VERIFIED"
    NEEDS_REPAIR = "NEEDS_REPAIR"
    UNVERIFIED = "UNVERIFIED"
    BLOCKED = "BLOCKED"


class VerificationBudgetExhausted(Exception):
    def __init__(self, used: int, limit: int) -> None:
        super().__init__(f"tool_calls budget exhausted during verification ({used}/{limit})")
        self.used, self.limit = used, limit


@dataclass(frozen=True)
class RepoSnapshot:
    label: str
    available: bool                                   # False: not a git repository (no tool calls made)
    status: tuple[tuple[str, str], ...] = ()          # (path, XY)
    diffstat: tuple[tuple[str, Optional[int], Optional[int]], ...] = ()   # (path, added, deleted)
    diff_excerpt: str = ""
    evidence_id: Optional[str] = None

    @property
    def paths(self) -> frozenset[str]:
        return frozenset(p for p, _ in self.status)

    def state_of(self) -> dict[str, tuple]:
        stats = {p: (a, d) for p, a, d in self.diffstat}
        return {p: (xy, stats.get(p)) for p, xy in self.status}


@dataclass(frozen=True)
class CommandRun:
    command: VerificationCommand
    phase: str
    classification: Classification
    evidence_id: str


@dataclass(frozen=True)
class CommandVerification:
    command: VerificationCommand
    baseline: Optional[CommandRun]
    post: CommandRun
    comparison: Comparison
    finding: Optional[FailureClass]   # repairable problem found by this command, if any
    positive: str                     # "strong", "weak" or ""
    note: str


@dataclass(frozen=True)
class CriterionResult:
    criterion: str
    status: str                       # "PASS", "FAIL" or "UNKNOWN"
    evidence_ids: tuple[str, ...]
    notes: str


@dataclass(frozen=True)
class BaselineResult:
    available: bool                   # False: BASELINE_NOT_AVAILABLE (no command to run)
    runs: tuple[CommandRun, ...]
    not_run: tuple[str, ...]          # command ids over the per-round cap
    initial: RepoSnapshot
    after_baseline: RepoSnapshot
    evidence_id: Optional[str] = None

    def run_for(self, command_id: str) -> Optional[CommandRun]:
        return next((r for r in self.runs if r.command.id == command_id), None)

    def render(self) -> str:
        if not self.available:
            return "Baseline: not available (no verification command was discovered)."
        lines = ["Baseline (before any edit):"]
        for r in self.runs:
            c = r.classification
            lines.append(f"- {r.command.id} [{r.command.purpose}] {r.command.text}: {c.status.value} ({c.reason})")
            if c.status != CommandStatus.PASS and c.excerpt:
                lines.append("  output tail: " + c.excerpt[-600:].replace("\n", "\n  "))
        return "\n".join(lines)


@dataclass(frozen=True)
class VerificationReport:
    round: int
    verdict: Verdict
    failure_class: Optional[FailureClass]
    summary: str
    command_results: tuple[CommandVerification, ...]
    criteria_results: tuple[CriterionResult, ...]
    changed_by_run: tuple[str, ...]
    preexisting_changes: tuple[str, ...]
    changed_outside_patches: tuple[str, ...]
    diff_evidence_id: Optional[str]
    risks: tuple[str, ...]

    @property
    def primary_failure(self) -> Optional[CommandVerification]:
        return next((c for c in self.command_results if c.finding == self.failure_class), None)


class VerificationEngine:
    def __init__(self, registry, state, limits, ledger: EvidenceLedger, changes: ChangeLedger) -> None:
        self.registry = registry
        self.state = state
        self.limits = limits
        self.ledger = ledger
        self.changes = changes
        self.root = Path(state.repo_root)

    # tool access ---------------------------------------------------------------
    def _dispatch(self, name: str, arguments: dict):
        m = self.state.metrics
        if m.tool_calls >= self.limits.max_tool_calls:
            raise VerificationBudgetExhausted(m.tool_calls, self.limits.max_tool_calls)
        return self.registry.dispatch(name, arguments)

    def snapshot(self, label: str, with_diff: bool) -> RepoSnapshot:
        profile = self.state.repo_profile
        if profile is None or not profile.is_git:
            item = self.ledger.record_snapshot(label, "not a git repository: no git snapshot", "", "UNAVAILABLE")
            return RepoSnapshot(label, False, evidence_id=item.id)
        status = self._dispatch("git_status", {})
        stat = self._dispatch("git_diff_stat", {})
        diff = self._dispatch("git_diff", {}) if with_diff else None
        if not status.success or not stat.success:
            error = (status.error or stat.error).message
            item = self.ledger.record_snapshot(label, f"git snapshot failed: {error}", "", "FAILED")
            return RepoSnapshot(label, False, evidence_id=item.id)
        entries = tuple((e.path, f"{e.index}{e.worktree}") for e in status.data.entries)
        stats = tuple((f.path, f.added, f.deleted) for f in stat.data.files)
        diff_text = diff.data.text if diff is not None and diff.success else ""
        excerpt = "\n".join(f"{xy} {p}" for p, xy in entries)[:MAX_SNAPSHOT_EXCERPT] or "clean"
        item = self.ledger.record_snapshot(label, f"{len(entries)} changed/untracked path(s); {stat.data.summary}",
                                           excerpt, "CLEAN" if not entries else "DIRTY")
        return RepoSnapshot(label, True, entries, stats, diff_text[:MAX_DIFF_EXCERPT], item.id)

    def _run_commands(self, phase: str) -> tuple[tuple[CommandRun, ...], tuple[str, ...]]:
        runs, not_run = [], []
        for command in self.state.verification_commands:
            if not command.within_cap:
                self.ledger.record_not_run(phase, command.id, command.kind, command.text,
                                           f"over max_verification_commands ({self.limits.max_verification_commands})")
                not_run.append(command.id)
                continue
            tool = "run_tests" if command.kind == "test" else "run_command"
            result = self._dispatch(tool, {"command": list(command.argv)})
            classification = classify(command.kind, command.argv, result)
            item = self.ledger.record_command(phase, command.id, command.kind, command.text, classification)
            runs.append(CommandRun(command, phase, classification, item.id))
        return tuple(runs), tuple(not_run)

    # baseline -------------------------------------------------------------------
    def baseline(self) -> BaselineResult:
        initial = self.snapshot("initial", with_diff=True)
        if not self.state.verification_commands:
            item = self.ledger.record_baseline_unavailable("no verification command was discovered")
            return BaselineResult(False, (), (), initial, initial, item.id)
        runs, not_run = self._run_commands("baseline")
        after = self.snapshot("after-baseline", with_diff=False) if initial.available else initial
        return BaselineResult(True, runs, not_run, initial, after)

    # verification ---------------------------------------------------------------
    def verify(self, round_no: int) -> VerificationReport:
        phase = f"post-{round_no}"
        baseline: BaselineResult = self.state.baseline
        runs, not_run = self._run_commands(phase)
        final = self.snapshot(phase, with_diff=True)

        results = [self._assess_command(baseline.run_for(r.command.id) if baseline else None, r)
                   for r in runs]
        risks = [f"{cid} not run: over max_verification_commands" for cid in not_run]

        changed = self.changes.net_changed()
        change_items = []
        for record in self.changes.records:
            if record.path in changed:
                item = self.ledger.record_file_change(
                    phase, record.path, f"changed by this run in {', '.join(record.touched_in)} "
                    f"({record.patches} patch(es)); sha256 {str(record.before_sha256)[:12]} -> {str(record.current_sha256)[:12]}")
                change_items.append(item.id)
        diff_id = None
        if final.available:
            diff_id = self.ledger.record_diff(phase, f"working-tree diff at verification round {round_no}",
                                              final.diff_excerpt).id
        preexisting = tuple(sorted(baseline.initial.paths)) if baseline else ()
        outside = self._changed_outside_patches(baseline, final)
        if preexisting:
            touched = sorted(set(preexisting) & set(changed))
            risks.append("pre-existing uncommitted changes (not made by this run): " + ", ".join(preexisting)
                         + (f"; this run also patched {', '.join(touched)}" if touched else ""))
        if outside:
            risks.append("changed during the run but not by patches (e.g. by commands): " + ", ".join(outside))

        criteria = self._assess_criteria(phase, results, change_items)
        findings = [r.finding for r in results if r.finding is not None]
        plan = self.state.plan
        expects_edit = plan is not None and any(s.kind == "edit" for s in plan.steps)
        if expects_edit and not changed:
            findings.append(FailureClass.DIFF_PROBLEM)
        if any(c.status == "FAIL" for c in criteria) and FailureClass.TASK_TEST_FAILURE not in findings:
            findings.append(FailureClass.TASK_TEST_FAILURE)

        for r in results:
            if r.note and r.finding is None and r.comparison in (Comparison.UNCHANGED_FAILURE, Comparison.IMPROVED,
                                                                 Comparison.ENVIRONMENT, Comparison.NOT_COMPARABLE,
                                                                 Comparison.NO_BASELINE):
                risks.append(f"{r.command.id} {r.command.text}: {r.note}")
            if r.positive == "weak":
                risks.append(f"{r.command.id} passed before and after the change; it may not exercise the change")

        verdict, failure_class, summary = self._decide(results, findings, changed)
        return VerificationReport(round_no, verdict, failure_class, summary, tuple(results), tuple(criteria),
                                  tuple(changed), preexisting, outside, diff_id, tuple(risks))

    def _assess_command(self, base: Optional[CommandRun], post: CommandRun) -> CommandVerification:
        cmd = post.command
        comparison = compare(base.classification if base else None, post.classification)
        finding, positive, note = None, "", ""
        failure_kind = FailureClass.TASK_TEST_FAILURE if cmd.kind == "test" else CHECK_FAILURE_CLASS.get(
            cmd.kind, FailureClass.BUILD_FAILURE)
        if comparison == Comparison.FIXED:
            positive, note = "strong", "failed before the change, passes after it"
        elif comparison == Comparison.IMPROVED:
            fixed = sorted(base.classification.fingerprint.failing_tests - post.classification.fingerprint.failing_tests)
            remaining = sorted(post.classification.fingerprint.failing_tests)
            positive = "strong"
            note = f"fixed {', '.join(fixed)}; still failing as before the change (pre-existing): {', '.join(remaining)}"
        elif comparison == Comparison.UNCHANGED_PASS:
            before, after = base.classification.fingerprint.tests_run, post.classification.fingerprint.tests_run
            if before is not None and after is not None and after > before:
                positive, note = "strong", f"passes with more tests than at baseline ({before} -> {after})"
            elif cmd.purpose == "task" and self.changes.net_changed():
                positive, note = "weak", "passes before and after the change"
            else:
                note = "passes before and after; no regression detected"
        elif comparison == Comparison.REGRESSED:
            finding = FailureClass.COMMAND_TIMEOUT if post.classification.status == CommandStatus.TIMEOUT else (
                FailureClass.REGRESSION if cmd.kind == "test" else failure_kind)
            note = "passed before the change, fails after it"
        elif comparison == Comparison.CHANGED_FAILURE:
            finding = FailureClass.REGRESSION
            new = sorted(post.classification.fingerprint.failing_tests - base.classification.fingerprint.failing_tests) \
                if post.classification.fingerprint and base.classification.fingerprint else []
            note = "fails differently than before the change" + (f"; new failures: {', '.join(new)}" if new else "")
        elif comparison == Comparison.UNCHANGED_FAILURE:
            if cmd.purpose == "task":
                finding, note = failure_kind, "still fails exactly as before the change"
            else:
                note = "failed before the change and still fails the same way (pre-existing; not attributed to this run)"
        elif comparison == Comparison.NO_BASELINE:
            if post.classification.status != CommandStatus.PASS and cmd.purpose == "task":
                finding = failure_kind
            note = f"no usable baseline; now {post.classification.status.value}"
        elif comparison == Comparison.ENVIRONMENT:
            note = f"cannot be used as evidence: {post.classification.reason}"
        else:
            note = f"cannot be used as evidence: {post.classification.status.value}"
        return CommandVerification(cmd, base, post, comparison, finding, positive, note)

    def _assess_criteria(self, phase: str, results, change_items) -> list[CriterionResult]:
        plan = self.state.plan
        if plan is None:
            return []
        fixed, failing = {}, {}
        for r in results:
            if r.command.kind != "test":
                continue
            post_fp = r.post.classification.fingerprint
            post_failing = post_fp.failing_tests if post_fp else frozenset()
            if r.baseline is not None and r.baseline.classification.fingerprint is not None:
                for test_id in r.baseline.classification.fingerprint.failing_tests - post_failing:
                    if r.post.classification.status in (CommandStatus.PASS, CommandStatus.TEST_FAILURE):
                        fixed[test_id] = (r.baseline.evidence_id, r.post.evidence_id)
            for test_id in post_failing:
                failing[test_id] = (r.post.evidence_id,)
        results_out = []
        for criterion in plan.acceptance_criteria:
            terms = self._criterion_terms(criterion)
            fail_hits = [t for t in failing if any(term in normalize_name(t) for term in terms)]
            pass_hits = [t for t in fixed if any(term in normalize_name(t) for term in terms)]
            structural = self._structural(criterion)
            if fail_hits:
                refs = sorted({e for t in fail_hits for e in failing[t]}, key=_evidence_order)
                status, notes = "FAIL", f"related test(s) fail after the change: {', '.join(sorted(fail_hits))}"
            elif pass_hits:
                refs = sorted({e for t in pass_hits for e in fixed[t]}, key=_evidence_order)
                status, notes = "PASS", f"related test(s) failed before and pass after: {', '.join(sorted(pass_hits))}"
            elif structural:
                refs = [i for i in change_items if self.ledger.get(i).path == structural] or []
                status, notes = "PASS", f"{structural} exists after the change (structural check only)"
            else:
                refs, status, notes = [], "UNKNOWN", "no observed evidence maps to this criterion"
            item = self.ledger.record_assessment(phase, criterion, status, refs, notes)
            results_out.append(CriterionResult(criterion, status, tuple(refs) + (item.id,), notes))
        return results_out

    @staticmethod
    def _criterion_terms(criterion: str) -> list[str]:
        signals = extract_task_signals(criterion)
        terms = [normalize_name(i) for i in signals.identifiers] + list(signals.name_parts)
        return [t for t in dict.fromkeys(terms) if len(t) >= 4]

    def _structural(self, criterion: str) -> Optional[str]:
        text = criterion.lower()
        if not any(w in text for w in ("exist", "creat", "add", "present")):
            return None
        for path in extract_task_signals(criterion).explicit_paths:
            try:
                if resolve_in_repo(self.root, path).is_file():
                    return path
            except ToolFailure:
                continue
        return None

    def _changed_outside_patches(self, baseline: Optional[BaselineResult], final: RepoSnapshot) -> tuple[str, ...]:
        if baseline is None or not final.available or not baseline.after_baseline.available:
            return ()
        before, after = baseline.after_baseline.state_of(), final.state_of()
        patched = set(self.changes.paths)
        return tuple(sorted(p for p in set(before) | set(after)
                            if p not in patched and before.get(p) != after.get(p)))

    def _decide(self, results, findings, changed) -> tuple[Verdict, Optional[FailureClass], str]:
        if findings:
            primary = next(c for c in REPAIR_PRIORITY if c in findings)
            culprit = next((r for r in results if r.finding == primary), None)
            where = f" ({culprit.command.id} {culprit.command.text}: {culprit.note})" if culprit else ""
            if primary == FailureClass.DIFF_PROBLEM:
                where = " (the plan has edit steps but this run changed no file)"
            return Verdict.NEEDS_REPAIR, primary, f"{primary.value}{where}"
        strong = [r for r in results if r.positive == "strong"]
        weak = [r for r in results if r.positive == "weak"]
        if strong or weak:
            basis = strong or weak
            label = "strong" if strong else "weak"
            return (Verdict.VERIFIED, None,
                    f"{label} evidence: " + "; ".join(f"{r.command.id} {r.note}" for r in basis))
        commands = self.state.verification_commands
        runnable = [r for r in results if r.comparison not in (Comparison.ENVIRONMENT, Comparison.NOT_COMPARABLE)]
        env = [r for r in results if r.comparison == Comparison.ENVIRONMENT]
        if commands and env and not runnable:
            return (Verdict.BLOCKED, FailureClass.ENVIRONMENT_ERROR,
                    "no verification command could run in this environment: "
                    + "; ".join(f"{r.command.id} {r.post.classification.reason}" for r in env))
        if not commands:
            return (Verdict.UNVERIFIED, FailureClass.NO_VERIFICATION_EVIDENCE,
                    "no verification command was discovered; correctness cannot be established"
                    + (f" (changed: {', '.join(changed)})" if changed else ""))
        return (Verdict.UNVERIFIED, FailureClass.ENVIRONMENT_ERROR if env else FailureClass.NO_VERIFICATION_EVIDENCE,
                "no positive evidence: " + ("; ".join(f"{r.command.id} {r.note}" for r in results) or "no command ran"))

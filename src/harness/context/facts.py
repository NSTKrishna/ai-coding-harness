"""Typed episodic facts: observed or operational summaries only, never reasoning.

``kind`` decides how a fact is treated by compaction: kinds in ``PROTECTED_KINDS``
are never dropped (they are what prevents repeating a failed repair); ``history``
facts (compacted old observations) are the first to go when space is short.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Optional

PROTECTED_KINDS = frozenset({"verification", "repair_attempt", "failure", "change", "decision"})


@dataclass(frozen=True)
class RepositoryFact:
    summary: str
    kind: str = "repository"

    def text(self) -> str:
        return self.summary


@dataclass(frozen=True)
class VerificationFact:
    round: int
    after_repair_cycle: int
    verdict: str
    failure_class: Optional[str]
    command_id: Optional[str]
    fingerprint: str
    changed: tuple[str, ...]
    kind: str = "verification"

    def text(self) -> str:
        return (f"round {self.round} after repair cycle {self.after_repair_cycle}: {self.verdict}"
                + (f" {self.failure_class}" if self.failure_class else "")
                + (f" [{self.command_id} fingerprint {self.fingerprint}]" if self.command_id else "")
                + f"; changed: {', '.join(self.changed) or 'none'}")


@dataclass(frozen=True)
class RepairAttemptFact:
    cycle: int
    failure_class: str
    failure_fingerprint: str
    files_changed: tuple[str, ...]
    outcome: str                     # "started", or the verdict that followed the repair
    kind: str = "repair_attempt"

    def text(self) -> str:
        return (f"cycle {self.cycle}: repairing {self.failure_class}"
                + (f" ({self.failure_fingerprint})" if self.failure_fingerprint else "")
                + f"; files: {', '.join(self.files_changed) or 'none'}; outcome: {self.outcome}")


@dataclass(frozen=True)
class FailureFact:
    fingerprint: str
    failure_class: str
    command_id: Optional[str]
    round: int
    kind: str = "failure"

    def text(self) -> str:
        return f"unresolved {self.failure_class} in round {self.round}" + (
            f": {self.command_id} {self.fingerprint}" if self.command_id else f": {self.fingerprint}")


@dataclass(frozen=True)
class ChangeFact:
    path: str
    touched_in: tuple[str, ...]
    patches: int
    kind: str = "change"

    def text(self) -> str:
        return f"{self.path} changed by this run in {', '.join(self.touched_in)} ({self.patches} patch(es))"


@dataclass(frozen=True)
class DecisionFact:
    decision: str
    reason: str
    kind: str = "decision"

    def text(self) -> str:
        return f"{self.decision}: {self.reason}"


@dataclass(frozen=True)
class HistoryFact:
    step: int
    tool: str
    arguments: str
    outcome: str
    note: str
    kind: str = "history"

    def text(self) -> str:
        return f"step {self.step}: {self.tool} {self.arguments} -> {self.outcome}" + (f" ({self.note})" if self.note else "")


def fact_data(fact) -> dict:
    data = asdict(fact)
    data.pop("kind", None)
    return data

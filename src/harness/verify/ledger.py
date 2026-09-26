"""EvidenceLedger and ChangeLedger.

Evidence items are created only by the ``record_*`` methods, each of which takes
something the harness actually observed (a tool result classification, a git
snapshot, a patch result, or other evidence ids). Ids are sequential per run
(``E1``, ``E2`` …) and never reused. Nothing a model says becomes evidence.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from typing import Optional, Sequence


class EvidenceKind(str, enum.Enum):
    TEST = "TEST"
    BUILD = "BUILD"
    LINT = "LINT"
    TYPECHECK = "TYPECHECK"
    DIFF = "DIFF"
    FILE_CHANGE = "FILE_CHANGE"
    BASELINE = "BASELINE"
    ENVIRONMENT = "ENVIRONMENT"
    SNAPSHOT = "SNAPSHOT"
    ACCEPTANCE_ASSESSMENT = "ACCEPTANCE_ASSESSMENT"


KIND_BY_COMMAND = {"test": EvidenceKind.TEST, "build": EvidenceKind.BUILD, "lint": EvidenceKind.LINT,
                   "format": EvidenceKind.LINT, "typecheck": EvidenceKind.TYPECHECK}


@dataclass(frozen=True)
class EvidenceItem:
    id: str
    kind: EvidenceKind
    phase: str                      # "initial", "baseline", "post-1", "post-2", …
    source: str                     # tool that produced it, or "verifier" for assessments
    description: str
    command: Optional[str] = None
    command_id: Optional[str] = None
    exit_code: Optional[int] = None
    result: Optional[str] = None    # CommandStatus / criterion status / snapshot note
    path: Optional[str] = None
    excerpt: str = ""               # bounded
    refs: tuple[str, ...] = ()      # other evidence ids this item is based on


class EvidenceLedger:
    def __init__(self) -> None:
        self._items: list[EvidenceItem] = []

    def _add(self, **fields) -> EvidenceItem:
        item = EvidenceItem(id=f"E{len(self._items) + 1}", **fields)
        self._items.append(item)
        return item

    def record_command(self, phase: str, command_id: str, kind: str, command: str, classification) -> EvidenceItem:
        from harness.verify.outcomes import CommandStatus
        env = classification.status == CommandStatus.ENVIRONMENT_ERROR
        return self._add(kind=EvidenceKind.ENVIRONMENT if env else KIND_BY_COMMAND.get(kind, EvidenceKind.TEST),
                         phase=phase, source="run_tests" if kind == "test" else "run_command",
                         description=classification.reason, command=command, command_id=command_id,
                         exit_code=classification.exit_code, result=classification.status.value,
                         excerpt=classification.excerpt)

    def record_not_run(self, phase: str, command_id: str, kind: str, command: str, reason: str) -> EvidenceItem:
        return self._add(kind=KIND_BY_COMMAND.get(kind, EvidenceKind.TEST), phase=phase, source="verifier",
                         description=f"not run: {reason}", command=command, command_id=command_id, result="NOT_RUN")

    def record_baseline_unavailable(self, reason: str) -> EvidenceItem:
        return self._add(kind=EvidenceKind.BASELINE, phase="baseline", source="verifier",
                         description=f"BASELINE_NOT_AVAILABLE: {reason}", result="NOT_AVAILABLE")

    def record_snapshot(self, phase: str, description: str, excerpt: str, result: str) -> EvidenceItem:
        return self._add(kind=EvidenceKind.SNAPSHOT, phase=phase, source="git_status/git_diff_stat",
                         description=description, excerpt=excerpt, result=result)

    def record_diff(self, phase: str, description: str, excerpt: str) -> EvidenceItem:
        return self._add(kind=EvidenceKind.DIFF, phase=phase, source="git_diff", description=description,
                         excerpt=excerpt)

    def record_file_change(self, phase: str, path: str, description: str) -> EvidenceItem:
        return self._add(kind=EvidenceKind.FILE_CHANGE, phase=phase, source="apply_patch", path=path,
                         description=description)

    def record_assessment(self, phase: str, criterion: str, status: str, refs: Sequence[str], notes: str) -> EvidenceItem:
        unknown = [r for r in refs if self.get(r) is None]
        if unknown:
            raise ValueError(f"assessment refers to unknown evidence {unknown}")
        return self._add(kind=EvidenceKind.ACCEPTANCE_ASSESSMENT, phase=phase, source="verifier",
                         description=f"{criterion} -> {status}: {notes}", result=status, refs=tuple(refs))

    def get(self, evidence_id: str) -> Optional[EvidenceItem]:
        index = int(evidence_id[1:]) - 1 if evidence_id[1:].isdigit() else -1
        return self._items[index] if 0 <= index < len(self._items) else None

    @property
    def items(self) -> tuple[EvidenceItem, ...]:
        return tuple(self._items)

    def of_kind(self, kind: EvidenceKind) -> tuple[EvidenceItem, ...]:
        return tuple(i for i in self._items if i.kind == kind)


@dataclass
class ChangeRecord:
    path: str
    before_sha256: Optional[str]           # content before the run's first patch of this file (None: created)
    current_sha256: Optional[str]          # after the latest patch (None: deleted)
    touched_in: list[str] = field(default_factory=list)   # "execute", "repair-1", …
    patches: int = 0


class ChangeLedger:
    """Files this run changed with apply_patch. Not a rollback mechanism."""

    def __init__(self) -> None:
        self._records: dict[str, ChangeRecord] = {}

    def record_patch(self, patched_files, label: str) -> None:
        for f in patched_files:
            record = self._records.get(f.path)
            if record is None:
                record = self._records[f.path] = ChangeRecord(f.path, f.before_sha256, f.after_sha256)
            record.current_sha256 = f.after_sha256
            record.patches += 1
            if label not in record.touched_in:
                record.touched_in.append(label)

    @property
    def records(self) -> tuple[ChangeRecord, ...]:
        return tuple(self._records.values())

    @property
    def paths(self) -> tuple[str, ...]:
        return tuple(self._records)

    def net_changed(self) -> tuple[str, ...]:
        """Paths whose content now differs from before the run's first patch."""
        return tuple(r.path for r in self._records.values() if r.before_sha256 != r.current_sha256)

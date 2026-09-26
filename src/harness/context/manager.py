"""ContextManager: permanent, working and episodic context. No model involved.

- Permanent: the original task and the repository summary. Never evicted.
- Working: snippets and observations, bounded by ``ContextLimits``
  (``max_evidence_items`` items, ``max_context_chars`` characters). When full,
  the lowest-priority item is evicted first (oldest first among equals).
  Identical content from the same source is stored once.
- Episodic: typed facts (``context.facts``), kept in order, deduplicated, capped
  at ``max_facts``. Protected kinds (verification, repair attempts, failures,
  changes, decisions) are never evicted; other facts go oldest first.

Compaction of the executor request lives in ``context.compaction`` (deterministic,
no model call); it uses ``drop_working`` and ``drop_facts`` below.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Any, Mapping, Optional

from harness.config import ContextLimits
from harness.context.facts import PROTECTED_KINDS, fact_data
from harness.context.working_set import WorkingSet

TRUNCATION_MARK = "\n[... truncated to fit the working-context limit ...]"


@dataclass(frozen=True)
class PermanentContext:
    task: str
    repo_summary: str


@dataclass(frozen=True)
class ContextItem:
    id: str
    kind: str          # e.g. "evidence", "observation"
    source: str        # e.g. "src/app.py:10-30", "run_tests"
    content: str
    priority: float
    seq: int           # insertion order


@dataclass(frozen=True)
class EpisodicFact:
    kind: str
    text: str
    source: Optional[str]
    seq: int
    data: Mapping[str, Any] = field(default_factory=dict)   # structured fields of a typed fact


class ContextManager:
    def __init__(self, task: str, repo_summary: str, limits: Optional[ContextLimits] = None,
                 *, max_facts: int = 200) -> None:
        self.limits = limits or ContextLimits()
        self.max_facts = max_facts
        self._permanent = PermanentContext(task=task, repo_summary=repo_summary)
        self._items: list[ContextItem] = []
        self._facts: list[EpisodicFact] = []
        self._seq = 0
        self.evicted = 0          # working items dropped to respect limits
        self.facts_dropped = 0

    # permanent ---------------------------------------------------------------
    @property
    def permanent(self) -> PermanentContext:
        return self._permanent

    # working -----------------------------------------------------------------
    @property
    def working_items(self) -> tuple[ContextItem, ...]:
        return tuple(self._items)

    @property
    def working_chars(self) -> int:
        return sum(len(i.content) for i in self._items)

    def load_working_set(self, working_set: WorkingSet) -> None:
        """Replace working context with the working set's evidence."""
        self._items = []
        for e in working_set.evidence:
            self.add_working_item("evidence", f"{e.path}:{e.line_start}-{e.line_end}", e.snippet,
                                  priority=e.value)

    def add_working_item(self, kind: str, source: str, content: str, priority: float = 0.0) -> Optional[str]:
        """Add an item; returns its id, or ``None`` if it was itself evicted as lowest priority."""
        digest = hashlib.sha256(f"{source}\0{content}".encode()).hexdigest()[:12]
        if any(i.id == digest for i in self._items):
            return digest  # already present: repeated context is not stored twice
        budget = self.limits.max_context_chars
        if len(content) > budget:
            content = content[: budget - len(TRUNCATION_MARK)] + TRUNCATION_MARK
        self._seq += 1
        self._items.append(ContextItem(digest, kind, source, content, priority, self._seq))
        while len(self._items) > self.limits.max_evidence_items or self.working_chars > budget:
            victim = min(self._items, key=lambda i: (i.priority, i.seq))
            self._items.remove(victim)
            self.evicted += 1
        return digest if any(i.id == digest for i in self._items) else None

    def remove_source(self, path: str) -> int:
        """Drop working items for ``path`` (e.g. after the file was edited). Returns how many."""
        before = len(self._items)
        self._items = [i for i in self._items if i.source != path and not i.source.startswith(path + ":")]
        return before - len(self._items)

    # episodic ----------------------------------------------------------------
    def record_fact(self, kind: str, text: str, source: Optional[str] = None,
                    data: Optional[Mapping[str, Any]] = None) -> EpisodicFact:
        for fact in self._facts:
            if fact.kind == kind and fact.text == text:
                return fact
        self._seq += 1
        fact = EpisodicFact(kind, text, source, self._seq, dict(data or {}))
        self._facts.append(fact)
        if len(self._facts) > self.max_facts:
            victim = next((f for f in self._facts if f.kind not in PROTECTED_KINDS), self._facts[0])
            self._facts.remove(victim)
            self.facts_dropped += 1
        return fact

    def record(self, fact, source: Optional[str] = None) -> EpisodicFact:
        """Record a typed fact from ``context.facts``."""
        return self.record_fact(fact.kind, fact.text(), source, fact_data(fact))

    def drop_facts(self, kind: str, keep_last: int = 0) -> int:
        """Drop facts of an unprotected ``kind``, oldest first, keeping the last ``keep_last``."""
        if kind in PROTECTED_KINDS:
            raise ValueError(f"{kind} facts are protected")
        matching = [f for f in self._facts if f.kind == kind]
        victims = matching[: max(len(matching) - keep_last, 0)]
        self._facts = [f for f in self._facts if f not in victims]
        self.facts_dropped += len(victims)
        return len(victims)

    def drop_working(self, kind: str, keep: int = 0) -> int:
        """Drop working items of ``kind``, lowest priority first, keeping the best ``keep``."""
        matching = sorted((i for i in self._items if i.kind == kind), key=lambda i: (-i.priority, i.seq))
        victims = matching[keep:]
        self._items = [i for i in self._items if i not in victims]
        self.evicted += len(victims)
        return len(victims)

    def facts(self, kind: Optional[str] = None) -> tuple[EpisodicFact, ...]:
        return tuple(f for f in self._facts if kind is None or f.kind == kind)

    # rendering -----------------------------------------------------------------
    def render(self) -> str:
        out = ["## Task", self._permanent.task, "## Repository", self._permanent.repo_summary]
        if self._facts:
            out.append("## Facts")
            out += [f"- [{f.kind}] {f.text}" for f in self._facts]
        if self._items:
            out.append("## Working context")
            for item in sorted(self._items, key=lambda i: i.seq):
                out += [f"### {item.source} ({item.kind})", item.content]
        return "\n".join(out) + "\n"

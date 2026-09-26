"""WorkingSet: the bounded repository context a planner receives.

``build_working_set`` enforces every ``ContextLimits`` bound and records what
it had to leave out. The rendered text is what counts against
``max_context_chars``, so the bound is checked on the exact output.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

from harness.config import ContextLimits

MAX_LINE_CHARS = 240          # longer snippet lines are cut
MAX_REASONS_LISTED = 3
MAX_REASON_CHARS = 120


@dataclass(frozen=True)
class Evidence:
    path: str
    line_start: int     # 1-based, inclusive
    line_end: int
    snippet: str        # lines line_start..line_end, each cut to MAX_LINE_CHARS
    reason: str
    signal: str         # the task signal behind it ("" for file heads)
    value: float        # selection priority (higher first)


@dataclass(frozen=True)
class CandidateSummary:
    """What the working set lists about a ranked file."""
    path: str
    score: int
    reasons: tuple[str, ...]


@dataclass(frozen=True)
class WorkingSet:
    repo_summary: str
    candidates: tuple[CandidateSummary, ...]   # listed candidates, best first
    selected_files: tuple[str, ...]            # files whose evidence may be included
    evidence: tuple[Evidence, ...]             # in reading order: file rank, then line
    limits: ContextLimits
    candidates_omitted: int = 0
    evidence_omitted: int = 0
    summary_truncated: bool = False

    def render(self) -> str:
        return _render(self.repo_summary, self.candidates, self.candidates_omitted,
                       self.evidence, self.selected_files)

    @property
    def estimated_chars(self) -> int:
        return len(self.render())

    @property
    def estimated_tokens(self) -> int:
        return self.estimated_chars // 4


def _render(summary: str, candidates: Sequence[CandidateSummary], omitted: int,
            evidence: Sequence[Evidence], selected: Sequence[str]) -> str:
    out = ["## Repository", summary]
    if candidates:
        more = f", {omitted} more not shown" if omitted else ""
        out.append(f"## Candidate files ({len(candidates)} shown{more})")
        for i, c in enumerate(candidates, start=1):
            reasons = "; ".join(r[:MAX_REASON_CHARS] for r in c.reasons[:MAX_REASONS_LISTED])
            out.append(f"{i}. {c.path} (score {c.score}): {reasons}")
    if evidence:
        rank = {p: i for i, p in enumerate(selected)}
        out.append("## Evidence")
        for e in sorted(evidence, key=lambda e: (rank.get(e.path, len(rank)), e.path, e.line_start)):
            out.append(f"### {e.path}:{e.line_start}-{e.line_end} ({e.reason})")
            for offset, line in enumerate(e.snippet.split("\n")):
                out.append(f"{e.line_start + offset:>5}| {line}")
    return "\n".join(out) + "\n"


def make_snippet(lines: Sequence[str], start: int, end: int) -> str:
    """Lines ``start..end`` (1-based, inclusive) without endings, each cut to MAX_LINE_CHARS."""
    cut = []
    for line in lines[start - 1:end]:
        line = line.rstrip("\r\n")
        cut.append(line if len(line) <= MAX_LINE_CHARS else line[: MAX_LINE_CHARS - 1] + "…")
    return "\n".join(cut)


def build_working_set(repo_summary: str, candidates: Sequence[CandidateSummary],
                      evidence_pool: Sequence[Evidence], limits: ContextLimits) -> WorkingSet:
    """Select the highest-value content that fits every limit.

    1. ``selected_files`` = the top ``max_active_files`` candidates.
    2. List at most ``max_candidates`` candidates.
    3. Add evidence for selected files in value order while the item count stays
       within ``max_evidence_items`` and the rendered size within ``max_context_chars``.
    4. If the summary and list alone do not fit, drop listed candidates from the
       bottom, then cut the summary.
    """
    budget = limits.max_context_chars
    selected = tuple(c.path for c in candidates[: limits.max_active_files])
    listed = list(candidates[: limits.max_candidates])
    summary = repo_summary
    truncated = False

    def size(ev) -> int:
        return len(_render(summary, listed, len(candidates) - len(listed), ev, selected))

    while listed and size([]) > budget:
        listed.pop()
    if size([]) > budget:
        # The summary appears once in the rendering, so its allowance is exact.
        allowance = budget - (size([]) - len(summary))
        summary = summary[: allowance - 1] + "…" if allowance >= 1 else ""
        truncated = True

    chosen: list[Evidence] = []
    pool = sorted((e for e in evidence_pool if e.path in selected),
                  key=lambda e: (-e.value, e.path, e.line_start))
    for item in pool:
        if len(chosen) >= limits.max_evidence_items:
            break
        if item.line_end - item.line_start + 1 > limits.max_snippet_lines:
            continue  # callers build snippets within the limit; never exceed it
        if size(chosen + [item]) <= budget:
            chosen.append(item)
    rank = {p: i for i, p in enumerate(selected)}
    chosen.sort(key=lambda e: (rank[e.path], e.path, e.line_start))
    return WorkingSet(
        repo_summary=summary,
        candidates=tuple(listed),
        selected_files=selected,
        evidence=tuple(chosen),
        limits=limits,
        candidates_omitted=len(candidates) - len(listed),
        evidence_omitted=len(pool) - len(chosen),
        summary_truncated=truncated,
    )

"""The run's verification command set.

Only commands the harness discovered are used (never invented):

1. test commands the plan selected (already validated against discovery) — purpose "task";
2. otherwise the first discovered test command — purpose "suite" (a broad fallback);
3. then a high-confidence build command, then high-confidence lint/typecheck commands
   (purpose "check"); plan-selected ones are purpose "task".

At most ``max_verification_commands`` are run per round; the rest are recorded
as NOT_RUN. Every run is a normal registry dispatch and counts toward
``max_tool_calls``.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from harness.repo.commands import CommandCandidate


@dataclass(frozen=True)
class VerificationCommand:
    id: str                     # "V1", "V2", … stable for the run
    kind: str                   # "test", "build", "lint", "typecheck", "format"
    argv: tuple[str, ...]
    purpose: str                # "task" (selected by the plan), "suite" (discovered fallback) or "check"
    reason: str
    within_cap: bool            # False: listed but over max_verification_commands

    @property
    def text(self) -> str:
        return " ".join(self.argv)


def select_verification_commands(plan, profile, repo_root: Path, max_commands: int) -> tuple[VerificationCommand, ...]:
    # Local import: harness.orchestrator imports this module.
    from harness.orchestrator.interpreter import resolve_command

    chosen: list[tuple[CommandCandidate, str]] = []
    seen: set[tuple[str, ...]] = set()

    def add(candidate: CommandCandidate, purpose: str) -> None:
        if candidate.argv not in seen:
            seen.add(candidate.argv)
            chosen.append((candidate, purpose))

    planned: Sequence[CommandCandidate] = plan.verification_candidates if plan is not None else ()
    for c in planned:
        if c.kind == "test":
            add(c, "task")
    if not any(p == "task" for _, p in chosen) and profile.test_commands:
        add(resolve_command(profile.test_commands[0], repo_root), "suite")
    for c in planned:
        if c.kind != "test":
            add(c, "task")
    for kind in ("build", "typecheck", "lint"):
        for c in profile.build_commands:
            if c.kind == kind and c.confidence == "high":
                add(resolve_command(c, repo_root), "check")
                break

    return tuple(
        VerificationCommand(f"V{i}", c.kind, c.argv, purpose, c.reason, within_cap=i <= max_commands)
        for i, (c, purpose) in enumerate(chosen, start=1)
    )

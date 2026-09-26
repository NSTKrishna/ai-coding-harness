"""The run's verification command set.

Only commands the harness discovered are used (never invented):

0. targeted test commands derived from a discovered suite (``targeting.py``) — ids
   ``T1``…, level "targeted", purpose "task"; they run first;
1. test commands the plan selected (already validated against discovery) — purpose "task";
2. otherwise the first discovered test command — purpose "suite" (a broad fallback);
3. then a high-confidence build command, then high-confidence lint/typecheck commands
   (purpose "check"); plan-selected ones are purpose "task".

When a targeted command narrows a suite, that suite becomes the broader regression
check (purpose "suite") and only runs after the target passes (engine escalation).

At most ``max_verification_commands`` are run per round; the rest are recorded
as NOT_RUN. Every run is a normal registry dispatch and counts toward
``max_tool_calls``.
"""

from __future__ import annotations

import shlex

from dataclasses import dataclass, replace
from pathlib import Path
from typing import Callable, Optional, Sequence

from harness.repo.commands import CommandCandidate


@dataclass(frozen=True)
class VerificationCommand:
    id: str                     # "V1", "V2", … stable for the run
    kind: str                   # "test", "build", "lint", "typecheck", "format"
    argv: tuple[str, ...]
    purpose: str                # "task" (selected by the plan), "suite" (discovered fallback) or "check"
    reason: str
    within_cap: bool            # False: listed but over max_verification_commands
    level: str = "suite"        # "targeted", "suite" or "check"
    derived_from: str = ""      # targeted only: the evidence it was derived from
    parent_id: Optional[str] = None   # targeted only: the suite it narrows
    confidence: str = ""

    @property
    def text(self) -> str:
        return shlex.join(self.argv)   # quoted: a model may copy it verbatim as a command string


def select_verification_commands(plan, profile, repo_root: Path, max_commands: int,
                                 derive_targets: Optional[Callable] = None) -> tuple[VerificationCommand, ...]:
    """``derive_targets(base_commands) -> TargetingResult`` adds targeted commands in front."""
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

    base = [VerificationCommand(f"V{i}", c.kind, c.argv, purpose, c.reason, within_cap=True,
                                level="check" if c.kind != "test" else "suite", confidence=c.confidence)
            for i, (c, purpose) in enumerate(chosen, start=1)]
    targets = derive_targets(base).targets if derive_targets is not None else ()
    narrowed = {t.parent_command_id for t in targets}
    base = [replace(c, purpose="suite") if c.id in narrowed else c for c in base]
    commands = [VerificationCommand(f"T{i}", "test", t.argv, "task", t.reason, within_cap=True, level="targeted",
                                    derived_from=t.derived_from, parent_id=t.parent_command_id,
                                    confidence=t.confidence)
                for i, t in enumerate(targets, start=1)] + base
    return tuple(replace(c, within_cap=i <= max_commands) for i, c in enumerate(commands, start=1))

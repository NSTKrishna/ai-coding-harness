"""Plain-text reports for ``harness inspect``. Deterministic: no timestamps or timings."""

from __future__ import annotations

from harness.repo.discovery import DiscoveryResult
from harness.repo.profile import RepoProfile


def _pairs(items) -> str:
    return ", ".join(f"{name} ({n})" for name, n in items) or "none"


def _indicators(items) -> str:
    return "; ".join(f"{i.name} [{i.evidence}]" for i in items) or "none found"


def format_profile(profile: RepoProfile) -> str:
    lines = [
        f"Repository: {profile.root}",
        f"  Inventory:       {profile.file_count} files, {profile.total_bytes:,} bytes "
        f"({'git: tracked + untracked, .gitignore respected' if profile.is_git else 'not a git repository: filesystem walk'})",
        f"  Languages:       {_pairs(profile.languages)}",
        f"  File categories: {_pairs(profile.categories)}",
        f"  Top directories: {_pairs(profile.important_dirs)}",
        f"  Source roots:    {', '.join(profile.source_roots) or 'none detected'}",
        f"  Test roots:      {', '.join(profile.test_roots) or 'none detected'}",
        f"  Manifests:       {', '.join(profile.manifests) or 'none'}",
        f"  Build systems:   {_indicators(profile.build_systems)}",
        f"  Test frameworks: {_indicators(profile.test_frameworks)}",
        f"  Lint/typecheck:  {_indicators(profile.lint_typecheck)}",
        f"  CI:              {', '.join(profile.ci) or 'none'}",
        f"  Docs:            {', '.join(profile.docs) or 'none'}",
        "  Test commands:" + ("" if profile.test_commands else " none identified (no supporting configuration)"),
    ]
    for c in profile.test_commands:
        lines.append(f"    {' '.join(c.argv)}  [{c.confidence}] {c.reason}")
    lines.append("  Build/lint/typecheck commands:" + ("" if profile.build_commands else " none identified"))
    for c in profile.build_commands:
        lines.append(f"    {c.kind:<9} {' '.join(c.argv)}  [{c.confidence}] {c.reason}")
    for w in profile.warnings:
        lines.append(f"  warning: {w}")
    return "\n".join(lines) + "\n"


def format_discovery(result: DiscoveryResult, top: int = 10) -> str:
    s = result.task_signals
    ws = result.working_set
    m = result.metrics
    lines = [format_profile(result.repo_profile).rstrip("\n"), "", "Task signals:"]
    for label, values in (("paths", s.explicit_paths), ("identifiers", s.identifiers),
                          ("name parts", s.name_parts), ("keywords", s.keywords), ("phrases", s.phrases)):
        lines.append(f"  {label + ':':<13} {', '.join(values) or '-'}")
    shown = result.candidates[:top]
    lines += ["", f"Candidate files (top {len(shown)} of {len(result.candidates)}):"]
    if not shown:
        lines.append("  none: no file matched any task signal")
    for i, c in enumerate(shown, start=1):
        marker = "*" if c.path in ws.selected_files else " "
        lines.append(f" {marker}{i:>2}. {c.path}  (score {c.score})")
        for reason in c.reasons[:4]:
            lines.append(f"        - {reason}")
    lines += [
        "",
        f"Working set (* = selected): {len(ws.selected_files)} files, {len(ws.evidence)} evidence items, "
        f"{ws.estimated_chars:,} / {ws.limits.max_context_chars:,} chars"
        + (f"; {ws.evidence_omitted} evidence items left out to stay within limits" if ws.evidence_omitted else ""),
        f"Discovery: {m.summary()}",
    ]
    for w in result.warnings:
        lines.append(f"warning: {w}")
    return "\n".join(lines) + "\n"

"""Repository intelligence: inventory, profile, task signals, candidate discovery.

Deterministic and model-free. Entry points: ``analyze_repository`` and
``discover_for_task``.
"""

from harness.repo.commands import CommandCandidate
from harness.repo.discovery import CandidateFile, DiscoveryMetrics, DiscoveryResult, discover_for_task
from harness.repo.inventory import FileRecord, Inventory, build_inventory
from harness.repo.profile import Indicator, RepoProfile, RepositoryAnalyzer, analyze_repository
from harness.repo.signals import TaskSignals, extract_task_signals

__all__ = [
    "CandidateFile", "CommandCandidate", "DiscoveryMetrics", "DiscoveryResult", "FileRecord",
    "Indicator", "Inventory", "RepoProfile", "RepositoryAnalyzer", "TaskSignals",
    "analyze_repository", "build_inventory", "discover_for_task", "extract_task_signals",
]

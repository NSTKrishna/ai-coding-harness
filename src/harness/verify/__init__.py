"""Verification (M5): baseline, verification engine, evidence ledger, recovery."""

from harness.verify.commands import VerificationCommand, select_verification_commands
from harness.verify.engine import (
    BaselineResult,
    CommandVerification,
    CriterionResult,
    RepoSnapshot,
    Verdict,
    VerificationBudgetExhausted,
    VerificationEngine,
    VerificationReport,
)
from harness.verify.ledger import ChangeLedger, EvidenceItem, EvidenceKind, EvidenceLedger
from harness.verify.outcomes import CommandStatus, Comparison, FailureClass, classify, compare
from harness.verify.recovery import RecoveryController, RepairContext, RepairLimit

__all__ = [
    "BaselineResult", "ChangeLedger", "CommandStatus", "CommandVerification", "Comparison", "CriterionResult",
    "EvidenceItem", "EvidenceKind", "EvidenceLedger", "FailureClass", "RecoveryController", "RepairContext",
    "RepairLimit", "RepoSnapshot", "Verdict", "VerificationBudgetExhausted", "VerificationCommand",
    "VerificationEngine", "VerificationReport", "classify", "compare", "select_verification_commands",
]

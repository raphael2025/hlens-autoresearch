"""Application-plane worker (ADR-0044, ADR-0049): idempotent jobs, the research-loop scheduler and
the degradation monitor. Never imports research/ (stages are injected by a composition root)."""

from apps.worker.degradation import DegradationCheck, DegradationMonitor
from apps.worker.jobs import JobOutcome, JobRunner, JobSpec
from apps.worker.journal import AppendOnlyJournal, JournalCorrupted
from apps.worker.loop import (
    AUTOMATABLE_TARGETS,
    EXTENDED_STAGE_ORDER,
    OPTIONAL_STAGES,
    STAGE_ORDER,
    AutomationForbidden,
    LifecycleGuard,
    LoopAuditCorrupted,
    LoopAuditLog,
    LoopBudget,
    LoopHalted,
    LoopRecord,
    LoopStage,
    ResearchLoop,
    RoundContext,
    RoundStatus,
    StageFailed,
    StageRecord,
    StageResult,
    StageStatus,
    StageUsage,
)
from apps.worker.metrics import RoundMetrics, StageMetrics, monotonic_clock

__all__ = [
    "AUTOMATABLE_TARGETS",
    "EXTENDED_STAGE_ORDER",
    "OPTIONAL_STAGES",
    "STAGE_ORDER",
    "AppendOnlyJournal",
    "AutomationForbidden",
    "DegradationCheck",
    "DegradationMonitor",
    "JobOutcome",
    "JobRunner",
    "JobSpec",
    "JournalCorrupted",
    "LifecycleGuard",
    "LoopAuditCorrupted",
    "LoopAuditLog",
    "LoopBudget",
    "LoopHalted",
    "LoopRecord",
    "LoopStage",
    "ResearchLoop",
    "RoundContext",
    "RoundMetrics",
    "RoundStatus",
    "StageRecord",
    "StageFailed",
    "StageMetrics",
    "StageResult",
    "StageStatus",
    "StageUsage",
    "monotonic_clock",
]

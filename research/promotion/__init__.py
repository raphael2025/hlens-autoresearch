"""Research-side promotion: evidence → StrategyArtifact → Strategy Registry (ADR-0005).

CODE_COMPLETE / DEBUG_PENDING (2026-09-26). See ``service.py`` and ``README.md``.
"""

from research.promotion.service import (
    PROMOTABLE_STATES,
    PromotionEvidence,
    PromotionPackage,
    PromotionRefusal,
    PromotionRefused,
    ValidationReplayProvider,
    ValidationReplayResult,
    build_artifact,
    promote,
)

__all__ = [
    "PROMOTABLE_STATES",
    "PromotionEvidence",
    "PromotionPackage",
    "PromotionRefusal",
    "PromotionRefused",
    "ValidationReplayProvider",
    "ValidationReplayResult",
    "build_artifact",
    "promote",
]

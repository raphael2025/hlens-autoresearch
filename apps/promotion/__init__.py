"""Application-side promotion: Equivalence Gate and deployment record (ADR-0005).

CODE_COMPLETE / DEBUG_PENDING (2026-09-26). See ``equivalence.py`` and ``README.md``.
"""

from apps.promotion.equivalence import (
    DEPLOYABLE_STATES,
    EquivalenceOutcome,
    EquivalenceRefusal,
    EquivalenceRefused,
    deployment_id,
    record_deployment,
    run_equivalence_gate,
)

__all__ = [
    "DEPLOYABLE_STATES",
    "EquivalenceOutcome",
    "EquivalenceRefusal",
    "EquivalenceRefused",
    "deployment_id",
    "record_deployment",
    "run_equivalence_gate",
]

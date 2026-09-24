"""Append-only Raw revisions for the Binance spot first slice (Phase 1 D2; ADR-0023).

- ``identity``: the versioned, hashed rule for observation keys, revision ids, payload hashes and
  the arrival-sequence block layout;
- ``availability``: ``binance.spot.publication@1.0.0`` — the historical availability policy and
  its evidence gaps;
- ``precedence``: ``binance.spot.archive-revision@1.0.0`` — replay, provable precedence or
  competing heads;
- ``store``: ``RawRevisionStore`` — archive revision, parsed-row microbatches, idempotent replay,
  crash recovery and competing-head reporting.
"""

from infrastructure.revision.availability import (
    AVAILABILITY_BINDING,
    AVAILABILITY_HASH,
    AVAILABILITY_POLICY_ID,
    AVAILABILITY_POLICY_VERSION,
    AvailabilityRule,
    AvailabilityRuleKind,
    AvailabilitySubject,
    AvailabilityViolation,
    decide_availability,
)
from infrastructure.revision.identity import (
    ARRIVAL_SEQ_STRIDE,
    IDENTITY_HASH,
    IDENTITY_RULE_ID,
    IDENTITY_RULE_VERSION,
    IdentityViolation,
)
from infrastructure.revision.precedence import (
    PRECEDENCE_BINDING,
    PRECEDENCE_HASH,
    PRECEDENCE_POLICY_ID,
    PRECEDENCE_POLICY_VERSION,
    PrecedenceOutcome,
    PrecedenceViolation,
    RevisionFacts,
    decide_precedence,
    maximal_heads,
)
from infrastructure.revision.store import (
    ARCHIVE_TABLE,
    DEFAULT_MICROBATCH_ROWS,
    ROW_TABLES,
    ArchiveContext,
    ArchiveIngested,
    ArchiveRejected,
    AvailabilityGapSummary,
    BatchCommit,
    IngestOutcome,
    RawRevisionStore,
    RevisionStoreConflict,
    RevisionStoreError,
)

__all__ = [
    "ARCHIVE_TABLE",
    "ARRIVAL_SEQ_STRIDE",
    "AVAILABILITY_BINDING",
    "AVAILABILITY_HASH",
    "AVAILABILITY_POLICY_ID",
    "AVAILABILITY_POLICY_VERSION",
    "DEFAULT_MICROBATCH_ROWS",
    "IDENTITY_HASH",
    "IDENTITY_RULE_ID",
    "IDENTITY_RULE_VERSION",
    "PRECEDENCE_BINDING",
    "PRECEDENCE_HASH",
    "PRECEDENCE_POLICY_ID",
    "PRECEDENCE_POLICY_VERSION",
    "ROW_TABLES",
    "ArchiveContext",
    "ArchiveIngested",
    "ArchiveRejected",
    "AvailabilityGapSummary",
    "AvailabilityRule",
    "AvailabilityRuleKind",
    "AvailabilitySubject",
    "AvailabilityViolation",
    "BatchCommit",
    "IdentityViolation",
    "IngestOutcome",
    "PrecedenceOutcome",
    "PrecedenceViolation",
    "RawRevisionStore",
    "RevisionFacts",
    "RevisionStoreConflict",
    "RevisionStoreError",
    "decide_availability",
    "decide_precedence",
    "maximal_heads",
]

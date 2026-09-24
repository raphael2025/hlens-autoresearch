"""Append-only Raw revisions for the Binance spot first slice (Phase 1 D2; ADR-0023).

- ``identity``: the versioned, hashed rule for observation keys, revision ids, payload hashes and
  the arrival-sequence block layout;
- ``availability``: ``binance.spot.publication@1.0.0`` — the historical availability policy and
  its evidence gaps;
- ``precedence``: ``binance.spot.archive-revision@1.0.0`` — replay, provable precedence or
  competing heads;
- ``store``: ``RawRevisionStore`` — archive revision, parsed-row microbatches, idempotent replay,
  crash recovery and competing-head reporting.

REST back-fill (Phase 1 D3B, ADR-0027) — pure rules only, no I/O:

- ``rest_identity``: ``hlens.binance.spot.rest-revision-identity@1.0.0`` — canonical page identity,
  response / element keys and payload hashes, revision ids, time-free ``edge_id`` and the REST
  ``arrival_seq`` interval (independent of ``identity``, which it never imports);
- ``rest_availability``: ``binance.spot.rest-publication@1.0.0``;
- ``rest_precedence``: ``binance.spot.rest-revision@1.0.0`` — replay or competing heads, never an
  edge;
- ``channel_precedence``: ``binance.spot.delivery-channel@1.0.0`` — D-33 content projections,
  comparison and the evidence-only archive → REST edge.
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
from infrastructure.revision.channel_precedence import (
    DELIVERY_CHANNEL_BINDING,
    DELIVERY_CHANNEL_HASH,
    DELIVERY_CHANNEL_POLICY_ID,
    DELIVERY_CHANNEL_POLICY_VERSION,
    Channel,
    ChannelComparison,
    ChannelEdge,
    ChannelPrecedenceViolation,
    ChannelRevision,
    ComparisonOutcome,
    build_channel_edge,
    compare_channels,
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
from infrastructure.revision.rest_availability import (
    REST_AVAILABILITY_BINDING,
    REST_AVAILABILITY_HASH,
    REST_AVAILABILITY_POLICY_ID,
    REST_AVAILABILITY_POLICY_VERSION,
    RestAvailabilitySubject,
    RestAvailabilityViolation,
    decide_rest_availability,
)
from infrastructure.revision.rest_identity import (
    REST_ARRIVAL_SEQ_BASE,
    REST_ARRIVAL_SEQ_STRIDE,
    REST_IDENTITY_HASH,
    REST_IDENTITY_RULE_ID,
    REST_IDENTITY_RULE_VERSION,
    RestArrivalSeqOverflow,
    RestIdentityViolation,
    RestPageQuery,
)
from infrastructure.revision.rest_precedence import (
    REST_PRECEDENCE_BINDING,
    REST_PRECEDENCE_HASH,
    REST_PRECEDENCE_POLICY_ID,
    REST_PRECEDENCE_POLICY_VERSION,
    RestPrecedenceViolation,
    decide_rest_precedence,
    rest_competing_revisions,
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
    "DELIVERY_CHANNEL_BINDING",
    "DELIVERY_CHANNEL_HASH",
    "DELIVERY_CHANNEL_POLICY_ID",
    "DELIVERY_CHANNEL_POLICY_VERSION",
    "IDENTITY_HASH",
    "IDENTITY_RULE_ID",
    "IDENTITY_RULE_VERSION",
    "PRECEDENCE_BINDING",
    "PRECEDENCE_HASH",
    "PRECEDENCE_POLICY_ID",
    "PRECEDENCE_POLICY_VERSION",
    "REST_ARRIVAL_SEQ_BASE",
    "REST_ARRIVAL_SEQ_STRIDE",
    "REST_AVAILABILITY_BINDING",
    "REST_AVAILABILITY_HASH",
    "REST_AVAILABILITY_POLICY_ID",
    "REST_AVAILABILITY_POLICY_VERSION",
    "REST_IDENTITY_HASH",
    "REST_IDENTITY_RULE_ID",
    "REST_IDENTITY_RULE_VERSION",
    "REST_PRECEDENCE_BINDING",
    "REST_PRECEDENCE_HASH",
    "REST_PRECEDENCE_POLICY_ID",
    "REST_PRECEDENCE_POLICY_VERSION",
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
    "Channel",
    "ChannelComparison",
    "ChannelEdge",
    "ChannelPrecedenceViolation",
    "ChannelRevision",
    "ComparisonOutcome",
    "IdentityViolation",
    "IngestOutcome",
    "PrecedenceOutcome",
    "PrecedenceViolation",
    "RawRevisionStore",
    "RestArrivalSeqOverflow",
    "RestAvailabilitySubject",
    "RestAvailabilityViolation",
    "RestIdentityViolation",
    "RestPageQuery",
    "RestPrecedenceViolation",
    "RevisionFacts",
    "RevisionStoreConflict",
    "RevisionStoreError",
    "build_channel_edge",
    "compare_channels",
    "decide_availability",
    "decide_precedence",
    "decide_rest_availability",
    "decide_rest_precedence",
    "maximal_heads",
    "rest_competing_revisions",
]

"""File-backed, append-only Strategy Registry and golden blob store (ADR-0005; 2026-09-26), and the
Profile freeze registry (ADR-0062; 2026-09-27), the retirement record store (ADR-0086; 2026-09-28),
and the lifecycle authority registry (ADR-0098 §1; 2026-09-30).

CODE_COMPLETE / DEBUG_PENDING. See ``README.md`` and ``registry.py`` for placement and rules.
"""

from infrastructure.registry.blobs import BlobCorrupted, BlobMissing, BlobStore
from infrastructure.registry.golden import (
    GoldenAnswer,
    GoldenPayloadInvalid,
    decode_golden_positions,
    decode_golden_signals,
    golden_positions_payload,
    golden_signals_payload,
    payload_hash,
    positions_json,
)
from infrastructure.registry.lifecycle import (
    ActiveSet,
    HeadMismatch,
    LifecycleHead,
    LifecycleRecord,
    LifecycleRegistry,
    StrategyLifecycle,
    UnknownHead,
)
from infrastructure.registry.profile_freeze import (
    FreezeConflict,
    ProfileFreeze,
    ProfileFreezeRegistry,
)
from infrastructure.registry.registry import (
    DuplicateRecord,
    RegistryCorrupted,
    RegistryError,
    RegistryLocked,
    RegistryRefused,
    StrategyRegistry,
    UnknownArtifact,
)
from infrastructure.registry.retirement import RetirementRegistry

__all__ = [
    "ActiveSet",
    "BlobCorrupted",
    "BlobMissing",
    "BlobStore",
    "DuplicateRecord",
    "FreezeConflict",
    "GoldenAnswer",
    "GoldenPayloadInvalid",
    "HeadMismatch",
    "LifecycleHead",
    "LifecycleRecord",
    "LifecycleRegistry",
    "ProfileFreeze",
    "ProfileFreezeRegistry",
    "RegistryCorrupted",
    "RegistryError",
    "RegistryLocked",
    "RegistryRefused",
    "RetirementRegistry",
    "StrategyLifecycle",
    "StrategyRegistry",
    "UnknownArtifact",
    "UnknownHead",
    "decode_golden_positions",
    "decode_golden_signals",
    "golden_positions_payload",
    "golden_signals_payload",
    "payload_hash",
    "positions_json",
]

"""File-backed, append-only Strategy Registry and golden blob store (ADR-0005; 2026-09-26),
Profile freeze registry (ADR-0062; 2026-09-27), and retirement record store (ADR-0086; 2026-09-28).

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
    "BlobCorrupted",
    "BlobMissing",
    "BlobStore",
    "DuplicateRecord",
    "FreezeConflict",
    "GoldenAnswer",
    "GoldenPayloadInvalid",
    "ProfileFreeze",
    "ProfileFreezeRegistry",
    "RegistryCorrupted",
    "RegistryError",
    "RegistryLocked",
    "RegistryRefused",
    "RetirementRegistry",
    "StrategyRegistry",
    "UnknownArtifact",
    "decode_golden_positions",
    "decode_golden_signals",
    "golden_positions_payload",
    "golden_signals_payload",
    "payload_hash",
    "positions_json",
]

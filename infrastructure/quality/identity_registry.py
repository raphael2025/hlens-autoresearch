"""Public identity registry for the bounded Dataset Quality join (ADR-0101 §5).

``CanonicalV3IdentityRegistry`` is the production implementation of
``infrastructure.dataset.quality.QualityIdentityRegistry``: the registered canonical-partition v3
rule hashes ``QualityReporterV3`` writes reports with, as a finite read-only mapping. The only
caller-chosen number is the identity byte bound (DQ-9 stays OPEN, no default).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from infrastructure.quality.report_v3 import CANONICAL_V3_IDENTITY_RULE_HASHES

__all__ = ["CanonicalV3IdentityRegistry"]


@dataclass(frozen=True, slots=True)
class CanonicalV3IdentityRegistry:
    """The registered canonical v3 identity hashes plus the caller's identity byte bound."""

    max_identity_bytes: int

    def __post_init__(self) -> None:
        if (
            isinstance(self.max_identity_bytes, bool)
            or not isinstance(self.max_identity_bytes, int)
            or self.max_identity_bytes < 1
        ):
            raise ValueError("max_identity_bytes must be an integer >= 1")

    @property
    def canonical_v3_hashes(self) -> Mapping[str, str]:
        return CANONICAL_V3_IDENTITY_RULE_HASHES

    @property
    def max_identity_rule_hashes(self) -> int:
        return len(CANONICAL_V3_IDENTITY_RULE_HASHES)

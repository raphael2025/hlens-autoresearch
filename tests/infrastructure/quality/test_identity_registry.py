"""``CanonicalV3IdentityRegistry`` (ADR-0101 §5): the Dataset Quality join's public registry."""

from __future__ import annotations

from dataclasses import FrozenInstanceError
from types import MappingProxyType
from typing import Any

import pytest

from infrastructure.dataset.quality import QualityIdentityRegistry
from infrastructure.quality import report_v3
from infrastructure.quality.identity_registry import CanonicalV3IdentityRegistry
from tests.infrastructure.quality.test_report_v3 import _identity_hashes


def test_hashes_are_the_registered_canonical_v3_identity_hashes() -> None:
    registry = CanonicalV3IdentityRegistry(max_identity_bytes=4096)
    assert dict(registry.canonical_v3_hashes) == _identity_hashes()  # independently derived
    assert registry.canonical_v3_hashes is report_v3.CANONICAL_V3_IDENTITY_RULE_HASHES
    assert report_v3.CANONICAL_V3_IDENTITY_RULE_HASHES is report_v3._IDENTITY_RULE_HASHES
    assert list(registry.canonical_v3_hashes) == sorted(registry.canonical_v3_hashes)
    assert {"quality", "pit"} <= set(registry.canonical_v3_hashes)


def test_bound_matches_the_reporters_registered_count() -> None:
    registry = CanonicalV3IdentityRegistry(max_identity_bytes=1)
    assert registry.max_identity_rule_hashes == len(registry.canonical_v3_hashes)
    assert registry.max_identity_rule_hashes == report_v3._MAX_IDENTITY_RULE_HASHES


def test_satisfies_the_quality_identity_registry_protocol() -> None:
    registry: QualityIdentityRegistry = CanonicalV3IdentityRegistry(max_identity_bytes=8192)
    assert registry.max_identity_bytes == 8192
    assert registry.max_identity_rule_hashes > 0
    assert all(isinstance(v, str) and len(v) == 64 for v in registry.canonical_v3_hashes.values())


def test_the_mapping_is_read_only() -> None:
    registry = CanonicalV3IdentityRegistry(max_identity_bytes=1)
    assert isinstance(registry.canonical_v3_hashes, MappingProxyType)
    with pytest.raises(TypeError):
        registry.canonical_v3_hashes["quality"] = "0" * 64  # type: ignore[index]


def test_the_registry_is_frozen_and_value_equal() -> None:
    registry = CanonicalV3IdentityRegistry(max_identity_bytes=10)
    with pytest.raises(FrozenInstanceError):
        registry.max_identity_bytes = 11  # type: ignore[misc]
    assert registry == CanonicalV3IdentityRegistry(max_identity_bytes=10)
    assert registry != CanonicalV3IdentityRegistry(max_identity_bytes=11)


@pytest.mark.parametrize("value", [0, -1, True, False, 1.5, "8", None])
def test_the_identity_byte_bound_must_be_a_positive_integer(value: Any) -> None:
    with pytest.raises(ValueError, match="max_identity_bytes"):
        CanonicalV3IdentityRegistry(max_identity_bytes=value)


def test_the_byte_bound_has_no_default() -> None:
    with pytest.raises(TypeError):
        CanonicalV3IdentityRegistry()  # type: ignore[call-arg]

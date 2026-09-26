"""Helpers for pins taken before the 2.0.0 -> 2.1.0 minor bump (ADR-0052 §4).

The bump changes a schema published at 2.0.0 in exactly one way: the default of its envelope
``schema_version`` (and of an envelope inside a default nested object). Byte pins of such
schemas are therefore checked on the schema with that envelope default mapped back to 2.0.0:
any other change still breaks the pin. Likewise a content-hash pin taken at 2.0.0 is checked on
the 2.0.0 twin of the object (every envelope at 2.0.0, every other value identical): what the
2.0.0 code built from the same inputs.
"""

from __future__ import annotations

from typing import Any

from core.domain.base import CONTRACT_SCHEMA_VERSION, Contract

__all__ = ["PRE_BUMP_VERSION", "as_published_at_2_0_0", "at_pre_bump"]

PRE_BUMP_VERSION = "2.0.0"


def as_published_at_2_0_0(schema: bytes) -> bytes:
    """``schema`` with the current envelope default written back as 2.0.0 (and nothing else)."""
    current = CONTRACT_SCHEMA_VERSION.encode()
    old = PRE_BUMP_VERSION.encode()
    mapped = schema.replace(b'"default": "' + current + b'"', b'"default": "' + old + b'"')
    mapped = mapped.replace(
        b'"schema_version": "' + current + b'"', b'"schema_version": "' + old + b'"'
    )
    assert mapped != schema, "the schema carries no current-version envelope default"
    return mapped


def _envelopes_at(value: Any, version: str) -> Any:
    if isinstance(value, dict):
        return {
            key: (version if key == "schema_version" else _envelopes_at(item, version))
            for key, item in value.items()
        }
    if isinstance(value, list | tuple):
        return [_envelopes_at(item, version) for item in value]
    return value


def at_pre_bump[C: Contract](obj: C) -> C:
    """The 2.0.0 twin of ``obj``: every envelope (nested included) at 2.0.0, all else equal."""
    return type(obj).model_validate(_envelopes_at(obj.model_dump(), PRE_BUMP_VERSION))

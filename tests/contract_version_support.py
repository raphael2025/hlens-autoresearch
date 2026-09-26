"""Helpers for pins taken before the 2.0.0 -> 2.1.0 minor bump (ADR-0052 §4).

The bump changes a schema published at 2.0.0 in exactly one way: the default of its envelope
``schema_version`` (and of an envelope inside a default nested object). Byte pins of such
schemas are therefore checked on the schema with that envelope default mapped back to 2.0.0:
any other change still breaks the pin. Likewise a content-hash pin taken at 2.0.0 is checked on
the 2.0.0 twin of the object (every envelope at 2.0.0, every other value identical): what the
2.0.0 code built from the same inputs.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from core.domain.base import CONTRACT_SCHEMA_VERSION, Contract, contract_schema_version_scope

__all__ = [
    "PRE_BUMP_VERSION",
    "as_published_at",
    "as_published_at_2_0_0",
    "at_pre_bump",
    "at_version",
    "built_at",
    "built_at_pre_bump",
    "envelopes_at",
    "envelopes_at_pre_bump",
]

PRE_BUMP_VERSION = "2.0.0"


def as_published_at_2_0_0(schema: bytes) -> bytes:
    """``schema`` with the current envelope default written back as 2.0.0 (and nothing else)."""
    return as_published_at(schema, PRE_BUMP_VERSION)


def as_published_at(schema: bytes, version: str) -> bytes:
    """``schema`` with the current envelope default written back as ``version`` (nothing else):
    byte pins taken while ``version`` was current (ADR-0055: 2.1.0 pins after the 2.2.0 bump)."""
    current = CONTRACT_SCHEMA_VERSION.encode()
    old = version.encode()
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


def envelopes_at(data: Any, version: str) -> Any:
    """JSON-like ``data`` with every ``schema_version`` at ``version`` (ADR-0055: byte pins of
    reports taken while 2.1.0 was current)."""
    return _envelopes_at(data, version)


def envelopes_at_pre_bump(data: Any) -> Any:
    """JSON-like ``data`` (e.g. a ``to_dict()`` report) with every ``schema_version`` at 2.0.0.

    For byte pins of reports taken before the bump: the one difference the 2.1.0 envelope makes
    to such a report is its embedded envelopes; everything else must still match the pin.
    """
    return _envelopes_at(data, PRE_BUMP_VERSION)


def at_version[C: Contract](obj: C, version: str) -> C:
    """The ``version`` twin of ``obj``: every envelope (nested included) at ``version``."""
    return type(obj).model_validate(_envelopes_at(obj.model_dump(), version))


def at_pre_bump[C: Contract](obj: C) -> C:
    """The 2.0.0 twin of ``obj``: every envelope (nested included) at 2.0.0, all else equal."""
    return type(obj).model_validate(_envelopes_at(obj.model_dump(), PRE_BUMP_VERSION))


@contextmanager
def built_at_pre_bump() -> Iterator[str]:
    """Construct contract objects as the 2.0.0 code did (test only).

    Inside the block every contract object built without an explicit envelope is 2.0.0; objects
    built earlier (module constants) keep theirs, so pass them through ``at_pre_bump`` first.
    Used to show that a pin taken before the bump still describes what the 2.0.0 code builds.
    """
    with contract_schema_version_scope(PRE_BUMP_VERSION):
        yield PRE_BUMP_VERSION


@contextmanager
def built_at(version: str) -> Iterator[str]:
    """Construct contract objects as the ``version`` code did (test only; ADR-0055: pins taken
    under the 2.1.0 envelope are checked on objects built inside a 2.1.0 scope). Module constants
    built earlier keep their envelope: pass them through ``at_version`` first."""
    with contract_schema_version_scope(version):
        yield version

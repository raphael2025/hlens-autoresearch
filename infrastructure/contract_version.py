"""Versioned replay of persisted Phase 1 objects (ADR-0052 "Implementation note — versioned
replay").

Every persisted row / object is rebuilt, and compared, at the contract version it was **committed
with** (V1); a write group with no committed member is written at the current version (V2). The
recorded version of a group is a write-time fact, like its block base and ready time: it is read
back from the committed rows and never recomputed. The row builders take the version as an
explicit argument, and the row's ``contract_schema_version`` column is the envelope of the
contract record they build from it (one source, never two).

``core.domain.base.contract_schema_version_scope`` is used only where a *persisted* object is
rebuilt through code that constructs contract objects deep inside readers (a manifest re-derived
by the dataset builder); a new write group never runs inside such a scope (``new_group_version``
fails closed on a leaked scope).

``PHASE1_PUBLICATION_VERSION`` (V3): the Phase 1 rule bindings, source bindings and registered
specs are published objects whose content hashes persisted data binds; their envelope is part of
that identity and stays the version they were published under, whatever minor is current.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Final

from core.domain.base import (
    CONTRACT_SCHEMA_VERSION,
    PUBLISHED_CONTRACT_SCHEMA_VERSIONS,
    scoped_contract_schema_version,
)
from infrastructure.catalog.iceberg_adapter import CatalogIntegrityError

__all__ = [
    "PHASE1_PUBLICATION_VERSION",
    "ContractVersionScopeLeak",
    "new_group_version",
    "recorded_version",
    "replay_version",
]

#: The contract version every Phase 1 binding / registered spec was published under (V3).
PHASE1_PUBLICATION_VERSION: Final = "2.0.0"


class ContractVersionScopeLeak(RuntimeError):
    """A new write group was about to be written inside a replay scope (fail closed)."""


def new_group_version() -> str:
    """The contract version of a **new** write group (no committed member): the current one (V2).

    A new group is never written at a historical version: called inside a replay scope
    (``contract_schema_version_scope``) it raises ``ContractVersionScopeLeak`` instead.
    """
    scoped = scoped_contract_schema_version()
    if scoped is not None:
        raise ContractVersionScopeLeak(
            f"a new write group was started inside a replay scope of contract version {scoped}: "
            "new objects are written at the current version only"
        )
    return CONTRACT_SCHEMA_VERSION


def recorded_version(values: Iterable[object], *, what: str) -> str:
    """The one published contract version a committed write group records (V1), or fail closed.

    ``values`` are the ``contract_schema_version`` of every committed member of the group (a
    Canonical unit's rows, one response revision, ...). No value, two different values, or a
    version that is not in ``PUBLISHED_CONTRACT_SCHEMA_VERSIONS`` is ``CatalogIntegrityError``:
    a group is written at one version, and a version this code never published cannot be
    reproduced by it.
    """
    distinct = set(values)
    if len(distinct) != 1:
        found = sorted(repr(value) for value in distinct)
        raise CatalogIntegrityError(
            f"{what} records {len(distinct)} contract versions {found}: a write group has "
            "exactly one"
        )
    [version] = distinct
    if not isinstance(version, str) or version not in PUBLISHED_CONTRACT_SCHEMA_VERSIONS:
        raise CatalogIntegrityError(
            f"{what} records contract version {version!r}, not one of the published "
            f"{list(PUBLISHED_CONTRACT_SCHEMA_VERSIONS)}: it cannot be replayed"
        )
    return version


def replay_version(value: object, *, what: str) -> str:
    """``recorded_version`` of a single persisted row / object."""
    return recorded_version([value], what=what)

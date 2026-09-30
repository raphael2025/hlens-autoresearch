"""Scratch-storage isolation shared by the Quality reporters and the Dataset Quality join.

A scratch adapter that is a distinct object can still alias the evidence namespace: two local
adapters over the same (or nested) warehouse / staging directories write into one another. The
check follows ``inner`` wrappers down to every ``LocalFileStorageAdapter`` on both sides and
compares their resolved roots; non-local adapters carry no roots to compare and pass.
"""

from __future__ import annotations

from core.contracts.storage import StorageAdapter
from infrastructure.settings import local_file_uri_to_path
from infrastructure.storage.local import LocalFileStorageAdapter

__all__ = ["local_storage_roots_overlap"]


def _local_adapters(storage: StorageAdapter) -> tuple[LocalFileStorageAdapter, ...]:
    found: list[LocalFileStorageAdapter] = []
    pending: list[object] = [storage]
    seen: set[int] = set()
    while pending:
        current = pending.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        if isinstance(current, LocalFileStorageAdapter):
            found.append(current)
        else:
            inner = getattr(current, "inner", None)
            if inner is not None:
                pending.append(inner)
    return tuple(found)


def local_storage_roots_overlap(evidence: StorageAdapter, scratch: StorageAdapter) -> bool:
    """Whether a local root of ``scratch`` equals, contains or lies inside one of ``evidence``."""
    for a in _local_adapters(evidence):
        for b in _local_adapters(scratch):
            roots_a = (
                local_file_uri_to_path(a.warehouse_uri, field_name="warehouse_uri").resolve(),
                local_file_uri_to_path(a.staging_uri, field_name="staging_uri").resolve(),
            )
            roots_b = (
                local_file_uri_to_path(b.warehouse_uri, field_name="warehouse_uri").resolve(),
                local_file_uri_to_path(b.staging_uri, field_name="staging_uri").resolve(),
            )
            if any(x == y or x in y.parents or y in x.parents for x in roots_a for y in roots_b):
                return True
    return False

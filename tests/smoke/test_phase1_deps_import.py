"""Phase 1 A3a: import-smoke for approved data-plane direct dependencies."""

from __future__ import annotations


def test_phase1_direct_deps_importable() -> None:
    """The four §6.1 direct packages must import, with PyIceberg's locked official extras.

    ``pyiceberg_core`` is not a fifth top-level dependency: it is PyIceberg's official
    ``pyiceberg-core`` extra (ADR-0026), required to write ``day(...)`` partitions.
    """
    import httpx
    import pyarrow  # type: ignore[import-untyped]
    import pydantic_settings
    import pyiceberg
    import pyiceberg_core  # type: ignore[import-untyped]
    from pyiceberg.catalog.sql import SqlCatalog

    _ = (httpx, pyarrow, pydantic_settings, pyiceberg, pyiceberg_core, SqlCatalog)

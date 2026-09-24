"""Phase 1 A3a: import-smoke for approved data-plane direct dependencies."""

from __future__ import annotations


def test_phase1_direct_deps_importable() -> None:
    """The four §6.1 direct packages (and SqlCatalog extra) must import."""
    import httpx
    import pyarrow  # type: ignore[import-untyped]
    import pydantic_settings
    import pyiceberg
    from pyiceberg.catalog.sql import SqlCatalog

    _ = (httpx, pyarrow, pydantic_settings, pyiceberg, SqlCatalog)

"""Idempotently create the fourteen Phase 1 tables in the configured catalog (C3 operator entry).

Reads runtime ``Settings`` from the environment (``HLENS_CATALOG_URI`` etc.; load
``.env.catalog`` into the environment first, never echo it). Existing tables with the same
definition are verified and left unchanged; any drift fails closed with a non-zero exit. Prints
only table name, definition binding, partition spec and whether the table was created — never the
DSN. Usage::

    set -a; . ./.env.catalog; set +a
    uv run python -m infrastructure.catalog.create_phase1_tables
"""

from __future__ import annotations

import sys

from core.contracts.catalog import CatalogError
from infrastructure.catalog.iceberg_adapter import open_postgres_catalog_adapter
from infrastructure.catalog.phase1_tables import PHASE1_REGISTRY, ensure_phase1_tables
from infrastructure.settings import Settings

__all__ = ["main"]


def main() -> int:
    settings = Settings()  # type: ignore[call-arg]
    try:
        with open_postgres_catalog_adapter(settings, PHASE1_REGISTRY) as adapter:
            states = ensure_phase1_tables(adapter)
            for state in states:
                binding = state.definition
                status = "created" if state.created else "already present (verified)"
                print(
                    f"{state.table}\t{binding.definition_id}@{binding.version}\t"
                    f"hash={binding.definition_hash[:16]}\t{state.partition}\t{status}\t"
                    f"current_snapshot={state.current_snapshot_id}"
                )
    except CatalogError as exc:
        print(f"phase 1 table creation failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

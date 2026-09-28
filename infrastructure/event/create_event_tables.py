"""Explicitly create the Phase 3 Event table in the configured catalog (ADR-0066).

The command is inert by default. Use ``--apply`` to open the configured PostgreSQL catalog and
create or verify only ``event.events``; Phase 1 tables are included in the adapter's registry so
it can resolve their definitions, but this command never provisions them.

Usage::

    set -a; . ./.env.catalog; set +a
    uv run python -m infrastructure.event.create_event_tables --apply

Successful output includes the registered ``definition_id@version`` binding, its full content
hash, and whether the table was created or already present and verified.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence

from infrastructure.catalog.definitions import TableDefinitionRegistry
from infrastructure.catalog.iceberg_adapter import open_postgres_catalog_adapter
from infrastructure.catalog.phase1_tables import PHASE1_TABLES
from infrastructure.event.table_definition import EVENT_EVENTS, PHASE3_TABLES, ensure_event_tables
from infrastructure.settings import Settings

__all__ = ["main"]


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Explicitly create or verify only the Phase 3 event.events table."
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="open the configured PostgreSQL catalog and create or verify event.events",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    if not args.apply:
        parser.print_help()
        return 0

    registry = TableDefinitionRegistry(PHASE1_TABLES + PHASE3_TABLES)
    try:
        settings = Settings()  # type: ignore[call-arg]
        with open_postgres_catalog_adapter(settings, registry) as adapter:
            states = ensure_event_tables(adapter)
    except Exception as exc:
        # Settings and catalog errors can contain connection details. Report only the exception
        # type so operator logs never expose a DSN or credentials.
        print(f"event.events operation failed: {type(exc).__name__}", file=sys.stderr)
        return 1

    if tuple(state.table for state in states) != (EVENT_EVENTS.table,):
        print("event.events operation failed: unexpected table scope", file=sys.stderr)
        return 1

    for state in states:
        if state.definition != EVENT_EVENTS.binding:
            print(
                "event.events operation failed: table definition binding mismatch",
                file=sys.stderr,
            )
            return 1
        status = "created" if state.created else "already present (verified)"
        binding = f"{state.definition.definition_id}@{state.definition.version}"
        print(
            f"{state.table}\t{binding}\tdefinition_hash={state.definition.definition_hash}"
            f"\t{status}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

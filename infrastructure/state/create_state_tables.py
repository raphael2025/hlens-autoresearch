"""Explicitly create the Phase 2 State table (ADR-0089); inert unless ``--apply`` is passed."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence

from infrastructure.catalog.definitions import TableDefinitionRegistry
from infrastructure.catalog.iceberg_adapter import open_postgres_catalog_adapter
from infrastructure.catalog.phase1_tables import PHASE1_TABLES
from infrastructure.settings import Settings
from infrastructure.state.table_definition import (
    PHASE2_TABLES,
    STATE_STATES,
    ensure_state_tables,
)

__all__ = ["main"]


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Explicitly create or verify only the Phase 2 state.states table."
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="open the configured PostgreSQL catalog and create or verify state.states",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    if not args.apply:
        print(
            "Plan only: create or verify state.states "
            f"({STATE_STATES.definition_id}@{STATE_STATES.version}, "
            f"definition_hash={STATE_STATES.definition_hash}) with day(evaluation_time). "
            "No settings or catalog were read. Pass --apply to perform this operation."
        )
        return 0

    registry = TableDefinitionRegistry(PHASE1_TABLES + PHASE2_TABLES)
    try:
        settings = Settings()  # type: ignore[call-arg]
        with open_postgres_catalog_adapter(settings, registry) as adapter:
            states = ensure_state_tables(adapter)
    except Exception as exc:
        print(f"state.states operation failed: {type(exc).__name__}", file=sys.stderr)
        return 1

    if tuple(state.table for state in states) != (STATE_STATES.table,):
        print("state.states operation failed: unexpected table scope", file=sys.stderr)
        return 1
    for state in states:
        if state.definition != STATE_STATES.binding:
            print("state.states operation failed: definition binding mismatch", file=sys.stderr)
            return 1
        status = "created" if state.created else "already present (verified)"
        print(
            f"{state.table}\t{state.definition.definition_id}@{state.definition.version}"
            f"\tdefinition_hash={state.definition.definition_hash}\t{status}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

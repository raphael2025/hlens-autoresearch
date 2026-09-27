"""Operator entrypoint for the Phase 3 Event table (ADR-0066)."""

from __future__ import annotations

from contextlib import contextmanager
from types import SimpleNamespace
from typing import Any

from infrastructure.catalog.iceberg_adapter import CatalogUnavailable
from infrastructure.catalog.phase1_tables import PHASE1_TABLES
from infrastructure.event import create_event_tables
from infrastructure.event.table_definition import EVENT_EVENTS, PHASE3_TABLES


def test_default_invocation_shows_help_without_loading_settings_or_catalog(
    monkeypatch: Any, capsys: Any
) -> None:
    def unexpected() -> None:
        raise AssertionError("default invocation must not load Settings")

    monkeypatch.setattr(create_event_tables, "Settings", unexpected)
    monkeypatch.setattr(create_event_tables, "open_postgres_catalog_adapter", unexpected)

    assert create_event_tables.main([]) == 0

    output = capsys.readouterr()
    assert "--apply" in output.out
    assert output.err == ""


def test_apply_composes_registry_and_calls_only_event_table_ensurer(
    monkeypatch: Any, capsys: Any
) -> None:
    settings = object()
    adapter = object()
    observed: dict[str, Any] = {}

    monkeypatch.setattr(create_event_tables, "Settings", lambda: settings)

    @contextmanager
    def open_adapter(received_settings: object, registry: Any) -> Any:
        observed["settings"] = received_settings
        observed["registry"] = registry
        yield adapter

    def ensure(received_adapter: object) -> tuple[Any, ...]:
        observed["adapter"] = received_adapter
        return (SimpleNamespace(table=EVENT_EVENTS.table, created=True),)

    monkeypatch.setattr(create_event_tables, "open_postgres_catalog_adapter", open_adapter)
    monkeypatch.setattr(create_event_tables, "ensure_event_tables", ensure)

    assert create_event_tables.main(["--apply"]) == 0

    registry = observed["registry"]
    assert observed["settings"] is settings
    assert observed["adapter"] is adapter
    assert tuple(registry) == PHASE1_TABLES + PHASE3_TABLES
    assert tuple(definition.table for definition in PHASE3_TABLES) == ("event.events",)
    assert tuple(definition.table for definition in registry) == tuple(
        definition.table for definition in PHASE1_TABLES
    ) + ("event.events",)
    assert capsys.readouterr().out == "event.events\tcreated\n"


def test_apply_reports_only_event_status_for_existing_table(monkeypatch: Any, capsys: Any) -> None:
    adapter = object()

    @contextmanager
    def open_adapter(_settings: object, _registry: Any) -> Any:
        yield adapter

    monkeypatch.setattr(create_event_tables, "Settings", lambda: object())
    monkeypatch.setattr(create_event_tables, "open_postgres_catalog_adapter", open_adapter)
    monkeypatch.setattr(
        create_event_tables,
        "ensure_event_tables",
        lambda received: (SimpleNamespace(table="event.events", created=False),),
    )

    assert create_event_tables.main(["--apply"]) == 0
    assert capsys.readouterr().out == "event.events\talready present (verified)\n"


def test_apply_redacts_catalog_error(monkeypatch: Any, capsys: Any) -> None:
    secret_dsn = "postgresql://operator:secret@catalog.internal/hlens"

    @contextmanager
    def open_adapter(_settings: object, _registry: Any) -> Any:
        raise CatalogUnavailable(secret_dsn)
        yield  # pragma: no cover - keeps this a context manager

    monkeypatch.setattr(create_event_tables, "Settings", lambda: object())
    monkeypatch.setattr(create_event_tables, "open_postgres_catalog_adapter", open_adapter)

    assert create_event_tables.main(["--apply"]) == 1

    output = capsys.readouterr()
    assert output.out == ""
    assert "CatalogUnavailable" in output.err
    assert secret_dsn not in output.err

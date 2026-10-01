"""Safe explicit provisioning boundary for ``state.states`` (ADR-0089)."""

from __future__ import annotations

from contextlib import contextmanager
from types import SimpleNamespace
from typing import Any

from infrastructure.catalog.iceberg_adapter import CatalogUnavailable
from infrastructure.catalog.phase1_tables import PHASE1_TABLES
from infrastructure.state import create_state_tables
from infrastructure.state.table_definition import PHASE2_TABLES, STATE_STATES


def test_default_invocation_is_plan_only_and_never_loads_settings_or_catalog(
    monkeypatch: Any, capsys: Any
) -> None:
    def unexpected() -> None:
        raise AssertionError("plan-only invocation must not load settings/catalog")

    monkeypatch.setattr(create_state_tables, "Settings", unexpected)
    monkeypatch.setattr(create_state_tables, "open_postgres_catalog_adapter", unexpected)
    assert create_state_tables.main([]) == 0
    output = capsys.readouterr()
    assert "state.states" in output.out and "--apply" in output.out
    assert output.err == ""


def test_apply_composes_registry_but_ensures_only_state_tables(
    monkeypatch: Any, capsys: Any
) -> None:
    adapter = object()
    observed: dict[str, Any] = {}
    monkeypatch.setattr(create_state_tables, "Settings", lambda: object())

    @contextmanager
    def open_adapter(_settings: object, registry: Any) -> Any:
        observed["registry"] = registry
        yield adapter

    def ensure(received: object) -> tuple[Any, ...]:
        observed["adapter"] = received
        return (
            SimpleNamespace(
                table=STATE_STATES.table, definition=STATE_STATES.binding, created=True
            ),
        )

    monkeypatch.setattr(create_state_tables, "open_postgres_catalog_adapter", open_adapter)
    monkeypatch.setattr(create_state_tables, "ensure_state_tables", ensure)
    assert create_state_tables.main(["--apply"]) == 0
    assert observed["adapter"] is adapter
    assert tuple(observed["registry"]) == PHASE1_TABLES + PHASE2_TABLES
    assert capsys.readouterr().out.startswith("state.states\t")


def test_apply_redacts_catalog_error(monkeypatch: Any, capsys: Any) -> None:
    secret = "postgresql://state-test:test-password@example.invalid/catalog"

    @contextmanager
    def open_adapter(_settings: object, _registry: Any) -> Any:
        raise CatalogUnavailable(secret)
        yield  # pragma: no cover

    monkeypatch.setattr(create_state_tables, "Settings", lambda: object())
    monkeypatch.setattr(create_state_tables, "open_postgres_catalog_adapter", open_adapter)
    assert create_state_tables.main(["--apply"]) == 1
    output = capsys.readouterr()
    assert "CatalogUnavailable" in output.err and secret not in output.err
    assert output.out == ""

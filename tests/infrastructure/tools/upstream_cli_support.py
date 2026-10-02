"""Shared fixtures of the ADR-0101 §6 upstream entry tests (settings, catalog seam, secrets).

The catalog is a test harness's SQLite catalog served through ``cli_support``'s opener seam, so the
PostgreSQL DSN in the settings is never connected; the network is a strict in-memory venue.
"""

from __future__ import annotations

from contextlib import nullcontext
from pathlib import Path
from typing import Any, Final

import pytest
from pydantic import AnyHttpUrl, SecretStr

from infrastructure.settings import Settings
from infrastructure.storage import LocalFileStorageAdapter
from infrastructure.tools import cli_support

SECRET_DSN: Final = "postgresql://hlens_user:fake-s3cr3t-pw@db.invalid:5432/hlens"
SECRETS: Final = ("fake-s3cr3t-pw", "hlens_user", SECRET_DSN)


def settings_over(storage: LocalFileStorageAdapter, scratch: Path, origin: str) -> Settings:
    """Settings over a harness's warehouse; the DSN is a never-connected secret."""
    return Settings(  # type: ignore[call-arg]
        _env_file=None,
        catalog_uri=SecretStr(SECRET_DSN),
        warehouse_uri=storage.warehouse_uri,
        staging_uri=storage.staging_uri,
        canonical_scratch_uri=scratch.as_uri(),
        binance_market_data_base_url=AnyHttpUrl(origin),
        binance_rest_min_request_interval_ms=50,
        http_max_retries=0,
    )


def patch_catalog(monkeypatch: pytest.MonkeyPatch, adapter: Any) -> list[str]:
    """Serve ``adapter`` instead of a PostgreSQL catalog; log each open."""
    opened: list[str] = []

    def opener(settings: Settings, registry: Any) -> Any:
        opened.append(settings.catalog_name)
        return nullcontext(adapter)

    monkeypatch.setattr(cli_support, "open_postgres_catalog_adapter", opener)
    return opened


def refuse_catalog(monkeypatch: pytest.MonkeyPatch) -> None:
    """Any catalog open fails the test: for steps that must not open one."""

    def opener(settings: Settings, registry: Any) -> Any:
        raise AssertionError("this step must not open the catalog")

    monkeypatch.setattr(cli_support, "open_postgres_catalog_adapter", opener)


def unreachable_catalog(monkeypatch: pytest.MonkeyPatch) -> None:
    """A catalog open that fails with the DSN in its message (as a driver error would)."""

    def opener(settings: Settings, registry: Any) -> Any:
        raise OSError(f"could not connect to {settings.catalog_uri.get_secret_value()}")

    monkeypatch.setattr(cli_support, "open_postgres_catalog_adapter", opener)


def assert_no_secret(text: str) -> None:
    for secret in SECRETS:
        assert secret not in text, f"{secret!r} leaked"

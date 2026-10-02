"""Shared plumbing of the ADR-0101 §6 upstream operator entries (listing, REST tail, quality).

Settings come from the environment (``HLENS_*``) and are never printed: a settings error names
only the failing fields, a known step error is printed with every credential-bearing string of the
settings redacted, and an unexpected error prints only its exception type (the pattern of
``infrastructure.state.run_cli``). ``open_data_plane`` opens the PostgreSQL catalog and the
evidence warehouse and closes both in reverse; tests replace ``open_postgres_catalog_adapter`` in
this module so no DSN is ever connected.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from datetime import UTC, date, datetime
from typing import Any, Final
from urllib.parse import urlsplit

from pydantic import ValidationError

from infrastructure.catalog import PHASE1_REGISTRY
from infrastructure.catalog.iceberg_adapter import (
    PyIcebergCatalogAdapter,
    open_postgres_catalog_adapter,
)
from infrastructure.settings import Settings
from infrastructure.storage import LocalFileStorageAdapter

__all__ = [
    "EXIT_ENVIRONMENT",
    "EXIT_FAILED",
    "EXIT_OK",
    "EXIT_PROFILE",
    "EXIT_USAGE",
    "default_to_plan",
    "emit",
    "instant",
    "load_settings",
    "open_data_plane",
    "redact",
    "secrets_of",
    "settings_error",
    "utc_day",
]

EXIT_OK: Final = 0
EXIT_FAILED: Final = 1
EXIT_USAGE: Final = 2
EXIT_PROFILE: Final = 3
EXIT_ENVIRONMENT: Final = 4

_URL_CREDENTIALS: Final = re.compile(r"(?i)\b([a-z][a-z0-9+.\-]*://)[^\s/@]*@")


def instant(text: str) -> datetime:
    """An ``argparse`` type: an ISO 8601 instant with an explicit offset, normalised to UTC."""
    try:
        value = datetime.fromisoformat(text)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"not an ISO 8601 timestamp: {text!r}") from exc
    if value.tzinfo is None or value.utcoffset() is None:
        raise argparse.ArgumentTypeError(
            f"{text!r} must carry a UTC offset (e.g. 2025-06-01T00:00:00Z)"
        )
    return value.astimezone(UTC)


def utc_day(text: str) -> date:
    """An ``argparse`` type: one UTC calendar day ``YYYY-MM-DD``."""
    try:
        return date.fromisoformat(text)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"not a YYYY-MM-DD day: {text!r}") from exc


def default_to_plan(argv: Sequence[str] | None, commands: Sequence[str]) -> list[str]:
    """``argv`` with ``plan`` inserted when no subcommand is named (the default prints the plan)."""
    arguments = list(sys.argv[1:] if argv is None else argv)
    if any(token in ("-h", "--help") for token in arguments[:1]):
        return arguments
    if not arguments or arguments[0] not in commands:
        return ["plan", *arguments]
    return arguments


def load_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]


def settings_error(exc: ValidationError) -> str:
    fields = sorted({".".join(str(part) for part in error["loc"]) for error in exc.errors()})
    return f"settings are invalid: {', '.join(fields)}"


def secrets_of(settings: Settings) -> list[str]:
    """Every credential-bearing string of the settings: the DSN and its user / password parts."""
    dsn = settings.catalog_uri.get_secret_value()
    parts = urlsplit(dsn)
    return [dsn, *(part for part in (parts.password, parts.username) if part)]


def redact(text: str, secrets: Sequence[str]) -> str:
    for secret in secrets:
        if secret:
            text = text.replace(secret, "<redacted>")
    return _URL_CREDENTIALS.sub(r"\1<redacted>@", text)


def emit(document: Mapping[str, Any]) -> None:
    print(json.dumps(document, sort_keys=True, indent=2))


@contextmanager
def open_data_plane(
    settings: Settings,
) -> Iterator[tuple[PyIcebergCatalogAdapter, LocalFileStorageAdapter]]:
    """The Phase 1 catalog (tables must exist; nothing is created) and the evidence warehouse."""
    with LocalFileStorageAdapter.from_settings(settings) as storage:
        with open_postgres_catalog_adapter(settings, PHASE1_REGISTRY) as adapter:
            yield adapter, storage

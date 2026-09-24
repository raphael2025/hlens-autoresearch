"""Unit tests for infrastructure.Settings (03-data.md §6.2 / acceptance #3)."""

from __future__ import annotations

import ast
import os
from pathlib import Path

import pytest
from pydantic import ValidationError

from infrastructure import Settings
from infrastructure import settings as settings_mod

VALID_CATALOG = "postgresql://hlens:fake-password@127.0.0.1:5432/hlens_catalog"
REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_WAREHOUSE = (REPO_ROOT / "data" / "warehouse").resolve().as_uri()
DEFAULT_STAGING = (REPO_ROOT / "data" / "warehouse" / "staging").resolve().as_uri()
_THIS_FILE = Path(__file__).resolve()


@pytest.fixture(autouse=True)
def _isolate_hlens_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in list(os.environ):
        if key.startswith("HLENS_"):
            monkeypatch.delenv(key, raising=False)


def _settings(monkeypatch: pytest.MonkeyPatch, **env: str) -> Settings:
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    return Settings(_env_file=None)  # type: ignore[call-arg]


def test_defaults_with_only_catalog_dsn(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = _settings(monkeypatch, HLENS_CATALOG_URI=VALID_CATALOG)
    assert settings.warehouse_uri == DEFAULT_WAREHOUSE
    assert settings.staging_uri == DEFAULT_STAGING
    assert settings.catalog_name == "hlens"
    assert settings.http_connect_timeout_seconds == 10
    assert settings.http_read_timeout_seconds == 60
    assert settings.http_max_retries == 5
    assert settings.http_user_agent == "hlens-autoresearch/0.0.0"
    assert str(settings.binance_archive_base_url) == "https://data.binance.vision/"
    assert str(settings.binance_market_data_base_url) == "https://data-api.binance.vision/"
    assert settings.catalog_uri.get_secret_value() == VALID_CATALOG


def test_exact_uppercase_environment_overrides(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    warehouse = (tmp_path / "wh").resolve()
    staging = (tmp_path / "wh" / "staging").resolve()
    settings = _settings(
        monkeypatch,
        HLENS_CATALOG_URI="postgresql+psycopg2://u:p@localhost:5432/db",
        HLENS_WAREHOUSE_URI=warehouse.as_uri(),
        HLENS_STAGING_URI=staging.as_uri(),
        HLENS_CATALOG_NAME="  research  ",
        HLENS_HTTP_CONNECT_TIMEOUT_SECONDS="3.5",
        HLENS_HTTP_READ_TIMEOUT_SECONDS="12",
        HLENS_HTTP_MAX_RETRIES="0",
        HLENS_HTTP_USER_AGENT="  agent/1  ",
        HLENS_BINANCE_ARCHIVE_BASE_URL="https://example.test/archive",
        HLENS_BINANCE_MARKET_DATA_BASE_URL="https://example.test/market",
    )
    assert settings.warehouse_uri == warehouse.as_uri()
    assert settings.staging_uri == staging.as_uri()
    assert settings.catalog_name == "research"
    assert settings.http_connect_timeout_seconds == 3.5
    assert settings.http_read_timeout_seconds == 12
    assert settings.http_max_retries == 0
    assert settings.http_user_agent == "agent/1"
    assert str(settings.binance_archive_base_url) == "https://example.test/archive"
    assert str(settings.binance_market_data_base_url) == "https://example.test/market"
    assert settings.catalog_uri.get_secret_value() == "postgresql+psycopg2://u:p@localhost:5432/db"


@pytest.mark.parametrize(
    "catalog",
    [
        pytest.param(None, id="missing"),
        pytest.param("", id="blank"),
        pytest.param("   ", id="whitespace"),
        pytest.param("sqlite:///:memory:", id="sqlite"),
        pytest.param("mysql://localhost/db", id="non-postgresql"),
        pytest.param("hlens", id="missing-scheme"),
        pytest.param("postgresql+://127.0.0.1:5432/db", id="postgresql-plus-no-driver"),
    ],
)
def test_catalog_uri_rejected(
    monkeypatch: pytest.MonkeyPatch,
    catalog: str | None,
) -> None:
    env: dict[str, str] = {}
    if catalog is not None:
        env["HLENS_CATALOG_URI"] = catalog
    with pytest.raises(ValidationError) as exc_info:
        _settings(monkeypatch, **env)
    text = str(exc_info.value)
    if catalog and catalog.strip():
        assert catalog.strip() not in text
        assert "fake-password" not in text


def test_catalog_secret_redacted_from_repr_and_str(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(monkeypatch, HLENS_CATALOG_URI=VALID_CATALOG)
    assert "fake-password" not in repr(settings)
    assert "fake-password" not in str(settings)
    assert VALID_CATALOG not in repr(settings)
    assert VALID_CATALOG not in str(settings)


@pytest.mark.parametrize(
    "uri",
    [
        "file:///mnt",
        "file:///mnt/data",
        "s3://bucket/key",
        "file:relative/path",
        "file:///tmp/warehouse?x=1",
        "file:///tmp/warehouse#frag",
        "file://remotehost/tmp/warehouse",
    ],
)
def test_warehouse_uri_rejects_unsafe_forms(
    monkeypatch: pytest.MonkeyPatch,
    uri: str,
) -> None:
    with pytest.raises(ValidationError):
        _settings(monkeypatch, HLENS_CATALOG_URI=VALID_CATALOG, HLENS_WAREHOUSE_URI=uri)


@pytest.mark.parametrize(
    "uri",
    [
        "file:///mnt",
        "file:///mnt/staging",
        "https://example.test/staging",
        "file:relative/staging",
        "file:///tmp/staging?x=1",
        "file:///tmp/staging#frag",
        "file://remotehost/tmp/staging",
    ],
)
def test_staging_uri_rejects_unsafe_forms(
    monkeypatch: pytest.MonkeyPatch,
    uri: str,
    tmp_path: Path,
) -> None:
    warehouse = (tmp_path / "wh").resolve().as_uri()
    with pytest.raises(ValidationError):
        _settings(
            monkeypatch,
            HLENS_CATALOG_URI=VALID_CATALOG,
            HLENS_WAREHOUSE_URI=warehouse,
            HLENS_STAGING_URI=uri,
        )


def test_staging_rejects_cross_filesystem(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    warehouse = (tmp_path / "wh").resolve()
    staging = (tmp_path / "st").resolve()

    def fake_device(path: Path) -> int:
        return 1 if path == warehouse or warehouse in path.parents else 2

    monkeypatch.setattr(settings_mod, "filesystem_device_id", fake_device)
    with pytest.raises(ValidationError, match="same filesystem"):
        _settings(
            monkeypatch,
            HLENS_CATALOG_URI=VALID_CATALOG,
            HLENS_WAREHOUSE_URI=warehouse.as_uri(),
            HLENS_STAGING_URI=staging.as_uri(),
        )


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("HLENS_HTTP_CONNECT_TIMEOUT_SECONDS", "0"),
        ("HLENS_HTTP_CONNECT_TIMEOUT_SECONDS", "-1"),
        ("HLENS_HTTP_READ_TIMEOUT_SECONDS", "0"),
        ("HLENS_HTTP_READ_TIMEOUT_SECONDS", "-0.1"),
        ("HLENS_HTTP_MAX_RETRIES", "-1"),
        ("HLENS_CATALOG_NAME", "   "),
        ("HLENS_HTTP_USER_AGENT", ""),
    ],
)
def test_numeric_bounds_and_nonblank_strings(
    monkeypatch: pytest.MonkeyPatch,
    key: str,
    value: str,
) -> None:
    with pytest.raises(ValidationError):
        _settings(monkeypatch, HLENS_CATALOG_URI=VALID_CATALOG, **{key: value})


def test_binance_public_endpoint_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = _settings(monkeypatch, HLENS_CATALOG_URI=VALID_CATALOG)
    assert str(settings.binance_archive_base_url).rstrip("/") == ("https://data.binance.vision")
    assert str(settings.binance_market_data_base_url).rstrip("/") == (
        "https://data-api.binance.vision"
    )


def test_settings_source_has_no_pytest_or_sqlite_branch() -> None:
    source = Path(settings_mod.__file__).read_text(encoding="utf-8")
    lowered = source.lower()
    assert "pytest" not in lowered
    assert "sqlite" not in lowered
    tree = ast.parse(source)
    forbidden = {"pytest", "PYTEST_CURRENT_TEST", "PYTEST_VERSION"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            assert node.value not in forbidden
        if isinstance(node, ast.Name):
            assert node.id not in {"pytest", "PYTEST_CURRENT_TEST"}


def test_inprocess_settings_suite_does_not_leak_hlens_env() -> None:
    """Reproduce Codex probe: nested pytest.main must not leave HLENS_* behind."""
    for key in [key for key in os.environ if key.startswith("HLENS_")]:
        del os.environ[key]
    assert not any(key.startswith("HLENS_") for key in os.environ)

    exit_code = pytest.main(
        [
            str(_THIS_FILE),
            "-q",
            "-k",
            "not inprocess_settings_suite_does_not_leak_hlens_env",
        ]
    )
    assert exit_code == 0
    leaked = sorted(key for key in os.environ if key.startswith("HLENS_"))
    assert leaked == []

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
DEFAULT_CANONICAL_SCRATCH = (REPO_ROOT / "data" / "scratch").resolve().as_uri()
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
    assert settings.canonical_scratch_uri == DEFAULT_CANONICAL_SCRATCH
    assert settings.canonical_scratch_path == REPO_ROOT / "data" / "scratch"
    assert settings.catalog_name == "hlens"
    assert settings.http_connect_timeout_seconds == 10
    assert settings.http_read_timeout_seconds == 60
    assert settings.http_max_retries == 5
    assert settings.http_user_agent == "hlens-autoresearch/0.0.0"
    assert str(settings.binance_archive_base_url) == "https://data.binance.vision/"
    assert str(settings.binance_market_data_base_url) == "https://data-api.binance.vision/"
    assert settings.binance_rest_max_pages_per_collect == 200
    assert settings.binance_rest_min_request_interval_ms == 250
    assert settings.binance_rest_max_retry_after_seconds == 60
    assert settings.binance_rest_max_response_bytes == 8388608
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
        HLENS_CANONICAL_SCRATCH_URI=(tmp_path / "scratch").as_uri(),
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
    assert settings.canonical_scratch_path == (tmp_path / "scratch").resolve()
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


@pytest.mark.parametrize(
    "uri",
    [
        "file:///mnt",
        "file:///mnt/scratch",
        "https://example.test/scratch",
        "file:relative/scratch",
        "file:///tmp/scratch?x=1",
        "file:///tmp/scratch#frag",
        "file://remotehost/tmp/scratch",
    ],
)
def test_canonical_scratch_uri_rejects_unsafe_forms(
    monkeypatch: pytest.MonkeyPatch,
    uri: str,
) -> None:
    with pytest.raises(ValidationError):
        _settings(
            monkeypatch,
            HLENS_CATALOG_URI=VALID_CATALOG,
            HLENS_CANONICAL_SCRATCH_URI=uri,
        )


@pytest.mark.parametrize("overlap", ["same", "nested", "parent"])
def test_canonical_scratch_uri_rejects_warehouse_overlap(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    overlap: str,
) -> None:
    warehouse = (tmp_path / "warehouse").resolve()
    scratch = {
        "same": warehouse,
        "nested": warehouse / "scratch",
        "parent": tmp_path,
    }[overlap]
    with pytest.raises(ValidationError, match="must not overlap warehouse_uri"):
        _settings(
            monkeypatch,
            HLENS_CATALOG_URI=VALID_CATALOG,
            HLENS_WAREHOUSE_URI=warehouse.as_uri(),
            HLENS_CANONICAL_SCRATCH_URI=scratch.as_uri(),
        )


@pytest.mark.parametrize("overlap", ["same", "nested", "parent"])
def test_canonical_scratch_uri_rejects_staging_overlap(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    overlap: str,
) -> None:
    warehouse = (tmp_path / "warehouse").resolve()
    staging = (tmp_path / "staging").resolve()
    scratch = {
        "same": staging,
        "nested": staging / "scratch",
        "parent": tmp_path,
    }[overlap]
    with pytest.raises(ValidationError, match="must not overlap"):
        _settings(
            monkeypatch,
            HLENS_CATALOG_URI=VALID_CATALOG,
            HLENS_WAREHOUSE_URI=warehouse.as_uri(),
            HLENS_STAGING_URI=staging.as_uri(),
            HLENS_CANONICAL_SCRATCH_URI=scratch.as_uri(),
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


#: ADR-0027 §12 / 03-data.md §6.2: env var → (lowest accepted, highest accepted).
REST_BOUNDS: dict[str, tuple[int, int]] = {
    "HLENS_BINANCE_REST_MAX_PAGES_PER_COLLECT": (1, 5000),
    "HLENS_BINANCE_REST_MIN_REQUEST_INTERVAL_MS": (50, 60000),
    "HLENS_BINANCE_REST_MAX_RETRY_AFTER_SECONDS": (1, 3600),
    "HLENS_BINANCE_REST_MAX_RESPONSE_BYTES": (65536, 67108864),
}
REST_FIELDS: dict[str, str] = {
    "HLENS_BINANCE_REST_MAX_PAGES_PER_COLLECT": "binance_rest_max_pages_per_collect",
    "HLENS_BINANCE_REST_MIN_REQUEST_INTERVAL_MS": "binance_rest_min_request_interval_ms",
    "HLENS_BINANCE_REST_MAX_RETRY_AFTER_SECONDS": "binance_rest_max_retry_after_seconds",
    "HLENS_BINANCE_REST_MAX_RESPONSE_BYTES": "binance_rest_max_response_bytes",
}


@pytest.mark.parametrize("key", sorted(REST_BOUNDS))
def test_rest_settings_accept_both_frozen_bounds(monkeypatch: pytest.MonkeyPatch, key: str) -> None:
    low, high = REST_BOUNDS[key]
    for value in (low, high):
        settings = _settings(monkeypatch, HLENS_CATALOG_URI=VALID_CATALOG, **{key: str(value)})
        assert getattr(settings, REST_FIELDS[key]) == value


@pytest.mark.parametrize("key", sorted(REST_BOUNDS))
def test_rest_settings_reject_values_outside_the_frozen_bounds(
    monkeypatch: pytest.MonkeyPatch, key: str
) -> None:
    low, high = REST_BOUNDS[key]
    for value in (str(low - 1), str(high + 1), "0", "-1", "1.5", "", "many"):
        with pytest.raises(ValidationError):
            _settings(monkeypatch, HLENS_CATALOG_URI=VALID_CATALOG, **{key: value})


def test_rest_settings_take_exact_uppercase_environment_overrides(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(
        monkeypatch,
        HLENS_CATALOG_URI=VALID_CATALOG,
        HLENS_BINANCE_REST_MAX_PAGES_PER_COLLECT="7",
        HLENS_BINANCE_REST_MIN_REQUEST_INTERVAL_MS="900",
        HLENS_BINANCE_REST_MAX_RETRY_AFTER_SECONDS="5",
        HLENS_BINANCE_REST_MAX_RESPONSE_BYTES="131072",
    )
    assert settings.binance_rest_max_pages_per_collect == 7
    assert settings.binance_rest_min_request_interval_ms == 900
    assert settings.binance_rest_max_retry_after_seconds == 5
    assert settings.binance_rest_max_response_bytes == 131072


def test_rest_settings_reuse_the_existing_http_and_base_url_fields() -> None:
    """ADR-0027 §12 forbids a second timeout / retry / user-agent / base-URL definition."""
    fields = set(Settings.model_fields)
    assert set(REST_FIELDS.values()) <= fields
    duplicates = {
        name
        for name in fields
        if name.startswith("binance_rest_")
        and any(token in name for token in ("timeout", "retries", "user_agent", "base_url"))
    }
    assert duplicates == set()


def test_the_collector_bounds_are_the_settings_bounds() -> None:
    from infrastructure.collector import binance_rest as rest

    assert (rest.MIN_PAGES_PER_COLLECT, rest.MAX_PAGES_PER_COLLECT) == (1, 5000)
    assert (rest.MIN_REQUEST_INTERVAL_MS, rest.MAX_REQUEST_INTERVAL_MS) == (50, 60000)
    assert (rest.MIN_RETRY_AFTER_SECONDS, rest.MAX_RETRY_AFTER_SECONDS) == (1, 3600)
    assert (rest.MIN_RESPONSE_BYTES, rest.MAX_RESPONSE_BYTES) == (65536, 67108864)


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

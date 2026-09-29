"""Typed runtime settings for the local data plane (03-data.md §6.2)."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Self
from urllib.parse import unquote, urlparse

from pydantic import (
    AnyHttpUrl,
    Field,
    SecretStr,
    field_validator,
    model_validator,
)
from pydantic_settings import BaseSettings, SettingsConfigDict

_REPO_ROOT = Path(__file__).resolve().parents[1]
_DEFAULT_WAREHOUSE_PATH = _REPO_ROOT / "data" / "warehouse"
_DEFAULT_CANONICAL_SCRATCH_PATH = _REPO_ROOT / "data" / "scratch"
_MNT_ROOT = Path("/mnt")


def default_warehouse_uri() -> str:
    """Absolute file:// URI of this repository's data/warehouse (cwd-independent)."""
    return _DEFAULT_WAREHOUSE_PATH.resolve().as_uri()


def default_staging_uri(data: dict[str, Any]) -> str:
    """Derive ``<warehouse_uri>/staging`` from already-validated warehouse data."""
    warehouse_path = local_file_uri_to_path(
        data["warehouse_uri"],
        field_name="warehouse_uri",
    )
    return (warehouse_path / "staging").as_uri()


def default_canonical_scratch_uri() -> str:
    """Absolute file URI of repository-owned, persistent canonical scratch storage."""
    return _DEFAULT_CANONICAL_SCRATCH_PATH.resolve().as_uri()


def _reject_storage_path_overlap(scratch: Path, warehouse: Path, staging: Path) -> None:
    for name, path in (("warehouse_uri", warehouse), ("staging_uri", staging)):
        if scratch == path or scratch in path.parents or path in scratch.parents:
            msg = f"canonical_scratch_uri must not overlap {name}"
            raise ValueError(msg)


def filesystem_device_id(path: Path) -> int:
    """Return st_dev of path or its nearest existing ancestor (read-only; no creation)."""
    candidate = path if path.is_absolute() else path.resolve(strict=False)
    while True:
        if candidate.exists():
            return candidate.stat().st_dev
        parent = candidate.parent
        if parent == candidate:
            msg = f"no existing ancestor for path: {path}"
            raise ValueError(msg)
        candidate = parent


def local_file_uri_to_path(uri: str, *, field_name: str) -> Path:
    """Parse a local absolute file:// URI into a Path; reject unsafe forms."""
    parsed = urlparse(uri)
    if parsed.scheme != "file":
        msg = f"{field_name} must use the file:// scheme"
        raise ValueError(msg)
    if parsed.query or parsed.fragment:
        msg = f"{field_name} must not include a query or fragment"
        raise ValueError(msg)
    if parsed.netloc:
        msg = f"{field_name} must be a local file URI without an authority"
        raise ValueError(msg)
    raw_path = unquote(parsed.path)
    path = Path(raw_path)
    if not path.is_absolute():
        msg = f"{field_name} must be an absolute file:// URI"
        raise ValueError(msg)
    resolved = path.resolve(strict=False)
    if resolved == _MNT_ROOT or _MNT_ROOT in resolved.parents:
        msg = f"{field_name} must not point at /mnt or any descendant"
        raise ValueError(msg)
    return resolved


def _is_postgresql_scheme(scheme: str) -> bool:
    if scheme == "postgresql":
        return True
    if scheme.startswith("postgresql+") and scheme != "postgresql+":
        return bool(scheme.removeprefix("postgresql+"))
    return False


def _validate_postgresql_secret(value: SecretStr) -> SecretStr:
    raw = value.get_secret_value().strip()
    if not raw:
        msg = "catalog_uri must not be blank"
        raise ValueError(msg)
    scheme = urlparse(raw).scheme.lower()
    if _is_postgresql_scheme(scheme):
        return SecretStr(raw)
    msg = "catalog_uri must use postgresql or postgresql+<driver>"
    raise ValueError(msg)


class Settings(BaseSettings):
    """Phase 1 typed settings boundary (no DB/network/directory side effects)."""

    model_config = SettingsConfigDict(
        env_prefix="HLENS_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        hide_input_in_errors=True,
        validate_default=True,
    )

    warehouse_uri: str = Field(default_factory=default_warehouse_uri)
    staging_uri: str = Field(default_factory=default_staging_uri)
    canonical_scratch_uri: str = Field(default_factory=default_canonical_scratch_uri)
    catalog_uri: SecretStr
    catalog_name: str = "hlens"
    http_connect_timeout_seconds: float = Field(default=10, gt=0)
    http_read_timeout_seconds: float = Field(default=60, gt=0)
    http_max_retries: int = Field(default=5, ge=0)
    http_user_agent: str = "hlens-autoresearch/0.0.0"
    binance_archive_base_url: AnyHttpUrl = Field(
        default_factory=lambda: AnyHttpUrl("https://data.binance.vision"),
    )
    binance_market_data_base_url: AnyHttpUrl = Field(
        default_factory=lambda: AnyHttpUrl("https://data-api.binance.vision"),
    )
    # REST collection operational bounds (ADR-0027 §12; 03-data.md §6.2). These are operational
    # safety parameters, not Constitution or Validation Profile thresholds.
    binance_rest_max_pages_per_collect: int = Field(default=200, ge=1, le=5000)
    binance_rest_min_request_interval_ms: int = Field(default=250, ge=50, le=60000)
    binance_rest_max_retry_after_seconds: int = Field(default=60, ge=1, le=3600)
    binance_rest_max_response_bytes: int = Field(default=8388608, ge=65536, le=67108864)

    @field_validator("catalog_uri", mode="before")
    @classmethod
    def _catalog_uri_to_secret(cls, value: object) -> SecretStr:
        if isinstance(value, SecretStr):
            return value
        if value is None:
            return SecretStr("")
        return SecretStr(str(value))

    @field_validator("catalog_uri", mode="after")
    @classmethod
    def _catalog_uri_postgresql_only(cls, value: SecretStr) -> SecretStr:
        return _validate_postgresql_secret(value)

    @field_validator("catalog_name", "http_user_agent", mode="before")
    @classmethod
    def _strip_nonblank(cls, value: object) -> object:
        if isinstance(value, str):
            stripped = value.strip()
            if not stripped:
                msg = "must not be blank"
                raise ValueError(msg)
            return stripped
        return value

    @field_validator("warehouse_uri", mode="after")
    @classmethod
    def _validate_warehouse_uri(cls, value: str) -> str:
        local_file_uri_to_path(value, field_name="warehouse_uri")
        return value

    @field_validator("staging_uri", mode="after")
    @classmethod
    def _validate_staging_uri(cls, value: str) -> str:
        local_file_uri_to_path(value, field_name="staging_uri")
        return value

    @field_validator("canonical_scratch_uri", mode="after")
    @classmethod
    def _validate_canonical_scratch_uri(cls, value: str) -> str:
        local_file_uri_to_path(value, field_name="canonical_scratch_uri")
        return value

    @model_validator(mode="after")
    def _same_filesystem(self) -> Self:
        warehouse_path = local_file_uri_to_path(
            self.warehouse_uri,
            field_name="warehouse_uri",
        )
        staging_path = local_file_uri_to_path(
            self.staging_uri,
            field_name="staging_uri",
        )
        scratch_path = local_file_uri_to_path(
            self.canonical_scratch_uri,
            field_name="canonical_scratch_uri",
        )
        if filesystem_device_id(warehouse_path) != filesystem_device_id(staging_path):
            msg = "staging_uri and warehouse_uri must be on the same filesystem"
            raise ValueError(msg)
        _reject_storage_path_overlap(scratch_path, warehouse_path, staging_path)
        return self

    @property
    def canonical_scratch_path(self) -> Path:
        return local_file_uri_to_path(
            self.canonical_scratch_uri,
            field_name="canonical_scratch_uri",
        )

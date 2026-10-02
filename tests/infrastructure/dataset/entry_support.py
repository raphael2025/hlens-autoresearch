"""Shared fixtures of the ADR-0101 production-entry tests (profile, settings, v3-ready world).

Every number is an arbitrary small value (DQ-9 OPEN; not a capacity choice), the same scale as the
v3 Quality / Dataset integration tests. The catalog is the world's SQLite harness; the real
PostgreSQL opener is replaced by ``patch_catalog`` so no DSN is ever connected.
"""

from __future__ import annotations

import copy
import json
from contextlib import nullcontext
from pathlib import Path
from typing import Any, Final

import pytest
from pydantic import AnyHttpUrl, SecretStr

from infrastructure.catalog.phase1_tables import (
    BINANCE_SPOT_EXCHANGE_INFO,
    CANONICAL_INSTRUMENT_LISTINGS,
)
from infrastructure.dataset import factory
from infrastructure.dataset.profile import DatasetBuildProfile, load_dataset_profile
from infrastructure.settings import Settings
from tests.infrastructure.dataset import dataset_support as ds
from tests.infrastructure.dataset.dataset_support import World
from tests.infrastructure.dataset.test_quality_v3_dataset_integration import (
    _canonical_reporter,
    _scratch,
)
from tests.infrastructure.quality.test_listing_report_v2 import _reporter
from tests.infrastructure.revision.rest_store_support import DAY

SECRET_DSN: Final = "postgresql://hlens_user:s3cr3t-pw@db.invalid:5432/hlens"

PROFILE_DOCUMENT: Final[dict[str, Any]] = {
    "schema_version": "1.0.0",
    "rule": {"chunk_rows": 2, "leaf_max_records": 2, "leaf_max_bytes": 16384, "fanout": 2},
    "pit": {
        "row_batch_rows": 1,
        "edge_batch_rows": 1,
        "merge_fanout": 2,
        "key_history_buffer": 1,
        "run_limits": {"leaf_max_records": 1, "leaf_max_bytes": 65536, "fanout": 2},
    },
    "universe": {
        "capacity": 2,
        "merge_fanout": 3,
        "run_limits": {"leaf_max_records": 8, "leaf_max_bytes": 4096, "fanout": 3},
    },
    "quality": {
        "stream_limits": {"leaf_max_records": 2, "leaf_max_bytes": 8192, "fanout": 2},
        "run_limits": {"leaf_max_records": 2, "leaf_max_bytes": 4096, "fanout": 2},
        "run_capacity": 2,
        "merge_fanout": 2,
        "max_run_object_bytes": 8192,
        "max_identity_bytes": 8192,
        "reporter": {
            "max_event_record_bytes": 4096,
            "max_revision_record_bytes": 1024,
            "max_gap_record_bytes": 4096,
            "max_input_record_bytes": 8192,
            "max_manifest_record_bytes": 32768,
            "retries": 2,
        },
    },
}


#: The ADR-0101 修订 1 §2 section (schema 1.1.0+); the values mirror the listing v2 test reporter.
LISTING_QUALITY_DOCUMENT: Final[dict[str, Any]] = {
    "metadata": {
        "max_metadata_bytes": 16 * 1024 * 1024,
        "max_item_bytes": 256 * 1024,
        "max_retained_json_bytes": 2 * 1024 * 1024,
        "read_chunk_bytes": 16 * 1024,
        "max_small_array_items": 512,
        "max_map_items": 512,
        "max_snapshots": 5000,
        "run_capacity": 64,
        "run_merge_fanout": 8,
        "run_limits": {"leaf_max_records": 32, "leaf_max_bytes": 1024 * 1024, "fanout": 8},
        "key_tree": {"page_max_bytes": 1024 * 1024, "leaf_max_records": 64, "fanout": 8},
    },
    "max_record_bytes": 16384,
    "prefix_leaf_max_records": 64,
    "prefix_fanout": 2,
    "prefix_max_node_bytes": 2 * 1024 * 1024,
    "prefix_max_record_bytes": 16 * 1024,
    "row_chunk_capacity": 2,
    "max_hash_chunk_bytes": 64 * 1024,
}


def profile_document() -> dict[str, Any]:
    return copy.deepcopy(PROFILE_DOCUMENT)


def listing_profile_document() -> dict[str, Any]:
    """A schema 1.1.0 profile with the ``listing_quality`` section."""
    document = profile_document()
    document["schema_version"] = "1.1.0"
    document["listing_quality"] = copy.deepcopy(LISTING_QUALITY_DOCUMENT)
    return document


def write_profile(path: Path, document: Any) -> Path:
    path.write_text(json.dumps(document), encoding="utf-8")
    return path


def make_profile(tmp_path: Path, **top_level: Any) -> tuple[Path, DatasetBuildProfile]:
    document = profile_document()
    document.update(top_level)
    path = write_profile(tmp_path / "profile.json", document)
    return path, load_dataset_profile(path)


def settings_for(w: World) -> Settings:
    """Settings over the world's own directories; the DSN is a never-connected secret."""
    return Settings(  # type: ignore[call-arg]
        _env_file=None,
        catalog_uri=SecretStr(SECRET_DSN),
        warehouse_uri=w.h.storage.warehouse_uri,
        staging_uri=w.h.storage.staging_uri,
        canonical_scratch_uri=w.h.canonical_scratch_directory.as_uri(),
        binance_market_data_base_url=AnyHttpUrl(ds.ORIGIN),
    )


def patch_catalog(monkeypatch: pytest.MonkeyPatch, w: World) -> list[str]:
    """Serve the world's SQLite adapter instead of a PostgreSQL catalog; log each open."""
    opened: list[str] = []

    def opener(settings: Settings, registry: Any) -> Any:
        opened.append(settings.catalog_name)
        return nullcontext(w.h.adapter)

    monkeypatch.setattr(factory, "open_postgres_catalog_adapter", opener)
    return opened


def v3_ready(w: World, tmp_path: Path) -> None:
    """Listings, BTC trades and the v3 canonical + v2 listing quality reports a v3 build joins."""
    w.listed()
    w.trades()
    w.report()
    canonical_scratch = _scratch(tmp_path, "report-canonical")
    listing_scratch = _scratch(tmp_path, "report-listing")
    try:
        for symbol in ("BTCUSDT", "ETHUSDT"):
            _canonical_reporter(
                w.h.adapter,
                w.h.storage,
                canonical_scratch,
                lambda: ds.K_Q,
                w.h.canonical_scratch_directory,
            ).report("agg_trades", symbol, DAY)
        listing = w.h.head(CANONICAL_INSTRUMENT_LISTINGS.table)
        raw = w.h.head(BINANCE_SPOT_EXCHANGE_INFO.table)
        assert listing is not None and raw is not None
        _reporter(
            w.x, listing_scratch, lambda: ds.K_Q, adapter=w.h.adapter, evidence=w.h.storage
        ).report(
            {CANONICAL_INSTRUMENT_LISTINGS.table: listing, BINANCE_SPOT_EXCHANGE_INFO.table: raw}
        )
    finally:
        canonical_scratch.close()
        listing_scratch.close()

"""Archive lineage verification releases every strict-parser spool immediately."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest

from infrastructure.catalog.iceberg_adapter import CatalogIntegrityError
from infrastructure.catalog.phase1_tables import BINANCE_SPOT_AGG_TRADES
from infrastructure.revision.row_integrity import PersistedRowVerifier
from tests.infrastructure.parser import parser_support as ps
from tests.infrastructure.revision import revision_support as rs
from tests.infrastructure.revision.revision_support import StoreHarness


def _two_archive_rows(harness: StoreHarness) -> list[dict[str, Any]]:
    first = rs.archive(harness.storage, rows=ps.agg_rows(ps.US_DAY, count=3))
    second = rs.archive(
        harness.storage,
        rows=rs.replacement_rows(ps.US_DAY, count=2),
        retrieved_at=first.collected.retrieved_at + timedelta(days=30),
    )
    harness.store().ingest(first.collected, first.context)
    harness.store().ingest(second.collected, second.context)
    columns = tuple(field.name for field in BINANCE_SPOT_AGG_TRADES.arrow_schema)
    return harness.rows(BINANCE_SPOT_AGG_TRADES.table, columns)


def test_multi_archive_verification_uses_one_spool_and_returns_metadata(
    harness: StoreHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    rows = _two_archive_rows(harness)
    verifier = PersistedRowVerifier(harness.adapter, harness.storage)
    original_reparse = verifier._reparse
    original_verify_rows = verifier._verify_archive_rows
    opened: list[Any] = []
    events: list[tuple[str, str]] = []

    def tracked_reparse(collected: Any, data_type: str, archive_id: str) -> Any:
        assert all(spool._spool.closed for spool in opened)
        parsed = original_reparse(collected, data_type, archive_id)
        opened.append(parsed)
        events.append(("parse", archive_id))
        return parsed

    def tracked_verify_rows(
        definition: Any, data_type: str, archive: Any, parsed: Any, members: Any
    ) -> None:
        events.append(("verify", archive.revision_id))
        original_verify_rows(definition, data_type, archive, parsed, members)

    monkeypatch.setattr(verifier, "_reparse", tracked_reparse)
    monkeypatch.setattr(verifier, "_verify_archive_rows", tracked_verify_rows)

    verified = verifier.verify_archive_elements(
        BINANCE_SPOT_AGG_TRADES, "agg_trades", "BTCUSDT", rows
    )

    archive_ids = sorted({row["archive_revision_id"] for row in rows})
    assert list(verified) == archive_ids
    assert all(not hasattr(item, "parsed") for item in verified.values())
    assert all(spool._spool.closed for spool in opened)
    # The first pass strictly parses every archive before history checks; only then does the
    # second pass reparse and verify members one archive at a time.
    assert events[:2] == [("parse", archive_id) for archive_id in archive_ids]
    assert events[2:] == [
        pair
        for archive_id in archive_ids
        for pair in (("parse", archive_id), ("verify", archive_id))
    ]
    assert verifier._archives == {}


def test_member_validation_failure_closes_current_archive_spool(
    harness: StoreHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    rows = _two_archive_rows(harness)
    verifier = PersistedRowVerifier(harness.adapter, harness.storage, cache_archives=True)
    original_reparse = verifier._reparse
    opened: list[Any] = []

    def tracked_reparse(collected: Any, data_type: str, archive_id: str) -> Any:
        assert all(spool._spool.closed for spool in opened)
        parsed = original_reparse(collected, data_type, archive_id)
        opened.append(parsed)
        return parsed

    def reject_members(*_: Any) -> None:
        raise CatalogIntegrityError("injected member validation failure")

    monkeypatch.setattr(verifier, "_reparse", tracked_reparse)
    monkeypatch.setattr(verifier, "_verify_archive_rows", reject_members)

    with pytest.raises(CatalogIntegrityError, match="injected member validation failure"):
        verifier.verify_archive_elements(BINANCE_SPOT_AGG_TRADES, "agg_trades", "BTCUSDT", rows)

    assert len(opened) == 3  # two preflight parses, then the first member-verification parse
    assert all(spool._spool.closed for spool in opened)


def test_archive_row_count_closes_its_strict_parse_spool(
    harness: StoreHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    item = rs.archive(harness.storage, rows=ps.agg_rows(ps.US_DAY, count=3))
    result = harness.store().ingest(item.collected, item.context)
    verifier = PersistedRowVerifier(harness.adapter, harness.storage, cache_archives=True)
    original_reparse = verifier._reparse
    opened: list[Any] = []

    def tracked_reparse(collected: Any, data_type: str, archive_id: str) -> Any:
        parsed = original_reparse(collected, data_type, archive_id)
        opened.append(parsed)
        return parsed

    monkeypatch.setattr(verifier, "_reparse", tracked_reparse)

    for _ in range(2):
        assert verifier.archive_row_count("agg_trades", "BTCUSDT", result.archive_revision_id) == 3
    assert len(opened) == 2
    assert all(spool._spool.closed for spool in opened)

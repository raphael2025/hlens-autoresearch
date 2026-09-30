"""The bounded selector reads assumed availability without a second per-key mapping."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime, timedelta

import pytest

from infrastructure.catalog.phase1_tables import BINANCE_SPOT_AGG_TRADES
from infrastructure.pit.assumption import (
    ASSUMPTION_LATENCY,
    _EffectiveAvailabilityView,
    effective_available_times,
)
from infrastructure.pit.selector import PitSelector
from infrastructure.revision.availability import AVAILABILITY_POLICY_ID, AVAILABILITY_POLICY_VERSION
from tests.infrastructure.pit.test_selector import (
    END,
    FAR,
    START,
    TRADE_AT,
    WITH_ASSUMPTION,
    _archive_only,
    _spec,
)
from tests.infrastructure.pit.test_selector_v3 import _bounded
from tests.infrastructure.revision.rest_store_support import SYMBOL, RestHarness

_GAP = (
    f"{AVAILABILITY_POLICY_ID}@{AVAILABILITY_POLICY_VERSION}:agg_trade_publication_bound_not_stated"
)
_OBSERVABLE = datetime(2024, 1, 1, tzinfo=UTC)
_STORED_LATER = _OBSERVABLE + timedelta(minutes=2)


def _row(
    revision_id: str,
    *,
    raw_table: str = BINANCE_SPOT_AGG_TRADES.table,
    gap: str | None = _GAP,
    stored: datetime = _STORED_LATER,
    event_time: datetime | None = _OBSERVABLE,
    interval_end: datetime | None = None,
) -> dict[str, object]:
    return {
        "revision_id": revision_id,
        "lineage_raw_table": raw_table,
        "availability_evidence_gap": gap,
        "available_time": stored,
        "event_time": event_time,
        "interval_end": interval_end,
    }


def test_view_matches_materialized_rule_and_borrows_rows_mapping() -> None:
    input_rows = {
        "archive-moved": _row("archive-moved"),
        "rest-unmoved": _row("rest-unmoved", raw_table="raw.rest", gap=_GAP),
        "archive-no-gap": _row("archive-no-gap", gap=None),
        "archive-earlier-stored": _row(
            "archive-earlier-stored", stored=_OBSERVABLE + timedelta(seconds=1)
        ),
        "archive-no-observable": _row("archive-no-observable", event_time=None, interval_end=None),
    }
    view = _EffectiveAvailabilityView(input_rows, bound=True)
    materialized = effective_available_times(tuple(input_rows.values()), bound=True)

    assert view._rows is input_rows
    assert len(view) == len(input_rows)
    assert dict(view) == {
        revision_id: moved[1] if (moved := materialized.get(revision_id)) else row["available_time"]
        for revision_id, row in input_rows.items()
    }
    assert view["archive-moved"] == _OBSERVABLE + ASSUMPTION_LATENCY
    assert view["archive-earlier-stored"] == _OBSERVABLE + timedelta(seconds=1)


def test_view_without_assumption_preserves_stored_time_and_missing_key_fails() -> None:
    input_rows: Mapping[str, Mapping[str, object]] = {"r1": _row("r1")}
    view = _EffectiveAvailabilityView(input_rows, bound=False)
    assert view["r1"] == _STORED_LATER
    with pytest.raises(KeyError):
        _ = view["missing"]


def test_bounded_selector_assumption_matches_legacy_selected_revision(h: RestHarness) -> None:
    _archive_only(h)
    spec = _spec(
        h,
        cutoff=FAR,
        at=TRADE_AT + ASSUMPTION_LATENCY,
        availability_bindings=WITH_ASSUMPTION,
    )
    legacy = PitSelector(
        h.adapter, h.storage, canonical_scratch_directory=h.canonical_scratch_directory
    ).select(spec, "agg_trades", SYMBOL, START, END)
    bounded = _bounded(h, spec)

    assert [item.selection for item in bounded] == list(legacy.selections)
    [selected] = [item for item in bounded if item.selection.selected_revision_id is not None]
    assert selected.lineage is not None
    assert selected.lineage.canonical_revision_id in legacy.assumed

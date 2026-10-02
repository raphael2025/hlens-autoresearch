"""``pin_dataset_pit_spec`` (ADR-0101 §5): fixed binding set, current heads, no hidden inputs."""

from __future__ import annotations

from datetime import datetime
from typing import Any

import pytest
from pydantic import ValidationError

from core.contracts.revision import PointInTimeSpec
from infrastructure.canonical import listing_rules as lr
from infrastructure.canonical import rules
from infrastructure.catalog.phase1_tables import (
    CANONICAL_BARS_1M,
    CANONICAL_TRADES,
    DATASET_EVIDENCE_MANIFESTS,
    DATASET_MANIFESTS,
    DATASET_SELECTION_CHUNKS,
    DATASET_SELECTIONS,
    PHASE1_TABLES,
)
from infrastructure.dataset.pinning import PINNED_TABLES, pin_dataset_pit_spec
from infrastructure.pit.selector import PIT_BINDING
from infrastructure.revision.channel_precedence import DELIVERY_CHANNEL_BINDING
from infrastructure.revision.exchange_info_availability import EXCHANGE_INFO_AVAILABILITY_BINDING
from infrastructure.tools import capacity_probe
from infrastructure.universe import listing_assumption as backfill
from tests.infrastructure.dataset import dataset_support as ds
from tests.infrastructure.dataset.dataset_support import World
from tests.infrastructure.revision.rest_store_support import utc

SIM = ds.SIM
CUTOFF = utc(2023, 12, 21)


def _pin(w: World, *, listing_assumption: bool = False, **overrides: Any) -> PointInTimeSpec:
    fields: dict[str, Any] = {
        "name": "test.pin",
        "version": "1.2.3",
        "simulation_time": SIM,
        "knowledge_cutoff": CUTOFF,
        "listing_assumption": listing_assumption,
    }
    fields.update(overrides)
    return pin_dataset_pit_spec(w.h.adapter, **fields)


def _expected_heads(w: World) -> dict[str, str]:
    return {t: head for t in PINNED_TABLES if (head := w.h.head(t)) is not None}


def test_pins_exactly_the_tables_that_have_a_snapshot_at_their_current_heads(w: World) -> None:
    w.listed()
    w.trades()
    spec = _pin(w)
    assert dict(spec.snapshot_bindings) == _expected_heads(w)
    # Trades flow: archive + REST raw tables, canonical trades, precedence evidence, listings.
    assert CANONICAL_TRADES.table in spec.snapshot_bindings
    assert CANONICAL_BARS_1M.table not in spec.snapshot_bindings  # no snapshot yet: not pinned
    assert w.h.head(CANONICAL_BARS_1M.table) is None


def test_a_table_gaining_its_first_snapshot_is_pinned_and_a_moved_head_changes_the_spec(
    w: World,
) -> None:
    w.listed()
    listed_only = _pin(w)
    assert CANONICAL_TRADES.table not in listed_only.snapshot_bindings
    w.trades()
    with_trades = _pin(w)
    assert CANONICAL_TRADES.table in with_trades.snapshot_bindings
    assert with_trades.content_hash() != listed_only.content_hash()
    w.more_trades()
    moved = _pin(w)
    assert (
        moved.snapshot_bindings[CANONICAL_TRADES.table]
        != (with_trades.snapshot_bindings[CANONICAL_TRADES.table])
    )
    assert dict(moved.snapshot_bindings) == _expected_heads(w)


def test_same_heads_and_inputs_give_the_same_spec(w: World) -> None:
    w.listed()
    w.trades()
    assert _pin(w) == _pin(w)
    assert _pin(w).content_hash() == _pin(w).content_hash()


def test_never_pins_a_dataset_table_even_after_they_have_snapshots(w: World) -> None:
    w.listed()
    w.trades()
    own = {
        DATASET_SELECTIONS.table,
        DATASET_SELECTION_CHUNKS.table,
        DATASET_EVIDENCE_MANIFESTS.table,
        DATASET_MANIFESTS.table,
    }
    assert not own & set(PINNED_TABLES)
    assert set(PINNED_TABLES) < {t.table for t in PHASE1_TABLES}
    assert not own & set(_pin(w).snapshot_bindings)


def test_the_fixed_set_covers_every_input_of_both_data_types_and_the_quality_tables() -> None:
    expected = {
        "raw.binance_spot_archives",
        "raw.binance_spot_agg_trades",
        "raw.binance_spot_klines_1m",
        "raw.binance_spot_rest_responses",
        "raw.binance_spot_rest_agg_trades",
        "raw.binance_spot_rest_klines_1m",
        "raw.binance_spot_precedence_evidence",
        "raw.binance_spot_exchange_info",
        "canonical.trades",
        "canonical.bars_1m",
        "canonical.instrument_listings",
        "quality.data_quality_report_manifests",
        "quality.data_quality_reports",
        "quality.availability_evidence_gaps",
    }
    assert set(PINNED_TABLES) == expected
    assert len(PINNED_TABLES) == len(expected)


def test_policy_bindings_and_instants_are_the_dataset_ones(w: World) -> None:
    w.listed()
    w.trades()
    spec = _pin(w)
    assert (spec.name, spec.version) == ("test.pin", "1.2.3")
    assert spec.simulation_time == SIM and spec.knowledge_cutoff == CUTOFF
    assert spec.simulation_start is None and spec.simulation_end is None
    assert spec.point_in_time_binding == PIT_BINDING
    assert set(spec.availability_bindings) == {
        rules.AVAILABILITY_BINDING,
        EXCHANGE_INFO_AVAILABILITY_BINDING,
    }
    assert set(spec.precedence_bindings) == {
        DELIVERY_CHANNEL_BINDING,
        rules.PRECEDENCE_MAP_BINDING,
        lr.LISTING_OBSERVATION_BINDING,
    }
    assert set(spec.parser_bindings) == {rules.NORMALIZER_BINDING, lr.LISTING_STATUS_BINDING}


def test_listing_assumption_is_bound_only_on_request(w: World) -> None:
    w.listed()
    w.trades()
    plain = _pin(w)
    assumed = _pin(w, listing_assumption=True)
    assert backfill.ASSUMPTION_BINDING not in plain.availability_bindings
    assert backfill.ASSUMPTION_BINDING in assumed.availability_bindings
    assert set(assumed.availability_bindings) - set(plain.availability_bindings) == {
        backfill.ASSUMPTION_BINDING
    }
    assert assumed.snapshot_bindings == plain.snapshot_bindings
    assert assumed.content_hash() != plain.content_hash()


def test_matches_the_capacity_probe_dataset_spec_semantics(w: World) -> None:
    """Parity guard: same tables, heads and policy bindings as the probe's ``_dataset_pit_spec``."""
    w.listed()
    w.trades()
    probe = capacity_probe._dataset_pit_spec(w.h.adapter)
    pinned = _pin(w)
    assert pinned.snapshot_bindings == probe.snapshot_bindings
    assert pinned.point_in_time_binding == probe.point_in_time_binding
    assert pinned.availability_bindings == probe.availability_bindings
    assert pinned.precedence_bindings == probe.precedence_bindings
    assert pinned.parser_bindings == probe.parser_bindings


def test_empty_catalog_cannot_be_pinned(w: World) -> None:
    assert _expected_heads(w) == {}
    with pytest.raises(ValidationError, match="snapshot_bindings"):
        _pin(w)


def test_instants_must_be_timezone_aware(w: World) -> None:
    w.listed()
    with pytest.raises(ValidationError):
        _pin(w, simulation_time=datetime(2023, 12, 20))
    with pytest.raises(ValidationError):
        _pin(w, knowledge_cutoff=datetime(2023, 12, 20))


def test_listing_assumption_is_a_required_keyword(w: World) -> None:
    w.listed()
    with pytest.raises(TypeError):
        pin_dataset_pit_spec(  # type: ignore[call-arg]
            w.h.adapter, name="x.y", version="1.0.0", simulation_time=SIM, knowledge_cutoff=SIM
        )
    with pytest.raises(TypeError):
        pin_dataset_pit_spec(  # type: ignore[call-arg]
            w.h.adapter, "x.y", "1.0.0", SIM, SIM, False
        )

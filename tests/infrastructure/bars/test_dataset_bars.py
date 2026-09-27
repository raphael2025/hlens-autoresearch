"""Research Dataset bars → Phase 4 outcome labels and Phase 5 backtests (ADR-0037 / 0038 notes).

``outcome_request_from_dataset`` / ``backtest_bars_from_dataset`` load the manifest through the
builder's own verifying ``ManifestStore`` and re-select the Canonical 1m bars under its point spec;
a bar keeps its own ``available_time`` and ``Decimal`` OHLC, and a bar available after
``price_cutoff`` is refused. Same ``World`` fixtures as the F3 / F4 dataset tests (SQLite catalog,
real stores, mock venue; nothing under test is faked). Five bars, one symbol: far below the
memory cap.
"""

from __future__ import annotations

import inspect
from collections.abc import Callable, Iterator
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from core.contracts.feature import FeatureObservation
from core.contracts.outcome import (
    OutcomeEvent,
    OutcomeLabelSpec,
    OutcomeMethod,
    OutcomePriceBar,
    OutcomeRequest,
)
from core.contracts.revision import PointInTimeSpec
from core.contracts.strategy import BacktestRequest, BacktestResult, PriceBar, TargetPosition
from core.domain.base import FrozenMapping, content_hash
from core.domain.specs import OutcomeSpec
from infrastructure.bars import (
    DatasetBarsError,
    DatasetPriceBars,
    backtest_bars_from_dataset,
    outcome_request_from_dataset,
)
from infrastructure.bars import dataset as bars_dataset
from infrastructure.catalog.iceberg_adapter import CatalogIntegrityError
from infrastructure.catalog.phase1_tables import DATASET_MANIFESTS
from infrastructure.dataset.builder import DatasetBuilt
from infrastructure.dataset.manifests import ManifestStore
from infrastructure.feature.dataset import DatasetBindingError
from infrastructure.feature.observations import bar_observations
from infrastructure.pit.assumption import ASSUMPTION_BINDING, ASSUMPTION_LATENCY
from infrastructure.pit.selector import PitSelector
from plugins.backtest import BarBacktester
from plugins.outcomes import ForwardReturnOutcome, TripleBarrierOutcome
from research.outcomes import materialize
from tests.infrastructure.canonical import canonical_support as c
from tests.infrastructure.dataset import dataset_support as ds
from tests.infrastructure.dataset.dataset_support import World
from tests.infrastructure.redteam import redteam_support as rt
from tests.infrastructure.revision import rest_store_support as ss
from tests.infrastructure.revision.rest_store_support import SYMBOL, utc
from tests.strategy_fixtures import COSTS

DAY_WINDOW = (utc(2023, 11, 14), utc(2023, 11, 15))
INTERVAL = (utc(2023, 11, 14), ds.SIM)
BTC = "BTC-USDT"
BARS = 5
#: The fixture's bars: 22:14 .. 22:18 of DAY, identical OHLC (``kline_item`` defaults).
FIRST = utc(2023, 11, 14, 22, 14)
MINUTE = timedelta(minutes=1)
OPEN, CLOSE = Decimal("92792.05"), Decimal("92782.13")
OUTCOME = OutcomeSpec(
    name="dataset_forward_2m",
    version="1.0.0",
    created_at=utc(2023, 11, 1),
    horizon=2 * MINUTE,
    label_definition="TEST ONLY: 2-minute forward return on dataset bars",
)
FORWARD = OutcomeLabelSpec.bind(OUTCOME, OutcomeMethod.FORWARD_RETURN)
BARRIER = OutcomeLabelSpec.bind(
    OUTCOME,
    OutcomeMethod.TRIPLE_BARRIER,
    upper_barrier=Decimal("0.01"),
    lower_barrier=Decimal("0.01"),
)
EVENTS = (
    OutcomeEvent(event_key="at-first-open", event_time=FIRST),
    OutcomeEvent(event_key="inside-a-bar", event_time=FIRST + MINUTE + MINUTE / 2),
    OutcomeEvent(event_key="window-past-the-data", event_time=FIRST + 4 * MINUTE),
)


@pytest.fixture
def w(tmp_path: Path) -> Iterator[World]:
    with ds.sqlite_world(tmp_path) as opened:
        yield opened


def _assumed(spec: PointInTimeSpec) -> PointInTimeSpec:
    return spec.model_copy(
        update={"availability_bindings": (*spec.availability_bindings, ASSUMPTION_BINDING)}
    )


def _dataset(w: World, *, assumed: bool = True, **spec_fields: Any) -> DatasetBuilt:
    """A BTCUSDT bar dataset of DAY (five 1m bars); the spec is taken after the data exists."""
    w.listed()
    w.bars(count=BARS)
    w.report("klines_1m")
    spec = w.spec(skip=rt.OWN, **spec_fields)
    return rt.build(
        w, _assumed(spec) if assumed else spec, data_type="klines_1m", window=DAY_WINDOW
    )


def _outcome_request(
    w: World, built: DatasetBuilt, label_spec: OutcomeLabelSpec = FORWARD, **kwargs: Any
) -> OutcomeRequest:
    return outcome_request_from_dataset(
        w.h.adapter,
        w.h.storage,
        builder=w.builder(),
        manifest_content_hash=kwargs.pop("manifest_content_hash", built.manifest.content_hash()),
        symbol=kwargs.pop("symbol", BTC),
        label_spec=label_spec,
        events=EVENTS,
        **kwargs,
    )


def _backtest_bars(w: World, built: DatasetBuilt, **kwargs: Any) -> DatasetPriceBars:
    return backtest_bars_from_dataset(
        w.h.adapter,
        w.h.storage,
        builder=w.builder(),
        manifest_content_hash=kwargs.pop("manifest_content_hash", built.manifest.content_hash()),
        **kwargs,
    )


# ---------------------------------------------------------------------------------------------
# positive: dataset → outcome labels


def test_dataset_bars_keep_their_own_times_and_decimal_prices(w: World) -> None:
    built = _dataset(w, assumed=False)
    spec = built.manifest.point_in_time
    selection = PitSelector(w.h.adapter, w.h.storage).select(spec, "klines_1m", SYMBOL, *DAY_WINDOW)
    rows = selection.selected_rows.values()
    stored = {row["interval_start"]: row["available_time"] for row in rows}
    request = _outcome_request(w, built)
    assert request.manifest_content_hash == built.manifest.content_hash()
    assert request.price_cutoff == spec.simulation_time
    assert [bar.interval_start for bar in request.bars] == [FIRST + i * MINUTE for i in range(BARS)]
    for bar in request.bars:
        assert bar.available_time == stored[bar.interval_start]  # the selection's, not recomputed
        assert (bar.open, bar.close) == (OPEN, CLOSE)
        assert all(type(value) is Decimal for value in (bar.open, bar.high, bar.low, bar.close))


def test_outcome_labels_from_a_dataset_are_known_only_after_their_exit(w: World) -> None:
    """ADR-0032 bound: each bar is available at close + 5 s, and so is every label."""
    built = _dataset(w)
    request = _outcome_request(w, built)
    for bar in request.bars:
        assert bar.available_time == bar.interval_end + ASSUMPTION_LATENCY
    table = materialize(ForwardReturnOutcome((FORWARD,)), request)

    first = table.get("at-first-open")
    assert first is not None and first.value is not None
    assert (first.entry_time, first.exit_time) == (FIRST, FIRST + 2 * MINUTE)
    assert (first.entry_price, first.exit_price) == (OPEN, CLOSE)
    assert first.value < 0
    assert first.exit_time is not None
    assert first.available_time == first.exit_time + ASSUMPTION_LATENCY

    inside = table.get("inside-a-bar")
    assert inside is not None and inside.entry_time == FIRST + 2 * MINUTE  # the next open

    past = table.get("window-past-the-data")
    assert past is not None and past.value is None  # the window leaves the data: not filled

    for label in table.computable():
        assert label.available_time is not None and label.exit_time is not None
        assert label.available_time > label.exit_time > label.event_time
        assert label not in table.known_as_of(label.available_time - timedelta(microseconds=1))
        assert label in table.known_as_of(label.available_time)
    assert table.known_as_of(ds.SIM) == table.computable()


def test_triple_barrier_labels_run_on_the_same_dataset_bars(w: World) -> None:
    built = _dataset(w)
    table = materialize(TripleBarrierOutcome((BARRIER,)), _outcome_request(w, built, BARRIER))
    first = table.get("at-first-open")
    assert first is not None and first.barrier == 0  # a flat path touches neither barrier


# ---------------------------------------------------------------------------------------------
# positive: dataset → BarBacktester


def _targets(bars: DatasetPriceBars) -> tuple[TargetPosition, ...]:
    """Long after the first bar is known (22:15:05), flat after the third (22:17:05)."""
    first, _, third, *_ = bars.bars
    return (
        TargetPosition(
            decision_time=first.available_time,
            instrument=BTC,
            target_weight=Decimal("1"),
            inputs_used=1,
            latest_input_available_time=first.available_time,
        ),
        TargetPosition(
            decision_time=third.available_time,
            instrument=BTC,
            target_weight=Decimal("0"),
            inputs_used=1,
            latest_input_available_time=third.available_time,
        ),
    )


def _request(
    bars: DatasetPriceBars, price_bars: tuple[PriceBar, ...] | None = None
) -> BacktestRequest:
    """The dataset run's request (``price_bars`` replaces the bars, same targets)."""
    return BacktestRequest(
        cost_model=COSTS,
        initial_equity=Decimal("10000"),
        bars=bars.bars if price_bars is None else price_bars,
        targets=_targets(bars),
    )


def _run(bars: DatasetPriceBars) -> BacktestResult:
    return BarBacktester().run(_request(bars))


def test_a_dataset_feeds_the_bar_backtester(w: World) -> None:
    built = _dataset(w)
    bars = _backtest_bars(w, built)
    assert bars.manifest_content_hash == built.manifest.content_hash()
    assert bars.price_cutoff == ds.SIM
    assert {bar.instrument for bar in bars.bars} == {BTC}  # every symbol with dataset rows
    result = _run(bars)
    # Decided at 22:15:05 → executed at the 22:16 open; flattened at the 22:18 open.
    assert [fill.fill_time for fill in result.fills] == [FIRST + 2 * MINUTE, FIRST + 4 * MINUTE]
    assert [fill.reference_price for fill in result.fills] == [OPEN, OPEN]
    assert result.unexecuted_targets == 0


def test_dataset_runs_are_bit_identical_across_reruns(w: World) -> None:
    built = _dataset(w)
    first_request, second_request = _outcome_request(w, built), _outcome_request(w, built)
    assert first_request.model_dump_json() == second_request.model_dump_json()
    provider = ForwardReturnOutcome((FORWARD,))
    first, second = provider.compute(first_request), provider.compute(second_request)
    assert first.result_hash == second.result_hash

    first_bars, second_bars = _backtest_bars(w, built), _backtest_bars(w, built)
    assert first_bars == second_bars
    assert _run(first_bars).result_hash == _run(second_bars).result_hash


# ---------------------------------------------------------------------------------------------
# ADR-0054 §4: a dataset PriceBar carries its selected revision's volume (B61)

#: ``_request(bars).content_hash()`` on the code before B61 (dfa432b), when dataset bars carried no
#: volume — identical in two independently built worlds. Stripping the volume must reproduce it.
PRE_VOLUME_REQUEST_HASH = "e07c52d91f9990bff8eb0931e1f450e23a05a2e425cdd9a5d33d3b0fda16dcbf"
#: ``kline_item``'s base-asset volume (every fixture bar).
VOLUME = Decimal("6.45789000")


def _without_volume(bars: DatasetPriceBars) -> tuple[PriceBar, ...]:
    return tuple(PriceBar.model_validate({**bar.model_dump(), "volume": None}) for bar in bars.bars)


def test_backtest_bars_carry_the_selected_revisions_decimal_volume(w: World) -> None:
    built = _dataset(w)
    spec = built.manifest.point_in_time
    selection = PitSelector(w.h.adapter, w.h.storage).select(spec, "klines_1m", SYMBOL, *DAY_WINDOW)
    stored = {row["interval_start"]: row["volume"] for row in selection.selected_rows.values()}
    bars = _backtest_bars(w, built)
    assert len(bars.bars) == BARS
    for bar in bars.bars:
        row = stored[bar.interval_start]
        assert type(row) is Decimal and type(bar.volume) is Decimal
        assert bar.volume == VOLUME
        # the row's own Decimal: same digits and exponent, not re-parsed, rounded or via float
        assert bar.volume.as_tuple() == row.as_tuple()
        assert "volume" in bar.model_dump()


def test_the_volume_is_bound_into_the_request_hash_and_the_empty_payload_is_unchanged(
    w: World,
) -> None:
    built = _dataset(w)
    bars = _backtest_bars(w, built)
    with_volume = _request(bars)
    stripped = _request(bars, _without_volume(bars))
    # the expected change: the dataset request now hashes its bars' volume
    assert with_volume.content_hash() != PRE_VOLUME_REQUEST_HASH
    # without volume the payload omits it and the pre-B61 hash is reproduced bit for bit
    assert stripped.content_hash() == PRE_VOLUME_REQUEST_HASH
    assert all("volume" not in bar.model_dump() for bar in stripped.bars)
    # a different volume on one bar is a different request
    first, *rest = bars.bars
    assert first.volume is not None
    changed = PriceBar.model_validate({**first.model_dump(), "volume": first.volume + Decimal("1")})
    other = _request(bars, (changed, *rest))
    assert len({other.content_hash(), with_volume.content_hash(), PRE_VOLUME_REQUEST_HASH}) == 3
    # the default execution model does not read volume: the same fills and equity path
    a, b = BarBacktester().run(with_volume), BarBacktester().run(stripped)
    assert (a.fills, a.equity_curve) == (b.fills, b.equity_curve)
    assert a.request_hash != b.request_hash


def test_independent_dataset_builds_give_identical_volumes(tmp_path: Path) -> None:
    requests = []
    for name in ("one", "two"):
        (tmp_path / name).mkdir()
        with ds.sqlite_world(tmp_path / name) as world:
            bars = _backtest_bars(world, _dataset(world))
            assert [bar.volume for bar in bars.bars] == [VOLUME] * BARS
            requests.append(_request(bars))
    assert requests[0] == requests[1]
    assert requests[0].content_hash() == requests[1].content_hash()


def test_the_outcome_path_carries_no_volume(w: World) -> None:
    assert "volume" not in OutcomePriceBar.model_fields
    request = _outcome_request(w, _dataset(w))
    assert request.bars and all("volume" not in bar.model_dump() for bar in request.bars)
    assert "volume" not in request.model_dump_json()


@pytest.mark.parametrize("bad", ["absent", "int", "text"])
def test_a_selected_bar_without_a_decimal_volume_is_refused(
    w: World, monkeypatch: pytest.MonkeyPatch, bad: str
) -> None:
    """Canonical ``volume`` is a required ``decimal(38, 18)`` column, so this cannot come from the
    catalog; the proven observations are altered on their way in (the code under test is not)."""
    real = bar_observations

    def altered(*args: Any, **kwargs: Any) -> tuple[FeatureObservation, ...]:
        out = []
        for item in real(*args, **kwargs):
            values = dict(item.values)
            if bad == "absent":
                del values["volume"]
            else:
                volume = values["volume"]
                # a non-numeric text (numeric text is already refused by FeatureObservation)
                values["volume"] = int(volume) if bad == "int" else "unknown"
            out.append(item.model_copy(update={"values": FrozenMapping(values)}))
        return tuple(out)

    built = _dataset(w)
    monkeypatch.setattr(bars_dataset, "bar_observations", altered)  # the name dataset.py calls
    with pytest.raises(CatalogIntegrityError, match="volume is not a Decimal"):
        _backtest_bars(w, built)
    # the same proven-bar path serves the outcome request: an unsound row is refused there too
    with pytest.raises(CatalogIntegrityError, match="volume is not a Decimal"):
        _outcome_request(w, built)


# ---------------------------------------------------------------------------------------------
# refusals: the manifest


def _forged_row(w: World, built: DatasetBuilt) -> str:
    genuine = built.manifest.content_hash()
    ids = (*built.manifest.quality_report_ids, "qr-forged")
    other = built.manifest.model_copy(update={"quality_report_ids": ids})
    row = dict(rt.manifest_row(other), manifest_content_hash=genuine)
    w.h.forge_rows(DATASET_MANIFESTS, [row], batch_id="forged-manifest")
    with pytest.raises(CatalogIntegrityError):
        ManifestStore(w.h.adapter, w.builder()).load(genuine)  # the store itself refuses it
    return genuine


def _unknown(w: World, built: DatasetBuilt) -> str:
    unknown = content_hash({"manifest": "never persisted"})
    assert ManifestStore(w.h.adapter, w.builder()).load(unknown) is None
    return unknown


TAMPERED: dict[str, Callable[[World, DatasetBuilt], str]] = {
    "forged-row-under-the-hash": _forged_row,
    "no-such-manifest": _unknown,
}


@pytest.mark.parametrize("attack", sorted(TAMPERED))
def test_a_manifest_that_does_not_load_yields_no_bars(w: World, attack: str) -> None:
    built = _dataset(w)
    manifest_hash = TAMPERED[attack](w, built)
    with pytest.raises((DatasetBindingError, CatalogIntegrityError)):
        _outcome_request(w, built, manifest_content_hash=manifest_hash)
    with pytest.raises((DatasetBindingError, CatalogIntegrityError)):
        _backtest_bars(w, built, manifest_content_hash=manifest_hash)


def test_there_is_no_unverified_entry(w: World) -> None:
    """RT-6: no parameter takes a manifest object or bars; a non-builder verifier is refused."""
    for entry in (outcome_request_from_dataset, backtest_bars_from_dataset):
        names = set(inspect.signature(entry).parameters)
        assert not names & {"manifest", "bars", "verifier", "observations"}
    built = _dataset(w)

    class _Trusting:
        def manifests(self) -> Any:
            return self

        def load(self, _: str) -> Any:
            return built.manifest

    with pytest.raises(DatasetBindingError, match="DatasetBuilder"):
        outcome_request_from_dataset(
            w.h.adapter,
            w.h.storage,
            builder=_Trusting(),  # type: ignore[arg-type]
            manifest_content_hash=built.manifest.content_hash(),
            symbol=BTC,
            label_spec=FORWARD,
            events=EVENTS,
        )


def test_an_interval_dataset_is_refused(w: World) -> None:
    built = _dataset(w, interval=INTERVAL)
    with pytest.raises(DatasetBarsError, match="point-simulation"):
        _backtest_bars(w, built)


# ---------------------------------------------------------------------------------------------
# refusals: the cutoff


def test_bars_available_after_the_price_cutoff_are_refused(w: World) -> None:
    built = _dataset(w)
    third_known = FIRST + 3 * MINUTE + ASSUMPTION_LATENCY  # the third bar (22:16) is known
    with pytest.raises(DatasetBarsError, match="after price_cutoff"):
        _outcome_request(w, built, price_cutoff=third_known)
    with pytest.raises(DatasetBarsError, match="after price_cutoff"):
        _backtest_bars(w, built, price_cutoff=third_known)

    # Narrowed to the bars known by then: accepted, and every bar is within the cutoff.
    request = _outcome_request(w, built, price_cutoff=third_known, end=FIRST + 3 * MINUTE)
    assert request.price_cutoff == third_known and len(request.bars) == 3
    assert max(bar.available_time for bar in request.bars) == third_known


def test_a_price_cutoff_beyond_the_datasets_view_is_refused(w: World) -> None:
    built = _dataset(w)
    with pytest.raises(DatasetBarsError, match="after the dataset's PIT view"):
        _backtest_bars(w, built, price_cutoff=ds.SIM + timedelta(seconds=1))


# ---------------------------------------------------------------------------------------------
# refusals: missing units


def test_a_symbol_without_dataset_rows_is_refused(w: World) -> None:
    built = _dataset(w)  # ETHUSDT is a member, but has no bars
    with pytest.raises(DatasetBarsError, match="no klines_1m rows"):
        _outcome_request(w, built, symbol="ETH-USDT")
    with pytest.raises(DatasetBarsError, match="no klines_1m rows"):
        _backtest_bars(w, built, symbols=(BTC, "ETH-USDT"))


def test_a_manifest_of_another_data_type_has_no_bar_rows(w: World) -> None:
    w.listed()
    w.bars()
    archive = c.ingest_archive(
        w.h, "agg_trades", ss.archive_agg_lines(ss.agg_items(3)), knowledge=ds.K_A
    )
    rt.normalize(w, c.ARCHIVE_AGGS.table, archive, at=ds.N_A)
    w.report("agg_trades")
    w.report("klines_1m", listing=False)
    built = rt.build(w)  # an agg_trades dataset whose spec also binds the bars
    with pytest.raises(DatasetBarsError, match="no klines_1m rows"):
        _backtest_bars(w, built)

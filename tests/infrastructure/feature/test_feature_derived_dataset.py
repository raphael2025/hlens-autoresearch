"""F4 feature runs over E4 derived bars bound to a verified Research Dataset manifest (F4-R2).

``feature_request_from_derived_bars`` loads the manifest through the builder's ``ManifestStore``
and proves that the caller's derived-bar observations are **exactly** ``derived_bar_observations``
of ``resample_bars`` over the manifest's own dataset selection: same PIT spec, lineage bound by the
manifest, closing constituents that are dataset rows, and a re-derivation that must match bar for
bar (ADR-0032 effective times included). Positive runs are bit-identical across re-runs and a
reopened catalog; every altered, dropped, added or moved bar is refused.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator, Sequence
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from core.contracts.feature import FeatureObservation, FeatureRequest, FeatureResult
from core.contracts.revision import PointInTimeSpec
from core.domain.base import FrozenMapping, Kind, Ref, content_hash
from infrastructure.canonical.resample import resample_bars
from infrastructure.dataset.builder import DatasetBuilt
from infrastructure.feature.dataset import DatasetBindingError, feature_request_from_derived_bars
from infrastructure.feature.observations import derived_bar_observations, pit_feature_request
from infrastructure.feature.runner import run_feature
from infrastructure.pit.assumption import ASSUMPTION_BINDING, ASSUMPTION_LATENCY
from infrastructure.pit.selector import PitSelector
from plugins.features import BarLogReturnProvider
from tests.infrastructure.dataset import dataset_support as ds
from tests.infrastructure.dataset.dataset_support import World
from tests.infrastructure.e2e.first_slice_support import BTC, ETH, ingest_bars_for
from tests.infrastructure.redteam import redteam_support as rt
from tests.infrastructure.revision.rest_store_support import utc

LAG = timedelta(minutes=1)
MINUTES = 5
DAY_WINDOW = (utc(2023, 11, 14), utc(2023, 11, 15))
FIVE_MINUTE_BAR = Ref(kind=Kind.REPRESENTATION, name="canonical_resample_5m", version="1.0.0")
FEATURE = BarLogReturnProvider.spec(available_lag=LAG, bar_input=FIVE_MINUTE_BAR)
AT = (ds.SIM + LAG,)


@pytest.fixture
def w(tmp_path: Path) -> Iterator[World]:
    with ds.sqlite_world(tmp_path) as opened:
        yield opened


def _assumed(spec: PointInTimeSpec) -> PointInTimeSpec:
    return spec.model_copy(
        update={"availability_bindings": (*spec.availability_bindings, ASSUMPTION_BINDING)}
    )


def _dataset(w: World, *, assumed: bool = False, **spec_fields: Any) -> DatasetBuilt:
    """A BTCUSDT + ETHUSDT bar dataset of DAY (12 minutes each: two complete 5-minute buckets)."""
    w.listed()
    ingest_bars_for(w, BTC, tag="btc", base="100")
    ingest_bars_for(w, ETH, tag="eth", base="200")
    w.report("klines_1m")
    spec = w.spec(skip=rt.OWN, **spec_fields)
    return rt.build(
        w, _assumed(spec) if assumed else spec, data_type="klines_1m", window=DAY_WINDOW
    )


def _derived(
    w: World,
    spec: PointInTimeSpec,
    symbol: str = BTC,
    *,
    minutes: int = MINUTES,
    window: tuple[datetime, datetime] = DAY_WINDOW,
) -> tuple[FeatureObservation, ...]:
    selection = PitSelector(w.h.adapter, w.h.storage).select(spec, "klines_1m", symbol, *window)
    return derived_bar_observations(resample_bars(selection, minutes, *window), selection, spec)


def _request(
    w: World,
    built: DatasetBuilt,
    observations: Sequence[FeatureObservation] | None = None,
    *,
    minutes: int = MINUTES,
    pit_spec: PointInTimeSpec | None = None,
    manifest_hash: str | None = None,
) -> FeatureRequest:
    spec = built.manifest.point_in_time
    return feature_request_from_derived_bars(
        w.h.adapter,
        w.h.storage,
        builder=w.builder(),
        manifest_content_hash=manifest_hash or built.manifest.content_hash(),
        pit_spec=pit_spec or spec,
        minutes=minutes,
        observations=_derived(w, spec) if observations is None else observations,
        feature=FEATURE,
        evaluation_times=AT,
    )


def _run(request: FeatureRequest) -> FeatureResult:
    return run_feature(BarLogReturnProvider((FEATURE,)), FEATURE, request)


# ---------------------------------------------------------------------------------------------
# positive: genuine manifest-bound derived-bar runs


def test_a_manifest_bound_derived_bar_run_is_bit_identical_across_reruns(w: World) -> None:
    built = _dataset(w)
    spec = built.manifest.point_in_time
    first_request = _request(w, built)
    assert len(first_request.observations) == 2  # only the complete buckets
    assert first_request.manifest_content_hash == built.manifest.content_hash()
    first = _run(first_request)
    [value] = first.values
    assert value.value is not None and value.inputs_used == 2

    # A second request from a fresh selection and re-derivation: bit-identical.
    second_request = _request(w, built)
    assert second_request.model_dump_json() == first_request.model_dump_json()
    second = _run(second_request)
    assert second.model_dump_json() == first.model_dump_json()

    # A fresh process (reopened catalog): the same request, the same result_hash.
    w.h.reopen()
    third = _run(_request(w, built))
    assert third.result_hash == first.result_hash

    # The bound request is exactly what the ad-hoc path would build from the same inputs.
    adhoc = pit_feature_request(
        pit_spec=spec,
        observations=first_request.observations,
        feature=FEATURE,
        evaluation_times=AT,
        manifest_content_hash=built.manifest.content_hash(),
    )
    assert adhoc == first_request


def test_derived_bars_of_every_member_symbol_bind_to_one_manifest(w: World) -> None:
    built = _dataset(w)
    spec = built.manifest.point_in_time
    both = (*_derived(w, spec, BTC), *_derived(w, spec, ETH))
    request = _request(w, built, both)
    assert len(request.observations) == 4
    assert {item.values["symbol"] for item in request.observations} == {"BTC-USDT", "ETH-USDT"}
    # Per symbol (the provider takes one symbol per request), each run is reproducible.
    eth = _run(_request(w, built, _derived(w, spec, ETH)))
    assert eth == _run(_request(w, built, _derived(w, spec, ETH)))
    assert eth.values[0].value is not None


def test_an_assumption_bound_dataset_derives_bars_at_their_effective_times(w: World) -> None:
    """ADR-0032: each constituent enters at close + 5 s, so a derived bar at bucket end + 5 s."""
    built = _dataset(w, assumed=True)
    request = _request(w, built)
    for item in request.observations:
        assert item.event_end_time is not None
        assert item.available_time == item.event_end_time + ASSUMPTION_LATENCY
    assert _run(request) == _run(_request(w, built))


# ---------------------------------------------------------------------------------------------
# refusals: the observations must be exactly resample_bars of the dataset's own selection


def _dropped(items: tuple[FeatureObservation, ...]) -> tuple[FeatureObservation, ...]:
    return items[:-1]


def _added(items: tuple[FeatureObservation, ...]) -> tuple[FeatureObservation, ...]:
    head = items[0]
    extra = head.model_copy(update={"observation_key": f"{head.observation_key}:extra"})
    return (*items, extra)


def _later(items: tuple[FeatureObservation, ...]) -> tuple[FeatureObservation, ...]:
    head, *rest = items
    return (head.model_copy(update={"available_time": head.available_time + LAG}), *rest)


def _earlier(items: tuple[FeatureObservation, ...]) -> tuple[FeatureObservation, ...]:
    *rest, last = items
    moved = last.model_copy(update={"available_time": last.available_time - timedelta(seconds=1)})
    return (*rest, moved)


def _shifted(items: tuple[FeatureObservation, ...]) -> tuple[FeatureObservation, ...]:
    """The first bar moved one bucket later: its closing constituent is outside the interval."""
    head, *rest = items
    period = timedelta(minutes=MINUTES)
    assert head.event_end_time is not None
    moved = head.model_copy(
        update={
            "event_time": head.event_time + period,
            "event_end_time": head.event_end_time + period,
            "available_time": head.available_time + period,
        }
    )
    return (moved, *rest)


def _revalued(items: tuple[FeatureObservation, ...]) -> tuple[FeatureObservation, ...]:
    head, *rest = items
    values = dict(head.values, close=Decimal("1000000"))
    return (head.model_copy(update={"values": FrozenMapping(values)}), *rest)


def _recontent(items: tuple[FeatureObservation, ...]) -> tuple[FeatureObservation, ...]:
    head, *rest = items
    values = dict(head.values, resample_content=f"sha256:{'0' * 64}")
    return (head.model_copy(update={"values": FrozenMapping(values)}), *rest)


def _unbound_lineage(items: tuple[FeatureObservation, ...]) -> tuple[FeatureObservation, ...]:
    head, *rest = items
    lineage = head.lineage.model_copy(update={"raw_revision_id": "raw-not-in-the-manifest"})
    return (head.model_copy(update={"lineage": lineage}), *rest)


ALTERED: dict[str, Callable[[tuple[FeatureObservation, ...]], tuple[FeatureObservation, ...]]] = {
    "one-bar-dropped": _dropped,
    "one-bar-added": _added,
    "available-later": _later,
    "available-earlier": _earlier,
    "bar-moved-one-bucket": _shifted,
    "close-changed": _revalued,
    "resample-content-changed": _recontent,
    "lineage-not-bound": _unbound_lineage,
}


@pytest.mark.parametrize("change", sorted(ALTERED))
def test_derived_bars_that_are_not_the_datasets_resample_are_refused(w: World, change: str) -> None:
    built = _dataset(w)
    genuine = _derived(w, built.manifest.point_in_time)
    _request(w, built, genuine)  # the genuine bars bind
    with pytest.raises(DatasetBindingError):
        _request(w, built, ALTERED[change](genuine))


def test_bars_resampled_over_another_window_or_period_are_refused(w: World) -> None:
    built = _dataset(w)
    spec = built.manifest.point_in_time
    # A narrower window drops the 22:05 bar's first minute: not the dataset's own selection.
    narrow = _derived(w, spec, window=(utc(2023, 11, 14, 22, 6), DAY_WINDOW[1]))
    with pytest.raises(DatasetBindingError, match="not exactly"):
        _request(w, built, narrow)
    # Genuine 1-minute "derived" bars claimed as 5-minute bars (and the reverse).
    one_minute = _derived(w, spec, minutes=1)
    with pytest.raises(DatasetBindingError, match="not exactly"):
        _request(w, built, one_minute)
    with pytest.raises(DatasetBindingError, match="not exactly"):
        _request(w, built, _derived(w, spec), minutes=10)
    with pytest.raises(DatasetBindingError, match="cannot be resampled"):
        _request(w, built, _derived(w, spec), minutes=7)


def test_the_assumption_bound_or_not_is_part_of_the_spec(w: World) -> None:
    built = _dataset(w, assumed=True)
    assumed_spec = built.manifest.point_in_time
    plain = assumed_spec.model_copy(
        update={"availability_bindings": w.spec().availability_bindings}
    )
    with pytest.raises(DatasetBindingError, match="another PIT spec"):
        _request(w, built, _derived(w, plain), pit_spec=plain)
    # Bars derived without the assumption, passed under the manifest's spec: their times moved.
    with pytest.raises(DatasetBindingError, match="not exactly"):
        _request(w, built, _derived(w, plain))


def test_an_interval_dataset_has_no_derived_bars(w: World) -> None:
    built = _dataset(w, assumed=True, interval=(utc(2023, 11, 14), ds.SIM))
    with pytest.raises(DatasetBindingError, match="point-simulation dataset"):
        _request(w, built, _derived(w, w.spec(), BTC))


def test_a_manifest_that_is_not_persisted_binds_nothing(w: World) -> None:
    built = _dataset(w)
    with pytest.raises(DatasetBindingError, match="no Research Dataset manifest"):
        _request(w, built, manifest_hash=content_hash({"manifest": "never persisted"}))


def test_a_non_members_derived_bars_are_refused(w: World) -> None:
    """ETHUSDT halts before SIM: its bars are no dataset rows, so its derived bars bind nothing."""
    w.listed()
    w.listed({"BTCUSDT": "TRADING", "ETHUSDT": "HALT"}, at=utc(2023, 11, 14, 23))
    ingest_bars_for(w, BTC, tag="btc", base="100")
    ingest_bars_for(w, ETH, tag="eth", base="200")
    w.report("klines_1m")
    built = rt.build(w, w.spec(skip=rt.OWN), data_type="klines_1m", window=DAY_WINDOW)
    assert {row["symbol"] for row in built.selection.rows} == {"BTC-USDT"}
    spec = built.manifest.point_in_time
    _request(w, built, _derived(w, spec, BTC))  # the member binds
    with pytest.raises(DatasetBindingError, match="not bound by the manifest|not a row"):
        _request(w, built, _derived(w, spec, ETH))

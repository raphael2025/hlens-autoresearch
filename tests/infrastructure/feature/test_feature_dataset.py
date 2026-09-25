"""F4 feature runs bound to a persisted, verified Research Dataset manifest (G2-R1c, RT-6).

``feature_request_from_dataset`` loads the manifest through ``ManifestStore`` and proves the
observations against it: the PIT spec, the dataset's own snapshot rows, the manifest's lineage, a
re-selection under the manifest's spec (ADR-0032 effective times included) and, for an interval
spec, the member-gated row spans. The red-team attacks (forged / missing manifest, another spec)
are in ``tests/infrastructure/redteam/test_rt_features.py``; this module holds the positive runs
and the finer refusals.
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
from core.domain.base import FrozenMapping
from infrastructure.dataset.builder import DatasetBuilt
from infrastructure.feature.dataset import DatasetBindingError, feature_request_from_dataset
from infrastructure.feature.observations import bar_observations, pit_feature_request
from infrastructure.feature.runner import run_feature
from infrastructure.pit.assumption import ASSUMPTION_BINDING, ASSUMPTION_LATENCY
from infrastructure.pit.selector import PitSelector
from plugins.features import BarVolumeSumProvider
from tests.infrastructure.canonical import canonical_support as c
from tests.infrastructure.dataset import dataset_support as ds
from tests.infrastructure.dataset.dataset_support import World
from tests.infrastructure.redteam import redteam_support as rt
from tests.infrastructure.revision import rest_store_support as ss
from tests.infrastructure.revision.rest_store_support import SYMBOL, utc

LAG = timedelta(minutes=1)
DAY_WINDOW = (utc(2023, 11, 14), utc(2023, 11, 15))
INTERVAL = (utc(2023, 11, 14), ds.SIM)
FEATURE = BarVolumeSumProvider.spec(2, available_lag=LAG)
FIVE_S = timedelta(seconds=5)


@pytest.fixture
def w(tmp_path: Path) -> Iterator[World]:
    with ds.sqlite_world(tmp_path) as opened:
        yield opened


def _assumed(spec: PointInTimeSpec) -> PointInTimeSpec:
    return spec.model_copy(
        update={"availability_bindings": (*spec.availability_bindings, ASSUMPTION_BINDING)}
    )


def _dataset(w: World, *, assumed: bool = False, **spec_fields: Any) -> DatasetBuilt:
    """A BTCUSDT bar dataset of DAY; the spec is taken after the data exists."""
    w.listed()
    w.bars()
    w.report("klines_1m")
    spec = w.spec(skip=rt.OWN, **spec_fields)
    return rt.build(
        w, _assumed(spec) if assumed else spec, data_type="klines_1m", window=DAY_WINDOW
    )


def _observations(w: World, spec: PointInTimeSpec) -> tuple[FeatureObservation, ...]:
    selection = PitSelector(w.h.adapter, w.h.storage).select(spec, "klines_1m", SYMBOL, *DAY_WINDOW)
    return bar_observations(selection, spec)


def _request(
    w: World,
    built: DatasetBuilt,
    evaluation_times: Sequence[datetime],
    observations: Sequence[FeatureObservation] | None = None,
) -> FeatureRequest:
    spec = built.manifest.point_in_time
    return feature_request_from_dataset(
        w.h.adapter,
        w.h.storage,
        manifest_content_hash=built.manifest.content_hash(),
        pit_spec=spec,
        observations=_observations(w, spec) if observations is None else observations,
        feature=FEATURE,
        evaluation_times=evaluation_times,
    )


def _run(request: FeatureRequest) -> FeatureResult:
    return run_feature(BarVolumeSumProvider((FEATURE,)), FEATURE, request)


# ---------------------------------------------------------------------------------------------
# positive: genuine manifest-bound runs


def test_a_manifest_bound_run_is_bit_identical_across_reruns(w: World) -> None:
    built = _dataset(w)
    first_request = _request(w, built, (ds.SIM + LAG,))
    assert first_request.manifest_content_hash == built.manifest.content_hash()
    assert first_request.knowledge_cutoff == built.manifest.point_in_time.knowledge_cutoff
    first = _run(first_request)
    [value] = first.values
    assert value.value is not None and value.inputs_used == 2

    # A second request, built from a fresh selection, and a second run: bit-identical.
    second_request = _request(w, built, (ds.SIM + LAG,))
    assert second_request.model_dump_json() == first_request.model_dump_json()
    assert second_request.content_hash() == first_request.content_hash()
    second = _run(second_request)
    assert second.model_dump_json() == first.model_dump_json()
    assert second.result_hash == first.result_hash

    # The manifest-bound path answers exactly what the ad-hoc path would for the same inputs.
    adhoc = pit_feature_request(
        pit_spec=built.manifest.point_in_time,
        observations=first_request.observations,
        feature=FEATURE,
        evaluation_times=(ds.SIM + LAG,),
        manifest_content_hash=built.manifest.content_hash(),
    )
    assert adhoc == first_request


def test_an_assumption_bound_interval_dataset_feeds_its_effective_times(w: World) -> None:
    """ADR-0032: the archive bars enter at close + 5 s, exactly as the dataset rows do."""
    built = _dataset(w, assumed=True, interval=INTERVAL)
    rows = {row["revision_id"]: row for row in built.selection.rows}
    stored = {row["revision_id"]: row["available_time"] for row in w.h.rows(c.BARS)}
    at = (
        utc(2023, 11, 14, 22, 17) + FIVE_S
    ) + LAG  # the third bar's effective time, seen at the lag
    request = _request(w, built, (at - timedelta(seconds=1), at))
    assert len(request.observations) == len(rows) == 3
    for item in request.observations:
        revision = item.lineage.canonical_revision_id
        assert item.event_end_time is not None
        assert item.available_time == item.event_end_time + ASSUMPTION_LATENCY
        assert item.available_time == rows[revision]["effective_from"]
        assert item.available_time < stored[revision]  # moved earlier than stored, never later
    result = _run(request)
    before, after = result.values
    assert (before.inputs_used, after.inputs_used) == (2, 2)
    # The third bar entered exactly at its effective time (the view of ``at``), not a tick before.
    assert before.latest_input_available_time == (utc(2023, 11, 14, 22, 16) + FIVE_S)
    assert after.latest_input_available_time == (utc(2023, 11, 14, 22, 17) + FIVE_S)
    assert _run(_request(w, built, (at - timedelta(seconds=1), at))) == result


# ---------------------------------------------------------------------------------------------
# refusals: the observations must be exactly the dataset's proven rows


def _dropped(items: tuple[FeatureObservation, ...]) -> tuple[FeatureObservation, ...]:
    return items[:-1]


def _later(items: tuple[FeatureObservation, ...]) -> tuple[FeatureObservation, ...]:
    head, *rest = items
    return (head.model_copy(update={"available_time": head.available_time + LAG}), *rest)


def _earlier(items: tuple[FeatureObservation, ...]) -> tuple[FeatureObservation, ...]:
    *rest, last = items
    moved = last.model_copy(update={"available_time": last.available_time - timedelta(seconds=1)})
    return (*rest, moved)


def _revalued(items: tuple[FeatureObservation, ...]) -> tuple[FeatureObservation, ...]:
    head, *rest = items
    values = dict(head.values, volume=Decimal("1000000"))
    return (head.model_copy(update={"values": FrozenMapping(values)}), *rest)


def _unbound_lineage(items: tuple[FeatureObservation, ...]) -> tuple[FeatureObservation, ...]:
    head, *rest = items
    lineage = head.lineage.model_copy(update={"raw_revision_id": "raw-not-in-the-manifest"})
    return (head.model_copy(update={"lineage": lineage}), *rest)


ALTERED: dict[str, Callable[[tuple[FeatureObservation, ...]], tuple[FeatureObservation, ...]]] = {
    "one-bar-dropped": _dropped,
    "available-later": _later,
    "available-earlier": _earlier,
    "value-changed": _revalued,
    "lineage-not-bound": _unbound_lineage,
}


@pytest.mark.parametrize("change", sorted(ALTERED))
def test_observations_that_are_not_the_datasets_proven_rows_are_refused(
    w: World, change: str
) -> None:
    built = _dataset(w)
    genuine = _observations(w, built.manifest.point_in_time)
    with pytest.raises(DatasetBindingError):
        _request(w, built, (ds.SIM + LAG,), ALTERED[change](genuine))


def test_the_assumption_bound_or_not_is_part_of_the_spec(w: World) -> None:
    """Bars selected without ADR-0032 cannot feed a dataset that binds it (and the reverse)."""
    built = _dataset(w, assumed=True)
    plain = built.manifest.point_in_time.model_copy(
        update={"availability_bindings": w.spec().availability_bindings}
    )
    with pytest.raises(DatasetBindingError, match="another PIT spec"):
        feature_request_from_dataset(
            w.h.adapter,
            w.h.storage,
            manifest_content_hash=built.manifest.content_hash(),
            pit_spec=plain,
            observations=_observations(w, plain),
            feature=FEATURE,
            evaluation_times=(ds.SIM + LAG,),
        )


def test_a_manifest_of_another_data_type_has_no_bar_rows(w: World) -> None:
    w.listed()
    w.bars()
    # Archive-only trades (the REST request id is the bars'): an agg_trades dataset.
    archive = c.ingest_archive(
        w.h, "agg_trades", ss.archive_agg_lines(ss.agg_items(3)), knowledge=ds.K_A
    )
    rt.normalize(w, c.ARCHIVE_AGGS.table, archive, at=ds.N_A)
    w.report("agg_trades")
    w.report("klines_1m", listing=False)
    built = rt.build(w)  # an agg_trades dataset whose spec also binds the bars
    with pytest.raises(DatasetBindingError, match="no klines_1m rows"):
        _request(w, built, (ds.SIM + LAG,))


def test_empty_observations_make_no_dataset_request(w: World) -> None:
    built = _dataset(w)
    with pytest.raises(DatasetBindingError, match="needs the dataset's observations"):
        _request(w, built, (ds.SIM + LAG,), ())


def test_an_interval_evaluation_outside_the_members_span_is_refused(w: World) -> None:
    """The universe gates rows: once BTCUSDT halts, its bars are no dataset rows any more."""
    halt = utc(2023, 11, 14, 22, 30)
    w.listed()
    w.listed({"BTCUSDT": "HALT", "ETHUSDT": "TRADING"}, at=halt)
    w.bars()
    w.report("klines_1m")
    spec = _assumed(w.spec(interval=INTERVAL, skip=rt.OWN))  # the bars enter before the halt
    built = rt.build(w, spec, data_type="klines_1m", window=DAY_WINDOW)
    until = {row["effective_until"] for row in built.selection.rows}
    assert until == {halt}  # a non-empty, member-gated dataset
    request = _request(w, built, (halt - timedelta(seconds=1) + LAG,))
    assert len(request.visible_at(halt - timedelta(seconds=1) + LAG, LAG)) == 3  # still a member
    with pytest.raises(DatasetBindingError, match="not a member"):
        _request(w, built, (halt + LAG,))

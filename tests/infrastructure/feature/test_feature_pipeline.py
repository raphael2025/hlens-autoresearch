"""F4 end to end: archive / REST → Canonical bars → F1 PIT selection → runner → FeatureResult.

Real Raw rows (D2 / D3E), the real normalizer, reconciler and selector (as in the F1 / E4 tests);
the features are the ``plugins/features`` providers run through ``run_feature``. The manifest hash
is a stand-in until F3 builds Research Dataset manifests (ADR-0029).
"""

from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest

from core.contracts.feature import FeatureObservation, FeatureRequest, FeatureResult
from core.contracts.revision import PointInTimeSpec, PointInTimeStatus
from core.domain.base import Kind, Ref, content_hash
from core.domain.specs import FeatureSpec
from infrastructure.canonical.resample import resample_bars
from infrastructure.feature.observations import (
    FeatureInputBuildError,
    bar_observations,
    derived_bar_observations,
    pit_feature_request,
)
from infrastructure.feature.runner import run_feature
from infrastructure.pit.selector import PitSelection, PitSelector
from plugins.features import (
    BarLogReturnProvider,
    BarRealizedVolatilityProvider,
    BarVolumeSumProvider,
)
from tests.infrastructure.canonical import canonical_support as c
from tests.infrastructure.pit.test_selector import FAR, _spec
from tests.infrastructure.revision import rest_store_support as ss
from tests.infrastructure.revision.rest_store_support import SYMBOL, RestHarness, StepClock, utc

M = ss.MINUTE_MS
LAG = timedelta(minutes=1)
K_ARCHIVE, K_REST = utc(2023, 12, 1), utc(2023, 12, 5)
N_ARCHIVE, N_REST = utc(2023, 12, 6), utc(2023, 12, 7)
K_EDGE = utc(2023, 12, 10)
S0, S1 = utc(2023, 11, 15), utc(2023, 12, 31)
DAY_START, DAY_END = utc(2023, 11, 14), utc(2023, 11, 15)
#: Stand-in until F3 builds manifests (ADR-0029); the runner binds whatever hash it is given.
MANIFEST = content_hash({"manifest": "stand-in until F3"})
PROVIDERS: tuple[tuple[type[Any], FeatureSpec], ...] = (
    (BarLogReturnProvider, BarLogReturnProvider.spec(available_lag=LAG)),
    (BarRealizedVolatilityProvider, BarRealizedVolatilityProvider.spec(3, available_lag=LAG)),
    (BarVolumeSumProvider, BarVolumeSumProvider.spec(3, available_lag=LAG)),
)


def _klines(count: int, first_ms: int, *, base: int = 100, volume: int = 10) -> list[list[Any]]:
    """Lawful minutes with uneven closes (so log returns differ)."""
    out = []
    for i in range(count):
        o = Decimal(base) + i
        close = o + Decimal("0.5") + Decimal(i % 3) / 4
        out.append(
            ss.kline_item(
                first_ms + i * M,
                open_=f"{o:.8f}",
                high=f"{close + 2:.8f}",
                low=f"{o - 1:.8f}",
                close=f"{close:.8f}",
                volume=f"{volume + i:.8f}",
                quote_volume="1000.00000000",
                trades=5 + i,
                taker_base="4.00000000",
                taker_quote="400.00000000",
            )
        )
    return out


def _archive_then_rest(h: RestHarness, *, rest_base: int = 200, rest_volume: int = 20) -> None:
    """Archive minutes 22:04..22:13 known first; REST minutes 22:14..22:18 known later."""
    archive = c.ingest_archive(
        h, "klines_1m", ss.archive_kline_lines(_klines(10, ss.T0 - 10 * M)), knowledge=K_ARCHIVE
    )
    c.normalizer(h, clock=StepClock(start=N_ARCHIVE)).normalize_unit(
        c.ARCHIVE_KLINES.table, archive
    )
    [response] = c.ingest_rest(
        h, "klines_1m", _klines(5, ss.T0, base=rest_base, volume=rest_volume), knowledge=K_REST
    )
    c.normalizer(h, clock=StepClock(start=N_REST)).normalize_unit(c.REST_KLINES.table, response)


def _observations(h: RestHarness, spec: PointInTimeSpec) -> tuple[FeatureObservation, ...]:
    selection = PitSelector(
        h.adapter, h.storage, canonical_scratch_directory=h.canonical_scratch_directory
    ).select(spec, "klines_1m", SYMBOL, DAY_START, DAY_END)
    return bar_observations(selection, spec)


def _times(observations: tuple[FeatureObservation, ...]) -> tuple[datetime, ...]:
    archive = max(o.available_time for o in observations if "archive" in o.lineage.source_table)
    rest = min(o.available_time for o in observations if "rest" in o.lineage.source_table)
    return (
        S0 + LAG,  # nothing available yet
        archive + LAG,  # the archive minutes
        rest + LAG - timedelta(microseconds=1),  # still only the archive minutes
        rest + LAG + timedelta(minutes=1),  # archive + REST minutes
        S1 - timedelta(days=1),
    )


def _run(h: RestHarness, times: tuple[datetime, ...] | None = None) -> dict[str, FeatureResult]:
    spec = _spec(h, cutoff=FAR, interval=(S0, S1))
    observations = _observations(h, spec)
    out = {}
    for provider, feature in PROVIDERS:
        request = pit_feature_request(
            pit_spec=spec,
            observations=observations,
            feature=feature,
            evaluation_times=times or _times(observations),
            manifest_content_hash=MANIFEST,
        )
        out[feature.name] = run_feature(provider((feature,)), feature, request)
    return out


def test_the_pipeline_is_reproducible_across_runs_and_catalogs(h: RestHarness) -> None:
    _archive_then_rest(h)
    first = _run(h)
    assert _run(h) == first  # a fresh selector, observations, request and provider
    with ss.sqlite_harness(h.tmp_path / "other") as other:
        _archive_then_rest(other)
        independent = _run(other)
    assert {name: result.result_hash for name, result in independent.items()} == {
        name: result.result_hash for name, result in first.items()
    }
    volume = [item.value for item in first["bar_volume_sum_3"].values]
    # nothing yet; the last three archive minutes (17 + 18 + 19); then the REST minutes 22..24.
    assert volume == [None, Decimal(54), Decimal(54), Decimal(69), Decimal(69)]
    assert (
        all(item.value is not None for item in first["bar_realized_vol_3"].values[1:])
        and first["bar_realized_vol_3"].values[0].value is None
    )


def test_later_data_cannot_change_earlier_values(h: RestHarness) -> None:
    """Perturb only the REST minutes (known later): values before they are visible do not move."""
    _archive_then_rest(h)
    times = _times(_observations(h, _spec(h, cutoff=FAR, interval=(S0, S1))))
    base = _run(h, times)
    with ss.sqlite_harness(h.tmp_path / "other") as other:
        _archive_then_rest(other, rest_base=300, rest_volume=90)
        perturbed = _run(other, times)
    for name, result in base.items():
        before = [item for item in result.values if item.evaluation_time < times[3]]
        after = [item for item in result.values if item.evaluation_time >= times[3]]
        other_values = {item.evaluation_time: item for item in perturbed[name].values}
        assert all(other_values[item.evaluation_time] == item for item in before), name
        assert any(other_values[item.evaluation_time] != item for item in after), name
        assert perturbed[name].result_hash != result.result_hash


def test_visible_sets_are_the_pit_selection_at_t_minus_lag(h: RestHarness) -> None:
    """REST available first, then the equal archive copy supersedes it (D-33 edge): each
    evaluation time sees exactly the revisions a point selection at ``t - lag`` selects."""
    items = _klines(5, ss.T0)
    # Retrieved ten minutes after the first minute opened: available long before the archive.
    [response] = c.ingest_rest(
        h, "klines_1m", items, knowledge=K_ARCHIVE, retrieved_ms=ss.T0 + 10 * M
    )
    archive = c.ingest_archive(h, "klines_1m", ss.archive_kline_lines(items), knowledge=K_REST)
    c.normalizer(h, clock=StepClock(start=N_ARCHIVE)).normalize_unit(c.REST_KLINES.table, response)
    c.normalizer(h, clock=StepClock(start=N_REST)).normalize_unit(c.ARCHIVE_KLINES.table, archive)
    h.reconciler(clock=StepClock(start=K_EDGE)).reconcile("klines_1m", SYMBOL, ss.DAY)

    start = utc(2023, 11, 14, 22)  # before the REST copy is available
    spec = _spec(h, cutoff=FAR, interval=(start, S1))
    observations = _observations(h, spec)
    assert len(observations) == 10  # both copies of every minute were selected at some time
    changes = sorted({o.available_time for o in observations})
    times = tuple(
        sorted(
            {
                start + LAG,
                *(at + LAG for at in changes),
                *(at + LAG - timedelta(microseconds=1) for at in changes),
            }
        )
    )
    feature = BarVolumeSumProvider.spec(5, available_lag=LAG)
    request = pit_feature_request(
        pit_spec=spec,
        observations=observations,
        feature=feature,
        evaluation_times=times,
        manifest_content_hash=MANIFEST,
    )
    selector = PitSelector(
        h.adapter, h.storage, canonical_scratch_directory=h.canonical_scratch_directory
    )
    tables = set()
    for at in times:
        point = _spec(h, cutoff=FAR, at=at - LAG)
        selected = {
            item.selected_revision_id
            for item in selector.select(point, "klines_1m", SYMBOL, DAY_START, DAY_END).selections
            if item.status is PointInTimeStatus.SELECTED
        }
        visible = request.visible_at(at, LAG)
        assert {o.lineage.canonical_revision_id for o in visible} == selected, at
        tables.add(frozenset(o.lineage.raw_table for o in visible))
    assert (
        frozenset({c.REST_KLINES.table}) in tables and frozenset({c.ARCHIVE_KLINES.table}) in tables
    )
    result = run_feature(BarVolumeSumProvider((feature,)), feature, request)
    assert [item.value for item in result.values][-1] == Decimal(10 + 11 + 12 + 13 + 14)


def test_derived_bars_feed_features_with_lineage(h: RestHarness) -> None:
    archive = c.ingest_archive(
        h, "klines_1m", ss.archive_kline_lines(_klines(12, ss.T0 - 9 * M)), knowledge=K_ARCHIVE
    )  # 22:05 .. 22:16: two complete 5-minute bars and an incomplete third
    c.normalizer(h, clock=StepClock(start=N_ARCHIVE)).normalize_unit(
        c.ARCHIVE_KLINES.table, archive
    )
    spec = _spec(h, cutoff=FAR)  # point selection at FAR (E4 needs one)
    selection = PitSelector(
        h.adapter, h.storage, canonical_scratch_directory=h.canonical_scratch_directory
    ).select(spec, "klines_1m", SYMBOL, DAY_START, DAY_END)
    bars = resample_bars(selection, 5, DAY_START, DAY_END)
    assert [bar.complete for bar in bars] == [True, True, False]
    observations = derived_bar_observations(bars, selection, spec)
    assert len(observations) == 2  # the incomplete bar is a gap, never a filled bar
    by_revision = {item.canonical_revision_id: item for item in selection.lineage}
    for bar, item in zip(bars, observations, strict=False):
        assert item.lineage == by_revision[bar.constituents[-1]]
        assert item.values["resample_content"] == f"sha256:{bar.content_sha256}"
        assert item.values["minutes"] == 5
    five = Ref(kind=Kind.REPRESENTATION, name="canonical_resample_5m", version="1.0.0")
    feature = BarVolumeSumProvider.spec(2, available_lag=LAG, bar_input=five)
    request = pit_feature_request(
        pit_spec=spec,
        observations=observations,
        feature=feature,
        evaluation_times=(FAR + LAG,),
        manifest_content_hash=MANIFEST,
    )
    [value] = run_feature(BarVolumeSumProvider((feature,)), feature, request).values
    assert value.value == Decimal(sum(10 + i for i in range(10)))
    assert value.inputs_used == 2


def test_evaluation_times_outside_the_pit_view_are_refused(h: RestHarness) -> None:
    _archive_then_rest(h)
    interval = _spec(h, cutoff=FAR, interval=(S0, S1))
    observations = _observations(h, interval)
    feature = BarVolumeSumProvider.spec(3, available_lag=LAG)
    for at in (S0 + LAG - timedelta(microseconds=1), S1 + LAG):
        with pytest.raises(FeatureInputBuildError, match="does not cover"):
            pit_feature_request(
                pit_spec=interval,
                observations=observations,
                feature=feature,
                evaluation_times=(at,),
                manifest_content_hash=MANIFEST,
            )
    point = _spec(h, cutoff=FAR, at=utc(2023, 12, 20))
    with pytest.raises(FeatureInputBuildError, match="does not cover"):
        pit_feature_request(
            pit_spec=point,
            observations=_observations(h, point),
            feature=feature,
            evaluation_times=(utc(2023, 12, 20),),  # t - lag is before the point
            manifest_content_hash=MANIFEST,
        )
    with pytest.raises(FeatureInputBuildError, match="another knowledge_cutoff"):
        selection = PitSelector(
            h.adapter, h.storage, canonical_scratch_directory=h.canonical_scratch_directory
        ).select(interval, "klines_1m", SYMBOL, DAY_START, DAY_END)
        bar_observations(selection, _spec(h, cutoff=utc(2029, 1, 1), interval=(S0, S1)))


def test_conflicts_and_other_tables_are_refused(h: RestHarness) -> None:
    _archive_then_rest(h)
    spec = _spec(h, cutoff=FAR, interval=(S0, S1))
    selection = PitSelector(
        h.adapter, h.storage, canonical_scratch_directory=h.canonical_scratch_directory
    ).select(spec, "klines_1m", SYMBOL, DAY_START, DAY_END)
    conflicted = PitSelection(**{**_fields(selection), "conflicts": ("some-key",)})
    with pytest.raises(FeatureInputBuildError, match="competing heads"):
        bar_observations(conflicted, spec)
    trades = PitSelection(**{**_fields(selection), "canonical_table": "canonical.trades"})
    with pytest.raises(FeatureInputBuildError, match="only canonical.bars_1m"):
        bar_observations(trades, spec)


def _fields(selection: PitSelection) -> dict[str, Any]:
    return {name: getattr(selection, name) for name in PitSelection.__dataclass_fields__}


def test_the_request_binds_the_pit_cutoff(h: RestHarness) -> None:
    _archive_then_rest(h)
    spec = _spec(h, cutoff=FAR, interval=(S0, S1))
    observations = _observations(h, spec)
    request: FeatureRequest = pit_feature_request(
        pit_spec=spec,
        observations=observations,
        feature=PROVIDERS[0][1],
        evaluation_times=_times(observations),
        manifest_content_hash=MANIFEST,
    )
    assert request.knowledge_cutoff == spec.knowledge_cutoff
    assert all(o.knowledge_time <= spec.knowledge_cutoff for o in request.observations)

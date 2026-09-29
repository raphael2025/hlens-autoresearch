"""F4 feature requests bound to a v3 evidence manifest (ADR-0077; C1-CONSUMERS).

``feature_request_from_dataset`` with an ``evidence_verifier`` loads either form through
``ManifestStore.load_any``. A v3 manifest's observations are proven by a chunk walk in lockstep
with its ``lineage`` / ``evidence_gaps`` streams (no re-selection): the request equals the v2
request of the same world but for the manifest hash, and every alteration the v2 path refuses is
refused. A v2 hash with a verifier is the v2 path; a v3 hash without one, and derived bars over a
v3 manifest (v2-only), are refused by the store. Same SQLite ``World`` as the F4 tests; real v3
builds (``tests.infrastructure.bars.v3_support``).
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from core.contracts.feature import FeatureObservation, FeatureRequest
from core.contracts.revision import PointInTimeSpec
from infrastructure.dataset.manifests import ManifestFormError
from infrastructure.feature.dataset import (
    DatasetBindingError,
    feature_request_from_dataset,
    feature_request_from_derived_bars,
)
from infrastructure.feature.observations import bar_observations, derived_bar_observations
from infrastructure.canonical.resample import resample_bars
from infrastructure.feature.runner import run_feature
from infrastructure.pit.selector import PitSelector
from plugins.features import BarVolumeSumProvider
from tests.infrastructure.bars import v3_support as v
from tests.infrastructure.dataset import dataset_support as ds
from tests.infrastructure.dataset.dataset_support import World
from tests.infrastructure.feature.test_feature_dataset import ALTERED, FEATURE, FIVE_S, LAG
from tests.infrastructure.redteam import redteam_support as rt
from tests.infrastructure.revision.rest_store_support import SYMBOL, utc


@pytest.fixture
def w(tmp_path: Path) -> Iterator[World]:
    with ds.sqlite_world(tmp_path) as opened:
        yield opened


class _Datasets:
    """A v2 and a v3 bar dataset of one spec (taken before both builds)."""

    def __init__(self, w: World, spec: PointInTimeSpec) -> None:
        self.w, self.spec = w, spec
        self.machinery = v.v3(w)
        built = rt.build(w, spec, data_type="klines_1m", window=v.DAY_WINDOW)
        self.v2_hash = built.manifest.content_hash()
        self.v3_hash = self.machinery.build(spec).manifest_hash

    def observations(self) -> tuple[FeatureObservation, ...]:
        selection = PitSelector(
            self.w.h.adapter,
            self.w.h.storage,
            canonical_scratch_directory=self.w.h.canonical_scratch_directory,
        ).select(self.spec, "klines_1m", SYMBOL, *v.DAY_WINDOW)
        return bar_observations(selection, self.spec)

    def derived_observations(self, minutes: int) -> tuple[FeatureObservation, ...]:
        selection = PitSelector(
            self.w.h.adapter,
            self.w.h.storage,
            canonical_scratch_directory=self.w.h.canonical_scratch_directory,
        ).select(self.spec, "klines_1m", SYMBOL, *v.DAY_WINDOW)
        return derived_bar_observations(
            resample_bars(selection, minutes, *v.DAY_WINDOW), selection, self.spec
        )

    def request(
        self,
        times: Sequence[datetime],
        *,
        v3: bool,
        observations: Sequence[FeatureObservation] | None = None,
        **kwargs: Any,
    ) -> FeatureRequest:
        return feature_request_from_dataset(
            self.w.h.adapter,
            self.w.h.storage,
            builder=self.w.builder(),
            manifest_content_hash=kwargs.pop("manifest_hash", self.v3_hash if v3 else self.v2_hash),
            pit_spec=kwargs.pop("pit_spec", self.spec),
            observations=self.observations() if observations is None else observations,
            feature=FEATURE,
            evaluation_times=times,
            evidence_verifier=kwargs.pop(
                "evidence_verifier", self.machinery.verifier if v3 else None
            ),
        )


def _datasets(w: World, *, interval: bool = False) -> _Datasets:
    v.ingest(w)
    return _Datasets(w, v.spec_of(w, interval=v.INTERVAL if interval else None))


def _same_but_the_hash(ours: FeatureRequest, theirs: FeatureRequest, v3_hash: str) -> None:
    assert ours.manifest_content_hash == v3_hash != theirs.manifest_content_hash
    assert ours.model_copy(update={"manifest_content_hash": theirs.manifest_content_hash}) == (
        theirs
    )
    provider = BarVolumeSumProvider((FEATURE,))
    assert run_feature(provider, FEATURE, ours).values == (
        run_feature(provider, FEATURE, theirs).values
    )


# ---------------------------------------------------------------------------------------------
# equivalence with the v2 path


def test_a_v3_point_request_is_the_v2_request_but_for_the_manifest_hash(w: World) -> None:
    data = _datasets(w)
    times = (ds.SIM + LAG,)
    _same_but_the_hash(data.request(times, v3=True), data.request(times, v3=False), data.v3_hash)


def test_a_v3_interval_request_keeps_the_effective_times_and_membership(w: World) -> None:
    """ADR-0032: the archive bars enter at close + 5 s, exactly as on the v2 path."""
    data = _datasets(w, interval=True)
    at = utc(2023, 11, 14, 22, 17) + FIVE_S + LAG  # the third bar's effective time, at the lag
    times = (at - timedelta(seconds=1), at)
    ours = data.request(times, v3=True)
    assert len(ours.observations) == v.BARS
    _same_but_the_hash(ours, data.request(times, v3=False), data.v3_hash)


# ---------------------------------------------------------------------------------------------
# the same refusals as the v2 path


@pytest.mark.parametrize("change", sorted(ALTERED))
def test_observations_that_are_not_the_v3_datasets_rows_are_refused(w: World, change: str) -> None:
    data = _datasets(w)
    altered = ALTERED[change](data.observations())
    for on_v3 in (False, True):
        with pytest.raises(DatasetBindingError):
            data.request((ds.SIM + LAG,), v3=on_v3, observations=altered)


def test_another_spec_than_the_v3_manifests_is_refused(w: World) -> None:
    data = _datasets(w)
    plain = data.spec.model_copy(update={"availability_bindings": w.spec().availability_bindings})
    with pytest.raises(DatasetBindingError, match="another PIT spec"):
        data.request((ds.SIM + LAG,), v3=True, pit_spec=plain)


def test_empty_observations_make_no_v3_request(w: World) -> None:
    data = _datasets(w)
    with pytest.raises(DatasetBindingError, match="needs the dataset's observations"):
        data.request((ds.SIM + LAG,), v3=True, observations=())


def test_a_v3_interval_evaluation_outside_the_members_span_is_refused(w: World) -> None:
    """The universe gates rows: once BTCUSDT halts, its bars are no dataset rows any more."""
    halt = utc(2023, 11, 14, 22, 30)
    w.listed()
    w.listed({"BTCUSDT": "HALT", "ETHUSDT": "TRADING"}, at=halt)
    w.bars()
    w.report("klines_1m")
    data = _Datasets(w, v.spec_of(w, interval=v.INTERVAL))
    still = halt - timedelta(seconds=1) + LAG
    for on_v3 in (False, True):
        request = data.request((still,), v3=on_v3)
        assert len(request.visible_at(still, LAG)) == 3  # still a member
        with pytest.raises(DatasetBindingError, match="not a member"):
            data.request((halt + LAG,), v3=on_v3)


# ---------------------------------------------------------------------------------------------
# v2 unchanged; v3 needs its verifier; derived bars are re-derived per UTC-day slice


def test_a_v2_hash_with_an_evidence_verifier_is_exactly_the_v2_path(w: World) -> None:
    data = _datasets(w)
    times = (ds.SIM + LAG,)
    with_verifier = data.request(times, v3=False, evidence_verifier=data.machinery.verifier)
    assert with_verifier == data.request(times, v3=False)


def test_a_v3_hash_without_an_evidence_verifier_is_refused(w: World) -> None:
    data = _datasets(w)
    with pytest.raises(ManifestFormError):
        data.request((ds.SIM + LAG,), v3=True, evidence_verifier=None)


def test_a_v3_derived_request_is_the_v2_request_but_for_the_manifest_hash(w: World) -> None:
    data = _datasets(w)
    derived = data.derived_observations(1)
    times = (ds.SIM + LAG,)
    ours = feature_request_from_derived_bars(
        w.h.adapter,
        w.h.storage,
        builder=w.builder(),
        manifest_content_hash=data.v3_hash,
        pit_spec=data.spec,
        minutes=1,
        observations=derived,
        feature=FEATURE,
        evaluation_times=times,
        evidence_verifier=data.machinery.verifier,
    )
    theirs = feature_request_from_derived_bars(
        w.h.adapter,
        w.h.storage,
        builder=w.builder(),
        manifest_content_hash=data.v2_hash,
        pit_spec=data.spec,
        minutes=1,
        observations=derived,
        feature=FEATURE,
        evaluation_times=times,
    )
    _same_but_the_hash(ours, theirs, data.v3_hash)


def test_a_v2_derived_hash_with_an_evidence_verifier_is_exactly_the_v2_path(w: World) -> None:
    data = _datasets(w)
    observations = data.derived_observations(1)
    args = {
        "adapter": w.h.adapter,
        "storage": w.h.storage,
        "builder": w.builder(),
        "manifest_content_hash": data.v2_hash,
        "pit_spec": data.spec,
        "minutes": 1,
        "observations": observations,
        "feature": FEATURE,
        "evaluation_times": (ds.SIM + LAG,),
    }
    with_verifier = feature_request_from_derived_bars(
        **args, evidence_verifier=data.machinery.verifier
    )
    without_verifier = feature_request_from_derived_bars(**args)
    assert with_verifier == without_verifier


def test_a_v3_derived_hash_without_an_evidence_verifier_is_refused(w: World) -> None:
    data = _datasets(w)
    with pytest.raises(ManifestFormError):
        feature_request_from_derived_bars(
            w.h.adapter,
            w.h.storage,
            builder=w.builder(),
            manifest_content_hash=data.v3_hash,
            pit_spec=data.spec,
            minutes=1,
            observations=data.derived_observations(1),
            feature=FEATURE,
            evaluation_times=(ds.SIM + LAG,),
        )


def test_v3_derived_observations_missing_a_bar_are_refused(w: World) -> None:
    data = _datasets(w)
    observations = data.derived_observations(1)
    with pytest.raises(DatasetBindingError, match="not exactly"):
        feature_request_from_derived_bars(
            w.h.adapter,
            w.h.storage,
            builder=w.builder(),
            manifest_content_hash=data.v3_hash,
            pit_spec=data.spec,
            minutes=1,
            observations=observations[:-1],
            feature=FEATURE,
            evaluation_times=(ds.SIM + LAG,),
            evidence_verifier=data.machinery.verifier,
        )

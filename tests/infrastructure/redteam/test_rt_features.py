"""G2 / feature runs over a Research Dataset whose manifest was tampered with.

F4 binds every ``FeatureRequest`` to a manifest by ``manifest_content_hash``. A request bound to
a manifest that does not load (forged row, no such manifest) or that describes another PIT spec
than the observations were selected under must never produce a ``FeatureResult``.

RT-6 (fixed in G2-R1c): a feature run over a Research Dataset is built by
``feature_request_from_dataset``, which loads the manifest through ``ManifestStore`` and proves the
observations against its PIT spec, dataset snapshot and lineage. ``pit_feature_request`` stays the
ad-hoc path and makes no dataset claim.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import timedelta
from typing import Any

import pytest

from core.contracts.feature import FeatureResult
from core.domain.base import content_hash
from infrastructure.catalog.iceberg_adapter import CatalogIntegrityError
from infrastructure.catalog.phase1_tables import DATASET_MANIFESTS
from infrastructure.dataset.builder import DatasetBuilt
from infrastructure.dataset.manifests import ManifestStore
from infrastructure.feature.dataset import feature_request_from_dataset
from infrastructure.feature.observations import FeatureInputBuildError, bar_observations
from infrastructure.feature.runner import FeatureRunnerError, run_feature
from infrastructure.pit.selector import PitSelector
from plugins.features import BarVolumeSumProvider
from tests.infrastructure.dataset import dataset_support as ds
from tests.infrastructure.dataset.dataset_support import World
from tests.infrastructure.redteam import redteam_support as rt
from tests.infrastructure.revision.rest_store_support import SYMBOL, utc

LAG = timedelta(minutes=1)
DAY_WINDOW = (utc(2023, 11, 14), utc(2023, 11, 15))
FEATURE = BarVolumeSumProvider.spec(2, available_lag=LAG)


def _bars_dataset(w: World) -> DatasetBuilt:
    w.listed()
    w.bars()
    w.report("klines_1m")
    return rt.build(w, data_type="klines_1m", window=DAY_WINDOW)


def _run(w: World, built: DatasetBuilt, manifest_hash: str, **spec: Any) -> FeatureResult:
    """Observations selected under the manifest's spec (or ``spec`` overrides), then F4."""
    pit = built.manifest.point_in_time.model_copy(update=spec)
    selection = PitSelector(w.h.adapter, w.h.storage).select(pit, "klines_1m", SYMBOL, *DAY_WINDOW)
    request = feature_request_from_dataset(
        w.h.adapter,
        w.h.storage,
        manifests=w.builder().manifests(),
        pit_spec=pit,
        observations=bar_observations(selection, pit),
        feature=FEATURE,
        evaluation_times=(ds.SIM + LAG,),
        manifest_content_hash=manifest_hash,
    )
    return run_feature(BarVolumeSumProvider((FEATURE,)), FEATURE, request)


def test_the_genuine_manifest_feeds_a_feature_run(w: World) -> None:
    built = _bars_dataset(w)
    result = _run(w, built, built.manifest.content_hash())
    [value] = result.values
    assert value.value is not None and value.inputs_used == 2


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
def test_a_feature_run_bound_to_a_manifest_that_does_not_load_is_refused(
    w: World, attack: str
) -> None:
    built = _bars_dataset(w)
    manifest_hash = TAMPERED[attack](w, built)
    with pytest.raises((FeatureInputBuildError, FeatureRunnerError, CatalogIntegrityError)):
        _run(w, built, manifest_hash)


def test_observations_selected_under_another_spec_cannot_borrow_a_manifest(w: World) -> None:
    built = _bars_dataset(w)
    other = utc(2023, 12, 18)  # another simulation time and knowledge cutoff than the manifest's
    assert other != built.manifest.point_in_time.knowledge_cutoff
    with pytest.raises((FeatureInputBuildError, FeatureRunnerError)):
        _run(w, built, built.manifest.content_hash(), simulation_time=other, knowledge_cutoff=other)

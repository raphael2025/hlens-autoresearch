"""Feature requests bound to a persisted, verified Research Dataset manifest (Phase 1 F4; G2 RT-6).

``FeatureRequest.manifest_content_hash`` claims that the observations belong to one Research
Dataset. ``pit_feature_request`` (and ``run_feature`` on what it returns) takes that hash on trust:
it is the ad-hoc / test path. **Only a request built by ``feature_request_from_dataset`` (Canonical
1-minute bars) or ``feature_request_from_derived_bars`` (E4 derived bars) is a feature run over a
Research Dataset**; each proves the claim before a request exists:

1. **manifest** — loaded through ``ManifestStore.load`` (the row's JSON re-hashed, the contract
   re-validated, every column re-derived). No persisted manifest under the hash, or a row that does
   not prove, refuses the request (``DatasetBindingError`` / ``CatalogIntegrityError``);
2. **PIT spec** — the spec the caller selected its observations under must be the manifest's own
   ``point_in_time`` (same content hash): another simulation time, cutoff, snapshot or policy
   binding (e.g. the ADR-0032 assumption bound or not) is refused;
3. **dataset snapshot** — the dataset's rows are read at the manifest's own ``DatasetRef``
   snapshot under the selection id the manifest determines (``selection_id_of``: rule, PIT spec,
   universe binding, ``klines_1m``, window). Every observation's revision must be a row there, with
   the row's observation key and event time (a derived bar: its closing constituent's row, inside
   the bar's interval);
4. **lineage** — every observation's ``SelectedRevisionLineage`` must be, field for field, one the
   manifest binds;
5. **content** — for every symbol the observations cover, the Canonical bars are re-selected by
   ``PitSelector`` under the manifest's spec over the dataset window, and the caller's
   observations of that symbol must be **exactly** ``bar_observations`` of that proven selection,
   restricted to the dataset's rows (nothing added, dropped or altered). ``available_time`` is
   therefore the one ``selected_rows`` carries — the ADR-0032 effective time when the manifest's
   spec binds the assumption, the stored one otherwise;
6. **membership** — for an interval spec, at the PIT view ``t - available_lag`` of every
   evaluation time, each observation the request makes visible must be the dataset row effective
   at that view (the dataset gates rows to member spans; a symbol outside the universe at ``t`` has
   no row there). A point spec's rows have no span: their existence is the membership.

**Derived bars** (F4-R2): ``resample_bars`` is a point-simulation rule, so the manifest's spec must
be a point spec. Step 5 becomes a **re-derivation**: the re-selection of each covered symbol must
select exactly the dataset's rows of that symbol (every constituent a dataset row, none missing),
and the caller's observations of that symbol must be **exactly** ``derived_bar_observations`` of
``resample_bars(selection, minutes, dataset window)`` — nothing added, dropped, moved or altered.
Each derived ``available_time`` is computed from the effective times ``selected_rows`` carries
(ADR-0032 when the manifest's spec binds the assumption). Step 6 does not apply (point spec).

The request is then ``pit_feature_request`` under the manifest's spec (evaluation times answerable
by its PIT view) with the manifest's own content hash.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any, Final

from pyiceberg.expressions import EqualTo

from core.contracts.feature import FeatureObservation, FeatureRequest
from core.contracts.revision import PointInTimeSpec, PointInTimeStatus
from core.contracts.storage import StorageAdapter
from core.contracts.universe import ResearchDatasetManifest
from core.domain.specs import FeatureSpec
from infrastructure.canonical import rules
from infrastructure.canonical.resample import ResampleError, resample_bars
from infrastructure.catalog.iceberg_adapter import CatalogIntegrityError
from infrastructure.dataset.builder import DatasetBuilder, selection_id_of
from infrastructure.dataset.selection import SELECTION_SCHEMA
from infrastructure.feature.observations import (
    FeatureInputBuildError,
    bar_observations,
    derived_bar_observations,
    pit_feature_request,
)
from infrastructure.pit.selector import PitSelection, PitSelector
from infrastructure.revision.store import RevisionCatalog

__all__ = [
    "DatasetBindingError",
    "feature_request_from_dataset",
    "feature_request_from_derived_bars",
    "load_manifest",
]

_DATA_TYPE: Final = "klines_1m"
_BARS_TABLE: Final = rules.CANONICAL_TABLES[_DATA_TYPE].table
_COLUMNS: Final = tuple(field.name for field in SELECTION_SCHEMA.fields)
_VENUE_SYMBOL: Final[Mapping[str, str]] = {
    item.symbol: venue for venue, item in rules.SYMBOLS.items()
}


class DatasetBindingError(FeatureInputBuildError):
    """The observations cannot be proven to belong to the manifest's Research Dataset."""


def load_manifest(builder: DatasetBuilder, manifest_content_hash: str) -> ResearchDatasetManifest:
    """The persisted manifest, loaded through ``builder``'s own ``ManifestStore`` — which
    re-derives it (G2 RT-4) — never through a caller-supplied verifier (G2-R2, cursor review).
    Absent: ``DatasetBindingError``."""
    if not isinstance(builder, DatasetBuilder):
        raise DatasetBindingError("a DatasetBuilder is needed to verify the manifest")
    manifest = builder.manifests().load(manifest_content_hash)
    if manifest is None:
        raise DatasetBindingError(
            f"no Research Dataset manifest is persisted as {manifest_content_hash}"
        )
    return manifest


def feature_request_from_dataset(
    adapter: RevisionCatalog,
    storage: StorageAdapter,
    *,
    builder: DatasetBuilder,
    manifest_content_hash: str,
    pit_spec: PointInTimeSpec,
    observations: Sequence[FeatureObservation],
    feature: FeatureSpec,
    evaluation_times: Sequence[datetime],
) -> FeatureRequest:
    """A request for ``feature`` over observations proven to be the manifest's dataset rows."""
    manifest = _bound_manifest(builder, manifest_content_hash, pit_spec, observations)
    rows = _dataset_rows(adapter, manifest)
    by_symbol = _by_symbol(manifest, rows, observations, derived=False)
    for symbol, given in sorted(by_symbol.items()):
        proven = _proven(adapter, storage, manifest, symbol, rows)
        _require_exact(symbol, given, proven, "bars")

    request = pit_feature_request(
        pit_spec=manifest.point_in_time,
        observations=observations,
        feature=feature,
        evaluation_times=evaluation_times,
        manifest_content_hash=manifest.content_hash(),
    )
    if manifest.point_in_time.simulation_time is None:
        _check_membership(request, feature, rows)
    return request


def feature_request_from_derived_bars(
    adapter: RevisionCatalog,
    storage: StorageAdapter,
    *,
    builder: DatasetBuilder,
    manifest_content_hash: str,
    pit_spec: PointInTimeSpec,
    minutes: int,
    observations: Sequence[FeatureObservation],
    feature: FeatureSpec,
    evaluation_times: Sequence[datetime],
) -> FeatureRequest:
    """A request for ``feature`` over ``minutes`` derived bars proven to be exactly
    ``resample_bars`` of the manifest's own dataset selection (F4-R2)."""
    manifest = _bound_manifest(builder, manifest_content_hash, pit_spec, observations)
    if manifest.point_in_time.simulation_time is None:
        raise DatasetBindingError("derived bars come from a point-simulation dataset (E4)")
    rows = _dataset_rows(adapter, manifest)
    by_symbol = _by_symbol(manifest, rows, observations, derived=True)
    for symbol, given in sorted(by_symbol.items()):
        proven = _proven_derived(adapter, storage, manifest, symbol, rows, minutes)
        _require_exact(symbol, given, proven, f"{minutes}-minute derived bars")

    return pit_feature_request(
        pit_spec=manifest.point_in_time,
        observations=observations,
        feature=feature,
        evaluation_times=evaluation_times,
        manifest_content_hash=manifest.content_hash(),
    )


# ---------------------------------------------------------------------------------------------


def _bound_manifest(
    builder: DatasetBuilder,
    manifest_content_hash: str,
    pit_spec: PointInTimeSpec,
    observations: Sequence[FeatureObservation],
) -> ResearchDatasetManifest:
    """Steps 1-2: the verified manifest, whose spec the observations were selected under."""
    manifest = load_manifest(builder, manifest_content_hash)
    spec = manifest.point_in_time
    if not isinstance(pit_spec, PointInTimeSpec) or pit_spec.content_hash() != spec.content_hash():
        raise DatasetBindingError(
            "the observations were selected under another PIT spec than the manifest binds"
        )
    if not observations:
        raise DatasetBindingError("a dataset feature request needs the dataset's observations")
    if not all(isinstance(item, FeatureObservation) for item in observations):
        raise DatasetBindingError("expected FeatureObservation instances")
    return manifest


def _dataset_rows(
    adapter: RevisionCatalog, manifest: ResearchDatasetManifest
) -> Mapping[str, list[Mapping[str, Any]]]:
    """revision -> its rows in the dataset's own snapshot, under the manifest's selection id."""
    dataset = manifest.dataset
    selection_id = selection_id_of(
        manifest.universe_spec,
        manifest.point_in_time,
        _DATA_TYPE,
        dataset.time_range_start,
        dataset.time_range_end,
    )
    found = adapter.scan_columns(
        dataset.table,
        columns=_COLUMNS,
        row_filter=EqualTo("selection_id", selection_id),  # type: ignore[call-arg, arg-type]
        snapshot_id=dataset.snapshot_id,
    ).to_pylist()
    if not found:
        raise DatasetBindingError(
            f"the dataset snapshot has no {_DATA_TYPE} rows for this manifest (selection "
            f"{selection_id})"
        )
    rows: dict[str, list[Mapping[str, Any]]] = {}
    for row in found:
        if row["canonical_table"] != _BARS_TABLE:
            raise CatalogIntegrityError(f"dataset row of {row['canonical_table']} in a bar dataset")
        rows.setdefault(row["revision_id"], []).append(row)
    return rows


def _by_symbol(
    manifest: ResearchDatasetManifest,
    rows: Mapping[str, list[Mapping[str, Any]]],
    observations: Sequence[FeatureObservation],
    *,
    derived: bool,
) -> dict[str, list[FeatureObservation]]:
    """Steps 3-4: every observation's lineage is bound and its revision a dataset row; grouped by
    the row's symbol. A bar observation carries its row's key and event time; a derived bar's
    (closing constituent's) row lies inside the bar's interval."""
    lineage = set(manifest.lineage)
    by_symbol: dict[str, list[FeatureObservation]] = {}
    for item in observations:
        revision = item.lineage.canonical_revision_id
        if item.lineage not in lineage:
            raise DatasetBindingError(f"revision {revision}: lineage is not bound by the manifest")
        found = rows.get(revision)
        if not found:
            raise DatasetBindingError(f"revision {revision} is not a row of the dataset snapshot")
        head = found[0]
        if derived:
            end = item.event_end_time
            if end is None or not item.event_time <= head["event_time"] < end:
                raise DatasetBindingError(
                    f"revision {revision}: its row is not inside the derived bar's interval"
                )
        elif (head["observation_key"], head["event_time"]) != (
            item.observation_key,
            item.event_time,
        ):
            raise DatasetBindingError(f"revision {revision}: key / event time differ from its row")
        by_symbol.setdefault(head["symbol"], []).append(item)
    return by_symbol


def _reselect(
    adapter: RevisionCatalog,
    storage: StorageAdapter,
    manifest: ResearchDatasetManifest,
    symbol: str,
) -> PitSelection:
    """``symbol``'s Canonical bars re-selected under the manifest's spec over its window."""
    venue = _VENUE_SYMBOL.get(symbol)
    if venue is None:
        raise CatalogIntegrityError(f"dataset rows of an unknown symbol {symbol!r}")
    dataset = manifest.dataset
    selection = PitSelector(adapter, storage).select(
        manifest.point_in_time,
        _DATA_TYPE,
        venue,
        dataset.time_range_start,
        dataset.time_range_end,
    )
    selection.require_no_conflict()
    return selection


def _retained(rows: Mapping[str, list[Mapping[str, Any]]], symbol: str) -> set[str]:
    return {revision for revision, found in rows.items() if found[0]["symbol"] == symbol}


def _proven(
    adapter: RevisionCatalog,
    storage: StorageAdapter,
    manifest: ResearchDatasetManifest,
    symbol: str,
    rows: Mapping[str, list[Mapping[str, Any]]],
) -> tuple[FeatureObservation, ...]:
    """``bar_observations`` of ``symbol`` re-selected under the manifest spec; dataset rows."""
    selection = _reselect(adapter, storage, manifest, symbol)
    retained = _retained(rows, symbol)
    proven = tuple(
        item
        for item in bar_observations(selection, manifest.point_in_time)
        if item.lineage.canonical_revision_id in retained
    )
    if len(proven) != len(retained):
        raise CatalogIntegrityError(
            f"the dataset's {symbol} rows are not what its manifest's spec selects"
        )
    return proven


def _proven_derived(
    adapter: RevisionCatalog,
    storage: StorageAdapter,
    manifest: ResearchDatasetManifest,
    symbol: str,
    rows: Mapping[str, list[Mapping[str, Any]]],
    minutes: int,
) -> tuple[FeatureObservation, ...]:
    """``derived_bar_observations`` of ``resample_bars`` over ``symbol``'s re-selection, which must
    select exactly the dataset's rows of ``symbol`` (a point spec: a member keeps every selected
    bar of the window)."""
    selection = _reselect(adapter, storage, manifest, symbol)
    selected = {
        item.selected_revision_id
        for item in selection.selections
        if item.status is PointInTimeStatus.SELECTED and item.selected_revision_id is not None
    }
    if selected != _retained(rows, symbol):
        raise CatalogIntegrityError(
            f"the dataset's {symbol} rows are not what its manifest's spec selects"
        )
    dataset = manifest.dataset
    try:
        bars = resample_bars(selection, minutes, dataset.time_range_start, dataset.time_range_end)
    except ResampleError as exc:
        raise DatasetBindingError(
            f"the dataset's {symbol} bars cannot be resampled: {exc}"
        ) from exc
    return derived_bar_observations(bars, selection, manifest.point_in_time)


def _require_exact(
    symbol: str,
    given: Sequence[FeatureObservation],
    proven: Sequence[FeatureObservation],
    what: str,
) -> None:
    if _ordered(given) != _ordered(proven):
        raise DatasetBindingError(
            f"the {symbol} observations are not exactly the dataset's proven {what} "
            f"({len(given)} given, {len(proven)} proven)"
        )


def _ordered(items: Sequence[FeatureObservation]) -> list[FeatureObservation]:
    return sorted(items, key=lambda item: (item.available_time, item.observation_key))


def _check_membership(
    request: FeatureRequest, feature: FeatureSpec, rows: Mapping[str, list[Mapping[str, Any]]]
) -> None:
    lag = feature.available_lag
    for at in request.evaluation_times:
        view = at - lag
        for item in request.visible_at(at, lag):
            revision = item.lineage.canonical_revision_id
            spans = [(row["effective_from"], row["effective_until"]) for row in rows[revision]]
            if not any(
                low is not None and high is not None and low <= view < high for low, high in spans
            ):
                raise DatasetBindingError(
                    f"evaluation time {at.isoformat()}: revision {revision} is not the dataset's "
                    f"row at {view.isoformat()} (not selected, or its symbol not a member)"
                )

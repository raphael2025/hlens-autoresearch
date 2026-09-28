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

**v3 evidence manifests (ADR-0077; C1-CONSUMERS).** ``evidence_verifier`` (default ``None``: the
v2 path above, unchanged, and a v3 hash is refused by the store with ``ManifestFormError``) is the
``StreamingEvidenceVerifier`` of the builder's own catalog. With it, step 1 is
``ManifestStore.load_any``: a v2 hash takes the v2 path unchanged; a v3 hash is proven by the
streaming verifier (a bounded re-derivation merged record by record against the manifest's
evidence streams and chunk table, ADR-0077 §6), and the request is then proven **without
re-selecting** and without any whole-dataset tuple:

- steps 3 / 5 read the dataset's rows chunk by chunk (``iter_dataset_chunks``: one chunk at the
  manifest's own snapshot, ordinals contiguous) in lockstep with the ``lineage`` and
  ``evidence_gaps`` streams (``iter_evidence``, root-hash authenticated): every revision's first
  row must be the next data lineage record, and a data gap is its revision's. The Canonical rows of
  one chunk's revisions are read at the spec's bound Canonical snapshot (exactly one row each; its
  key, event time, symbol, lineage and gap must be the chunk row's and the streams') and become
  observations equal to ``bar_observations``' (the ADR-0032 effective ``available_time`` exactly
  when the spec binds it). The Canonical rows themselves were re-verified against their units by
  the verifier's re-derivation (``PitSelector.iter_bounded``) at the same immutable snapshot;
- step 4 is the exact comparison itself (an observation's lineage is part of it);
- step 6 keeps the spans of the requested symbols' rows only.

The v3 path holds one chunk plus what the request itself carries (its observations and, for an
interval spec, their rows' spans). Derived bars are re-derived one UTC-day slice at a time from
the verified v3 observations; a v3 call requires the verifier. The verifier's catalog and evidence
reader are read through its public ``adapter`` and ``builder`` properties; it must prove manifests
of its own catalog.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Final

from pyiceberg.expressions import And, EqualTo, In

from core.contracts.feature import FeatureObservation, FeatureRequest
from core.contracts.revision import PointInTimeSelection, PointInTimeSpec, PointInTimeStatus
from core.contracts.storage import StorageAdapter
from core.contracts.universe import (
    AvailabilityEvidenceGap,
    EvidenceStream,
    ResearchDatasetEvidenceManifest,
    ResearchDatasetManifest,
    SelectedRevisionLineage,
)
from core.domain.base import Contract, FrozenMapping
from core.domain.specs import FeatureSpec
from infrastructure.canonical import rules
from infrastructure.canonical.resample import ResampleError, resample_bars
from infrastructure.catalog.iceberg_adapter import CatalogIntegrityError
from infrastructure.catalog.phase1_tables import (
    CANONICAL_INSTRUMENT_LISTINGS,
    DATASET_SELECTION_CHUNKS,
)
from infrastructure.dataset.builder import DatasetBuilder, DatasetEvidenceBuilder, selection_id_of
from infrastructure.dataset.evidence import evidence_record_bytes
from infrastructure.dataset.manifests import ManifestStore
from infrastructure.dataset.selection import SELECTION_SCHEMA
from infrastructure.dataset.verify_v3 import StreamingEvidenceVerifier
from infrastructure.feature.observations import (
    BAR_VALUE_COLUMNS,
    FeatureInputBuildError,
    bar_observations,
    derived_bar_observations,
    pit_feature_request,
)
from infrastructure.pit.assumption import assumption_bound, effective_available_times
from infrastructure.pit.selector import PitSelection, PitSelector
from infrastructure.revision.store import RevisionCatalog

__all__ = [
    "AnyDatasetManifest",
    "DatasetBindingError",
    "DatasetRowEvidence",
    "dataset_chunk_observations",
    "evidence_catalog",
    "evidence_lineage_tables",
    "feature_request_from_dataset",
    "feature_request_from_derived_bars",
    "iter_dataset_chunks",
    "iter_manifest_evidence",
    "load_any_manifest",
    "load_manifest",
]

#: A persisted Research Dataset manifest of either form (v2 ``ResearchDatasetManifest``, v3
#: ``ResearchDatasetEvidenceManifest``; ADR-0077 §8.2).
type AnyDatasetManifest = ResearchDatasetManifest | ResearchDatasetEvidenceManifest

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


def load_any_manifest(
    builder: DatasetBuilder,
    manifest_content_hash: str,
    evidence_verifier: StreamingEvidenceVerifier | None = None,
) -> AnyDatasetManifest:
    """The persisted manifest of either form, proven by its own verifier (module docs, v3).

    ``evidence_verifier`` None: exactly ``load_manifest`` (v2; a v3 hash is ``ManifestFormError``).
    Otherwise ``ManifestStore.load_any`` over the builder's own catalog, with the builder as the v2
    verifier and ``evidence_verifier`` (which must prove manifests of that same catalog) as the v3
    one. Absent: ``DatasetBindingError``.
    """
    if evidence_verifier is None:
        return load_manifest(builder, manifest_content_hash)
    if not isinstance(builder, DatasetBuilder):
        raise DatasetBindingError("a DatasetBuilder is needed to verify the manifest")
    catalog = evidence_catalog(evidence_verifier)
    if builder.adapter is not catalog:
        raise DatasetBindingError(
            "the evidence verifier proves manifests of another catalog than the builder's"
        )
    store = ManifestStore(catalog, builder, evidence_verifier=evidence_verifier)
    manifest = store.load_any(manifest_content_hash)
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
    evidence_verifier: StreamingEvidenceVerifier | None = None,
) -> FeatureRequest:
    """A request for ``feature`` over observations proven to be the manifest's dataset rows.

    ``evidence_verifier``: module docs (v3); ``None`` is the v2 path, unchanged.
    """
    if evidence_verifier is None:
        manifest = _bound_manifest(builder, manifest_content_hash, pit_spec, observations)
    else:
        loaded = load_any_manifest(builder, manifest_content_hash, evidence_verifier)
        if isinstance(loaded, ResearchDatasetEvidenceManifest):
            return _evidence_feature_request(
                adapter,
                loaded,
                evidence_verifier,
                pit_spec=pit_spec,
                observations=observations,
                feature=feature,
                evaluation_times=evaluation_times,
            )
        manifest = _checked_binding(loaded, pit_spec, observations)
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
    evidence_verifier: StreamingEvidenceVerifier | None = None,
) -> FeatureRequest:
    """A request for ``feature`` over ``minutes`` derived bars proven to be exactly
    ``resample_bars`` of the manifest's own dataset selection (F4-R2).

    ``evidence_verifier``: module docs (v3); ``None`` is the v2 path, unchanged."""
    if evidence_verifier is None:
        manifest = _bound_manifest(builder, manifest_content_hash, pit_spec, observations)
    else:
        loaded = load_any_manifest(builder, manifest_content_hash, evidence_verifier)
        if isinstance(loaded, ResearchDatasetEvidenceManifest):
            return _evidence_derived_feature_request(
                adapter,
                loaded,
                evidence_verifier,
                pit_spec=pit_spec,
                minutes=minutes,
                observations=observations,
                feature=feature,
                evaluation_times=evaluation_times,
            )
        manifest = _checked_binding(loaded, pit_spec, observations)
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
    return _checked_binding(load_manifest(builder, manifest_content_hash), pit_spec, observations)


def _checked_binding[M: (ResearchDatasetManifest, ResearchDatasetEvidenceManifest)](
    manifest: M,
    pit_spec: PointInTimeSpec,
    observations: Sequence[FeatureObservation],
) -> M:
    """Step 2 of a verified manifest of either form."""
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


# =========================================================================================
# v3: evidence manifests (ADR-0077; module docs)
# =========================================================================================

_LISTINGS: Final = CANONICAL_INSTRUMENT_LISTINGS.table
_CHUNK_COLUMNS: Final = tuple(field.name for field in DATASET_SELECTION_CHUNKS.arrow_schema)
_END: Final = object()


def _verifier_parts(evidence_verifier: object) -> tuple[RevisionCatalog, DatasetEvidenceBuilder]:
    """The verifier's catalog and evidence reader (public ``adapter`` / ``builder`` properties)."""
    if not isinstance(evidence_verifier, StreamingEvidenceVerifier):
        raise DatasetBindingError(
            "a StreamingEvidenceVerifier is needed to verify a v3 evidence manifest"
        )
    catalog: RevisionCatalog | None = evidence_verifier.adapter
    reader = evidence_verifier.builder
    if catalog is None or not isinstance(reader, DatasetEvidenceBuilder):
        raise DatasetBindingError("the evidence verifier has no catalog or evidence reader")
    return catalog, reader


def evidence_catalog(evidence_verifier: StreamingEvidenceVerifier) -> RevisionCatalog:
    """The catalog whose v3 manifests ``evidence_verifier`` proves."""
    return _verifier_parts(evidence_verifier)[0]


def iter_manifest_evidence(
    evidence_verifier: StreamingEvidenceVerifier,
    manifest: ResearchDatasetEvidenceManifest,
    stream: EvidenceStream,
) -> AbstractContextManager[Iterator[Contract]]:
    """One of ``manifest``'s evidence streams, read with the verifier's own rule
    (``DatasetEvidenceBuilder.iter_evidence``: root-hash authenticated, one object at a time)."""
    return _verifier_parts(evidence_verifier)[1].iter_evidence(manifest, stream)


def evidence_lineage_tables(
    evidence_verifier: StreamingEvidenceVerifier, manifest: ResearchDatasetEvidenceManifest
) -> frozenset[str]:
    """The Canonical tables of ``manifest``'s lineage (one pass; only table names are kept)."""
    tables: set[str] = set()
    with iter_manifest_evidence(evidence_verifier, manifest, EvidenceStream.LINEAGE) as records:
        for record in records:
            if not isinstance(record, SelectedRevisionLineage):  # pragma: no cover - the model
                raise CatalogIntegrityError("a lineage record is malformed")
            tables.add(record.canonical_table)
    return frozenset(tables)


@dataclass(frozen=True, slots=True)
class DatasetRowEvidence:
    """One row of a v3 dataset chunk with its revision's evidence.

    The first row of a revision carries its ``lineage`` record and its data ``gap`` record (or
    ``None``: the revision has no evidence gap); a later row of the same revision (another member
    span of an interval spec) carries neither.
    """

    row: Mapping[str, Any]
    lineage: SelectedRevisionLineage | None
    gap: AvailabilityEvidenceGap | None


class _Peek:
    """One-record lookahead over an evidence iterator."""

    def __init__(self, records: Iterator[Contract]) -> None:
        self._records = records
        self._next: object = _END

    def peek(self) -> object:
        if self._next is _END:
            self._next = next(self._records, _END)
        return self._next

    def take(self) -> object:
        found = self.peek()
        self._next = _END
        return found


def _close(iterator: object) -> None:
    close = getattr(iterator, "close", None)
    if callable(close):
        close()


@contextmanager
def iter_dataset_chunks(
    adapter: RevisionCatalog,
    manifest: ResearchDatasetEvidenceManifest,
    evidence_verifier: StreamingEvidenceVerifier,
) -> Iterator[Iterator[tuple[DatasetRowEvidence, ...]]]:
    """The dataset's rows, one chunk at a time, with their revisions' lineage and gaps.

    ``manifest`` must already be verified (``load_any_manifest``); ``adapter`` must be the catalog
    it was proven on. Each chunk is read at the manifest's own dataset snapshot (bounded scan of
    ``selection_id`` x ``chunk_index``) and its ordinals must be contiguous; the listing prefixes of
    the ``lineage`` / ``evidence_gaps`` streams are skipped, then every revision's first row (rows
    of one key are adjacent and a revision belongs to one key, ADR-0077 §2) takes the next data
    lineage record and, when the next data gap is of that revision, that gap. Anything left over
    in either stream, a record of another revision, or a row count other than the manifest's is
    ``CatalogIntegrityError``. Held: one chunk, one lookahead record per stream, one key's
    revision ids.
    """
    if not isinstance(manifest, ResearchDatasetEvidenceManifest):
        raise DatasetBindingError("a v3 ResearchDatasetEvidenceManifest is needed")
    catalog, reader = _verifier_parts(evidence_verifier)
    if adapter is not catalog:
        raise DatasetBindingError(
            "the dataset is read through another catalog than the one its manifest was proven on"
        )
    canonical = rules.CANONICAL_TABLES.get(manifest.data_type)
    if canonical is None:
        raise CatalogIntegrityError(f"manifest data type {manifest.data_type!r} is unknown")
    with (
        reader.iter_evidence(manifest, EvidenceStream.LINEAGE) as lineage,
        reader.iter_evidence(manifest, EvidenceStream.EVIDENCE_GAPS) as gaps,
    ):
        chunks = _walk_chunks(adapter, manifest, canonical.table, _Peek(lineage), _Peek(gaps))
        try:
            yield chunks
        finally:
            chunks.close()


def _walk_chunks(
    adapter: RevisionCatalog,
    manifest: ResearchDatasetEvidenceManifest,
    canonical: str,
    lineage: _Peek,
    gaps: _Peek,
) -> Iterator[tuple[DatasetRowEvidence, ...]]:
    while isinstance(head := lineage.peek(), SelectedRevisionLineage) and (
        head.canonical_table == _LISTINGS
    ):
        lineage.take()
    while isinstance(gap_head := gaps.peek(), AvailabilityEvidenceGap) and (
        gap_head.table == _LISTINGS
    ):
        gaps.take()
    group: tuple[str, str] | None = None
    seen: set[str] = set()  # the current key's revisions (one key group, as the builder holds)
    total = 0
    for index in range(manifest.chunk_count):
        out: list[DatasetRowEvidence] = []
        for row in _chunk_rows(adapter, manifest, index):
            if (row["selection_id"], row["chunk_index"], row["canonical_table"]) != (
                manifest.selection_id,
                index,
                canonical,
            ):
                raise CatalogIntegrityError(
                    f"dataset row {row['row_ordinal']} is not a {canonical} row of chunk {index} "
                    f"of {manifest.selection_id}"
                )
            key = (row["symbol"], row["observation_key"])
            if key != group:
                group, seen = key, set()
            revision = row["revision_id"]
            if revision in seen:
                out.append(DatasetRowEvidence(row, None, None))
                continue
            seen.add(revision)
            record = lineage.take()
            if (
                not isinstance(record, SelectedRevisionLineage)
                or record.canonical_table != canonical
                or record.canonical_revision_id != revision
            ):
                raise CatalogIntegrityError(
                    f"dataset row {row['row_ordinal']}: revision {revision} is not the next data "
                    "lineage record of the manifest"
                )
            gap: AvailabilityEvidenceGap | None = None
            following = gaps.peek()
            if (
                isinstance(following, AvailabilityEvidenceGap)
                and following.table == canonical
                and following.revision_id == revision
            ):
                gaps.take()
                gap = following
            out.append(DatasetRowEvidence(row, record, gap))
        total += len(out)
        yield tuple(out)
    if total != manifest.row_count:  # pragma: no cover - every chunk's size is checked
        raise CatalogIntegrityError(f"{total} dataset rows read, the manifest commits more")
    if lineage.peek() is not _END:
        raise CatalogIntegrityError("the manifest's lineage has records of no dataset row")
    if gaps.peek() is not _END:
        raise CatalogIntegrityError("the manifest's evidence gaps have records of no dataset row")


def _chunk_rows(
    adapter: RevisionCatalog, manifest: ResearchDatasetEvidenceManifest, index: int
) -> list[dict[str, Any]]:
    """Chunk ``index`` at the dataset snapshot, by ``row_ordinal`` (never more than its size)."""
    first = index * manifest.chunk_rows
    size = min(manifest.chunk_rows, manifest.row_count - first)
    batches = adapter.scan_column_batches(
        manifest.dataset.table,
        columns=_CHUNK_COLUMNS,
        row_filter=And(
            EqualTo("selection_id", manifest.selection_id),  # type: ignore[call-arg, arg-type]
            EqualTo("chunk_index", index),  # type: ignore[call-arg, arg-type]
        ),
        snapshot_id=manifest.dataset.snapshot_id,
    )
    found: list[dict[str, Any]] = []
    try:
        for batch in batches:
            if len(found) + batch.num_rows > size:
                raise CatalogIntegrityError(
                    f"chunk {index} of {manifest.selection_id} holds more than its {size} rows"
                )
            found.extend(batch.to_pylist())
    finally:
        _close(batches)
    found.sort(key=lambda row: row["row_ordinal"])
    if [row["row_ordinal"] for row in found] != list(range(first, first + size)):
        raise CatalogIntegrityError(
            f"chunk {index} of {manifest.selection_id} is not rows {first}..{first + size - 1}"
        )
    return found


def dataset_chunk_observations(
    adapter: RevisionCatalog,
    manifest: ResearchDatasetEvidenceManifest,
    chunk: Sequence[DatasetRowEvidence],
    symbols: frozenset[str] | None = None,
) -> list[tuple[DatasetRowEvidence, FeatureObservation]]:
    """The bar observation of every revision whose first row is in ``chunk`` (of ``symbols``, or
    all), from its Canonical row at the spec's bound snapshot (module docs, v3)."""
    if manifest.data_type != _DATA_TYPE:
        raise DatasetBindingError(f"a {manifest.data_type} dataset has no {_DATA_TYPE} bars")
    firsts = [
        item
        for item in chunk
        if item.lineage is not None and (symbols is None or item.row["symbol"] in symbols)
    ]
    if not firsts:
        return []
    spec = manifest.point_in_time
    snapshot = spec.snapshot_bindings.get(_BARS_TABLE)
    if snapshot is None:
        raise CatalogIntegrityError(f"the manifest's PIT spec does not bind {_BARS_TABLE}")
    wanted = tuple(item.row["revision_id"] for item in firsts)
    found = adapter.scan_columns(
        _BARS_TABLE,
        columns=tuple(field.name for field in rules.CANONICAL_TABLES[_DATA_TYPE].arrow_schema),
        row_filter=In("revision_id", wanted),  # type: ignore[call-arg, arg-type]
        snapshot_id=snapshot,
    ).to_pylist()
    rows: dict[str, Mapping[str, Any]] = {}  # bounded by one chunk
    for row in found:
        if row["revision_id"] in rows:
            raise CatalogIntegrityError(f"Canonical revision {row['revision_id']} is read twice")
        rows[row["revision_id"]] = row
    bound = assumption_bound(spec)
    out: list[tuple[DatasetRowEvidence, FeatureObservation]] = []
    for item in firsts:
        canonical_row = rows.get(item.row["revision_id"])
        if canonical_row is None:
            raise CatalogIntegrityError(
                f"revision {item.row['revision_id']} has no row in {_BARS_TABLE} at the bound "
                "snapshot"
            )
        out.append((item, _chunk_observation(item, canonical_row, bound=bound)))
    return out


def _chunk_observation(
    item: DatasetRowEvidence, row: Mapping[str, Any], *, bound: bool
) -> FeatureObservation:
    """``bar_observations``' observation of one proven revision (same fields, same values)."""
    revision = item.row["revision_id"]
    if (row["observation_key"], row["interval_start"], row["symbol"]) != (
        item.row["observation_key"],
        item.row["event_time"],
        item.row["symbol"],
    ):
        raise CatalogIntegrityError(f"revision {revision}: key / event time differ from its row")
    lineage = SelectedRevisionLineage(
        canonical_table=_BARS_TABLE,
        canonical_revision_id=revision,
        raw_table=row["lineage_raw_table"],
        raw_revision_id=row["lineage_raw_revision_id"],
        source_table=row["lineage_source_table"],
        source_revision_id=row["lineage_source_revision_id"],
    )
    if item.lineage is None or evidence_record_bytes(lineage) != evidence_record_bytes(
        item.lineage
    ):
        raise CatalogIntegrityError(
            f"revision {revision}: its Canonical row's lineage is not the manifest's"
        )
    stored_gap = row["availability_evidence_gap"]
    if (item.gap is None) != (stored_gap is None) or (
        item.gap is not None and item.gap.gap != stored_gap
    ):
        raise CatalogIntegrityError(
            f"revision {revision}: its Canonical row's evidence gap is not the manifest's"
        )
    moved = effective_available_times([row], bound=bound)
    available = moved[revision][1] if revision in moved else row["available_time"]
    return FeatureObservation(
        observation_key=row["observation_key"],
        event_time=row["interval_start"],
        event_end_time=row["interval_end"],
        available_time=available,
        knowledge_time=row["knowledge_time"],
        values=FrozenMapping(
            {"symbol": row["symbol"], **{name: row[name] for name in BAR_VALUE_COLUMNS}}
        ),
        lineage=lineage,
    )


def _evidence_feature_request(
    adapter: RevisionCatalog,
    manifest: ResearchDatasetEvidenceManifest,
    evidence_verifier: StreamingEvidenceVerifier,
    *,
    pit_spec: PointInTimeSpec,
    observations: Sequence[FeatureObservation],
    feature: FeatureSpec,
    evaluation_times: Sequence[datetime],
) -> FeatureRequest:
    """Steps 2-6 over a verified v3 manifest (module docs)."""
    _checked_binding(manifest, pit_spec, observations)
    if manifest.data_type != _DATA_TYPE:
        raise DatasetBindingError(f"a {manifest.data_type} dataset has no {_DATA_TYPE} bars")
    given: dict[str, list[FeatureObservation]] = {}
    for item in observations:
        symbol = item.values.get("symbol")
        if not isinstance(symbol, str):
            raise DatasetBindingError(f"observation {item.observation_key} names no symbol")
        given.setdefault(symbol, []).append(item)
    wanted = frozenset(given)
    interval = manifest.point_in_time.simulation_time is None
    proven: dict[str, list[FeatureObservation]] = {}
    spans: dict[str, list[Mapping[str, Any]]] = {}  # the requested symbols' rows (step 6)
    with iter_dataset_chunks(adapter, manifest, evidence_verifier) as chunks:
        for chunk in chunks:
            if interval:
                for entry in chunk:
                    if entry.row["symbol"] in wanted:
                        spans.setdefault(entry.row["revision_id"], []).append(entry.row)
            for entry, observation in dataset_chunk_observations(adapter, manifest, chunk, wanted):
                proven.setdefault(entry.row["symbol"], []).append(observation)
    for symbol, items in sorted(given.items()):
        _require_exact(symbol, items, proven.get(symbol, ()), "bars")
    request = pit_feature_request(
        pit_spec=manifest.point_in_time,
        observations=observations,
        feature=feature,
        evaluation_times=evaluation_times,
        manifest_content_hash=manifest.content_hash(),
    )
    if interval:
        _check_membership(request, feature, spans)
    return request


def _evidence_derived_feature_request(
    adapter: RevisionCatalog,
    manifest: ResearchDatasetEvidenceManifest,
    evidence_verifier: StreamingEvidenceVerifier,
    *,
    pit_spec: PointInTimeSpec,
    minutes: int,
    observations: Sequence[FeatureObservation],
    feature: FeatureSpec,
    evaluation_times: Sequence[datetime],
) -> FeatureRequest:
    """Re-derive v3 observations in one verified UTC-day slice at a time (ADR-0077)."""
    _checked_binding(manifest, pit_spec, observations)
    if manifest.data_type != _DATA_TYPE:
        raise DatasetBindingError(f"a {manifest.data_type} dataset has no {_DATA_TYPE} bars")
    if manifest.point_in_time.simulation_time is None:
        raise DatasetBindingError("derived bars come from a point-simulation dataset (E4)")
    given: dict[str, list[FeatureObservation]] = {}
    for item in observations:
        symbol = item.values.get("symbol")
        if not isinstance(symbol, str):
            raise DatasetBindingError(f"observation {item.observation_key} names no symbol")
        given.setdefault(symbol, []).append(item)

    proven: dict[str, list[FeatureObservation]] = {}
    wanted = frozenset(given)
    slice_key: tuple[str, datetime] | None = None
    slice_observations: list[FeatureObservation] = []

    def finish_slice() -> None:
        if slice_key is None or not slice_observations:
            return
        symbol, day = slice_key
        start = max(manifest.dataset.time_range_start, day)
        end = min(manifest.dataset.time_range_end, day + timedelta(days=1))
        if start >= end:
            raise CatalogIntegrityError("a v3 dataset observation is outside its declared window")
        selection = _derived_slice_selection(manifest.point_in_time, slice_observations)
        try:
            bars = resample_bars(selection, minutes, start, end)
        except ResampleError as exc:
            raise DatasetBindingError(
                f"the dataset's {symbol} bars cannot be resampled: {exc}"
            ) from exc
        proven.setdefault(symbol, []).extend(
            derived_bar_observations(bars, selection, manifest.point_in_time)
        )

    with iter_dataset_chunks(adapter, manifest, evidence_verifier) as chunks:
        for chunk in chunks:
            for entry, observation in dataset_chunk_observations(
                adapter, manifest, chunk, wanted
            ):
                event_day = observation.event_time.replace(
                    hour=0, minute=0, second=0, microsecond=0
                )
                current = (entry.row["symbol"], event_day)
                if slice_key != current:
                    if slice_key is not None and current < slice_key:
                        raise CatalogIntegrityError(
                            "v3 dataset rows are not ordered by symbol and UTC-day slice"
                        )
                    finish_slice()
                    slice_key = current
                    slice_observations = []
                slice_observations.append(observation)
    finish_slice()

    for symbol, items in sorted(given.items()):
        _require_exact(symbol, items, proven.get(symbol, ()), f"{minutes}-minute derived bars")
    return pit_feature_request(
        pit_spec=manifest.point_in_time,
        observations=observations,
        feature=feature,
        evaluation_times=evaluation_times,
        manifest_content_hash=manifest.content_hash(),
    )


def _derived_slice_selection(
    spec: PointInTimeSpec, observations: Sequence[FeatureObservation]
) -> PitSelection:
    """A bounded point selection synthesized only from a verified v3 UTC-day slice."""
    simulation_time = spec.simulation_time
    knowledge_cutoff = spec.knowledge_cutoff
    if simulation_time is None or knowledge_cutoff is None:
        raise DatasetBindingError("derived bars require a point-simulation dataset (E4)")
    selections: list[PointInTimeSelection] = []
    selected_rows: dict[str, Mapping[str, Any]] = {}
    lineage: list[SelectedRevisionLineage] = []
    for observation in observations:
        revision = observation.lineage.canonical_revision_id
        if revision in selected_rows:
            raise CatalogIntegrityError(f"Canonical revision {revision} occurs twice in a slice")
        selections.append(
            PointInTimeSelection(
                observation_key=observation.observation_key,
                simulation_time=simulation_time,
                knowledge_cutoff=knowledge_cutoff,
                status=PointInTimeStatus.SELECTED,
                selected_revision_id=revision,
                maximal_heads=(revision,),
            )
        )
        selected_rows[revision] = {
            "revision_id": revision,
            "observation_key": observation.observation_key,
            "interval_start": observation.event_time,
            "interval_end": observation.event_end_time,
            "symbol": observation.values["symbol"],
            "available_time": observation.available_time,
            "knowledge_time": observation.knowledge_time,
            **{name: observation.values[name] for name in BAR_VALUE_COLUMNS},
        }
        lineage.append(observation.lineage)
    return PitSelection(
        canonical_table=rules.CANONICAL_TABLES[_DATA_TYPE].table,
        selections=tuple(selections),
        lineage=tuple(lineage),
        evidence_gaps=(),
        conflicts=(),
        records={},
        edges={},
        evidence_bound=True,
        selected_rows=selected_rows,
    )

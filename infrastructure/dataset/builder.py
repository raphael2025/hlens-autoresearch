"""Research Dataset + ``ResearchDatasetManifest`` (Phase 1 F3; ADR-0023 §5 / §6, ADR-0024 §6).

``DatasetBuilder.select(universe, pit, data_type, start, end)`` is the read-only, deterministic
part; ``build`` materializes it and persists the manifest. Under ``hlens.dataset.pit-selection
@1.0.0``:

1. **bindings** (fail closed) — every availability / precedence / parser binding of the PIT spec
   must be a registered one with its exact hash (``KNOWN_BINDINGS``); the Canonical table of the
   data type, the listing tables and ``quality.data_quality_reports`` must be bound; the Raw
   evidence table (ADR-0027 §13 as D-F1n ⑤ assigns it to F3) and the evidence-gap table
   (ADR-0031) must be bound **whenever they had a snapshot when the build ran** (see
   ``_check_unbound``: a replay of a materialized selection is judged at its own build, G2 RT-5);
   the dataset's own table must not be an upstream binding;
2. **universe** — F2 ``UniverseBuilder`` at the bound snapshots; unconstructible = no dataset;
3. **selection** — per member symbol, ``PitSelector.select`` slice by slice (``agg_trades``: UTC
   hours, ``klines_1m``: UTC days, clipped to the window), each key selected only by the slice
   owning it (a key seen twice is an integrity error); ``require_no_conflict`` on every slice;
4. **membership gating** — a selected revision enters for the simulation span where it is the
   selection **and** its symbol is a universe member (point simulation: the symbol is a member);
5. **quality** — every covered partition (member symbol x UTC day of the window, plus the day of
   any retained revision's event) and the listing history must already have a committed report
   **at exactly the bound snapshots**: each is re-derived by its reporter on a
   ``PinnedCatalogView`` with ``existing_only`` (no clock, no write), and every evidence gap the
   manifest binds must be recorded — read with ``evidence_gaps_of`` on that view (ADR-0031), never
   from the report row — by the report it cites. Reports are *required*, not produced
   here: a report written now would describe the current heads and land after the spec already
   pinned ``quality.data_quality_reports``, so it could never be bound by this spec;
6. **materialization** — the rows (``selection.SELECTION_SCHEMA``) are appended to the caller's
   research table as one batch whose id is the ``selection_id`` (derived from the rule, the PIT
   spec hash, the universe binding, the data type and the window), so a rebuild replays the same
   snapshot; an empty selection is refused (a batch needs rows, and a dataset without its own
   snapshot cannot be bound). The rows are read back at that snapshot;
7. **manifest** — ``ResearchDatasetManifest`` (own ``DatasetRef``, the PIT spec, universe binding,
   members / exclusions, listing + data lineage, report ids, evidence gaps) persisted through
   ``ManifestStore`` (content-hash idempotent, verified on replay). Same inputs → bit-identical
   manifest. The manifest binds the dataset's own snapshot, so it can only follow the batch: a
   failure in between leaves a batch without a manifest, which no reader may use (ADR-0023 §6);
   a rerun replays the same batch and completes the manifest (F3-R1, cursor review 1).

``verify_manifest`` (the ``ManifestVerifier`` of ``ManifestStore``, G2 RT-4) proves a manifest is
exactly what a build of its own inputs produced: its dataset snapshot commits the batch
``selection_id_for`` those inputs, and ``select`` at the bound snapshots re-derives every member,
exclusion, lineage entry, report id, evidence gap and the rows the snapshot committed.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from typing import Any, Final

import pyarrow as pa  # type: ignore[import-untyped]
from pyiceberg.expressions import And, EqualTo
from pyiceberg.schema import assign_fresh_schema_ids

from core.contracts.catalog import (
    BatchConflict,
    CommitConflict,
    CommitRequest,
    SnapshotInfo,
    SnapshotNotFound,
    TableNotFound,
)
from core.contracts.revision import PointInTimeSelection, PointInTimeSpec, PointInTimeStatus
from core.contracts.storage import StorageAdapter
from core.contracts.universe import (
    AvailabilityEvidenceGap,
    ResearchDatasetManifest,
    SelectedRevisionLineage,
    UniverseSelectionSpec,
    UniverseSpecBinding,
)
from core.domain.base import (
    CONTRACT_SCHEMA_VERSION,
    canonical_json,
    contract_schema_version_scope,
)
from core.domain.specs import DatasetRef, Zone
from infrastructure import contract_version
from infrastructure.canonical import listing_rules as lr
from infrastructure.canonical import rules
from infrastructure.catalog.definitions import RegisteredTableDefinition
from infrastructure.catalog.fingerprint import PYARROW_BATCH_FINGERPRINT
from infrastructure.catalog.iceberg_adapter import CatalogIntegrityError
from infrastructure.catalog.phase1_tables import (
    BINANCE_SPOT_PRECEDENCE_EVIDENCE,
    CANONICAL_INSTRUMENT_LISTINGS,
    DATA_QUALITY_REPORTS,
    DATASET_MANIFESTS,
    QUALITY_EVIDENCE_GAPS,
)
from infrastructure.dataset.manifests import ManifestPersisted, ManifestStore
from infrastructure.dataset.selection import SELECTION_NAMESPACE, SELECTION_SCHEMA
from infrastructure.parser.binance_archive import PARSER_BINDING as ARCHIVE_PARSER_BINDING
from infrastructure.parser.binance_exchange_info import EXCHANGE_INFO_DECODER_BINDING
from infrastructure.parser.binance_rest import DECODER_BINDING as REST_DECODER_BINDING
from infrastructure.pit.assumption import ASSUMPTION_BINDING
from infrastructure.pit.selector import PIT_BINDING, PitSelection, PitSelector
from infrastructure.pit.view import PinnedCatalogView
from infrastructure.quality.listing_report import ListingQualityReporter
from infrastructure.quality.reporter import QualityReporter, evidence_gaps_of
from infrastructure.revision.availability import AVAILABILITY_BINDING as ARCHIVE_AVAILABILITY
from infrastructure.revision.channel_precedence import DELIVERY_CHANNEL_BINDING
from infrastructure.revision.exchange_info_availability import EXCHANGE_INFO_AVAILABILITY_BINDING
from infrastructure.revision.precedence import PRECEDENCE_BINDING as ARCHIVE_PRECEDENCE
from infrastructure.revision.rest_availability import REST_AVAILABILITY_BINDING
from infrastructure.revision.rest_precedence import REST_PRECEDENCE_BINDING
from infrastructure.revision.row_integrity import snapshots_of_batches
from infrastructure.revision.store import BatchCommit, RevisionCatalog
from infrastructure.universe.builder import REGISTERED_UNIVERSES, UniverseBuilder, UniverseBuilt

__all__ = [
    "DATASET_RULE_HASH",
    "DATASET_RULE_ID",
    "DATASET_RULE_SPEC",
    "DATASET_RULE_VERSION",
    "KNOWN_BINDINGS",
    "DatasetBuildError",
    "DatasetBuilder",
    "DatasetBuilt",
    "DatasetEmpty",
    "DatasetQualityError",
    "DatasetSelection",
    "DatasetSpecError",
    "selection_id_for",
    "selection_id_of",
]

DATASET_RULE_ID: Final = "hlens.dataset.pit-selection"
DATASET_RULE_VERSION: Final = "1.0.0"
DATASET_RULE_SPEC: Final[dict[str, Any]] = {
    "rule": DATASET_RULE_ID,
    "version": DATASET_RULE_VERSION,
    "adr": ["ADR-0023 §5 / §6", "ADR-0024 §4 / §6", "ADR-0027 §13", "ADR-0028 §7"],
    "inputs": "UniverseSelectionSpec, PointInTimeSpec, data_type, UTC event window [start, end)",
    "bindings": "every policy / parser binding registered with its exact hash; Canonical, "
    "listing and quality tables bound; the Raw evidence table bound whenever it has a snapshot",
    "universe": "F2 UniverseBuilder at the bound snapshots; unconstructible = no dataset",
    "slices": {"agg_trades": "UTC hour", "klines_1m": "UTC day"},
    "selection": "hlens.pit.maximal-head@1.0.0 per member symbol and slice, keys owned by one "
    "slice; any conflict fails the dataset closed",
    "membership": "a selected revision enters for the simulation span where it is selected and "
    "its symbol is a member",
    "quality": "each covered partition (member symbol x UTC day of the window or of a retained "
    "revision's event) and the listing history have a committed report at exactly the bound "
    "snapshots (re-derived); every bound evidence gap is recorded in the report it cites",
    "rows": "one per (key, selected revision, span): selection_id, canonical_table, symbol, "
    "observation_key, revision_id, event_time, effective_from, effective_until",
    "empty": "refused",
    "selection_id": "<rule>@<version>.<sha256 of rule hash, PIT spec hash, universe binding, "
    "data_type, window>",
}
DATASET_RULE_HASH: Final = hashlib.sha256(
    canonical_json(DATASET_RULE_SPEC).encode("utf-8")
).hexdigest()

#: Every binding a first-slice PIT spec may carry (id + version + hash); anything else is an
#: unregistered policy / parser version and the build fails closed (roadmap #18).
KNOWN_BINDINGS: Final = frozenset(
    {
        ARCHIVE_AVAILABILITY,
        REST_AVAILABILITY_BINDING,
        EXCHANGE_INFO_AVAILABILITY_BINDING,
        rules.AVAILABILITY_BINDING,
        ASSUMPTION_BINDING,
        ARCHIVE_PRECEDENCE,
        REST_PRECEDENCE_BINDING,
        DELIVERY_CHANNEL_BINDING,
        rules.PRECEDENCE_MAP_BINDING,
        lr.LISTING_OBSERVATION_BINDING,
        ARCHIVE_PARSER_BINDING,
        REST_DECODER_BINDING,
        EXCHANGE_INFO_DECODER_BINDING,
        rules.NORMALIZER_BINDING,
        lr.LISTING_STATUS_BINDING,
    }
)

_SLICES: Final[Mapping[str, timedelta]] = {
    "agg_trades": timedelta(hours=1),
    "klines_1m": timedelta(days=1),
}
_DAY: Final = timedelta(days=1)
_ZERO: Final = timedelta(0)
_NO_SPAN: Final = datetime.min.replace(tzinfo=UTC)
_ATTEMPTS: Final = 8
_EVIDENCE: Final = BINANCE_SPOT_PRECEDENCE_EVIDENCE.table
_LISTINGS: Final = CANONICAL_INSTRUMENT_LISTINGS.table
_QUALITY: Final = DATA_QUALITY_REPORTS.table
_GAPS: Final = QUALITY_EVIDENCE_GAPS.table
#: Tables bound whenever they had a snapshot when the build ran (not the listing tables: a
#: missing listing history is the universe's refusal), with the decision that requires it.
_BOUND_IF_PRESENT: Final = ((_EVIDENCE, "ADR-0027 §13"), (_GAPS, "ADR-0031"))


class DatasetBuildError(Exception):
    """Base class: no Research Dataset can be built from these inputs (fail closed)."""


class DatasetSpecError(DatasetBuildError, ValueError):
    """The request cannot drive a build (bindings, scope, window, table)."""


class DatasetQualityError(DatasetBuildError):
    """A covered partition's report does not record what the manifest must bind."""


class DatasetEmpty(DatasetBuildError):
    """Nothing was selected: an empty selection has no snapshot of its own to bind."""


@dataclass(frozen=True, slots=True)
class DatasetSelection:
    """The deterministic, read-only result of ``select`` (everything but the own snapshot)."""

    selection_id: str
    universe_spec: UniverseSelectionSpec
    point_in_time: PointInTimeSpec
    data_type: str
    start: datetime
    end: datetime
    universe: UniverseBuilt
    rows: tuple[Mapping[str, Any], ...]
    lineage: tuple[SelectedRevisionLineage, ...]
    evidence_gaps: tuple[AvailabilityEvidenceGap, ...]
    quality_report_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class DatasetBuilt:
    selection: DatasetSelection
    manifest: ResearchDatasetManifest
    dataset_commit: BatchCommit
    manifest_persisted: ManifestPersisted

    @property
    def replayed(self) -> bool:
        return self.dataset_commit.replayed and self.manifest_persisted.replayed


def selection_id_for(
    universe: UniverseSelectionSpec,
    pit: PointInTimeSpec,
    data_type: str,
    start: datetime,
    end: datetime,
) -> str:
    return selection_id_of(universe.binding(), pit, data_type, start, end)


def selection_id_of(
    universe: UniverseSpecBinding,
    pit: PointInTimeSpec,
    data_type: str,
    start: datetime,
    end: datetime,
) -> str:
    """``selection_id_for`` from the universe's binding: what a manifest alone can recompute."""
    document = {
        "rule": DATASET_RULE_HASH,
        "point_in_time": pit.content_hash(),
        "universe": universe.model_dump(mode="json"),
        "data_type": data_type,
        "start": start.isoformat(),
        "end": end.isoformat(),
    }
    digest = hashlib.sha256(canonical_json(document).encode("utf-8")).hexdigest()
    return f"{DATASET_RULE_ID}@{DATASET_RULE_VERSION}.{digest}"


class DatasetBuilder:
    """Builds, materializes and records Research Datasets; deterministic for fixed inputs."""

    def __init__(
        self,
        adapter: RevisionCatalog,
        storage: StorageAdapter,
        *,
        market_data_base_url: str,
        dataset_table: RegisteredTableDefinition,
    ) -> None:
        _check_dataset_table(dataset_table)
        self._adapter = adapter
        self._storage = storage
        self._origin = market_data_base_url
        self._table = dataset_table

    # ------------------------------------------------------------------ entry points

    def build(
        self,
        universe: UniverseSelectionSpec,
        pit: PointInTimeSpec,
        data_type: str,
        start: datetime,
        end: datetime,
    ) -> DatasetBuilt:
        selection = self.select(universe, pit, data_type, start, end)
        if not selection.rows:
            raise DatasetEmpty(
                f"{data_type} [{start.isoformat()}, {end.isoformat()}) selects nothing for the "
                "universe's members: an empty Research Dataset has no snapshot of its own"
            )
        commit = self._materialize(selection)
        recorded = self._recorded_manifest_version(commit)
        if recorded is None:
            # A new manifest: the current version, never inside a leaked replay scope.
            contract_version.new_group_version()
        elif recorded != CONTRACT_SCHEMA_VERSION:
            # The dataset batch replayed and its manifest is persisted at an earlier version:
            # this build is that manifest's replay, re-derived at its recorded version
            # (ADR-0052 versioned replay, V7), never a second manifest differing in envelope.
            with contract_schema_version_scope(recorded):
                again = self.select(universe, pit, data_type, start, end)
            if again.selection_id != selection.selection_id or again.rows != selection.rows:
                raise CatalogIntegrityError(  # pragma: no cover - select is deterministic
                    f"selection {selection.selection_id} re-derives differently"
                )
            selection = again

        def dataset() -> DatasetRef:
            return DatasetRef(
                zone=Zone.RESEARCH_DATASET,
                table=self._table.table,
                snapshot_id=commit.snapshot_id,
                time_range_start=start,
                time_range_end=end,
            )

        if recorded is None or recorded == CONTRACT_SCHEMA_VERSION:
            manifest = _manifest_of(selection, dataset())
        else:
            with contract_schema_version_scope(recorded):
                manifest = _manifest_of(selection, dataset())
        # ``select`` derived the selection in this very call and ``_materialize`` read its rows
        # back at the snapshot: the manifest is checked against it rather than re-selected.
        verifier = _JustSelected(self._adapter, self._table, selection)
        persisted = ManifestStore(self._adapter, verifier).persist(manifest)
        return DatasetBuilt(selection, manifest, commit, persisted)

    def manifests(self) -> ManifestStore:
        """The ``ManifestStore`` whose manifests this builder verifies (persist and load)."""
        return ManifestStore(self._adapter, self)

    def verify_manifest(self, manifest: ResearchDatasetManifest) -> None:
        """``manifest`` is exactly what a build of its own inputs produced, or raise (G2 RT-4).

        A hash-consistent, contract-valid manifest can still be false (an exclusion dropped over
        the genuine dataset snapshot). So its dataset must be a snapshot of this builder's table
        committing the batch ``selection_id_for`` the manifest's own universe spec (a registered
        one), PIT spec, window and one data type; ``select`` at the bound snapshots (read-only,
        deterministic) must then re-derive the manifest field for field, and the snapshot's rows
        (batch fingerprint and count). Memory: one selection, the dataset's rows — as a build.
        """
        if not isinstance(manifest, ResearchDatasetManifest):
            raise DatasetSpecError("manifest must be a ResearchDatasetManifest")
        snapshot = _dataset_snapshot(self._adapter, self._table, manifest)
        binding = manifest.universe_spec
        universe = REGISTERED_UNIVERSES.get((binding.name, binding.version))
        if universe is None or universe.binding() != binding:
            raise CatalogIntegrityError(
                f"manifest universe {binding.name}@{binding.version} is not a registered spec"
            )
        pit, dataset = manifest.point_in_time, manifest.dataset
        start, end = dataset.time_range_start, dataset.time_range_end
        data_types = [
            data_type
            for data_type in sorted(rules.CANONICAL_TABLES)
            if selection_id_for(universe, pit, data_type, start, end) == snapshot.batch_id
        ]
        if len(data_types) != 1:
            raise CatalogIntegrityError(
                f"dataset snapshot {dataset.snapshot_id} of {dataset.table} does not commit the "
                "selection of the manifest's own inputs"
            )
        # Re-derived at the version the manifest was persisted with (ADR-0052 versioned replay,
        # V7): every object the derivation builds carries that envelope.
        version = contract_version.replay_version(
            manifest.schema_version, what=f"manifest {manifest.content_hash()}"
        )
        with contract_schema_version_scope(version):
            selection = self.select(universe, pit, data_types[0], start, end)
            _check_manifest(self._adapter, self._table, manifest, selection)
        batch = pa.Table.from_pylist(
            [dict(row) for row in selection.rows], schema=self._table.arrow_schema
        )
        if (
            snapshot.added_rows != batch.num_rows
            or snapshot.batch_fingerprint != self._table.fingerprint_rule.fingerprint(batch)
        ):
            raise CatalogIntegrityError(
                f"dataset snapshot {dataset.snapshot_id} of {dataset.table} committed other rows "
                "than its manifest's inputs select"
            )

    def select(
        self,
        universe: UniverseSelectionSpec,
        pit: PointInTimeSpec,
        data_type: str,
        start: datetime,
        end: datetime,
    ) -> DatasetSelection:
        """Everything a build decides, at the bound snapshots; nothing is written."""
        canonical = self._check_request(pit, data_type, start, end)
        selection_id = selection_id_for(universe, pit, data_type, start, end)
        self._check_unbound(pit, selection_id)
        built = UniverseBuilder(
            self._adapter, self._storage, market_data_base_url=self._origin
        ).build(universe, pit)
        column = _time_column(data_type)
        selector = PitSelector(self._adapter, self._storage)
        rows: list[dict[str, Any]] = []
        lineage: dict[str, SelectedRevisionLineage] = {}
        gaps: dict[str, str] = {}
        #: venue symbol → covered UTC days; revision → (venue symbol, its event day).
        partitions: dict[str, set[date]] = {}
        gap_partition: dict[str, tuple[str, date]] = {}
        seen_keys: set[str] = set()
        for venue_symbol in sorted(universe.symbols):
            member_spans = built.member_spans.get(venue_symbol, ())
            if not member_spans:
                continue
            days = partitions.setdefault(venue_symbol, set(_days(start, end)))
            for low, high in _slices(data_type, start, end):
                result = selector.select(pit, data_type, venue_symbol, low, high)
                result.require_no_conflict()
                kept = self._gate(
                    result, pit, member_spans, seen_keys, selection_id, venue_symbol, column
                )
                slice_lineage = {item.canonical_revision_id: item for item in result.lineage}
                slice_gaps = {item.revision_id: item.gap for item in result.evidence_gaps}
                for row in kept:
                    revision = row["revision_id"]
                    rows.append(row)
                    found = slice_lineage.get(revision)
                    if found is None:  # pragma: no cover - the selector records every one
                        raise CatalogIntegrityError(f"selected revision {revision} has no lineage")
                    lineage[revision] = found
                    day = row["event_time"].astimezone(UTC).date()
                    days.add(day)
                    gap = slice_gaps.get(revision)
                    if gap is not None:
                        gaps[revision] = gap
                        gap_partition[revision] = (venue_symbol, day)
        report_ids, evidence_gaps = self._verify_reports(
            pit, data_type, canonical, partitions, gaps, gap_partition, built
        )
        rows.sort(key=_row_order)
        return DatasetSelection(
            selection_id=selection_id,
            universe_spec=universe,
            point_in_time=pit,
            data_type=data_type,
            start=start,
            end=end,
            universe=built,
            rows=tuple(rows),
            lineage=(*built.lineage, *(lineage[revision] for revision in sorted(lineage))),
            evidence_gaps=evidence_gaps,
            quality_report_ids=tuple(sorted(report_ids)),
        )

    # ------------------------------------------------------------------ checks

    def _check_request(
        self, pit: PointInTimeSpec, data_type: str, start: datetime, end: datetime
    ) -> str:
        if not isinstance(pit, PointInTimeSpec):
            raise DatasetSpecError("pit must be a PointInTimeSpec")
        canonical = rules.CANONICAL_TABLES.get(data_type)
        if canonical is None:
            raise DatasetSpecError(f"unsupported data_type {data_type!r}")
        for label, value in (("start", start), ("end", end)):
            if (
                not isinstance(value, datetime)
                or value.tzinfo is None
                or value.utcoffset() != _ZERO
            ):
                raise DatasetSpecError(f"{label} must be a timezone-aware UTC datetime")
        if not start < end:
            raise DatasetSpecError("the window must not be empty")
        if pit.point_in_time_binding != PIT_BINDING:
            raise DatasetSpecError("the PIT rule must be hlens.pit.maximal-head@1.0.0")
        for field in ("availability_bindings", "precedence_bindings", "parser_bindings"):
            for binding in getattr(pit, field):
                if binding not in KNOWN_BINDINGS:
                    raise DatasetSpecError(
                        f"{field}: {binding.policy_id}@{binding.version} with this hash is not "
                        "registered"
                    )
        bound = pit.snapshot_bindings
        # The listing tables are the universe's to require (a missing history is its refusal).
        for table in (canonical.table, _QUALITY):
            if table not in bound:
                raise DatasetSpecError(f"the PIT spec does not bind {table}")
        if self._table.table in bound:
            raise DatasetSpecError("the dataset's own table must not be an upstream binding")
        return canonical.table

    def _check_unbound(self, pit: PointInTimeSpec, selection_id: str) -> None:
        """The evidence and gap tables are bound if they had a snapshot when the build ran.

        The D-33 edges in ``_EVIDENCE`` resolve archive / REST heads (ADR-0027 §13); quality rule
        2.0.0 keeps its evidence gaps in ``_GAPS`` (ADR-0031): read unbound, every report's gaps
        would look missing. The requirement is judged at the build the spec describes, not at
        today's heads (G2 RT-5); heads only move forward, so:

        - a **new** build (its ``selection_id`` never materialized) runs now: refused while
          either table has a snapshot now;
        - a **replay** of a materialized selection is judged at its first build: the one writer
          of the dataset table commits batch ``selection_id`` only after this very check passed
          for the same spec (the id hashes the PIT spec, bindings included), so the table had no
          snapshot then and its first snapshot came later. A batch forged under that id widens
          nothing: ``_materialize`` / ``verify_manifest`` accept it only with exactly the rows
          ``select`` derives, and an unbound table reads as empty on the pinned view, which can
          only remove information, never change a selection (``PinnedCatalogView``).

        Commit timestamps (``SnapshotInfo.committed_at``) are audit-only by contract and Iceberg
        sequence numbers are per table: neither orders two tables' commits, so neither is used.
        """
        bound = pit.snapshot_bindings
        missing = [
            (table, adr)
            for table, adr in _BOUND_IF_PRESENT
            if table not in bound and self._head(table) is not None
        ]
        if not missing:
            return
        snapshots = snapshots_of_batches(self._adapter, self._table.table, (selection_id,))
        materialized = {item.snapshot_id for item in snapshots[selection_id]}
        if materialized and self._manifested(materialized):
            # A replay of a completed build: a persisted manifest already binds this batch's
            # snapshot, so the requirement held when it was first built (G2 RT-5). A batch alone
            # (planted, or a build that died before its manifest) proves no such build: it is
            # judged as a new build (G2-R2, cursor review).
            return
        table, adr = missing[0]
        raise DatasetSpecError(f"{table} has a snapshot but the PIT spec does not bind it ({adr})")

    def _manifested(self, snapshot_ids: set[str]) -> bool:
        """Whether a persisted manifest names one of ``snapshot_ids`` of this builder's table."""
        rows = self._adapter.scan_columns(
            DATASET_MANIFESTS.table,
            columns=("dataset_table", "dataset_snapshot_id"),
            row_filter=EqualTo("dataset_table", self._table.table),  # type: ignore[call-arg, arg-type]
        ).to_pylist()
        return any(row["dataset_snapshot_id"] in snapshot_ids for row in rows)

    # ------------------------------------------------------------------ selection

    def _gate(
        self,
        result: PitSelection,
        pit: PointInTimeSpec,
        member_spans: Sequence[tuple[datetime | None, datetime | None]],
        seen_keys: set[str],
        selection_id: str,
        venue_symbol: str,
        column: str,
    ) -> list[dict[str, Any]]:
        by_key: dict[str, list[PointInTimeSelection]] = {}
        for item in result.selections:
            by_key.setdefault(item.observation_key, []).append(item)
        kept: list[dict[str, Any]] = []
        for key, items in sorted(by_key.items()):
            if key in seen_keys:
                raise CatalogIntegrityError(f"observation key {key} is owned by two slices")
            seen_keys.add(key)
            for span_from, span_until, revision in _selected_spans(items, pit):
                row = result.selected_rows[revision]
                for member_from, member_until in member_spans:
                    effective = _intersect(span_from, span_until, member_from, member_until)
                    if effective is None:
                        continue
                    kept.append(
                        {
                            "selection_id": selection_id,
                            "canonical_table": result.canonical_table,
                            "symbol": rules.SYMBOLS[venue_symbol].symbol,
                            "observation_key": key,
                            "revision_id": revision,
                            "event_time": row[column],
                            "effective_from": effective[0],
                            "effective_until": effective[1],
                        }
                    )
        return kept

    # ------------------------------------------------------------------ quality

    def _verify_reports(
        self,
        pit: PointInTimeSpec,
        data_type: str,
        canonical: str,
        partitions: Mapping[str, set[date]],
        gaps: Mapping[str, str],
        gap_partition: Mapping[str, tuple[str, date]],
        built: UniverseBuilt,
    ) -> tuple[set[str], tuple[AvailabilityEvidenceGap, ...]]:
        """Every covered report re-derived at the bound snapshots; the gaps it records bound.

        Gaps are read with ``evidence_gaps_of`` on the pinned view (ADR-0031), never from the
        report row; each bound gap must be recorded, with the same text, by the report cited.
        """
        view = PinnedCatalogView(self._adapter, pit.snapshot_bindings)
        reporter = QualityReporter(view, self._storage)
        report_ids: set[str] = set()
        bound: list[AvailabilityEvidenceGap] = []
        by_partition: dict[tuple[str, date], list[str]] = {}
        for revision, partition in gap_partition.items():
            by_partition.setdefault(partition, []).append(revision)
        for venue_symbol, days in sorted(partitions.items()):
            for day in sorted(days):
                out = reporter.report(data_type, venue_symbol, day, existing_only=True)
                report_ids.add(out.report_id)
                wanted = {
                    revision: gaps[revision]
                    for revision in by_partition.get((venue_symbol, day), ())
                }
                bound.extend(_bind_gaps(view, out.report_id, canonical, wanted))
        listing = ListingQualityReporter(
            view, self._storage, market_data_base_url=self._origin
        ).report(existing_only=True)
        report_ids.add(listing.report_id)
        bound.extend(_bind_gaps(view, listing.report_id, _LISTINGS, dict(built.evidence_gaps)))
        return report_ids, tuple(sorted(bound, key=lambda item: (item.table, item.revision_id)))

    # ------------------------------------------------------------------ materialization

    def _materialize(self, selection: DatasetSelection) -> BatchCommit:
        definition = self._table
        batch = pa.Table.from_pylist(
            [dict(row) for row in selection.rows], schema=definition.arrow_schema
        )
        expected = sorted(batch.to_pylist(), key=_row_order)
        last: Exception | None = None
        for _ in range(_ATTEMPTS):
            request = CommitRequest(
                table=definition.table,
                batch_id=selection.selection_id,
                batch_fingerprint=definition.fingerprint_rule.fingerprint(batch),
                row_count=len(expected),
                expected_parent_snapshot_id=self._head(definition.table),
            )
            try:
                result = self._adapter.commit_batch(request, batch)
            except CommitConflict as exc:
                last = exc
                continue
            except BatchConflict as exc:
                raise CatalogIntegrityError(
                    f"selection {selection.selection_id} is materialized with other rows"
                ) from exc
            snapshot = result.snapshot.snapshot_id
            found = self._adapter.scan_columns(
                definition.table,
                columns=tuple(field.name for field in definition.arrow_schema),
                row_filter=EqualTo("selection_id", selection.selection_id),  # type: ignore[call-arg, arg-type]
                snapshot_id=snapshot,
            ).to_pylist()
            if sorted(found, key=_row_order) != expected:
                raise CatalogIntegrityError(
                    f"selection {selection.selection_id} reads back differently at {snapshot}"
                )
            return BatchCommit(
                table=definition.table,
                batch_id=selection.selection_id,
                snapshot_id=snapshot,
                outcome=result.outcome,
                row_count=len(expected),
            )
        raise CatalogIntegrityError(
            f"selection {selection.selection_id} lost {_ATTEMPTS} races"
        ) from last

    def _recorded_manifest_version(self, commit: BatchCommit) -> str | None:
        """The version of the manifest(s) persisted for a replayed dataset batch, else ``None``.

        A dataset batch committed by this call has no manifest yet; a replayed one may have one
        (a build that stopped before persisting it has none: its manifest is new).
        """
        if not commit.replayed:
            return None
        found = self._adapter.scan_columns(
            DATASET_MANIFESTS.table,
            columns=("contract_schema_version",),
            row_filter=And(
                EqualTo("dataset_table", commit.table),  # type: ignore[call-arg, arg-type]
                EqualTo("dataset_snapshot_id", commit.snapshot_id),  # type: ignore[call-arg, arg-type]
            ),
        ).column("contract_schema_version")
        versions = found.to_pylist()
        if not versions:
            return None
        return contract_version.recorded_version(
            versions, what=f"the manifests of dataset snapshot {commit.snapshot_id}"
        )

    def _head(self, table: str) -> str | None:
        info = self._adapter.load_table(table)
        if info is None:
            raise TableNotFound(f"table {table} does not exist")
        return None if info.current_snapshot is None else info.current_snapshot.snapshot_id


# =========================================================================================
# manifest verification (G2 RT-4)
# =========================================================================================


@dataclass(frozen=True, slots=True)
class _JustSelected:
    """``build``'s verifier: the manifest against the selection ``select`` just derived."""

    adapter: RevisionCatalog
    table: RegisteredTableDefinition
    selection: DatasetSelection

    def verify_manifest(self, manifest: ResearchDatasetManifest) -> None:
        _check_manifest(self.adapter, self.table, manifest, self.selection)


def _manifest_of(selection: DatasetSelection, dataset: DatasetRef) -> ResearchDatasetManifest:
    return ResearchDatasetManifest(
        dataset=dataset,
        point_in_time=selection.point_in_time,
        universe_spec=selection.universe_spec.binding(),
        members=selection.universe.members,
        exclusions=selection.universe.exclusions,
        lineage=selection.lineage,
        quality_report_ids=selection.quality_report_ids,
        evidence_gaps=selection.evidence_gaps,
    )


def _dataset_snapshot(
    adapter: RevisionCatalog,
    definition: RegisteredTableDefinition,
    manifest: ResearchDatasetManifest,
) -> SnapshotInfo:
    """The manifest's own dataset snapshot, which must be one of ``definition``'s table."""
    dataset = manifest.dataset
    if dataset.zone is not Zone.RESEARCH_DATASET or dataset.table != definition.table:
        raise CatalogIntegrityError(
            f"manifest dataset {dataset.zone.value}:{dataset.table} is not {definition.table}"
        )
    try:
        return adapter.get_snapshot(dataset.table, dataset.snapshot_id)
    except SnapshotNotFound as exc:
        raise CatalogIntegrityError(
            f"manifest dataset snapshot {dataset.snapshot_id} is not a snapshot of {dataset.table}"
        ) from exc


def _check_manifest(
    adapter: RevisionCatalog,
    definition: RegisteredTableDefinition,
    manifest: ResearchDatasetManifest,
    selection: DatasetSelection,
) -> None:
    """``manifest`` binds the batch of ``selection`` and states exactly what it derived."""
    snapshot = _dataset_snapshot(adapter, definition, manifest)
    if snapshot.batch_id != selection.selection_id:
        raise CatalogIntegrityError(
            f"dataset snapshot {snapshot.snapshot_id} commits {snapshot.batch_id!r}, not the "
            f"selection {selection.selection_id} of the manifest's inputs"
        )
    # The expectation is built at the manifest's own recorded version (V7).
    version = contract_version.replay_version(
        manifest.schema_version, what=f"manifest {manifest.content_hash()}"
    )
    with contract_schema_version_scope(version):
        expected = _manifest_of(selection, manifest.dataset)
    if expected != manifest:
        drift = [
            name
            for name in ResearchDatasetManifest.model_fields
            if getattr(expected, name) != getattr(manifest, name)
        ]
        raise CatalogIntegrityError(
            f"manifest {manifest.content_hash()} is not what its inputs build: {drift} differ"
        )


# =========================================================================================
# pure helpers
# =========================================================================================


def _check_dataset_table(definition: RegisteredTableDefinition) -> None:
    if not isinstance(definition, RegisteredTableDefinition):
        raise DatasetSpecError("dataset_table must be a RegisteredTableDefinition")
    if definition.table.split(".", 1)[0] != SELECTION_NAMESPACE:
        raise DatasetSpecError(f"the dataset table must be in the {SELECTION_NAMESPACE} namespace")
    fresh = assign_fresh_schema_ids(SELECTION_SCHEMA)
    if definition.schema.model_dump_json() != fresh.model_dump_json():
        raise DatasetSpecError("the dataset table must have exactly SELECTION_SCHEMA")
    if definition.fingerprint_rule.rule_id != PYARROW_BATCH_FINGERPRINT.rule_id:
        raise DatasetSpecError("the dataset table must use the Phase 1 batch fingerprint")


def _time_column(data_type: str) -> str:
    return "event_time" if data_type == "agg_trades" else "interval_start"


def _days(start: datetime, end: datetime) -> list[date]:
    days: list[date] = []
    day = start.astimezone(UTC).date()
    while datetime.combine(day, time(), tzinfo=UTC) < end:
        days.append(day)
        day += _DAY
    return days


def _slices(data_type: str, start: datetime, end: datetime) -> list[tuple[datetime, datetime]]:
    """The window cut on the UTC grid of the data type's slice (hours / days), clipped."""
    step = _SLICES[data_type]
    midnight = datetime.combine(start.astimezone(UTC).date(), time(), tzinfo=UTC)
    low = start
    slices = []
    while low < end:
        grid = midnight + ((low - midnight) // step + 1) * step
        high = min(grid, end)
        slices.append((low, high))
        low = high
    return slices


def _selected_spans(
    items: Sequence[PointInTimeSelection], pit: PointInTimeSpec
) -> list[tuple[datetime | None, datetime | None, str]]:
    """Per key, the simulation spans (None = point) in which each revision is selected."""
    ordered = sorted(items, key=lambda item: item.simulation_time)
    spans: list[tuple[datetime | None, datetime | None, str]] = []
    for index, item in enumerate(ordered):
        if item.status is not PointInTimeStatus.SELECTED or item.selected_revision_id is None:
            continue
        if pit.simulation_time is not None:
            spans.append((None, None, item.selected_revision_id))
            continue
        until = (
            ordered[index + 1].simulation_time if index + 1 < len(ordered) else pit.simulation_end
        )
        spans.append((item.simulation_time, until, item.selected_revision_id))
    return spans


def _intersect(
    a_from: datetime | None,
    a_until: datetime | None,
    b_from: datetime | None,
    b_until: datetime | None,
) -> tuple[datetime | None, datetime | None] | None:
    ends = (a_from, a_until, b_from, b_until)
    if all(end is None for end in ends):
        # A point simulation: the selection holds and the symbol is a member.
        return (None, None)
    if a_from is None or a_until is None or b_from is None or b_until is None:
        # Interval spans are always closed (F3-R1, cursor review 2): a partial one is a bug.
        raise CatalogIntegrityError(f"an interval span is open at one end: {ends}")
    low, high = max(a_from, b_from), min(a_until, b_until)
    return (low, high) if low < high else None


def _bind_gaps(
    view: PinnedCatalogView, report_id: str, table: str, wanted: Mapping[str, str]
) -> list[AvailabilityEvidenceGap]:
    """The report's records of the ``wanted`` gaps of ``table`` (every one must be recorded)."""
    if not wanted:
        return []
    recorded = {
        item["revision_id"]: item["gap"]
        for item in evidence_gaps_of(view, report_id)
        if item["table"] == table and item["revision_id"] in wanted
    }
    missing = sorted(revision for revision, gap in wanted.items() if recorded.get(revision) != gap)
    if missing:
        raise DatasetQualityError(
            f"report {report_id} does not record the evidence gap of {table} revision {missing[0]}"
            f" ({len(missing)} missing)"
        )
    return [
        AvailabilityEvidenceGap(
            table=table, revision_id=revision, quality_report_id=report_id, gap=recorded[revision]
        )
        for revision in sorted(wanted)
    ]


def _row_order(row: Mapping[str, Any]) -> tuple[str, str, datetime, str]:
    return (
        row["symbol"],
        row["observation_key"],
        row["effective_from"] or _NO_SPAN,
        row["revision_id"],
    )

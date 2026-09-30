"""Research Dataset + ``ResearchDatasetManifest`` (Phase 1 F3; ADR-0023 §5 / §6, ADR-0024 §6).

``DatasetBuilder.select(universe, pit, data_type, start, end)`` is the read-only, deterministic
part; ``build`` materializes it and persists the manifest. Under ``hlens.dataset.pit-selection
@1.0.0``:

1. **bindings** (fail closed) — every availability / precedence / parser binding of the PIT spec
   must be a registered one with its exact hash (``KNOWN_BINDINGS``, or the ADR-0051 listing
   backfill assumption: ``ACCEPTED_BINDINGS``); the Canonical table of the
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
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from contextlib import AbstractContextManager, nullcontext
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from typing import Any, Final, Protocol, cast

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
    DATASET_EVIDENCE_FORMAT,
    AvailabilityEvidenceGap,
    DatasetChunkProof,
    DatasetQualityReportRef,
    DatasetQualitySubject,
    DatasetRuleBinding,
    EvidenceStream,
    EvidenceStreamRef,
    PitConflictHeadEvidence,
    ResearchDatasetEvidenceManifest,
    ResearchDatasetManifest,
    SelectedRevisionLineage,
    UniverseExclusion,
    UniverseMember,
    UniverseSelectionSpec,
    UniverseSpecBinding,
    dataset_chunk_batch_id,
)
from core.contracts.universe import (
    PitConflictEvidenceResult as PitConflictResult,
)
from core.domain.base import (
    CONTRACT_SCHEMA_VERSION,
    Contract,
    canonical_json,
    contract_schema_version_scope,
    parse_semver,
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
    DATA_QUALITY_REPORT_MANIFESTS,
    DATA_QUALITY_REPORTS,
    DATASET_MANIFESTS,
    QUALITY_EVIDENCE_GAPS,
)
from infrastructure.dataset import evidence as _evidence
from infrastructure.dataset.evidence import (
    EVIDENCE_PROJECTION,
    EvidenceLimitError,
    EvidenceTreeLimits,
    EvidenceTreeWriter,
)
from infrastructure.dataset.manifests import ManifestPersisted, ManifestStore
from infrastructure.dataset.selection import SELECTION_NAMESPACE, SELECTION_SCHEMA
from infrastructure.parser.binance_archive import PARSER_BINDING as ARCHIVE_PARSER_BINDING
from infrastructure.parser.binance_exchange_info import EXCHANGE_INFO_DECODER_BINDING
from infrastructure.parser.binance_rest import DECODER_BINDING as REST_DECODER_BINDING
from infrastructure.pit.assumption import ASSUMPTION_BINDING
from infrastructure.pit.selector import PIT_BINDING, PitConflictError, PitSelection, PitSelector
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
from infrastructure.universe.builder import (
    REGISTERED_UNIVERSES,
    UniverseBuilder,
    UniverseBuilt,
    check_listing_bindings,
)
from infrastructure.universe.listing_assumption import (
    ASSUMPTION_BINDING as LISTING_ASSUMPTION_BINDING,
)

__all__ = [
    "ACCEPTED_BINDINGS",
    "DATASET_EVIDENCE_RULE_VERSION",
    "DATASET_RULE_HASH",
    "DATASET_RULE_ID",
    "DATASET_RULE_SPEC",
    "DATASET_RULE_VERSION",
    "KNOWN_BINDINGS",
    "ChunkCommitted",
    "DatasetBuildError",
    "DatasetBuildSummary",
    "DatasetBuilder",
    "DatasetBuilt",
    "DatasetChunkWriter",
    "DatasetDerivation",
    "DatasetDerivationSink",
    "DatasetEmpty",
    "DatasetEvidenceBuilder",
    "DatasetEvidenceRequest",
    "DatasetEvidenceRule",
    "DatasetEvidenceSources",
    "DatasetQualityError",
    "DatasetSelection",
    "DatasetSpecError",
    "EvidenceManifestStore",
    "MemberSpan",
    "PinnedQualityEvidence",
    "PitKeyEvaluation",
    "PitKeyGroup",
    "PitKeySource",
    "PitConflictResult",
    "PitSelectedRevision",
    "QualityEvidenceSource",
    "UniverseEvidenceSource",
    "dataset_evidence_rule",
    "evidence_selection_id",
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
#: ``KNOWN_BINDINGS`` plus the ADR-0051 listing backfill assumption (D-LIST, accepted
#: 2026-09-28): a spec may bind it (exact id, version and hash), it is never required. Kept out of
#: ``KNOWN_BINDINGS`` itself, whose published envelopes and hashes are pinned as the Phase 1
#: registry; the manifest binds it through its PIT spec like any other availability policy.
ACCEPTED_BINDINGS: Final = KNOWN_BINDINGS | {LISTING_ASSUMPTION_BINDING}

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
_QUALITY_MANIFESTS: Final = DATA_QUALITY_REPORT_MANIFESTS.table
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

    @property
    def adapter(self) -> RevisionCatalog:
        """The catalog this builder builds, materializes and verifies against (public; C1-CONSUMERS
        follow-up: previously read only through the private ``_adapter`` attribute)."""
        return self._adapter

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
                if binding not in ACCEPTED_BINDINGS:
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


# =========================================================================================
# v3: bounded evidence datasets (ADR-0077; ``hlens.dataset.pit-selection@2.1.0``)
# =========================================================================================
#
# Everything above is the v2 (legacy, materializing) path and is left exactly as it was. The v3
# path below never holds a collection that grows with the selected rows or the window:
#
# - its inputs are read-only, explicitly closed, ordered iterators behind three Protocols
#   (``UniverseEvidenceSource`` for B-UNIV, ``PitKeySource`` for B-PIT and
#   ``QualityEvidenceSource``, implemented here by ``PinnedQualityEvidence``);
# - one generator (``_EvidenceDerivation``) derives the dataset rows and five of the six evidence
#   streams in their canonical orders and pushes each item to a ``DatasetDerivationSink``;
#   ordering, uniqueness, ownership, lineage and report bindings are checked on adjacent items;
# - ``DatasetEvidenceBuilder.select`` runs it against any sink (the streaming verifier, B4,
#   compares against ``iter_evidence`` / ``iter_rows``); ``build`` runs it against a sink that
#   writes evidence trees and hands fixed-size chunks to a ``DatasetChunkWriter`` (B3), then
#   persists the fixed-size ``ResearchDatasetEvidenceManifest`` through an
#   ``EvidenceManifestStore`` (B4) and returns a fixed-size ``DatasetBuildSummary``.
#
# Held state: one key group's revision ids (DQ-5 / §2 "dedupe inside the key group"), one
# symbol's member spans (bounded by its membership changes in the window, as B-UNIV's instants),
# one chunk of rows (``chunk_rows``), one leaf and a ``depth x fanout`` index stack per stream,
# one cached report id and the adjacent-item comparison state.

DATASET_EVIDENCE_RULE_VERSION: Final = "2.1.0"  # F-C, 2026-09-28: EVIDENCE_PROJECTION text fix


#: The evidence streams the generator derives; ``chunk_proofs`` come from the chunk commits.
def _derived_streams(schema_version: str) -> tuple[EvidenceStream, ...]:
    """Streams in one recorded manifest era (ADR-0094 preserves six-stream replay)."""
    version = parse_semver(schema_version)
    core = tuple(int(version.group(name)) for name in ("major", "minor", "patch"))
    return tuple(
        stream
        for stream in EvidenceStream
        if stream is not EvidenceStream.CHUNK_PROOFS
        and (stream is not EvidenceStream.PIT_CONFLICTS or core >= (2, 5, 0))
    )


#: A member span of one venue symbol: ``(None, None)`` for a point simulation.
MemberSpan = tuple[datetime | None, datetime | None]


def _evidence_rule_spec(chunk_rows: int, limits: EvidenceTreeLimits) -> dict[str, Any]:
    return {
        "rule": DATASET_RULE_ID,
        "version": DATASET_EVIDENCE_RULE_VERSION,
        "adr": [
            "ADR-0023 §5 / §6",
            "ADR-0024 §4 / §6",
            "ADR-0027 §13",
            "ADR-0028 §7",
            "ADR-0077",
        ],
        "inputs": "UniverseSelectionSpec, PointInTimeSpec, data_type, "
        "UTC event window [start, end)",
        "bindings": "every policy / parser binding registered with its exact hash; Canonical, "
        "listing and quality tables bound; the Raw evidence and evidence-gap tables bound whenever "
        "they have a snapshot (a persisted manifest of this selection exempts its replay)",
        "universe": "ordered member / exclusion entries and listing lineage at the bound "
        "snapshots; unconstructible = no dataset",
        "slices": {"agg_trades": "UTC hour", "klines_1m": "UTC day"},
        "selection": "hlens.pit.maximal-head@1.0.0 per member symbol and slice; a key is owned by "
        "the slice holding its chain's earliest event (DQ-5); keys strictly increasing per slice; "
        "any conflict fails the dataset closed",
        "membership": "a selected revision enters for the simulation span where it is selected and "
        "its symbol is a member",
        "quality": "the listing history and each covered partition (member symbol x UTC day of the "
        "window or of a retained revision's event, days non-decreasing per symbol) have a "
        "committed report at exactly the bound snapshots (re-derived); every bound evidence gap "
        "is recorded, with the same text, in the report of its partition",
        "rows": "one per (key, selected revision, span): selection_id, canonical_table, symbol, "
        "observation_key, revision_id, event_time, effective_from, effective_until, chunk_index, "
        "row_ordinal",
        "row_order": "(member symbol, slice, observation_key, effective_from, revision_id); "
        "row_ordinal from 0, contiguous; chunk_index = row_ordinal // chunk_rows",
        "chunks": {
            "chunk_rows": chunk_rows,
            "batch_id": "<selection_id>.chunk-<chunk_index, 10 digits zero-padded>",
            "last": "may hold fewer than chunk_rows rows",
        },
        "evidence": {
            "format": DATASET_EVIDENCE_FORMAT,
            "projection": EVIDENCE_PROJECTION,
            "leaf_max_records": limits.leaf_max_records,
            "leaf_max_bytes": limits.leaf_max_bytes,
            "leaf_bytes": "sum of a leaf's record line bytes, header excluded",
            "fanout": limits.fanout,
            "streams": {
                "members": "(episode.observation_key(), effective_from)",
                "exclusions": "(episode.observation_key(), effective_from)",
                "lineage": "listing lineage by canonical_revision_id, then data lineage by the "
                "row_ordinal of the revision's first row",
                "evidence_gaps": "listing gaps by revision_id, then data gaps by the row_ordinal "
                "of the revision's first row",
                "quality_reports": "listing, then (venue symbol, UTC day) strictly increasing",
                "chunk_proofs": "chunk_index from 0, contiguous",
            },
        },
        "empty": "refused",
        "selection_id": "<rule>@<version>.<sha256 of rule hash, PIT spec hash, universe binding, "
        "data_type, window>",
    }


def _rule_hash(chunk_rows: int, limits: EvidenceTreeLimits) -> str:
    spec = _evidence_rule_spec(chunk_rows, limits)
    return hashlib.sha256(canonical_json(spec).encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class DatasetEvidenceRule:
    """``hlens.dataset.pit-selection@2.1.0`` with its four resource parameters (ADR-0077 §3.6).

    The parameters are part of the rule spec and so of ``rule_hash``: another value is another
    rule. No value is chosen here (DQ-9 OPEN): build one with ``dataset_evidence_rule``.
    """

    chunk_rows: int
    limits: EvidenceTreeLimits
    rule_hash: str

    def __post_init__(self) -> None:
        if (
            isinstance(self.chunk_rows, bool)
            or not isinstance(self.chunk_rows, int)
            or self.chunk_rows < 1
        ):
            raise DatasetSpecError(f"chunk_rows must be an integer >= 1, got {self.chunk_rows!r}")
        if not isinstance(self.limits, EvidenceTreeLimits):
            raise DatasetSpecError("limits must be EvidenceTreeLimits")
        if self.rule_hash != _rule_hash(self.chunk_rows, self.limits):
            raise DatasetSpecError("rule_hash is not the hash of this rule's spec")

    @property
    def spec(self) -> dict[str, Any]:
        return _evidence_rule_spec(self.chunk_rows, self.limits)

    def binding(self) -> DatasetRuleBinding:
        return DatasetRuleBinding(
            rule_id=DATASET_RULE_ID, version=DATASET_EVIDENCE_RULE_VERSION, rule_hash=self.rule_hash
        )

    def binds(self, binding: DatasetRuleBinding) -> bool:
        """Whether ``binding`` names exactly this rule (id, version and hash)."""
        return (binding.rule_id, binding.version, binding.rule_hash) == (
            DATASET_RULE_ID,
            DATASET_EVIDENCE_RULE_VERSION,
            self.rule_hash,
        )


def dataset_evidence_rule(
    *, chunk_rows: int, leaf_max_records: int, leaf_max_bytes: int, fanout: int
) -> DatasetEvidenceRule:
    """The v3 dataset rule for these parameters; every one is required (DQ-9: no defaults)."""
    try:
        limits = EvidenceTreeLimits(
            leaf_max_records=leaf_max_records, leaf_max_bytes=leaf_max_bytes, fanout=fanout
        )
    except EvidenceLimitError as exc:
        raise DatasetSpecError(str(exc)) from exc
    if isinstance(chunk_rows, bool) or not isinstance(chunk_rows, int) or chunk_rows < 1:
        raise DatasetSpecError(f"chunk_rows must be an integer >= 1, got {chunk_rows!r}")
    return DatasetEvidenceRule(chunk_rows, limits, _rule_hash(chunk_rows, limits))


def evidence_selection_id(
    rule: DatasetEvidenceRule,
    universe: UniverseSpecBinding,
    pit: PointInTimeSpec,
    data_type: str,
    start: datetime,
    end: datetime,
) -> str:
    """The v3 ``selection_id``: what a v3 manifest alone recomputes (rule, PIT, universe, window).

    Another rule (version or parameters) is another id, so v2 and v3 selections never mix.
    """
    document = {
        "rule": rule.rule_hash,
        "point_in_time": pit.content_hash(),
        "universe": universe.model_dump(mode="json"),
        "data_type": data_type,
        "start": start.isoformat(),
        "end": end.isoformat(),
    }
    digest = hashlib.sha256(canonical_json(document).encode("utf-8")).hexdigest()
    return f"{DATASET_RULE_ID}@{DATASET_EVIDENCE_RULE_VERSION}.{digest}"


# ------------------------------------------------------------------ inputs (read-only, ordered)


@dataclass(frozen=True, slots=True)
class DatasetEvidenceRequest:
    """What a v3 dataset is derived from (the same inputs as v2, bundled)."""

    universe: UniverseSelectionSpec
    pit: PointInTimeSpec
    data_type: str
    start: datetime
    end: datetime


class UniverseEvidenceSource(Protocol):
    """The universe of one ``(spec, pit)`` as ordered cursors (B-UNIV, ADR-0077 §6.1.1).

    Structurally ``infrastructure.universe.builder.UniverseSpanCursor`` (from
    ``UniverseBuilder.cursor(request.universe, request.pit)``; the caller builds it for the same
    request). Every method opens an independent, explicitly closed pass; the builder opens
    ``members`` + ``exclusions`` together, then ``listing_lineage`` + ``evidence_gaps`` together,
    then ``member_spans`` once. Required orders (checked here, fail closed otherwise):

    - ``members`` / ``exclusions``: each by ``(episode.observation_key(), effective_from)``
      (ADR-0077 §2; the cursor's generation order must coincide with it);
    - ``listing_lineage``: each cited ``canonical.instrument_listings`` revision once, by
      ``canonical_revision_id`` (ADR-0077 §2);
    - ``evidence_gaps``: ``(listing revision id, gap)`` of the gap-bearing ones, in the same order
      as ``listing_lineage``;
    - ``member_spans``: ``(venue symbol, effective_from, effective_until)`` of every member span,
      by venue symbol then start (point: ``(symbol, None, None)``).
    """

    def members(self) -> AbstractContextManager[Iterator[UniverseMember]]: ...

    def exclusions(self) -> AbstractContextManager[Iterator[UniverseExclusion]]: ...

    def listing_lineage(self) -> AbstractContextManager[Iterator[SelectedRevisionLineage]]: ...

    def member_spans(
        self,
    ) -> AbstractContextManager[Iterator[tuple[str, datetime | None, datetime | None]]]: ...

    def evidence_gaps(self) -> AbstractContextManager[Iterator[tuple[str, str]]]: ...


@dataclass(frozen=True, slots=True)
class PitSelectedRevision:
    """The selected revision of one evaluation, with what a dataset row and its lineage need."""

    revision_id: str
    #: The proven Canonical row's time column (``event_time`` / ``interval_start``), UTC.
    event_time: datetime
    lineage: SelectedRevisionLineage
    #: The row's ``availability_evidence_gap`` (None = evidence given).
    evidence_gap: str | None


@dataclass(frozen=True, slots=True)
class PitKeyEvaluation:
    """One ``PointInTimeSelection`` of a key, reduced to what the dataset consumes."""

    simulation_time: datetime
    status: PointInTimeStatus
    selected: PitSelectedRevision | None
    head_count: int | None = None


@dataclass(frozen=True, slots=True)
class PitKeyGroup:
    """One observation key owned by the slice, with its evaluations.

    ``owner_event_time`` is the earliest event of the key's revision chain — the witness of the
    slice ownership rule (``PIT_SPEC["window"]``, DQ-5): it must lie in the slice. ``evaluations``
    is consumed once, in strictly increasing ``simulation_time`` (point: exactly one, at the
    simulation time; interval: the first at the interval start).
    """

    observation_key: str
    owner_event_time: datetime
    evaluations: Iterable[PitKeyEvaluation]


class PitKeySource(Protocol):
    """PIT selection of one (symbol, slice) as an ordered cursor (B-PIT, ADR-0077 §6.1.2).

    Yields only the keys the slice owns, in strictly increasing ``observation_key``, each once,
    releasing a key's state before the next; bindings are checked as by ``PitSelector.select``.
    """

    def keys(
        self,
        pit: PointInTimeSpec,
        data_type: str,
        venue_symbol: str,
        start: datetime,
        end: datetime,
        *,
        conflict_sink: Callable[[PitConflictHeadEvidence], None] | None = None,
    ) -> AbstractContextManager[Iterator[PitKeyGroup]]: ...


class QualityEvidenceSource(Protocol):
    """Bounded ADR-0093 reports re-derived at Dataset's pinned snapshots."""

    def listing_report(self) -> str:
        """The listing-history report id (``QualityReportMissing`` if not committed)."""
        ...

    def partition_report(self, venue_symbol: str, day: date) -> str:
        """The (symbol, UTC day) report id of the dataset's data type."""
        ...

    def claim_gap(self, report_id: str, table: str, revision_id: str, gap: str) -> None:
        """Queue one Dataset gap claim for bounded external ordered-join verification."""
        ...

    def finish_reports(self) -> None:
        """Drain and verify the active report's complete evidence-gap root."""
        ...

    def close(self) -> None:
        """Release bounded claim buffers after success or failure."""
        ...


@dataclass(frozen=True, slots=True)
class DatasetEvidenceSources:
    universe: UniverseEvidenceSource
    pit: PitKeySource
    quality: QualityEvidenceSource


class PinnedQualityEvidence:
    """``QualityEvidenceSource`` over a ``PinnedCatalogView`` of the PIT spec (as v2 reads it).

    OPEN (ADR-0077 §6.1.5): ``evidence_gaps_of`` returns one report's gaps as a list; this class
    holds the gaps of **one** report at a time. Whether that is bounded by a contract-level fixed
    size, or must become a row-wise stream, is not proven here.
    """

    def __init__(
        self,
        adapter: RevisionCatalog,
        storage: StorageAdapter,
        pit: PointInTimeSpec,
        data_type: str,
        *,
        market_data_base_url: str,
    ) -> None:
        self._view = PinnedCatalogView(adapter, pit.snapshot_bindings)
        self._storage = storage
        self._data_type = data_type
        self._origin = market_data_base_url
        self._reporter = QualityReporter(self._view, storage)
        self._gaps_of: str | None = None
        self._gaps: dict[tuple[str, str], str] = {}

    def listing_report(self) -> str:
        return (
            ListingQualityReporter(self._view, self._storage, market_data_base_url=self._origin)
            .report(existing_only=True)
            .report_id
        )

    def partition_report(self, venue_symbol: str, day: date) -> str:
        return self._reporter.report(
            self._data_type, venue_symbol, day, existing_only=True
        ).report_id

    def recorded_gap(self, report_id: str, table: str, revision_id: str) -> str | None:
        if self._gaps_of != report_id:
            self._gaps = {
                (item["table"], item["revision_id"]): item["gap"]
                for item in evidence_gaps_of(self._view, report_id)
            }
            self._gaps_of = report_id
        return self._gaps.get((table, revision_id))


# ------------------------------------------------------------------ outputs


class DatasetDerivationSink(Protocol):
    """Receives the derivation in order: evidence records per stream and rows by ordinal."""

    def evidence(self, stream: EvidenceStream, record: Contract) -> None: ...

    def row(self, row: Mapping[str, Any]) -> None: ...


@dataclass(frozen=True, slots=True)
class ChunkCommitted:
    proof: DatasetChunkProof
    replayed: bool


class DatasetChunkWriter(Protocol):
    """Commits one fixed-size chunk of rows (B3, ADR-0077 §4; ``infrastructure/dataset/chunks``).

    ``commit_chunk`` commits (or, if already committed, proves identical) the batch
    ``dataset_chunk_batch_id(selection_id, chunk_index)`` and reads it back; a chunk committed with
    other rows, a hole (a later chunk committed before an earlier one is missing) or a duplicate
    batch is ``CatalogIntegrityError``. ``seal`` proves no chunk ``>= chunk_count`` of the
    selection exists. ``table`` is the chunk table (the manifest's ``dataset.table``).
    """

    @property
    def table(self) -> str: ...

    def commit_chunk(
        self, selection_id: str, chunk_index: int, rows: Sequence[Mapping[str, Any]]
    ) -> ChunkCommitted: ...

    def seal(self, selection_id: str, chunk_count: int) -> None: ...


class EvidenceManifestStore(Protocol):
    """The v3 manifest table (B4, ADR-0077 §7 / §8.2; ``infrastructure/dataset/manifests``)."""

    def recorded_version(self, selection_id: str) -> str | None:
        """The contract version of a persisted manifest of ``selection_id``, else ``None``."""
        ...

    def persist(self, manifest: ResearchDatasetEvidenceManifest) -> bool:
        """Verify and persist (content-hash idempotent); ``True`` when it was already there."""
        ...


@dataclass(frozen=True, slots=True)
class DatasetDerivation:
    """Fixed-size result of ``select``: the id and how much was derived."""

    selection_id: str
    row_count: int
    #: ``(stream, record count)`` of the five derived streams, by stream name.
    record_counts: tuple[tuple[EvidenceStream, int], ...]


@dataclass(frozen=True, slots=True)
class DatasetBuildSummary:
    """Fixed-size result of ``build`` (ADR-0077 §5): no rows, lineage or gap tuples."""

    selection_id: str
    manifest: ResearchDatasetEvidenceManifest
    manifest_hash: str
    dataset: DatasetRef
    row_count: int
    chunk_count: int
    replayed_chunk_count: int
    evidence: tuple[EvidenceStreamRef, ...]
    manifest_replayed: bool

    @property
    def replayed(self) -> bool:
        return self.manifest_replayed and self.replayed_chunk_count == self.chunk_count


# ------------------------------------------------------------------ the builder


class DatasetEvidenceBuilder:
    """Builds v3 Research Datasets with a fixed working set; deterministic for fixed inputs."""

    def __init__(
        self, adapter: RevisionCatalog, storage: StorageAdapter, *, rule: DatasetEvidenceRule
    ) -> None:
        if not isinstance(rule, DatasetEvidenceRule):
            raise DatasetSpecError("rule must be a DatasetEvidenceRule")
        self._adapter = adapter
        self._storage = storage
        self._rule = rule

    @property
    def rule(self) -> DatasetEvidenceRule:
        return self._rule

    def selection_id(self, request: DatasetEvidenceRequest) -> str:
        _check_evidence_request(request, dataset_table=None)
        return evidence_selection_id(
            self._rule,
            request.universe.binding(),
            request.pit,
            request.data_type,
            request.start,
            request.end,
        )

    def select(
        self,
        request: DatasetEvidenceRequest,
        *,
        sources: DatasetEvidenceSources,
        sink: DatasetDerivationSink,
        manifested: bool,
        schema_version: str = CONTRACT_SCHEMA_VERSION,
    ) -> DatasetDerivation:
        """Derive everything but the chunk proofs into ``sink``; nothing is written.

        ``manifested``: whether a persisted manifest binds this selection (a replay, exempt from
        the unbound-table refusal as in v2). A caller re-deriving a persisted manifest runs this
        inside ``contract_schema_version_scope(<its recorded version>)``.
        """
        canonical = _check_evidence_request(request, dataset_table=None)
        selection_id = self.selection_id(request)
        _check_unbound_evidence(self._adapter, request.pit, manifested=manifested)
        return _EvidenceDerivation(
            request, canonical, selection_id, self._rule, sources, sink, schema_version
        ).run()

    def build(
        self,
        request: DatasetEvidenceRequest,
        *,
        sources: DatasetEvidenceSources,
        chunks: DatasetChunkWriter,
        manifests: EvidenceManifestStore,
    ) -> DatasetBuildSummary:
        """Derive, commit the chunks, write the evidence trees, persist the manifest.

        A rerun re-derives from scratch (no cursor is persisted): committed chunks are proven and
        skipped by ``chunks``, evidence objects are ``already_present``, the manifest is
        idempotent. A manifest persisted at an earlier contract version is re-derived at that
        version (ADR-0052 V7); evidence and rows carry no envelope, so their bytes do not move.
        """
        canonical = _check_evidence_request(request, dataset_table=chunks.table)
        selection_id = self.selection_id(request)
        recorded = manifests.recorded_version(selection_id)
        _check_unbound_evidence(self._adapter, request.pit, manifested=recorded is not None)
        scope: AbstractContextManager[object]
        if recorded is None:
            version = contract_version.new_group_version()
            scope = nullcontext()
        else:
            version = contract_version.replay_version(
                recorded, what=f"the evidence manifest of {selection_id}"
            )
            scope = contract_schema_version_scope(version)
        with scope:
            sink = _EvidenceBuildSink(self._storage, self._rule, selection_id, chunks, version)
            derived = _EvidenceDerivation(
                request, canonical, selection_id, self._rule, sources, sink, version
            ).run()
            if derived.row_count == 0:
                raise DatasetEmpty(
                    f"{request.data_type} [{request.start.isoformat()}, {request.end.isoformat()})"
                    " selects nothing for the universe's members: an empty Research Dataset has "
                    "no snapshot of its own"
                )
            streams, chunk_count, replayed_chunks, snapshot_id = sink.finish()
            chunks.seal(selection_id, chunk_count)
            manifest = ResearchDatasetEvidenceManifest(
                dataset=DatasetRef(
                    zone=Zone.RESEARCH_DATASET,
                    table=chunks.table,
                    snapshot_id=snapshot_id,
                    time_range_start=request.start,
                    time_range_end=request.end,
                ),
                point_in_time=request.pit,
                universe_spec=request.universe.binding(),
                rule=self._rule.binding(),
                data_type=request.data_type,
                selection_id=selection_id,
                row_count=derived.row_count,
                chunk_rows=self._rule.chunk_rows,
                chunk_count=chunk_count,
                evidence=streams,
            )
        if manifest.schema_version != version:  # pragma: no cover - built in its version scope
            raise CatalogIntegrityError(f"manifest of {selection_id} built outside its version")
        manifest_replayed = manifests.persist(manifest)
        return DatasetBuildSummary(
            selection_id=selection_id,
            manifest=manifest,
            manifest_hash=manifest.content_hash(),
            dataset=manifest.dataset,
            row_count=manifest.row_count,
            chunk_count=manifest.chunk_count,
            replayed_chunk_count=replayed_chunks,
            evidence=manifest.evidence,
            manifest_replayed=manifest_replayed,
        )

    def iter_evidence(
        self, manifest: ResearchDatasetEvidenceManifest, stream: EvidenceStream
    ) -> AbstractContextManager[Iterator[Contract]]:
        """``evidence.iter_evidence`` with this builder's rule, which ``manifest`` must bind."""
        if not isinstance(manifest, ResearchDatasetEvidenceManifest):
            raise DatasetSpecError("manifest must be a ResearchDatasetEvidenceManifest")
        if not self._rule.binds(manifest.rule) or manifest.chunk_rows != self._rule.chunk_rows:
            raise CatalogIntegrityError(
                f"manifest rule {manifest.rule.rule_id}@{manifest.rule.version} is not this "
                "builder's rule (its parameters would not read its evidence)"
            )
        return _evidence.iter_evidence(self._storage, manifest, stream, limits=self._rule.limits)


# ------------------------------------------------------------------ request checks


def _check_utc(value: object, what: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() != _ZERO:
        raise DatasetSpecError(f"{what} must be a timezone-aware UTC datetime")
    return value


def _check_evidence_request(request: DatasetEvidenceRequest, *, dataset_table: str | None) -> str:
    """The v2 request checks for a v3 request; returns the data type's Canonical table."""
    if not isinstance(request, DatasetEvidenceRequest):
        raise DatasetSpecError("request must be a DatasetEvidenceRequest")
    universe, pit = request.universe, request.pit
    if not isinstance(universe, UniverseSelectionSpec):
        raise DatasetSpecError("universe must be a UniverseSelectionSpec")
    registered = REGISTERED_UNIVERSES.get((universe.name, universe.version))
    if registered is None or registered.content_hash() != universe.content_hash():
        raise DatasetSpecError(
            f"universe spec {universe.name}@{universe.version} with this hash is not registered"
        )
    if not isinstance(pit, PointInTimeSpec):
        raise DatasetSpecError("pit must be a PointInTimeSpec")
    canonical = rules.CANONICAL_TABLES.get(request.data_type)
    if canonical is None:
        raise DatasetSpecError(f"unsupported data_type {request.data_type!r}")
    start = _check_utc(request.start, "start")
    end = _check_utc(request.end, "end")
    if not start < end:
        raise DatasetSpecError("the window must not be empty")
    if pit.point_in_time_binding != PIT_BINDING:
        raise DatasetSpecError("the PIT rule must be hlens.pit.maximal-head@1.0.0")
    for field in ("availability_bindings", "precedence_bindings", "parser_bindings"):
        for binding in getattr(pit, field):
            if binding not in ACCEPTED_BINDINGS:
                raise DatasetSpecError(
                    f"{field}: {binding.policy_id}@{binding.version} with this hash is not "
                    "registered"
                )
    bound = pit.snapshot_bindings
    for table in (canonical.table, _QUALITY, _QUALITY_MANIFESTS):
        if table not in bound:
            raise DatasetSpecError(f"the PIT spec does not bind {table}")
    check_listing_bindings(pit)
    if dataset_table is not None:
        if dataset_table.split(".", 1)[0] != SELECTION_NAMESPACE:
            raise DatasetSpecError(
                f"the dataset table must be in the {SELECTION_NAMESPACE} namespace"
            )
        if dataset_table in bound:
            raise DatasetSpecError("the dataset's own table must not be an upstream binding")
    return canonical.table


def _head_of(adapter: RevisionCatalog, table: str) -> str | None:
    info = adapter.load_table(table)
    if info is None:
        raise TableNotFound(f"table {table} does not exist")
    return None if info.current_snapshot is None else info.current_snapshot.snapshot_id


def _check_unbound_evidence(
    adapter: RevisionCatalog, pit: PointInTimeSpec, *, manifested: bool
) -> None:
    """v2's ``_check_unbound`` for v3: only a persisted manifest exempts a replay (G2 RT-5)."""
    if manifested:
        return
    for table, adr in _BOUND_IF_PRESENT:
        if table not in pit.snapshot_bindings and _head_of(adapter, table) is not None:
            raise DatasetSpecError(
                f"{table} has a snapshot but the PIT spec does not bind it ({adr})"
            )


# ------------------------------------------------------------------ the generator


def _iter_slices(
    data_type: str, start: datetime, end: datetime
) -> Iterator[tuple[datetime, datetime]]:
    """``_slices`` as a generator (the window is never listed)."""
    step = _SLICES[data_type]
    midnight = datetime.combine(start.astimezone(UTC).date(), time(), tzinfo=UTC)
    low = start
    while low < end:
        high = min(midnight + ((low - midnight) // step + 1) * step, end)
        yield low, high
        low = high


def _last_window_day(start: datetime, end: datetime) -> date:
    """The last UTC day ``d`` with ``midnight(d) < end`` (the last day ``_days`` lists)."""
    last = end.astimezone(UTC).date()
    if datetime.combine(last, time(), tzinfo=UTC) >= end:
        last -= _DAY
    return last


def _required(value: datetime | None) -> datetime:
    if value is None:  # pragma: no cover - the contract: an interval has both ends
        raise DatasetSpecError("the PIT spec has neither a simulation time nor an interval")
    return value


def _selected_spans_of(
    evaluations: Iterable[PitKeyEvaluation],
    pit: PointInTimeSpec,
    key: str,
    *,
    on_conflict: Callable[[PitKeyEvaluation], PitConflictResult | None] | None = None,
) -> Iterator[tuple[datetime | None, datetime | None, PitSelectedRevision]]:
    """``_selected_spans`` over a once-consumed, time-ordered evaluation stream."""
    point = pit.simulation_time is not None
    previous: PitKeyEvaluation | None = None
    for evaluation in evaluations:
        if not isinstance(evaluation, PitKeyEvaluation):
            raise CatalogIntegrityError(f"key {key}: an evaluation is not a PitKeyEvaluation")
        at = _check_utc(evaluation.simulation_time, f"key {key} simulation_time")
        if evaluation.status is PointInTimeStatus.CONFLICT:
            if evaluation.head_count is not None and evaluation.head_count < 2:
                raise CatalogIntegrityError(f"key {key}: conflict has fewer than two heads")
            result = None if on_conflict is None else on_conflict(evaluation)
            raise PitConflictError(
                f"observation key {key} has competing heads at {at.isoformat()}: the dataset "
                "build fails closed",
                result=result,
            )
        if evaluation.status not in (PointInTimeStatus.SELECTED, PointInTimeStatus.ABSENT) or (
            evaluation.status is PointInTimeStatus.SELECTED
        ) != (evaluation.selected is not None):
            raise CatalogIntegrityError(f"key {key}: a {evaluation.status} result is malformed")
        if point:
            if previous is not None or at != pit.simulation_time:
                raise CatalogIntegrityError(f"key {key}: a point simulation has one evaluation")
        elif previous is None:
            if at != pit.simulation_start:
                raise CatalogIntegrityError(f"key {key}: the first evaluation is not the start")
        elif not previous.simulation_time < at < _required(pit.simulation_end):
            raise CatalogIntegrityError(f"key {key}: evaluations are not increasing in the span")
        if previous is not None and previous.selected is not None:
            yield previous.simulation_time, at, previous.selected
        previous = evaluation
    if previous is None:
        raise CatalogIntegrityError(f"key {key} has no evaluation")
    if previous.selected is not None:
        if point:
            yield None, None, previous.selected
        else:
            yield previous.simulation_time, pit.simulation_end, previous.selected


def _checked_member_spans(
    spans: Iterator[MemberSpan], pit: PointInTimeSpec, venue_symbol: str
) -> Iterator[MemberSpan]:
    """The member spans, proven ordered, disjoint and inside the simulation (or the one point)."""
    point = pit.simulation_time is not None
    previous: MemberSpan | None = None
    for span in spans:
        low, high = span
        if point:
            if previous is not None or (low, high) != (None, None):
                raise CatalogIntegrityError(
                    f"{venue_symbol}: a point simulation has one open member span"
                )
        else:
            if low is None or high is None:
                raise CatalogIntegrityError(f"{venue_symbol}: an interval member span is open")
            _check_utc(low, "member span start")
            _check_utc(high, "member span end")
            if not _required(pit.simulation_start) <= low < high <= _required(pit.simulation_end):
                raise CatalogIntegrityError(f"{venue_symbol}: a member span leaves the simulation")
            if previous is not None and _required(previous[1]) > low:
                raise CatalogIntegrityError(f"{venue_symbol}: member spans overlap or are unsorted")
        previous = span
        yield span


class _MemberSpans:
    """The cursor's member spans, taken one venue symbol at a time (symbols ascending).

    Holds the spans of **one** symbol: bounded by that symbol's membership changes inside the
    simulation window (the bound B-UNIV declares for its change instants), never by rows or keys.
    The whole-universe ``member_spans`` mapping of v2 is never built; reopening the cursor per
    observation key instead would re-walk the universe once per key.
    """

    def __init__(
        self,
        spans: Iterator[tuple[str, datetime | None, datetime | None]],
        pit: PointInTimeSpec,
    ) -> None:
        self._spans = spans
        self._pit = pit
        self._pending = self._next()

    def _next(self) -> tuple[str, datetime | None, datetime | None] | None:
        item = next(self._spans, None)
        if item is not None and (
            not isinstance(item, tuple) or len(item) != 3 or not isinstance(item[0], str)
        ):
            raise CatalogIntegrityError(f"a member span is malformed: {item!r}")
        return item

    def take(self, venue_symbol: str) -> tuple[MemberSpan, ...]:
        mine: list[MemberSpan] = []
        while self._pending is not None and self._pending[0] == venue_symbol:
            mine.append((self._pending[1], self._pending[2]))
            self._pending = self._next()
        if self._pending is not None and self._pending[0] < venue_symbol:
            raise CatalogIntegrityError(
                f"member spans of {self._pending[0]} are out of symbol order or not in the spec"
            )
        return tuple(_checked_member_spans(iter(mine), self._pit, venue_symbol))

    def close(self) -> None:
        if self._pending is not None:
            raise CatalogIntegrityError(
                f"member spans of {self._pending[0]} are out of symbol order or not in the spec"
            )


def _gated_rows(
    selected: Iterator[tuple[datetime | None, datetime | None, PitSelectedRevision]],
    members: Iterator[MemberSpan],
) -> Iterator[tuple[tuple[datetime | None, datetime | None], PitSelectedRevision]]:
    """Every (selected span x member span) intersection, by start: a merge of two sorted,
    disjoint families. Yields exactly what v2's nested loop (each selected span, each member
    span) yields, in the same order, holding one item of each side.

    ``selected`` is always drained: every evaluation of the key is checked (a conflict after the
    last member span still fails the dataset closed, as v2's ``require_no_conflict`` does)."""
    a = next(selected, None)
    b = next(members, None)
    while a is not None and b is not None:
        a_from, a_until, revision = a
        b_from, b_until = b
        effective = _intersect(a_from, a_until, b_from, b_until)
        if effective is not None:
            yield effective, revision
        if a_until is None or b_until is None:
            break  # a point simulation: one selected span, one member span
        if a_until <= b_until:
            a = next(selected, None)
        else:
            b = next(members, None)
    for _ in selected:
        pass


def _entry_items(
    entries: Iterator[UniverseMember] | Iterator[UniverseExclusion],
    stream: EvidenceStream,
    pit: PointInTimeSpec,
) -> Iterator[tuple[str, datetime, datetime, EvidenceStream, Contract]]:
    """``(observation_key, start, end, stream, entry)`` of one entry stream, shapes checked."""
    kind = UniverseMember if stream is EvidenceStream.MEMBERS else UniverseExclusion
    point = pit.simulation_time is not None
    for entry in entries:
        if type(entry) is not kind:
            raise CatalogIntegrityError(f"a {stream.value} entry is not a {kind.__name__}")
        low, high = entry.effective_from, entry.effective_until
        if point:
            if low is not None:
                raise CatalogIntegrityError(f"{stream.value}: a point entry has a span")
            yield entry.episode.observation_key(), _NO_SPAN, _NO_SPAN, stream, entry
            continue
        if low is None or high is None:
            raise CatalogIntegrityError(f"{stream.value}: an interval entry is open")
        if low < _required(pit.simulation_start) or high > _required(pit.simulation_end):
            raise CatalogIntegrityError(f"{stream.value}: an entry leaves the simulation")
        yield entry.episode.observation_key(), low, high, stream, entry


def _merged_entries(
    members: Iterator[tuple[str, datetime, datetime, EvidenceStream, Contract]],
    exclusions: Iterator[tuple[str, datetime, datetime, EvidenceStream, Contract]],
) -> Iterator[tuple[str, datetime, datetime, EvidenceStream, Contract]]:
    """Two-pointer merge by ``(observation_key, start)``; ties keep both (the caller rejects)."""
    a, b = next(members, None), next(exclusions, None)
    while a is not None or b is not None:
        if b is None or (a is not None and (a[0], a[1]) <= (b[0], b[1])):
            assert a is not None
            yield a
            a = next(members, None)
        else:
            yield b
            b = next(exclusions, None)


def _listing_gap(item: object) -> tuple[str, str] | None:
    if item is None:
        return None
    if (
        not isinstance(item, tuple)
        or len(item) != 2
        or not all(isinstance(part, str) and part for part in item)
    ):
        raise CatalogIntegrityError(f"a listing evidence gap is malformed: {item!r}")
    return item[0], item[1]


class _PartitionReports:
    """The (symbol, day) reports of one member symbol, emitted in strictly increasing day.

    Window days are emitted in order; a retained revision's event day is merged in when its row
    arrives. A day outside the window arriving after a later day was emitted cannot be placed in
    order without a set of emitted days, so it fails closed (ADR-0077 §2, "prove monotone or fail
    closed").
    """

    def __init__(self, derivation: _EvidenceDerivation, venue_symbol: str) -> None:
        request = derivation.request
        self._derivation = derivation
        self._symbol = venue_symbol
        self._first = request.start.astimezone(UTC).date()
        self._last = _last_window_day(request.start, request.end)
        self._next = self._first
        self._emitted: date | None = None
        self._cached: tuple[date, str] | None = None

    def cover(self, day: date) -> str:
        """Emit what must precede ``day`` and ``day`` itself; the id of ``day``'s report."""
        while self._next <= self._last and self._next < day:
            self._emit(self._next)
            self._next += _DAY
        if self._emitted is None or day > self._emitted:
            if day == self._next:
                self._next += _DAY
            return self._emit(day)
        if day < self._emitted and not self._first <= day <= self._last:
            raise CatalogIntegrityError(
                f"{self._symbol}: event day {day.isoformat()} outside the window arrives after "
                f"{self._emitted.isoformat()}: the report order cannot be proven"
            )
        return self._report(day)

    def close(self) -> None:
        while self._next <= self._last:
            self._emit(self._next)
            self._next += _DAY

    def _emit(self, day: date) -> str:
        report_id = self._report(day)
        self._derivation.report(
            DatasetQualityReportRef(
                report_id=report_id,
                subject=DatasetQualitySubject.SYMBOL_DAY,
                symbol=self._symbol,
                day=day,
            )
        )
        self._emitted = day
        return report_id

    def _report(self, day: date) -> str:
        if self._cached is None or self._cached[0] != day:
            self._cached = (day, self._derivation.quality.partition_report(self._symbol, day))
        return self._cached[1]


class _EvidenceDerivation:
    """One pass of the v3 generator: rows and five evidence streams, in canonical order."""

    def __init__(
        self,
        request: DatasetEvidenceRequest,
        canonical: str,
        selection_id: str,
        rule: DatasetEvidenceRule,
        sources: DatasetEvidenceSources,
        sink: DatasetDerivationSink,
        schema_version: str,
    ) -> None:
        if not isinstance(sources, DatasetEvidenceSources):
            raise DatasetSpecError("sources must be DatasetEvidenceSources")
        if any(
            not callable(getattr(sources.quality, name, None))
            for name in ("claim_gap", "finish_reports", "close")
        ):
            raise DatasetSpecError(
                "Dataset v3 requires the bounded ADR-0093 Quality source; legacy inline-report "
                "sources are unsupported"
            )
        self.request = request
        self.quality = sources.quality
        self._canonical = canonical
        self._selection_id = selection_id
        self._chunk_rows = rule.chunk_rows
        self._universe = sources.universe
        self._pit = sources.pit
        self._sink = sink
        self._bound = request.pit.snapshot_bindings
        self._point = request.pit.simulation_time is not None
        self._streams = _derived_streams(schema_version)
        self._counts: dict[EvidenceStream, int] = dict.fromkeys(self._streams, 0)
        self._rows = 0
        self._last_report: tuple[int, str, str] | None = None

    def run(self) -> DatasetDerivation:
        try:
            return self._run()
        finally:
            close = getattr(self.quality, "close", None)
            if callable(close):
                close()

    def _run(self) -> DatasetDerivation:
        listing = self.quality.listing_report()
        self.report(
            DatasetQualityReportRef(report_id=listing, subject=DatasetQualitySubject.LISTING)
        )
        self._entries()
        self._listing_lineage(listing)
        with self._universe.member_spans() as spans:
            members = _MemberSpans(spans, self.request.pit)
            for venue_symbol in self.request.universe.symbols:
                self._symbol(venue_symbol, members.take(venue_symbol))
            members.close()
        finish_reports = getattr(self.quality, "finish_reports", None)
        if callable(finish_reports):
            finish_reports()
        return DatasetDerivation(
            selection_id=self._selection_id,
            row_count=self._rows,
            record_counts=tuple(
                (stream, self._counts[stream])
                for stream in sorted(self._streams, key=lambda item: item.value)
            ),
        )

    # ------------------------------------------------------------------ emission

    def _emit(self, stream: EvidenceStream, record: Contract) -> None:
        self._sink.evidence(stream, record)
        self._counts[stream] += 1

    def report(self, record: DatasetQualityReportRef) -> None:
        key = record.sort_key()
        if self._last_report is not None and key <= self._last_report:
            raise CatalogIntegrityError(f"quality report {record.report_id} is out of order")
        self._last_report = key
        self._emit(EvidenceStream.QUALITY_REPORTS, record)

    def _check_lineage_tables(self, lineage: SelectedRevisionLineage) -> None:
        for table in (lineage.canonical_table, lineage.raw_table, lineage.source_table):
            if table not in self._bound:
                raise CatalogIntegrityError(
                    f"lineage cites {table}, which the PIT spec does not bind"
                )

    # ------------------------------------------------------------------ universe

    def _entries(self) -> None:
        """Members and exclusions merged by ``(observation_key, effective_from)``: each stream
        in canonical order, no episode both member and excluded (or overlapping) at one time."""
        with self._universe.members() as members, self._universe.exclusions() as exclusions:
            ordered = _merged_entries(
                _entry_items(members, EvidenceStream.MEMBERS, self.request.pit),
                _entry_items(exclusions, EvidenceStream.EXCLUSIONS, self.request.pit),
            )
            previous: tuple[str, datetime, datetime, EvidenceStream] | None = None
            for key, start, end, stream, entry in ordered:
                if previous is not None:
                    prev_key, prev_start, prev_end, prev_stream = previous
                    if key == prev_key and (self._point or prev_end > start):
                        if prev_stream is not stream:
                            raise CatalogIntegrityError(
                                f"episode {key} is both a member and excluded at one time"
                            )
                        raise CatalogIntegrityError(f"episode {key}: {stream.value} overlap")
                    if (key, start) <= (prev_key, prev_start):
                        raise CatalogIntegrityError(
                            f"{stream.value} of {key} are out of canonical order (ADR-0077 §2)"
                        )
                previous = (key, start, end, stream)
                self._emit(stream, entry)

    def _listing_lineage(self, listing_report: str) -> None:
        """Listing lineage by ``canonical_revision_id``; each listing gap bound to the listing
        report, merged in from the gap cursor (same order; a gap without lineage fails)."""
        with (
            self._universe.listing_lineage() as lineages,
            self._universe.evidence_gaps() as gaps,
        ):
            pending = _listing_gap(next(gaps, None))
            previous: str | None = None
            for lineage in lineages:
                if not isinstance(lineage, SelectedRevisionLineage):
                    raise CatalogIntegrityError("a listing lineage item is malformed")
                revision = lineage.canonical_revision_id
                if lineage.canonical_table != _LISTINGS:
                    raise CatalogIntegrityError(f"listing lineage {revision} is not of {_LISTINGS}")
                if previous is not None and revision <= previous:
                    raise CatalogIntegrityError(
                        f"listing lineage {revision} is duplicated or out of canonical order "
                        "(ADR-0077 §2: by canonical_revision_id)"
                    )
                previous = revision
                self._check_lineage_tables(lineage)
                self._emit(EvidenceStream.LINEAGE, lineage)
                if pending is not None and pending[0] == revision:
                    self._gap(listing_report, _LISTINGS, revision, pending[1])
                    pending = _listing_gap(next(gaps, None))
            if pending is not None:
                raise CatalogIntegrityError(
                    f"listing evidence gap of {pending[0]} has no listing lineage in its order"
                )

    def _gap(self, report_id: str, table: str, revision: str, gap: str) -> None:
        self.quality.claim_gap(report_id, table, revision, gap)
        self._emit(
            EvidenceStream.EVIDENCE_GAPS,
            AvailabilityEvidenceGap(
                table=table, revision_id=revision, quality_report_id=report_id, gap=gap
            ),
        )

    # ------------------------------------------------------------------ data

    def _symbol(self, venue_symbol: str, member_spans: tuple[MemberSpan, ...]) -> None:
        if not member_spans:
            return  # not a member at any time: no rows and no partition reports (as v2)
        request = self.request
        reports = _PartitionReports(self, venue_symbol)
        for low, high in _iter_slices(request.data_type, request.start, request.end):
            with self._pit.keys(
                request.pit,
                request.data_type,
                venue_symbol,
                low,
                high,
                conflict_sink=self._pit_conflict_head,
            ) as groups:
                previous: str | None = None
                for group in groups:
                    self._key(group, venue_symbol, member_spans, low, high, previous, reports)
                    previous = group.observation_key
        reports.close()

    def _key(
        self,
        group: PitKeyGroup,
        venue_symbol: str,
        member_spans: tuple[MemberSpan, ...],
        low: datetime,
        high: datetime,
        previous: str | None,
        reports: _PartitionReports,
    ) -> None:
        if not isinstance(group, PitKeyGroup):
            raise CatalogIntegrityError("a PIT key group is not a PitKeyGroup")
        key = group.observation_key
        if not isinstance(key, str) or not key:
            raise CatalogIntegrityError("a PIT key group has no observation key")
        if previous is not None and key <= previous:
            raise CatalogIntegrityError(
                f"observation key {key} is duplicated or out of order in its slice"
            )
        owner = _check_utc(group.owner_event_time, f"key {key} owner_event_time")
        if not low <= owner < high:
            raise CatalogIntegrityError(
                f"observation key {key} is not owned by the slice [{low.isoformat()}, "
                f"{high.isoformat()}) (its chain starts at {owner.isoformat()})"
            )
        # PIT's bounded selector evaluates one fixed graph while candidates only accrue. A
        # selected revision therefore remains the sole head until a newly available descendant
        # replaces it; it cannot become the sole head again, and a conflict terminates the key.
        # `_evaluate_bounded` emits only selection changes. `_selected_spans_of` preserves that
        # order, and `_gated_rows` only intersects it with ordered disjoint member spans. One
        # selected revision may thus produce several rows when a member span is split, but those
        # occurrences are adjacent in output. This scalar covers repeated evaluation/span
        # intersections without retaining O(H_key) revision IDs.
        last_lineage_revision: str | None = None
        rows = _gated_rows(
            _selected_spans_of(
                group.evaluations,
                self.request.pit,
                key,
                on_conflict=lambda evaluation: self._finish_pit_conflict(key, evaluation),
            ),
            iter(member_spans),
        )
        for (effective_from, effective_until), selected in rows:
            event_time = self._row(venue_symbol, key, selected, effective_from, effective_until)
            report_id = reports.cover(event_time.astimezone(UTC).date())
            if selected.revision_id != last_lineage_revision:
                self._data_lineage(selected, report_id)
                last_lineage_revision = selected.revision_id

    def _pit_conflict_head(self, record: PitConflictHeadEvidence) -> None:
        if EvidenceStream.PIT_CONFLICTS not in self._counts:
            raise CatalogIntegrityError(
                "a PIT conflict cannot be replayed in a manifest version before 2.5.0"
            )
        self._sink.evidence(EvidenceStream.PIT_CONFLICTS, record)
        self._counts[EvidenceStream.PIT_CONFLICTS] += 1

    def _finish_pit_conflict(
        self, key: str, evaluation: PitKeyEvaluation
    ) -> PitConflictResult | None:
        finish = getattr(self._sink, "finish_pit_conflict", None)
        if not callable(finish):
            return None
        return cast(PitConflictResult, finish(key, evaluation, self.request.pit))

    def _row(
        self,
        venue_symbol: str,
        key: str,
        selected: PitSelectedRevision,
        effective_from: datetime | None,
        effective_until: datetime | None,
    ) -> datetime:
        if not isinstance(selected, PitSelectedRevision) or not selected.revision_id:
            raise CatalogIntegrityError(f"key {key}: a selected revision is malformed")
        event_time = _check_utc(selected.event_time, f"revision {selected.revision_id} event")
        ordinal = self._rows
        self._sink.row(
            {
                "selection_id": self._selection_id,
                "canonical_table": self._canonical,
                "symbol": rules.SYMBOLS[venue_symbol].symbol,
                "observation_key": key,
                "revision_id": selected.revision_id,
                "event_time": event_time,
                "effective_from": effective_from,
                "effective_until": effective_until,
                "chunk_index": ordinal // self._chunk_rows,
                "row_ordinal": ordinal,
            }
        )
        self._rows += 1
        return event_time

    def _data_lineage(self, selected: PitSelectedRevision, report_id: str) -> None:
        lineage = selected.lineage
        if (
            not isinstance(lineage, SelectedRevisionLineage)
            or lineage.canonical_table != self._canonical
            or lineage.canonical_revision_id != selected.revision_id
        ):
            raise CatalogIntegrityError(f"selected revision {selected.revision_id} has no lineage")
        self._check_lineage_tables(lineage)
        self._emit(EvidenceStream.LINEAGE, lineage)
        if selected.evidence_gap is not None:
            self._gap(report_id, self._canonical, selected.revision_id, selected.evidence_gap)


class _EvidenceBuildSink:
    """``build``'s sink: evidence trees + fixed-size chunks handed to the chunk writer."""

    def __init__(
        self,
        storage: StorageAdapter,
        rule: DatasetEvidenceRule,
        selection_id: str,
        chunks: DatasetChunkWriter,
        version: str,
    ) -> None:
        self._writers = {
            stream: EvidenceTreeWriter(storage, stream, limits=rule.limits, schema_version=version)
            for stream in (*_derived_streams(version), EvidenceStream.CHUNK_PROOFS)
        }
        self._chunk_rows = rule.chunk_rows
        self._selection_id = selection_id
        self._chunks = chunks
        self._rows: list[Mapping[str, Any]] = []
        self._chunk_count = 0
        self._replayed = 0
        self._snapshot_id: str | None = None

    def evidence(self, stream: EvidenceStream, record: Contract) -> None:
        if stream is EvidenceStream.CHUNK_PROOFS:
            raise CatalogIntegrityError("chunk proofs come from chunk commits only")
        self._writers[stream].append(record)

    def row(self, row: Mapping[str, Any]) -> None:
        self._rows.append(row)
        if len(self._rows) == self._chunk_rows:
            self._commit()

    def finish(self) -> tuple[tuple[EvidenceStreamRef, ...], int, int, str]:
        if self._rows:
            self._commit()
        if self._snapshot_id is None:  # pragma: no cover - build refuses an empty selection first
            raise DatasetEmpty("no chunk was committed")
        streams = tuple(
            self._writers[stream].finish()
            for stream in sorted(self._writers, key=lambda item: item.value)
        )
        return streams, self._chunk_count, self._replayed, self._snapshot_id

    def finish_pit_conflict(
        self, key: str, evaluation: PitKeyEvaluation, pit: PointInTimeSpec
    ) -> PitConflictResult:
        writer = self._writers[EvidenceStream.PIT_CONFLICTS]
        if evaluation.head_count is None or evaluation.head_count < 2:
            raise CatalogIntegrityError(f"key {key}: conflict result has no complete head count")
        if writer.record_count != evaluation.head_count:
            raise CatalogIntegrityError(
                f"key {key}: wrote {writer.record_count} conflict heads, expected "
                f"{evaluation.head_count}"
            )
        ref = writer.finish()
        return PitConflictResult(
            rule_id=PIT_BINDING.policy_id,
            rule_version=PIT_BINDING.version,
            rule_hash=PIT_BINDING.policy_hash,
            observation_key=key,
            simulation_time=evaluation.simulation_time,
            knowledge_cutoff=pit.knowledge_cutoff,
            head_count=evaluation.head_count,
            evidence=ref,
        )

    def _commit(self) -> None:
        index = self._chunk_count
        first = index * self._chunk_rows
        rows = tuple(self._rows)
        self._rows = []
        for offset, row in enumerate(rows):
            if (row["chunk_index"], row["row_ordinal"]) != (index, first + offset):
                raise CatalogIntegrityError(f"chunk {index} rows are not contiguous")
        committed = self._chunks.commit_chunk(self._selection_id, index, rows)
        proof = committed.proof
        if not isinstance(proof, DatasetChunkProof) or (
            proof.chunk_index,
            proof.batch_id,
            proof.first_row_ordinal,
            proof.row_count,
        ) != (index, dataset_chunk_batch_id(self._selection_id, index), first, len(rows)):
            raise CatalogIntegrityError(f"chunk {index} of {self._selection_id} proves other rows")
        self._writers[EvidenceStream.CHUNK_PROOFS].append(proof)
        self._chunk_count += 1
        self._replayed += int(committed.replayed)
        self._snapshot_id = proof.snapshot_id

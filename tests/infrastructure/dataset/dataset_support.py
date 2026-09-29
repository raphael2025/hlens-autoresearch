"""Builders for the F2 / F3 tests: one catalog, real stores, real listing history, real reports.

A ``World`` joins the D3E harness (archives, REST pages, normalizer, reconciler over the mock
venue) and the E2 harness (exchangeInfo snapshots, listing derivation) on **one** catalog and
warehouse. ``DatasetBuilder`` writes to the production Research Dataset table
``research.dataset_selections`` (DS-1, ADR-0033), created like every other Phase 1 table by
``ensure_phase1_tables``. Nothing under test is faked: only transports and clocks are injected.
"""

from __future__ import annotations

import hashlib
from collections import defaultdict
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from dataclasses import field as dataclass_field
from datetime import date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Final, cast

from core.contracts.revision import PointInTimeSpec, PointInTimeStatus
from core.contracts.storage import ObjectRef, StorageAdapter
from core.contracts.universe import (
    DATASET_EVIDENCE_FORMAT,
    DatasetChunkProof,
    DegradedEpisodeKey,
    EpisodeIdentityBasis,
    EvidenceObjectRef,
    EvidenceStream,
    EvidenceStreamRef,
    ExclusionReason,
    ResearchDatasetEvidenceManifest,
    SelectedRevisionLineage,
    UniverseExclusion,
    UniverseMember,
    UniverseSelectionSpec,
    dataset_chunk_batch_id,
)
from core.domain.base import Contract, canonical_json
from core.domain.specs import InstrumentType
from infrastructure.canonical import listing_rules as lr
from infrastructure.canonical import rules
from infrastructure.catalog.definitions import TableDefinitionRegistry
from infrastructure.catalog.iceberg_adapter import CatalogIntegrityError
from infrastructure.catalog.phase1_tables import (
    DATASET_MANIFESTS,
    DATASET_SELECTIONS,
    PHASE1_TABLES,
)
from infrastructure.dataset.builder import (
    ChunkCommitted,
    DatasetBuilder,
    DatasetEvidenceRequest,
    DatasetEvidenceSources,
    MemberSpan,
    PitKeyEvaluation,
    PitKeyGroup,
    PitSelectedRevision,
)
from infrastructure.dataset.evidence import publish_evidence_object
from infrastructure.pit.runs import RunLimits
from infrastructure.pit.selector import PIT_BINDING
from infrastructure.quality.listing_report import ListingQualityReporter
from infrastructure.quality.reporter import QualityReporter
from infrastructure.revision.channel_precedence import DELIVERY_CHANNEL_BINDING
from infrastructure.revision.exchange_info_availability import EXCHANGE_INFO_AVAILABILITY_BINDING
from infrastructure.revision.store import RevisionCatalog
from infrastructure.settings import local_file_uri_to_path
from infrastructure.storage import LocalFileStorageAdapter
from infrastructure.universe.builder import FIRST_SLICE_UNIVERSE, UniverseBuilder
from infrastructure.universe.run_params import UniverseRunParams
from tests.infrastructure.canonical import canonical_support as c
from tests.infrastructure.catalog.catalog_support import (
    PostgresCatalogHarness,
    SqliteCatalogHarness,
)
from tests.infrastructure.collector import rest_support as cs
from tests.infrastructure.collector.rest_support import FakeTime, RestVenue
from tests.infrastructure.revision import exchange_info_support as xs
from tests.infrastructure.revision import rest_store_support as ss
from tests.infrastructure.revision.rest_store_support import (
    DAY,
    SYMBOL,
    RestHarness,
    StepClock,
    utc,
)

ORIGIN: Final = xs.ORIGIN
REGISTRY: Final = TableDefinitionRegistry(PHASE1_TABLES)
UNIVERSE_RUN_PARAMS: Final = UniverseRunParams(
    capacity=2,
    merge_fanout=3,
    limits=RunLimits(leaf_max_records=8, leaf_max_bytes=4096, fanout=3),
)

__all__ = [
    "DAY",
    "END",
    "ETH_HALT",
    "FakeChunkWriter",
    "FakeManifests",
    "FakePit",
    "FakeQuality",
    "FakeUniverse",
    "GAP_TEXT",
    "L3",
    "LocalFileStorageAdapter",
    "LookupLies",
    "ORIGIN",
    "RecordingSink",
    "SIM",
    "START",
    "TRADING",
    "V3_BINDINGS",
    "V3_CHUNK_TABLE",
    "V3_EVENT",
    "V3_LISTINGS",
    "V3_LISTING_REPORT",
    "V3_SLICE_21",
    "V3_SLICE_22",
    "V3_TRADES",
    "World",
    "episode_of",
    "evidence_index",
    "evidence_leaf",
    "evidence_object_bytes",
    "evidence_object_path",
    "evidence_storage",
    "fake_heads",
    "listing_lineage",
    "partition_report",
    "point_key",
    "point_scenario",
    "postgres_world",
    "publish_bytes",
    "selected",
    "sqlite_world",
    "stream_ref",
    "symbol_of",
    "trade_lineage",
    "utc",
    "v3_episode",
    "v3_exclusion",
    "v3_member",
    "v3_pit",
    "v3_request",
    "v3_sources",
]

#: Listing observations (exchangeInfo retrieved_at) before the market data of 2023-11-14.
L1: Final = utc(2023, 11, 10)
L2: Final = utc(2023, 11, 12)
L3: Final = utc(2023, 11, 13)
LISTING_CLOCK: Final = utc(2023, 11, 25)
#: Raw / Canonical / edge knowledge (as in the F1 tests) and the reports' clock.
K_A, K_R, N_A, N_R, K_E = (utc(2023, 12, d) for d in (1, 5, 6, 7, 10))
K_Q: Final = utc(2023, 12, 15)
SIM: Final = utc(2023, 12, 20)
#: The event window: two agg_trades hour slices around the 22:14 trades of DAY.
START, END = utc(2023, 11, 14, 21), utc(2023, 11, 14, 23)
TRADING: Final = xs.TRADING
ETH_HALT: Final = {"BTCUSDT": "TRADING", "ETHUSDT": "HALT"}


def episode_of(entry: UniverseMember | UniverseExclusion) -> DegradedEpisodeKey:
    """The first slice has no stable product id: every episode is a degraded key (ADR-0029)."""
    episode = entry.episode
    assert isinstance(episode, DegradedEpisodeKey)
    return episode


def symbol_of(entry: UniverseMember | UniverseExclusion) -> str:
    return episode_of(entry).symbol


@dataclass
class World:
    h: RestHarness
    x: xs.Harness

    # ------------------------------------------------------------------ listings

    def observe(self, request_id: str, statuses: Mapping[str, str | None], at: datetime) -> None:
        self.x.observe(request_id, statuses, at, server_time=int(at.timestamp() * 1000))

    def derive(self) -> Any:
        deriver = self.x.deriver(self.h.adapter)
        try:
            return deriver.derive()
        finally:
            deriver.close()

    def listed(self, statuses: Mapping[str, str | None] = TRADING, at: datetime = L1) -> None:
        self.observe(f"snap-{at.isoformat()}", statuses, at)
        self.derive()

    # ------------------------------------------------------------------ market data

    def trades(self, *, count: int = 3, reconcile: bool = True) -> None:
        """Archive + REST copies of ``count`` BTCUSDT trades at 22:14 of DAY (F1 fixture)."""
        items = ss.agg_items(count)
        archive = c.ingest_archive(self.h, "agg_trades", ss.archive_agg_lines(items), knowledge=K_A)
        [response] = c.ingest_rest(self.h, "agg_trades", items, knowledge=K_R)
        c.normalizer(self.h, clock=StepClock(start=N_A)).normalize_unit(
            c.ARCHIVE_AGGS.table, archive
        )
        c.normalizer(self.h, clock=StepClock(start=N_R)).normalize_unit(c.REST_AGGS.table, response)
        if reconcile:
            self.h.reconciler(clock=StepClock(start=K_E)).reconcile("agg_trades", SYMBOL, DAY)

    def more_trades(self) -> None:
        """One more BTCUSDT trade (id 110) from a new REST page, normalized: heads move."""
        [later] = c.ingest_rest(
            self.h,
            "agg_trades",
            ss.agg_items(1, first_id=110, first_ms=ss.T0 + 10),
            knowledge=K_R,
            request_id="req-110",
        )
        c.normalizer(self.h, clock=StepClock(start=N_R)).normalize_unit(c.REST_AGGS.table, later)

    def bars(self, *, count: int = 3) -> None:
        items = ss.kline_items(count)
        archive = c.ingest_archive(
            self.h, "klines_1m", ss.archive_kline_lines(items), knowledge=K_A
        )
        [response] = c.ingest_rest(self.h, "klines_1m", items, knowledge=K_R)
        c.normalizer(self.h, clock=StepClock(start=N_A)).normalize_unit(
            c.ARCHIVE_KLINES.table, archive
        )
        c.normalizer(self.h, clock=StepClock(start=N_R)).normalize_unit(
            c.REST_KLINES.table, response
        )
        self.h.reconciler(clock=StepClock(start=K_E)).reconcile("klines_1m", SYMBOL, DAY)

    # ------------------------------------------------------------------ reports and specs

    def report(
        self,
        data_type: str = "agg_trades",
        symbols: tuple[str, ...] = ("BTCUSDT", "ETHUSDT"),
        days: tuple[date, ...] = (DAY,),
        *,
        listing: bool = True,
    ) -> list[str]:
        ids = []
        reporter = QualityReporter(
            self.h.adapter,
            self.h.storage,
            canonical_scratch_directory=self.h.canonical_scratch_directory,
            clock=StepClock(start=K_Q),
        )
        for symbol in symbols:
            for day in days:
                ids.append(reporter.report(data_type, symbol, day).report_id)
        if listing:
            ids.append(
                ListingQualityReporter(
                    self.h.adapter,
                    self.h.storage,
                    market_data_base_url=ORIGIN,
                    clock=StepClock(start=K_Q),
                )
                .report()
                .report_id
            )
        return ids

    def bindings(self, *, skip: tuple[str, ...] = ()) -> dict[str, str]:
        """Current heads of every Phase 1 input table with a snapshot (not the manifests)."""
        return {
            table.table: head
            for table in PHASE1_TABLES
            if table.table not in (DATASET_MANIFESTS.table, *skip)
            and (head := self.h.head(table.table)) is not None
        }

    def spec(
        self,
        *,
        at: datetime = SIM,
        cutoff: datetime = SIM,
        interval: tuple[datetime, datetime] | None = None,
        skip: tuple[str, ...] = (),
        **overrides: Any,
    ) -> PointInTimeSpec:
        fields: dict[str, Any] = {
            "name": "test.dataset",
            "version": "1.0.0",
            "knowledge_cutoff": cutoff,
            "snapshot_bindings": self.bindings(skip=skip),
            "point_in_time_binding": PIT_BINDING,
            "availability_bindings": (
                rules.AVAILABILITY_BINDING,
                EXCHANGE_INFO_AVAILABILITY_BINDING,
            ),
            "precedence_bindings": (
                DELIVERY_CHANNEL_BINDING,
                rules.PRECEDENCE_MAP_BINDING,
                lr.LISTING_OBSERVATION_BINDING,
            ),
            "parser_bindings": (rules.NORMALIZER_BINDING, lr.LISTING_STATUS_BINDING),
        }
        if interval is None:
            fields["simulation_time"] = at
        else:
            fields["simulation_start"], fields["simulation_end"] = interval
        fields.update(overrides)
        return PointInTimeSpec(**fields)

    def builder(self) -> DatasetBuilder:
        return DatasetBuilder(
            self.h.adapter,
            self.h.storage,
            canonical_scratch_directory=self.h.canonical_scratch_directory,
            market_data_base_url=ORIGIN,
            dataset_table=DATASET_SELECTIONS,
        )

    def universe(self) -> UniverseBuilder:
        return UniverseBuilder(self.h.adapter, self.h.storage, market_data_base_url=ORIGIN)


@contextmanager
def sqlite_world(tmp_path: Path) -> Iterator[World]:
    tmp_path.mkdir(parents=True, exist_ok=True)
    with _world(tmp_path, SqliteCatalogHarness(tmp_path, REGISTRY)) as world:
        yield world


@contextmanager
def postgres_world(tmp_path: Path, uri: str) -> Iterator[World]:
    tmp_path.mkdir(parents=True, exist_ok=True)
    with _world(tmp_path, PostgresCatalogHarness(tmp_path, REGISTRY, uri=uri)) as world:
        yield world


@contextmanager
def _world(
    tmp_path: Path, catalog: SqliteCatalogHarness | PostgresCatalogHarness
) -> Iterator[World]:
    with ss._harness(tmp_path, catalog) as h:
        x = xs.Harness(
            tmp_path=tmp_path,
            catalog=cast(SqliteCatalogHarness, catalog),
            adapter=h.adapter,
            storage=h.storage,
            venue=RestVenue(origin=ORIGIN),
            wire_clock=xs.SetClock(),
            clock=xs.StepClock(start=LISTING_CLOCK, step=timedelta(seconds=1)),
            time=FakeTime(),
        )
        yield World(h, x)


# =========================================================================================
# v3 (ADR-0077) helpers: evidence trees and the bounded dataset builder
#
# The v3 builder reads its inputs through ordered Protocols (B-UNIV / B-PIT implement the real
# generators). Until those land, the builder is exercised against the in-memory fakes below;
# evidence objects go to a real ``LocalFileStorageAdapter``. Nothing here fixes a DQ-9 value:
# every test passes its own (arbitrary, small) limits.
# =========================================================================================

V3_TRADES: Final = "canonical.trades"
V3_LISTINGS: Final = "canonical.instrument_listings"
V3_CHUNK_TABLE: Final = "research.dataset_selection_chunks"
V3_LISTING_REPORT: Final = "report-listing"
V3_TRADABLE_FROM: Final = utc(2023, 1, 1)
V3_EVENT: Final = utc(2023, 11, 14, 22, 14)
V3_SLICE_21, V3_SLICE_22 = utc(2023, 11, 14, 21), utc(2023, 11, 14, 22)
#: Every table a v3 test spec binds (fake snapshot ids; nothing is read from them).
V3_BINDINGS: Final[Mapping[str, str]] = {
    V3_TRADES: "1",
    "canonical.bars_1m": "1",
    V3_LISTINGS: "1",
    "quality.data_quality_reports": "1",
    "quality.availability_evidence_gaps": "1",
    "raw.binance_spot_exchange_info": "1",
    "raw.binance_spot_agg_trades": "1",
    "raw.binance_spot_archives": "1",
    "raw.binance_spot_precedence_evidence": "1",
}


def evidence_storage(tmp_path: Path) -> LocalFileStorageAdapter:
    return cs.make_storage(tmp_path, "evidence-warehouse")


def evidence_object_path(storage: LocalFileStorageAdapter, key: str) -> Path:
    return local_file_uri_to_path(storage.warehouse_uri, field_name="warehouse_uri") / key


def evidence_object_bytes(storage: StorageAdapter, key: str) -> bytes:
    ref = storage.lookup(key)
    assert ref is not None, key
    with storage.open_read(ref) as handle:
        data: bytes = handle.read()
    return data


# ------------------------------------------------------------------ hand-built evidence objects


def evidence_line(document: Mapping[str, Any]) -> bytes:
    return (canonical_json(dict(document)) + "\n").encode("utf-8")


def evidence_leaf(stream: EvidenceStream, first: int, records: Sequence[bytes]) -> bytes:
    header = {
        "first_ordinal": first,
        "format": DATASET_EVIDENCE_FORMAT,
        "node": "leaf",
        "record_count": len(records),
        "stream": stream.value,
    }
    return evidence_line(header) + b"".join(records)


def evidence_index(
    stream: EvidenceStream,
    level: int,
    children: Sequence[tuple[EvidenceObjectRef, int, int]],
    *,
    first: int | None = None,
    count: int | None = None,
) -> bytes:
    """An index object over ``(ref, first_ordinal, record_count)`` children."""
    header = {
        "first_ordinal": (children[0][1] if children else 0) if first is None else first,
        "format": DATASET_EVIDENCE_FORMAT,
        "level": level,
        "node": "index",
        "record_count": sum(item[2] for item in children) if count is None else count,
        "stream": stream.value,
    }
    lines = [
        evidence_line(
            {
                "first_ordinal": first_ordinal,
                "key": ref.key,
                "record_count": record_count,
                "sha256": ref.sha256,
                "size": ref.size,
            }
        )
        for ref, first_ordinal, record_count in children
    ]
    return evidence_line(header) + b"".join(lines)


def publish_bytes(storage: StorageAdapter, data: bytes) -> EvidenceObjectRef:
    return publish_evidence_object(storage, data)


def stream_ref(
    stream: EvidenceStream,
    root: EvidenceObjectRef,
    *,
    record_count: int,
    leaf_count: int,
    depth: int,
) -> EvidenceStreamRef:
    return EvidenceStreamRef(
        stream=stream,
        format=DATASET_EVIDENCE_FORMAT,
        record_count=record_count,
        leaf_count=leaf_count,
        depth=depth,
        root=root,
    )


@dataclass
class LookupLies:
    """A storage whose ``lookup`` reports another size than the object has (§6.2.4)."""

    inner: StorageAdapter

    def stage(self, request: Any, content: Any) -> Any:
        return self.inner.stage(request, content)

    def publish(self, staged: Any) -> Any:
        return self.inner.publish(staged)

    def lookup(self, key: str) -> ObjectRef | None:
        found = self.inner.lookup(key)
        return None if found is None else found.model_copy(update={"size": found.size + 1})

    def open_read(self, ref: ObjectRef) -> Any:
        return self.inner.open_read(ref)


# ------------------------------------------------------------------ v3 request and inputs


def v3_pit(
    *,
    at: datetime | None = SIM,
    interval: tuple[datetime, datetime] | None = None,
    skip: tuple[str, ...] = (),
) -> PointInTimeSpec:
    fields: dict[str, Any] = {
        "name": "test.dataset-v3",
        "version": "1.0.0",
        "knowledge_cutoff": SIM,
        "snapshot_bindings": {
            table: head for table, head in V3_BINDINGS.items() if table not in skip
        },
        "point_in_time_binding": PIT_BINDING,
        "availability_bindings": (
            rules.AVAILABILITY_BINDING,
            EXCHANGE_INFO_AVAILABILITY_BINDING,
        ),
        "precedence_bindings": (
            DELIVERY_CHANNEL_BINDING,
            rules.PRECEDENCE_MAP_BINDING,
            lr.LISTING_OBSERVATION_BINDING,
        ),
        "parser_bindings": (rules.NORMALIZER_BINDING, lr.LISTING_STATUS_BINDING),
    }
    if interval is None:
        fields["simulation_time"] = at
    else:
        fields["simulation_start"], fields["simulation_end"] = interval
    return PointInTimeSpec(**fields)


def v3_request(
    pit: PointInTimeSpec | None = None,
    *,
    universe: UniverseSelectionSpec = FIRST_SLICE_UNIVERSE,
    data_type: str = "agg_trades",
    start: datetime = START,
    end: datetime = END,
) -> DatasetEvidenceRequest:
    return DatasetEvidenceRequest(
        universe=universe,
        pit=v3_pit() if pit is None else pit,
        data_type=data_type,
        start=start,
        end=end,
    )


def v3_episode(symbol: str) -> DegradedEpisodeKey:
    return DegradedEpisodeKey(
        basis=EpisodeIdentityBasis.DEGRADED_SYMBOL_START,
        venue="binance",
        instrument_type=InstrumentType.SPOT,
        symbol=symbol,
        tradable_from=V3_TRADABLE_FROM,
    )


def v3_member(symbol: str, revision: str, span: MemberSpan = (None, None)) -> UniverseMember:
    return UniverseMember(
        episode=v3_episode(symbol),
        listing_revision_id=revision,
        effective_from=span[0],
        effective_until=span[1],
    )


def v3_exclusion(symbol: str, revision: str, span: MemberSpan = (None, None)) -> UniverseExclusion:
    return UniverseExclusion(
        episode=v3_episode(symbol),
        listing_revision_id=revision,
        reason=ExclusionReason.NOT_TRADABLE,
        effective_from=span[0],
        effective_until=span[1],
    )


def listing_lineage(revision: str) -> SelectedRevisionLineage:
    return SelectedRevisionLineage(
        canonical_table=V3_LISTINGS,
        canonical_revision_id=revision,
        raw_table="raw.binance_spot_exchange_info",
        raw_revision_id=f"raw-{revision}",
        source_table="raw.binance_spot_exchange_info",
        source_revision_id=f"source-{revision}",
    )


def trade_lineage(revision: str, *, table: str = V3_TRADES) -> SelectedRevisionLineage:
    return SelectedRevisionLineage(
        canonical_table=table,
        canonical_revision_id=revision,
        raw_table="raw.binance_spot_agg_trades",
        raw_revision_id=f"raw-{revision}",
        source_table="raw.binance_spot_archives",
        source_revision_id=f"archive-{revision}",
    )


def selected(
    revision: str, *, gap: str | None = None, event: datetime = V3_EVENT
) -> PitSelectedRevision:
    return PitSelectedRevision(
        revision_id=revision, event_time=event, lineage=trade_lineage(revision), evidence_gap=gap
    )


def point_key(
    key: str,
    revision: str,
    *,
    gap: str | None = None,
    event: datetime = V3_EVENT,
    owner: datetime | None = None,
    status: PointInTimeStatus = PointInTimeStatus.SELECTED,
) -> PitKeyGroup:
    """One key of a point simulation at ``SIM``: one evaluation."""
    chosen = None
    if status is PointInTimeStatus.SELECTED:
        chosen = selected(revision, gap=gap, event=event)
    return PitKeyGroup(
        observation_key=key,
        owner_event_time=event if owner is None else owner,
        evaluations=(PitKeyEvaluation(simulation_time=SIM, status=status, selected=chosen),),
    )


def partition_report(symbol: str, day: date) -> str:
    return f"report-{symbol}-{day.isoformat()}"


@dataclass
class FakeUniverse:
    """``UniverseEvidenceSource`` over fixed streams (stand-in for ``UniverseSpanCursor``)."""

    members_: Sequence[UniverseMember]
    exclusions_: Sequence[UniverseExclusion]
    lineage: Sequence[SelectedRevisionLineage]
    gaps: Sequence[tuple[str, str]]
    spans: Sequence[tuple[str, datetime | None, datetime | None]]
    opened: int = 0
    open_now: int = 0

    @contextmanager
    def _cursor(self, values: Sequence[Any]) -> Iterator[Iterator[Any]]:
        self.opened += 1
        self.open_now += 1
        try:
            yield iter(values)
        finally:
            self.open_now -= 1

    def members(self) -> Any:
        return self._cursor(self.members_)

    def exclusions(self) -> Any:
        return self._cursor(self.exclusions_)

    def listing_lineage(self) -> Any:
        return self._cursor(self.lineage)

    def evidence_gaps(self) -> Any:
        return self._cursor(self.gaps)

    def member_spans(self) -> Any:
        return self._cursor(self.spans)


@dataclass
class FakePit:
    """``PitKeySource`` over fixed key groups per ``(venue symbol, slice start)``.

    Stand-in for B-PIT's generator."""

    groups: Mapping[tuple[str, datetime], Sequence[PitKeyGroup]]
    open_now: int = 0

    @contextmanager
    def keys(
        self,
        pit: PointInTimeSpec,
        data_type: str,
        venue_symbol: str,
        start: datetime,
        end: datetime,
    ) -> Iterator[Iterator[PitKeyGroup]]:
        self.open_now += 1
        try:
            yield iter(self.groups.get((venue_symbol, start), ()))
        finally:
            self.open_now -= 1


@dataclass
class FakeQuality:
    """``QualityEvidenceSource``: every partition has a report; gaps as given."""

    gaps: Mapping[tuple[str, str, str], str] = dataclass_field(default_factory=dict)
    asked: list[tuple[str, date]] = dataclass_field(default_factory=list)

    def listing_report(self) -> str:
        return V3_LISTING_REPORT

    def partition_report(self, venue_symbol: str, day: date) -> str:
        self.asked.append((venue_symbol, day))
        return partition_report(venue_symbol, day)

    def recorded_gap(self, report_id: str, table: str, revision_id: str) -> str | None:
        return self.gaps.get((report_id, table, revision_id))


def _jsonable(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        key: value.isoformat() if isinstance(value, datetime) else value
        for key, value in row.items()
    }


class FakeChunkWriter:
    """``DatasetChunkWriter`` in memory (B3 stand-in): idempotent per batch id, no holes."""

    def __init__(self, table: str = V3_CHUNK_TABLE) -> None:
        self._table = table
        self.batches: dict[str, tuple[tuple[dict[str, Any], ...], DatasetChunkProof]] = {}
        self.sealed: list[tuple[str, int]] = []
        self.lie: bool = False

    @property
    def table(self) -> str:
        return self._table

    def commit_chunk(
        self, selection_id: str, chunk_index: int, rows: Sequence[Mapping[str, Any]]
    ) -> ChunkCommitted:
        batch_id = dataset_chunk_batch_id(selection_id, chunk_index)
        content = tuple(dict(row) for row in rows)
        existing = self.batches.get(batch_id)
        if existing is not None:
            if existing[0] != content:
                raise CatalogIntegrityError(f"{batch_id} is committed with other rows")
            return ChunkCommitted(proof=existing[1], replayed=True)
        fingerprint = hashlib.sha256(
            canonical_json([_jsonable(row) for row in content]).encode("utf-8")
        ).hexdigest()
        proof = DatasetChunkProof(
            chunk_index=chunk_index,
            batch_id=batch_id,
            snapshot_id=f"snap-{len(self.batches) + 1}",
            first_row_ordinal=content[0]["row_ordinal"] + (1 if self.lie else 0),
            row_count=len(content),
            batch_fingerprint=fingerprint,
        )
        self.batches[batch_id] = (content, proof)
        return ChunkCommitted(proof=proof, replayed=False)

    def seal(self, selection_id: str, chunk_count: int) -> None:
        self.sealed.append((selection_id, chunk_count))
        prefix = f"{selection_id}.chunk-"
        found = sorted(batch for batch in self.batches if batch.startswith(prefix))
        if found != [dataset_chunk_batch_id(selection_id, i) for i in range(chunk_count)]:
            raise CatalogIntegrityError(f"{selection_id} chunks are not exactly 0..{chunk_count}")

    def rows(self, selection_id: str) -> list[dict[str, Any]]:
        prefix = f"{selection_id}.chunk-"
        return [
            row
            for batch in sorted(self.batches)
            if batch.startswith(prefix)
            for row in self.batches[batch][0]
        ]


class FakeManifests:
    """``EvidenceManifestStore`` in memory (B4 stand-in): content-hash idempotent."""

    def __init__(self) -> None:
        self.stored: dict[str, ResearchDatasetEvidenceManifest] = {}

    def recorded_version(self, selection_id: str) -> str | None:
        found = self.stored.get(selection_id)
        return None if found is None else found.schema_version

    def persist(self, manifest: ResearchDatasetEvidenceManifest) -> bool:
        existing = self.stored.get(manifest.selection_id)
        if existing is not None:
            if existing.content_hash() != manifest.content_hash():
                raise CatalogIntegrityError(f"{manifest.selection_id} has another manifest")
            return True
        self.stored[manifest.selection_id] = manifest
        return False


def fake_heads(heads: Mapping[str, str | None] | None = None) -> RevisionCatalog:
    """A catalog stand-in answering only ``load_table`` (the unbound-table check)."""
    known = dict(heads or {})

    def load_table(table: str) -> Any:
        head = known.get(table)
        return SimpleNamespace(
            current_snapshot=None if head is None else SimpleNamespace(snapshot_id=head)
        )

    return cast(RevisionCatalog, SimpleNamespace(load_table=load_table))


@dataclass
class RecordingSink:
    """A ``DatasetDerivationSink`` that keeps everything (tests only: it is O(N) on purpose)."""

    records: dict[EvidenceStream, list[Contract]] = dataclass_field(
        default_factory=lambda: defaultdict(list)
    )
    rows: list[dict[str, Any]] = dataclass_field(default_factory=list)

    def evidence(self, stream: EvidenceStream, record: Contract) -> None:
        self.records[stream].append(record)

    def row(self, row: Mapping[str, Any]) -> None:
        self.rows.append(dict(row))


GAP_TEXT: Final = "no archive evidence: available_time = ingest_time"


def point_scenario() -> tuple[FakeUniverse, FakePit, FakeQuality]:
    """BTCUSDT a member with three selected trades (``r2`` has a gap); ETHUSDT excluded."""
    universe = FakeUniverse(
        members_=(v3_member("BTCUSDT", "listing-btc"),),
        exclusions_=(v3_exclusion("ETHUSDT", "listing-eth"),),
        lineage=(listing_lineage("listing-btc"), listing_lineage("listing-eth")),
        gaps=(("listing-eth", GAP_TEXT),),
        spans=(("BTCUSDT", None, None),),
    )
    pit = FakePit(
        groups={
            ("BTCUSDT", V3_SLICE_22): (
                point_key("k1", "r1"),
                point_key("k2", "r2", gap=GAP_TEXT),
                point_key("k3", "r3"),
            )
        }
    )
    quality = FakeQuality(
        gaps={
            (V3_LISTING_REPORT, V3_LISTINGS, "listing-eth"): GAP_TEXT,
            (partition_report("BTCUSDT", DAY), V3_TRADES, "r2"): GAP_TEXT,
        }
    )
    return universe, pit, quality


def v3_sources(
    universe: FakeUniverse, pit: FakePit, quality: FakeQuality
) -> DatasetEvidenceSources:
    return DatasetEvidenceSources(universe=universe, pit=pit, quality=quality)

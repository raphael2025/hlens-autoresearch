"""Test-only rows and evolution targets for the seventeen Phase 1 production tables
(C3/D3B/E2/QG-1/DS-1).

Rows are built from **validated contract objects** (``RevisionRecord``, ``ListingRevision``,
``ResearchDatasetManifest``, ``CollectedObject`` …) so the tests show that the physical columns
carry the contracts, and ``*_from_row`` rebuilds them with the same content hash. These helpers
are not production codecs (Raw / Canonical writers belong to batches D ~ F).
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any, Final

import pyarrow as pa  # type: ignore[import-untyped]
from pyiceberg.partitioning import PartitionField, PartitionSpec
from pyiceberg.transforms import IdentityTransform, MonthTransform

from core.contracts.collector import CollectedObject
from core.contracts.revision import (
    AvailabilityDecision,
    ObservationTimes,
    PointInTimeSpec,
    PolicyBinding,
    PolicyRole,
    PrecedenceEvidence,
    RevisionGraph,
    RevisionRecord,
)
from core.contracts.storage import ObjectRef
from core.contracts.universe import (
    EpisodeIdentityBasis,
    ListingRevision,
    ListingStatus,
    ResearchDatasetManifest,
    SelectedRevisionLineage,
    StableEpisodeKey,
    TradableInterval,
    UniverseCandidateSource,
    UniverseMember,
    UniverseSelectionSpec,
)
from core.domain.base import CONTRACT_SCHEMA_VERSION, canonical_json
from core.domain.specs import DatasetRef, Instrument, InstrumentType, Zone
from infrastructure.catalog import (
    PHASE1_TABLES,
    RegisteredTableDefinition,
    TableDefinitionRegistry,
)
from infrastructure.catalog.phase1_tables import (
    BINANCE_SPOT_AGG_TRADES,
    BINANCE_SPOT_ARCHIVES,
    BINANCE_SPOT_EXCHANGE_INFO,
    BINANCE_SPOT_KLINES_1M,
    BINANCE_SPOT_PRECEDENCE_EVIDENCE,
    BINANCE_SPOT_REST_AGG_TRADES,
    BINANCE_SPOT_REST_KLINES_1M,
    BINANCE_SPOT_REST_RESPONSES,
    CANONICAL_BARS_1M,
    CANONICAL_INSTRUMENT_LISTINGS,
    CANONICAL_TRADES,
    DATA_QUALITY_REPORTS,
    DATASET_EVIDENCE_MANIFESTS,
    DATASET_MANIFESTS,
    DATASET_SELECTION_CHUNKS,
    DATASET_SELECTIONS,
    QUALITY_EVIDENCE_GAPS,
)
from infrastructure.revision import exchange_info_identity, rest_identity
from infrastructure.revision.channel_precedence import DELIVERY_CHANNEL_BINDING
from infrastructure.revision.exchange_info_availability import (
    ExchangeInfoAvailabilitySubject,
    decide_exchange_info_availability,
)
from infrastructure.revision.rest_availability import (
    RestAvailabilitySubject,
    decide_rest_availability,
)

T0: Final = datetime(2024, 12, 31, 23, 59, tzinfo=UTC)
US: Final = timedelta(microseconds=1)


def sha(label: str) -> str:
    return hashlib.sha256(label.encode("utf-8")).hexdigest()


def policy(role: PolicyRole, policy_id: str) -> PolicyBinding:
    return PolicyBinding(
        role=role, policy_id=policy_id, version="1.0.0", policy_hash=sha(policy_id)
    )


AVAILABILITY: Final = policy(PolicyRole.AVAILABILITY, "binance.spot.publication")
PRECEDENCE: Final = policy(PolicyRole.PRECEDENCE, "binance.spot.archive-revision")
PARSER: Final = policy(PolicyRole.PARSER, "binance.spot.archive.parser")


def revision(
    key: str,
    revision_id: str,
    *,
    event_time: datetime,
    event_end_time: datetime | None = None,
    arrival_seq: int = 0,
    supersedes: tuple[str, ...] = (),
    evidence_gap: bool = False,
    source_revision_id: str | None = None,
) -> RevisionRecord:
    observable = event_end_time or event_time
    ingest = observable + timedelta(days=600)
    times = ObservationTimes(
        event_time=event_time,
        event_end_time=event_end_time,
        source_time=observable if not evidence_gap else None,
        available_time=ingest if evidence_gap else observable + 250 * US,
        ingest_time=ingest,
        knowledge_time=ingest + timedelta(seconds=3),
        declared_latency=250 * US,
    )
    decision = AvailabilityDecision(
        times=times,
        policy=AVAILABILITY,
        evidence=() if evidence_gap else ("public feed publishes trades in real time",),
        evidence_gap="no publication evidence" if evidence_gap else None,
    )
    return RevisionRecord(
        observation_key=key,
        revision_id=revision_id,
        source_id=f"src:{key}",
        payload_hash=sha(revision_id),
        arrival_seq=arrival_seq,
        supersedes=supersedes,
        source_revision_id=source_revision_id,
        availability=decision,
    )


def edge(record: RevisionRecord, older: str) -> PrecedenceEvidence:
    return PrecedenceEvidence(
        observation_key=record.observation_key,
        revision_id=record.revision_id,
        superseded_revision_id=older,
        policy=PRECEDENCE,
        evidence=(f"checksum {older} replaced", "publication log"),
        knowledge_time=record.availability.times.knowledge_time,
    )


# --------------------------------------------------------------------------- revision block


def revision_columns(
    record: RevisionRecord,
    evidence: tuple[PrecedenceEvidence, ...] = (),
    *,
    interval: bool = False,
    instantaneous: bool = False,
) -> dict[str, Any]:
    """The revision block of a row (see ``phase1_tables`` module docs)."""
    RevisionGraph(revisions=(record,), precedence_evidence=evidence)  # a valid graph
    times = record.availability.times
    decision = record.availability
    row: dict[str, Any] = {
        "observation_key": record.observation_key,
        "revision_id": record.revision_id,
        "source_id": record.source_id,
        "payload_hash": record.payload_hash,
        "arrival_seq": record.arrival_seq,
        "supersedes": list(record.supersedes),
        "source_revision_id": record.source_revision_id,
        "source_revision_time": record.source_revision_time,
        "source_time": times.source_time,
        "available_time": times.available_time,
        "ingest_time": times.ingest_time,
        "knowledge_time": times.knowledge_time,
        "declared_latency_us": times.declared_latency // US,
        "availability_policy_id": decision.policy.policy_id,
        "availability_policy_version": decision.policy.version,
        "availability_policy_hash": decision.policy.policy_hash,
        "availability_evidence": list(decision.evidence),
        "availability_evidence_gap": decision.evidence_gap,
        "precedence_evidence": [
            {
                "superseded_revision_id": item.superseded_revision_id,
                "policy_id": item.policy.policy_id,
                "policy_version": item.policy.version,
                "policy_hash": item.policy.policy_hash,
                "evidence": list(item.evidence),
                "knowledge_time": item.knowledge_time,
            }
            for item in sorted(evidence, key=lambda item: item.superseded_revision_id)
        ],
        "contract_schema_version": record.schema_version,
    }
    if interval:
        row["interval_start"] = times.event_time
        row["interval_end"] = times.event_end_time
    elif instantaneous:
        assert times.event_end_time is None
        row["event_time"] = times.event_time
    else:
        row["event_time"] = times.event_time
        row["event_end_time"] = times.event_end_time
    return row


def revision_from_row(row: dict[str, Any]) -> tuple[RevisionRecord, tuple[PrecedenceEvidence, ...]]:
    """Rebuild the ``RevisionRecord`` and its ingest-time evidence from a row."""
    version = row["contract_schema_version"]
    interval = "interval_start" in row
    event_time = row["interval_start"] if interval else row["event_time"]
    event_end = row["interval_end"] if interval else row.get("event_end_time")
    times = ObservationTimes(
        schema_version=version,
        event_time=event_time,
        event_end_time=event_end,
        source_time=row["source_time"],
        available_time=row["available_time"],
        ingest_time=row["ingest_time"],
        knowledge_time=row["knowledge_time"],
        declared_latency=row["declared_latency_us"] * US,
    )
    availability = PolicyBinding(
        schema_version=version,
        role=PolicyRole.AVAILABILITY,
        policy_id=row["availability_policy_id"],
        version=row["availability_policy_version"],
        policy_hash=row["availability_policy_hash"],
    )
    record = RevisionRecord(
        schema_version=version,
        observation_key=row["observation_key"],
        revision_id=row["revision_id"],
        source_id=row["source_id"],
        payload_hash=row["payload_hash"],
        arrival_seq=row["arrival_seq"],
        supersedes=tuple(row["supersedes"]),
        source_revision_id=row["source_revision_id"],
        source_revision_time=row["source_revision_time"],
        availability=AvailabilityDecision(
            schema_version=version,
            times=times,
            policy=availability,
            evidence=tuple(row["availability_evidence"]),
            evidence_gap=row["availability_evidence_gap"],
        ),
    )
    evidence = tuple(
        PrecedenceEvidence(
            schema_version=version,
            observation_key=record.observation_key,
            revision_id=record.revision_id,
            superseded_revision_id=item["superseded_revision_id"],
            policy=PolicyBinding(
                schema_version=version,
                role=PolicyRole.PRECEDENCE,
                policy_id=item["policy_id"],
                version=item["policy_version"],
                policy_hash=item["policy_hash"],
            ),
            evidence=tuple(item["evidence"]),
            knowledge_time=item["knowledge_time"],
        )
        for item in row["precedence_evidence"]
    )
    return record, evidence


def parser_columns() -> dict[str, Any]:
    return {
        "parser_id": PARSER.policy_id,
        "parser_version": PARSER.version,
        "parser_hash": PARSER.policy_hash,
    }


def lineage_columns(item: SelectedRevisionLineage) -> dict[str, Any]:
    return {
        "lineage_raw_table": item.raw_table,
        "lineage_raw_revision_id": item.raw_revision_id,
        "lineage_source_table": item.source_table,
        "lineage_source_revision_id": item.source_revision_id,
    }


# --------------------------------------------------------------------------- per-table rows


def archive_row(
    tag: str = "a", *, symbol: str = "BTCUSDT", start: datetime = datetime(2024, 12, 31, tzinfo=UTC)
) -> dict[str, Any]:
    end = start + timedelta(days=1)
    day = start.date().isoformat()
    uri = f"https://data.binance.vision/data/spot/daily/klines/{symbol}/1m/{symbol}-1m-{day}.zip"
    collected_fields: dict[str, Any] = dict(
        ref=ObjectRef(
            key=f"raw/binance/spot/klines_1m/{symbol}/{day}/{sha(tag)[:16]}.zip",
            uri=f"file:///warehouse/raw/binance/{sha(tag)[:16]}.zip",
            sha256=sha(f"archive-{tag}"),
            size=123_456,
        ),
        symbol=symbol,
        coverage_start=start,
        coverage_end=end,
        source_uri=uri,
        retrieved_at=end + timedelta(days=600),
        source_sha256=sha(f"archive-{tag}"),
        source_metadata={"etag": f'"{tag}"', "last-modified": "Wed, 01 Jan 2025 00:10:00 GMT"},
    )
    collected = CollectedObject(**collected_fields)
    older = f"archive-rev-{tag}-0"
    record = revision(
        uri,
        f"archive-rev-{tag}-1",
        event_time=start,
        event_end_time=end,
        arrival_seq=7,
        supersedes=(older,),
        source_revision_id=f'"{tag}"',
    )
    row = revision_columns(record, (edge(record, older),))
    row.update(
        source_binding_id="binance.public.spot.archive",
        source_binding_version="1.0.0",
        collector_id="binance.spot.archive.collector",
        collector_version="1.0.0",
        collection_request_id=f"req-{tag}",
        data_type="klines_1m",
        symbol=collected.symbol,
        coverage_start=collected.coverage_start,
        coverage_end=collected.coverage_end,
        source_uri=collected.source_uri,
        retrieved_at=collected.retrieved_at,
        source_sha256=collected.source_sha256,
        object_key=collected.ref.key,
        object_uri=collected.ref.uri,
        object_sha256=collected.ref.sha256,
        object_size_bytes=collected.ref.size,
        source_metadata=[
            {"name": name, "value": value}
            for name, value in sorted(collected.source_metadata.items())
        ],
    )
    return row


def agg_trade_row(
    tag: str = "a", *, symbol: str = "BTCUSDT", when: datetime = T0, trade_id: int = 1
) -> dict[str, Any]:
    record = revision(
        f"binance:spot:{symbol}:aggtrade:{trade_id}",
        f"raw-aggtrade-{tag}-{trade_id}",
        event_time=when,
        evidence_gap=trade_id % 2 == 0,
    )
    row = revision_columns(record, instantaneous=True)
    row.update(
        symbol=symbol,
        archive_revision_id=f"archive-rev-{tag}-1",
        archive_line_number=trade_id,
        **parser_columns(),
        agg_trade_id=trade_id,
        price=Decimal("93712.01000000"),
        quantity=Decimal("0.00010000"),
        first_trade_id=trade_id * 10,
        last_trade_id=trade_id * 10 + 2,
        timestamp_raw=int(when.timestamp() * 1000),
        is_buyer_maker=True,
        is_best_match=None,
    )
    return row


def kline_row(tag: str = "a", *, symbol: str = "BTCUSDT", start: datetime = T0) -> dict[str, Any]:
    end = start + timedelta(minutes=1)
    record = revision(
        f"binance:spot:{symbol}:kline:1m:{start.isoformat()}",
        f"raw-kline-{tag}-{symbol}-{start.isoformat()}",
        event_time=start,
        event_end_time=end,
    )
    row = revision_columns(record, interval=True)
    row.update(
        symbol=symbol,
        archive_revision_id=f"archive-rev-{tag}-1",
        archive_line_number=1,
        **parser_columns(),
        open_time_raw=int(start.timestamp() * 1000),
        open=Decimal("93700.00"),
        high=Decimal("93750.50"),
        low=Decimal("93690.10"),
        close=Decimal("93712.01"),
        volume=Decimal("12.34567800"),
        close_time_raw=int(end.timestamp() * 1000) - 1,
        quote_asset_volume=Decimal("1157000.12345678"),
        number_of_trades=321,
        taker_buy_base_asset_volume=Decimal("6.1"),
        taker_buy_quote_asset_volume=Decimal("571600.5"),
        ignore_raw="0",
    )
    return row


def trade_row(tag: str = "a", *, symbol: str = "BTCUSDT", when: datetime = T0) -> dict[str, Any]:
    record = revision(
        f"binance:spot:{symbol}:trade:9001", f"canonical-trade-{tag}", event_time=when
    )
    lineage = SelectedRevisionLineage(
        canonical_table="canonical.trades",
        canonical_revision_id=record.revision_id,
        raw_table="raw.binance_spot_agg_trades",
        raw_revision_id=f"raw-aggtrade-{tag}-9001",
        source_table="raw.binance_spot_archives",
        source_revision_id=f"archive-rev-{tag}-1",
    )
    row = revision_columns(record, instantaneous=True)
    row.update(
        venue="binance",
        instrument_type="spot",
        symbol=symbol,
        venue_symbol=symbol,
        venue_trade_id="9001",
        price=Decimal("93712.01"),
        quantity=Decimal("0.0001"),
        buyer_is_maker=False,
        **lineage_columns(lineage),
    )
    return row


def bar_row(
    tag: str = "a", *, symbol: str = "BTCUSDT", start: datetime = T0, close: str = "93712.01"
) -> dict[str, Any]:
    end = start + timedelta(minutes=1)
    record = revision(
        f"binance:spot:{symbol}:bar:1m:{start.isoformat()}",
        f"canonical-bar-{tag}-{symbol}-{start.isoformat()}",
        event_time=start,
        event_end_time=end,
    )
    lineage = SelectedRevisionLineage(
        canonical_table="canonical.bars_1m",
        canonical_revision_id=record.revision_id,
        raw_table="raw.binance_spot_klines_1m",
        raw_revision_id=f"raw-kline-{tag}-{symbol}-{start.isoformat()}",
        source_table="raw.binance_spot_archives",
        source_revision_id=f"archive-rev-{tag}-1",
    )
    row = revision_columns(record, interval=True)
    row.update(
        venue="binance",
        instrument_type="spot",
        symbol=symbol,
        venue_symbol=symbol,
        open=Decimal("93700"),
        high=Decimal("93750.5"),
        low=Decimal("93690.1"),
        close=Decimal(close),
        volume=Decimal("12.345678"),
        quote_volume=Decimal("1157000.12345678"),
        trade_count=321,
        taker_buy_base_volume=Decimal("6.1"),
        taker_buy_quote_volume=Decimal("571600.5"),
        **lineage_columns(lineage),
    )
    return row


BTC: Final = Instrument(
    venue="binance", symbol="BTCUSDT", instrument_type=InstrumentType.SPOT, base="BTC", quote="USDT"
)
BTC_EPISODE: Final = StableEpisodeKey(
    basis=EpisodeIdentityBasis.STABLE_PRODUCT_ID,
    venue="binance",
    instrument_type=InstrumentType.SPOT,
    venue_product_id="BTCUSDT",
)


def listing_revision(tag: str = "a") -> tuple[ListingRevision, SelectedRevisionLineage]:
    listed = datetime(2017, 8, 17, 4, tzinfo=UTC)
    record = revision(
        BTC_EPISODE.observation_key(), f"listing-{tag}", event_time=listed, evidence_gap=True
    )
    listing = ListingRevision(
        revision=record,
        episode=BTC_EPISODE,
        instrument=BTC,
        tradable_intervals=(TradableInterval(tradable_from=listed, tradable_until=None),),
        status=ListingStatus.LISTED,
        source_status="TRADING",
    )
    lineage = SelectedRevisionLineage(
        canonical_table="canonical.instrument_listings",
        canonical_revision_id=record.revision_id,
        raw_table="raw.binance_spot_archives",
        raw_revision_id=f"listing-raw-{tag}",
        source_table="raw.binance_spot_archives",
        source_revision_id=f"listing-source-{tag}",
    )
    return listing, lineage


def listing_row(tag: str = "a") -> dict[str, Any]:
    listing, lineage = listing_revision(tag)
    episode = listing.episode
    row = revision_columns(listing.revision)
    row.update(
        episode_basis=episode.basis.value,
        episode_venue=episode.venue,
        episode_instrument_type=episode.instrument_type.value,
        episode_venue_product_id=getattr(episode, "venue_product_id", None),
        episode_symbol=getattr(episode, "symbol", None),
        episode_tradable_from=getattr(episode, "tradable_from", None),
        venue=listing.instrument.venue,
        symbol=listing.instrument.symbol,
        instrument_type=listing.instrument.instrument_type.value,
        base=listing.instrument.base,
        quote=listing.instrument.quote,
        tradable_intervals=[
            {"tradable_from": item.tradable_from, "tradable_until": item.tradable_until}
            for item in listing.tradable_intervals
        ],
        status=listing.status.value,
        source_status=listing.source_status,
        status_reason=listing.status_reason,
        renamed_from=None,
        **lineage_columns(lineage),
    )
    return row


def listing_from_row(row: dict[str, Any]) -> ListingRevision:
    record, _ = revision_from_row(row)
    version = row["contract_schema_version"]
    episode = StableEpisodeKey(
        schema_version=version,
        basis=row["episode_basis"],
        venue=row["episode_venue"],
        instrument_type=row["episode_instrument_type"],
        venue_product_id=row["episode_venue_product_id"],
    )
    return ListingRevision(
        schema_version=version,
        revision=record,
        episode=episode,
        instrument=Instrument(
            schema_version=version,
            venue=row["venue"],
            symbol=row["symbol"],
            instrument_type=row["instrument_type"],
            base=row["base"],
            quote=row["quote"],
        ),
        tradable_intervals=tuple(
            TradableInterval(schema_version=version, **item) for item in row["tradable_intervals"]
        ),
        status=row["status"],
        source_status=row["source_status"],
        status_reason=row["status_reason"],
        renamed_from=None,
    )


def quality_row(tag: str = "a") -> dict[str, Any]:
    day = datetime(2024, 12, 31, tzinfo=UTC)
    return {
        "report_id": f"qr-{tag}",
        "quality_rule_id": "hlens.quality.canonical",
        "quality_rule_version": "1.0.0",
        "quality_rule_hash": sha("hlens.quality.canonical"),
        "subject_table": "canonical.bars_1m",
        "subject_snapshot_id": "4242",
        "subject_symbol": "BTCUSDT",
        "subject_start": day,
        "subject_end": day + timedelta(days=1),
        "knowledge_time": day + timedelta(days=601),
        "events": [
            {
                "event_id": "e1",
                "event_type": "gap",
                "table": "canonical.bars_1m",
                "observation_key": None,
                "revision_ids": [],
                "event_start": day + timedelta(hours=3),
                "event_end": day + timedelta(hours=3, minutes=2),
                "detail": "two 1m bars missing; not inferred",
            }
        ],
        "evidence_gaps": [
            {"table": "canonical.instrument_listings", "revision_id": f"listing-{tag}", "gap": "x"}
        ],
    }


def manifest(tag: str = "a") -> ResearchDatasetManifest:
    _, lineage = listing_revision(tag)
    simulation = datetime(2024, 12, 31, 12, tzinfo=UTC)
    spec = UniverseSelectionSpec(
        name="binance.spot.btc-eth",
        version="1.0.0",
        candidate_source=UniverseCandidateSource.POINT_IN_TIME_LISTINGS,
        venue="binance",
        instrument_type=InstrumentType.SPOT,
        symbols=("BTCUSDT", "ETHUSDT"),
        filters=(),
    )
    pit_fields: dict[str, Any] = {
        "name": "hlens.pit.maximal-head",
        "version": "1.0.0",
        "simulation_time": simulation,
        "knowledge_cutoff": simulation + timedelta(days=700),
        "snapshot_bindings": {
            "canonical.instrument_listings": "11",
            "quality.data_quality_reports": "12",
            "raw.binance_spot_archives": "13",
        },
        "point_in_time_binding": policy(PolicyRole.POINT_IN_TIME, "hlens.pit.maximal-head"),
        "availability_bindings": (AVAILABILITY,),
        "precedence_bindings": (PRECEDENCE,),
        "parser_bindings": (PARSER,),
    }
    return ResearchDatasetManifest(
        dataset=DatasetRef(
            zone=Zone.RESEARCH_DATASET,
            table="research.c3test_dataset",
            snapshot_id=f"77{len(tag)}",
            time_range_start=simulation - timedelta(days=1),
            time_range_end=simulation,
        ),
        point_in_time=PointInTimeSpec(**pit_fields),
        universe_spec=spec.binding(),
        members=(
            UniverseMember(episode=BTC_EPISODE, listing_revision_id=lineage.canonical_revision_id),
        ),
        exclusions=(),
        lineage=(lineage,),
        quality_report_ids=(f"qr-{tag}",),
        evidence_gaps=(),
    )


def manifest_row(tag: str = "a") -> dict[str, Any]:
    return manifest_row_of(manifest(tag))


def manifest_row_of(item: ResearchDatasetManifest) -> dict[str, Any]:
    pit = item.point_in_time
    return {
        "manifest_content_hash": item.content_hash(),
        "contract_schema_version": item.schema_version,
        "dataset_zone": item.dataset.zone.value,
        "dataset_table": item.dataset.table,
        "dataset_snapshot_id": item.dataset.snapshot_id,
        "dataset_time_range_start": item.dataset.time_range_start,
        "dataset_time_range_end": item.dataset.time_range_end,
        "point_in_time_name": pit.name,
        "point_in_time_version": pit.version,
        "point_in_time_hash": pit.content_hash(),
        "simulation_time": pit.simulation_time,
        "simulation_start": pit.simulation_start,
        "simulation_end": pit.simulation_end,
        "knowledge_cutoff": pit.knowledge_cutoff,
        "universe_spec_name": item.universe_spec.name,
        "universe_spec_version": item.universe_spec.version,
        "universe_spec_hash": item.universe_spec.spec_hash,
        "snapshot_bindings": [
            {"table": table, "snapshot_id": snapshot}
            for table, snapshot in sorted(pit.snapshot_bindings.items())
        ],
        "quality_report_ids": list(item.quality_report_ids),
        "manifest_json": canonical_json(item.model_dump(mode="json")),
    }


# --------------------------------------------------------------------------- REST rows (D3B)

REST_DECODER: Final = policy(PolicyRole.PARSER, "binance.spot.rest.decoder")
REST_ORIGIN: Final = "https://market-data.invalid"


def _ms(value: datetime) -> int:
    return (value - datetime(1970, 1, 1, tzinfo=UTC)) // timedelta(milliseconds=1)


def rest_record(
    key: str,
    payload_hash: str,
    subject: RestAvailabilitySubject,
    *,
    event_time: datetime,
    event_end_time: datetime | None,
    ingest_time: datetime,
    arrival_seq: int,
) -> RevisionRecord:
    decision = decide_rest_availability(
        subject,
        event_time=event_time,
        event_end_time=event_end_time,
        ingest_time=ingest_time,
        knowledge_time=ingest_time + timedelta(seconds=2),
    )
    source = rest_identity.rest_source_identity()
    return RevisionRecord(
        observation_key=key,
        revision_id=rest_identity.revision_id(key, source, payload_hash),
        source_id=source,
        payload_hash=payload_hash,
        arrival_seq=arrival_seq,
        availability=decision,
    )


def rest_response_row(
    tag: str = "a", *, symbol: str = "BTCUSDT", start: datetime = T0
) -> dict[str, Any]:
    query = rest_identity.RestPageQuery.klines_from_start(symbol, _ms(start))
    page = rest_identity.page_identity_sha256(query, REST_ORIGIN)
    body = sha(f"rest-body-{tag}")
    requested = start + timedelta(days=600)
    retrieved = requested + timedelta(milliseconds=140)
    record = rest_record(
        rest_identity.response_observation_key(page),
        body,
        RestAvailabilitySubject.RESPONSE,
        event_time=requested,
        event_end_time=retrieved,
        ingest_time=retrieved,
        arrival_seq=rest_identity.REST_ARRIVAL_SEQ_BASE,
    )
    row = revision_columns(record)
    row.update(
        source_binding_id=rest_identity.REST_SOURCE_ID,
        source_binding_version=rest_identity.REST_SOURCE_VERSION,
        collector_id="binance.spot.rest.collector",
        collector_version="1.0.0",
        collection_request_id=f"rest-attempt-{tag}",
        page_index=0,
        data_type=query.data_type,
        symbol=symbol,
        request_origin=REST_ORIGIN,
        request_path=query.path,
        request_query=query.query_string(),
        declared_time_unit=rest_identity.DECLARED_TIME_UNIT,
        page_limit=query.limit,
        page_identity_sha256=page,
        source_uri=rest_identity.page_source_uri(query, REST_ORIGIN),
        requested_at=requested,
        retrieved_at=retrieved,
        http_status=200,
        source_metadata=[{"name": "x-mbx-used-weight-1m", "value": "2"}],
        object_key=rest_identity.response_object_key(query.data_type, symbol, body, page),
        object_uri=f"file:///warehouse/raw/binance/spot/rest/{body[:16]}.json",
        object_sha256=body,
        object_size_bytes=2048,
        decoder_id=REST_DECODER.policy_id,
        decoder_version=REST_DECODER.version,
        decoder_hash=REST_DECODER.policy_hash,
        decode_outcome="accepted",
        decode_rejection_code=None,
        element_count=1,
        answered_start=start,
        answered_end=start + timedelta(minutes=1),
    )
    return row


def _decoder_columns(response_tag: str, element_index: int) -> dict[str, Any]:
    return {
        "response_revision_id": f"rest-response-{response_tag}",
        "element_index": element_index,
        "decoder_id": REST_DECODER.policy_id,
        "decoder_version": REST_DECODER.version,
        "decoder_hash": REST_DECODER.policy_hash,
    }


def rest_agg_trade_row(
    tag: str = "a", *, symbol: str = "BTCUSDT", when: datetime = T0, trade_id: int = 1
) -> dict[str, Any]:
    natives: dict[str, Any] = {
        "agg_trade_id": trade_id,
        "price": Decimal("93712.01000000"),
        "quantity": Decimal("0.00010000") * len(tag),
        "first_trade_id": trade_id * 10,
        "last_trade_id": trade_id * 10 + 2,
        "timestamp_raw": _ms(when),
        "is_buyer_maker": True,
        "is_best_match": True,
    }
    record = rest_record(
        rest_identity.agg_trade_observation_key(symbol, trade_id),
        rest_identity.agg_trade_payload_hash(symbol, natives),
        RestAvailabilitySubject.AGG_TRADE,
        event_time=when,
        event_end_time=None,
        ingest_time=when + timedelta(days=600),
        arrival_seq=rest_identity.element_arrival_seq(rest_identity.REST_ARRIVAL_SEQ_BASE, 0),
    )
    row = revision_columns(record, instantaneous=True)
    row.update(symbol=symbol, **_decoder_columns(tag, 0), **natives)
    return row


def rest_kline_row(
    tag: str = "a", *, symbol: str = "BTCUSDT", start: datetime = T0
) -> dict[str, Any]:
    end = start + timedelta(minutes=1)
    natives: dict[str, Any] = {
        "open_time_raw": _ms(start),
        "open": Decimal("93700.00"),
        "high": Decimal("93750.50"),
        "low": Decimal("93690.10"),
        "close": Decimal("93712.01"),
        "volume": Decimal("12.34567800") * len(tag),
        "close_time_raw": _ms(end) - 1,
        "quote_asset_volume": Decimal("1157000.12345678"),
        "number_of_trades": 321,
        "taker_buy_base_asset_volume": Decimal("6.1"),
        "taker_buy_quote_asset_volume": Decimal("571600.5"),
        "ignore_raw": "0",
    }
    record = rest_record(
        rest_identity.kline_1m_observation_key(symbol, start),
        rest_identity.kline_1m_payload_hash(symbol, natives),
        RestAvailabilitySubject.KLINE_1M,
        event_time=start,
        event_end_time=end,
        ingest_time=end + timedelta(days=600),
        arrival_seq=rest_identity.element_arrival_seq(rest_identity.REST_ARRIVAL_SEQ_BASE, 0),
    )
    row = revision_columns(record, interval=True)
    row.update(symbol=symbol, **_decoder_columns(tag, 0), **natives)
    return row


def precedence_evidence_row(tag: str = "a") -> dict[str, Any]:
    key = f"binance:spot:agg_trade:BTCUSDT:{len(tag)}"
    archive_revision, rest_revision = f"archive-row-{tag}", f"rest-row-{tag}"
    item = PrecedenceEvidence(
        observation_key=key,
        revision_id=archive_revision,
        superseded_revision_id=rest_revision,
        policy=DELIVERY_CHANNEL_BINDING,
        evidence=("canonical market content equal", f"projection_sha256={sha(tag)}"),
        knowledge_time=T0 + timedelta(days=601),
    )
    return {
        "edge_id": rest_identity.edge_id(
            DELIVERY_CHANNEL_BINDING, key, archive_revision, rest_revision
        ),
        "observation_key": item.observation_key,
        "revision_id": item.revision_id,
        "revision_table": BINANCE_SPOT_AGG_TRADES.table,
        "superseded_revision_id": item.superseded_revision_id,
        "superseded_table": BINANCE_SPOT_REST_AGG_TRADES.table,
        "policy_id": item.policy.policy_id,
        "policy_version": item.policy.version,
        "policy_hash": item.policy.policy_hash,
        "evidence": list(item.evidence),
        "knowledge_time": item.knowledge_time,
        "revision_snapshot_id": "101",
        "superseded_snapshot_id": "202",
        "projection_sha256": sha(tag),
        "contract_schema_version": item.schema_version,
    }


def precedence_evidence_from_row(row: dict[str, Any]) -> PrecedenceEvidence:
    """Rebuild the complete ``PrecedenceEvidence`` (both ends explicit) from an evidence row."""
    version = row["contract_schema_version"]
    return PrecedenceEvidence(
        schema_version=version,
        observation_key=row["observation_key"],
        revision_id=row["revision_id"],
        superseded_revision_id=row["superseded_revision_id"],
        policy=PolicyBinding(
            schema_version=version,
            role=PolicyRole.PRECEDENCE,
            policy_id=row["policy_id"],
            version=row["policy_version"],
            policy_hash=row["policy_hash"],
        ),
        evidence=tuple(row["evidence"]),
        knowledge_time=row["knowledge_time"],
    )


EXCHANGE_INFO_DECODER: Final = policy(PolicyRole.PARSER, "binance.spot.exchange-info.decoder")


def exchange_info_row(tag: str = "a") -> dict[str, Any]:
    """One ``raw.binance_spot_exchange_info`` snapshot revision (E2, ADR-0029)."""
    query = exchange_info_identity.ExchangeInfoQuery()
    request_identity = exchange_info_identity.request_identity_sha256(query, REST_ORIGIN)
    key = exchange_info_identity.snapshot_observation_key(request_identity)
    body = sha(f"exchange-info-body-{tag}")
    requested = T0 + timedelta(days=600)
    retrieved = requested + timedelta(milliseconds=90)
    decision = decide_exchange_info_availability(
        ExchangeInfoAvailabilitySubject.SNAPSHOT,
        requested_at=requested,
        ingest_time=retrieved,
        knowledge_time=retrieved + timedelta(seconds=2),
    )
    source = exchange_info_identity.exchange_info_source_identity()
    record = RevisionRecord(
        observation_key=key,
        revision_id=exchange_info_identity.revision_id(key, source, body),
        source_id=source,
        payload_hash=body,
        arrival_seq=0,
        availability=decision,
    )
    row = revision_columns(record)
    row.update(
        source_binding_id=exchange_info_identity.EXCHANGE_INFO_SOURCE_ID,
        source_binding_version=exchange_info_identity.EXCHANGE_INFO_SOURCE_VERSION,
        collector_id="binance.spot.public-exchange-info",
        collector_version="1.0.0",
        collection_request_id=f"exchange-info-attempt-{tag}",
        request_origin=REST_ORIGIN,
        request_path=query.path,
        request_query=query.query_string(),
        request_identity_sha256=request_identity,
        source_uri=exchange_info_identity.request_source_uri(query, REST_ORIGIN),
        requested_at=requested,
        retrieved_at=retrieved,
        http_status=200,
        source_metadata=[{"name": "x-mbx-used-weight-1m", "value": "20"}],
        object_key=exchange_info_identity.response_object_key(body, request_identity),
        object_uri=f"file:///warehouse/raw/binance/spot/exchange-info/{body[:16]}.json",
        object_sha256=body,
        object_size_bytes=4096,
        decoder_id=EXCHANGE_INFO_DECODER.policy_id,
        decoder_version=EXCHANGE_INFO_DECODER.version,
        decoder_hash=EXCHANGE_INFO_DECODER.policy_hash,
        server_time_raw=1_788_000_000_000,
        requested_symbols=["BTCUSDT", "ETHUSDT"],
        symbols=[
            {"symbol": "BTCUSDT", "status": "TRADING", "base_asset": "BTC", "quote_asset": "USDT"}
        ],
    )
    return row


def quality_evidence_gap_row(
    tag: str = "a", *, symbol: str = "BTCUSDT", start: datetime = T0
) -> dict[str, Any]:
    """One ``quality.availability_evidence_gaps`` row (QG-1, ADR-0031)."""
    return {
        "quality_report_id": f"qr-{tag}",
        "table": "canonical.instrument_listings",
        "revision_id": f"listing-{tag}",
        "gap": "x",
        "subject_symbol": symbol,
        "subject_start": start,
        "batch_index": 0,
    }


def dataset_selection_row(
    tag: str = "a", *, symbol: str = "BTCUSDT", start: datetime = T0
) -> dict[str, Any]:
    """One ``research.dataset_selections`` row (DS-1, ADR-0033): a pointer, no revision block."""
    return {
        "selection_id": f"selection-{tag}",
        "canonical_table": CANONICAL_TRADES.table,
        "symbol": symbol,
        "observation_key": f"binance:spot:{symbol}:aggtrade:{len(tag)}",
        "revision_id": f"canonical-trade-{tag}",
        "event_time": start,
        "effective_from": None,
        "effective_until": None,
    }


def dataset_evidence_manifest_row(tag: str = "a", **_: Any) -> dict[str, Any]:
    """One minimal ``research.dataset_evidence_manifests`` physical row (ADR-0077 B2)."""
    return {
        "manifest_content_hash": "a" * 64,
        "contract_schema_version": CONTRACT_SCHEMA_VERSION,
        "dataset_zone": "research_dataset",
        "dataset_table": "research.dataset_selection_chunks",
        "dataset_snapshot_id": "9103",
        "dataset_time_range_start": T0,
        "dataset_time_range_end": T0 + timedelta(days=1),
        "point_in_time_name": "phase1.first_slice",
        "point_in_time_version": "1.0.0",
        "point_in_time_hash": "b" * 64,
        "simulation_time": T0,
        "simulation_start": None,
        "simulation_end": None,
        "knowledge_cutoff": T0,
        "universe_spec_name": "phase1.first_slice",
        "universe_spec_version": "1.0.0",
        "universe_spec_hash": "c" * 64,
        "snapshot_bindings": [],
        "rule_id": "hlens.pit.maximal-head",
        "rule_version": "1.0.0",
        "rule_hash": "d" * 64,
        "data_type": "agg_trades",
        "selection_id": f"selection-{tag}",
        "row_count": 1,
        "chunk_rows": 1,
        "chunk_count": 1,
        "evidence": [],
        "manifest_json": "{}",
    }


def dataset_selection_chunk_row(
    tag: str = "a", *, symbol: str = "BTCUSDT", start: datetime = T0
) -> dict[str, Any]:
    """One minimal ``research.dataset_selection_chunks`` row (ADR-0077 B2)."""
    return {
        "selection_id": f"selection-{tag}",
        "canonical_table": CANONICAL_TRADES.table,
        "symbol": symbol,
        "observation_key": f"binance:spot:{symbol}:aggtrade:{len(tag)}",
        "revision_id": f"canonical-trade-{tag}",
        "event_time": start,
        "effective_from": None,
        "effective_until": None,
        "chunk_index": 0,
        "row_ordinal": 0,
    }


#: One minimal valid row builder per production table (keyed by table name).
ROW_BUILDERS: Final[dict[str, Callable[..., dict[str, Any]]]] = {
    BINANCE_SPOT_ARCHIVES.table: archive_row,
    BINANCE_SPOT_AGG_TRADES.table: agg_trade_row,
    BINANCE_SPOT_KLINES_1M.table: kline_row,
    CANONICAL_TRADES.table: trade_row,
    CANONICAL_BARS_1M.table: bar_row,
    CANONICAL_INSTRUMENT_LISTINGS.table: listing_row,
    DATA_QUALITY_REPORTS.table: quality_row,
    DATASET_MANIFESTS.table: manifest_row,
    BINANCE_SPOT_REST_RESPONSES.table: rest_response_row,
    BINANCE_SPOT_REST_AGG_TRADES.table: rest_agg_trade_row,
    BINANCE_SPOT_REST_KLINES_1M.table: rest_kline_row,
    BINANCE_SPOT_PRECEDENCE_EVIDENCE.table: precedence_evidence_row,
    BINANCE_SPOT_EXCHANGE_INFO.table: exchange_info_row,
    QUALITY_EVIDENCE_GAPS.table: quality_evidence_gap_row,
    DATASET_SELECTIONS.table: dataset_selection_row,
    DATASET_EVIDENCE_MANIFESTS.table: dataset_evidence_manifest_row,
    DATASET_SELECTION_CHUNKS.table: dataset_selection_chunk_row,
}
assert set(ROW_BUILDERS) == {definition.table for definition in PHASE1_TABLES}
assert (
    CONTRACT_SCHEMA_VERSION == "2.5.0"
)  # ADR-0088; ADR-0077's 2.3.0 envelope remains a published history version.


def batch_for(definition: RegisteredTableDefinition, rows: list[dict[str, Any]]) -> pa.Table:
    return pa.Table.from_pylist(rows, schema=definition.arrow_schema)


def minimal_batch(definition: RegisteredTableDefinition, tag: str = "a") -> pa.Table:
    return batch_for(definition, [ROW_BUILDERS[definition.table](tag)])


# --------------------------------------------------------------------------- evolution targets


def bars_month_target(field_id: int = 1002, spec_id: int = 1) -> RegisteredTableDefinition:
    """``canonical.bars_1m@1.1.0``: identity(symbol) + month(interval_start) (test-only target)."""
    source = CANONICAL_BARS_1M
    symbol = source.schema.find_field("symbol").field_id
    start = source.schema.find_field("interval_start").field_id
    return RegisteredTableDefinition(
        table=source.table,
        definition_id=source.definition_id,
        version="1.1.0",
        schema=source.schema,
        fingerprint_rule=source.fingerprint_rule,
        partition_spec=PartitionSpec(
            PartitionField(
                source_id=symbol, field_id=1000, transform=IdentityTransform(), name="symbol"
            ),
            PartitionField(
                source_id=start,
                field_id=field_id,
                transform=MonthTransform(),
                name="interval_start_month",
            ),
            spec_id=spec_id,
        ),
        properties=source.properties,
        evolves_from=source.binding,
    )


def archives_by_symbol_target(field_id: int = 1000) -> RegisteredTableDefinition:
    """``raw.binance_spot_archives@1.1.0``: unpartitioned → identity(symbol) (test-only target)."""
    source = BINANCE_SPOT_ARCHIVES
    symbol = source.schema.find_field("symbol").field_id
    return RegisteredTableDefinition(
        table=source.table,
        definition_id=source.definition_id,
        version="1.1.0",
        schema=source.schema,
        fingerprint_rule=source.fingerprint_rule,
        partition_spec=PartitionSpec(
            PartitionField(
                source_id=symbol, field_id=field_id, transform=IdentityTransform(), name="symbol"
            ),
            spec_id=1,
        ),
        properties=source.properties,
        evolves_from=source.binding,
    )


BARS_MONTH: Final = bars_month_target()
ARCHIVES_BY_SYMBOL: Final = archives_by_symbol_target()
EVOLUTION_REGISTRY: Final = TableDefinitionRegistry(
    (*PHASE1_TABLES, BARS_MONTH, ARCHIVES_BY_SYMBOL)
)


def logical_rows(table: pa.Table) -> list[dict[str, Any]]:
    """Rows in a canonical order (``repr`` of each row) for order-independent comparisons."""
    return sorted(table.to_pylist(), key=repr)

"""Bounded listing-observation projection runs.

The legacy listing deriver accepts arbitrary row iterables but materializes each symbol's full
observation history. This module turns the already-proven Raw row run into a disk-backed,
deterministically ordered observation run so bounded listing consumers can replay one symbol at a
time without retaining all observations.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Iterator, Mapping
from dataclasses import replace
from itertools import chain, groupby, islice
from typing import Any

from core.contracts.storage import StorageAdapter
from core.contracts.universe import (
    DegradedEpisodeKey,
    EpisodeIdentityBasis,
    ListingStatus,
    TradableInterval,
)
from core.domain.specs import Instrument, InstrumentType
from infrastructure.canonical import listing_rules as lr
from infrastructure.canonical.rules import SYMBOLS
from infrastructure.catalog.iceberg_adapter import CatalogIntegrityError
from infrastructure.revision.exchange_info_store import ExchangeInfoRowVerifier
from infrastructure.streaming.runs import RunLimits, RunRef, RunSetBuilder, iter_run

__all__ = ["iter_planned_listing_revisions", "listing_observations_run"]


def _observation_key(row: Mapping[str, Any]) -> tuple[str, Any, str]:
    return (row["venue_symbol"], row["retrieved_at"], row["snapshot_revision_id"])


def _tie_ids(rows: Iterable[Mapping[str, Any]], state: dict[str, Any]) -> Iterator[str]:
    for row in rows:
        state["count"] += 1
        yield row["snapshot_revision_id"]
    state["exhausted"] = True


def _fixed_detail(message: str) -> Callable[[int], str]:
    return lambda _count: message


def _deliver_finding(
    finding_sink: Callable[[str, str, Any, Iterable[str], Callable[[int], str]], None],
    code: str,
    symbol: str,
    instant: Any,
    revision_ids: Iterable[str],
    detail_for_count: Callable[[int], str],
) -> int:
    """Require synchronous sinks to consume every evidence ID before returning."""
    state = {"count": 0, "exhausted": False}

    def guarded_ids() -> Iterator[str]:
        for revision_id in revision_ids:
            state["count"] += 1
            yield revision_id
        state["exhausted"] = True

    finding_sink(code, symbol, instant, guarded_ids(), detail_for_count)
    if not state["exhausted"]:
        raise CatalogIntegrityError("listing finding sink did not consume all revision IDs")
    return state["count"]


def listing_observations_run(
    raw_rows: RunRef | None,
    *,
    storage: StorageAdapter,
    capacity: int,
    merge_fanout: int,
    limits: RunLimits,
    max_record_bytes: int,
) -> RunRef | None:
    """Build the sorted observation run from a verified Raw proof run.

    ``raw_rows`` must be the result of ``ExchangeInfoRowVerifier.verify_table_bounded``. Its
    records bind each row to the actual verified Raw ``snapshot_id`` and ``snapshot_ordinal``.
    Every resource bound is required. The output order is venue symbol, retrieved time, then
    snapshot revision ID (Unicode code-point order for the final text key). Each output record
    retains the verified snapshot membership needed for listing batch replay.
    """
    if raw_rows is None:
        return None
    for name, value, minimum in (
        ("capacity", capacity, 1),
        ("merge_fanout", merge_fanout, 2),
        ("max_record_bytes", max_record_bytes, 1),
    ):
        if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
            raise ValueError(f"{name} must be an integer >= {minimum}")
    if not isinstance(limits, RunLimits):
        raise ValueError("limits must be RunLimits")

    builder = RunSetBuilder(
        storage,
        key=_observation_key,
        capacity=capacity,
        merge_fanout=merge_fanout,
        limits=limits,
    )
    with builder, iter_run(storage, raw_rows) as source:
        for item in source:
            if not isinstance(item, Mapping):
                raise CatalogIntegrityError("Raw proof run contains a non-mapping record")
            row = item.get("row")
            if not isinstance(row, Mapping):
                raise CatalogIntegrityError("Raw proof run record has no row mapping")
            snapshot_id = item.get("snapshot_id")
            ordinal = item.get("snapshot_ordinal")
            if not isinstance(snapshot_id, str) or not snapshot_id:
                raise CatalogIntegrityError("Raw proof run record has no snapshot membership")
            if isinstance(ordinal, bool) or not isinstance(ordinal, int) or ordinal < 0:
                raise CatalogIntegrityError("Raw proof run record has invalid snapshot ordinal")
            requested = row.get("requested_symbols")
            symbols = row.get("symbols")
            if not isinstance(requested, list) or not isinstance(symbols, list):
                raise CatalogIntegrityError("Raw proof row has invalid listing symbol arrays")
            if any(not isinstance(symbol, str) for symbol in requested):
                raise CatalogIntegrityError("Raw proof row has a non-text requested symbol")
            if len(set(requested)) != len(requested):
                raise CatalogIntegrityError("Raw proof row requests a symbol more than once")
            if any(symbol not in lr.FIRST_SLICE_ASSETS for symbol in requested):
                raise CatalogIntegrityError("Raw proof row requests a symbol outside listing scope")
            revision_id = row.get("revision_id")
            if not isinstance(revision_id, str) or not revision_id:
                raise CatalogIntegrityError("Raw proof row has no revision_id")
            for venue_symbol in requested:
                matches = [
                    symbol_row
                    for symbol_row in symbols
                    if isinstance(symbol_row, Mapping) and symbol_row.get("symbol") == venue_symbol
                ]
                if len(matches) > 1:
                    raise CatalogIntegrityError(
                        f"Raw proof row repeats {venue_symbol} in its symbol payload"
                    )
                symbol_row = matches[0] if matches else None
                observation = {
                    "snapshot_id": snapshot_id,
                    "snapshot_ordinal": ordinal,
                    "venue_symbol": venue_symbol,
                    "snapshot_revision_id": revision_id,
                    "requested_at": row["requested_at"],
                    "retrieved_at": row["retrieved_at"],
                    "raw_knowledge_time": row["knowledge_time"],
                    "status": None if symbol_row is None else symbol_row.get("status"),
                    "base_asset": None if symbol_row is None else symbol_row.get("base_asset"),
                    "quote_asset": None if symbol_row is None else symbol_row.get("quote_asset"),
                }
                ExchangeInfoRowVerifier._check_bounded_row(observation, max_record_bytes)
                builder.add(observation)
        return builder.finish()


def iter_planned_listing_revisions(
    rows: Iterable[Mapping[str, Any]],
    *,
    max_record_bytes: int,
    finding_sink: Callable[[str, str, Any, Iterable[str], Callable[[int], str]], None],
) -> Iterator[lr.PlannedListing]:
    """Derive the current chain one record at a time from sorted observation rows.

    ``rows`` must be ordered by ``(venue_symbol, retrieved_at, snapshot_revision_id)`` as emitted
    by :func:`listing_observations_run`. Findings are delivered synchronously to ``finding_sink``;
    its revision-ID iterable must be consumed before the callback returns. This is what lets a
    caller feed event/revision writers without collecting tie heads or findings in a tuple.

    The current plan retains one interval list, bounded by the mandatory maximum canonical listing
    row size. Its predecessor link is pruned to one node because the next revision only needs the
    preceding revision ID and observation for its precedence evidence.
    """
    if (
        isinstance(max_record_bytes, bool)
        or not isinstance(max_record_bytes, int)
        or max_record_bytes <= 0
    ):
        raise ValueError("max_record_bytes must be a positive integer")

    def as_observation(row: Mapping[str, Any]) -> lr.Observation:
        return lr.Observation(
            venue_symbol=row["venue_symbol"],
            snapshot_revision_id=row["snapshot_revision_id"],
            requested_at=row["requested_at"],
            retrieved_at=row["retrieved_at"],
            raw_knowledge_time=row["raw_knowledge_time"],
            status=row["status"],
            base_asset=row["base_asset"],
            quote_asset=row["quote_asset"],
        )

    def emit_plan(planned: lr.PlannedListing) -> lr.PlannedListing:
        row = lr.listing_columns(
            planned,
            arrival_seq=0,
            knowledge_time=planned.observation.raw_knowledge_time,
        )
        ExchangeInfoRowVerifier._check_bounded_row(row, max_record_bytes)
        return planned

    for venue_symbol, symbol_rows in groupby(rows, key=lambda row: row["venue_symbol"]):
        if venue_symbol not in lr.FIRST_SLICE_ASSETS:
            raise CatalogIntegrityError(
                f"listing observations contain unknown symbol {venue_symbol!r}"
            )
        base, quote = lr.FIRST_SLICE_ASSETS[venue_symbol]
        canonical = SYMBOLS[venue_symbol].symbol
        instrument = Instrument(
            venue="binance",
            symbol=canonical,
            instrument_type=InstrumentType.SPOT,
            base=base,
            quote=quote,
        )
        current: lr.PlannedListing | None = None
        stopped = False
        for instant, instant_rows in groupby(symbol_rows, key=lambda row: row["retrieved_at"]):
            instant_iterator = iter(instant_rows)
            first_two = list(islice(instant_iterator, 2))
            first_row = first_two[0] if first_two else None
            if first_row is None:
                continue
            if len(first_two) == 2:
                tied_rows = chain(first_two, instant_iterator)
                tie_state = {"count": 0, "exhausted": False}
                ids = _tie_ids(tied_rows, tie_state)

                def tie_detail(count: int, symbol: str = venue_symbol, at: Any = instant) -> str:
                    return (
                        f"{count} snapshots observed {symbol} at {at.isoformat()}: "
                        f"{lr.LISTING_OBSERVATION_ID}@{lr.LISTING_OBSERVATION_VERSION} cannot "
                        "order them; the chain stops here (fail closed)"
                    )

                consumed = _deliver_finding(
                    finding_sink,
                    lr.FINDING_OBSERVATION_TIE,
                    venue_symbol,
                    instant,
                    ids,
                    tie_detail,
                )
                if not tie_state["exhausted"] or tie_state["count"] < 2 or consumed < 2:
                    raise CatalogIntegrityError("listing finding sink did not consume tied IDs")
                stopped = True
                break

            observation = as_observation(first_row)
            kind, code = lr.classify(observation)
            if kind is lr.ObservationClass.UNRESOLVED:
                assert code is not None
                detail = (
                    f"{venue_symbol} at {instant.isoformat()}: "
                    + {
                        lr.FINDING_SYMBOL_MISSING: "absent from the snapshot",
                        lr.FINDING_INSTRUMENT_MISMATCH: (
                            "base / quote assets are not the frozen ones"
                        ),
                        lr.FINDING_STATUS_UNKNOWN: f"status {observation.status!r} has no mapping",
                    }[code]
                    + "; no inference, universe fails closed until the next resolved observation"
                )
                _deliver_finding(
                    finding_sink,
                    code,
                    venue_symbol,
                    instant,
                    iter((observation.snapshot_revision_id,)),
                    _fixed_detail(detail),
                )
                continue

            status = (
                ListingStatus.LISTED
                if kind is lr.ObservationClass.LISTED
                else ListingStatus.SUSPENDED
            )
            if current is None:
                if status is ListingStatus.SUSPENDED:
                    detail = (
                        f"{venue_symbol} observed {observation.status} at {instant.isoformat()} "
                        "before any TRADING observation: no episode exists yet"
                    )
                    _deliver_finding(
                        finding_sink,
                        lr.FINDING_SUSPENDED_BEFORE_TRADING,
                        venue_symbol,
                        instant,
                        iter((observation.snapshot_revision_id,)),
                        _fixed_detail(detail),
                    )
                    continue
                episode = DegradedEpisodeKey(
                    basis=EpisodeIdentityBasis.DEGRADED_SYMBOL_START,
                    venue="binance",
                    instrument_type=InstrumentType.SPOT,
                    symbol=canonical,
                    tradable_from=instant,
                )
                planned = lr._plan(
                    observation,
                    episode,
                    instrument,
                    (TradableInterval(tradable_from=instant, tradable_until=None),),
                    status,
                    lr._reason("first", observation, instant),
                    None,
                )
                current = emit_plan(planned)
                yield current
                continue

            if status is current.status:
                continue
            episode = current.episode
            if status is ListingStatus.SUSPENDED:
                last = current.intervals[-1]
                intervals = (
                    *current.intervals[:-1],
                    TradableInterval(tradable_from=last.tradable_from, tradable_until=instant),
                )
                reason = lr._reason("suspended", observation, episode.tradable_from)
            else:
                intervals = (
                    *current.intervals,
                    TradableInterval(tradable_from=instant, tradable_until=None),
                )
                reason = lr._reason("relisted", observation, episode.tradable_from)
            previous = replace(current, previous=None)
            planned = lr._plan(
                observation, episode, instrument, intervals, status, reason, previous
            )
            current = emit_plan(planned)
            yield current
        if stopped:
            # Drain this symbol's unread rows so the outer groupby advances to the next symbol.
            for _ in symbol_rows:
                pass

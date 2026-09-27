"""Price bars of a persisted, verified Research Dataset → Phase 4 outcomes and Phase 5 backtests.

``OutcomeRequest`` (ADR-0037) and ``BacktestRequest`` (ADR-0038) take price bars as given; their
callers so far built synthetic bars. This module is the **only** path that turns a Research
Dataset's Canonical ``bars_1m`` rows into ``OutcomePriceBar`` / ``PriceBar``. It mirrors
``infrastructure.feature.dataset`` (G2 RT-6): the caller never supplies bars or a manifest object,
only a ``DatasetBuilder`` and a manifest content hash, and every bar is proven before it exists:

1. **manifest** — loaded through ``builder``'s own verifying ``ManifestStore``
   (``load_manifest``: the row's JSON re-hashed, the contract re-validated, every column and the
   whole build re-derived). No persisted manifest under the hash, or one that does not prove, is
   refused (``DatasetBindingError`` / ``CatalogIntegrityError``). There is no ad-hoc entry that
   takes a bare hash on trust;
2. **point spec** — the manifest's ``point_in_time`` must be a point simulation (one PIT view per
   bar). An interval dataset is refused: which revision of a bar a label or a fill may use would
   depend on the view, which a price series cannot carry (a gap left for a later batch);
3. **dataset snapshot** — the dataset's ``klines_1m`` rows are read at the manifest's own
   ``DatasetRef`` snapshot under the selection id the manifest determines (``selection_id_of``).
   No rows, or no rows of a requested symbol, is refused (fail closed on a missing unit);
4. **re-selection** — for each covered symbol, the Canonical bars are re-selected by
   ``PitSelector`` under the manifest's spec over the dataset window (conflict-free) and must
   select **exactly** the dataset's rows of that symbol (an incomplete or padded unit is
   ``CatalogIntegrityError``, as in the feature path); each dataset row's observation key and
   event time are its selected revision's, and every selected revision's lineage is bound by the
   manifest;
5. **mapping** — each proven bar keeps its own interval and its own ``available_time`` as the
   selection carries it (the ADR-0032 effective time only when the manifest's spec binds that
   assumption, the stored one otherwise); OHLC stay ``Decimal`` (``decimal(38, 18)`` columns),
   never floats. Gaps are not filled: a missing minute stays missing (the outcome engine labels
   it ``None``, the backtester keeps the last mark);
6. **cutoff** — ``price_cutoff`` defaults to the manifest's ``simulation_time`` and may not be
   later (the dataset knows no price beyond its PIT view). A bar of the requested window whose
   ``available_time`` is after ``price_cutoff`` is **refused**, never silently dropped: the caller
   narrows the window instead.

``OutcomeRequest`` carries the manifest's content hash and the cutoff; ``BacktestRequest`` has no
slot for either (its schema is frozen), so ``backtest_bars_from_dataset`` returns them next to the
bars in ``DatasetPriceBars`` for the caller's reproducibility record.

``manifest_cache`` (default ``None``: every call re-verifies, as above) is an explicit
``VerifiedManifestCache``: step 1 then reuses a proof of the same manifest by the same builder only
while every snapshot the proof read is unchanged (``infrastructure.bars.verified``). Steps 2 - 6
always run.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from itertools import pairwise
from typing import Any, Final

from pyiceberg.expressions import EqualTo

from core.contracts.feature import FeatureObservation
from core.contracts.outcome import OutcomeEvent, OutcomeLabelSpec, OutcomePriceBar, OutcomeRequest
from core.contracts.revision import PointInTimeStatus
from core.contracts.storage import StorageAdapter
from core.contracts.strategy import PriceBar
from core.contracts.universe import ResearchDatasetManifest, SelectedRevisionLineage
from infrastructure.bars.verified import VerifiedManifestCache, load_verified_manifest
from infrastructure.canonical import rules
from infrastructure.catalog.iceberg_adapter import CatalogIntegrityError
from infrastructure.dataset.builder import DatasetBuilder, selection_id_of
from infrastructure.dataset.selection import SELECTION_SCHEMA
from infrastructure.feature.dataset import DatasetBindingError
from infrastructure.feature.observations import bar_observations
from infrastructure.pit.selector import PitSelector
from infrastructure.revision.store import RevisionCatalog

__all__ = [
    "DatasetBarsError",
    "DatasetPriceBars",
    "backtest_bars_from_dataset",
    "outcome_request_from_dataset",
]

_DATA_TYPE: Final = "klines_1m"
_BARS_TABLE: Final = rules.CANONICAL_TABLES[_DATA_TYPE].table
_COLUMNS: Final = tuple(field.name for field in SELECTION_SCHEMA.fields)
_VENUE_SYMBOL: Final[Mapping[str, str]] = {
    item.symbol: venue for venue, item in rules.SYMBOLS.items()
}
_OHLC: Final = ("open", "high", "low", "close")


class DatasetBarsError(DatasetBindingError):
    """The Research Dataset cannot honestly supply these price bars (fail closed)."""


@dataclass(frozen=True, slots=True)
class _ProvenBar:
    symbol: str
    interval_start: datetime
    interval_end: datetime
    available_time: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal


@dataclass(frozen=True, slots=True)
class DatasetPriceBars:
    """Backtest bars proven against one manifest, with what bound them."""

    manifest_content_hash: str
    price_cutoff: datetime
    bars: tuple[PriceBar, ...]


def outcome_request_from_dataset(
    adapter: RevisionCatalog,
    storage: StorageAdapter,
    *,
    builder: DatasetBuilder,
    manifest_content_hash: str,
    symbol: str,
    label_spec: OutcomeLabelSpec,
    events: Sequence[OutcomeEvent],
    price_cutoff: datetime | None = None,
    start: datetime | None = None,
    end: datetime | None = None,
    manifest_cache: VerifiedManifestCache | None = None,
) -> OutcomeRequest:
    """An ``OutcomeRequest`` for ``symbol`` (Canonical symbol, e.g. ``BTC-USDT``) whose bars are
    the manifest's proven dataset bars in ``[start, end)`` (default: the dataset window)."""
    manifest, cutoff, proven = _proven_bars(
        adapter,
        storage,
        builder,
        manifest_content_hash,
        (symbol,),
        price_cutoff,
        start,
        end,
        manifest_cache,
    )
    bars = tuple(
        OutcomePriceBar(
            interval_start=bar.interval_start,
            interval_end=bar.interval_end,
            available_time=bar.available_time,
            open=bar.open,
            high=bar.high,
            low=bar.low,
            close=bar.close,
        )
        for bar in proven[symbol]
    )
    return OutcomeRequest(
        label_spec=label_spec,
        manifest_content_hash=manifest.content_hash(),
        price_cutoff=cutoff,
        events=tuple(events),
        bars=bars,
    )


def backtest_bars_from_dataset(
    adapter: RevisionCatalog,
    storage: StorageAdapter,
    *,
    builder: DatasetBuilder,
    manifest_content_hash: str,
    symbols: Iterable[str] | None = None,
    price_cutoff: datetime | None = None,
    start: datetime | None = None,
    end: datetime | None = None,
    manifest_cache: VerifiedManifestCache | None = None,
) -> DatasetPriceBars:
    """``PriceBar``s (instrument = Canonical symbol) of ``symbols`` (default: every symbol with
    dataset rows) proven against the manifest, in ``[start, end)``."""
    manifest, cutoff, proven = _proven_bars(
        adapter,
        storage,
        builder,
        manifest_content_hash,
        None if symbols is None else tuple(symbols),
        price_cutoff,
        start,
        end,
        manifest_cache,
    )
    bars = tuple(
        PriceBar(
            instrument=bar.symbol,
            interval_start=bar.interval_start,
            interval_end=bar.interval_end,
            available_time=bar.available_time,
            open=bar.open,
            high=bar.high,
            low=bar.low,
            close=bar.close,
        )
        for symbol in sorted(proven)
        for bar in proven[symbol]
    )
    if not bars:
        raise DatasetBarsError("the dataset has no bars in the requested window")
    return DatasetPriceBars(manifest.content_hash(), cutoff, bars)


# ---------------------------------------------------------------------------------------------


def _proven_bars(
    adapter: RevisionCatalog,
    storage: StorageAdapter,
    builder: DatasetBuilder,
    manifest_content_hash: str,
    symbols: tuple[str, ...] | None,
    price_cutoff: datetime | None,
    start: datetime | None,
    end: datetime | None,
    manifest_cache: VerifiedManifestCache | None,
) -> tuple[ResearchDatasetManifest, datetime, dict[str, tuple[_ProvenBar, ...]]]:
    manifest = load_verified_manifest(builder, manifest_content_hash, manifest_cache)  # step 1
    spec = manifest.point_in_time
    if spec.simulation_time is None:  # step 2
        raise DatasetBarsError(
            "outcome / backtest bars come from a point-simulation dataset (one PIT view)"
        )
    cutoff = spec.simulation_time if price_cutoff is None else price_cutoff
    if cutoff.utcoffset() is None:
        raise DatasetBarsError("price_cutoff must be timezone-aware UTC")
    if cutoff > spec.simulation_time:
        raise DatasetBarsError(
            f"price_cutoff {cutoff.isoformat()} is after the dataset's PIT view "
            f"{spec.simulation_time.isoformat()}"
        )
    dataset = manifest.dataset
    low = dataset.time_range_start if start is None else start
    high = dataset.time_range_end if end is None else end
    if not dataset.time_range_start <= low < high <= dataset.time_range_end:
        raise DatasetBarsError("the requested window must be a non-empty part of the dataset's")

    rows = _dataset_rows(adapter, manifest)  # step 3
    covered = sorted({found[0]["symbol"] for found in rows.values()})
    wanted = covered if symbols is None else sorted(set(symbols))
    if not wanted:
        raise DatasetBarsError("no symbol requested")
    missing = [symbol for symbol in wanted if symbol not in covered]
    if missing:
        raise DatasetBarsError(f"the dataset has no {_DATA_TYPE} rows of {missing}")

    lineage = set(manifest.lineage)
    proven: dict[str, tuple[_ProvenBar, ...]] = {}
    for symbol in wanted:  # steps 4-6
        bars = [
            bar
            for bar in _reselected(adapter, storage, manifest, symbol, rows, lineage)
            if low <= bar.interval_start < high
        ]
        late = [bar for bar in bars if bar.available_time > cutoff]
        if late:
            raise DatasetBarsError(
                f"{len(late)} {symbol} bar(s) of the window become available after price_cutoff "
                f"{cutoff.isoformat()} (first {late[0].interval_start.isoformat()} at "
                f"{late[0].available_time.isoformat()})"
            )
        proven[symbol] = tuple(bars)
    return manifest, cutoff, proven


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
        raise DatasetBarsError(
            f"the dataset snapshot has no {_DATA_TYPE} rows for this manifest (selection "
            f"{selection_id})"
        )
    rows: dict[str, list[Mapping[str, Any]]] = {}
    for row in found:
        if row["canonical_table"] != _BARS_TABLE:
            raise CatalogIntegrityError(f"dataset row of {row['canonical_table']} in a bar dataset")
        rows.setdefault(row["revision_id"], []).append(row)
    return rows


def _reselected(
    adapter: RevisionCatalog,
    storage: StorageAdapter,
    manifest: ResearchDatasetManifest,
    symbol: str,
    rows: Mapping[str, list[Mapping[str, Any]]],
    lineage: set[SelectedRevisionLineage],
) -> list[_ProvenBar]:
    """``symbol``'s bars re-selected under the manifest's spec: exactly its dataset rows."""
    venue = _VENUE_SYMBOL.get(symbol)
    if venue is None:
        raise CatalogIntegrityError(f"dataset rows of an unknown symbol {symbol!r}")
    dataset = manifest.dataset
    spec = manifest.point_in_time
    selection = PitSelector(adapter, storage).select(
        spec, _DATA_TYPE, venue, dataset.time_range_start, dataset.time_range_end
    )
    selection.require_no_conflict()
    selected = {
        item.selected_revision_id
        for item in selection.selections
        if item.status is PointInTimeStatus.SELECTED and item.selected_revision_id is not None
    }
    retained = {revision for revision, found in rows.items() if found[0]["symbol"] == symbol}
    if selected != retained:
        raise CatalogIntegrityError(
            f"the dataset's {symbol} rows are not what its manifest's spec selects "
            f"({len(retained)} rows, {len(selected)} selected)"
        )
    out = [_bar(symbol, item, rows, lineage) for item in bar_observations(selection, spec)]
    out.sort(key=lambda bar: bar.interval_start)
    for earlier, later in pairwise(out):
        if later.interval_start < earlier.interval_end:
            raise CatalogIntegrityError(f"the dataset's {symbol} bars overlap at {later}")
    return out


def _bar(
    symbol: str,
    item: FeatureObservation,
    rows: Mapping[str, list[Mapping[str, Any]]],
    lineage: set[SelectedRevisionLineage],
) -> _ProvenBar:
    revision = item.lineage.canonical_revision_id
    if item.lineage not in lineage:
        raise DatasetBarsError(f"revision {revision}: lineage is not bound by the manifest")
    head = rows[revision][0]
    if (head["observation_key"], head["event_time"]) != (item.observation_key, item.event_time):
        raise CatalogIntegrityError(f"revision {revision}: key / event time differ from its row")
    if item.values.get("symbol") != symbol or item.event_end_time is None:
        raise CatalogIntegrityError(f"revision {revision}: not a {symbol} bar")
    prices: dict[str, Decimal] = {}
    for name in _OHLC:
        value = item.values.get(name)
        if not isinstance(value, Decimal):
            raise CatalogIntegrityError(f"revision {revision}: {name} is not a Decimal")
        prices[name] = value
    return _ProvenBar(
        symbol=symbol,
        interval_start=item.event_time,
        interval_end=item.event_end_time,
        available_time=item.available_time,
        open=prices["open"],
        high=prices["high"],
        low=prices["low"],
        close=prices["close"],
    )

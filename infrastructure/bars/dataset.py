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
   never floats. A backtest ``PriceBar`` also carries the same selected revision's base-asset
   ``volume`` (ADR-0054 §4), the ``Decimal`` exactly as the proven row holds it (never inferred,
   zero-filled or converted); a value that is not a ``Decimal`` is ``CatalogIntegrityError``, as
   for OHLC. ``OutcomePriceBar`` has no volume and gets none. Gaps are not filled: a missing
   minute stays missing (the outcome engine labels it ``None``, the backtester keeps the last
   mark);
6. **cutoff** — ``price_cutoff`` defaults to the manifest's ``simulation_time`` and may not be
   later (the dataset knows no price beyond its PIT view). A bar of the requested window whose
   ``available_time`` is after ``price_cutoff`` is **refused**, never silently dropped: the caller
   narrows the window instead.

Because every dataset ``PriceBar`` carries a non-null ``volume``, a ``BacktestRequest`` built
from dataset bars hashes differently from one built by this module before ADR-0054's volume
mapping (B61; expected: the volume is part of the bar). A ``PriceBar`` without ``volume`` still
omits it, so every other request hash is unchanged.

``OutcomeRequest`` carries the manifest's content hash and the cutoff; ``BacktestRequest`` has no
slot for either (its schema is frozen), so ``backtest_bars_from_dataset`` returns them next to the
bars in ``DatasetPriceBars`` for the caller's reproducibility record.

``manifest_cache`` (default ``None``: every call re-verifies, as above) is an explicit
``VerifiedManifestCache``: step 1 then reuses a proof of the same manifest by the same builder only
while every snapshot the proof read is unchanged (``infrastructure.bars.verified``). Steps 2 - 6
always run.

**v3 evidence manifests (ADR-0077; C1-CONSUMERS).** ``evidence_verifier`` (default ``None``: the
path above, unchanged; a v3 hash is then refused by the store with ``ManifestFormError``) is the
``StreamingEvidenceVerifier`` of the builder's own catalog. Step 1 becomes ``load_verified_any``
(``ManifestStore.load_any``, cache-aware): a v2 hash continues exactly as above; a v3 hash was
proven by the streaming verifier (its bounded re-derivation already re-selected every key under the
spec and matched the chunk table, ADR-0077 §6), so steps 3 - 4 do **not** re-select and hold no
whole-dataset tuple: the dataset's rows are read chunk by chunk in lockstep with the ``lineage`` /
``evidence_gaps`` streams, and each chunk's revisions are read from ``canonical.bars_1m`` at the
spec's bound snapshot (``infrastructure.feature.dataset.iter_dataset_chunks`` /
``dataset_chunk_observations``: every row's key, event time, symbol, lineage and gap must be the
manifest's). The manifest must also record ``data_type == klines_1m``. Steps 2, 5 and 6 are the
same code; the bars equal the v2 path's for the same world (only the manifest hash differs). What
the v3 path holds beyond one chunk is its answer (the requested bars) and the covered symbol names.
``adapter`` must be the catalog the manifest was proven on.

``feature_observations_from_dataset`` gives a v3 manifest's own bar observations of one symbol (the
same chunk walk): what a dataset-backed research loop feeds ``feature_request_from_dataset`` with,
instead of re-selecting the window.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from itertools import pairwise
from pathlib import Path
from typing import Any, Final

from pyiceberg.expressions import EqualTo

from core.contracts.feature import FeatureObservation
from core.contracts.outcome import OutcomeEvent, OutcomeLabelSpec, OutcomePriceBar, OutcomeRequest
from core.contracts.revision import PointInTimeSpec, PointInTimeStatus
from core.contracts.storage import StorageAdapter
from core.contracts.strategy import PriceBar
from core.contracts.universe import (
    ResearchDatasetEvidenceManifest,
    ResearchDatasetManifest,
    SelectedRevisionLineage,
)
from core.domain.specs import DatasetRef
from infrastructure.bars.verified import (
    VerifiedManifestCache,
    load_verified_any,
    load_verified_manifest,
)
from infrastructure.canonical import rules
from infrastructure.catalog.iceberg_adapter import CatalogIntegrityError
from infrastructure.dataset.builder import DatasetBuilder, selection_id_of
from infrastructure.dataset.selection import SELECTION_SCHEMA
from infrastructure.dataset.verify_v3 import StreamingEvidenceVerifier
from infrastructure.feature.dataset import (
    AnyDatasetManifest,
    DatasetBindingError,
    dataset_chunk_observations,
    iter_dataset_chunks,
)
from infrastructure.feature.observations import bar_observations
from infrastructure.pit.selector import PitSelector
from infrastructure.revision.store import RevisionCatalog

__all__ = [
    "DatasetBarsError",
    "DatasetPriceBars",
    "backtest_bars_from_dataset",
    "feature_observations_from_dataset",
    "outcome_request_from_dataset",
]

_DATA_TYPE: Final = "klines_1m"
_BARS_TABLE: Final = rules.CANONICAL_TABLES[_DATA_TYPE].table
_COLUMNS: Final = tuple(field.name for field in SELECTION_SCHEMA.fields)
_VENUE_SYMBOL: Final[Mapping[str, str]] = {
    item.symbol: venue for venue, item in rules.SYMBOLS.items()
}
_OHLC: Final = ("open", "high", "low", "close")
_VOLUME: Final = "volume"


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
    #: base-asset volume of the same selected revision (``PriceBar.volume``, ADR-0054 §4)
    volume: Decimal


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
    evidence_verifier: StreamingEvidenceVerifier | None = None,
) -> OutcomeRequest:
    """An ``OutcomeRequest`` for ``symbol`` (Canonical symbol, e.g. ``BTC-USDT``) whose bars are
    the manifest's proven dataset bars in ``[start, end)`` (default: the dataset window).

    ``evidence_verifier``: module docs (v3); ``None`` is the v2 path, unchanged."""
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
        evidence_verifier,
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
    evidence_verifier: StreamingEvidenceVerifier | None = None,
) -> DatasetPriceBars:
    """``PriceBar``s (instrument = Canonical symbol) of ``symbols`` (default: every symbol with
    dataset rows) proven against the manifest, in ``[start, end)``.

    ``evidence_verifier``: module docs (v3); ``None`` is the v2 path, unchanged."""
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
        evidence_verifier,
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
            volume=bar.volume,
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
    evidence_verifier: StreamingEvidenceVerifier | None = None,
) -> tuple[AnyDatasetManifest, datetime, dict[str, tuple[_ProvenBar, ...]]]:
    manifest: AnyDatasetManifest
    if evidence_verifier is None:
        manifest = load_verified_manifest(builder, manifest_content_hash, manifest_cache)  # step 1
    else:
        manifest = load_verified_any(
            builder, manifest_content_hash, manifest_cache, evidence_verifier
        )
        if isinstance(manifest, ResearchDatasetEvidenceManifest):
            spec, dataset = manifest.point_in_time, manifest.dataset
            cutoff, low, high = _bounds(spec, dataset, price_cutoff, start, end)  # steps 2, 6
            if manifest.data_type != _DATA_TYPE:
                raise DatasetBarsError(f"a {manifest.data_type} dataset has no {_DATA_TYPE} bars")
            bars = _evidence_bars(adapter, manifest, evidence_verifier, symbols, cutoff, low, high)
            return manifest, cutoff, bars
    cutoff, low, high = _bounds(manifest.point_in_time, manifest.dataset, price_cutoff, start, end)

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
            for bar in _reselected(
                adapter,
                storage,
                manifest,
                symbol,
                rows,
                lineage,
                canonical_scratch_directory=builder.canonical_scratch_directory,
            )
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


def _bounds(
    spec: PointInTimeSpec,
    dataset: DatasetRef,
    price_cutoff: datetime | None,
    start: datetime | None,
    end: datetime | None,
) -> tuple[datetime, datetime, datetime]:
    """Steps 2 and 6: a point spec, the cutoff (not after its view) and the requested window."""
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
    low = dataset.time_range_start if start is None else start
    high = dataset.time_range_end if end is None else end
    if not dataset.time_range_start <= low < high <= dataset.time_range_end:
        raise DatasetBarsError("the requested window must be a non-empty part of the dataset's")
    return cutoff, low, high


def _evidence_bars(
    adapter: RevisionCatalog,
    manifest: ResearchDatasetEvidenceManifest,
    evidence_verifier: StreamingEvidenceVerifier,
    symbols: tuple[str, ...] | None,
    cutoff: datetime,
    low: datetime,
    high: datetime,
) -> dict[str, tuple[_ProvenBar, ...]]:
    """Steps 3 - 6 over a verified v3 manifest (module docs, v3): one chunk walk, no
    re-selection. Held: one chunk, the covered symbol names and the requested bars."""
    wanted = None if symbols is None else frozenset(symbols)
    if wanted is not None and not wanted:
        raise DatasetBarsError("no symbol requested")
    covered: set[str] = set()
    found: dict[str, list[_ProvenBar]] = {}
    with iter_dataset_chunks(adapter, manifest, evidence_verifier) as chunks:  # step 3
        for chunk in chunks:
            covered.update(item.row["symbol"] for item in chunk)
            for item, observation in dataset_chunk_observations(adapter, manifest, chunk, wanted):
                symbol = item.row["symbol"]
                bar = _mapped(symbol, observation)  # steps 4 - 5 (proven by the chunk walk)
                if low <= bar.interval_start < high:
                    found.setdefault(symbol, []).append(bar)
    names = sorted(covered) if wanted is None else sorted(wanted)
    missing = [symbol for symbol in names if symbol not in covered]
    if missing:
        raise DatasetBarsError(f"the dataset has no {_DATA_TYPE} rows of {missing}")
    proven: dict[str, tuple[_ProvenBar, ...]] = {}
    for symbol in names:
        bars = sorted(found.get(symbol, ()), key=lambda bar: bar.interval_start)
        for earlier, later in pairwise(bars):
            if later.interval_start < earlier.interval_end:
                raise CatalogIntegrityError(f"the dataset's {symbol} bars overlap at {later}")
        late = [bar for bar in bars if bar.available_time > cutoff]  # step 6
        if late:
            raise DatasetBarsError(
                f"{len(late)} {symbol} bar(s) of the window become available after price_cutoff "
                f"{cutoff.isoformat()} (first {late[0].interval_start.isoformat()} at "
                f"{late[0].available_time.isoformat()})"
            )
        proven[symbol] = tuple(bars)
    return proven


def feature_observations_from_dataset(
    adapter: RevisionCatalog,
    *,
    builder: DatasetBuilder,
    manifest_content_hash: str,
    symbol: str,
    evidence_verifier: StreamingEvidenceVerifier,
    manifest_cache: VerifiedManifestCache | None = None,
) -> tuple[FeatureObservation, ...]:
    """A verified **v3** manifest's own bar observations of ``symbol`` (Canonical), in revision
    order as ``bar_observations`` gives them: exactly what ``feature_request_from_dataset``
    proves against (module docs, v3). A v2 manifest is refused (its observations come from a PIT
    selection under its spec)."""
    manifest = load_verified_any(builder, manifest_content_hash, manifest_cache, evidence_verifier)
    if not isinstance(manifest, ResearchDatasetEvidenceManifest):
        raise DatasetBindingError(
            f"manifest {manifest_content_hash} is not a v3 evidence manifest (a v2 manifest's "
            "observations come from its PIT selection)"
        )
    if manifest.data_type != _DATA_TYPE:
        raise DatasetBarsError(f"a {manifest.data_type} dataset has no {_DATA_TYPE} bars")
    wanted = frozenset((symbol,))
    out: list[FeatureObservation] = []
    with iter_dataset_chunks(adapter, manifest, evidence_verifier) as chunks:
        for chunk in chunks:
            for _, observation in dataset_chunk_observations(adapter, manifest, chunk, wanted):
                out.append(observation)
    out.sort(key=lambda item: item.lineage.canonical_revision_id)
    return tuple(out)


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
    *,
    canonical_scratch_directory: Path,
) -> list[_ProvenBar]:
    """``symbol``'s bars re-selected under the manifest's spec: exactly its dataset rows."""
    venue = _VENUE_SYMBOL.get(symbol)
    if venue is None:
        raise CatalogIntegrityError(f"dataset rows of an unknown symbol {symbol!r}")
    dataset = manifest.dataset
    spec = manifest.point_in_time
    selection = PitSelector(
        adapter, storage, canonical_scratch_directory=canonical_scratch_directory
    ).select(spec, _DATA_TYPE, venue, dataset.time_range_start, dataset.time_range_end)
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
    return _mapped(symbol, item)


def _mapped(symbol: str, item: FeatureObservation) -> _ProvenBar:
    """One proven bar observation as a ``_ProvenBar`` (step 5)."""
    revision = item.lineage.canonical_revision_id
    if item.values.get("symbol") != symbol or item.event_end_time is None:
        raise CatalogIntegrityError(f"revision {revision}: not a {symbol} bar")
    prices: dict[str, Decimal] = {}
    for name in _OHLC:
        value = item.values.get(name)
        if not isinstance(value, Decimal):
            raise CatalogIntegrityError(f"revision {revision}: {name} is not a Decimal")
        prices[name] = value
    volume = item.values.get(_VOLUME)
    if not isinstance(volume, Decimal):
        raise CatalogIntegrityError(f"revision {revision}: {_VOLUME} is not a Decimal")
    return _ProvenBar(
        symbol=symbol,
        interval_start=item.event_time,
        interval_end=item.event_end_time,
        available_time=item.available_time,
        open=prices["open"],
        high=prices["high"],
        low=prices["low"],
        close=prices["close"],
        volume=volume,
    )

"""Round data from verified Research Dataset manifests (ADR-0049 implementation note, dataset-backed
loop, 2026-09-26).

The synthetic ``IngestStage`` generates each round's market. ``DatasetIngestStage`` is the second
round data source: each round reads the data a **declared** ``DatasetRound`` names — never bare
hashes taken on trust, every manifest is loaded through the ``DatasetBuilder``'s own verifying
``ManifestStore``:

1. **cutoff** — the price manifest is loaded first (``load_manifest``, verified); its view
   (``simulation_time``, which the pair rule makes the feature interval's end) must not be after
   the round's ``as_of``: no data past the round's cutoff enters the round. Every research bar and
   every feature observation is re-checked against it;
2. **research window** — the data window must lie inside the Profile's research window
   ``[research_window_start, sealed_oos_boundary)``: a research manifest never holds a sealed-window
   (or pre-research) row, so no sealed observation can reach a feature request (which must carry
   every dataset row of its symbol) or a backtest;
3. **manifest pair** — ``infrastructure.bars.pair_manifests`` loads and verifies the round's
   feature (interval simulation) and price (point simulation) manifests and proves they describe
   the same market data, the price view at the end of the feature interval (backlog E1); the
   feature manifest is then loaded again (verified) for its PIT spec;
4. **bars** — the backtest bars come from ``infrastructure.bars.backtest_bars_from_dataset`` (the
   price manifest, proven bar by bar, none available after the price cutoff);
5. **features** — the round's feature observations are the interval manifest's
   ``bar_observations`` of its own PIT selection; the state stage turns them into a request only
   through ``infrastructure.feature.feature_request_from_dataset`` (proven to be exactly the
   dataset's rows, membership checked), evaluated at every research bar's close;
6. **sealed window** — an optional ``sealed_manifest_hash`` (a point manifest over the Profile's
   sealed OOS window) is loaded like the bars and split by the fixed calendar exactly as the
   synthetic ingest splits its market: sealed-window bars are **withheld** in ``SealedBars`` and
   never join the research data, other bars are unused (counted).

The accumulated research data of a round is its own pair's research bars: each round's pair covers
the research window up to that round's cutoff, so the data grows with the rounds as the synthetic
loop's accumulated window does, and re-evaluations see the grown data.

``DatasetSegment`` is the ``RoundData`` the later stages read: the validator gets the proven bars
(``dataset_bars``), the ``ManifestPair`` and the manifest hash of every feature request behind the
signals, so ``G0.manifest_binding`` runs on every report; the reproducibility tuple names both
manifests' ``DatasetRef``.

**Not wired** (fail closed): G5 on dataset rounds would need signals over the released sealed bars,
hence a sealed-window feature manifest; ``DatasetLoopConfig`` refuses an ``OosUnsealBudget`` and
``signals_with`` refuses, so the sealed window simply stays sealed.
"""

from __future__ import annotations

from collections.abc import Mapping, MutableMapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from functools import cached_property
from typing import Any, Final

from apps.worker.loop import RoundContext, StageResult, StageUsage
from core.contracts.feature import FeatureObservation, FeatureProvider
from core.contracts.storage import StorageAdapter
from core.contracts.strategy import PriceBar, SignalObservation
from core.contracts.universe import ResearchDatasetManifest
from core.contracts.validation_profile import ValidationProfile
from core.domain.base import content_hash
from core.domain.specs import DatasetRef, FeatureSpec
from infrastructure.bars import (
    PAIR_RULE,
    PAIR_RULE_HASH,
    DatasetPriceBars,
    ManifestPair,
    backtest_bars_from_dataset,
    pair_manifests,
)
from infrastructure.canonical import rules
from infrastructure.dataset.builder import DatasetBuilder
from infrastructure.feature.dataset import feature_request_from_dataset, load_manifest
from infrastructure.feature.observations import bar_observations
from infrastructure.feature.runner import run_feature
from infrastructure.pit.selector import PitSelector
from infrastructure.revision.store import RevisionCatalog
from infrastructure.strategy.signals import signals_from_features
from research.loop.segment import FeatureRuns, SealedBars, decision_grid
from research.validation.sealed_oos import SealedOosLocked
from research.validation.splits import midnight_utc

__all__ = [
    "DATASET_SOURCE",
    "DatasetCatalog",
    "DatasetIngestStage",
    "DatasetRound",
    "DatasetRoundRefused",
    "DatasetSegment",
]

#: The ``source`` label of a dataset-backed round (ingest summary, fingerprint).
DATASET_SOURCE: Final = "research_dataset"
#: ``plugin_versions`` key name of the manifest-pairing rule (a plugin key is ``name@semver``).
PAIR_PLUGIN: Final = "research_dataset_manifest_pair"
_DATA_TYPE: Final = "klines_1m"
_VENUE: Final[Mapping[str, str]] = {item.symbol: venue for venue, item in rules.SYMBOLS.items()}


class DatasetRoundRefused(ValueError):
    """A round's declared manifests cannot honestly supply its data (fail closed)."""


@dataclass(frozen=True, slots=True)
class DatasetRound:
    """The manifests one round reads: a feature / price pair and optionally the sealed window's.

    Hashes are declarations, never trusted: the ingest loads every one through the builder's
    verifying ``ManifestStore`` and refuses the round when any does not prove (module docs).
    """

    feature_manifest_hash: str
    price_manifest_hash: str
    sealed_manifest_hash: str | None = None

    def __post_init__(self) -> None:
        for name in ("feature_manifest_hash", "price_manifest_hash"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} must be a manifest content hash")
        sealed = self.sealed_manifest_hash
        if sealed is not None and (not isinstance(sealed, str) or not sealed.strip()):
            raise ValueError("sealed_manifest_hash must be a manifest content hash or None")

    def payload(self) -> dict[str, str | None]:
        return {
            "feature_manifest_hash": self.feature_manifest_hash,
            "price_manifest_hash": self.price_manifest_hash,
            "sealed_manifest_hash": self.sealed_manifest_hash,
        }


@dataclass(frozen=True)
class DatasetCatalog:
    """The live catalog handles the dataset ingest reads through (not configuration)."""

    adapter: RevisionCatalog
    storage: StorageAdapter
    builder: DatasetBuilder


def _iso(moment: datetime | None) -> str | None:
    return None if moment is None else moment.isoformat()


@dataclass(frozen=True)
class DatasetSegment:
    """One dataset-backed round: the pair's proven research bars and the withheld sealed bars."""

    catalog: DatasetCatalog
    symbol: str
    pair: ManifestPair
    feature_manifest: ResearchDatasetManifest
    price_manifest: ResearchDatasetManifest
    prices: DatasetPriceBars
    observations: tuple[FeatureObservation, ...]
    sealed: SealedBars[PriceBar]
    sealed_manifest_hash: str | None
    decision_times: tuple[datetime, ...]

    @property
    def research_bars(self) -> tuple[PriceBar, ...]:
        return self.prices.bars

    @property
    def research_start(self) -> datetime | None:
        return self.research_bars[0].interval_start if self.research_bars else None

    @property
    def research_end(self) -> datetime | None:
        return self.research_bars[-1].interval_end if self.research_bars else None

    @cached_property
    def data_hash(self) -> str:
        return content_hash(
            {
                "manifest_pair": self.pair.pair_hash,
                "symbol": self.symbol,
                "start": _iso(self.research_start),
                "end": _iso(self.research_end),
                "bars": len(self.research_bars),
            }
        )

    @property
    def manifest_content_hash(self) -> str:
        """The price manifest: the validator's labels are about exactly its proven bars."""
        return self.prices.manifest_content_hash

    @property
    def plugins(self) -> Mapping[str, str]:
        """The manifest-pairing rule (``hlens.dataset.manifest-pair``) under a plugin key."""
        return {f"{PAIR_PLUGIN}@{PAIR_RULE['version']}": PAIR_RULE_HASH}

    def source_fields(self) -> dict[str, Any]:
        return {
            "feature_manifest_hash": self.pair.feature_manifest_hash,
            "price_manifest_hash": self.pair.price_manifest_hash,
            "manifest_pair_hash": self.pair.pair_hash,
        }

    def dataset_snapshots(self, as_of: datetime) -> tuple[DatasetRef, ...]:
        return (self.feature_manifest.dataset, self.price_manifest.dataset)

    def bar_volume(self) -> Mapping[tuple[str, datetime], Decimal]:
        return {
            (self.symbol, row.event_time): volume
            for row in self.observations
            if isinstance(volume := row.values.get("volume"), Decimal)
        }

    def validator_binding(self, feature_manifest_hashes: Sequence[str]) -> dict[str, Any]:
        return {
            "dataset_bars": self.prices,
            "manifest_pair": self.pair,
            "feature_manifest_hashes": tuple(feature_manifest_hashes),
        }

    def as_price_bars(self, bars: Sequence[Any]) -> tuple[PriceBar, ...]:
        return tuple(bars)

    def sealed_manifest_label(self) -> str:
        return content_hash(
            {"research": self.manifest_content_hash, "sealed": self.sealed_manifest_hash}
        )

    def feature_runs(
        self,
        provider: FeatureProvider,
        spec: FeatureSpec,
        *,
        chunk: int,
        cache: MutableMapping[str, Any],
    ) -> FeatureRuns:
        """One request over the interval manifest's observations, built only by
        ``feature_request_from_dataset`` and evaluated at every research bar's close (``chunk``
        does not apply: the request must carry every dataset row of the symbol)."""
        times = tuple(bar.interval_end for bar in self.research_bars)
        if not times:
            return (), (), _no_sealed_signals
        key = content_hash(
            {
                "pair": self.pair.pair_hash,
                "feature": spec.content_hash(),
                "research": self.data_hash,
            }
        )
        cached = cache.get(key)
        if cached is None:
            request = feature_request_from_dataset(
                self.catalog.adapter,
                self.catalog.storage,
                builder=self.catalog.builder,
                manifest_content_hash=self.pair.feature_manifest_hash,
                pit_spec=self.feature_manifest.point_in_time,
                observations=self.observations,
                feature=spec,
                evaluation_times=times,
            )
            result = run_feature(provider, spec, request)
            signals = signals_from_features(
                result, feature=spec.ref, instrument=self.symbol, knowledge_time=times[-1]
            )
            cached = (((request, result),), signals)
            cache[key] = cached
        return cached[0], cached[1], _no_sealed_signals


def _no_sealed_signals(extra: Sequence[Any]) -> tuple[SignalObservation, ...]:
    raise SealedOosLocked(
        "G5 on dataset-backed rounds is not wired (it needs a sealed-window feature manifest): "
        "the sealed window stays sealed"
    )


def _refuse(ctx: RoundContext, message: str) -> DatasetRoundRefused:
    return DatasetRoundRefused(
        f"round {ctx.round_index} (as of {ctx.as_of.isoformat()}): {message}"
    )


class DatasetIngestStage:
    """Each round's data from its declared, verified manifests (see module docs)."""

    name = "ingest"

    def __init__(
        self,
        catalog: DatasetCatalog,
        profile: ValidationProfile,
        *,
        symbol: str,
        rounds: Sequence[DatasetRound],
        compute_seconds: Decimal,
        decision_step: timedelta,
        decision_warmup: timedelta,
        label_horizon: timedelta,
    ) -> None:
        if symbol not in _VENUE:
            raise ValueError(f"{symbol!r} is not a Canonical symbol")
        self._catalog = catalog
        self._profile = profile
        self._symbol = symbol
        self._rounds = tuple(rounds)
        self._compute = compute_seconds
        self._step = decision_step
        self._warmup = decision_warmup
        self._horizon = label_horizon

    def estimate(self, ctx: RoundContext) -> StageUsage:
        return StageUsage(compute_seconds=self._compute)

    def run(self, ctx: RoundContext) -> StageResult:
        if ctx.round_index >= len(self._rounds):
            raise _refuse(ctx, f"only {len(self._rounds)} dataset round(s) are declared")
        declared = self._rounds[ctx.round_index]
        catalog, symbol = self._catalog, self._symbol
        split = self._profile.data_split
        start = midnight_utc(split.research_window_start)
        boundary = midnight_utc(split.sealed_oos_boundary)
        window = (boundary, boundary + split.sealed_oos_length)

        # The price manifest first (verified): a round past its cutoff or outside the research
        # window is refused before anything else is read (module docs, steps 1 - 3).
        price = load_manifest(catalog.builder, declared.price_manifest_hash)
        view = price.point_in_time.simulation_time
        if view is None:
            raise _refuse(ctx, "the price manifest is not a point simulation")
        # 1. no data past the round's cutoff
        if view > ctx.as_of:
            raise _refuse(ctx, f"the price view {view.isoformat()} is after the round's cutoff")
        # 2. the research window only: never a sealed-window row in a research manifest (the
        # pair rule makes the feature manifest's data window the same)
        data = price.dataset
        if data.time_range_start < start or data.time_range_end > boundary:
            raise _refuse(
                ctx,
                f"the pair's data window [{data.time_range_start.isoformat()}, "
                f"{data.time_range_end.isoformat()}) is not inside the research window "
                f"[{start.isoformat()}, {boundary.isoformat()})",
            )
        # 3. the verified pair, then the feature manifest (verified again) for its PIT spec
        pair = pair_manifests(
            catalog.builder, declared.feature_manifest_hash, declared.price_manifest_hash
        )
        feature = load_manifest(catalog.builder, pair.feature_manifest_hash)
        if (feature.dataset.time_range_start, feature.dataset.time_range_end) != (
            data.time_range_start,
            data.time_range_end,
        ):  # proven by pair_manifests; re-checked because the window gates the sealed data
            raise _refuse(ctx, "the feature and price manifests cover different windows")
        # 4. proven backtest bars of the price manifest
        prices = backtest_bars_from_dataset(
            catalog.adapter,
            catalog.storage,
            builder=catalog.builder,
            manifest_content_hash=pair.price_manifest_hash,
            symbols=(symbol,),
        )
        late = [bar for bar in prices.bars if bar.available_time > ctx.as_of]
        outside = [bar for bar in prices.bars if bar.interval_end > boundary]
        if prices.price_cutoff > ctx.as_of or late or outside:
            raise _refuse(ctx, "a research bar is after the cutoff or the sealed OOS boundary")
        # 5. the interval manifest's own observations of the symbol
        selection = PitSelector(catalog.adapter, catalog.storage).select(
            feature.point_in_time,
            _DATA_TYPE,
            _VENUE[symbol],
            data.time_range_start,
            data.time_range_end,
        )
        selection.require_no_conflict()
        rows = bar_observations(selection, feature.point_in_time)
        if any(row.available_time > ctx.as_of for row in rows):
            raise _refuse(ctx, "a feature observation becomes available after the cutoff")
        # 6. the sealed window: withheld, never research data
        sealed: list[PriceBar] = []
        unused = 0
        if declared.sealed_manifest_hash is not None:
            held = backtest_bars_from_dataset(
                catalog.adapter,
                catalog.storage,
                builder=catalog.builder,
                manifest_content_hash=declared.sealed_manifest_hash,
                symbols=(symbol,),
            )
            if held.price_cutoff > ctx.as_of:
                raise _refuse(ctx, "the sealed manifest's view is after the round's cutoff")
            for bar in held.bars:
                if bar.interval_start >= window[0] and bar.interval_end <= window[1]:
                    sealed.append(bar)
                else:
                    unused += 1
        segment = DatasetSegment(
            catalog=catalog,
            symbol=symbol,
            pair=pair,
            feature_manifest=feature,
            price_manifest=price,
            prices=prices,
            observations=rows,
            sealed=SealedBars(sealed, window),
            sealed_manifest_hash=declared.sealed_manifest_hash,
            decision_times=decision_grid(
                prices.bars, step=self._step, warmup=self._warmup, horizon=self._horizon
            ),
        )
        summary = {
            "source": DATASET_SOURCE,
            "symbol": symbol,
            "feature_manifest_hash": pair.feature_manifest_hash,
            "price_manifest_hash": pair.price_manifest_hash,
            "manifest_pair_hash": pair.pair_hash,
            "pair_rule_hash": PAIR_RULE_HASH,
            "sealed_manifest_hash": declared.sealed_manifest_hash,
            "data_window": [data.time_range_start.isoformat(), data.time_range_end.isoformat()],
            "price_view": view.isoformat(),
            "price_cutoff": prices.price_cutoff.isoformat(),
            "latest_available_time": max(bar.available_time for bar in prices.bars).isoformat(),
            "feature_observations": len(rows),
            "research_bars": len(prices.bars),
            "accumulated_research_bars": len(prices.bars),
            "sealed_bars_withheld": len(sealed),
            "unused_bars": unused,
            "research_data_hash": segment.data_hash,
            "decision_times": len(segment.decision_times),
        }
        return StageResult(summary, self.estimate(ctx), {"segment": segment})

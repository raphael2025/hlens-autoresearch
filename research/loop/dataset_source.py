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
6. **sealed window** — either (a) an optional ``sealed_manifest_hash`` (a point manifest over the
   Profile's sealed OOS window), loaded like the bars and split by the fixed calendar exactly as
   the synthetic ingest splits its market: sealed-window bars are **withheld** and never join the
   research data, other bars are unused (counted); G5 cannot run on it (no sealed-window
   features), so it stays sealed; or (b) a declared **sealed manifest pair**
   (``sealed_feature_manifest_hash`` + ``sealed_price_manifest_hash``), which the ingest does
   **not read at all** (``SealedDatasetPair``, below).

The accumulated research data of a round is its own pair's research bars: each round's pair covers
the research window up to that round's cutoff, so the data grows with the rounds as the synthetic
loop's accumulated window does, and re-evaluations see the grown data.

``DatasetSegment`` is the ``RoundData`` the later stages read: the validator gets the proven bars
(``dataset_bars``), the ``ManifestPair`` and the manifest hash of every feature request behind the
signals, so ``G0.manifest_binding`` runs on every report; the reproducibility tuple names both
manifests' ``DatasetRef``.

``DatasetCatalog.manifest_cache`` (default ``None``: every load re-verifies) is an optional,
explicit ``infrastructure.bars.VerifiedManifestCache`` for the loads of steps 1, 3, 4 and 6: a
proof is reused only by the same builder while every snapshot it read is unchanged, so the rounds'
data, summaries and record hashes are identical with or without it. The feature request's own load
(``feature_request_from_dataset``, Phase 1) always re-verifies, and so do the sealed pair's
loads (G5, below).

Sealed OOS (G5) on dataset rounds (ADR-0049 implementation note, dataset G5, 2026-09-26). A round
that declares a sealed manifest pair hands the validation stage a ``SealedDatasetPair``. Nothing of
it is read before the family's single evaluation is claimed (``SealedOosVault.claim_evaluation``,
which records the evaluation as consumed): before the claim the stage only asks ``evaluable`` —
decided from the declaration, the Profile's window and the round's cutoff (the window must have
ended by ``as_of``), never from storage. ``release`` (after the claim) then:

1. loads both sealed manifests through the builder's verifying ``ManifestStore``;
2. proves they share the research pair's upstream state: equal ``snapshot_bindings``, equal
   ``knowledge_cutoff``, the same ADR-0032 assumption choice and equal availability, precedence,
   parser and PIT-rule bindings, the same universe spec binding, the same Research Dataset table
   and the same set of Canonical tables in the lineage;
3. proves they cover **exactly** the Profile's sealed window (data window
   ``[boundary, boundary + sealed_oos_length)``) and that the price view is known by the round's
   cutoff and not before the window's end (``window end <= view <= as_of``);
4. proves the two are one chain with ``pair_manifests`` (the research pair's own rule);
5. reads the proven sealed bars (``backtest_bars_from_dataset``) and the sealed feature
   manifest's own observations; each is re-checked against the cutoff.

Any refusal raises ``SealedDataRefused`` after the claim: the G5 report is ``INCONCLUSIVE`` with
``consumed_without_result:sealed_data_refused`` and the family's window stays closed (fail closed:
a misdeclared pair costs the family its unsealing; it is never re-opened). ``signals_with`` then
runs the feature over the sealed feature manifest only through ``feature_request_from_dataset``
(evaluated at every released bar's close; the request must carry every dataset row of the symbol
and no other, so no feature bridges the boundary: the first sealed evaluations are not computable
and a strategy warms up again inside the sealed window — conservative; a window shorter than the
warm-up ends as ``consumed_without_result``), and ``sealed_binding``
gives the validator the sealed ``DatasetPriceBars``, the sealed ``ManifestPair`` and the sealed
feature requests' manifest hashes, so the G5 report carries ``G0.manifest_binding`` for the sealed
pair (and for the research bars G5 re-runs on).

**Not provable across the two pairs from manifest fields** (documented, not checked): the member /
exclusion sets (a listing may legitimately change between the research and the sealed window; the
bar path refuses a symbol without rows), the quality report ids (per-day partitions differ by
construction), lineage revisions and evidence gaps (different data). Within the sealed pair,
``pair_manifests`` checks all of them.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, MutableMapping, Sequence
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
    VerifiedManifestCache,
    backtest_bars_from_dataset,
    load_verified_manifest,
    pair_manifests,
)
from infrastructure.canonical import rules
from infrastructure.dataset.builder import DatasetBuilder
from infrastructure.feature.dataset import (
    DatasetBindingError,
    feature_request_from_dataset,
    load_manifest,
)
from infrastructure.feature.observations import bar_observations
from infrastructure.feature.runner import run_feature
from infrastructure.pit.assumption import assumption_bound
from infrastructure.pit.selector import PitSelector
from infrastructure.revision.store import RevisionCatalog
from infrastructure.strategy.signals import signals_from_features
from research.loop.segment import (
    FeatureRuns,
    SealedBars,
    SealedDataRefused,
    SealedSource,
    decision_grid,
)
from research.validation.sealed_oos import SealedEvaluation, SealedOosLocked
from research.validation.splits import midnight_utc

__all__ = [
    "DATASET_SOURCE",
    "DatasetCatalog",
    "DatasetIngestStage",
    "DatasetRound",
    "DatasetRoundRefused",
    "DatasetSegment",
    "SealedDatasetPair",
    "WithheldSealedBars",
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

    The sealed window is declared either as ``sealed_manifest_hash`` (a point manifest whose bars
    are withheld; G5 cannot run on it) or as a sealed **pair** (``sealed_feature_manifest_hash`` +
    ``sealed_price_manifest_hash``: an interval and a point manifest over exactly the Profile's
    sealed window, read only after a claimed G5 evaluation), never both. Hashes are declarations,
    never trusted: every one is loaded through the builder's verifying ``ManifestStore``, and the
    round (the claimed evaluation, for the sealed pair) is refused when any does not prove.
    """

    feature_manifest_hash: str
    price_manifest_hash: str
    sealed_manifest_hash: str | None = None
    sealed_feature_manifest_hash: str | None = None
    sealed_price_manifest_hash: str | None = None

    def __post_init__(self) -> None:
        for name in ("feature_manifest_hash", "price_manifest_hash"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} must be a manifest content hash")
        for name in (
            "sealed_manifest_hash",
            "sealed_feature_manifest_hash",
            "sealed_price_manifest_hash",
        ):
            value = getattr(self, name)
            if value is not None and (not isinstance(value, str) or not value.strip()):
                raise ValueError(f"{name} must be a manifest content hash or None")
        if (self.sealed_feature_manifest_hash is None) != (self.sealed_price_manifest_hash is None):
            raise ValueError("a sealed manifest pair needs both its feature and price manifest")
        if self.has_sealed_pair and self.sealed_manifest_hash is not None:
            raise ValueError(
                "declare the sealed window either as sealed_manifest_hash (withheld only) or as "
                "a sealed manifest pair, not both"
            )

    @property
    def has_sealed_pair(self) -> bool:
        return self.sealed_feature_manifest_hash is not None

    def payload(self) -> dict[str, str | None]:
        """The declaration (fingerprint). The sealed pair's keys appear only when a pair is
        declared, so a round without one keeps its earlier payload."""
        out = {
            "feature_manifest_hash": self.feature_manifest_hash,
            "price_manifest_hash": self.price_manifest_hash,
            "sealed_manifest_hash": self.sealed_manifest_hash,
        }
        if self.has_sealed_pair:
            out["sealed_feature_manifest_hash"] = self.sealed_feature_manifest_hash
            out["sealed_price_manifest_hash"] = self.sealed_price_manifest_hash
        return out


@dataclass(frozen=True)
class DatasetCatalog:
    """The live catalog handles the dataset ingest reads through (not configuration).

    ``manifest_cache``: an optional ``VerifiedManifestCache`` for this builder's verified loads
    (module docs); ``None`` re-verifies every load.
    """

    adapter: RevisionCatalog
    storage: StorageAdapter
    builder: DatasetBuilder
    manifest_cache: VerifiedManifestCache | None = None


def _iso(moment: datetime | None) -> str | None:
    return None if moment is None else moment.isoformat()


class WithheldSealedBars(SealedBars[PriceBar]):
    """The sealed-window bars of a withheld-only declaration (``sealed_manifest_hash``, or none).

    Never evaluable: without a sealed-window feature manifest there are no signals over the
    sealed bars, so G5 cannot run and the window stays sealed (the bars are only counted).
    """

    def evaluable(self, as_of: datetime) -> str | None:
        return "no sealed manifest pair is declared for this round (the sealed window stays sealed)"

    def release(self, evaluation: SealedEvaluation) -> tuple[PriceBar, ...]:
        raise SealedOosLocked("a withheld-only sealed window is never released")


@dataclass(frozen=True)
class _ReleasedSealed:
    """What one claimed evaluation read of a sealed pair (after the claim)."""

    pair: ManifestPair
    feature_manifest: ResearchDatasetManifest
    prices: DatasetPriceBars
    observations: tuple[FeatureObservation, ...]
    feature_hashes: list[str]


class SealedDatasetPair:
    """A round's declared sealed-window manifest pair, read only after a claimed evaluation.

    See the module docs ("Sealed OOS (G5) on dataset rounds"). Before ``release`` nothing is read:
    ``evaluable`` uses the declaration, the Profile's window and the round's cutoff only. Every
    ``release`` (one per claimed evaluation) loads and proves the pair afresh, so no family's
    sealed data is ever read before that family's own claim.
    """

    def __init__(
        self,
        catalog: DatasetCatalog,
        *,
        symbol: str,
        feature_manifest_hash: str,
        price_manifest_hash: str,
        window: tuple[datetime, datetime],
        as_of: datetime,
        research_price: ResearchDatasetManifest,
    ) -> None:
        self._catalog = catalog
        self._symbol = symbol
        self.feature_manifest_hash = feature_manifest_hash
        self.price_manifest_hash = price_manifest_hash
        self._window = window
        self._as_of = as_of
        self._research = research_price
        self._released: _ReleasedSealed | None = None

    @property
    def window(self) -> tuple[datetime, datetime]:
        return self._window

    def evaluable(self, as_of: datetime) -> str | None:
        """``None`` once the Profile's sealed window has ended by the round's cutoff (then the
        whole window can be known by ``as_of``); decided without reading anything."""
        if as_of < self._window[1]:
            return (
                f"the sealed window ends at {self._window[1].isoformat()}, after this round's "
                f"cutoff {as_of.isoformat()}"
            )
        return None

    def release(self, evaluation: SealedEvaluation) -> tuple[PriceBar, ...]:
        """The proven sealed bars of the claimed ``evaluation`` (loads and proves the pair now)."""
        if (evaluation.window.start, evaluation.window.end) != self._window:
            raise SealedOosLocked("the evaluation was claimed for another sealed window")
        evaluation.take("bars")
        self._released = None
        self._released = self._load()
        return self._released.prices.bars

    def signals(
        self, provider: FeatureProvider, spec: FeatureSpec, extra: Sequence[Any]
    ) -> tuple[SignalObservation, ...]:
        """``spec`` over the sealed feature manifest, evaluated at every released bar's close."""
        state = self._released
        if state is None:
            raise SealedOosLocked("the sealed pair has not been released for a claimed evaluation")
        bars = tuple(extra)
        if bars != state.prices.bars:
            raise SealedOosLocked("sealed signals run only over the released sealed bars")
        if not bars:
            return ()
        times = tuple(bar.interval_end for bar in bars)
        catalog = self._catalog
        request = feature_request_from_dataset(
            catalog.adapter,
            catalog.storage,
            builder=catalog.builder,
            manifest_content_hash=state.pair.feature_manifest_hash,
            pit_spec=state.feature_manifest.point_in_time,
            observations=state.observations,
            feature=spec,
            evaluation_times=times,
        )
        state.feature_hashes.append(request.manifest_content_hash)
        result = run_feature(provider, spec, request)
        return signals_from_features(
            result, feature=spec.ref, instrument=self._symbol, knowledge_time=times[-1]
        )

    def binding(self) -> dict[str, Any]:
        """The released data's ``ValidatorSetup`` binding fields (empty before a release)."""
        state = self._released
        if state is None:
            return {}
        return {
            "manifest_content_hash": state.prices.manifest_content_hash,
            "dataset_bars": state.prices,
            "manifest_pair": state.pair,
            "feature_manifest_hashes": tuple(state.feature_hashes),
        }

    # ------------------------------------------------------------------------------------------

    def _load(self) -> _ReleasedSealed:
        catalog, symbol, as_of = self._catalog, self._symbol, self._as_of
        start, end = self._window
        try:  # 1. verified loads
            price = load_manifest(catalog.builder, self.price_manifest_hash)
            feature = load_manifest(catalog.builder, self.feature_manifest_hash)
        except DatasetBindingError as exc:
            raise SealedDataRefused(f"a sealed manifest does not load: {exc}") from exc
        for name, manifest in (("feature", feature), ("price", price)):
            self._require_shared_upstream(name, manifest)  # 2.
            data = manifest.dataset  # 3. exactly the Profile's sealed window
            if (data.time_range_start, data.time_range_end) != (start, end):
                raise SealedDataRefused(
                    f"the sealed {name} manifest covers [{data.time_range_start.isoformat()}, "
                    f"{data.time_range_end.isoformat()}), not exactly the Profile's sealed "
                    f"window [{start.isoformat()}, {end.isoformat()})"
                )
        view = price.point_in_time.simulation_time
        if view is None:
            raise SealedDataRefused("the sealed price manifest is not a point simulation")
        if view < end or view > as_of:
            raise SealedDataRefused(
                f"the sealed price view {view.isoformat()} is not within [the window's end "
                f"{end.isoformat()}, the round's cutoff {as_of.isoformat()}]"
            )
        try:  # 4. one chain (the research pair's rule); 5. the proven sealed bars
            pair = pair_manifests(catalog.builder, feature.content_hash(), price.content_hash())
            prices = backtest_bars_from_dataset(
                catalog.adapter,
                catalog.storage,
                builder=catalog.builder,
                manifest_content_hash=pair.price_manifest_hash,
                symbols=(symbol,),
            )
        except DatasetBindingError as exc:
            raise SealedDataRefused(f"the sealed pair does not prove: {exc}") from exc
        if prices.price_cutoff > as_of or any(
            bar.available_time > as_of or bar.interval_start < start or bar.interval_end > end
            for bar in prices.bars
        ):
            raise SealedDataRefused("a sealed bar is after the cutoff or outside the window")
        selection = PitSelector(catalog.adapter, catalog.storage).select(
            feature.point_in_time, _DATA_TYPE, _VENUE[symbol], start, end
        )
        selection.require_no_conflict()
        rows = bar_observations(selection, feature.point_in_time)
        if any(row.available_time > as_of for row in rows):
            raise SealedDataRefused("a sealed feature observation is available after the cutoff")
        return _ReleasedSealed(pair, feature, prices, rows, [])

    def _require_shared_upstream(self, name: str, sealed: ResearchDatasetManifest) -> None:
        """The research pair's upstream state (the research pair's two manifests are equal in
        all of these by ``pair_manifests``, so its price manifest stands for both)."""
        research = self._research
        s_spec, r_spec = sealed.point_in_time, research.point_in_time
        if s_spec.snapshot_bindings != r_spec.snapshot_bindings:
            tables = sorted(
                table
                for table in {*s_spec.snapshot_bindings, *r_spec.snapshot_bindings}
                if s_spec.snapshot_bindings.get(table) != r_spec.snapshot_bindings.get(table)
            )
            raise SealedDataRefused(
                f"the sealed {name} manifest's upstream snapshots differ from the research "
                f"pair's for {tables}"
            )
        if s_spec.knowledge_cutoff != r_spec.knowledge_cutoff:
            raise SealedDataRefused(
                f"the sealed {name} manifest's knowledge cutoff differs from the research pair's"
            )
        if assumption_bound(s_spec) != assumption_bound(r_spec):
            raise SealedDataRefused(
                f"the ADR-0032 assumption choice of the sealed {name} manifest differs from the "
                "research pair's"
            )
        for field in (
            "availability_bindings",
            "precedence_bindings",
            "parser_bindings",
            "point_in_time_binding",
        ):
            if getattr(s_spec, field) != getattr(r_spec, field):
                raise SealedDataRefused(
                    f"{field} of the sealed {name} manifest differ from the research pair's"
                )
        if sealed.universe_spec != research.universe_spec:
            raise SealedDataRefused(f"the sealed {name} manifest binds another universe spec")
        if sealed.dataset.table != research.dataset.table:
            raise SealedDataRefused(f"the sealed {name} manifest is of another dataset table")
        tables_of = {item.canonical_table for item in sealed.lineage}
        if tables_of != {item.canonical_table for item in research.lineage}:
            raise SealedDataRefused(
                f"the sealed {name} manifest's lineage spans other Canonical tables"
            )


@dataclass(frozen=True)
class DatasetSegment:
    """One dataset-backed round: the pair's proven research bars and the sealed window (withheld
    bars, or the declared sealed pair — unread until a claimed evaluation releases it)."""

    catalog: DatasetCatalog
    symbol: str
    pair: ManifestPair
    feature_manifest: ResearchDatasetManifest
    price_manifest: ResearchDatasetManifest
    prices: DatasetPriceBars
    observations: tuple[FeatureObservation, ...]
    sealed: SealedSource
    #: The sealed data's manifest (the withheld point manifest, or the sealed pair's price
    #: manifest): the G5 outcome request's label.
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

    def sealed_binding(self) -> dict[str, Any]:
        """The released sealed pair's binding (G5's ``G0.manifest_binding``); empty otherwise."""
        sealed = self.sealed
        return sealed.binding() if isinstance(sealed, SealedDatasetPair) else {}

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
            return (), (), self._signals_with(provider, spec, ())
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
        return cached[0], cached[1], self._signals_with(provider, spec, cached[1])

    def _signals_with(
        self,
        provider: FeatureProvider,
        spec: FeatureSpec,
        found: tuple[SignalObservation, ...],
    ) -> Callable[[Sequence[Any]], tuple[SignalObservation, ...]]:
        """The research signals followed by signals over the released sealed bars (G5 only; only
        a released ``SealedDatasetPair`` has any)."""

        def signals_with(extra: Sequence[Any]) -> tuple[SignalObservation, ...]:
            sealed = self.sealed
            if not isinstance(sealed, SealedDatasetPair):
                raise SealedOosLocked(
                    "no sealed manifest pair is declared for this round: the sealed window "
                    "stays sealed"
                )
            return found + sealed.signals(provider, spec, extra)

        return signals_with


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
        cache = catalog.manifest_cache
        price = load_verified_manifest(catalog.builder, declared.price_manifest_hash, cache)
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
            catalog.builder,
            declared.feature_manifest_hash,
            declared.price_manifest_hash,
            manifest_cache=cache,
        )
        feature = load_verified_manifest(catalog.builder, pair.feature_manifest_hash, cache)
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
            manifest_cache=cache,
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
        # 6. the sealed window: withheld, never research data; a declared sealed pair is not read
        # here at all (only after a claimed G5 evaluation, SealedDatasetPair)
        sealed: list[PriceBar] = []
        unused = 0
        source: SealedSource
        if declared.sealed_price_manifest_hash is not None:
            assert declared.sealed_feature_manifest_hash is not None  # both or neither
            source = SealedDatasetPair(
                catalog,
                symbol=symbol,
                feature_manifest_hash=declared.sealed_feature_manifest_hash,
                price_manifest_hash=declared.sealed_price_manifest_hash,
                window=window,
                as_of=ctx.as_of,
                research_price=price,
            )
        elif declared.sealed_manifest_hash is not None:
            held = backtest_bars_from_dataset(
                catalog.adapter,
                catalog.storage,
                builder=catalog.builder,
                manifest_content_hash=declared.sealed_manifest_hash,
                symbols=(symbol,),
                manifest_cache=cache,
            )
            if held.price_cutoff > ctx.as_of:
                raise _refuse(ctx, "the sealed manifest's view is after the round's cutoff")
            for bar in held.bars:
                if bar.interval_start >= window[0] and bar.interval_end <= window[1]:
                    sealed.append(bar)
                else:
                    unused += 1
            source = WithheldSealedBars(sealed, window)
        else:
            source = WithheldSealedBars((), window)
        segment = DatasetSegment(
            catalog=catalog,
            symbol=symbol,
            pair=pair,
            feature_manifest=feature,
            price_manifest=price,
            prices=prices,
            observations=rows,
            sealed=source,
            sealed_manifest_hash=declared.sealed_manifest_hash
            or declared.sealed_price_manifest_hash,
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
            # a declared sealed pair is not read by the ingest: nothing withheld to count
            "sealed_bars_withheld": None if declared.has_sealed_pair else len(sealed),
            "unused_bars": unused,
            "research_data_hash": segment.data_hash,
            "decision_times": len(segment.decision_times),
        }
        if declared.has_sealed_pair:
            summary["sealed_feature_manifest_hash"] = declared.sealed_feature_manifest_hash
            summary["sealed_price_manifest_hash"] = declared.sealed_price_manifest_hash
        return StageResult(summary, self.estimate(ctx), {"segment": segment})

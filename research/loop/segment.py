"""Shared building blocks of the loop's research stages (Phase 11; ADR-0049, W2 wiring).

- ``Segment``: one round's data. Its research part is the **accumulated research window** — every
  research-window bar ingested up to the round's ``as_of`` (``ResearchPiece`` per ingested market,
  oldest first; ADR-0049 accumulated-window note) — so the Profile's walk-forward, which spans the
  whole research window, can be covered once enough rounds have run. Bars whose label could reach
  the Profile's sealed OOS window are **never** part of it: they are **withheld** in ``SealedBars``
  and are released only against the ``SealedEvaluation`` a ``SealedOosVault`` hands out after the
  family's unsealing, which has already consumed the family's single evaluation (Constitution
  C-S1 ~ C-S3; the vault enforces one unsealing per family and the explicit global budget);
- ``decision_grid``: decision times ``start + warmup + k * step`` whose label (``horizon``) ends
  inside the research data;
- ``observations`` / ``feature_pairs``: synthetic bars as ``FeatureObservation`` rows (lineage
  labels point at the synthetic market) and F4 feature runs over contiguous chunks — a chunk sees
  one earlier bar, so values do not depend on the chunk size (a performance parameter only);
- ``trial_point``: the strategy and parameter point a hypothesis pre-registers
  (``strategy = name@version`` exactly once, ``param <key> = <value>`` any number of times; any
  other condition is refused, so a hypothesis is never run as something else than it says);
- ``decimal_text``: floats that enter hashed loop records are first turned into Decimal text with
  a fixed quantization (``RECORD_QUANTUM``, half-even), so a record hash never depends on float
  formatting.

Round data sources (ADR-0049 implementation note, dataset-backed loop, 2026-09-26). The stages
after the ingest read a round's data only through ``RoundData``: the research bars and their
decision grid, the withheld ``SealedBars``, the feature runs over the research data, and what the
reproducibility tuple and the validator bind (dataset snapshots, the manifest hash of the labels,
the dataset binding of ``G0.manifest_binding``). The sealed window is a ``SealedSource``: whether a
round could evaluate it is decided without reading sealed data (``evaluable``), and its data leaves
storage only on ``release`` against a claimed evaluation (ADR-0049 implementation note, dataset
G5). ``Segment`` is the synthetic implementation (``IngestStage``; its record hashes are unchanged
by the refactor), ``DatasetSegment`` (``research.loop.dataset_source``) the one over verified
Research Dataset manifests.

Synthetic sealed bars (ADR-0049 implementation note, review fixes 4, 2026-09-26). The synthetic
ingest **generates** its sealed-window bars as part of generating the round's market and then
withholds them; generating is not reading real sealed data (the market is synthetic, so there is
no real out-of-sample observation to leak). The generated market stays with the ingest: in
``ResearchMemory.markets`` (a durable restore regenerates it from its spec) and as
``Segment.market`` / ``ResearchPiece.market`` (their market hash labels the research data). The
stages after the ingest must not see the sealed bars, and they do not: they read the round only
through the ``RoundData`` protocol, which exposes no market object and hands sealed bars out only
via ``sealed.release`` against a claimed evaluation (proved by a test that runs the whole loop, G5
included, over a proxy exposing only the protocol's members, and by a source scan that no stage
other than the ingest touches ``market`` / ``markets``).
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping, MutableMapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import ROUND_HALF_EVEN, Context, Decimal, localcontext
from functools import cached_property
from typing import Any, Final, Protocol

from core.contracts.feature import (
    FeatureObservation,
    FeatureProvider,
    FeatureRequest,
    FeatureResult,
)
from core.contracts.strategy import PriceBar, SignalObservation
from core.contracts.synthetic import SyntheticBar, SyntheticMarket
from core.contracts.universe import SelectedRevisionLineage
from core.domain.base import FrozenMapping, content_hash
from core.domain.research import Hypothesis
from core.domain.specs import DatasetRef, FeatureSpec, Zone
from infrastructure.feature.runner import run_feature
from infrastructure.strategy.signals import signals_from_features
from research.validation.sealed_oos import SealedEvaluation, SealedOosLocked

__all__ = [
    "RECORD_QUANTUM",
    "FeatureRuns",
    "Param",
    "ResearchPiece",
    "RoundData",
    "SealedBars",
    "SealedDataRefused",
    "SealedSource",
    "Segment",
    "Timed",
    "TrialPoint",
    "decimal_text",
    "decision_grid",
    "feature_pairs",
    "observations",
    "price_bar",
    "trial_point",
]

#: Quantum of every float written into a hashed loop record (as Decimal text, half-even).
RECORD_QUANTUM: Final = Decimal("1e-12")
_CONTEXT: Final = Context(prec=80, rounding=ROUND_HALF_EVEN)
_STRATEGY: Final = re.compile(r"^strategy\s*=\s*([a-z][a-z0-9_]*@\S+)$")
_PARAM: Final = re.compile(r"^param\s+([a-z][a-z0-9_]*)\s*=\s*(\S+)$")
_INT: Final = re.compile(r"^-?\d+$")
#: A requestable strategy parameter value (floats cannot be requested, ADR-0041).
type Param = str | int | bool


def decimal_text(value: float | Decimal | int | None) -> str | None:
    """``value`` as Decimal text quantized to ``RECORD_QUANTUM`` (non-finite: ``nan`` / ``inf``)."""
    if value is None:
        return None
    if isinstance(value, bool):
        raise TypeError("a bool is not a number here")
    number = value if isinstance(value, Decimal) else Decimal(repr(value))
    if number.is_nan():
        return "nan"
    if number.is_infinite():
        return "inf" if number > 0 else "-inf"
    with localcontext(_CONTEXT):
        return str(number.quantize(RECORD_QUANTUM))


def price_bar(symbol: str, bar: SyntheticBar) -> PriceBar:
    return PriceBar(
        instrument=symbol,
        interval_start=bar.interval_start,
        interval_end=bar.interval_end,
        available_time=bar.interval_end,
        open=bar.open,
        high=bar.high,
        low=bar.low,
        close=bar.close,
    )


class Timed(Protocol):
    """A bar's interval (``SyntheticBar`` and ``PriceBar`` both have one)."""

    @property
    def interval_start(self) -> datetime: ...

    @property
    def interval_end(self) -> datetime: ...


class SealedDataRefused(Exception):
    """The sealed window's data cannot honestly be released for a claimed evaluation.

    Raised by a ``SealedSource.release`` **after** the claim (the evaluation is consumed): the
    validation stage records the G5 report as ``consumed_without_result:sealed_data_refused``.
    """


class SealedSource(Protocol):
    """A round's sealed OOS window as the validation stage sees it (ADR-0049 dataset G5 note).

    ``evaluable`` decides — **without reading any sealed data** — whether this round could
    evaluate the window at all (``None``) or why it stays sealed (the reason, recorded).
    ``release`` hands the window's bars out only against the family's claimed one-shot
    ``SealedEvaluation``; a source that reads storage reads it only there, after the claim.
    """

    @property
    def window(self) -> tuple[datetime, datetime]: ...

    def evaluable(self, as_of: datetime) -> str | None: ...

    def release(self, evaluation: SealedEvaluation) -> tuple[Any, ...]: ...


class SealedBars[BarT: Timed]:
    """Bars inside the sealed OOS window; released only against a claimed one-shot evaluation.

    ``release`` needs the family's ``SealedEvaluation`` (``SealedOosVault.claim_evaluation``),
    which has already recorded the family's single evaluation as consumed: releasing the bars can
    therefore never leave an unsealed-but-unevaluated window behind (ADR-0049 review fixes 2).
    The claim hands the bars out once.
    """

    def __init__(self, bars: Sequence[BarT], window: tuple[datetime, datetime]) -> None:
        self._bars = tuple(bars)
        self._window = window

    @property
    def window(self) -> tuple[datetime, datetime]:
        return self._window

    def __len__(self) -> int:
        return len(self._bars)

    def evaluable(self, as_of: datetime) -> str | None:
        """``None`` when the round withheld sealed-window bars, else why it stays sealed."""
        return None if self._bars else "no sealed-window data in this round"

    def release(self, evaluation: SealedEvaluation) -> tuple[BarT, ...]:
        if (evaluation.window.start, evaluation.window.end) != self.window:
            raise SealedOosLocked("the evaluation was claimed for another sealed window")
        evaluation.take("bars")
        return self._bars


#: A round's feature runs over its research data: the (request, result) pairs, the strategy
#: signals of those values, and ``signals_with(extra)`` — the same signals followed by signals over
#: ``extra`` released sealed bars (G5 only).
type FeatureRuns = tuple[
    tuple[tuple[FeatureRequest, FeatureResult], ...],
    tuple[SignalObservation, ...],
    Callable[[Sequence[Any]], tuple[SignalObservation, ...]],
]


class RoundData(Protocol):
    """What the stages after the ingest read of one round's data (module docs)."""

    @property
    def symbol(self) -> str: ...

    @property
    def decision_times(self) -> tuple[datetime, ...]: ...

    @property
    def sealed(self) -> SealedSource: ...

    @property
    def research_bars(self) -> tuple[PriceBar, ...]:
        """The accumulated research bars (time order); never a sealed-window bar."""
        ...

    @property
    def research_start(self) -> datetime | None: ...

    @property
    def research_end(self) -> datetime | None: ...

    @property
    def data_hash(self) -> str:
        """Identity of the accumulated research data."""
        ...

    @property
    def manifest_content_hash(self) -> str:
        """The manifest hash the validator's outcome request carries (a label when synthetic)."""
        ...

    @property
    def plugins(self) -> Mapping[str, str]:
        """The data source's plugin / rule versions for the reproducibility tuple."""
        ...

    def source_fields(self) -> dict[str, Any]:
        """The data source's identity in every experiment summary row."""
        ...

    def dataset_snapshots(self, as_of: datetime) -> tuple[DatasetRef, ...]:
        """The reproducibility tuple's dataset snapshots of the research data."""
        ...

    def bar_volume(self) -> Mapping[tuple[str, datetime], Decimal] | None:
        """Traded quantity per ``(instrument, interval_start)`` of the research bars."""
        ...

    def validator_binding(self, feature_manifest_hashes: Sequence[str]) -> dict[str, Any]:
        """The ``ValidatorSetup`` dataset-binding fields (empty on the synthetic path)."""
        ...

    def feature_runs(
        self,
        provider: FeatureProvider,
        spec: FeatureSpec,
        *,
        chunk: int,
        cache: MutableMapping[str, Any],
    ) -> FeatureRuns:
        """``spec`` over the research data; ``cache`` is the caller's (pure) per-stage cache."""
        ...

    def as_price_bars(self, bars: Sequence[Any]) -> tuple[PriceBar, ...]:
        """Released sealed bars as backtest ``PriceBar``s."""
        ...

    def sealed_manifest_label(self) -> str:
        """The manifest hash of the G5 outcome request over research + released sealed bars."""
        ...

    def sealed_binding(self) -> dict[str, Any]:
        """The ``ValidatorSetup`` dataset-binding fields of the **released** sealed data (G5's
        ``G0.manifest_binding``); empty when there is nothing to bind (synthetic path)."""
        ...


@dataclass(frozen=True)
class ResearchPiece:
    """The research-window bars one ingested market contributed to the accumulated research data."""

    round_index: int
    market: SyntheticMarket
    bars: tuple[SyntheticBar, ...]

    def identity(self) -> dict[str, object]:
        return {
            "round": self.round_index,
            "market_hash": self.market.market_hash,
            "start": self.bars[0].interval_start.isoformat(),
            "end": self.bars[-1].interval_end.isoformat(),
            "bars": len(self.bars),
        }


@dataclass(frozen=True)
class Segment:
    """One round's data: the accumulated research bars and this round's withheld sealed bars.

    ``pieces`` is the accumulated research data up to the round's ``as_of`` (oldest first; this
    round's contribution, if any, last) and ``research`` their bars in time order; ``market`` is
    this round's newly ingested market and ``sealed`` its sealed-window bars (withheld). Only
    bars inside the Profile's research window whose label ends before the sealed OOS boundary are
    ever in ``pieces``.
    """

    market: SyntheticMarket
    symbol: str
    provider_key: str
    provider_hash: str
    pieces: tuple[ResearchPiece, ...]
    sealed: SealedBars[SyntheticBar]
    decision_times: tuple[datetime, ...]

    @cached_property
    def research(self) -> tuple[SyntheticBar, ...]:
        return tuple(bar for piece in self.pieces for bar in piece.bars)

    @property
    def new_research(self) -> tuple[SyntheticBar, ...]:
        """This round's own contribution to the research data (possibly empty)."""
        last = self.pieces[-1] if self.pieces else None
        return last.bars if last is not None and last.market is self.market else ()

    @cached_property
    def research_bars(self) -> tuple[PriceBar, ...]:
        return tuple(price_bar(self.symbol, bar) for bar in self.research)

    @property
    def research_start(self) -> datetime | None:
        return self.pieces[0].bars[0].interval_start if self.pieces else None

    @property
    def research_end(self) -> datetime | None:
        return self.pieces[-1].bars[-1].interval_end if self.pieces else None

    @property
    def data_hash(self) -> str:
        """Identity of the accumulated research data (every piece's market hash and range)."""
        return content_hash([piece.identity() for piece in self.pieces])

    # ------------------------------------------------------------------ RoundData (synthetic)

    @property
    def manifest_content_hash(self) -> str:
        """The research data hash: an unverified label (``synthetic_unverified`` path)."""
        return self.data_hash

    @property
    def plugins(self) -> Mapping[str, str]:
        return {self.provider_key: self.provider_hash}

    def source_fields(self) -> dict[str, Any]:
        return {"market_hash": self.market.market_hash}

    def dataset_snapshots(self, as_of: datetime) -> tuple[DatasetRef, ...]:
        """One snapshot per ingested market contributing research bars (else this round's)."""
        table = f"synthetic.{self.provider_key}"
        return tuple(
            DatasetRef(
                zone=Zone.CANONICAL,
                table=table,
                snapshot_id=piece.market.market_hash,
                time_range_start=piece.bars[0].interval_start,
                time_range_end=piece.bars[-1].interval_end,
            )
            for piece in self.pieces
        ) or (
            DatasetRef(
                zone=Zone.CANONICAL,
                table=table,
                snapshot_id=self.market.market_hash,
                time_range_start=self.research_start or as_of,
                time_range_end=self.research_end or as_of,
            ),
        )

    def bar_volume(self) -> Mapping[tuple[str, datetime], Decimal]:
        return {(self.symbol, bar.interval_start): bar.volume for bar in self.research}

    def validator_binding(self, feature_manifest_hashes: Sequence[str]) -> dict[str, Any]:
        """Nothing to bind: the synthetic path's manifest hash is a label (never verified)."""
        return {}

    def as_price_bars(self, bars: Sequence[Any]) -> tuple[PriceBar, ...]:
        return tuple(price_bar(self.symbol, bar) for bar in bars)

    def sealed_manifest_label(self) -> str:
        return content_hash({"research": self.data_hash, "sealed": self.market.market_hash})

    def sealed_binding(self) -> dict[str, Any]:
        """Nothing to bind: the synthetic sealed bars are a generated market's."""
        return {}

    def feature_runs(
        self,
        provider: FeatureProvider,
        spec: FeatureSpec,
        *,
        chunk: int,
        cache: MutableMapping[str, Any],
    ) -> FeatureRuns:
        """Feature runs over the accumulated research data, piece by piece.

        A piece's features depend only on its own bars and the last bar before it (both fixed
        once ingested), so each piece is evaluated once and reused in later rounds (``cache``);
        its requests are labelled with the piece's own market hash.
        """
        pairs: list[Any] = []
        signals: list[SignalObservation] = []
        prefix: FeatureObservation | None = None
        previous: dict[str, object] | None = None
        for piece in self.pieces:
            key = content_hash({"piece": piece.identity(), "after": previous})
            cached = cache.get(key)
            if cached is None:
                rows = observations(piece.market, piece.bars, self.symbol)
                cached = _synthetic_features(
                    provider, spec, self.symbol, rows, prefix, piece.market.market_hash, chunk
                )
                cache[key] = cached
            pairs.extend(cached[0])
            signals.extend(cached[1])
            [prefix] = observations(piece.market, piece.bars[-1:], self.symbol)
            previous = piece.identity()
        found, last_row = tuple(signals), prefix

        def signals_with(extra: Sequence[Any]) -> tuple[SignalObservation, ...]:
            """Signals over the research data followed by ``extra`` bars of this round's market
            (the released sealed bars of a claimed G5 evaluation)."""
            more = observations(self.market, extra, self.symbol)
            return (
                found
                + _synthetic_features(
                    provider, spec, self.symbol, more, last_row, self.market.market_hash, chunk
                )[1]
            )

        return tuple(pairs), found, signals_with


def _synthetic_features(
    provider: FeatureProvider,
    spec: FeatureSpec,
    symbol: str,
    rows: Sequence[FeatureObservation],
    prefix: FeatureObservation | None,
    manifest: str,
    chunk: int,
) -> tuple[tuple[tuple[FeatureRequest, FeatureResult], ...], tuple[SignalObservation, ...]]:
    pairs = feature_pairs(provider, spec, rows, chunk=chunk, manifest=manifest, prefix=prefix)
    signals = tuple(
        signal
        for _, result in pairs
        for signal in signals_from_features(
            result,
            feature=spec.ref,
            instrument=symbol,
            knowledge_time=result.values[-1].evaluation_time,
        )
    )
    return pairs, signals


def decision_grid(
    bars: Sequence[Timed], *, step: timedelta, warmup: timedelta, horizon: timedelta
) -> tuple[datetime, ...]:
    """``start + warmup + k * step`` while its label (``horizon``) ends before the last bar ends."""
    if not bars:
        return ()
    if step <= timedelta(0) or warmup < timedelta(0) or horizon <= timedelta(0):
        raise ValueError("step and horizon must be positive, warmup non-negative")
    ends = {bar.interval_end for bar in bars}
    last = bars[-1].interval_end
    out: list[datetime] = []
    t = bars[0].interval_start + warmup
    while t + horizon < last:
        if t in ends:
            out.append(t)
        t += step
    return tuple(out)


def observations(
    market: SyntheticMarket, bars: Sequence[SyntheticBar], symbol: str
) -> tuple[FeatureObservation, ...]:
    """Synthetic bars as feature observations (available and known at the bar's end)."""
    out: list[FeatureObservation] = []
    for bar in bars:
        values: dict[str, Decimal | int | str] = {
            "symbol": symbol,
            "open": bar.open,
            "high": bar.high,
            "low": bar.low,
            "close": bar.close,
            "volume": bar.volume,
            "trade_count": bar.trade_count,
        }
        revision = f"synthetic-{bar.interval_start.isoformat()}"
        out.append(
            FeatureObservation(
                observation_key=f"synthetic:{symbol}:{bar.interval_start.isoformat()}",
                event_time=bar.interval_start,
                event_end_time=bar.interval_end,
                available_time=bar.interval_end,
                knowledge_time=bar.interval_end,
                values=FrozenMapping(values),
                lineage=SelectedRevisionLineage(
                    canonical_table="canonical.bars_1m",
                    canonical_revision_id=revision,
                    raw_table="raw.synthetic_bars",
                    raw_revision_id=revision,
                    source_table="raw.synthetic_markets",
                    source_revision_id=market.market_hash,
                ),
            )
        )
    return tuple(out)


def feature_pairs(
    provider: FeatureProvider,
    spec: FeatureSpec,
    rows: Sequence[FeatureObservation],
    *,
    chunk: int,
    manifest: str,
    prefix: FeatureObservation | None = None,
) -> tuple[tuple[FeatureRequest, FeatureResult], ...]:
    """``spec`` evaluated at every row's ``available_time`` via ``run_feature``, chunk by chunk.

    Each chunk's request also carries the row just before the chunk (one earlier bar; for the
    first chunk, ``prefix`` — the last row of the preceding data — when given); the runner
    truncates every call to the visible prefix, so there is no look-ahead inside a chunk either.
    """
    if chunk < 1:
        raise ValueError("chunk must be positive")
    if prefix is not None and rows and prefix.available_time >= rows[0].available_time:
        raise ValueError("the prefix row must precede the rows")
    pairs: list[tuple[FeatureRequest, FeatureResult]] = []
    for start in range(0, len(rows), chunk):
        before = rows[start - 1 : start] if start else (() if prefix is None else (prefix,))
        part = (*before, *rows[start : start + chunk])
        times = tuple(row.available_time for row in rows[start : start + chunk])
        request = FeatureRequest(
            feature=spec.ref,
            spec_hash=spec.content_hash(),
            manifest_content_hash=manifest,  # ad-hoc synthetic run: the market hash is the label
            knowledge_cutoff=times[-1],
            evaluation_times=times,
            observations=tuple(part),
        )
        pairs.append((request, run_feature(provider, spec, request)))
    return tuple(pairs)


@dataclass(frozen=True, slots=True)
class TrialPoint:
    strategy: str  # ``name@version`` of the StrategySpec
    overrides: FrozenMapping[str, Param]


def _scalar(text: str) -> Param:
    if text in {"true", "false"}:
        return text == "true"
    if _INT.fullmatch(text):
        return int(text)
    return text


def trial_point(hypothesis: Hypothesis) -> TrialPoint:
    """The strategy and parameter overrides a hypothesis pre-registered (see module docs)."""
    strategy: list[str] = []
    overrides: dict[str, Param] = {}
    for condition in hypothesis.conditions:
        text = condition.strip()
        if match := _STRATEGY.fullmatch(text):
            strategy.append(match.group(1))
        elif match := _PARAM.fullmatch(text):
            key = match.group(1)
            if key in overrides:
                raise ValueError(f"{hypothesis.ref} declares parameter {key!r} twice")
            overrides[key] = _scalar(match.group(2))
        else:
            raise ValueError(f"{hypothesis.ref}: unsupported condition {condition!r}")
    if len(strategy) != 1:
        raise ValueError(f"{hypothesis.ref} does not name exactly one 'strategy = name@version'")
    return TrialPoint(strategy=strategy[0], overrides=FrozenMapping(overrides))

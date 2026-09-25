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
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import ROUND_HALF_EVEN, Context, Decimal, localcontext
from functools import cached_property
from typing import Final

from core.contracts.feature import (
    FeatureObservation,
    FeatureProvider,
    FeatureRequest,
    FeatureResult,
)
from core.contracts.strategy import PriceBar
from core.contracts.synthetic import SyntheticBar, SyntheticMarket
from core.contracts.universe import SelectedRevisionLineage
from core.domain.base import FrozenMapping, content_hash
from core.domain.research import Hypothesis
from core.domain.specs import FeatureSpec
from infrastructure.feature.runner import run_feature
from research.validation.sealed_oos import SealedEvaluation, SealedOosLocked

__all__ = [
    "RECORD_QUANTUM",
    "Param",
    "ResearchPiece",
    "SealedBars",
    "Segment",
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


class SealedBars:
    """Bars inside the sealed OOS window; released only against a claimed one-shot evaluation.

    ``release`` needs the family's ``SealedEvaluation`` (``SealedOosVault.claim_evaluation``),
    which has already recorded the family's single evaluation as consumed: releasing the bars can
    therefore never leave an unsealed-but-unevaluated window behind (ADR-0049 review fixes 2).
    The claim hands the bars out once.
    """

    def __init__(self, bars: Sequence[SyntheticBar], window: tuple[datetime, datetime]) -> None:
        self._bars = tuple(bars)
        self.window = window

    def __len__(self) -> int:
        return len(self._bars)

    def release(self, evaluation: SealedEvaluation) -> tuple[SyntheticBar, ...]:
        if (evaluation.window.start, evaluation.window.end) != self.window:
            raise SealedOosLocked("the evaluation was claimed for another sealed window")
        evaluation.take("bars")
        return self._bars


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
    sealed: SealedBars
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


def decision_grid(
    bars: Sequence[SyntheticBar], *, step: timedelta, warmup: timedelta, horizon: timedelta
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

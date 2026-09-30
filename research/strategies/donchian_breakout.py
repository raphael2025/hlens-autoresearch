"""Donchian channel breakout, spot long / flat (ADR-0085 ``STR-TF-DONCHIAN-001``).

Research code — never production (H5). Source: the strategy library entry
``STR-TF-DONCHIAN-001`` / ``STR-BREAKOUT-001`` (Donchian's channel rule; the Turtle trading rules).
The project knowledge base holds no seed item for the
channel rule itself; the spec's ``lineage`` cites the trend-persistence claims the rule trades on,
``strategy_time_series_momentum@1.0.0`` and ``strategy_crypto_time_series_momentum@1.0.0``. They are
claims to test, not evidence.

Signals: ``bar_close``, ``bar_high``, ``bar_low`` (``price_signals.py``), one bar per
``event_time``. Rule, per instrument and decision time ``t``, over the bars visible at ``t``
(``available_time <= t``, by ``event_time``), replayed in order from a flat state:

- a bar is *complete* when all three values are visible and ``Decimal``; an incomplete bar (a
  missing value or an explicit ``None``) resets the state to flat and starts a new run — nothing is
  filled in;
- a channel is defined only when the current run holds at least ``max(entry_window,
  exit_window)`` complete bars **before** the bar being evaluated (so once long, the exit channel
  always exists);
- flat → long when ``close`` is strictly above the highest ``high`` of the previous
  ``entry_window`` bars of the run;
- long → flat when ``close`` is strictly below the lowest ``low`` of the previous ``exit_window``
  bars of the run.

The target at ``t`` is the state after the latest visible bar: long = an equal slice
``1 / len(instruments)`` of gross exposure (as ``tsmom_bars``), flat = 0. When the latest bar is
incomplete or had no channel yet, the target is flat with ``inputs_used = 0``. Otherwise
``inputs_used`` counts the observations of the current run (the state depends on all of them). The
state depends on how far back the visible history reaches: the replay starts flat at the first
visible bar. Spot long / flat only: no short target (short cost, gap ST-4, is undefined).

The parameter space is declared here (``DONCHIAN_PARAM_SPACE``); a spec is built only at an
explicit declared point (ADR-0085 rule 2: no default) and a request outside the space is refused
(``UnsupportedStrategy``, C-T1). The declared values are strategy parameters in bars of the signal's
bar size, not validation thresholds.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import ROUND_HALF_EVEN, Context, Decimal, localcontext
from typing import Final

from core.contracts.strategy import (
    SignalObservation,
    StrategyInputError,
    StrategyProviderDescriptor,
    StrategyRequest,
    StrategyResult,
    TargetPosition,
    UnsupportedStrategy,
)
from core.domain.base import FrozenMapping, Kind, Ref
from core.domain.specs import StrategySpec
from research.strategies._declared import build_spec, check_spec, positive_int
from research.strategies._params import resolve_params
from research.strategies.price_signals import BAR_CLOSE_SIGNAL, BAR_HIGH_SIGNAL, BAR_LOW_SIGNAL

__all__ = [
    "DONCHIAN_KNOWLEDGE",
    "DONCHIAN_NAME",
    "DONCHIAN_PARAM_SPACE",
    "DONCHIAN_SIGNALS",
    "DonchianBreakoutProvider",
    "donchian_spec",
]

DONCHIAN_NAME: Final = "donchian_breakout"
#: KnowledgeItem refs of the trend claim the rule trades on (its lineage; see module docs).
DONCHIAN_KNOWLEDGE: Final = (
    Ref(kind=Kind.KNOWLEDGE, name="strategy_time_series_momentum", version="1.0.0"),
    Ref(kind=Kind.KNOWLEDGE, name="strategy_crypto_time_series_momentum", version="1.0.0"),
)
DONCHIAN_SIGNALS: Final = (BAR_CLOSE_SIGNAL, BAR_HIGH_SIGNAL, BAR_LOW_SIGNAL)
#: Declared parameter space, in bars: the Donchian / Turtle channel lengths (entry 20 or 55,
#: exit 10 or 20). Four points.
DONCHIAN_PARAM_SPACE: Final[dict[str, tuple[str | int | float | bool, ...]]] = {
    "entry_window": (20, 55),
    "exit_window": (10, 20),
}
_SPEC_TIME: Final = datetime(2026, 9, 28, tzinfo=UTC)
_CONTEXT: Final = Context(prec=50, rounding=ROUND_HALF_EVEN)
_WEIGHT_QUANTUM: Final = Decimal("1e-18")


def donchian_spec(*, entry_window: int, exit_window: int) -> StrategySpec:
    """``strategy:donchian_breakout@1.0.0`` at an explicit, declared parameter point."""
    return build_spec(
        name=DONCHIAN_NAME,
        created_at=_SPEC_TIME,
        lineage=DONCHIAN_KNOWLEDGE,
        signals=DONCHIAN_SIGNALS,
        space=DONCHIAN_PARAM_SPACE,
        point={"entry_window": entry_window, "exit_window": exit_window},
    )


@dataclass(frozen=True, slots=True)
class _Bar:
    close: Decimal
    high: Decimal
    low: Decimal
    observations: tuple[SignalObservation, ...]


def _bars(series: Sequence[SignalObservation]) -> list[_Bar | None]:
    """The instrument's visible bars in ``event_time`` order; ``None`` = incomplete bar."""
    by_time: dict[datetime, dict[Ref, SignalObservation]] = {}
    for item in series:
        if item.value is not None and not isinstance(item.value, Decimal):
            raise StrategyInputError(f"{item.signal} values must be Decimal or None")
        by_time.setdefault(item.event_time, {})[item.signal] = item
    out: list[_Bar | None] = []
    for event_time in sorted(by_time):
        row = by_time[event_time]
        values = [row[ref].value if ref in row else None for ref in DONCHIAN_SIGNALS]
        close, high, low = values
        if isinstance(close, Decimal) and isinstance(high, Decimal) and isinstance(low, Decimal):
            out.append(_Bar(close, high, low, tuple(row[ref] for ref in DONCHIAN_SIGNALS)))
        else:
            out.append(None)
    return out


def _replay(
    bars: Sequence[_Bar | None], entry_window: int, exit_window: int
) -> tuple[bool, list[_Bar] | None]:
    """``(long, run)`` after the latest bar; ``run`` is ``None`` when not computable there."""
    need = max(entry_window, exit_window)
    run: list[_Bar] = []
    long = False
    evaluated = False
    for bar in bars:
        if bar is None:
            run, long, evaluated = [], False, False
            continue
        evaluated = len(run) >= need
        if evaluated:
            if not long and bar.close > max(item.high for item in run[-entry_window:]):
                long = True
            elif long and bar.close < min(item.low for item in run[-exit_window:]):
                long = False
        run.append(bar)
    return long, (run if evaluated else None)


class DonchianBreakoutProvider:
    """``StrategyProvider`` for ``donchian_breakout@1.0.0``; deterministic, ``Decimal`` only."""

    def __init__(self, specs: Sequence[StrategySpec]) -> None:
        chosen = tuple(specs)
        if not chosen:
            raise ValueError("at least one explicit donchian_breakout spec is required")
        for spec in chosen:
            check_spec(
                spec, name=DONCHIAN_NAME, signals=DONCHIAN_SIGNALS, space=DONCHIAN_PARAM_SPACE
            )
        self._specs = {str(spec.ref): spec for spec in chosen}
        self._descriptor = StrategyProviderDescriptor(
            name="research_donchian_breakout",
            version="0.1.0",
            deterministic=True,
            supported_strategies=FrozenMapping(
                {key: spec.content_hash() for key, spec in self._specs.items()}
            ),
        )

    @property
    def descriptor(self) -> StrategyProviderDescriptor:
        return self._descriptor

    def target_positions(self, request: StrategyRequest) -> StrategyResult:
        if not isinstance(request, StrategyRequest):
            raise StrategyInputError("target_positions needs a StrategyRequest")
        if not self._descriptor.supports(request.strategy, request.spec_hash):
            raise UnsupportedStrategy(f"{request.strategy} with this spec hash is not supported")
        spec = self._specs[str(request.strategy)]
        params = resolve_params(spec, request.params)
        entry_window = positive_int(params["entry_window"], "entry_window")
        exit_window = positive_int(params["exit_window"], "exit_window")
        for item in request.signals:
            if item.signal not in DONCHIAN_SIGNALS:
                raise StrategyInputError(f"{spec.ref} does not consume signal {item.signal}")

        positions: list[TargetPosition] = []
        with localcontext(_CONTEXT):
            slice_weight = (Decimal(1) / len(request.instruments)).quantize(_WEIGHT_QUANTUM)
            for decision_time in request.decision_times:
                visible = request.visible_at(decision_time)
                for instrument in request.instruments:
                    series = [item for item in visible if item.instrument == instrument]
                    long, run = _replay(_bars(series), entry_window, exit_window)
                    positions.append(_position(decision_time, instrument, long, run, slice_weight))
        return StrategyResult.build(request, self._descriptor, positions)


def _position(
    decision_time: datetime,
    instrument: str,
    long: bool,
    run: list[_Bar] | None,
    slice_weight: Decimal,
) -> TargetPosition:
    if run is None:
        return TargetPosition(
            decision_time=decision_time,
            instrument=instrument,
            target_weight=Decimal(0),
            inputs_used=0,
        )
    used = [item for bar in run for item in bar.observations]
    return TargetPosition(
        decision_time=decision_time,
        instrument=instrument,
        target_weight=slice_weight if long else Decimal(0),
        inputs_used=len(used),
        latest_input_available_time=max(item.available_time for item in used),
    )

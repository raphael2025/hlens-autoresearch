"""Single-instrument z-score mean reversion, spot long / flat (ADR-0085 ``STR-MR-ZSCORE-001``).

Research code — never production (H5). Source: the strategy library entry ``STR-MR-ZSCORE-001``
(price z-score against a moving average; Bollinger bands). The project knowledge base holds no seed
item for it, so the spec's ``lineage`` is empty and the strategy is not a ``library.py`` entry
(every entry there must resolve a source) until such an item is seeded.

Signal: ``bar_close`` (``price_signals.py``), one close per ``event_time``. Rule, per instrument and
decision time ``t``, over the closes visible at ``t`` (``available_time <= t``, by ``event_time``),
replayed in order from a flat state:

- ``z = (close - SMA) / sd`` over the ``window`` latest closes of the current run, the current close
  included; ``sd`` is the population standard deviation (divide by ``window``; ``sqrt`` at 50
  significant digits);
- an explicit ``None`` close resets the state to flat and starts a new run (nothing is filled in);
  fewer than ``window`` closes in the run, or ``sd = 0`` (division by zero), make ``z`` missing:
  the state resets to flat;
- flat → long when ``z < -entry_z``; long → flat when ``z > -exit_z`` (``exit_z < entry_z``).

The target at ``t`` is the state after the latest visible close: long = an equal slice
``1 / len(instruments)`` of gross exposure (as ``tsmom_bars``), flat = 0. When ``z`` is missing at
the latest close the target is flat with ``inputs_used = 0``; otherwise ``inputs_used`` counts the
closes the replay read since the run began. The replay starts flat at the first visible close.
Spot long / flat only: no short target (short cost, gap ST-4, is undefined).

The parameter space is declared here (``ZSCORE_PARAM_SPACE``); a spec is built only at an explicit
declared point (ADR-0085 rule 2: no default) and a request outside the space is refused
(``UnsupportedStrategy``, C-T1). The z thresholds are ints: a spec holds no ``Decimal`` and a
request refuses numeric text, so an int is the only exact numeric type both sides accept (as
``xsmom_bars``'s ``gross_exposure``). The declared values are strategy parameters, not validation
thresholds.
"""

from __future__ import annotations

from collections.abc import Sequence
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
from core.domain.base import FrozenMapping
from core.domain.specs import StrategySpec
from research.strategies._declared import build_spec, check_spec, positive_int
from research.strategies._params import resolve_params
from research.strategies.price_signals import BAR_CLOSE_SIGNAL

__all__ = [
    "ZSCORE_NAME",
    "ZSCORE_PARAM_SPACE",
    "ZSCORE_SIGNALS",
    "ZScoreReversionProvider",
    "zscore_spec",
]

ZSCORE_NAME: Final = "zscore_reversion"
ZSCORE_SIGNALS: Final = (BAR_CLOSE_SIGNAL,)
#: Declared parameter space: moving-average window in bars (Bollinger's 20, and 60 / 240) and
#: entry / exit thresholds in standard deviations (enter below -1 or -2 sd, exit back above the
#: mean). Every point satisfies ``exit_z < entry_z``. Six points.
ZSCORE_PARAM_SPACE: Final[dict[str, tuple[str | int | float | bool, ...]]] = {
    "window": (20, 60, 240),
    "entry_z": (1, 2),
    "exit_z": (0,),
}
_SPEC_TIME: Final = datetime(2026, 9, 28, tzinfo=UTC)
_CONTEXT: Final = Context(prec=50, rounding=ROUND_HALF_EVEN)
_WEIGHT_QUANTUM: Final = Decimal("1e-18")


def _int(value: object, key: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise UnsupportedStrategy(f"{key} must be an int")
    return value


def _thresholds(entry_z: object, exit_z: object) -> tuple[int, int]:
    entry, exit_ = _int(entry_z, "entry_z"), _int(exit_z, "exit_z")
    if not exit_ < entry:
        raise UnsupportedStrategy("zscore_reversion needs exit_z < entry_z")
    return entry, exit_


def zscore_spec(*, window: int, entry_z: int, exit_z: int) -> StrategySpec:
    """``strategy:zscore_reversion@1.0.0`` at an explicit, declared parameter point."""
    try:
        _thresholds(entry_z, exit_z)
    except UnsupportedStrategy as exc:
        raise ValueError(str(exc)) from exc
    return build_spec(
        name=ZSCORE_NAME,
        created_at=_SPEC_TIME,
        lineage=(),
        signals=ZSCORE_SIGNALS,
        space=ZSCORE_PARAM_SPACE,
        point={"window": window, "entry_z": entry_z, "exit_z": exit_z},
    )


def _closes(series: Sequence[SignalObservation]) -> list[SignalObservation]:
    """The instrument's visible closes in ``event_time`` order."""
    for item in series:
        if item.value is not None and not isinstance(item.value, Decimal):
            raise StrategyInputError("bar_close values must be Decimal or None")
    return sorted(series, key=lambda item: item.event_time)


def _zscore(closes: Sequence[Decimal]) -> Decimal | None:
    """``(last - mean) / population sd`` of ``closes`` at 50 digits; ``None`` when ``sd = 0``."""
    with localcontext(_CONTEXT):
        count = len(closes)
        mean = sum(closes, Decimal(0)) / count
        variance = sum(((value - mean) ** 2 for value in closes), Decimal(0)) / count
        if variance == 0:
            return None
        return (closes[-1] - mean) / variance.sqrt()


def _replay(
    closes: Sequence[SignalObservation], window: int, entry_z: int, exit_z: int
) -> tuple[bool, list[SignalObservation] | None]:
    """``(long, run)`` after the latest close; ``run`` is ``None`` when ``z`` is missing there."""
    run: list[SignalObservation] = []
    long = False
    computable = False
    for item in closes:
        if not isinstance(item.value, Decimal):
            run, long, computable = [], False, False
            continue
        run.append(item)
        value = (
            _zscore([obs.value for obs in run[-window:] if isinstance(obs.value, Decimal)])
            if len(run) >= window
            else None
        )
        computable = value is not None
        if value is None:
            long = False
        elif not long and value < -entry_z:
            long = True
        elif long and value > -exit_z:
            long = False
    return long, (run if computable else None)


class ZScoreReversionProvider:
    """``StrategyProvider`` for ``zscore_reversion@1.0.0``; deterministic, ``Decimal`` only."""

    def __init__(self, specs: Sequence[StrategySpec]) -> None:
        chosen = tuple(specs)
        if not chosen:
            raise ValueError("at least one explicit zscore_reversion spec is required")
        for spec in chosen:
            check_spec(spec, name=ZSCORE_NAME, signals=ZSCORE_SIGNALS, space=ZSCORE_PARAM_SPACE)
        self._specs = {str(spec.ref): spec for spec in chosen}
        self._descriptor = StrategyProviderDescriptor(
            name="research_zscore_reversion",
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
        window = positive_int(params["window"], "window")
        entry_z, exit_z = _thresholds(params["entry_z"], params["exit_z"])
        for item in request.signals:
            if item.signal not in ZSCORE_SIGNALS:
                raise StrategyInputError(f"{spec.ref} does not consume signal {item.signal}")

        positions: list[TargetPosition] = []
        with localcontext(_CONTEXT):
            slice_weight = (Decimal(1) / len(request.instruments)).quantize(_WEIGHT_QUANTUM)
            for decision_time in request.decision_times:
                visible = request.visible_at(decision_time)
                for instrument in request.instruments:
                    closes = _closes([item for item in visible if item.instrument == instrument])
                    long, run = _replay(closes, window, entry_z, exit_z)
                    positions.append(_position(decision_time, instrument, long, run, slice_weight))
        return StrategyResult.build(request, self._descriptor, positions)


def _position(
    decision_time: datetime,
    instrument: str,
    long: bool,
    run: list[SignalObservation] | None,
    slice_weight: Decimal,
) -> TargetPosition:
    if run is None:
        return TargetPosition(
            decision_time=decision_time,
            instrument=instrument,
            target_weight=Decimal(0),
            inputs_used=0,
        )
    return TargetPosition(
        decision_time=decision_time,
        instrument=instrument,
        target_weight=slice_weight if long else Decimal(0),
        inputs_used=len(run),
        latest_input_available_time=max(item.available_time for item in run),
    )

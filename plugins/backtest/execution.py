"""Opt-in execution realism for ``BarBacktester`` (ADR-0038 note, 2026-09-26; carry-over:
ADR-0054). Simulation only.

``ExecutionModel`` switches on, one by one and only when its parameter is given explicitly:

- **participation cap** (``max_participation_rate`` + ``bar_volume``): a fill at a bar trades at
  most ``max_participation_rate × bar volume`` units. Under the ``next_bar_open`` execution model
  (``carry_over=False``) what does not fit is **cancelled at that bar and reported**
  (``UnfilledRemainder``): that model pins every fill of a target to its execution bar
  (``BacktestResult.check_answers`` / ``execution_bar``) with at most one fill per target. A
  strategy that re-issues its target at the next decision time is re-sized against the actual
  holdings, so the position converges bar by bar (re-targeting);
- **carry-over** (``carry_over=True``, ADR-0054; needs the cap): the backtester declares the
  ``next_bar_open_participation`` execution model instead. A target is sized once, at its execution
  bar, into a quantity change; what the cap does not let through **carries over** to the
  instrument's next bar open, until it is filled, superseded by a later target for the instrument
  (re-sized against the actual holdings; the old remainder is cancelled) or the data end. The bar
  volume comes from ``PriceBar.volume`` (a bar on the carry path without one is refused, never
  filled in); ``bar_volume`` is optional here and, when given, must agree with it. Each sized
  target's record is a ``FillRemainder`` in ``BacktestResult.remainders``;
- **square-root market impact** (``impact_coefficient`` + ``bar_volume``): the fill price moves
  against the trade by ``impact_coefficient × sqrt(participation)`` of the reference price, with
  ``participation = |quantity| / bar volume`` (= traded notional / bar volume notional). This is the
  impact model the G4 capacity check assumes (``research.validation.robustness.capacity_check``:
  ``coefficient * sqrt(participation)`` per unit traded), so the backtest and the capacity estimate
  price impact the same way. It adds to the cost model's slippage:
  buy ``open × (1 + slippage_rate + impact)``, sell ``open × (1 − slippage_rate − impact)``; the
  impact is therefore part of ``Fill.slippage_cost = |quantity| × |fill_price − open|``;
- **funding** (``short_borrow_rate`` and / or ``cash_borrow_rate``, each **per bar step**, i.e. per
  distinct ``interval_start`` of the grid — the caller converts an annual rate): after the trades at
  a bar's open, ``short_borrow_rate × Σ |short quantity × mark|`` plus ``cash_borrow_rate ×
  max(0, −cash)`` (leverage: a margin loan) is debited from cash. ``BacktestResult`` has no funding
  field (frozen contract), so the cost appears in ``EquityPoint.cash`` / ``equity`` and in
  ``final_equity`` / PnL, **not** in ``total_fees`` / ``total_slippage``; it is itemised in the
  ``ExecutionReport`` (``FundingCharge`` per bar step).

No parameter has a default value: ``None`` means *off* (``carry_over`` defaults to ``False``, the
pre-existing behaviour). Without carry-over, bar volume is supplied here — traded quantity per
``(instrument, interval_start)``, the same shape as
``research.strategies.validation.ValidatorSetup.bar_volume`` — and is bound, together with every
parameter, into ``fingerprint``, which ``BarBacktester`` puts into the descriptor's version build
metadata and thereby into ``BacktestResult.provider_hash`` (with carry-over, the volumes are in the
request's ``PriceBar.volume`` and so in ``request_hash``). A trade at a bar whose volume is missing
is refused (``BacktestInputError``), never filled in; a ``bar_volume`` entry that disagrees with the
bar's own ``PriceBar.volume`` is refused too (ADR-0054 §4).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import MappingProxyType
from typing import Final

from core.contracts.strategy import FillRemainder
from core.domain.base import content_hash

__all__ = [
    "IMPACT_MODEL",
    "ExecutionModel",
    "ExecutionReport",
    "FillExecution",
    "FundingCharge",
    "UnfilledRemainder",
]

#: The only impact model: the G4 capacity check's square-root law.
IMPACT_MODEL: Final = "square_root"
_REVISION: Final = 1

type VolumeKey = tuple[str, datetime]


def _decimal(value: object, label: str) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, Decimal):
        raise TypeError(f"{label} must be a Decimal (floats and ints are not accepted)")
    if not value.is_finite():
        raise ValueError(f"{label} must be finite")
    return value


def _rate(value: Decimal | None, label: str) -> Decimal | None:
    if value is None:
        return None
    rate = _decimal(value, label)
    if not Decimal(0) <= rate < Decimal(1):
        raise ValueError(f"{label} must lie in [0, 1) per bar step")
    return rate


def _canonical(value: Decimal | None) -> str | None:
    if value is None:
        return None
    return format(value.normalize(), "f")


def _utc(moment: object, label: str) -> datetime:
    if not isinstance(moment, datetime) or moment.utcoffset() != timedelta(0):
        raise ValueError(f"{label} must be a timezone-aware UTC datetime")
    return moment.astimezone(UTC)


@dataclass(frozen=True, eq=False)
class ExecutionModel:
    """Explicit, opt-in execution parameters; ``None`` = that behaviour is off (no defaults).

    - ``max_participation_rate``: in ``(0, 1]``; needs ``bar_volume``;
    - ``impact_coefficient``: ``>= 0``, square-root law (``IMPACT_MODEL``); needs ``bar_volume``;
    - ``short_borrow_rate`` / ``cash_borrow_rate``: in ``[0, 1)`` per bar step;
    - ``bar_volume``: traded quantity (``>= 0``) per ``(instrument, interval_start)``; required by,
      and only accepted with, the cap or the impact (optional with ``carry_over``, whose volumes
      come from ``PriceBar.volume``);
    - ``carry_over`` (ADR-0054): ``True`` carries the cap's remainder to later bars
      (``next_bar_open_participation``); needs ``max_participation_rate``.

    At least one behaviour must be switched on — with none, use ``BarBacktester()``.
    """

    max_participation_rate: Decimal | None = None
    impact_coefficient: Decimal | None = None
    short_borrow_rate: Decimal | None = None
    cash_borrow_rate: Decimal | None = None
    bar_volume: Mapping[VolumeKey, Decimal] | None = field(default=None, repr=False)
    carry_over: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.carry_over, bool):
            raise TypeError("carry_over must be a bool")
        if self.carry_over and self.max_participation_rate is None:
            raise ValueError("carry_over needs max_participation_rate (nothing to carry otherwise)")
        if self.max_participation_rate is not None:
            rate = _decimal(self.max_participation_rate, "max_participation_rate")
            if not Decimal(0) < rate <= Decimal(1):
                raise ValueError("max_participation_rate must lie in (0, 1]")
        if self.impact_coefficient is not None:
            if _decimal(self.impact_coefficient, "impact_coefficient") < 0:
                raise ValueError("impact_coefficient must be >= 0")
        _rate(self.short_borrow_rate, "short_borrow_rate")
        _rate(self.cash_borrow_rate, "cash_borrow_rate")
        if not (self.needs_volume or self.charges_funding):
            raise ValueError("an ExecutionModel must switch on at least one behaviour")
        if self.needs_volume and self.bar_volume is None and not self.carry_over:
            raise ValueError("the participation cap and the impact model need bar_volume")
        if not self.needs_volume and self.bar_volume is not None:
            raise ValueError("bar_volume is only accepted with the participation cap or impact")
        if self.bar_volume is not None:
            volumes: dict[VolumeKey, Decimal] = {}
            for key, volume in self.bar_volume.items():
                if not (isinstance(key, tuple) and len(key) == 2 and isinstance(key[0], str)):
                    raise ValueError("bar_volume keys are (instrument, interval_start)")
                if not key[0]:
                    raise ValueError("bar_volume instrument must be non-empty")
                moment = _utc(key[1], "bar_volume interval_start")
                if _decimal(volume, f"bar_volume[{key[0]}, {moment.isoformat()}]") < 0:
                    raise ValueError("bar_volume must be >= 0")
                if (key[0], moment) in volumes:
                    raise ValueError(f"bar_volume repeats {key[0]} @ {moment.isoformat()}")
                volumes[(key[0], moment)] = volume
            object.__setattr__(self, "bar_volume", MappingProxyType(volumes))
        object.__setattr__(self, "_fingerprint", content_hash(self._payload()))

    @property
    def needs_volume(self) -> bool:
        return self.max_participation_rate is not None or self.impact_coefficient is not None

    @property
    def charges_funding(self) -> bool:
        return self.short_borrow_rate is not None or self.cash_borrow_rate is not None

    @property
    def fingerprint(self) -> str:
        """SHA-256 of the canonical JSON of every parameter and every volume (lowercase hex)."""
        value: str = object.__getattribute__(self, "_fingerprint")
        return value

    def _payload(self) -> dict[str, object]:
        volumes = sorted(
            [name, moment.isoformat(), _canonical(volume)]
            for (name, moment), volume in (self.bar_volume or {}).items()
        )
        impact = (
            None
            if self.impact_coefficient is None
            else {"model": IMPACT_MODEL, "coefficient": _canonical(self.impact_coefficient)}
        )
        payload: dict[str, object] = {
            "execution_model": "hlens_bar_execution",
            "revision": _REVISION,
            "max_participation_rate": _canonical(self.max_participation_rate),
            "impact": impact,
            "short_borrow_rate": _canonical(self.short_borrow_rate),
            "cash_borrow_rate": _canonical(self.cash_borrow_rate),
            "bar_volume": volumes,
        }
        if self.carry_over:  # absent when off: pre-existing fingerprints stay bit-identical
            payload["carry_over"] = True
        return payload

    def volume(self, instrument: str, interval_start: datetime) -> Decimal | None:
        if self.bar_volume is None:
            return None
        return self.bar_volume.get((instrument, interval_start.astimezone(UTC)))


@dataclass(frozen=True)
class UnfilledRemainder:
    """A target's trade cut by the participation cap at its execution bar; the rest is cancelled
    (``next_bar_open`` only; with ``carry_over`` see ``ExecutionReport.carried``)."""

    instrument: str
    decision_time: datetime
    bar_time: datetime
    bar_volume: Decimal
    desired_quantity: Decimal
    filled_quantity: Decimal
    cancelled_quantity: Decimal


@dataclass(frozen=True)
class FillExecution:
    """Per fill (aligned with ``BacktestResult.fills``): participation and the impact's share of
    ``slippage_cost`` (``|quantity| × open × impact``, quantized); ``None`` without volume."""

    instrument: str
    fill_time: datetime
    bar_volume: Decimal | None
    participation: Decimal | None
    impact_cost: Decimal


@dataclass(frozen=True)
class FundingCharge:
    """Funding debited from cash at one bar step (after that bar's trades, at its open marks)."""

    time: datetime
    short_notional: Decimal
    borrowed_cash: Decimal
    cost: Decimal


@dataclass(frozen=True)
class ExecutionReport:
    """What ``BacktestResult`` cannot carry, bound to it by ``result_hash``.

    ``execution_fingerprint`` is ``None`` for the default (v1) backtester, whose report is empty.
    ``remainders`` lists the cap's cancelled remainders (``next_bar_open`` only); ``carried`` is,
    with ``carry_over``, exactly ``BacktestResult.remainders`` (ADR-0054), and empty otherwise.
    """

    result_hash: str
    execution_fingerprint: str | None
    fills: tuple[FillExecution, ...]
    remainders: tuple[UnfilledRemainder, ...]
    funding: tuple[FundingCharge, ...]
    total_impact: Decimal
    total_funding: Decimal
    carried: tuple[FillRemainder, ...] = ()

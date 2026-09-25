"""Shared label-window logic of the first OutcomeProviders (ADR-0037).

Alignment rules (``core/contracts/outcome.py``): the entry bar is the first bar with
``interval_start >= event_time`` and must start less than one bar length after the event; the
label window is ``[entry_time, entry_time + horizon]`` and must be covered by contiguous bars
(``end == next start``). A gap or missing data is never filled: the provider emits ``None``.
"""

from __future__ import annotations

from bisect import bisect_left
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import ROUND_HALF_EVEN, Context, Decimal
from typing import ClassVar, Final

from core.contracts.outcome import (
    OutcomeEvent,
    OutcomeLabel,
    OutcomeLabelSpec,
    OutcomeMethod,
    OutcomePriceBar,
    OutcomeProviderDescriptor,
    OutcomeRequest,
    OutcomeResult,
    UnsupportedOutcome,
)
from core.domain.base import FrozenMapping

__all__ = [
    "RETURN_SCALE",
    "LabelProviderBase",
    "LabelWindow",
    "label_window",
    "quantize_return",
    "unknown",
]

#: Decimal places of every label value.
RETURN_SCALE: Final = Decimal("1e-18")
_CONTEXT: Final = Context(prec=50, rounding=ROUND_HALF_EVEN)


def quantize_return(exit_price: Decimal, entry_price: Decimal) -> Decimal:
    """``exit / entry - 1`` at 50 significant digits, quantized half-even to 18 places."""
    raw = _CONTEXT.subtract(_CONTEXT.divide(exit_price, entry_price), Decimal(1))
    return raw.quantize(RETURN_SCALE, rounding=ROUND_HALF_EVEN, context=_CONTEXT)


@dataclass(frozen=True)
class LabelWindow:
    """The contiguous bars of one label window; ``complete`` iff they reach ``entry + horizon``."""

    entry_time: datetime
    bars: tuple[OutcomePriceBar, ...]
    complete: bool


def label_window(
    bars: Sequence[OutcomePriceBar], event_time: datetime, horizon: timedelta
) -> LabelWindow | None:
    """The window after ``event_time``; ``None`` when there is no timely entry bar."""
    index = bisect_left(bars, event_time, key=lambda bar: bar.interval_start)
    if index == len(bars):
        return None
    entry = bars[index]
    if entry.interval_start - event_time >= entry.interval_end - entry.interval_start:
        return None  # the entry would be delayed by a whole bar or more: missing data
    end = entry.interval_start + horizon
    run: list[OutcomePriceBar] = []
    broken = False
    for bar in bars[index:]:
        if bar.interval_end > end:
            break
        if run and bar.interval_start != run[-1].interval_end:
            broken = True
            break
        run.append(bar)
    complete = bool(run) and not broken and run[-1].interval_end == end
    return LabelWindow(entry_time=entry.interval_start, bars=tuple(run), complete=complete)


def unknown(event: OutcomeEvent) -> OutcomeLabel:
    """An explicitly not computable label (never filled)."""
    return OutcomeLabel(event_key=event.event_key, event_time=event.event_time, value=None)


class LabelProviderBase:
    """Descriptor handling shared by the label providers; subclasses implement ``_label``."""

    METHOD: ClassVar[OutcomeMethod]
    NAME: ClassVar[str]
    VERSION: ClassVar[str] = "1.0.0"

    def __init__(self, label_specs: Iterable[OutcomeLabelSpec]) -> None:
        specs = tuple(label_specs)
        if not specs:
            raise ValueError("a provider must serve at least one label spec")
        supported: dict[str, str] = {}
        for spec in specs:
            if spec.method is not self.METHOD:
                raise ValueError(f"{self.NAME} serves {self.METHOD.value}, not {spec.method.value}")
            key = str(spec.outcome)
            if key in supported:
                raise ValueError(f"two label specs for {key}")
            supported[key] = spec.content_hash()
        self._descriptor = OutcomeProviderDescriptor(
            name=self.NAME,
            version=self.VERSION,
            deterministic=True,
            supported_outcomes=FrozenMapping(supported),
        )

    @property
    def descriptor(self) -> OutcomeProviderDescriptor:
        return self._descriptor

    def compute(self, request: OutcomeRequest) -> OutcomeResult:
        if not self._descriptor.supports(request.label_spec):
            raise UnsupportedOutcome(f"{request.label_spec.outcome} is not served by {self.NAME}")
        labels = tuple(self._label(request, event) for event in request.events)
        return OutcomeResult.build(request, self._descriptor, labels)

    def _label(self, request: OutcomeRequest, event: OutcomeEvent) -> OutcomeLabel:
        raise NotImplementedError

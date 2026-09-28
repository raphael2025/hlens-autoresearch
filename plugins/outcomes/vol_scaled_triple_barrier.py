"""Volatility-scaled triple-barrier outcome (ADR-0088 decision 5, contract 2.4.0).

Same shape as ``triple_barrier`` (``plugins/outcomes/triple_barrier.py``): entry at the open of
the first bar at/after the event, vertical barrier at ``entry_time + horizon``, same-bar double
touch resolved pessimistically to the lower barrier, gaps exit at the open. The only difference is
how the upper/lower prices are set: instead of a fixed fraction of the entry price
(``OutcomeLabelSpec.upper_barrier`` / ``lower_barrier``), they are scaled by the volatility feature
value that is visible at the entry time::

    upper = entry * (1 + barrier_multiplier * volatility)
    lower = entry * (1 - barrier_multiplier * volatility)

``barrier_multiplier`` comes from the label spec (contract-validated ``> 0``). The volatility value
itself is **not** part of the frozen ``OutcomeRequest`` (ADR-0088 only extended
``OutcomeLabelSpec`` with a ``Ref[FEATURE]`` identifying *which* feature to use, not a data
channel for its values — that wiring, from a ``FeatureResult`` to a ``compute()`` call, is
explicitly out of this ADR's scope, left to a later plugins/research batch). This provider
therefore takes the resolved per-event volatility values as a constructor argument, scoped to one
``OutcomeRequest`` the same way ``label_specs`` already is — callers are expected to construct a
fresh instance per request, mirroring the existing ``ForwardReturnOutcome(label_specs=(spec,))``
call site in ``research/loop/operator_providers.py``.

**Missing-data convention (this module's choice, consistent with the rest of this package)**:
``plugins/outcomes/_window.py`` never fills a gap — an event without the data it needs gets an
explicit ``None`` label, never an exception, because "no data yet" is an expected, common state
(the same rule ``label_window`` already applies to incomplete/gapped price windows). This provider
applies the same rule to volatility: an event whose ``event_key`` is absent from ``volatility`` (or
maps to ``None``) is **not visible / missing** and yields ``unknown(event)``, exactly like a
gapped price window — this collapses ADR-0088's "不可见" (not visible yet) and "缺失" (absent)
into one case, because without a timestamped volatility series there is no way to tell them apart
from inside this provider, and the contract already places the burden of *timing* correctness (the
value must be the one visible at ``entry_time``, never a later revision) on the caller, the same
trust boundary ``OutcomeRequest.bars`` already relies on for PIT-selected prices.

A volatility value that **is** present but ``<= 0`` is different: it is not "no data", it is
malformed data (a non-positive scale collapses or inverts the barriers), so it is treated like the
existing contract-time barrier checks in ``core/contracts/outcome.py`` and ``OutcomePriceBar``'s
positive-price invariant — fail closed hard, via ``OutcomeInputError``, not a silent ``None``
label. The same applies when the scaled lower barrier would be non-positive
(``barrier_multiplier * volatility >= 1``): the barrier is nonsensical, not merely unknown.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from decimal import Decimal

from core.contracts.outcome import (
    OutcomeEvent,
    OutcomeInputError,
    OutcomeLabel,
    OutcomeLabelSpec,
    OutcomeMethod,
    OutcomePriceBar,
    OutcomeRequest,
)
from plugins.outcomes._window import LabelProviderBase, label_window, unknown
from plugins.outcomes.triple_barrier import _touch

__all__ = ["VolScaledTripleBarrierOutcome"]


class VolScaledTripleBarrierOutcome(LabelProviderBase):
    METHOD = OutcomeMethod.VOL_SCALED_TRIPLE_BARRIER
    NAME = "hlens_vol_scaled_triple_barrier"

    def __init__(
        self,
        label_specs: Iterable[OutcomeLabelSpec],
        *,
        volatility: Mapping[str, Decimal | None],
    ) -> None:
        """``volatility``: ``event_key`` → the volatility feature value visible at that event's
        entry time (the caller's responsibility, PIT-selected the same way price bars already
        are), or ``None`` / an absent key when no such value is visible yet.
        """
        super().__init__(label_specs)
        self._volatility = dict(volatility)

    def _label(self, request: OutcomeRequest, event: OutcomeEvent) -> OutcomeLabel:
        spec = request.label_spec
        window = label_window(request.bars, event.event_time, spec.horizon)
        if window is None or not window.bars:
            return unknown(event)
        if spec.barrier_multiplier is None:  # pragma: no cover — contract forbids it
            raise ValueError("vol_scaled_triple_barrier label spec without barrier_multiplier")
        volatility = self._volatility.get(event.event_key)
        if volatility is None:
            return unknown(event)
        if volatility <= 0:
            raise OutcomeInputError(
                f"{event.event_key!r}: volatility must be > 0 to scale a barrier, got {volatility}"
            )
        entry_price = window.bars[0].open
        scale = spec.barrier_multiplier * volatility
        upper = entry_price * (1 + scale)
        lower = entry_price * (1 - scale)
        if lower <= 0:
            raise OutcomeInputError(
                f"{event.event_key!r}: barrier_multiplier * volatility = {scale} collapses or "
                "inverts the lower barrier (must be < 1)"
            )
        used: list[OutcomePriceBar] = []
        for bar in window.bars:
            used.append(bar)
            if bar.low <= lower:
                exit_price = bar.open if bar.open <= lower else lower
                return _touch(event, window.entry_time, entry_price, exit_price, -1, used)
            if bar.high >= upper:
                exit_price = bar.open if bar.open >= upper else upper
                return _touch(event, window.entry_time, entry_price, exit_price, 1, used)
        if not window.complete:
            return unknown(event)
        return _touch(event, window.entry_time, entry_price, used[-1].close, 0, used)

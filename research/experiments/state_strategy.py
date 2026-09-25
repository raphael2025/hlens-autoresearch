"""State x Strategy performance decomposition (roadmap Phase 6; ADR-0039).

The strategy's return realized over ``(t, t_next]`` is attributed to the state **known at t**
(a ``StateValue`` evaluated at t from inputs visible at t; unknown states are ``None`` and kept
as their own cell, never dropped). For every state label the matrix reports count, mean return,
hit rate and total, plus the share of the total gains coming from the single best state and from
its largest few returns — the inputs of Constitution C-R2 ("must not depend on a few events of a
single state"). No threshold is applied here: acceptance numbers belong to the Validation Profile
(P4 / P8). Every conditional variant researched is registered as a ``conditioning`` hypothesis in
the ``TrialLedger`` so it counts as a trial (Constitution C-T1, roadmap P6 acceptance).

Wiring to Phase 5 (W1): ``matrix_from_backtest`` takes a P5 ``BacktestResult`` and a P2
``StateResult``. ``backtest_returns`` turns the equity curve into per-bar simple returns keyed by
the **start** of the period they are realized over: consecutive equity points ``(t, e_t)`` and
``(t_next, e_next)`` give ``e_next / e_t - 1`` at key ``t`` (the book held over ``(t, t_next]`` was
decided at or before ``t`` under ``next_bar_open``, and its return includes that bar's fees and
slippage). The first bar has no earlier mark in the result, so its return is not attributed. The
matrix records the ``result_hash`` of the backtest and of the state result it was built from.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime
from decimal import ROUND_HALF_EVEN, Context, Decimal, localcontext
from itertools import pairwise
from typing import Final

from core.contracts.state import StateResult
from core.contracts.strategy import BacktestResult
from core.domain.base import Ref, content_hash
from research.hypotheses import TrialLedger, conditioning

__all__ = [
    "RETURN_QUANTUM",
    "StateCell",
    "StateStrategyMatrix",
    "backtest_returns",
    "matrix_from_backtest",
    "register_conditionals",
    "state_strategy_matrix",
]

#: Per-bar returns derived from an equity curve are quantized to this step (half-even).
RETURN_QUANTUM: Final = Decimal("1e-18")
_CONTEXT: Final = Context(prec=50, rounding=ROUND_HALF_EVEN)


@dataclass(frozen=True, slots=True)
class StateCell:
    state: str | None
    count: int
    total: Decimal
    mean: Decimal | None
    hit_rate: Decimal | None
    top_returns: tuple[Decimal, ...]


@dataclass(frozen=True, slots=True)
class StateStrategyMatrix:
    strategy: Ref
    state: Ref
    cells: tuple[StateCell, ...]
    #: Share of positive total gains produced by the best cell (None when there are no gains).
    best_state_share: Decimal | None
    #: Share of the best cell's gains produced by its ``top_k`` largest returns.
    top_k_share_in_best_state: Decimal | None
    top_k: int
    #: ``result_hash`` of the P5 backtest the returns came from (None: a plain return mapping).
    backtest_result_hash: str | None = None
    #: ``result_hash`` of the P2 state result (None: a plain time -> label mapping).
    state_result_hash: str | None = None

    @property
    def matrix_hash(self) -> str:
        """Content hash of the whole matrix, its inputs' hashes included."""
        return content_hash(
            {
                "strategy": str(self.strategy),
                "state": str(self.state),
                "cells": [
                    {
                        "state": cell.state,
                        "count": cell.count,
                        "total": str(cell.total),
                        "mean": None if cell.mean is None else str(cell.mean),
                        "hit_rate": None if cell.hit_rate is None else str(cell.hit_rate),
                        "top_returns": [str(value) for value in cell.top_returns],
                    }
                    for cell in self.cells
                ],
                "best_state_share": _text(self.best_state_share),
                "top_k_share_in_best_state": _text(self.top_k_share_in_best_state),
                "top_k": self.top_k,
                "backtest_result_hash": self.backtest_result_hash,
                "state_result_hash": self.state_result_hash,
            }
        )

    def report(self) -> str:
        lines = [
            f"# {self.strategy} × {self.state}",
            "",
            f"backtest: {self.backtest_result_hash} · states: {self.state_result_hash}",
            "",
            "| state | n | mean | hit rate | total |",
            "|---|---|---|---|---|",
        ]
        for cell in self.cells:
            lines.append(
                f"| {cell.state if cell.state is not None else '(unknown)'} | {cell.count} | "
                f"{cell.mean} | {cell.hit_rate} | {cell.total} |"
            )
        lines += [
            "",
            f"best-state share of gains: {self.best_state_share}",
            f"top-{self.top_k} share inside the best state: {self.top_k_share_in_best_state}",
            "(no thresholds applied — the Validation Profile decides; Constitution C-R2)",
        ]
        return "\n".join(lines)


def state_strategy_matrix(
    strategy: Ref,
    state: Ref,
    returns: Mapping[datetime, Decimal],
    states: StateResult | Mapping[datetime, str | None],
    *,
    top_k: int = 5,
) -> StateStrategyMatrix:
    if top_k < 1:
        raise ValueError("top_k must be positive")
    known: Mapping[datetime, str | None] = (
        {value.evaluation_time: value.state for value in states.values}
        if isinstance(states, StateResult)
        else states
    )
    buckets: dict[str | None, list[Decimal]] = {}
    for at in sorted(returns):
        value = returns[at]
        if not isinstance(value, Decimal) or not value.is_finite():
            raise ValueError(f"the return at {at.isoformat()} must be a finite Decimal")
        if at not in known:
            raise ValueError(f"no state was evaluated at {at.isoformat()}")
        label = known[at]
        buckets.setdefault(None if label is None else str(label), []).append(value)
    cells = tuple(
        _cell(label, values)
        for label, values in sorted(
            buckets.items(), key=lambda item: (item[0] is None, item[0] or "")
        )
    )
    gains = {
        cell.state: sum((r for r in _values(buckets, cell.state) if r > 0), Decimal(0))
        for cell in cells
    }
    total_gain = sum(gains.values(), Decimal(0))
    best = max(gains, key=lambda label: gains[label]) if total_gain > 0 else None
    best_share = None if best is None else gains[best] / total_gain
    top_share = None
    if best is not None and gains[best] > 0:
        largest = sorted((r for r in _values(buckets, best) if r > 0), reverse=True)[:top_k]
        top_share = sum(largest, Decimal(0)) / gains[best]
    return StateStrategyMatrix(
        strategy=strategy,
        state=state,
        cells=cells,
        best_state_share=best_share,
        top_k_share_in_best_state=top_share,
        top_k=top_k,
        state_result_hash=states.result_hash if isinstance(states, StateResult) else None,
    )


def _text(value: Decimal | None) -> str | None:
    return None if value is None else str(value)


def backtest_returns(backtest: BacktestResult) -> dict[datetime, Decimal]:
    """Per-bar simple returns of a P5 equity curve, keyed by the start of each period.

    ``(t, e_t), (t_next, e_next)`` -> ``{t: e_next / e_t - 1}`` (50 digits, quantized to
    ``RETURN_QUANTUM``). A non-positive equity mark has no return: the curve is refused.
    """
    if not isinstance(backtest, BacktestResult):
        raise ValueError("backtest_returns needs a BacktestResult")
    out: dict[datetime, Decimal] = {}
    with localcontext(_CONTEXT):
        for earlier, later in pairwise(backtest.equity_curve):
            if earlier.equity <= 0:
                raise ValueError(
                    f"equity at {earlier.time.isoformat()} is not positive: no return is defined"
                )
            out[earlier.time] = (later.equity / earlier.equity - 1).quantize(RETURN_QUANTUM)
    return out


def matrix_from_backtest(
    strategy: Ref,
    state: Ref,
    backtest: BacktestResult,
    states: StateResult,
    *,
    top_k: int = 5,
) -> StateStrategyMatrix:
    """``state_strategy_matrix`` over a P5 backtest's per-bar returns and a P2 state result.

    The return over ``(t, t_next]`` goes to the state evaluated at ``t`` (known at ``t``); every
    period start must have a state evaluation (otherwise ``ValueError``). The matrix binds both
    inputs by ``result_hash``.
    """
    if not isinstance(states, StateResult):
        raise ValueError("matrix_from_backtest needs a StateResult")
    matrix = state_strategy_matrix(strategy, state, backtest_returns(backtest), states, top_k=top_k)
    return replace(matrix, backtest_result_hash=backtest.result_hash)


def _values(buckets: Mapping[str | None, list[Decimal]], label: str | None) -> list[Decimal]:
    return buckets[label]


def _cell(label: str | None, values: Sequence[Decimal]) -> StateCell:
    count = len(values)
    total = sum(values, Decimal(0))
    return StateCell(
        state=label,
        count=count,
        total=total,
        mean=total / count if count else None,
        hit_rate=Decimal(sum(1 for v in values if v > 0)) / count if count else None,
        top_returns=tuple(sorted(values, reverse=True)[:5]),
    )


def register_conditionals(
    ledger: TrialLedger,
    *,
    strategy: Ref,
    state: Ref,
    labels: Sequence[str],
    family_id: str,
    minimum_effect: str,
) -> int:
    """Register one conditioning hypothesis per label researched; returns the family's trials."""
    for label in labels:
        ledger.register(
            conditioning(
                f"h_{strategy.name}_given_{state.name}_{label}",
                family_id,
                strategy,
                state,
                label,
                minimum_effect,
            )
        )
    return ledger.trials(family_id)

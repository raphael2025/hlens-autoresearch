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

Pre-committed cells (code completion, 2026-09-26): ``register_matrix_conditionals`` derives the
conditioning hypotheses from the declared ``StateSpec.state_space`` of the matrix's state plus the
unknown-state cell — never from caller-chosen labels, never filtered by results — and registers
all of them in the ``TrialLedger`` (all or none) before it reports anything per cell. A declared
label the returns never visited is a zero-support cell and is still a trial. Sample support is
reported per cell against ``min_support``, a required caller parameter with no default: ``None``
reports every cell as unsupported (no stated threshold), and a cell below the threshold is
reported unsupported, never dropped. No verdict is produced here (Validation Profile, P4 / P8).
``register_conditionals`` (caller-supplied labels) is kept for compatibility.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime
from decimal import ROUND_HALF_EVEN, Context, Decimal, localcontext
from itertools import pairwise
from typing import Final, Literal

from core.contracts.state import StateResult
from core.contracts.strategy import BacktestResult
from core.domain.base import Ref, content_hash
from core.domain.research import Hypothesis
from core.domain.specs import StateSpec
from research.hypotheses import LedgerError, TrialLedger, conditioning

__all__ = [
    "RETURN_QUANTUM",
    "UNKNOWN_STATE_VALUE",
    "CellSupport",
    "ConditionalRegistration",
    "StateCell",
    "StateStrategyMatrix",
    "backtest_returns",
    "conditional_hypotheses",
    "matrix_from_backtest",
    "register_conditionals",
    "register_matrix_conditionals",
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


#: The ``state_value`` text of the unknown-state (``None``) cell's conditioning hypothesis.
UNKNOWN_STATE_VALUE: Final = "<unknown>"

type SupportReason = Literal["no_support_threshold", "below_min_support", "meets_min_support"]


@dataclass(frozen=True, slots=True)
class CellSupport:
    """One pre-registered cell: its trial and its sample support (no verdict)."""

    state: str | None
    count: int
    #: ``Ref`` string of the cell's registered conditioning hypothesis.
    hypothesis: str
    #: 1-based position of the cell's registration among the family's trials.
    trial_index: int
    supported: bool
    reason: SupportReason


@dataclass(frozen=True, slots=True)
class ConditionalRegistration:
    """Every cell of one matrix registered as a trial, with per-cell sample support."""

    matrix_hash: str
    family_id: str
    #: The caller's support threshold (``None``: not stated -> every cell unsupported).
    min_support: int | None
    #: Declared state-space order, then the unknown-state cell; never filtered.
    cells: tuple[CellSupport, ...]
    #: Hypotheses this call added to the ledger (0 when every cell was already registered).
    newly_registered: int
    #: ``TrialLedger.trials(family_id)`` after the registration.
    family_trials: int


def _conditional_name(strategy: Ref, state: Ref, label: str | None) -> str:
    suffix = "unknown_state" if label is None else label
    return f"h_{strategy.name}_given_{state.name}_{suffix}"


def conditional_hypotheses(
    *, strategy: Ref, state_spec: StateSpec, family_id: str, minimum_effect: str
) -> tuple[tuple[str | None, Hypothesis], ...]:
    """One conditioning hypothesis per declared label, then one for the unknown-state cell.

    Depends only on the declared state space (known before any result); a label cell's
    hypothesis is exactly the one ``register_conditionals`` registers for that label.
    """
    if not isinstance(state_spec, StateSpec):
        raise ValueError("conditional_hypotheses needs the StateSpec that declares the states")
    cells: list[str | None] = [*state_spec.state_space, None]
    names = [_conditional_name(strategy, state_spec.ref, label) for label in cells]
    if len(set(names)) != len(names):
        raise ValueError(f"cell hypothesis names collide: {sorted(names)}")
    return tuple(
        (
            label,
            conditioning(
                name,
                family_id,
                strategy,
                state_spec.ref,
                UNKNOWN_STATE_VALUE if label is None else label,
                minimum_effect,
            ),
        )
        for label, name in zip(cells, names, strict=True)
    )


def _support(count: int, min_support: int | None) -> tuple[bool, SupportReason]:
    if min_support is None:
        return False, "no_support_threshold"
    if count < min_support:
        return False, "below_min_support"
    return True, "meets_min_support"


def register_matrix_conditionals(
    ledger: TrialLedger,
    matrix: StateStrategyMatrix,
    *,
    state_spec: StateSpec,
    family_id: str,
    minimum_effect: str,
    min_support: int | None,
) -> ConditionalRegistration:
    """Register every cell of ``matrix`` as a trial, then report per-cell sample support.

    The cells are the declared ``state_spec.state_space`` plus the unknown-state cell, whatever
    the returns visited (module docs). ``state_spec`` must be the matrix's state; a matrix cell
    outside the declared space is refused. Registration is all or none: a conflict with an
    existing ledger entry (same name and version, other content) raises ``LedgerError`` before
    anything is registered; re-registering identical cells adds no trial. ``min_support`` is
    required (no default) and, when given, a positive int.
    """
    if not isinstance(matrix, StateStrategyMatrix):
        raise ValueError("register_matrix_conditionals needs a StateStrategyMatrix")
    if min_support is not None and (
        isinstance(min_support, bool) or not isinstance(min_support, int) or min_support < 1
    ):
        raise ValueError("min_support must be a positive int, or None when no threshold is stated")
    if not isinstance(state_spec, StateSpec) or state_spec.ref != matrix.state:
        raise ValueError(f"state_spec must declare the matrix's state {matrix.state}")
    declared = set(state_spec.state_space)
    stray = sorted(c.state for c in matrix.cells if c.state is not None and c.state not in declared)
    if stray:
        raise ValueError(f"matrix cells outside the declared state space: {stray}")
    planned = conditional_hypotheses(
        strategy=matrix.strategy,
        state_spec=state_spec,
        family_id=family_id,
        minimum_effect=minimum_effect,
    )
    existing = {(h.name, h.version): h for h in ledger.hypotheses}
    for _, hypothesis in planned:
        known = existing.get((hypothesis.name, hypothesis.version))
        if known is not None and known.content_hash() != hypothesis.content_hash():
            raise LedgerError(f"{hypothesis.ref} is registered with other content: new version")
    newly = sum(1 for _, hypothesis in planned if ledger.register(hypothesis))
    counts = {cell.state: cell.count for cell in matrix.cells}
    cells: list[CellSupport] = []
    for label, hypothesis in planned:
        count = counts.get(label, 0)
        supported, reason = _support(count, min_support)
        cells.append(
            CellSupport(
                state=label,
                count=count,
                hypothesis=str(hypothesis.ref),
                trial_index=ledger.trial_index(hypothesis),
                supported=supported,
                reason=reason,
            )
        )
    return ConditionalRegistration(
        matrix_hash=matrix.matrix_hash,
        family_id=family_id,
        min_support=min_support,
        cells=tuple(cells),
        newly_registered=newly,
        family_trials=ledger.trials(family_id),
    )

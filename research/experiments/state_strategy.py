"""State x Strategy performance decomposition (roadmap Phase 6; ADR-0039).

The strategy's return realized over ``(t, t_next]`` is attributed to the state **known at t**
(a ``StateValue`` evaluated at t from inputs visible at t; unknown states are ``None`` and kept
as their own cell, never dropped). For every state label the matrix reports count, mean return,
hit rate and total, plus the share of the total gains coming from the single best state and from
its largest few returns — the inputs of Constitution C-R2 ("must not depend on a few events of a
single state"). No threshold is applied here: acceptance numbers belong to the Validation Profile
(P4 / P8). Every conditional variant researched is registered as a ``conditioning`` hypothesis in
the ``TrialLedger`` so it counts as a trial (Constitution C-T1, roadmap P6 acceptance).

With an explicit positive ``max_state_age``, an optional as-of mode attributes each return to the
latest state evaluation at or before its start time. Missing history and stale states become the
unknown cell. Omitting it preserves exact-time alignment.

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

Trial-keyed cells for the continuous loop (2026-09-26, CODE_COMPLETE / DEBUG_PENDING; decided by
Claude under Raphael's 2026-09-26 autonomous-decision instruction): ``register_trial_conditionals``
registers the same declared cells, keyed by the hypothesis whose trial produced the matrix
(``trial_conditional_hypotheses``), in that hypothesis's family, one trial per cell and look —
the parent's first trial registers the cells, a re-evaluation (``attempt``) registers one
re-evaluation of every cell under the same key. Used by ``research/loop`` only with an explicit
``ConditionalPlan``. Per-cell validation is not done here: the loop's ``ValidationStage`` runs
in-sample G0 – G3 on each supported cell only when the plan says ``validate_cells=True``
(``research.loop.trials``, **Per-cell validation**).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
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
    "register_trial_conditionals",
    "state_strategy_matrix",
    "trial_conditional_hypotheses",
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
    #: Explicit as-of horizon; None preserves exact-time alignment.
    max_state_age: timedelta | None = None
    #: State lookup rule; included in the matrix identity when using the additive as-of mode.
    alignment_mode: Literal["exact", "as_of"] = "exact"

    def __post_init__(self) -> None:
        if self.alignment_mode not in ("exact", "as_of"):
            raise ValueError("alignment_mode must be 'exact' or 'as_of'")
        if self.alignment_mode == "exact" and self.max_state_age is not None:
            raise ValueError("exact alignment cannot carry max_state_age")
        if self.alignment_mode == "as_of" and (
            not isinstance(self.max_state_age, timedelta) or self.max_state_age <= timedelta(0)
        ):
            raise ValueError("as_of alignment requires a positive max_state_age")

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
                **(
                    {
                        "alignment_mode": self.alignment_mode,
                        "max_state_age": str(self.max_state_age),
                    }
                    if self.alignment_mode == "as_of"
                    else {}
                ),
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
        if self.alignment_mode == "as_of":
            lines.insert(2, f"state alignment: as_of (maximum age {self.max_state_age})")
        return "\n".join(lines)


def state_strategy_matrix(
    strategy: Ref,
    state: Ref,
    returns: Mapping[datetime, Decimal],
    states: StateResult | Mapping[datetime, str | None],
    *,
    top_k: int = 5,
    max_state_age: timedelta | None = None,
) -> StateStrategyMatrix:
    if top_k < 1:
        raise ValueError("top_k must be positive")
    if max_state_age is not None and (
        not isinstance(max_state_age, timedelta) or max_state_age <= timedelta(0)
    ):
        raise ValueError("max_state_age must be a positive timedelta, or None for exact alignment")
    known: Mapping[datetime, str | None] = (
        {value.evaluation_time: value.state for value in states.values}
        if isinstance(states, StateResult)
        else states
    )
    state_at = (
        _asof_states(known, sorted(returns), max_state_age) if max_state_age is not None else known
    )
    buckets: dict[str | None, list[Decimal]] = {}
    for at in sorted(returns):
        value = returns[at]
        if not isinstance(value, Decimal) or not value.is_finite():
            raise ValueError(f"the return at {at.isoformat()} must be a finite Decimal")
        if max_state_age is None and at not in state_at:
            raise ValueError(f"no state was evaluated at {at.isoformat()}")
        label = state_at[at]
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
        max_state_age=max_state_age,
        alignment_mode="as_of" if max_state_age is not None else "exact",
        state_result_hash=states.result_hash if isinstance(states, StateResult) else None,
    )


def _asof_states(
    states: Mapping[datetime, str | None],
    return_times: Sequence[datetime],
    max_state_age: timedelta,
) -> dict[datetime, str | None]:
    """Resolve each return to the latest UTC state evaluation at or before its start time."""
    times = [*states, *return_times]
    if any(not isinstance(at, datetime) or at.utcoffset() != timedelta(0) for at in times):
        raise ValueError("as-of state and return times must be UTC datetimes")
    evaluated = sorted(states)
    out: dict[datetime, str | None] = {}
    index = 0
    latest: datetime | None = None
    for at in return_times:
        while index < len(evaluated) and evaluated[index] <= at:
            latest = evaluated[index]
            index += 1
        if latest is None or at - latest > max_state_age:
            out[at] = None
        else:
            out[at] = states[latest]
    return out


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
    max_state_age: timedelta | None = None,
) -> StateStrategyMatrix:
    """``state_strategy_matrix`` over a P5 backtest's per-bar returns and a P2 state result.

    The return over ``(t, t_next]`` goes to the state evaluated at ``t`` (known at ``t``). In
    exact mode every period start must have a state evaluation (otherwise ``ValueError``). When
    ``max_state_age`` is provided, each start uses the latest state evaluation at or before it,
    with stale / absent history assigned to unknown. The matrix binds both inputs by
    ``result_hash``.
    """
    if not isinstance(states, StateResult):
        raise ValueError("matrix_from_backtest needs a StateResult")
    matrix = state_strategy_matrix(
        strategy,
        state,
        backtest_returns(backtest),
        states,
        top_k=top_k,
        max_state_age=max_state_age,
    )
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
    #: ``register_trial_conditionals``: the ``Ref`` string of the trial's hypothesis (else None).
    parent: str | None = None
    #: ``register_trial_conditionals``: the look's attempt key (``None``: the first look).
    attempt: str | None = None


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


def _checked_matrix(
    matrix: StateStrategyMatrix, state_spec: StateSpec, min_support: int | None, caller: str
) -> None:
    if not isinstance(matrix, StateStrategyMatrix):
        raise ValueError(f"{caller} needs a StateStrategyMatrix")
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


def _register_cells(
    ledger: TrialLedger,
    matrix: StateStrategyMatrix,
    planned: tuple[tuple[str | None, Hypothesis], ...],
    *,
    family_id: str,
    min_support: int | None,
    attempt: str | None,
    parent: Hypothesis | None,
) -> ConditionalRegistration:
    """All or none: every conflict is found before the first registration (see callers)."""
    existing = {(h.name, h.version): h for h in ledger.hypotheses}
    for _, hypothesis in planned:
        known = existing.get((hypothesis.name, hypothesis.version))
        if known is not None and known.content_hash() != hypothesis.content_hash():
            raise LedgerError(f"{hypothesis.ref} is registered with other content: new version")
        if attempt is not None and known is None:
            raise LedgerError(
                f"{hypothesis.ref} is not registered: a re-evaluation look ({attempt}) needs the "
                "cell's first look registered first"
            )
    if attempt is None:
        newly = sum(1 for _, hypothesis in planned if ledger.register(hypothesis))
    else:
        newly = sum(
            1 for _, hypothesis in planned if ledger.register_reevaluation(hypothesis, attempt)
        )
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
                trial_index=ledger.trial_index(hypothesis, attempt),
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
        parent=None if parent is None else str(parent.ref),
        attempt=attempt,
    )


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
    _checked_matrix(matrix, state_spec, min_support, "register_matrix_conditionals")
    planned = conditional_hypotheses(
        strategy=matrix.strategy,
        state_spec=state_spec,
        family_id=family_id,
        minimum_effect=minimum_effect,
    )
    return _register_cells(
        ledger,
        matrix,
        planned,
        family_id=family_id,
        min_support=min_support,
        attempt=None,
        parent=None,
    )


def trial_conditional_hypotheses(
    *, parent: Hypothesis, strategy: Ref, state_spec: StateSpec, minimum_effect: str
) -> tuple[tuple[str | None, Hypothesis], ...]:
    """The cell hypotheses of one trial of ``parent``: declared labels, then the unknown cell.

    Same cells and statements as ``conditional_hypotheses``, but keyed by the trial's hypothesis
    instead of the bare strategy, so each hypothesis a family evaluates (another parameter point
    of the same strategy, an offspring, an LLM draft) has its own conditional trials: name
    ``<parent name>_given_<state name>_<label | unknown_state>``, the parent's version and
    family, and ``parent.ref`` appended to ``origin_refs`` (traceable to the trial it decomposes).
    Depends only on the parent, the strategy and the declared state space — never on a result.
    """
    if not isinstance(parent, Hypothesis):
        raise ValueError("trial_conditional_hypotheses needs the trial's Hypothesis")
    if not isinstance(state_spec, StateSpec):
        raise ValueError(
            "trial_conditional_hypotheses needs the StateSpec that declares the states"
        )
    cells: list[str | None] = [*state_spec.state_space, None]
    names = [
        f"{parent.name}_given_{state_spec.ref.name}_"
        + ("unknown_state" if label is None else label)
        for label in cells
    ]
    if len(set(names)) != len(names):
        raise ValueError(f"cell hypothesis names collide: {sorted(names)}")
    planned: list[tuple[str | None, Hypothesis]] = []
    for label, name in zip(cells, names, strict=True):
        base = conditioning(
            name,
            parent.family_id,
            strategy,
            state_spec.ref,
            UNKNOWN_STATE_VALUE if label is None else label,
            minimum_effect,
        )
        planned.append(
            (
                label,
                Hypothesis.model_validate(
                    {
                        **base.model_dump(),
                        "version": parent.version,
                        "origin_refs": (*base.origin_refs, parent.ref),
                    }
                ),
            )
        )
    return tuple(planned)


def register_trial_conditionals(
    ledger: TrialLedger,
    matrix: StateStrategyMatrix,
    *,
    parent: Hypothesis,
    attempt: str | None,
    state_spec: StateSpec,
    minimum_effect: str,
    min_support: int | None,
) -> ConditionalRegistration:
    """Register every cell of one trial's ``matrix`` as a trial of ``parent``'s family.

    The continuous loop's form of ``register_matrix_conditionals`` (research/loop, opt-in
    ``ConditionalPlan``): the cells are ``trial_conditional_hypotheses`` (declared state space +
    unknown cell, keyed by ``parent``) and each look at them is one counted trial, mirroring the
    parent's own trial: ``attempt=None`` (the parent's first trial) registers the cell hypotheses;
    ``attempt=<key>`` (a pre-registered re-evaluation of the parent) registers one re-evaluation
    of every cell under the same key (``TrialLedger.register_reevaluation``) and requires the
    cells' first look to be registered. All or none; idempotent (the same look again adds no
    trial). ``min_support`` is required (no default), as in ``register_matrix_conditionals``;
    ``matrix.strategy`` must be the strategy the parent's trial ran.
    """
    _checked_matrix(matrix, state_spec, min_support, "register_trial_conditionals")
    if attempt is not None and (not isinstance(attempt, str) or not attempt.strip()):
        raise ValueError("attempt must be None (the first look) or a non-blank attempt key")
    planned = trial_conditional_hypotheses(
        parent=parent,
        strategy=matrix.strategy,
        state_spec=state_spec,
        minimum_effect=minimum_effect,
    )
    return _register_cells(
        ledger,
        matrix,
        planned,
        family_id=parent.family_id,
        min_support=min_support,
        attempt=None if attempt is None else attempt.strip(),
        parent=parent,
    )

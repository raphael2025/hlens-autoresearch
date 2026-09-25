"""Shared fixtures for the Phase 8 robustness (G4) tests.

!!! TEST ONLY !!!  ``G4_TEST_ONLY_PROFILE`` extends the Phase 4 ``TEST_ONLY_PROFILE`` with
arbitrary, **uncalibrated** robustness numbers chosen to make the smoke tests readable. They are
not a proposal, not a calibration result and must never be used for research or copied into a real
Validation Profile (Profile numbers remain TBD until the Step 2 freeze, ADR-0007 / ADR-0041).
``G4_TEST_ONLY_PARAMS`` are likewise arbitrary TEST ONLY values for the rules without a Profile
field.

The trial families are built on ``plugins/synthetic`` ``RandomWalkMarket`` returns:

- ``noise_family``: N seeded random long / short strategies on a pure-noise market — the classic
  best-of-N overfit: whichever wins in sample does so by luck;
- ``momentum_family``: sign-of-trailing-return strategies over several lookbacks on a market with
  a planted lag-1 autocorrelation — a genuine effect the short lookback captures.
"""

from __future__ import annotations

import random
from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta
from decimal import Decimal

from core.contracts.synthetic import SyntheticMarket
from core.contracts.validation_profile import ParameterStabilityParams, SignificanceParams
from plugins.synthetic import RandomWalkMarket
from research.validation.g4 import RobustnessInput, RobustnessParams
from research.validation.overfitting import sharpe_ratio
from research.validation.returns import ParamPoint, PeriodReturns, TrialReturns
from research.validation.robustness import CapacityFill, StateTrade
from tests.research.validation.fixtures import TEST_ONLY_PROFILE, market_spec

#: TEST ONLY — arbitrary, uncalibrated numbers (see module docstring).
G4_TEST_ONLY_PROFILE = TEST_ONLY_PROFILE.model_copy(
    update={
        "name": "test_only_g4_uncalibrated",
        "significance": SignificanceParams(
            multiple_testing_method="bonferroni",
            multiple_testing_threshold=0.01,
            overfitting_metric="pbo_cscv",
            overfitting_threshold=0.2,
            trial_count_scope="family",
        ),
        "parameter_stability": ParameterStabilityParams(
            neighborhood_definition="adjacent_grid",
            min_neighborhood_performance_ratio=0.5,
            min_positive_neighbor_fraction=0.5,
            time_alignment_offsets=(timedelta(minutes=1),),
        ),
    }
)
#: TEST ONLY — the same profile gated on the Deflated Sharpe ratio instead of PBO.
G4_TEST_ONLY_DSR_PROFILE = G4_TEST_ONLY_PROFILE.model_copy(
    update={
        "name": "test_only_g4_dsr_uncalibrated",
        "significance": G4_TEST_ONLY_PROFILE.significance.model_copy(
            update={"overfitting_metric": "deflated_sharpe"}
        ),
    }
)
#: TEST ONLY — explicit parameters for rules without a Profile field.
G4_TEST_ONLY_PARAMS = RobustnessParams(
    cscv_partitions=10,
    max_participation_rate=0.01,
    min_capacity=None,
    impact_coefficient=0.1,
    cross_asset_min_positive_fraction=0.5,
    max_undersampled_pnl_share=None,
)
NO_EXPLICIT_PARAMS = RobustnessParams(
    cscv_partitions=None,
    max_participation_rate=None,
    min_capacity=None,
    impact_coefficient=None,
    cross_asset_min_positive_fraction=None,
    max_undersampled_pnl_share=None,
)
COST_PER_UNIT = Decimal("0.00001")  # per unit of position change (TEST ONLY)
MINUTES = 1440


def market(seed: int, strength: str | None = None) -> SyntheticMarket:
    return RandomWalkMarket().generate(market_spec(seed, strength, minutes=MINUTES))


def _bar_returns(mkt: SyntheticMarket) -> tuple[list[Decimal], list[datetime]]:
    closes = [bar.close for bar in mkt.bars]
    returns = [Decimal(0)] + [b / a - 1 for a, b in zip(closes, closes[1:], strict=False)]
    return returns, [bar.interval_end for bar in mkt.bars]


def _run(mkt: SyntheticMarket, positions: Sequence[int], delay: int = 0) -> PeriodReturns:
    """Position ``p[t]`` decided at the end of bar ``t`` is held over bar ``t + 1 + delay``;
    changing it costs ``COST_PER_UNIT`` per unit in the period it starts earning."""
    returns, times = _bar_returns(mkt)
    held = [positions[t - delay] if t >= delay else 0 for t in range(len(returns))]
    gross = [Decimal(0)] + [held[t - 1] * returns[t] for t in range(1, len(returns))]
    before = [0, 0] + held
    cost = [Decimal(0)] + [
        abs(held[t - 1] - before[t]) * COST_PER_UNIT for t in range(1, len(returns))
    ]
    return PeriodReturns(times=tuple(times), gross=tuple(gross), cost=tuple(cost))


def random_positions(count: int, seed: int, block: int) -> list[int]:
    rng = random.Random(seed)
    out: list[int] = []
    side = 1
    for t in range(count):
        if t % block == 0:
            side = rng.choice((-1, 1))
        out.append(side)
    return out


def momentum_positions(mkt: SyntheticMarket, lookback: int) -> list[int]:
    returns, _ = _bar_returns(mkt)
    out: list[int] = []
    for t in range(len(returns)):
        window = sum(returns[max(1, t - lookback + 1) : t + 1], Decimal(0)) if t >= lookback else 0
        out.append((window > 0) - (window < 0))
    return out


def noise_family(seed: int, variants: int = 40) -> tuple[SyntheticMarket, list[TrialReturns]]:
    mkt = market(seed)
    trials = [
        TrialReturns(
            params={"variant": k},
            returns=_run(mkt, random_positions(len(mkt.bars), 1000 + k, 30)),
        )
        for k in range(variants)
    ]
    return mkt, trials


LOOKBACKS = (1, 2, 3, 4, 5, 6)


def momentum_family(seed: int, strength: str) -> tuple[SyntheticMarket, list[TrialReturns]]:
    mkt = market(seed, strength)
    trials = [
        TrialReturns(params={"lookback": n}, returns=_run(mkt, momentum_positions(mkt, n)))
        for n in LOOKBACKS
    ]
    return mkt, trials


def best(trials: Sequence[TrialReturns]) -> TrialReturns:
    """The data-snooper's pick: the best in-sample Sharpe ratio of the whole family."""
    return max(trials, key=lambda trial: sharpe_ratio(trial.returns.net_floats()))


def state_trades(returns: PeriodReturns, hours: int = 1) -> tuple[StateTrade, ...]:
    """One trade per ``hours`` block, labelled by a calendar state (UTC morning / afternoon)."""
    out: list[StateTrade] = []
    block = hours * 60
    for start in range(0, len(returns) - block, block):
        part = slice(start, start + block)
        net = sum(returns.net()[part], Decimal(0))
        begin, end = returns.times[start], returns.times[start + block]
        out.append(StateTrade("am" if begin.hour < 12 else "pm", begin, end, net))
    return tuple(out)


def capacity_fills(returns: PeriodReturns, volume: Decimal) -> tuple[CapacityFill, ...]:
    return tuple(
        CapacityFill(time=t, traded_fraction=c / COST_PER_UNIT, bar_volume_notional=volume)
        for t, c in zip(returns.times, returns.cost, strict=True)
        if c > 0
    )


def robustness_input(
    trials: Sequence[TrialReturns],
    chosen: ParamPoint,
    space: Mapping[str, tuple[str | int | float | bool, ...]],
    *,
    profile: object = G4_TEST_ONLY_PROFILE,
    params: RobustnessParams = G4_TEST_ONLY_PARAMS,
    delayed: PeriodReturns | None = None,
    shifted: dict[timedelta, PeriodReturns] | None = None,
    states: bool = True,
    per_asset: dict[str, PeriodReturns] | None = None,
    declared: tuple[str, ...] = ("SYN-USDT",),
    family_trial_count: int | None = None,
) -> RobustnessInput:
    chosen_returns = next(t.returns for t in trials if dict(t.params) == dict(chosen))
    return RobustnessInput(
        profile=profile,  # type: ignore[arg-type]
        family_trial_count=family_trial_count or len(trials),
        param_space=space,
        chosen=chosen,
        trials=tuple(trials),
        delayed=delayed,
        time_shifted=shifted or {},
        state_trades=state_trades(chosen_returns) if states else None,
        capacity_fills=capacity_fills(chosen_returns, Decimal(1_000_000)),
        per_asset=per_asset if per_asset is not None else {"SYN-USDT": chosen_returns},
        declared_instruments=declared,
        params=params,
    )

"""Phase 9: the seeded synthetic market (ADR-0042; ADR-0088 decision 4 for GARCH / jump)."""

from __future__ import annotations

import random
from datetime import UTC, datetime
from decimal import ROUND_HALF_EVEN, Context, Decimal, localcontext

import pytest
from pydantic import ValidationError

from core.contracts.synthetic import (
    JumpEffect,
    PlantedEffect,
    SyntheticBar,
    SyntheticMarketSpec,
    VolatilityClusteringEffect,
)
from plugins.synthetic import RandomWalkMarket


def _spec(**overrides: object) -> SyntheticMarketSpec:
    fields: dict[str, object] = {
        "name": "noise",
        "version": "1.0.0",
        "symbol": "SYN-USDT",
        "start": datetime(2024, 1, 1, tzinfo=UTC),
        "minutes": 500,
        "seed": 7,
        "initial_price": Decimal("100"),
        "volatility": Decimal("0.001"),
    }
    fields.update(overrides)
    return SyntheticMarketSpec(**fields)  # type: ignore[arg-type]


def _returns(bars: tuple[SyntheticBar, ...]) -> list[float]:
    return [float(bar.close / bar.open - 1) for bar in bars]


def _autocorrelation(values: list[float], lag: int) -> float:
    mean = sum(values) / len(values)
    num = sum((values[i] - mean) * (values[i - lag] - mean) for i in range(lag, len(values)))
    den = sum((v - mean) ** 2 for v in values)
    return num / den


def test_the_same_seed_gives_the_same_market() -> None:
    a = RandomWalkMarket().generate(_spec())
    b = RandomWalkMarket().generate(_spec())
    c = RandomWalkMarket().generate(_spec(seed=8))
    assert a.market_hash == b.market_hash != c.market_hash
    assert a.truth == ()


def test_bars_are_lawful_minutes() -> None:
    market = RandomWalkMarket().generate(_spec())
    for bar in market.bars:
        assert bar.low <= min(bar.open, bar.close) <= max(bar.open, bar.close) <= bar.high
    assert market.bars[1].open == market.bars[0].close


def test_a_planted_autocorrelation_is_recorded_and_visible() -> None:
    effect = PlantedEffect(lag_minutes=1, strength=Decimal("0.6"))
    planted = RandomWalkMarket().generate(_spec(minutes=3000, effects=(effect,)))
    noise = RandomWalkMarket().generate(_spec(minutes=3000))
    assert planted.truth == (effect,)
    assert _autocorrelation(_returns(planted.bars), 1) > 0.4
    assert abs(_autocorrelation(_returns(noise.bars), 1)) < 0.1


def test_a_non_stationary_effect_is_refused() -> None:
    with pytest.raises(ValidationError, match="strength"):
        PlantedEffect(lag_minutes=1, strength=Decimal("1"))


# ==========================================================================================
# ADR-0088 决策 4：GARCH(1,1) 波动率聚集与 Poisson 跳跃
#
# ``_reference_market`` 独立复刻 ``plugins/synthetic/random_walk.py`` 记录的算法与 RNG 抽取
# 顺序（不 import 生成器的私有函数），作为契约层之外的"手算"交叉验证：两套独立实现在同一
# 种子下逐位一致，才说明公式与抽取顺序都对。
# ==========================================================================================

_RETURN_SCALE = Decimal("1e-12")
_PRICE_SCALE = Decimal("1e-8")
_VARIANCE_SCALE = Decimal("1e-18")
_REF_CONTEXT = Context(prec=50, rounding=ROUND_HALF_EVEN)


def _q(value: Decimal, scale: Decimal) -> Decimal:
    return value.quantize(scale, rounding=ROUND_HALF_EVEN)


def _reference_market(
    *,
    seed: int,
    minutes: int,
    initial_price: Decimal,
    volatility: Decimal,
    drift: Decimal = Decimal(0),
    autocorrelation: tuple[tuple[int, Decimal], ...] = (),
    garch: tuple[Decimal, Decimal, Decimal] | None = None,
    jumps: tuple[tuple[Decimal, Decimal], ...] = (),
) -> tuple[list[Decimal], list[Decimal], int]:
    """Returns ``(opens, closes, jump_hit_count)``. ``autocorrelation`` is ``(lag, strength)``
    pairs; ``garch`` is ``(omega, alpha, beta)``; ``jumps`` is ``(intensity, jump_scale)`` pairs.
    """
    rng = random.Random(seed)
    returns: list[Decimal] = []
    opens: list[Decimal] = []
    closes: list[Decimal] = []
    jump_hits = 0
    close = initial_price
    previous_shock = Decimal(0)
    previous_variance = Decimal(0)
    if garch is not None:
        omega, alpha, beta = garch
        with localcontext(_REF_CONTEXT):
            previous_variance = _q(omega / (Decimal(1) - alpha - beta), _VARIANCE_SCALE)
    for minute in range(minutes):
        z = Decimal(repr(rng.gauss(0.0, 1.0)))
        if garch is not None:
            omega, alpha, beta = garch
            if minute == 0:
                variance = previous_variance
            else:
                with localcontext(_REF_CONTEXT):
                    variance = _q(
                        omega + alpha * previous_shock * previous_shock + beta * previous_variance,
                        _VARIANCE_SCALE,
                    )
            with localcontext(_REF_CONTEXT):
                sigma = _q(variance.sqrt(), _RETURN_SCALE)
            previous_variance = variance
        else:
            sigma = volatility
        noise = _q(z * sigma, _RETURN_SCALE)
        previous_shock = noise
        jump_total = Decimal(0)
        for intensity, jump_scale in jumps:
            if rng.random() < float(intensity):
                magnitude = abs(Decimal(repr(rng.gauss(0.0, 1.0)))) * jump_scale
                sign = Decimal(1) if rng.random() < 0.5 else Decimal(-1)
                jump_total += _q(sign * magnitude, _RETURN_SCALE)
                jump_hits += 1
        carried = Decimal(0)
        for lag, strength in autocorrelation:
            if minute - lag >= 0:
                carried += strength * returns[minute - lag]
        r = _q(drift + noise + carried + jump_total, _RETURN_SCALE)
        returns.append(r)
        opened = close
        close = _q(max(opened * (1 + r), _PRICE_SCALE), _PRICE_SCALE)
        opens.append(opened)
        closes.append(close)
        # spread / volume / trade_count: same draws, unchecked here, kept only to stay aligned.
        rng.random()
        rng.uniform(1.0, 100.0)
        rng.randint(1, 500)
    return opens, closes, jump_hits


def test_garch_and_jump_effects_are_deterministic_given_the_seed() -> None:
    garch = VolatilityClusteringEffect(
        omega=Decimal("0.000001"), alpha=Decimal("0.1"), beta=Decimal("0.85")
    )
    jump = JumpEffect(intensity_per_minute=Decimal("0.05"), jump_scale=Decimal("0.02"))
    a = RandomWalkMarket().generate(_spec(minutes=100, effects=(garch, jump)))
    b = RandomWalkMarket().generate(_spec(minutes=100, effects=(garch, jump)))
    c = RandomWalkMarket().generate(_spec(minutes=100, seed=8, effects=(garch, jump)))
    assert a.market_hash == b.market_hash != c.market_hash
    assert a.truth == (garch, jump)


def test_a_garch_effect_matches_a_hand_rolled_variance_recursion() -> None:
    garch = VolatilityClusteringEffect(
        omega=Decimal("0.000001"), alpha=Decimal("0.1"), beta=Decimal("0.85")
    )
    minutes = 5
    market = RandomWalkMarket().generate(_spec(minutes=minutes, effects=(garch,)))
    opens, closes, jump_hits = _reference_market(
        seed=7,
        minutes=minutes,
        initial_price=Decimal("100"),
        volatility=Decimal("0.001"),
        garch=(Decimal("0.000001"), Decimal("0.1"), Decimal("0.85")),
    )
    assert jump_hits == 0
    assert [bar.open for bar in market.bars] == opens
    assert [bar.close for bar in market.bars] == closes
    assert market.truth == (garch,)


def test_two_garch_effects_are_refused_as_ambiguous() -> None:
    garch = VolatilityClusteringEffect(
        omega=Decimal("0.000001"), alpha=Decimal("0.1"), beta=Decimal("0.85")
    )
    with pytest.raises(ValueError, match="最多植入一个"):
        RandomWalkMarket().generate(_spec(minutes=5, effects=(garch, garch)))


def test_the_jump_frequency_is_exact_for_a_fixed_seed() -> None:
    jump = JumpEffect(intensity_per_minute=Decimal("0.05"), jump_scale=Decimal("0.02"))
    minutes = 2000
    market = RandomWalkMarket().generate(_spec(minutes=minutes, effects=(jump,)))
    opens, closes, jump_hits = _reference_market(
        seed=7,
        minutes=minutes,
        initial_price=Decimal("100"),
        volatility=Decimal("0.001"),
        jumps=((Decimal("0.05"), Decimal("0.02")),),
    )
    assert jump_hits == 95  # exact count at seed=7 (independently hand-derived, not tuned)
    assert [bar.open for bar in market.bars] == opens
    assert [bar.close for bar in market.bars] == closes
    assert market.truth == (jump,)


def test_composed_autocorrelation_garch_and_jump_all_land_in_truth() -> None:
    autocorrelation = PlantedEffect(lag_minutes=1, strength=Decimal("0.3"))
    garch = VolatilityClusteringEffect(
        omega=Decimal("0.000001"), alpha=Decimal("0.1"), beta=Decimal("0.85")
    )
    jump = JumpEffect(intensity_per_minute=Decimal("0.05"), jump_scale=Decimal("0.02"))
    minutes = 50
    market = RandomWalkMarket().generate(
        _spec(minutes=minutes, effects=(autocorrelation, garch, jump))
    )
    opens, closes, jump_hits = _reference_market(
        seed=7,
        minutes=minutes,
        initial_price=Decimal("100"),
        volatility=Decimal("0.001"),
        autocorrelation=((1, Decimal("0.3")),),
        garch=(Decimal("0.000001"), Decimal("0.1"), Decimal("0.85")),
        jumps=((Decimal("0.05"), Decimal("0.02")),),
    )
    assert jump_hits == 2
    assert [bar.open for bar in market.bars] == opens
    assert [bar.close for bar in market.bars] == closes
    assert market.truth == (autocorrelation, garch, jump)


def test_a_zero_strength_autocorrelation_still_drops_out_of_truth_when_composed() -> None:
    inert = PlantedEffect(lag_minutes=1, strength=Decimal("0"))
    jump = JumpEffect(intensity_per_minute=Decimal("0.05"), jump_scale=Decimal("0.02"))
    market = RandomWalkMarket().generate(_spec(minutes=20, effects=(inert, jump)))
    assert market.truth == (jump,)


def test_a_pre_adr_0088_spec_still_generates_byte_identical_bars() -> None:
    """No GARCH / jump effect: the RNG draw order and formulas are exactly the pre-ADR-0088 ones
    (z, then spread / volume / trade_count — no extra draws), so a 2.0.0 ~ 2.3.0-shaped spec
    (autocorrelation only, or none) must reproduce the values that formula always gave."""
    market = RandomWalkMarket().generate(_spec(minutes=3))
    assert [bar.close for bar in market.bars] == [
        Decimal("99.97441197"),
        Decimal("100.02554203"),
        Decimal("99.99791855"),
    ]
    assert [bar.high for bar in market.bars] == [
        Decimal("100.03254672"),
        Decimal("100.03024973"),
        Decimal("100.04722985"),
    ]
    assert [bar.low for bar in market.bars] == [
        Decimal("99.94187357"),
        Decimal("99.96970667"),
        Decimal("99.97623672"),
    ]
    assert [bar.volume for bar in market.bars] == [
        Decimal("8.17119238"),
        Decimal("58.69601258"),
        Decimal("7.91568693"),
    ]
    assert [bar.trade_count for bar in market.bars] == [275, 466, 47]

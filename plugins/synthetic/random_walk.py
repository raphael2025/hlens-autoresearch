"""A seeded random-walk market with optional planted effects (Phase 9; ADR-0088 decision 4).

Per minute ``t`` the base diffusive shock is ``z_t ~ N(0, 1)`` drawn from ``random.Random(seed)``.
Three effect kinds may be planted (``SyntheticMarketSpec.effects``), all optional and composable:

* ``PlantedEffect`` (``return_autocorrelation``): adds
  ``sum(strength_k * r_{t - lag_k})`` to the return, for every planted lag whose effect starts
  before ``t``. A zero ``strength`` is a no-op and is dropped from ``truth``.
* ``VolatilityClusteringEffect`` (``volatility_clustering``): replaces the constant per-minute
  return scale with a GARCH(1,1) process, ``sigma2_t = omega + alpha * eps_{t-1}^2 + beta *
  sigma2_{t-1}``, seeded at the unconditional variance ``omega / (1 - alpha - beta)`` for the
  first minute (``eps_t`` is exactly this minute's *diffusive* shock ``z_t * sigma_t`` — not the
  full return, which may also carry drift, autocorrelation and jumps). At most one may be planted
  (a second GARCH process would leave ``sigma_t`` ambiguous).
* ``JumpEffect`` (``jump``): each minute, independently, a jump fires with probability
  ``intensity_per_minute``; its size is ``jump_scale * abs(z)`` for a fresh standard normal draw
  ``z``, with an independently drawn random sign. Several may be planted; their contributions add.

``close_t = close_{t-1} * (1 + r_t)`` with ``r_t = drift + noise_t + carried + jump_t``. Every
value is quantized to a fixed ``Decimal`` scale right after it is drawn or derived, so the output
is a pure function of the spec (the same seed gives the same ``market_hash``).

**RNG draw order per minute** (the contract that makes replay and hand-calculation exact):
``z`` (the base shock) is drawn first, unconditionally; then, in ``spec.effects`` order, each
``JumpEffect`` draws its intensity coin flip and, only on a hit, a magnitude draw and a sign coin;
then the unchanged tail — spread, volume, trade count. A spec with **no** jump effects therefore
draws in exactly the pre-ADR-0088 order, and old (2.0.0 ~ 2.3.0) specs reproduce byte-identical
markets (``PlantedEffect`` is unaffected either way — it costs no extra draws).

``truth`` lists exactly the effects actually planted: non-zero-``strength`` autocorrelation, and
every GARCH / jump effect present (their contract fields — ``omega > 0`` and
``intensity_per_minute > 0`` — make "planted but inert" impossible). With none, the market is
pure noise (the null a validation pipeline must not reject too often).
"""

from __future__ import annotations

import random
from datetime import timedelta
from decimal import ROUND_HALF_EVEN, Context, Decimal, localcontext
from typing import Final

from core.contracts.synthetic import (
    JumpEffect,
    PlantedEffect,
    SyntheticBar,
    SyntheticEffect,
    SyntheticMarket,
    SyntheticMarketSpec,
    SyntheticProviderDescriptor,
    VolatilityClusteringEffect,
    market_hash,
)

__all__ = ["RandomWalkMarket"]

_RETURN_SCALE: Final = Decimal("1e-12")
_PRICE_SCALE: Final = Decimal("1e-8")
_VARIANCE_SCALE: Final = Decimal("1e-18")
_MINUTE: Final = timedelta(minutes=1)

#: Headroom precision for GARCH division / multiplication / sqrt (module convention, see
#: ``plugins/features/bars.py:_LOG_CONTEXT``); results are quantized right back to a fixed
#: ``Decimal`` scale, so the active context's ambient precision never leaks into the output.
_GARCH_CONTEXT: Final = Context(prec=50, rounding=ROUND_HALF_EVEN)


def _q(value: Decimal, scale: Decimal) -> Decimal:
    return value.quantize(scale, rounding=ROUND_HALF_EVEN)


def _is_active(effect: SyntheticEffect) -> bool:
    """``strength == 0`` autocorrelation is a no-op (excluded from ``truth``); GARCH and jump are
    always active once planted — their contract fields (``omega > 0``,
    ``intensity_per_minute > 0``) rule out an inert instance."""
    if isinstance(effect, PlantedEffect):
        return effect.strength != 0
    return True


def _unconditional_variance(effect: VolatilityClusteringEffect) -> Decimal:
    """Sigma^2 at minute 0: the GARCH(1,1) unconditional variance ``omega / (1 - alpha - beta)``
    (stationary because the contract requires ``alpha + beta < 1``)."""
    with localcontext(_GARCH_CONTEXT):
        variance = effect.omega / (Decimal(1) - effect.alpha - effect.beta)
    return _q(variance, _VARIANCE_SCALE)


def _garch_variance(
    effect: VolatilityClusteringEffect, previous_shock: Decimal, previous_variance: Decimal
) -> Decimal:
    """``sigma2_t = omega + alpha * eps_{t-1}^2 + beta * sigma2_{t-1}``."""
    with localcontext(_GARCH_CONTEXT):
        variance = (
            effect.omega
            + effect.alpha * previous_shock * previous_shock
            + effect.beta * previous_variance
        )
    return _q(variance, _VARIANCE_SCALE)


def _sqrt(value: Decimal) -> Decimal:
    with localcontext(_GARCH_CONTEXT):
        root = value.sqrt()
    return _q(root, _RETURN_SCALE)


def _jump_draw(rng: random.Random, effect: JumpEffect) -> Decimal:
    """One minute's jump decision for a single ``JumpEffect``: a coin at ``intensity_per_minute``
    and, only on a hit, an independent magnitude (``jump_scale * abs(z)``) and sign draw."""
    if rng.random() >= float(effect.intensity_per_minute):
        return Decimal(0)
    magnitude = abs(Decimal(repr(rng.gauss(0.0, 1.0)))) * effect.jump_scale
    sign = Decimal(1) if rng.random() < 0.5 else Decimal(-1)
    return _q(sign * magnitude, _RETURN_SCALE)


class RandomWalkMarket:
    """Deterministic given the spec's seed."""

    def __init__(self) -> None:
        self._descriptor = SyntheticProviderDescriptor(
            name="hlens_synthetic_random_walk", version="1.0.0", deterministic=True
        )

    @property
    def descriptor(self) -> SyntheticProviderDescriptor:
        return self._descriptor

    def generate(self, spec: SyntheticMarketSpec) -> SyntheticMarket:
        rng = random.Random(spec.seed)
        autocorrelation = tuple(
            effect
            for effect in spec.effects
            if isinstance(effect, PlantedEffect) and _is_active(effect)
        )
        garch_effects = tuple(
            effect for effect in spec.effects if isinstance(effect, VolatilityClusteringEffect)
        )
        if len(garch_effects) > 1:
            raise ValueError(
                "最多植入一个 VolatilityClusteringEffect：两个 GARCH 过程会让 sigma_t 有歧义"
            )
        garch = garch_effects[0] if garch_effects else None
        jump_effects = tuple(effect for effect in spec.effects if isinstance(effect, JumpEffect))
        truth = tuple(effect for effect in spec.effects if _is_active(effect))

        returns: list[Decimal] = []
        bars: list[SyntheticBar] = []
        close = spec.initial_price
        previous_shock = Decimal(0)
        previous_variance = _unconditional_variance(garch) if garch is not None else Decimal(0)
        for minute in range(spec.minutes):
            z = Decimal(repr(rng.gauss(0.0, 1.0)))
            if garch is not None:
                variance = (
                    previous_variance
                    if minute == 0
                    else _garch_variance(garch, previous_shock, previous_variance)
                )
                sigma = _sqrt(variance)
                previous_variance = variance
            else:
                sigma = spec.volatility
            noise = _q(z * sigma, _RETURN_SCALE)
            previous_shock = noise
            jump = sum((_jump_draw(rng, effect) for effect in jump_effects), Decimal(0))
            carried = sum(
                (
                    effect.strength * returns[minute - effect.lag_minutes]
                    for effect in autocorrelation
                    if minute - effect.lag_minutes >= 0
                ),
                Decimal(0),
            )
            r = _q(spec.drift + noise + carried + jump, _RETURN_SCALE)
            returns.append(r)
            opened = close
            close = _q(max(opened * (1 + r), _PRICE_SCALE), _PRICE_SCALE)
            spread = _q(Decimal(repr(rng.random())) * spec.volatility / 2, _RETURN_SCALE)
            high = _q(max(opened, close) * (1 + spread), _PRICE_SCALE)
            low = _q(min(opened, close) * (1 - spread), _PRICE_SCALE)
            volume = _q(Decimal(repr(rng.uniform(1.0, 100.0))), _PRICE_SCALE)
            start = spec.start + minute * _MINUTE
            bars.append(
                SyntheticBar(
                    interval_start=start,
                    interval_end=start + _MINUTE,
                    open=opened,
                    high=high,
                    low=low,
                    close=close,
                    volume=volume,
                    trade_count=rng.randint(1, 500),
                )
            )
        spec_hash = spec.content_hash()
        provider = self._descriptor.plugin_key
        frozen = tuple(bars)
        return SyntheticMarket(
            spec_hash=spec_hash,
            provider=provider,
            bars=frozen,
            truth=truth,
            market_hash=market_hash(spec_hash, provider, frozen, truth),
        )

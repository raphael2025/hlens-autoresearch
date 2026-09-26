"""Phase 9 medium-scale gate calibration EVIDENCE setups (evidence only — not a Profile decision).

!!! TEST ONLY Profiles !!!  Both setups compare only the deliberately extreme, **uncalibrated**
TEST ONLY candidates of ``gate_fixtures`` (``test_only_lax_uncalibrated`` /
``test_only_strict_uncalibrated``). The measured rates describe how those two fixtures behave on
synthetic markets; they are not a calibration result, choose no threshold and no Profile value
(D-09 TBD-1..5 stay unfrozen; ADR-0042 / ADR-0007).

This module lives next to the fixtures (not in ``research/``) so that no research-plane module
imports TEST ONLY Profiles. Every input is explicit here — seed ranges, planted strengths, the
candidate tuple, ``alpha`` — and the factories take no defaults from the harness. The committed
reports under ``docs/research/calibration/`` are regenerated through the existing CLI entry point
``gate_calibration.main`` (one at a time, memory-capped; exact commands in that directory's
README): ``main(["--setup",
"tests.research.synthetic_lab.evidence_setups:single_instrument_evidence", "--out", DIR])`` and
the same with ``multi_instrument_evidence``.

(``python -m research.synthetic_lab.gate_calibration`` works too since B52: its ``__main__``
block runs the package module's ``main``; before, ``-m`` re-executed the module as ``__main__``
with distinct setup classes and refused every setup.)

The ``*_probe`` factories are the 4-seed timing probes used to choose the seed counts.
"""

from __future__ import annotations

from core.contracts.synthetic import PlantedEffect
from core.contracts.validation_profile import ValidationProfile
from plugins.synthetic import RandomWalkMarket
from research.synthetic_lab.gate_calibration import (
    GateCalibrationSetup,
    MultiInstrumentArm,
    MultiInstrumentCalibrationSetup,
)
from tests.research.synthetic_lab.gate_fixtures import (
    BASE_SPEC,
    G5_BASE_SPEC,
    LAX_TEST_ONLY_PROFILE,
    MULTI_SYMBOLS,
    STRICT_TEST_ONLY_PROFILE,
    STRONG,
    TEST_ONLY_ALPHA,
    WEAK,
    detector,
    multi_detector,
    sealed_inputs,
)

#: TEST ONLY candidates (both uncalibrated extremes; the harness never ranks or recommends).
TEST_ONLY_CANDIDATES: tuple[ValidationProfile, ...] = (
    LAX_TEST_ONLY_PROFILE,
    STRICT_TEST_ONLY_PROFILE,
)
#: The two planted strengths of setup (a): ``lag_minutes=60``, strength 0.5 and 0.2.
PLANTED_STRENGTHS: tuple[PlantedEffect, ...] = (STRONG, WEAK)

#: Seed-range starts, disjoint per arm and from the smoke tests' ranges (0.., 100.., 200..).
SINGLE_NOISE_SEED_START = 10_000
SINGLE_PLANTED_SEED_START = 20_000
MULTI_ALL_NOISE_SEED_START = 30_000
MULTI_ALL_PLANTED_SEED_START = 40_000
MULTI_MIXED_SEED_START = 50_000

#: Seeds per arm of the committed evidence runs, chosen from the 4-seed timing probes so that each
#: run stays within about 20 minutes under a 6 GB memory cap (docs/research/calibration/).
SINGLE_SEEDS = 250
MULTI_SEEDS = 200
PROBE_SEEDS = 4


def _seeds(start: int, count: int) -> tuple[int, ...]:
    if count < 1:
        raise ValueError("count must be >= 1")
    return tuple(range(start, start + count))


def single_instrument_setup(seeds: int) -> GateCalibrationSetup:
    """Setup (a): one instrument, G5 on, a noise arm + two planted strengths, lax + strict."""
    return GateCalibrationSetup(
        provider=RandomWalkMarket(),
        base=G5_BASE_SPEC,
        detector=detector(sealed_inputs_for=sealed_inputs),
        candidates=TEST_ONLY_CANDIDATES,
        noise_seeds=_seeds(SINGLE_NOISE_SEED_START, seeds),
        planted=PLANTED_STRENGTHS,
        planted_seeds=_seeds(SINGLE_PLANTED_SEED_START, seeds),
        alpha=TEST_ONLY_ALPHA,
        sealed_oos_g5=True,
    )


def multi_instrument_arms(seeds: int) -> tuple[MultiInstrumentArm, ...]:
    """Setup (b) arms over ``MULTI_SYMBOLS`` (k = 2): all noise, all planted, mixed (S0 planted)."""
    return (
        MultiInstrumentArm(
            "all_noise", "all_noise", (None, None), _seeds(MULTI_ALL_NOISE_SEED_START, seeds)
        ),
        MultiInstrumentArm(
            "all_planted",
            "all_planted",
            (STRONG, STRONG),
            _seeds(MULTI_ALL_PLANTED_SEED_START, seeds),
        ),
        MultiInstrumentArm("mixed", "mixed", (STRONG, None), _seeds(MULTI_MIXED_SEED_START, seeds)),
    )


def multi_instrument_setup(seeds: int) -> MultiInstrumentCalibrationSetup:
    """Setup (b): k = 2 instruments, three arms, lax + strict (no G5 in this mode)."""
    return MultiInstrumentCalibrationSetup(
        provider=RandomWalkMarket(),
        base=BASE_SPEC,
        detector=multi_detector(),
        candidates=TEST_ONLY_CANDIDATES,
        symbols=MULTI_SYMBOLS,
        arms=multi_instrument_arms(seeds),
        alpha=TEST_ONLY_ALPHA,
    )


def single_instrument_evidence() -> GateCalibrationSetup:
    """CLI factory of the committed single-instrument evidence report."""
    return single_instrument_setup(SINGLE_SEEDS)


def multi_instrument_evidence() -> MultiInstrumentCalibrationSetup:
    """CLI factory of the committed multi-instrument evidence report."""
    return multi_instrument_setup(MULTI_SEEDS)


def single_instrument_probe() -> GateCalibrationSetup:
    """4-seed timing probe of setup (a)."""
    return single_instrument_setup(PROBE_SEEDS)


def multi_instrument_probe() -> MultiInstrumentCalibrationSetup:
    """4-seed timing probe of setup (b)."""
    return multi_instrument_setup(PROBE_SEEDS)

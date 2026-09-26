"""Calibration harness for the Phase 4 / Phase 8 validation gates on synthetic truth (Phase 9).

ADR-0042 (Implementation note, gate calibration harness). Status: FRAMEWORK_IMPLEMENTED /
NOT_VALIDATED.

**Evidence only — not a Profile decision.** The Validation Profile numbers (D-09 TBD-1..5) are
frozen by Raphael after calibration (two-step freeze, ADR-0007 / ADR-0037). This harness produces
the evidence for that decision: for every *candidate* Profile the caller supplies, how often the
full validation pipeline passes pure noise (false-positive rate), how often it passes markets with
a planted effect (power per strength / lag), how often it is ``INCONCLUSIVE`` and how often a pass
would spend the one-shot sealed OOS. It never chooses, ranks or proposes a Profile, has no default
Profile, and writes no number into any Profile. Synthetic results never support a real-market
conclusion (roadmap P9).

Pieces:

- ``GateDetector``: ``(market, candidate Profile) -> ValidationReport``. The intended detector is
  ``StrategyValidatorDetector`` — the full G0 → G4 pipeline run through the strategy validator
  (``research.strategies.validation.PipelineBacktestValidator``) on the research window of each
  market. Its trial runs are cached per market (``CachingTrialRunner``), so extra candidate
  Profiles only re-run the gates, not the backtests.
- ``run_gate_calibration``: every seed of every arm (``noise`` plus one arm per planted effect)
  is generated once and validated under every candidate Profile, in a fixed order.
- Rates are exact counts with a Clopper-Pearson interval (``intervals.py``, rational arithmetic,
  ``Decimal`` output). Per gate, a run where the pipeline stopped before the gate counts as
  ``not_evaluated`` (and not as a pass).
- Sealed OOS consumption: G5 is a one-shot, budgeted event (``SealedOosVault``) that only a
  G0 – G4 ``PASS`` may reach. Each passing run unseals its own family in a fresh in-memory vault
  per candidate; the vault's capacity is the number of runs (a bookkeeping bound, not a budget
  choice). G5 itself is not run here.
- Detector failures: an exception raised by ``detect`` is a failure of the method under
  calibration on that market, not of the harness. The run is recorded as ``INCONCLUSIVE`` with no
  gates (every gate ``not_evaluated``) and the exception type / message in ``detector_error``;
  each arm reports its ``detector_errors`` count. It is never a pass. Misconfiguration of the
  harness itself (``DetectorConfigurationError``, a report under another Profile, a market whose
  truth is not the planted effects) still raises: those are caller bugs, not evidence.
- ``GateCalibrationReport``: deterministic, JSON-ready, content-hashed (``report_hash``); it
  records every input (generator, detector, base spec, seeds, planted effects, candidate Profile
  refs and hashes, interval method and ``alpha``).
- ``main`` / ``write_gate_calibration``: write the report through ``research/reports``.
"""

from __future__ import annotations

import argparse
import importlib
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from typing import Final, Protocol

from core.contracts.feature import ObservationScalar
from core.contracts.strategy import BacktestProvider
from core.contracts.synthetic import (
    PlantedEffect,
    SyntheticMarket,
    SyntheticMarketProvider,
    SyntheticMarketSpec,
)
from core.contracts.validation_profile import ValidationProfile
from core.domain.base import content_hash
from core.domain.research import ValidationReport, Verdict
from research.reports.envelope import WrittenReport
from research.reports.gate_calibration import write_gate_calibration_report
from research.strategies.pipeline import CandidateTrialRunner, EvaluationInputs, StrategyCandidate
from research.strategies.validation import (
    PipelineBacktestValidator,
    TrialRun,
    TrialRunner,
    ValidatorSetup,
)
from research.synthetic_lab.intervals import INTERVAL_METHOD, BinomialRate, binomial_rate
from research.validation.sealed_oos import InMemoryUnsealingLedger, SealedOosVault

__all__ = [
    "DISCLAIMER",
    "NOISE_ARM",
    "ArmEvidence",
    "CachingTrialRunner",
    "CandidateEvidence",
    "DetectorConfigurationError",
    "GateCalibrationReport",
    "GateCalibrationSetup",
    "GateDetector",
    "GateEvidence",
    "RunRecord",
    "StrategyValidatorDetector",
    "main",
    "planted_arm_id",
    "run_gate_calibration",
    "write_gate_calibration",
]

DISCLAIMER: Final = "evidence only — not a Profile decision"
STATUS: Final = "FRAMEWORK_IMPLEMENTED / NOT_VALIDATED"
SCHEMA_VERSION: Final = "1.0.0"
NOTE: Final = (
    "Candidate Profiles are supplied by the caller and are compared, not ranked. The Validation "
    "Profile numbers (D-09 TBD-1..5) are frozen by Raphael (ADR-0007 two-step freeze); a frozen "
    "Profile may cite this report's hash in provenance.calibration_report. Synthetic results never "
    "support a real-market conclusion (roadmap P9)."
)
NOISE_ARM: Final = "noise"
_UNSEALED_BY: Final = "gate_calibration_harness(simulated)"
#: Upper bound on the recorded exception message (the report must stay small and deterministic).
_ERROR_MESSAGE_LIMIT: Final = 200


class DetectorConfigurationError(ValueError):
    """The detector was wired wrongly by the caller; a harness bug, never counted as evidence."""


# ======================================================================================
# Detector
# ======================================================================================


class GateDetector(Protocol):
    """The method under calibration: validate one market under one candidate Profile."""

    @property
    def name(self) -> str: ...

    def describe(self) -> Mapping[str, str]:
        """What identifies the detector (recorded as a report input)."""
        ...

    def detect(self, market: SyntheticMarket, profile: ValidationProfile) -> ValidationReport: ...


_TrialKey = tuple[tuple[tuple[str, str], ...], int, timedelta, tuple[str, ...] | None]


class CachingTrialRunner:
    """A ``TrialRunner`` that memoizes its inner runner (trial runs do not depend on the Profile).

    Only the cached arguments are part of the key, and the inner runner is a pure function of
    them for one market, so a cache hit is the same ``TrialRun`` a fresh run would produce.
    """

    def __init__(self, inner: TrialRunner) -> None:
        self._inner = inner
        self._runs: dict[_TrialKey, TrialRun] = {}

    def run(
        self,
        params: Mapping[str, ObservationScalar],
        *,
        delay_bars: int = 0,
        decision_offset: timedelta = timedelta(0),
        instruments: tuple[str, ...] | None = None,
    ) -> TrialRun:
        point = tuple(sorted((name, repr(value)) for name, value in params.items()))
        key = (point, delay_bars, decision_offset, instruments)
        if key not in self._runs:
            self._runs[key] = self._inner.run(
                params,
                delay_bars=delay_bars,
                decision_offset=decision_offset,
                instruments=instruments,
            )
        return self._runs[key]


#: ``(market, candidate Profile, cached trial runner) -> ValidatorSetup``; the setup must use the
#: given runner and bind the given Profile.
SetupFactory = Callable[[SyntheticMarket, ValidationProfile, TrialRunner], ValidatorSetup]


class StrategyValidatorDetector:
    """The full G0 → G4 pipeline through ``PipelineBacktestValidator`` (see module docs).

    ``inputs_for`` must return the **research window** of the market only (the validator fails
    ``G1.sealed_oos_excluded`` otherwise). The trial runner of the last market is kept, so the
    harness's market-outer / Profile-inner order reuses every backtest across candidates.
    """

    def __init__(
        self,
        *,
        name: str,
        candidate: StrategyCandidate,
        backtester: BacktestProvider,
        inputs_for: Callable[[SyntheticMarket], EvaluationInputs],
        setup_for: SetupFactory,
    ) -> None:
        self._name = name
        self._candidate = candidate
        self._backtester = backtester
        self._inputs_for = inputs_for
        self._setup_for = setup_for
        self._cached: tuple[str, CachingTrialRunner] | None = None

    @property
    def name(self) -> str:
        return self._name

    def describe(self) -> Mapping[str, str]:
        spec = self._candidate.spec
        return {
            "adapter": "research.strategies.validation.PipelineBacktestValidator",
            "strategy": str(spec.ref),
            "strategy_hash": spec.content_hash(),
            "hypothesis_family_id": self._candidate.hypothesis_family_id,
        }

    def _runner(self, market: SyntheticMarket) -> CachingTrialRunner:
        if self._cached is None or self._cached[0] != market.market_hash:
            inner = CandidateTrialRunner(
                self._candidate, self._inputs_for(market), self._backtester
            )
            self._cached = (market.market_hash, CachingTrialRunner(inner))
        return self._cached[1]

    def detect(self, market: SyntheticMarket, profile: ValidationProfile) -> ValidationReport:
        runner = self._runner(market)
        setup = self._setup_for(market, profile, runner)
        if setup.trials is not runner:
            raise DetectorConfigurationError("setup_for must use the trial runner it is given")
        if setup.context.profile.content_hash() != profile.content_hash():
            raise DetectorConfigurationError(
                "setup_for must bind the candidate Profile it is given"
            )
        spec = self._candidate.spec
        # The point the validator re-runs (G0 reproducibility compares the two result hashes):
        # spec defaults overridden by the chosen point, declared keys only.
        point = {**dict(spec.params), **dict(setup.chosen_params)}
        request = {k: v for k, v in point.items() if k in spec.param_search_space}
        backtest = runner.run(request).backtest  # type: ignore[arg-type]
        validation = PipelineBacktestValidator(setup).validate(spec.ref, spec, backtest)
        return validation.report


# ======================================================================================
# Harness inputs and records
# ======================================================================================


def planted_arm_id(effect: PlantedEffect) -> str:
    return f"planted_lag{effect.lag_minutes}_strength{effect.strength}"


@dataclass(frozen=True)
class GateCalibrationSetup:
    """Every input of one calibration run. Nothing has a default: absence must be explicit.

    - ``base``: the pure-noise market spec; each arm / seed only changes ``seed`` and ``effects``;
    - ``candidates``: the caller's candidate Profiles (compared, never chosen here);
    - ``planted``: one arm per effect (strength / lag), each run on every ``planted_seeds`` seed;
    - ``alpha``: the two-sided level of the reported Clopper-Pearson intervals (a reporting
      parameter, not a Profile number).
    """

    provider: SyntheticMarketProvider
    base: SyntheticMarketSpec
    detector: GateDetector
    candidates: tuple[ValidationProfile, ...]
    noise_seeds: tuple[int, ...]
    planted: tuple[PlantedEffect, ...]
    planted_seeds: tuple[int, ...]
    alpha: Decimal

    def __post_init__(self) -> None:
        if self.base.effects:
            raise ValueError("the base spec must be pure noise (no planted effects)")
        if not self.candidates:
            raise ValueError("at least one candidate Profile is required")
        hashes = [profile.content_hash() for profile in self.candidates]
        if len(set(hashes)) != len(hashes):
            raise ValueError("candidate Profiles must be distinct")
        if not self.noise_seeds:
            raise ValueError("at least one noise seed is required")
        if self.planted and not self.planted_seeds:
            raise ValueError("planted effects need planted_seeds")
        for seeds in (self.noise_seeds, self.planted_seeds):
            if len(set(seeds)) != len(seeds):
                raise ValueError("seeds must be distinct")
        arms = [planted_arm_id(effect) for effect in self.planted]
        if any(effect.strength == 0 for effect in self.planted):
            raise ValueError("a planted effect with strength 0 is noise")
        if len(set(arms)) != len(arms):
            raise ValueError("planted effects must be distinct")
        if not Decimal(0) < self.alpha < Decimal(1):
            raise ValueError("alpha must be in (0, 1)")

    def arms(self) -> tuple[tuple[str, tuple[PlantedEffect, ...], tuple[int, ...]], ...]:
        planted = tuple(
            (planted_arm_id(effect), (effect,), self.planted_seeds) for effect in self.planted
        )
        return ((NOISE_ARM, (), self.noise_seeds), *planted)

    def inputs_payload(self) -> dict[str, object]:
        return {
            "generator": self.provider.descriptor.plugin_key,
            "detector": {"name": self.detector.name, **dict(self.detector.describe())},
            "base_spec": self.base.model_dump(mode="json"),
            "base_spec_hash": self.base.content_hash(),
            "noise_seeds": list(self.noise_seeds),
            "planted_seeds": list(self.planted_seeds),
            "planted_effects": [
                {
                    "arm": planted_arm_id(effect),
                    "effect": effect.model_dump(mode="json"),
                    "effect_hash": effect.content_hash(),
                }
                for effect in self.planted
            ],
            "candidate_profiles": [
                {"profile": str(profile.ref), "profile_hash": profile.content_hash()}
                for profile in self.candidates
            ],
            "interval": {"method": INTERVAL_METHOD, "alpha": str(self.alpha)},
        }


@dataclass(frozen=True, slots=True)
class RunRecord:
    """One market validated under one candidate Profile."""

    arm: str
    seed: int
    market_spec_hash: str
    market_hash: str
    verdict: Verdict
    gates: tuple[tuple[str, Verdict], ...]
    gates_hash: str
    sealed_oos_unsealed: bool = False
    #: ``"<ExceptionType>: <message>"`` when ``detect`` raised (the run is then ``INCONCLUSIVE``).
    detector_error: str | None = None

    def __post_init__(self) -> None:
        if self.detector_error is not None and (
            self.verdict is not Verdict.INCONCLUSIVE or self.gates
        ):
            raise ValueError("a detector error is an INCONCLUSIVE run without gates")

    def to_payload(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "arm": self.arm,
            "seed": self.seed,
            "market_spec_hash": self.market_spec_hash,
            "market_hash": self.market_hash,
            "verdict": self.verdict.value,
            "gates_hash": self.gates_hash,
            "failing_gates": [g for g, v in self.gates if v is Verdict.FAIL],
            "inconclusive_gates": [g for g, v in self.gates if v is Verdict.INCONCLUSIVE],
            "sealed_oos_unsealed": self.sealed_oos_unsealed,
        }
        # Additive and only when present: reports without detector errors keep their hashes.
        if self.detector_error is not None:
            payload["detector_error"] = self.detector_error
        return payload


def _pass_key(arm: str) -> str:
    """Noise passes are false positives; planted passes are detections (power)."""
    return "false_positive_rate" if arm == NOISE_ARM else "power"


@dataclass(frozen=True, slots=True)
class ArmEvidence:
    """Pipeline-level rates of one arm under one candidate (``passed``: verdict ``PASS``)."""

    arm: str
    passed: BinomialRate
    inconclusive: BinomialRate
    failed: int
    sealed_oos_consumed: BinomialRate
    #: Runs whose detector raised (already counted in ``inconclusive``).
    detector_errors: int = 0

    def to_payload(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "runs": self.passed.n,
            _pass_key(self.arm): self.passed.to_payload(),
            "inconclusive_rate": self.inconclusive.to_payload(),
            "failed": self.failed,
            "sealed_oos_consumption_rate": self.sealed_oos_consumed.to_payload(),
        }
        if self.detector_errors:
            payload["detector_errors"] = self.detector_errors
        return payload


@dataclass(frozen=True, slots=True)
class GateEvidence:
    """One gate's rates in one arm; ``n`` is every run of the arm (not-evaluated is not a pass)."""

    gate_id: str
    arm: str
    passed: BinomialRate
    inconclusive: BinomialRate
    failed: int
    not_evaluated: int

    def to_payload(self) -> dict[str, object]:
        return {
            _pass_key(self.arm): self.passed.to_payload(),
            "inconclusive_rate": self.inconclusive.to_payload(),
            "failed": self.failed,
            "not_evaluated": self.not_evaluated,
        }


@dataclass(frozen=True)
class CandidateEvidence:
    profile: str
    profile_hash: str
    arms: tuple[ArmEvidence, ...]
    gates: tuple[GateEvidence, ...]
    runs: tuple[RunRecord, ...]
    sealed_oos_unsealings: int

    def arm(self, arm: str) -> ArmEvidence:
        return next(item for item in self.arms if item.arm == arm)

    @property
    def false_positive_rate(self) -> BinomialRate:
        return self.arm(NOISE_ARM).passed

    def power(self, effect: PlantedEffect) -> BinomialRate:
        return self.arm(planted_arm_id(effect)).passed

    def gate(self, gate_id: str, arm: str) -> GateEvidence:
        return next(g for g in self.gates if g.gate_id == gate_id and g.arm == arm)

    def gate_ids(self) -> tuple[str, ...]:
        return tuple(sorted({g.gate_id for g in self.gates}))

    def to_payload(self) -> dict[str, object]:
        gates: dict[str, dict[str, object]] = {}
        for gate in self.gates:
            gates.setdefault(gate.gate_id, {})[gate.arm] = gate.to_payload()
        return {
            "profile": self.profile,
            "profile_hash": self.profile_hash,
            "pipeline": {arm.arm: arm.to_payload() for arm in self.arms},
            "gates": gates,
            "sealed_oos_unsealings": self.sealed_oos_unsealings,
            "runs": [run.to_payload() for run in self.runs],
        }


@dataclass(frozen=True)
class GateCalibrationReport:
    """The evidence report (see module docs); ``report_hash`` covers the whole payload."""

    inputs: Mapping[str, object]
    candidates: tuple[CandidateEvidence, ...]
    report_hash: str = field(init=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "report_hash", content_hash(self._body()))

    def _body(self) -> dict[str, object]:
        return {
            "kind": "gate_calibration",
            "schema_version": SCHEMA_VERSION,
            "status": STATUS,
            "disclaimer": DISCLAIMER,
            "note": NOTE,
            "inputs": dict(self.inputs),
            "candidates": [candidate.to_payload() for candidate in self.candidates],
        }

    def to_payload(self) -> dict[str, object]:
        return {**self._body(), "report_hash": self.report_hash}

    def candidate(self, profile: ValidationProfile) -> CandidateEvidence:
        wanted = profile.content_hash()
        return next(item for item in self.candidates if item.profile_hash == wanted)


# ======================================================================================
# Harness
# ======================================================================================


def _record(arm: str, seed: int, market: SyntheticMarket, report: ValidationReport) -> RunRecord:
    return RunRecord(
        arm=arm,
        seed=seed,
        market_spec_hash=market.spec_hash,
        market_hash=market.market_hash,
        verdict=report.verdict,
        gates=tuple((gate.gate_id, gate.verdict) for gate in report.gates),
        gates_hash=content_hash([gate.content_hash() for gate in report.gates]),
    )


def _errored(arm: str, seed: int, market: SyntheticMarket, error: Exception) -> RunRecord:
    """A run whose detector raised: ``INCONCLUSIVE``, no gates, the error recorded."""
    message = " ".join(str(error).split())[:_ERROR_MESSAGE_LIMIT]
    return RunRecord(
        arm=arm,
        seed=seed,
        market_spec_hash=market.spec_hash,
        market_hash=market.market_hash,
        verdict=Verdict.INCONCLUSIVE,
        gates=(),
        gates_hash=content_hash([]),
        detector_error=f"{type(error).__name__}: {message}" if message else type(error).__name__,
    )


def _consume_sealed_oos(profile: ValidationProfile, runs: Sequence[RunRecord]) -> list[RunRecord]:
    """Each passing run spends its family's single unsealing (see module docs)."""
    vault = SealedOosVault(profile, InMemoryUnsealingLedger(), max_unsealings=len(runs))
    out: list[RunRecord] = []
    for run in runs:
        unsealed = run.verdict is Verdict.PASS
        if unsealed:
            family = f"gate_calibration:{run.arm}:{run.seed}"
            vault.unseal(family, approved_by=_UNSEALED_BY, at=vault.window.start)
        out.append(
            RunRecord(
                arm=run.arm,
                seed=run.seed,
                market_spec_hash=run.market_spec_hash,
                market_hash=run.market_hash,
                verdict=run.verdict,
                gates=run.gates,
                gates_hash=run.gates_hash,
                sealed_oos_unsealed=unsealed,
                detector_error=run.detector_error,
            )
        )
    return out


def _evidence(
    profile: ValidationProfile, runs: Sequence[RunRecord], arms: Sequence[str], alpha: Decimal
) -> CandidateEvidence:
    records = _consume_sealed_oos(profile, runs)
    arm_rows: list[ArmEvidence] = []
    gate_rows: list[GateEvidence] = []
    gate_ids = sorted({gate for run in records for gate, _ in run.gates})
    for arm in arms:
        mine = [run for run in records if run.arm == arm]
        n = len(mine)
        verdicts = [run.verdict for run in mine]
        arm_rows.append(
            ArmEvidence(
                arm=arm,
                passed=binomial_rate(verdicts.count(Verdict.PASS), n, alpha),
                inconclusive=binomial_rate(verdicts.count(Verdict.INCONCLUSIVE), n, alpha),
                failed=verdicts.count(Verdict.FAIL),
                sealed_oos_consumed=binomial_rate(
                    sum(run.sealed_oos_unsealed for run in mine), n, alpha
                ),
                detector_errors=sum(run.detector_error is not None for run in mine),
            )
        )
        for gate_id in gate_ids:
            seen = [dict(run.gates).get(gate_id) for run in mine]
            gate_rows.append(
                GateEvidence(
                    gate_id=gate_id,
                    arm=arm,
                    passed=binomial_rate(seen.count(Verdict.PASS), n, alpha),
                    inconclusive=binomial_rate(seen.count(Verdict.INCONCLUSIVE), n, alpha),
                    failed=seen.count(Verdict.FAIL),
                    not_evaluated=seen.count(None),
                )
            )
    return CandidateEvidence(
        profile=str(profile.ref),
        profile_hash=profile.content_hash(),
        arms=tuple(arm_rows),
        gates=tuple(gate_rows),
        runs=tuple(records),
        sealed_oos_unsealings=sum(run.sealed_oos_unsealed for run in records),
    )


def run_gate_calibration(setup: GateCalibrationSetup) -> GateCalibrationReport:
    """Validate every market of every arm under every candidate Profile (see module docs)."""
    runs: dict[str, list[RunRecord]] = {p.content_hash(): [] for p in setup.candidates}
    for arm, effects, seeds in setup.arms():
        for seed in seeds:
            spec = setup.base.model_copy(update={"seed": seed, "effects": effects})
            market = setup.provider.generate(spec)
            if market.truth != effects:
                raise ValueError(f"{arm}/{seed}: the market's truth is not the planted effects")
            for profile in setup.candidates:
                try:
                    report = setup.detector.detect(market, profile)
                except DetectorConfigurationError:
                    raise
                except Exception as error:  # the method failed on this market: evidence
                    runs[profile.content_hash()].append(_errored(arm, seed, market, error))
                    continue
                if report.validation_profile_hash != profile.content_hash():
                    raise ValueError(f"{setup.detector.name}: report is not under {profile.ref}")
                runs[profile.content_hash()].append(_record(arm, seed, market, report))
    arms = [arm for arm, _, _ in setup.arms()]
    return GateCalibrationReport(
        inputs=setup.inputs_payload(),
        candidates=tuple(
            _evidence(profile, runs[profile.content_hash()], arms, setup.alpha)
            for profile in setup.candidates
        ),
    )


# ======================================================================================
# Entry point
# ======================================================================================


def write_gate_calibration(root: Path, setup: GateCalibrationSetup) -> WrittenReport:
    """Run the harness and write the report under ``<root>/gate_calibration/<report_hash>.json``."""
    return write_gate_calibration_report(root, run_gate_calibration(setup))


def _load_setup(target: str) -> GateCalibrationSetup:
    module_name, _, attr = target.partition(":")
    if not module_name or not attr:
        raise SystemExit("--setup must be 'package.module:factory'")
    factory = getattr(importlib.import_module(module_name), attr)
    setup = factory()
    if not isinstance(setup, GateCalibrationSetup):
        raise SystemExit(f"{target} did not return a GateCalibrationSetup")
    return setup


def main(argv: Sequence[str] | None = None) -> int:
    """``python -m research.synthetic_lab.gate_calibration --setup mod:factory --out DIR``.

    The caller's factory supplies every input (candidate Profiles included); the CLI has none.
    """
    parser = argparse.ArgumentParser(description=f"Gate calibration harness ({DISCLAIMER}).")
    parser.add_argument("--setup", required=True, help="'package.module:factory' -> setup")
    parser.add_argument("--out", required=True, type=Path, help="report root directory")
    args = parser.parse_args(argv)
    written = write_gate_calibration(args.out, _load_setup(args.setup))
    print(f"{written.kind}/{written.id} -> {written.path} (written={written.written})")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())

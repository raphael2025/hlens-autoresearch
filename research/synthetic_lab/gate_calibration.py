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
  choice). By default G5 itself is not run.
- Optional G5 mode (``GateCalibrationSetup.sealed_oos_g5``, opt-in, off by default; with it off
  every report and ``report_hash`` is byte-identical to the harness without it). The detector
  must implement ``detect_sealed`` (``SealedGateDetector``); a detector without it is **refused**
  at setup time (``ValueError``), never silently reported. In G5 mode the generated market is
  split per candidate at the Profile's sealed window (``SealedWindow``): ``detect`` and
  ``detect_sealed`` only ever receive the research view (bars ending at or before the window
  start); the sealed-window bars are withheld in a ``SealedRelease`` and leave it only through
  ``release()`` — after the harness has unsealed the run's family and claimed its one-shot
  evaluation (``SealedOosVault.claim_evaluation``, mirroring the research loop's G5, R21 / R27).
  Only a G0 – G4 ``PASS`` is unsealed, so a non-passing run never has its sealed bars released.
  After the claim the evaluation is consumed whatever happens: a detector that exits early
  reports ``G5.oos_evaluation = consumed_without_result:<reason>``
  (``research.validation.sealed_oos_without_result``) and one that raises is recorded as an
  ``INCONCLUSIVE`` G5 without gates, ``consumed_without_result`` and ``detector_error`` — never a
  pass. Per arm the report adds the G5 pass / inconclusive / fail rates among the runs that
  reached G5 and the end-to-end (G0 – G5) pass rate over every run of the arm (false-positive
  rate on noise, power on planted arms). ``StrategyValidatorDetector.detect_sealed`` re-runs the
  candidate over research + released sealed bars (``sealed_inputs_for``), labels its non-flat
  sealed-window targets and runs ``research.validation.run_sealed_oos`` on the claimed
  evaluation, as ``research.loop.trials`` does. Each calibration run is its own simulated family
  (``gate_calibration:<arm>:<seed>``); the G5 context binds that family and its unsealing.
- Detector failures: an exception raised by ``detect`` is a failure of the method under
  calibration on that market, not of the harness. The run is recorded as ``INCONCLUSIVE`` with no
  gates (every gate ``not_evaluated``) and the exception type / message in ``detector_error``;
  each arm reports its ``detector_errors`` count. It is never a pass. Misconfiguration of the
  harness itself (``DetectorConfigurationError``, a report under another Profile, a market whose
  truth is not the planted effects) still raises: those are caller bugs, not evidence.
- Multi-instrument mode (Phase 9 implementation note, 2026-09-26; CODE_COMPLETE /
  DEBUG_PENDING; opt-in: a separate ``MultiInstrumentCalibrationSetup`` run by
  ``run_multi_instrument_calibration``, so every ``GateCalibrationSetup`` report and hash is
  untouched). It measures the Phase 8 multi-instrument path (``ValidatorSetup.instruments``,
  ``research.validation.instruments``), whose pooled G1 negative controls mix instruments by time
  and whose false-alarm rate on multi-instrument data is otherwise uncalibrated. Each run of each
  caller-declared ``MultiInstrumentArm`` (kind ``all_noise`` / ``all_planted`` / ``mixed``, one
  ``PlantedEffect | None`` per symbol, nothing defaulted; a kind that disagrees with the effects
  is refused) generates k >= 2 independent markets, one per distinct symbol, whose generator
  seeds are derived from the run seed (``instrument_seed``), and validates them together through
  ``MultiInstrumentGateDetector.detect_instruments`` (``MultiInstrumentValidatorDetector``: the
  full pipeline, whose setup must validate exactly the book's symbols; a report that took the
  single-instrument path is a ``DetectorConfigurationError``). The report adds, per arm, its
  ``kind``, the pipeline ``fail_rate`` and each instrument's own verdict rates (``instruments``:
  role, pass / inconclusive / fail rates, ``not_evaluated`` when a pooled stage failed first);
  per gate — pooled ``G1.shuffle_control`` / ``G1.shift_control`` and every
  ``<gate>.instrument.<symbol>`` sub-gate included — a ``fail_rate``, with the pass rate named
  by the arm's kind (``false_positive_rate`` / ``power`` / ``pass_rate`` for ``mixed``) or, for a
  per-instrument gate, by that instrument's role. Evidence only: no threshold, no Profile, no gate
  change. G5 mode is not offered in this mode.
- ``GateCalibrationReport``: deterministic, JSON-ready, content-hashed (``report_hash``); it
  records every input (generator, detector, base spec, seeds, planted effects, candidate Profile
  refs and hashes, interval method and ``alpha``).
- ``main`` / ``write_gate_calibration``: write the report through ``research/reports``.
"""

from __future__ import annotations

import argparse
import importlib
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from types import MappingProxyType
from typing import Final, Protocol, cast

from core.contracts.feature import ObservationScalar
from core.contracts.outcome import OutcomeEvent, OutcomePriceBar, OutcomeRequest
from core.contracts.profile_selection import OosUnsealing
from core.contracts.strategy import BacktestProvider, TargetPosition
from core.contracts.synthetic import (
    PlantedEffect,
    SyntheticBar,
    SyntheticMarket,
    SyntheticMarketProvider,
    SyntheticMarketSpec,
)
from core.contracts.synthetic import market_hash as synthetic_market_hash
from core.contracts.validation_profile import ValidationProfile
from core.domain.base import content_hash
from core.domain.research import ValidationReport, Verdict, derive_verdict
from research.outcomes.table import materialize
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
from research.validation import (
    SealedOosInput,
    build_report,
    run_sealed_oos,
    sealed_oos_without_result,
)
from research.validation.controls import FixedSides
from research.validation.instruments import EVENT_KEY_SEPARATOR, INSTRUMENT_INFIX
from research.validation.pipeline import CONSUMED_WITHOUT_RESULT
from research.validation.sealed_oos import (
    InMemoryUnsealingLedger,
    SealedEvaluation,
    SealedOosVault,
    SealedWindow,
)

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
    "INSTRUMENT_SEED_RULE",
    "InstrumentArmEvidence",
    "InstrumentRunRecord",
    "MULTI_ARM_KINDS",
    "MultiInstrumentArm",
    "MultiInstrumentCalibrationSetup",
    "MultiInstrumentGateDetector",
    "MultiInstrumentValidatorDetector",
    "RunRecord",
    "SealedArmEvidence",
    "SealedGateDetector",
    "SealedInputsFactory",
    "SealedRelease",
    "SealedRunRecord",
    "StrategyValidatorDetector",
    "instrument_seed",
    "main",
    "planted_arm_id",
    "run_gate_calibration",
    "run_multi_instrument_calibration",
    "supports_sealed_oos",
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
#: Each calibration run is its own simulated hypothesis family.
_FAMILY_FORMAT: Final = "gate_calibration:<arm>:<seed>"
EVALUATED: Final = "evaluated"
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


class SealedRelease:
    """One calibration run's claimed G5 evaluation and its withheld sealed-window bars.

    The harness creates it only **after** ``unseal`` and ``claim_evaluation`` of the run's family,
    so the evaluation is already consumed when ``detect_sealed`` sees it. ``release()`` hands the
    sealed bars out once (``SealedEvaluation.take("bars")``; a second call raises
    ``SealedOosAlreadyEvaluated``). ``vault`` / ``evaluation`` are what
    ``research.validation.SealedOosInput`` needs to run G5 on the claim.
    """

    def __init__(
        self,
        *,
        family_id: str,
        vault: SealedOosVault,
        evaluation: SealedEvaluation,
        unsealing: OosUnsealing,
        bars: tuple[SyntheticBar, ...],
    ) -> None:
        if evaluation.family_id != family_id or not vault.is_evaluated(family_id):
            raise ValueError("a sealed release needs the family's claimed evaluation")
        self.family_id = family_id
        self.vault = vault
        self.evaluation = evaluation
        self.unsealing = unsealing
        self._bars = bars

    @property
    def window(self) -> SealedWindow:
        return self.evaluation.window

    @property
    def released(self) -> bool:
        return self.evaluation.taken("bars")

    def release(self) -> tuple[SyntheticBar, ...]:
        """The sealed-window bars, handed out once against the claimed evaluation."""
        self.evaluation.take("bars")
        return self._bars


class SealedGateDetector(GateDetector, Protocol):
    """A detector that can also run G5 on a claimed sealed evaluation (optional G5 mode).

    ``detect_sealed`` receives the same research-view market ``detect`` saw and must return the
    G5 report under ``profile``: its gates come from ``research.validation.run_sealed_oos`` on
    ``sealed.evaluation`` or, when it ends early, ``sealed_oos_without_result`` (never a PASS).
    """

    def detect_sealed(
        self, market: SyntheticMarket, profile: ValidationProfile, sealed: SealedRelease
    ) -> ValidationReport: ...


def supports_sealed_oos(detector: GateDetector) -> bool:
    """``detect_sealed`` exists and the detector does not declare itself unable to run it."""
    if not callable(getattr(detector, "detect_sealed", None)):
        return False
    return bool(getattr(detector, "sealed_oos_supported", True))


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
#: ``(research inputs, released sealed bars) -> G5 inputs``: the research bars followed by the
#: sealed bars, decision times inside the sealed window only, and the signals over all of them.
SealedInputsFactory = Callable[[EvaluationInputs, tuple[SyntheticBar, ...]], EvaluationInputs]


def _side(weight: Decimal) -> int:
    return int(weight > 0) - int(weight < 0)


def _event_key(target: TargetPosition) -> str:
    return f"{target.instrument}|{target.decision_time.isoformat()}"


def _requested_point(
    candidate: StrategyCandidate, setup: ValidatorSetup
) -> dict[str, ObservationScalar]:
    """The point the validator re-runs (G0 reproducibility compares the two result hashes):
    spec defaults overridden by the chosen point, declared keys only."""
    spec = candidate.spec
    point = {**dict(spec.params), **dict(setup.chosen_params)}
    return {k: v for k, v in point.items() if k in spec.param_search_space}  # type: ignore[misc]


class StrategyValidatorDetector:
    """The full G0 → G4 pipeline through ``PipelineBacktestValidator`` (see module docs).

    ``inputs_for`` must return the **research window** of the market only (the validator fails
    ``G1.sealed_oos_excluded`` otherwise). The trial runner of the last market is kept, so the
    harness's market-outer / Profile-inner order reuses every backtest across candidates.

    ``sealed_inputs_for`` (optional) enables ``detect_sealed`` (G5 mode, module docs); without it
    ``sealed_oos_supported`` is ``False`` and a G5-mode setup refuses the detector.
    """

    def __init__(
        self,
        *,
        name: str,
        candidate: StrategyCandidate,
        backtester: BacktestProvider,
        inputs_for: Callable[[SyntheticMarket], EvaluationInputs],
        setup_for: SetupFactory,
        sealed_inputs_for: SealedInputsFactory | None = None,
    ) -> None:
        self._name = name
        self._candidate = candidate
        self._backtester = backtester
        self._inputs_for = inputs_for
        self._setup_for = setup_for
        self._sealed_inputs_for = sealed_inputs_for
        self._cached: tuple[str, CachingTrialRunner] | None = None

    @property
    def sealed_oos_supported(self) -> bool:
        return self._sealed_inputs_for is not None

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

    def _setup(
        self, market: SyntheticMarket, profile: ValidationProfile, runner: CachingTrialRunner
    ) -> ValidatorSetup:
        setup = self._setup_for(market, profile, runner)
        if setup.trials is not runner:
            raise DetectorConfigurationError("setup_for must use the trial runner it is given")
        if setup.context.profile.content_hash() != profile.content_hash():
            raise DetectorConfigurationError(
                "setup_for must bind the candidate Profile it is given"
            )
        return setup

    def _request(self, setup: ValidatorSetup) -> dict[str, ObservationScalar]:
        return _requested_point(self._candidate, setup)

    def detect(self, market: SyntheticMarket, profile: ValidationProfile) -> ValidationReport:
        runner = self._runner(market)
        setup = self._setup(market, profile, runner)
        spec = self._candidate.spec
        backtest = runner.run(self._request(setup)).backtest
        validation = PipelineBacktestValidator(setup).validate(spec.ref, spec, backtest)
        return validation.report

    def detect_sealed(
        self, market: SyntheticMarket, profile: ValidationProfile, sealed: SealedRelease
    ) -> ValidationReport:
        """G5 on the claimed evaluation (module docs; mirrors ``research.loop.trials``)."""
        if self._sealed_inputs_for is None:
            raise DetectorConfigurationError("detect_sealed needs sealed_inputs_for")
        setup = self._setup(market, profile, self._runner(market))
        context = setup.context
        g5_context = replace(
            context,
            report_id=content_hash(
                {"report": context.report_id, "stage": "sealed_oos", "family": sealed.family_id}
            ),
            metadata=context.metadata.model_copy(
                update={"hypothesis_family_id": sealed.family_id, "oos_unsealing": sealed.unsealing}
            ),
        )

        def without_result(reason: str) -> ValidationReport:
            gates = sealed_oos_without_result(g5_context, sealed.vault, sealed.evaluation, reason)
            return build_report(g5_context, gates)

        sealed_bars = sealed.release()
        inputs = self._sealed_inputs_for(self._inputs_for(market), sealed_bars)
        if not inputs.decision_times:
            return without_result("no_sealed_decision_time")
        if any(t < sealed.window.start for t in inputs.decision_times):
            raise DetectorConfigurationError("sealed_inputs_for must decide inside the window")
        candidate = self._candidate
        run = CandidateTrialRunner(candidate, inputs, self._backtester).run(self._request(setup))
        traded = [t for t in run.targets if _side(t.target_weight) != 0]
        if not traded:
            return without_result("no_non_flat_target_in_the_sealed_window")
        outcome_bars = tuple(
            OutcomePriceBar(
                interval_start=bar.interval_start,
                interval_end=bar.interval_end,
                available_time=bar.available_time,
                open=bar.open,
                high=bar.high,
                low=bar.low,
                close=bar.close,
            )
            for bar in inputs.bars
        )
        table = materialize(
            setup.outcome_provider,
            OutcomeRequest(
                label_spec=context.label_spec,
                manifest_content_hash=content_hash(
                    {
                        "research": setup.manifest_content_hash,
                        "sealed": [bar.content_hash() for bar in sealed_bars],
                    }
                ),
                price_cutoff=max(bar.available_time for bar in outcome_bars),
                events=tuple(
                    OutcomeEvent(event_key=_event_key(t), event_time=t.decision_time)
                    for t in traded
                ),
                bars=outcome_bars,
            ),
        )
        study = FixedSides(
            refs=tuple(candidate.spec.signals),
            by_event={_event_key(t): _side(t.target_weight) for t in traded},
        )
        gates = run_sealed_oos(
            SealedOosInput(g5_context, sealed.vault, table, study, sealed.evaluation)
        )
        return build_report(g5_context, gates)


class MultiInstrumentGateDetector(Protocol):
    """Multi-instrument mode: validate one book of instruments under one candidate Profile.

    ``markets`` maps each symbol of the setup (in its order) to that instrument's market; the
    report must come from the multi-instrument path (``ValidatorSetup.instruments``): pooled
    G0 – G3, each instrument's own ``<gate>.instrument.<symbol>`` G0 – G3, then G4.
    """

    @property
    def name(self) -> str: ...

    def describe(self) -> Mapping[str, str]: ...

    def detect_instruments(
        self, markets: Mapping[str, SyntheticMarket], profile: ValidationProfile
    ) -> ValidationReport: ...


#: ``(markets by symbol, candidate Profile, cached trial runner) -> ValidatorSetup``; the setup must
#: use the given runner, bind the given Profile and validate exactly the given symbols.
BookSetupFactory = Callable[
    [Mapping[str, SyntheticMarket], ValidationProfile, TrialRunner], ValidatorSetup
]


class MultiInstrumentValidatorDetector:
    """The full multi-instrument G0 → G4 pipeline through ``PipelineBacktestValidator``.

    Like ``StrategyValidatorDetector`` (research window only, trial runs cached per book) but over
    several instruments: ``inputs_for`` returns the research-window inputs of every instrument of
    the book, and ``setup_for`` a ``ValidatorSetup`` whose ``instruments`` are exactly the book's
    symbols (anything else is a ``DetectorConfigurationError``).
    """

    def __init__(
        self,
        *,
        name: str,
        candidate: StrategyCandidate,
        backtester: BacktestProvider,
        inputs_for: Callable[[Mapping[str, SyntheticMarket]], EvaluationInputs],
        setup_for: BookSetupFactory,
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
            "path": "research.validation.instruments.run_multi_instrument_validation",
            "strategy": str(spec.ref),
            "strategy_hash": spec.content_hash(),
            "hypothesis_family_id": self._candidate.hypothesis_family_id,
        }

    def _runner(self, markets: Mapping[str, SyntheticMarket]) -> CachingTrialRunner:
        key = _book_hash({symbol: market.market_hash for symbol, market in markets.items()})
        if self._cached is None or self._cached[0] != key:
            inner = CandidateTrialRunner(
                self._candidate, self._inputs_for(markets), self._backtester
            )
            self._cached = (key, CachingTrialRunner(inner))
        return self._cached[1]

    def detect_instruments(
        self, markets: Mapping[str, SyntheticMarket], profile: ValidationProfile
    ) -> ValidationReport:
        runner = self._runner(markets)
        setup = self._setup_for(markets, profile, runner)
        if setup.trials is not runner:
            raise DetectorConfigurationError("setup_for must use the trial runner it is given")
        if setup.context.profile.content_hash() != profile.content_hash():
            raise DetectorConfigurationError(
                "setup_for must bind the candidate Profile it is given"
            )
        if setup.instruments is None or set(setup.validated_instruments) != set(markets):
            raise DetectorConfigurationError(
                "setup_for must validate exactly the book's instruments "
                "(ValidatorSetup.instruments)"
            )
        spec = self._candidate.spec
        backtest = runner.run(_requested_point(self._candidate, setup)).backtest
        return PipelineBacktestValidator(setup).validate(spec.ref, spec, backtest).report


def _book_hash(hashes: Mapping[str, str]) -> str:
    """One content hash for a book's per-instrument hashes (symbol -> hash)."""
    return content_hash({"instruments": dict(sorted(hashes.items()))})


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
      parameter, not a Profile number);
    - ``sealed_oos_g5``: the one opt-in switch (module docs, G5 mode). It is ``False`` unless the
      caller sets it, so setups written before G5 mode existed keep their reports byte-identical.
      With it on, the detector must support ``detect_sealed`` and ``base`` must generate every
      candidate's whole sealed window after some research data (checked from the spec only).
    """

    provider: SyntheticMarketProvider
    base: SyntheticMarketSpec
    detector: GateDetector
    candidates: tuple[ValidationProfile, ...]
    noise_seeds: tuple[int, ...]
    planted: tuple[PlantedEffect, ...]
    planted_seeds: tuple[int, ...]
    alpha: Decimal
    sealed_oos_g5: bool = False

    def __post_init__(self) -> None:
        self._check_inputs()
        if not isinstance(self.sealed_oos_g5, bool):
            raise ValueError("sealed_oos_g5 must be a bool")
        if self.sealed_oos_g5:
            self._check_g5()

    def _check_g5(self) -> None:
        """Decided from the detector and the spec only: no market is generated or read."""
        if not supports_sealed_oos(self.detector):
            raise ValueError(
                f"G5 mode needs a detector with detect_sealed; {self.detector.name!r} has none"
            )
        end = self.base.start + timedelta(minutes=self.base.minutes)
        for profile in self.candidates:
            window = SealedWindow.from_profile(profile)
            if not self.base.start < window.start or end < window.end:
                raise ValueError(
                    f"G5 mode: the base spec must cover {profile.ref}'s sealed window "
                    f"[{window.start.isoformat()}, {window.end.isoformat()}) after research data"
                )

    def _check_inputs(self) -> None:
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
        payload: dict[str, object] = {
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
        # Additive and only when on: setups without G5 mode keep their report hashes.
        if self.sealed_oos_g5:
            payload["sealed_oos_g5"] = {"enabled": True, "family_id": _FAMILY_FORMAT}
        return payload

    def run_count(self) -> int:
        return sum(len(seeds) for _, _, seeds in self.arms())


def _check_common(
    base: SyntheticMarketSpec, candidates: tuple[ValidationProfile, ...], alpha: Decimal
) -> None:
    if base.effects:
        raise ValueError("the base spec must be pure noise (no planted effects)")
    if not candidates:
        raise ValueError("at least one candidate Profile is required")
    hashes = [profile.content_hash() for profile in candidates]
    if len(set(hashes)) != len(hashes):
        raise ValueError("candidate Profiles must be distinct")
    if not Decimal(0) < alpha < Decimal(1):
        raise ValueError("alpha must be in (0, 1)")


#: An arm name is part of each run's simulated family id (``gate_calibration:<arm>:<seed>``).
_ARM_NAME: Final = re.compile(r"[A-Za-z0-9_.-]+")
#: What an instrument's generator seed is derived from (recorded in the report inputs).
INSTRUMENT_SEED_RULE: Final = (
    "int(content_hash({'kind': 'gate_calibration_instrument_seed', 'run_seed': <seed>, "
    "'index': <i>, 'symbol': <symbol>})[:12], 16)"
)


def instrument_seed(run_seed: int, index: int, symbol: str) -> int:
    """The generator seed of instrument ``index`` / ``symbol`` in the run seeded ``run_seed``.

    Deterministic, independent of the arm (two arms given the same run seed share their noise
    draws: a paired comparison, the caller's choice) and 48 bits wide.
    """
    digest = content_hash(
        {
            "kind": "gate_calibration_instrument_seed",
            "run_seed": run_seed,
            "index": index,
            "symbol": symbol,
        }
    )
    return int(digest[:12], 16)


def _role(effect: PlantedEffect | None) -> str:
    return NOISE_ARM if effect is None else planted_arm_id(effect)


@dataclass(frozen=True)
class MultiInstrumentArm:
    """One multi-instrument arm, declared by the caller: nothing is defaulted or inferred.

    - ``kind``: ``all_noise`` / ``all_planted`` / ``mixed`` (``MULTI_ARM_KINDS``); it must agree
      with ``effects``, so a mislabelled arm is refused rather than reported under the wrong name;
    - ``effects``: one entry per symbol of the setup, in its order: ``None`` for a pure-noise
      instrument, else the one ``PlantedEffect`` planted into it;
    - ``seeds``: the run seeds; each instrument's generator seed is ``instrument_seed``.
    """

    name: str
    kind: str
    effects: tuple[PlantedEffect | None, ...]
    seeds: tuple[int, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not _ARM_NAME.fullmatch(self.name):
            raise ValueError(f"arm name {self.name!r} must match {_ARM_NAME.pattern}")
        if self.kind not in MULTI_ARM_KINDS:
            raise ValueError(f"{self.name}: kind must be one of {MULTI_ARM_KINDS}")
        if not isinstance(self.effects, tuple) or not self.effects:
            raise ValueError(f"{self.name}: effects must declare every instrument")
        if any(effect is not None and effect.strength == 0 for effect in self.effects):
            raise ValueError(f"{self.name}: a planted effect with strength 0 is noise")
        planted = sum(effect is not None for effect in self.effects)
        actual = (
            "all_noise"
            if planted == 0
            else "all_planted"
            if planted == len(self.effects)
            else "mixed"
        )
        if actual != self.kind:
            raise ValueError(
                f"{self.name}: declared kind {self.kind!r} but its effects are {actual}"
            )
        if not self.seeds:
            raise ValueError(f"{self.name}: at least one seed is required")
        if len(set(self.seeds)) != len(self.seeds):
            raise ValueError("seeds must be distinct")
        if any(isinstance(s, bool) or not isinstance(s, int) or s < 0 for s in self.seeds):
            raise ValueError(f"{self.name}: seeds must be non-negative integers")

    @property
    def roles(self) -> tuple[str, ...]:
        """Per symbol: ``noise`` or the planted arm id of its effect."""
        return tuple(_role(effect) for effect in self.effects)


@dataclass(frozen=True)
class MultiInstrumentCalibrationSetup:
    """Every input of one multi-instrument calibration (the opt-in multi-instrument mode).

    Separate from ``GateCalibrationSetup`` (whose reports are untouched): each run generates one
    ``RandomWalkMarket``-style market per symbol from ``base`` (only ``symbol`` / ``seed`` /
    ``effects`` change) and ``detector.detect_instruments`` validates them together. Nothing has a
    default: ``symbols`` (at least two, distinct, without ``|``) and ``arms`` (at least one,
    distinct names, one effect entry per symbol) are the caller's. G5 mode is not offered here.
    """

    provider: SyntheticMarketProvider
    base: SyntheticMarketSpec
    detector: MultiInstrumentGateDetector
    candidates: tuple[ValidationProfile, ...]
    symbols: tuple[str, ...]
    arms: tuple[MultiInstrumentArm, ...]
    alpha: Decimal

    def __post_init__(self) -> None:
        _check_common(self.base, self.candidates, self.alpha)
        if not callable(getattr(self.detector, "detect_instruments", None)):
            raise ValueError(
                f"multi-instrument mode needs a detector with detect_instruments; "
                f"{getattr(self.detector, 'name', self.detector)!r} has none"
            )
        symbols = self.symbols
        if not isinstance(symbols, tuple) or len(symbols) < 2:
            raise ValueError("multi-instrument mode needs at least two symbols (k >= 2)")
        if len(set(symbols)) != len(symbols):
            raise ValueError(f"symbols must be distinct: {symbols}")
        if any(not isinstance(s, str) or not s or EVENT_KEY_SEPARATOR in s for s in symbols):
            raise ValueError(f"a symbol must be a non-empty string without {EVENT_KEY_SEPARATOR!r}")
        if not isinstance(self.arms, tuple) or not self.arms:
            raise ValueError("multi-instrument mode needs at least one declared arm")
        if not all(isinstance(arm, MultiInstrumentArm) for arm in self.arms):
            raise ValueError("every arm must be a declared MultiInstrumentArm")
        names = [arm.name for arm in self.arms]
        if len(set(names)) != len(names):
            raise ValueError(f"arm names must be distinct: {names}")
        for arm in self.arms:
            if len(arm.effects) != len(symbols):
                raise ValueError(
                    f"{arm.name}: {len(arm.effects)} effect entries for {len(symbols)} symbols"
                )

    def inputs_payload(self) -> dict[str, object]:
        return {
            "generator": self.provider.descriptor.plugin_key,
            "detector": {"name": self.detector.name, **dict(self.detector.describe())},
            "base_spec": self.base.model_dump(mode="json"),
            "base_spec_hash": self.base.content_hash(),
            "multi_instrument": {
                "symbols": list(self.symbols),
                "instrument_seed": INSTRUMENT_SEED_RULE,
                "arms": [
                    {
                        "arm": arm.name,
                        "kind": arm.kind,
                        "seeds": list(arm.seeds),
                        "instruments": [
                            {
                                "symbol": symbol,
                                "role": _role(effect),
                                "effect": None
                                if effect is None
                                else effect.model_dump(mode="json"),
                                "effect_hash": None if effect is None else effect.content_hash(),
                            }
                            for symbol, effect in zip(self.symbols, arm.effects, strict=True)
                        ],
                    }
                    for arm in self.arms
                ],
            },
            "candidate_profiles": [
                {"profile": str(profile.ref), "profile_hash": profile.content_hash()}
                for profile in self.candidates
            ],
            "interval": {"method": INTERVAL_METHOD, "alpha": str(self.alpha)},
        }

    def run_count(self) -> int:
        return sum(len(arm.seeds) for arm in self.arms)


def _gate_lists(gates: Sequence[tuple[str, Verdict]]) -> dict[str, list[str]]:
    return {
        "failing_gates": [g for g, v in gates if v is Verdict.FAIL],
        "inconclusive_gates": [g for g, v in gates if v is Verdict.INCONCLUSIVE],
    }


@dataclass(frozen=True, slots=True)
class SealedRunRecord:
    """The G5 step of one G0 – G4 ``PASS`` run (G5 mode only; module docs).

    ``consumed_without_result``: the claimed evaluation ended before any G5 statistic (the
    detector reported ``consumed_without_result:<reason>`` or raised); such a G5 is never a pass.
    ``sealed_bars_released``: the withheld sealed bars left the ``SealedRelease``.
    """

    family_id: str
    verdict: Verdict
    gates: tuple[tuple[str, Verdict], ...]
    gates_hash: str
    consumed_without_result: bool
    sealed_bars_released: bool
    #: ``"<ExceptionType>: <message>"`` when ``detect_sealed`` raised.
    detector_error: str | None = None

    def __post_init__(self) -> None:
        if self.detector_error is not None and (
            self.verdict is not Verdict.INCONCLUSIVE
            or self.gates
            or not self.consumed_without_result
        ):
            raise ValueError(
                "a G5 detector error is an INCONCLUSIVE, consumed_without_result G5 without gates"
            )
        if self.consumed_without_result and self.verdict is Verdict.PASS:
            raise ValueError("a G5 consumed without result is never a PASS")

    def to_payload(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "family_id": self.family_id,
            "verdict": self.verdict.value,
            "status": CONSUMED_WITHOUT_RESULT if self.consumed_without_result else EVALUATED,
            "gates_hash": self.gates_hash,
            **_gate_lists(self.gates),
            "sealed_bars_released": self.sealed_bars_released,
        }
        if self.detector_error is not None:
            payload["detector_error"] = self.detector_error
        return payload


@dataclass(frozen=True, slots=True)
class InstrumentRunRecord:
    """One instrument of a multi-instrument run (multi-instrument mode only).

    ``role`` is ``noise`` or the planted arm id of its effect; ``verdict`` is the instrument's own
    verdict (``derive_verdict`` of its ``<gate>.instrument.<symbol>`` gates), ``None`` when no
    per-instrument gate was evaluated (a pooled stage failed, or the detector raised).
    """

    symbol: str
    role: str
    seed: int
    market_spec_hash: str
    market_hash: str
    verdict: Verdict | None

    def to_payload(self) -> dict[str, object]:
        return {
            "symbol": self.symbol,
            "role": self.role,
            "seed": self.seed,
            "market_spec_hash": self.market_spec_hash,
            "market_hash": self.market_hash,
            "verdict": None if self.verdict is None else self.verdict.value,
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
    #: The G5 step (G5 mode, G0 – G4 ``PASS`` runs only); ``None`` when G5 was not reached.
    g5: SealedRunRecord | None = None
    #: Multi-instrument mode only: the run's instruments (in the setup's symbol order).
    instruments: tuple[InstrumentRunRecord, ...] | None = None

    def __post_init__(self) -> None:
        if self.detector_error is not None and (
            self.verdict is not Verdict.INCONCLUSIVE or self.gates
        ):
            raise ValueError("a detector error is an INCONCLUSIVE run without gates")
        if self.instruments is not None and self.g5 is not None:
            raise ValueError("G5 mode is not available in multi-instrument mode")
        if self.g5 is not None and not (self.verdict is Verdict.PASS and self.sealed_oos_unsealed):
            raise ValueError("only an unsealed G0 - G4 PASS reaches G5")

    @property
    def end_to_end_passed(self) -> bool:
        """G0 – G5 all passed (only meaningful in G5 mode)."""
        return self.g5 is not None and self.g5.verdict is Verdict.PASS

    def all_gates(self) -> tuple[tuple[str, Verdict], ...]:
        return self.gates if self.g5 is None else (*self.gates, *self.g5.gates)

    def to_payload(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "arm": self.arm,
            "seed": self.seed,
            "market_spec_hash": self.market_spec_hash,
            "market_hash": self.market_hash,
            "verdict": self.verdict.value,
            "gates_hash": self.gates_hash,
            **_gate_lists(self.gates),
            "sealed_oos_unsealed": self.sealed_oos_unsealed,
        }
        # Additive and only when present: reports without detector errors keep their hashes.
        if self.detector_error is not None:
            payload["detector_error"] = self.detector_error
        if self.g5 is not None:
            payload["sealed_oos_g5"] = self.g5.to_payload()
        if self.instruments is not None:
            payload["instruments"] = [item.to_payload() for item in self.instruments]
        return payload


def _pass_key(arm: str) -> str:
    """Noise passes are false positives; planted passes are detections (power)."""
    return "false_positive_rate" if arm == NOISE_ARM else "power"


def _rate_payload(rate: BinomialRate | None) -> dict[str, object] | None:
    return None if rate is None else rate.to_payload()


@dataclass(frozen=True, slots=True)
class SealedArmEvidence:
    """G5 rates of one arm under one candidate (G5 mode only).

    ``passed`` / ``inconclusive`` / ``failed`` are conditional on reaching G5 (``n = reached``;
    ``None`` when no run of the arm reached G5). ``end_to_end`` is the G0 – G5 pass rate over
    **every** run of the arm: the false-positive rate on noise, the power on a planted arm.
    """

    arm: str
    reached: int
    passed: BinomialRate | None
    inconclusive: BinomialRate | None
    failed: BinomialRate | None
    consumed_without_result: int
    detector_errors: int
    end_to_end: BinomialRate

    def to_payload(self) -> dict[str, object]:
        return {
            "reached": self.reached,
            "pass_rate": _rate_payload(self.passed),
            "inconclusive_rate": _rate_payload(self.inconclusive),
            "fail_rate": _rate_payload(self.failed),
            "consumed_without_result": self.consumed_without_result,
            "detector_errors": self.detector_errors,
            "end_to_end_g0_g5": {_pass_key(self.arm): self.end_to_end.to_payload()},
        }


#: Multi-instrument arm kinds and the name of their pipeline pass rate. A ``mixed`` PASS needs
#: every instrument's own gates to pass, noise ones included, so it is named neutrally.
MULTI_ARM_KINDS: Final = ("all_noise", "all_planted", "mixed")
_KIND_PASS_KEYS: Final = {
    "all_noise": "false_positive_rate",
    "all_planted": "power",
    "mixed": "pass_rate",
}


@dataclass(frozen=True, slots=True)
class InstrumentArmEvidence:
    """One instrument's own verdict rates in one multi-instrument arm (``n``: every run)."""

    symbol: str
    role: str
    passed: BinomialRate
    inconclusive: BinomialRate
    failed: BinomialRate
    not_evaluated: int

    def to_payload(self) -> dict[str, object]:
        return {
            "role": self.role,
            _pass_key(self.role): self.passed.to_payload(),
            "inconclusive_rate": self.inconclusive.to_payload(),
            "fail_rate": self.failed.to_payload(),
            "not_evaluated": self.not_evaluated,
        }


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
    #: G5 mode only.
    g5: SealedArmEvidence | None = None
    #: Multi-instrument mode only (``kind`` / ``failed_rate`` / ``instruments`` go together).
    kind: str | None = None
    failed_rate: BinomialRate | None = None
    instruments: tuple[InstrumentArmEvidence, ...] | None = None

    def to_payload(self) -> dict[str, object]:
        key = _pass_key(self.arm) if self.kind is None else _KIND_PASS_KEYS[self.kind]
        payload: dict[str, object] = {
            "runs": self.passed.n,
            key: self.passed.to_payload(),
            "inconclusive_rate": self.inconclusive.to_payload(),
            "failed": self.failed,
            "sealed_oos_consumption_rate": self.sealed_oos_consumed.to_payload(),
        }
        if self.detector_errors:
            payload["detector_errors"] = self.detector_errors
        if self.g5 is not None:
            payload["sealed_oos_g5"] = self.g5.to_payload()
        # Additive and only in multi-instrument mode: every other report keeps its hash.
        if self.kind is not None:
            payload["kind"] = self.kind
        if self.failed_rate is not None:
            payload["fail_rate"] = self.failed_rate.to_payload()
        if self.instruments is not None:
            payload["instruments"] = {item.symbol: item.to_payload() for item in self.instruments}
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
    #: Multi-instrument mode only: the pass-rate name (by the arm's kind, or by the instrument's
    #: role for a ``<gate>.instrument.<symbol>`` gate) and the fail rate.
    pass_key: str | None = None
    failed_rate: BinomialRate | None = None

    def to_payload(self) -> dict[str, object]:
        payload: dict[str, object] = {
            self.pass_key or _pass_key(self.arm): self.passed.to_payload(),
            "inconclusive_rate": self.inconclusive.to_payload(),
            "failed": self.failed,
            "not_evaluated": self.not_evaluated,
        }
        if self.failed_rate is not None:
            payload["fail_rate"] = self.failed_rate.to_payload()
        return payload


@dataclass(frozen=True)
class CandidateEvidence:
    profile: str
    profile_hash: str
    arms: tuple[ArmEvidence, ...]
    gates: tuple[GateEvidence, ...]
    runs: tuple[RunRecord, ...]
    sealed_oos_unsealings: int
    #: G5 mode only: the number of claimed (consumed) sealed evaluations.
    sealed_oos_g5_evaluations: int | None = None

    def arm(self, arm: str) -> ArmEvidence:
        return next(item for item in self.arms if item.arm == arm)

    @property
    def false_positive_rate(self) -> BinomialRate:
        return self.arm(NOISE_ARM).passed

    def power(self, effect: PlantedEffect) -> BinomialRate:
        return self.arm(planted_arm_id(effect)).passed

    def g5(self, arm: str) -> SealedArmEvidence:
        evidence = self.arm(arm).g5
        if evidence is None:
            raise ValueError("G5 was not run (GateCalibrationSetup.sealed_oos_g5 is off)")
        return evidence

    @property
    def end_to_end_false_positive_rate(self) -> BinomialRate:
        """G0 – G5 pass rate on noise (G5 mode only)."""
        return self.g5(NOISE_ARM).end_to_end

    def end_to_end_power(self, effect: PlantedEffect) -> BinomialRate:
        """G0 – G5 pass rate of a planted arm (G5 mode only)."""
        return self.g5(planted_arm_id(effect)).end_to_end

    def gate(self, gate_id: str, arm: str) -> GateEvidence:
        return next(g for g in self.gates if g.gate_id == gate_id and g.arm == arm)

    def instrument(self, arm: str, symbol: str) -> InstrumentArmEvidence:
        """One instrument's own verdict rates in one arm (multi-instrument mode only)."""
        instruments = self.arm(arm).instruments
        if instruments is None:
            raise ValueError("per-instrument evidence exists in multi-instrument mode only")
        return next(item for item in instruments if item.symbol == symbol)

    def gate_ids(self) -> tuple[str, ...]:
        return tuple(sorted({g.gate_id for g in self.gates}))

    def to_payload(self) -> dict[str, object]:
        gates: dict[str, dict[str, object]] = {}
        for gate in self.gates:
            gates.setdefault(gate.gate_id, {})[gate.arm] = gate.to_payload()
        payload: dict[str, object] = {
            "profile": self.profile,
            "profile_hash": self.profile_hash,
            "pipeline": {arm.arm: arm.to_payload() for arm in self.arms},
            "gates": gates,
            "sealed_oos_unsealings": self.sealed_oos_unsealings,
        }
        if self.sealed_oos_g5_evaluations is not None:
            payload["sealed_oos_g5_evaluations"] = self.sealed_oos_g5_evaluations
        payload["runs"] = [run.to_payload() for run in self.runs]
        return payload


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


def _error_text(error: Exception) -> str:
    message = " ".join(str(error).split())[:_ERROR_MESSAGE_LIMIT]
    return f"{type(error).__name__}: {message}" if message else type(error).__name__


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
    return RunRecord(
        arm=arm,
        seed=seed,
        market_spec_hash=market.spec_hash,
        market_hash=market.market_hash,
        verdict=Verdict.INCONCLUSIVE,
        gates=(),
        gates_hash=content_hash([]),
        detector_error=_error_text(error),
    )


def _family(arm: str, seed: int) -> str:
    return f"gate_calibration:{arm}:{seed}"


def _split(
    market: SyntheticMarket, window: SealedWindow
) -> tuple[SyntheticMarket, tuple[SyntheticBar, ...]]:
    """G5 mode: the research view (bars ending at or before the window start) and the withheld
    sealed-window bars. Splitting a generated synthetic market is not reading it: no detector
    step sees the second part before its family's claim (module docs)."""
    research = tuple(bar for bar in market.bars if bar.interval_end <= window.start)
    sealed = tuple(
        bar
        for bar in market.bars
        if window.start <= bar.interval_start and bar.interval_end <= window.end
    )
    view = SyntheticMarket(
        spec_hash=market.spec_hash,
        provider=market.provider,
        bars=research,
        truth=market.truth,
        market_hash=synthetic_market_hash(
            market.spec_hash, market.provider, research, market.truth
        ),
    )
    return view, sealed


def _run_g5(
    detector: SealedGateDetector,
    vault: SealedOosVault,
    family: str,
    unsealing: OosUnsealing,
    view: SyntheticMarket,
    sealed_bars: tuple[SyntheticBar, ...],
    profile: ValidationProfile,
) -> SealedRunRecord:
    """Claim the family's one evaluation, then run the detector's G5 on it (module docs)."""
    # From here on the evaluation is consumed, whatever happens next (R21 / R27).
    evaluation = vault.claim_evaluation(family)
    sealed = SealedRelease(
        family_id=family, vault=vault, evaluation=evaluation, unsealing=unsealing, bars=sealed_bars
    )
    try:
        report = detector.detect_sealed(view, profile, sealed)
    except DetectorConfigurationError:
        raise
    except Exception as error:  # the method failed on the claimed window: evidence, not a pass
        return SealedRunRecord(
            family_id=family,
            verdict=Verdict.INCONCLUSIVE,
            gates=(),
            gates_hash=content_hash([]),
            consumed_without_result=True,
            sealed_bars_released=sealed.released,
            detector_error=_error_text(error),
        )
    if report.validation_profile_hash != profile.content_hash():
        raise ValueError(f"{detector.name}: G5 report is not under {profile.ref}")
    if not any(gate.gate_id.startswith("G5.") for gate in report.gates):
        raise DetectorConfigurationError("detect_sealed must return the G5 gates")
    if report.verdict is Verdict.PASS and not evaluation.taken("labels"):
        raise DetectorConfigurationError(
            "a G5 PASS must come from run_sealed_oos on the claimed evaluation"
        )
    return SealedRunRecord(
        family_id=family,
        verdict=report.verdict,
        gates=tuple((gate.gate_id, gate.verdict) for gate in report.gates),
        gates_hash=content_hash([gate.content_hash() for gate in report.gates]),
        consumed_without_result=any(
            gate.metric.startswith(f"{CONSUMED_WITHOUT_RESULT}:") for gate in report.gates
        ),
        sealed_bars_released=sealed.released,
    )


def _sealed_arm(arm: str, mine: Sequence[RunRecord], alpha: Decimal) -> SealedArmEvidence:
    reached = [run.g5 for run in mine if run.g5 is not None]
    k = len(reached)

    def conditional(verdict: Verdict) -> BinomialRate | None:
        if not k:
            return None
        return binomial_rate(sum(g5.verdict is verdict for g5 in reached), k, alpha)

    return SealedArmEvidence(
        arm=arm,
        reached=k,
        passed=conditional(Verdict.PASS),
        inconclusive=conditional(Verdict.INCONCLUSIVE),
        failed=conditional(Verdict.FAIL),
        consumed_without_result=sum(g5.consumed_without_result for g5 in reached),
        detector_errors=sum(g5.detector_error is not None for g5 in reached),
        end_to_end=binomial_rate(sum(run.end_to_end_passed for run in mine), len(mine), alpha),
    )


def _evidence(
    profile: ValidationProfile,
    records: Sequence[RunRecord],
    arms: Sequence[str],
    alpha: Decimal,
    *,
    sealed_oos_g5: bool,
) -> CandidateEvidence:
    arm_rows: list[ArmEvidence] = []
    gate_rows: list[GateEvidence] = []
    gate_ids = sorted({gate for run in records for gate, _ in run.all_gates()})
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
                g5=_sealed_arm(arm, mine, alpha) if sealed_oos_g5 else None,
            )
        )
        for gate_id in gate_ids:
            seen = [dict(run.all_gates()).get(gate_id) for run in mine]
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
        sealed_oos_g5_evaluations=(
            sum(run.g5 is not None for run in records) if sealed_oos_g5 else None
        ),
    )


def run_gate_calibration(setup: GateCalibrationSetup) -> GateCalibrationReport:
    """Validate every market of every arm under every candidate Profile (see module docs).

    Each G0 – G4 ``PASS`` spends its family's single unsealing in the candidate's own in-memory
    vault (capacity: the number of runs, a bookkeeping bound); in G5 mode it then claims the
    evaluation and runs ``detect_sealed``.
    """
    runs: dict[str, list[RunRecord]] = {p.content_hash(): [] for p in setup.candidates}
    vaults = {
        p.content_hash(): SealedOosVault(
            p, InMemoryUnsealingLedger(), max_unsealings=setup.run_count()
        )
        for p in setup.candidates
    }
    for arm, effects, seeds in setup.arms():
        for seed in seeds:
            spec = setup.base.model_copy(update={"seed": seed, "effects": effects})
            market = setup.provider.generate(spec)
            if market.truth != effects:
                raise ValueError(f"{arm}/{seed}: the market's truth is not the planted effects")
            for profile in setup.candidates:
                key = profile.content_hash()
                vault = vaults[key]
                view, sealed_bars = (
                    _split(market, vault.window) if setup.sealed_oos_g5 else (market, ())
                )
                try:
                    report = setup.detector.detect(view, profile)
                except DetectorConfigurationError:
                    raise
                except Exception as error:  # the method failed on this market: evidence
                    runs[key].append(_errored(arm, seed, market, error))
                    continue
                if report.validation_profile_hash != profile.content_hash():
                    raise ValueError(f"{setup.detector.name}: report is not under {profile.ref}")
                record = _record(arm, seed, market, report)
                if record.verdict is Verdict.PASS:
                    family = _family(arm, seed)
                    unsealing = vault.unseal(
                        family, approved_by=_UNSEALED_BY, at=vault.window.start
                    )
                    g5 = None
                    if setup.sealed_oos_g5:
                        detector = cast(SealedGateDetector, setup.detector)
                        g5 = _run_g5(detector, vault, family, unsealing, view, sealed_bars, profile)
                    record = replace(record, sealed_oos_unsealed=True, g5=g5)
                runs[key].append(record)
    arms = [arm for arm, _, _ in setup.arms()]
    return GateCalibrationReport(
        inputs=setup.inputs_payload(),
        candidates=tuple(
            _evidence(
                profile,
                runs[profile.content_hash()],
                arms,
                setup.alpha,
                sealed_oos_g5=setup.sealed_oos_g5,
            )
            for profile in setup.candidates
        ),
    )


# ======================================================================================
# Multi-instrument mode
# ======================================================================================


def _instrument_symbol(gate_id: str, symbols: Sequence[str]) -> str | None:
    """The symbol a ``<gate>.instrument.<symbol>`` gate belongs to (``None``: a pooled gate)."""
    return next((s for s in symbols if gate_id.endswith(f"{INSTRUMENT_INFIX}{s}")), None)


def _generate_book(
    setup: MultiInstrumentCalibrationSetup, arm: MultiInstrumentArm, seed: int
) -> tuple[dict[str, SyntheticMarket], dict[str, int]]:
    markets: dict[str, SyntheticMarket] = {}
    seeds: dict[str, int] = {}
    for index, (symbol, effect) in enumerate(zip(setup.symbols, arm.effects, strict=True)):
        effects = () if effect is None else (effect,)
        seeds[symbol] = instrument_seed(seed, index, symbol)
        spec = setup.base.model_copy(
            update={"symbol": symbol, "seed": seeds[symbol], "effects": effects}
        )
        market = setup.provider.generate(spec)
        if market.truth != effects:
            raise ValueError(
                f"{arm.name}/{seed}/{symbol}: the market's truth is not the planted effects"
            )
        markets[symbol] = market
    if len(set(seeds.values())) != len(seeds):  # 48-bit hash collision: refuse, never share
        raise ValueError(f"{arm.name}/{seed}: two instruments derived the same seed")
    return markets, seeds


def _book_record(
    setup: MultiInstrumentCalibrationSetup,
    arm: MultiInstrumentArm,
    seed: int,
    markets: Mapping[str, SyntheticMarket],
    seeds: Mapping[str, int],
    outcome: ValidationReport | Exception,
) -> RunRecord:
    gates = () if isinstance(outcome, Exception) else outcome.gates
    instruments = []
    for symbol, role in zip(setup.symbols, arm.roles, strict=True):
        mine = [gate for gate in gates if _instrument_symbol(gate.gate_id, (symbol,))]
        instruments.append(
            InstrumentRunRecord(
                symbol=symbol,
                role=role,
                seed=seeds[symbol],
                market_spec_hash=markets[symbol].spec_hash,
                market_hash=markets[symbol].market_hash,
                verdict=derive_verdict(mine) if mine else None,
            )
        )
    common = {
        "arm": arm.name,
        "seed": seed,
        "market_spec_hash": _book_hash({s: m.spec_hash for s, m in markets.items()}),
        "market_hash": _book_hash({s: m.market_hash for s, m in markets.items()}),
        "gates": tuple((gate.gate_id, gate.verdict) for gate in gates),
        "gates_hash": content_hash([gate.content_hash() for gate in gates]),
        "instruments": tuple(instruments),
    }
    if isinstance(outcome, Exception):
        return RunRecord(
            verdict=Verdict.INCONCLUSIVE,
            detector_error=_error_text(outcome),
            **common,  # type: ignore[arg-type]
        )
    return RunRecord(verdict=outcome.verdict, **common)  # type: ignore[arg-type]


def _multi_evidence(
    setup: MultiInstrumentCalibrationSetup,
    profile: ValidationProfile,
    records: Sequence[RunRecord],
) -> CandidateEvidence:
    """The standard evidence plus, per arm, its kind, fail rates and per-instrument rates."""
    alpha = setup.alpha
    plain = _evidence(profile, records, [a.name for a in setup.arms], alpha, sealed_oos_g5=False)
    by_name = {arm.name: arm for arm in setup.arms}
    arms: list[ArmEvidence] = []
    for evidence in plain.arms:
        arm = by_name[evidence.arm]
        mine = [run for run in records if run.arm == arm.name]
        n = len(mine)
        rows: list[InstrumentArmEvidence] = []
        for index, (symbol, role) in enumerate(zip(setup.symbols, arm.roles, strict=True)):
            verdicts = [cast(tuple[InstrumentRunRecord, ...], run.instruments)[index].verdict
                        for run in mine]  # fmt: skip
            rows.append(
                InstrumentArmEvidence(
                    symbol=symbol,
                    role=role,
                    passed=binomial_rate(verdicts.count(Verdict.PASS), n, alpha),
                    inconclusive=binomial_rate(verdicts.count(Verdict.INCONCLUSIVE), n, alpha),
                    failed=binomial_rate(verdicts.count(Verdict.FAIL), n, alpha),
                    not_evaluated=verdicts.count(None),
                )
            )
        arms.append(
            replace(
                evidence,
                kind=arm.kind,
                failed_rate=binomial_rate(evidence.failed, n, alpha),
                instruments=tuple(rows),
            )
        )
    gates: list[GateEvidence] = []
    for gate in plain.gates:
        arm = by_name[gate.arm]
        owner = _instrument_symbol(gate.gate_id, setup.symbols)
        key = (
            _KIND_PASS_KEYS[arm.kind]
            if owner is None
            else _pass_key(arm.roles[setup.symbols.index(owner)])
        )
        gates.append(
            replace(
                gate,
                pass_key=key,
                failed_rate=binomial_rate(gate.failed, gate.passed.n, alpha),
            )
        )
    return replace(plain, arms=tuple(arms), gates=tuple(gates))


def run_multi_instrument_calibration(
    setup: MultiInstrumentCalibrationSetup,
) -> GateCalibrationReport:
    """Multi-instrument mode (module docs): every run of every declared arm generates one market
    per symbol and is validated under every candidate through ``detect_instruments``.

    Detector failures are evidence (an ``INCONCLUSIVE`` run without gates, ``detector_error``),
    as in ``run_gate_calibration``; a report under another Profile, or one that did not take the
    multi-instrument path (``G0.single_instrument_adapter``), is a harness misconfiguration and
    raises. A pooled PASS spends its family's unsealing exactly as a single-instrument PASS does.
    """
    runs: dict[str, list[RunRecord]] = {p.content_hash(): [] for p in setup.candidates}
    vaults = {
        p.content_hash(): SealedOosVault(
            p, InMemoryUnsealingLedger(), max_unsealings=setup.run_count()
        )
        for p in setup.candidates
    }
    for arm in setup.arms:
        for seed in arm.seeds:
            markets, seeds = _generate_book(setup, arm, seed)
            view = MappingProxyType(markets)
            for profile in setup.candidates:
                key = profile.content_hash()
                try:
                    outcome: ValidationReport | Exception = setup.detector.detect_instruments(
                        view, profile
                    )
                except DetectorConfigurationError:
                    raise
                except Exception as error:  # the method failed on this book: evidence
                    outcome = error
                if isinstance(outcome, ValidationReport):
                    if outcome.validation_profile_hash != key:
                        raise ValueError(
                            f"{setup.detector.name}: report is not under {profile.ref}"
                        )
                    if any(g.gate_id == "G0.single_instrument_adapter" for g in outcome.gates):
                        raise DetectorConfigurationError(
                            f"{setup.detector.name}: the report took the single-instrument path"
                        )
                record = _book_record(setup, arm, seed, markets, seeds, outcome)
                if record.verdict is Verdict.PASS:
                    vault = vaults[key]
                    vault.unseal(
                        _family(arm.name, seed), approved_by=_UNSEALED_BY, at=vault.window.start
                    )
                    record = replace(record, sealed_oos_unsealed=True)
                runs[key].append(record)
    return GateCalibrationReport(
        inputs=setup.inputs_payload(),
        candidates=tuple(
            _multi_evidence(setup, profile, runs[profile.content_hash()])
            for profile in setup.candidates
        ),
    )


# ======================================================================================
# Entry point
# ======================================================================================


AnySetup = GateCalibrationSetup | MultiInstrumentCalibrationSetup


def write_gate_calibration(root: Path, setup: AnySetup) -> WrittenReport:
    """Run the harness and write the report under ``<root>/gate_calibration/<report_hash>.json``
    (a ``MultiInstrumentCalibrationSetup`` runs the multi-instrument mode)."""
    if isinstance(setup, MultiInstrumentCalibrationSetup):
        return write_gate_calibration_report(root, run_multi_instrument_calibration(setup))
    return write_gate_calibration_report(root, run_gate_calibration(setup))


def _load_setup(target: str) -> AnySetup:
    module_name, _, attr = target.partition(":")
    if not module_name or not attr:
        raise SystemExit("--setup must be 'package.module:factory'")
    factory = getattr(importlib.import_module(module_name), attr)
    setup = factory()
    if not isinstance(setup, GateCalibrationSetup | MultiInstrumentCalibrationSetup):
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

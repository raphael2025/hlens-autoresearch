"""Backtest validation: the ``BacktestValidator`` seam and its Phase 4 + Phase 8 implementation.

Phase 5 (ADR-0038) defined the narrow seam the strategy pipeline needs:

    BacktestValidator.validate(subject, spec, backtest) -> BacktestValidation

``BacktestValidation`` carries the ``ValidationReport`` for the subject, the ``ReasonCode`` to file
in the Failure Registry when the verdict is ``FAIL`` (``GateResult`` carries no reason code, so the
validator states it) and, optionally, the JSON-ready report view for later visualization.

``PipelineBacktestValidator`` (ADR-0041) is the implementation backed by ``research/validation``:

1. it re-runs the chosen parameter point through a ``TrialRunner`` (the strategy → risk → backtest
   path of ``pipeline.CandidateTrialRunner``); the re-run's ``result_hash`` against the given
   backtest's is G0 reproducibility;
2. adapter gates (G0): the backtest's cost model has the same rates as the bound ``CostModelSpec``
   (``G0.backtest_cost_model``), and the re-run trades exactly the validated instrument(s):
   ``G0.single_instrument_adapter`` for the default one-instrument setup (several traded
   instruments are ``INCONCLUSIVE`` there, unchanged), ``G0.instrument_scope`` for a setup that
   declares ``ValidatorSetup.instruments`` (see **Multi-instrument validation** below); when the
   setup declares the backtest's execution
   model (``ValidatorSetup.backtester`` / ``.execution``, implementation note, 2026-09-26), also
   ``G0.execution_model`` — the given ``backtest.provider_hash`` must be exactly the declared
   model's, refused (FAIL) otherwise; unset, no gate is added (byte-identical to before the note);
3. every non-flat target becomes an ``OutcomeEvent`` at its decision time; the bound
   ``OutcomeProvider`` labels it over the same bars (``next_bar_open`` entry = the backtester's);
   the sides are ``FixedSides`` built **only** from contract-checked ``TargetPosition`` rows (their
   inputs are signal kinds with ``available_time <= decision_time``), never from labels;
4. ``research.validation.run_validation`` runs G0 → G3 and then G4, whose input is built lazily
   (only when G0 – G3 did not fail): every point of the spec's declared ``param_search_space``,
   the Profile's delay stress and time-alignment offsets, one run per declared instrument, the
   caller's causal state labels and bar volumes, and the holding horizon of the CSCV purge: the
   larger of the bound label spec's ``horizon`` and the longest holding period of the re-run
   (a non-flat decision held until the next decision or the end of data; review fixes 2);
5. the report's verdict is ``derive_verdict`` of all gates; the failure reason comes from
   ``research.validation.reason_for_gate``.

Price-bar binding (backlog E5). The ``OutcomeRequest`` the validator builds carries
``ValidatorSetup.manifest_content_hash``. There are two paths, and the view always names the one
taken in ``extra["price_binding"]``:

- **dataset** (``ValidatorSetup.dataset_bars`` is a ``DatasetPriceBars``, produced only by
  ``infrastructure.bars.backtest_bars_from_dataset`` after it proved every bar against the
  persisted manifest): the adapter gate ``G0.manifest_binding`` checks that the setup's manifest
  hash **is** the wrapper's, that every bar of the re-run (hence of the backtest, via
  ``G0.reproducibility``, and of the labels, which are built from those bars) **is** one of the
  wrapper's proven bars, that the validated instrument has bars at all, and that no bar is
  available after the wrapper's ``price_cutoff``. Any mismatch is ``FAIL`` at G0: the labels would
  be about other data than the backtest, a broken binding like ``G0.bindings`` (07-validation §2:
  G0 fail → Failure Registry; Constitution C-P1: the reproduction tuple binds the data). It is
  filed as ``REJECTED`` / ``CONTRACT_VIOLATION`` by ``reason_for_gate`` (the ``G0.`` row). The
  validator does not re-prove the manifest itself (research holds no catalog handle); it proves
  that what it validates is exactly what the wrapper proved;
- **manifest pair** (backlog E1 follow-up; ``ValidatorSetup.manifest_pair`` is the chain's
  ``infrastructure.bars.ManifestPair`` from ``pair_manifests``): one chain has an interval
  manifest for its features and a point manifest for its prices. ``G0.manifest_binding`` then
  also checks that the pair's price hash **is** ``dataset_bars``' manifest hash, that the pair's
  feature hash **is** the manifest hash of every feature request behind the signals, and that the
  pair hash recomputes (``pair_hash_of``). The validator cannot see the feature manifest itself:
  ``SignalObservation`` and ``TrialRun`` carry no manifest hash, so the caller passes each feature
  request's ``manifest_content_hash`` in ``ValidatorSetup.feature_manifest_hashes`` (at least one
  when a pair is given) and they are compared. A pair without ``dataset_bars``, or feature hashes
  without a pair, is an inconsistent setup (nothing to check them against): ``FAIL``, never a
  silent skip. Like the bars, the pair is not re-proven here (no builder in research): a
  ``ManifestPair`` is a record of ``pair_manifests``' proof, and the validator proves that what it
  validates is exactly what that record names;
- **synthetic** (``dataset_bars=None``, no pair, no feature hashes): the manifest hash is a
  caller-given label (e.g. a synthetic market hash) and is **not verified**. No gate is added
  (an unverifiable binding is neither a PASS nor a reason to change the synthetic lab's
  verdicts); the view labels the path ``synthetic_unverified``. Such a report is never evidence
  about a Research Dataset.

G4 outside ``validate`` (backlog E4). ``robustness_input(spec, backtest)`` is the public builder of
exactly the input ``validate`` hands to G4 (it re-runs the chosen point and refuses a backtest the
re-run does not reproduce). ``robustness_diagnostic(spec, backtest)`` runs G4 on it **even after an
earlier FAIL**; its result is a ``RobustnessDiagnostic`` labelled ``diagnostic_report_only``: it is
never part of a ``ValidationReport``, never changes a verdict and never files a failure.

G5 (sealed OOS) is deliberately not part of ``validate``: the unsealing is a one-shot, budgeted
event (``SealedOosVault``) run separately. The backtest handed to ``validate`` must therefore cover
the research window only (a label reaching the sealed window fails ``G1.sealed_oos_excluded``).
**A G0 – G4 PASS from ``validate`` is never promotable without a G5 result**:
``BacktestValidation.promotion_blocked_reason`` (and the view's ``promotion`` block) is
``"sealed_oos_not_evaluated"`` for such a report; only a report that also passed G5 is eligible
for the lifecycle review (ADR-0006), which remains a separate, human-approved step.

Provenance limit (ADR-0041): ``G1.label_blind_sides`` cannot detect a ``FixedSides`` that was
pre-filled from outcome signs outside the pipeline (the sides would be identical under blinded
and real labels). The defence is provenance: this adapter builds its ``FixedSides`` only from
contract-checked ``TargetPosition`` rows, never from labels.

Execution model wiring (implementation note, 2026-09-26). Before this note, a candidate backtested
with an opt-in ``plugins.backtest.execution.ExecutionModel`` (ADR-0038 execution note: bar-volume
participation cap, square-root impact, funding) was validated by re-running it through whatever
``TrialRunner`` the caller wired into ``ValidatorSetup.trials`` — nothing checked that the
``TrialRunner``'s own backtester actually matched, so a mismatch surfaced only indirectly, as a
generic ``G0.reproducibility`` failure (``NOT_REPRODUCIBLE`` / ``FAILED``, C-P3) indistinguishable
from any other cause of a differing hash. ``ValidatorSetup.backtester`` / ``.execution`` (mutually
exclusive; both optional, default ``None``) now let a caller **declare** the execution model the
candidate was backtested with:

- ``G0.execution_model`` checks the given ``backtest.provider_hash`` against the declared model's
  descriptor hash directly (no re-run needed) — a mismatch is refused (FAIL → REJECTED /
  CONTRACT_VIOLATION), before any of G0 – G4 runs; ``robustness_input`` refuses the same mismatch
  before re-running the declared parameter grid;
- the G4 capacity check (``research.validation.robustness.capacity_check``) then reads its impact
  coefficient from the same declared model instead of only ``RobustnessParams.impact_coefficient``
  (``research.validation.g4._resolved_impact``): the model's value takes priority when given, and
  an explicit ``RobustnessParams.impact_coefficient`` that disagrees with it is never silently
  overridden — ``G4.capacity.impact_estimated`` becomes ``INCONCLUSIVE`` (metric
  ``impact_coefficient_mismatch``) instead, reporting both values. The model's coefficient is
  handed over as its own ``Decimal`` and compared exactly (review fixes 3, 2026-09-26): an
  explicit ``0.1`` agrees with ``Decimal("0.1")``; no ``float`` round trip decides a mismatch.

Neither field is set by any pre-existing caller (``bar_volume`` remains a separate, unrelated
field used for the causal state / capacity plumbing): the default path — plain ``BarBacktester()``,
no declared execution model — adds no new gate and is byte-identical, including the report hash.

Multi-instrument validation (Phase 8 implementation note, 2026-09-26; CODE_COMPLETE /
DEBUG_PENDING). No core / contract / Schema change. ``ValidatorSetup.instruments`` (default
``None``) names the exact set (at least two) of instruments a backtest trades; ``None`` keeps the
single-instrument path byte-identical (report hashes pinned in
``tests/research/strategies/test_multi_instrument_validation.py``). Given:

- ``G0.instrument_scope`` replaces ``G0.single_instrument_adapter``: PASS when the re-run trades
  exactly the validated instruments, else ``INCONCLUSIVE`` (``instruments_outside_scope``; an
  instrument outside the scope would have no labels) and nothing is labelled;
- ``G0.manifest_binding`` (dataset path) binds every instrument on its own:
  ``instrument_bars[<name>]``, ``bars_in_manifest[<name>]`` and ``price_cutoff[<name>]`` join the
  global checks, so one instrument's unbound bars fail G0 exactly as before (``REJECTED`` /
  ``CONTRACT_VIOLATION``);
- ``OutcomeRequest`` stays single-instrument: **one request per instrument**, each over that
  instrument's (verified) bars and bound to the same ``manifest_content_hash``, materialized on
  its own. Every label's ``event_key`` already names its instrument (``<instrument>|<time>``);
- ``research.validation.instruments`` pools the tables (refusing any label keyed to another
  instrument) and runs pooled G0 – G3 under the standard gate ids, then each instrument's own
  G0 – G3 recorded as ``<gate_id>.instrument.<name>`` (an instrument without labels:
  ``G0.data_available.instrument.<name>`` = ``INCONCLUSIVE``), then G4 only when nothing failed.
  The verdict is ``derive_verdict`` of all gates — any ``FAIL`` fails, else any ``INCONCLUSIVE``
  is ``INCONCLUSIVE``, and a PASS needs the pooled evidence and every instrument's to pass. The
  view adds ``extra["instruments"]`` and ``extra["per_instrument"]`` (verdict, label count);
- no trial is added: the per-instrument evidence re-uses the one re-run (no extra
  ``TrialRunner`` call), and the G3 adjustment of every stage uses the unchanged
  ``metadata.family_trial_count`` — an instrument is not a trial (C-T1);
- G4: every declared instrument's cross-asset returns are its own ``TrialRunner.run(...,
  instruments=(name,))`` run; the multi-instrument base run is never reused as one asset's
  returns (even when the declared scope names a single asset). The C-R2 state trades mark a
  decision time as exposed when any instrument's target is non-flat (the base returns are the
  portfolio's).

G4 cross-asset for cross-sectional strategies (ADR-0059, Accepted 2026-09-26; CODE_COMPLETE /
DEBUG_PENDING). No core / contract / Schema change. ``RobustnessInput.per_asset_exposed`` records,
for every declared instrument's single-asset re-run, whether it ever held a position
(``_exposed``: a non-flat executed target or a fill — decided from positions, never from returns);
when all are flat, ``G4.cross_asset.positive_fraction`` is ``INCONCLUSIVE``
(``not_applicable_zero_exposure_single_asset``, C). A strategy **declared** cross-sectional
(``research.strategies.cross_section``, by spec name; never inferred from results) is also re-run
once per sub-universe of ``robustness.subuniverse_partition(declared_instruments)`` (A), after the
per-asset runs; those are robustness re-runs of the chosen trial like the per-asset ones, so
``family_trial_count`` is unchanged. Every other strategy makes exactly the same ``TrialRunner``
calls as before and its report is byte-identical (hashes pinned in
``tests/research/strategies/test_cross_sectional_g4.py``).

C-T4 market benchmark and inverse control (ADR-0060, Accepted 2026-09-26; CODE_COMPLETE /
DEBUG_PENDING). No core / contract / Schema change. ``ValidatorSetup.market_benchmark`` (default
``False``) opts the validator in: ``True`` hands G2 a ``research.validation.benchmark`` source built
from the one re-run, so the report carries the items the Profile's
``benchmark.market_benchmark_rule`` / ``.inverse_control_reported`` call for
(``G2.market_benchmark.<rule>`` / ``G2.inverse_control``, reported only; an unregistered rule name —
e.g. the TEST ONLY placeholder ``"test-only"`` — is ``G2.market_benchmark`` = ``INCONCLUSIVE``).
The source first re-runs the chosen trial's own targets through the declared backtester (or a plain
``BarBacktester()`` when none is declared) and requires the re-run's exact ``result_hash``: if the
execution model cannot be reproduced, every requested item is ``INCONCLUSIVE``
(``benchmark_unavailable:execution_model_not_reproduced``), never computed under another model.
Then, through that same backtester, cost model, bars and initial equity:

- ``buy_and_hold_equal_weight``: one ``1 / N`` target per validated instrument (the pooled scope on
  the multi-instrument path; the one instrument otherwise) at the re-run's first decision time,
  held to the end of the data;
- the inverse control: every re-run target with its weight negated (flat stays flat).

These are backtests of the same trial, not ``TrialRunner`` calls: ``family_trial_count`` and the
runner's call sequence are unchanged. ``False`` (every pre-existing caller: the loop, the synthetic
lab, the e2e tests) adds nothing, so their reports are byte-identical (hashes pinned in
``tests/research/strategies/test_market_benchmark.py``).

Known limit (DEBUG_PENDING): the pooled G1 negative controls permute / circularly shift the
pooled label sequence, which interleaves instruments by time, so a control may pair one
instrument's side with another's label. That is still a valid null (it can only break
alignment), but its false-alarm rate on multi-instrument data is uncalibrated (Phase 9).
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from decimal import Decimal
from itertools import product
from typing import Protocol

from core.contracts.feature import ObservationScalar
from core.contracts.outcome import OutcomeEvent, OutcomePriceBar, OutcomeProvider, OutcomeRequest
from core.contracts.strategy import (
    BacktestCostModel,
    BacktestProvider,
    BacktestProviderError,
    BacktestRequest,
    BacktestResult,
    PriceBar,
    TargetPosition,
)
from core.domain.base import Ref
from core.domain.research import GateResult, ValidationReport, Verdict
from core.domain.specs import StrategySpec
from core.errors import ReasonCode
from infrastructure.bars.dataset import DatasetPriceBars
from infrastructure.bars.pair import ManifestPair, pair_hash_of
from plugins.backtest import BarBacktester, ExecutionModel
from research.outcomes.table import OutcomeTable, materialize
from research.strategies.cross_section import is_cross_sectional
from research.validation.benchmark import (
    BenchmarkEvidence,
    BenchmarkSource,
    MarketBenchmarkRule,
    Unavailable,
)
from research.validation.controls import FixedSides
from research.validation.g4 import (
    RobustnessInput,
    RobustnessParams,
    RobustnessResult,
    run_robustness,
    run_validation,
)
from research.validation.gates import flag_gate, inconclusive_gate
from research.validation.instruments import (
    EVENT_KEY_SEPARATOR,
    MultiInstrumentRun,
    pool_outcomes,
    run_multi_instrument_validation,
)
from research.validation.pipeline import (
    InSampleInput,
    ValidationContext,
    build_report,
    reason_for_gate,
)
from research.validation.report import promotion_blocked_reason, report_view
from research.validation.returns import (
    ParamPoint,
    PeriodReturns,
    TrialReturns,
    from_backtest,
    param_key,
)
from research.validation.robustness import (
    CapacityFill,
    StateTrade,
    SubUniverse,
    subuniverse_partition,
)

__all__ = [
    "BacktestValidation",
    "BacktestValidator",
    "DIAGNOSTIC_MODE",
    "PRICE_BINDING_DATASET",
    "PRICE_BINDING_SYNTHETIC",
    "PipelineBacktestValidator",
    "RobustnessDiagnostic",
    "TrialRun",
    "TrialRunner",
    "ValidatorSetup",
    "binding_mismatches",
]

Params = Mapping[str, ObservationScalar]
SpecScalar = str | int | float | bool

#: View label of the dataset path: the bars are a verified manifest's (``G0.manifest_binding``).
PRICE_BINDING_DATASET = "dataset_manifest_verified"
#: View label of the synthetic path: the manifest hash is an unverified label.
PRICE_BINDING_SYNTHETIC = "synthetic_unverified"
#: Label of a G4 run outside ``validate``: report only, never a verdict.
DIAGNOSTIC_MODE = "diagnostic_report_only"


@dataclass(frozen=True, slots=True)
class BacktestValidation:
    """A validator's answer: the report, and the failure reason when the verdict is ``FAIL``."""

    report: ValidationReport
    failure_reason: ReasonCode | None = None
    view: Mapping[str, object] | None = None

    def __post_init__(self) -> None:
        if (self.report.verdict is Verdict.FAIL) != (self.failure_reason is not None):
            raise ValueError("failure_reason is required exactly when the verdict is FAIL")

    @property
    def promotion_blocked_reason(self) -> str | None:
        """Why the report cannot support a promotion; a PASS without G5 is never promotable."""
        return promotion_blocked_reason(self.report)

    def check_subject(self, subject: Ref) -> None:
        if self.report.subject.target_identity() != subject.target_identity():
            raise ValueError(f"the report is about {self.report.subject}, not {subject}")


class BacktestValidator(Protocol):
    """Validate one strategy's backtest under the frozen Constitution + Validation Profile."""

    def validate(
        self, subject: Ref, spec: StrategySpec, backtest: BacktestResult
    ) -> BacktestValidation: ...


@dataclass(frozen=True, slots=True)
class TrialRun:
    """One simulated trial: its (constrained, possibly delayed) targets and backtest."""

    targets: tuple[TargetPosition, ...]
    backtest: BacktestResult
    bars: tuple[PriceBar, ...]
    cost_model: BacktestCostModel


class TrialRunner(Protocol):
    """Re-runs the candidate at a parameter point (``pipeline.CandidateTrialRunner``)."""

    def run(
        self,
        params: Params,
        *,
        delay_bars: int = 0,
        decision_offset: timedelta = timedelta(0),
        instruments: tuple[str, ...] | None = None,
    ) -> TrialRun: ...


@dataclass(frozen=True)
class ValidatorSetup:
    """Everything the adapter binds. No field has a default: absence must be explicit.

    - ``context``: the experiment binding (run, metadata, Profile, ``CostModelSpec``, label spec);
    - ``state_of``: a **causal** state label for a decision time (``None`` → C-R2 INCONCLUSIVE);
    - ``bar_volume``: traded quantity per ``(instrument, interval_start)`` (``None`` → C-R5
      INCONCLUSIVE);
    - ``declared_instruments``: the declared scope for C-R3 (each is run on its own);
    - ``dataset_bars``: the ``DatasetPriceBars`` the trials run on (dataset path, checked by
      ``G0.manifest_binding``), or ``None`` for the synthetic path, whose ``manifest_content_hash``
      is an unverified label (named ``synthetic_unverified`` in the view);
    - ``manifest_pair``: the chain's verified feature / price ``ManifestPair`` (backlog E1), or
      ``None``; given, ``G0.manifest_binding`` also checks it against ``dataset_bars`` and
      ``feature_manifest_hashes``;
    - ``feature_manifest_hashes``: the ``manifest_content_hash`` of each feature request whose
      values feed the signals (the validator cannot see them otherwise); compared with the
      pair's feature hash;
    - ``backtester`` / ``execution`` (implementation note, 2026-09-26): the execution model the
      candidate was actually backtested with — either the full ``BacktestProvider`` (any variant,
      e.g. a ``plugins.backtest.bar.BarBacktester(execution=...)``, or another provider entirely),
      or, for the common case, just the ``plugins.backtest.execution.ExecutionModel`` (the
      validator wraps it in a plain ``BarBacktester``). At most one of the two may be given.
      When either is given, the adapter gate ``G0.execution_model`` refuses (FAIL) a ``backtest``
      whose ``provider_hash`` does not match it, and ``robustness_input`` refuses the same
      mismatch before re-running the declared parameter grid; the G4 capacity check then reads its
      impact coefficient from the same model (see ``robustness.capacity_check``). Neither field is
      required to reproduce a plain ``BarBacktester()`` backtest (the pre-existing behaviour is
      untouched): they exist to make an *opt-in* execution model an explicit, checked part of the
      setup instead of an unstated assumption of whatever ``TrialRunner`` the caller wired in.
    - ``control_seeds`` (debugging pass, 2026-09-26): passed unchanged to
      ``InSampleInput.control_seeds`` — the optional multi-seed G1 negative controls
      (``research.validation.pipeline`` module docs). ``None`` keeps the single-seed controls.
    - ``instruments`` (multi-instrument validation, 2026-09-26): ``None`` (default) validates
      exactly ``instrument`` (the single-instrument path, byte-identical). Given, the exact set
      of at least two distinct instruments the backtest trades; ``instrument`` must be one of
      them (it stays the view's ``instrument``). See the module docs, **Multi-instrument
      validation**.
    - ``market_benchmark`` (ADR-0060, 2026-09-26): ``False`` (default) adds no C-T4 market
      benchmark / inverse-control item (byte-identical); ``True`` computes what the Profile's
      ``benchmark`` block calls for (module docs, **C-T4 market benchmark and inverse control**).

    Only the last eight fields have defaults (``None`` / empty): they keep the synthetic callers
    and the pre-existing (no execution model, single-seed controls, one instrument) callers
    unchanged, and the view always labels the path taken.
    """

    context: ValidationContext
    outcome_provider: OutcomeProvider
    manifest_content_hash: str
    instrument: str
    trials: TrialRunner
    chosen_params: ParamPoint
    seed: int
    robustness: RobustnessParams
    state_of: Callable[[datetime], str] | None
    bar_volume: Mapping[tuple[str, datetime], Decimal] | None
    declared_instruments: tuple[str, ...]
    dataset_bars: DatasetPriceBars | None = None
    manifest_pair: ManifestPair | None = None
    feature_manifest_hashes: tuple[str, ...] = ()
    backtester: BacktestProvider | None = None
    execution: ExecutionModel | None = None
    control_seeds: tuple[int, ...] | None = None
    instruments: tuple[str, ...] | None = None
    market_benchmark: bool = False

    def __post_init__(self) -> None:
        if self.backtester is not None and self.execution is not None:
            raise ValueError("ValidatorSetup takes either backtester or execution, not both")
        names = self.instruments
        if names is None:
            return
        if not isinstance(names, tuple) or len(names) < 2 or len(set(names)) != len(names):
            raise ValueError(
                "instruments must be None (one instrument) or a tuple of at least two distinct "
                "instruments"
            )
        if any(not name or EVENT_KEY_SEPARATOR in name for name in names):
            raise ValueError(
                f"an instrument name must be non-empty without {EVENT_KEY_SEPARATOR!r}"
            )
        if self.instrument not in names:
            raise ValueError(f"instrument {self.instrument!r} is not one of instruments {names}")

    @property
    def validated_instruments(self) -> tuple[str, ...]:
        """The instruments this setup validates (sorted when several are given)."""
        return (self.instrument,) if self.instruments is None else tuple(sorted(self.instruments))


def _binding_declared(setup: ValidatorSetup) -> bool:
    """Whether the setup takes the dataset path (anything to verify); else it is synthetic."""
    return (
        setup.dataset_bars is not None
        or setup.manifest_pair is not None
        or bool(setup.feature_manifest_hashes)
    )


def binding_mismatches(setup: ValidatorSetup, bars: Sequence[PriceBar]) -> list[str]:
    """The failed dataset-binding checks of ``bars`` under ``setup`` (empty = bound).

    Always empty on the synthetic path (no ``dataset_bars``, no ``manifest_pair``, no
    ``feature_manifest_hashes``): there is nothing to verify against, which the view labels
    instead. An inconsistent setup (a pair without bars, feature hashes without a pair) is a
    mismatch, never a skip.
    """
    proven, pair = setup.dataset_bars, setup.manifest_pair
    checks: dict[str, bool] = {}
    if proven is None:
        checks["pair_without_dataset_bars"] = pair is None
    else:
        allowed = {bar.content_hash() for bar in proven.bars}
        checks |= {
            "manifest_hash": setup.manifest_content_hash == proven.manifest_content_hash,
            "bars_in_manifest": all(bar.content_hash() in allowed for bar in bars),
            "price_cutoff": all(bar.available_time <= proven.price_cutoff for bar in bars),
        }
        if setup.instruments is None:
            checks["instrument_bars"] = any(bar.instrument == setup.instrument for bar in bars)
        else:  # multi-instrument: each validated instrument is bound on its own
            for name in setup.validated_instruments:
                mine = [bar for bar in bars if bar.instrument == name]
                checks |= {
                    f"instrument_bars[{name}]": bool(mine),
                    f"bars_in_manifest[{name}]": all(bar.content_hash() in allowed for bar in mine),
                    f"price_cutoff[{name}]": all(
                        bar.available_time <= proven.price_cutoff for bar in mine
                    ),
                }
    if pair is None:
        checks["feature_hashes_without_pair"] = not setup.feature_manifest_hashes
    else:
        features = setup.feature_manifest_hashes
        checks |= {
            "pair_hash": pair.pair_hash
            == pair_hash_of(pair.feature_manifest_hash, pair.price_manifest_hash),
            "pair_feature_manifest": bool(features)
            and all(item == pair.feature_manifest_hash for item in features),
        }
        if proven is not None:
            checks["pair_price_manifest"] = pair.price_manifest_hash == proven.manifest_content_hash
    return sorted(name for name, ok in checks.items() if not ok)


@dataclass(frozen=True, slots=True)
class RobustnessDiagnostic:
    """G4 run outside ``validate`` (e.g. after an earlier FAIL): **report only**.

    ``mode`` is always ``DIAGNOSTIC_MODE``. A diagnostic is never part of a ``ValidationReport``,
    never changes a verdict and never files a failure; ``backtest_result_hash`` names the backtest
    it is about.
    """

    result: RobustnessResult
    backtest_result_hash: str
    mode: str = DIAGNOSTIC_MODE

    def __post_init__(self) -> None:
        if self.mode != DIAGNOSTIC_MODE:
            raise ValueError(f"a robustness diagnostic is always {DIAGNOSTIC_MODE!r}")

    @property
    def gates(self) -> tuple[GateResult, ...]:
        return self.result.gates

    def to_dict(self) -> dict[str, object]:
        return {
            "mode": self.mode,
            "verdict_effect": "none",
            "backtest_result_hash": self.backtest_result_hash,
            "robustness": self.result.to_dict(),
        }


def _outcome_bars(bars: Sequence[PriceBar], instrument: str) -> tuple[OutcomePriceBar, ...]:
    return tuple(
        OutcomePriceBar(
            interval_start=bar.interval_start,
            interval_end=bar.interval_end,
            available_time=bar.available_time,
            open=bar.open,
            high=bar.high,
            low=bar.low,
            close=bar.close,
        )
        for bar in sorted(bars, key=lambda item: item.interval_start)
        if bar.instrument == instrument
    )


def _side(weight: Decimal) -> int:
    return (weight > 0) - (weight < 0)


def _event_key(target: TargetPosition) -> str:
    return f"{target.instrument}|{target.decision_time.isoformat()}"


def _exposed(run: TrialRun) -> bool:
    """Whether a re-run ever held a position (ADR-0059 C criterion): at least one non-flat target
    (after risk / delay: the targets the backtest executed) or at least one fill. Decided from the
    run's positions, never from its returns (a flat run and a zero-return run are different)."""
    return any(_side(t.target_weight) != 0 for t in run.targets) or bool(run.backtest.fills)


def _longest_holding(targets: Sequence[TargetPosition], base: PeriodReturns) -> timedelta:
    """Longest span a non-flat target is held: until its instrument's next decision (or the end
    of data), the same holding interval ``_state_trades`` uses."""
    end_of_data = base.times[-1] if base.times else None
    rows: dict[str, list[TargetPosition]] = {}
    for target in targets:
        rows.setdefault(target.instrument, []).append(target)
    longest = timedelta(0)
    for mine in rows.values():
        ordered = sorted(mine, key=lambda target: target.decision_time)
        for index, target in enumerate(ordered):
            if _side(target.target_weight) == 0:
                continue
            end = ordered[index + 1].decision_time if index + 1 < len(ordered) else end_of_data
            if end is not None and end > target.decision_time:
                longest = max(longest, end - target.decision_time)
    return longest


def _backtest(
    backtester: BacktestProvider, rerun: TrialRun, targets: tuple[TargetPosition, ...]
) -> BacktestResult:
    """``targets`` backtested exactly like ``rerun`` (same bars, cost model, initial equity)."""
    request = BacktestRequest(
        cost_model=rerun.cost_model,
        initial_equity=rerun.backtest.initial_equity,
        bars=rerun.bars,
        targets=targets,
    )
    result = backtester.run(request)
    result.check_answers(request, backtester.descriptor)
    return result


def _returns_of(
    backtester: BacktestProvider, rerun: TrialRun, targets: tuple[TargetPosition, ...]
) -> PeriodReturns | Unavailable:
    """The period returns of ``targets`` under ``rerun``'s model; a run that cannot be simulated
    (or whose equity is not positive) is ``Unavailable``, never a number."""
    try:
        return from_backtest(_backtest(backtester, rerun, targets))
    except (BacktestProviderError, ValueError) as exc:
        return Unavailable(f"rerun_failed:{type(exc).__name__}")


def _buy_and_hold(
    rule: MarketBenchmarkRule, rerun: TrialRun, instruments: Sequence[str]
) -> tuple[TargetPosition, ...]:
    """ADR-0060 ``buy_and_hold_equal_weight``: the rule's weights, entered at the re-run's first
    decision time and never changed. The rule itself is the target's one input (no market data;
    ``latest_input_available_time`` is the decision time, so nothing is looked ahead)."""
    start = min(target.decision_time for target in rerun.targets)
    return tuple(
        TargetPosition(
            decision_time=start,
            instrument=name,
            target_weight=weight,
            inputs_used=1,
            latest_input_available_time=start,
        )
        for name, weight in sorted(rule.weights(instruments).items())
    )


def _negated(targets: Sequence[TargetPosition]) -> tuple[TargetPosition, ...]:
    """Every target with its weight negated (the ADR-0060 inverse control); flat stays flat."""
    return tuple(
        target
        if target.target_weight == 0
        else TargetPosition.model_validate(
            {**target.model_dump(), "target_weight": -target.target_weight}
        )
        for target in targets
    )


def _full(spec: StrategySpec, point: ParamPoint) -> dict[str, SpecScalar]:
    """The identity of a trial: spec defaults overridden by the point."""
    return {**dict(spec.params), **dict(point)}


def _grid(spec: StrategySpec) -> list[dict[str, SpecScalar]]:
    """Every point of the declared ``param_search_space`` (the family's trials, C-T1)."""
    space = spec.param_search_space
    names = sorted(space)
    return [
        _full(spec, dict(zip(names, values, strict=True)))
        for values in product(*(space[name] for name in names))
    ]


def _request(spec: StrategySpec, point: ParamPoint) -> dict[str, ObservationScalar]:
    """The request params of a point: declared keys only; floats cannot be requested."""
    out: dict[str, ObservationScalar] = {}
    for name, value in point.items():
        if name not in spec.param_search_space:
            continue
        if isinstance(value, float):
            raise ValueError(f"{spec.ref}: float parameter {name}={value!r} cannot be requested")
        out[name] = value
    return out


class PipelineBacktestValidator:
    """``BacktestValidator`` backed by ``research/validation`` (see module docs)."""

    def __init__(self, setup: ValidatorSetup) -> None:
        self._setup = setup

    def validate(
        self, subject: Ref, spec: StrategySpec, backtest: BacktestResult
    ) -> BacktestValidation:
        setup, ctx = self._setup, self._setup.context
        if ctx.subject.target_identity() != subject.target_identity():
            raise ValueError(f"the validation context is about {ctx.subject}, not {subject}")
        if spec.ref.target_identity() != subject.target_identity():
            raise ValueError(f"the spec {spec.ref} is not the subject {subject}")
        chosen = _full(spec, setup.chosen_params)
        rerun = setup.trials.run(_request(spec, chosen))
        adapter = self._adapter_gates(rerun, backtest)
        if any(gate.verdict is not Verdict.PASS for gate in adapter):
            return self._answer(build_report(ctx, adapter), None, rerun)
        traded = [t for t in rerun.targets if _side(t.target_weight) != 0]
        if not traded:
            gate = inconclusive_gate("G0.data_available", "non_flat_targets", 0.0)
            return self._answer(build_report(ctx, (*adapter, gate)), None, rerun)
        if setup.instruments is not None:
            return self._validate_many(spec, chosen, rerun, backtest, adapter, traded)
        bars = _outcome_bars(rerun.bars, setup.instrument)
        request = OutcomeRequest(
            label_spec=ctx.label_spec,
            manifest_content_hash=setup.manifest_content_hash,
            price_cutoff=max(bar.available_time for bar in bars),
            events=tuple(
                OutcomeEvent(event_key=_event_key(t), event_time=t.decision_time) for t in traded
            ),
            bars=bars,
        )
        table = materialize(setup.outcome_provider, request)
        study = FixedSides(
            refs=tuple(spec.signals),
            by_event={_event_key(t): _side(t.target_weight) for t in traded},
        )
        in_sample = InSampleInput(
            context=ctx,
            outcomes=table,
            study=study,
            seed=setup.seed,
            reproduce=lambda: rerun.backtest.result_hash,
            recorded_result_hash=backtest.result_hash,
            control_seeds=setup.control_seeds,
            benchmark=self._benchmark_source(rerun),
        )
        run = run_validation(in_sample, lambda: self._robustness_input(spec, chosen, rerun))
        report = build_report(ctx, (*adapter, *run.gates))
        return self._answer(report, run.robustness, rerun)

    def robustness_input(self, spec: StrategySpec, backtest: BacktestResult) -> RobustnessInput:
        """The G4 input ``validate`` would build for ``backtest`` under this setup (backlog E4).

        Re-runs the setup's chosen point; a backtest the re-run does not reproduce is refused
        (``ValueError``): the input would describe another backtest. A ``backtest`` whose
        ``provider_hash`` does not match the setup's declared ``backtester`` / ``execution``
        (implementation note, 2026-09-26) is refused the same way, before the (possibly expensive)
        re-run of the declared parameter grid.
        """
        subject = self._setup.context.subject
        if spec.ref.target_identity() != subject.target_identity():
            raise ValueError(f"the validation context is about {subject}, not {spec.ref}")
        declared = self._declared_backtester()
        if declared is not None and backtest.provider_hash != declared.descriptor.content_hash():
            raise ValueError(
                "the backtest's execution model does not match the validator's backtester: "
                "no G4 input"
            )
        chosen = _full(spec, self._setup.chosen_params)
        rerun = self._setup.trials.run(_request(spec, chosen))
        if rerun.backtest.result_hash != backtest.result_hash:
            raise ValueError("the chosen parameters do not reproduce this backtest: no G4 input")
        return self._robustness_input(spec, chosen, rerun)

    def robustness_diagnostic(
        self, spec: StrategySpec, backtest: BacktestResult
    ) -> RobustnessDiagnostic:
        """G4 on ``robustness_input`` whatever G0 – G3 said; ``diagnostic_report_only``."""
        return RobustnessDiagnostic(
            result=run_robustness(self.robustness_input(spec, backtest)),
            backtest_result_hash=backtest.result_hash,
        )

    # ----------------------------------------------------------------------------------

    def _declared_backtester(self) -> BacktestProvider | None:
        """The setup's declared execution model as a full provider, or ``None`` (implementation
        note, 2026-09-26): unset by every pre-existing caller, so ``_execution_model_gate`` adds
        no gate for them and the report stays byte-identical."""
        setup = self._setup
        if setup.backtester is not None:
            return setup.backtester
        if setup.execution is not None:
            return BarBacktester(execution=setup.execution)
        return None

    def _declared_execution(self) -> ExecutionModel | None:
        """The declared ``ExecutionModel``, if any (directly, or via a ``BarBacktester``); the G4
        capacity check's impact coefficient is read from it (implementation note, 2026-09-26)."""
        setup = self._setup
        if setup.execution is not None:
            return setup.execution
        backtester = setup.backtester
        return backtester.execution if isinstance(backtester, BarBacktester) else None

    def _execution_model_gate(self, backtest: BacktestResult) -> GateResult | None:
        """``G0.execution_model`` (implementation note, 2026-09-26): ``None`` when the setup
        declares no ``backtester`` / ``execution`` (the pre-existing, byte-identical path);
        otherwise a structural check that ``backtest.provider_hash`` is exactly the declared
        model's — a mismatch is refused (FAIL → REJECTED / CONTRACT_VIOLATION), never silently
        trusted just because ``G0.reproducibility`` would eventually catch it too."""
        declared = self._declared_backtester()
        if declared is None:
            return None
        expected = declared.descriptor.content_hash()
        match = backtest.provider_hash == expected
        return flag_gate("G0.execution_model", "provider_hash_equal", match, 1.0 if match else 0.0)

    def _adapter_gates(self, rerun: TrialRun, backtest: BacktestResult) -> tuple[GateResult, ...]:
        spec = self._setup.context.cost_model
        same = (rerun.cost_model.fee_rate, rerun.cost_model.slippage_rate) == (
            spec.fee_rate_per_side,
            spec.slippage_rate_per_side,
        )
        instruments = {target.instrument for target in rerun.targets}
        gates = [
            flag_gate("G0.backtest_cost_model", "cost_rates_equal", same, float(same)),
            self._instrument_gate(instruments),
        ]
        execution_gate = self._execution_model_gate(backtest)
        if execution_gate is not None:
            gates.append(execution_gate)
        if not _binding_declared(self._setup):  # synthetic path: labelled in the view, no gate
            return tuple(gates)
        mismatches = binding_mismatches(self._setup, rerun.bars)
        binding = flag_gate(
            "G0.manifest_binding",
            "manifest_binding_mismatch_count",
            not mismatches,
            float(len(mismatches)),
        )
        gates.append(binding)
        return tuple(gates)

    def _instrument_gate(self, traded: set[str]) -> GateResult:
        """``G0.single_instrument_adapter`` (one instrument, unchanged) or, for a multi-instrument
        setup, ``G0.instrument_scope``: the re-run trades exactly the validated instruments
        (``INCONCLUSIVE`` otherwise — an instrument outside the scope has no labels)."""
        setup = self._setup
        if setup.instruments is None:
            if traded == {setup.instrument}:
                return flag_gate("G0.single_instrument_adapter", "instruments", True, 1.0)
            return inconclusive_gate(
                "G0.single_instrument_adapter", "instruments", float(len(traded))
            )
        scope = set(setup.validated_instruments)
        if traded == scope:
            return flag_gate("G0.instrument_scope", "instruments", True, float(len(scope)))
        return inconclusive_gate(
            "G0.instrument_scope", "instruments_outside_scope", float(len(traded ^ scope))
        )

    def _validate_many(
        self,
        spec: StrategySpec,
        chosen: ParamPoint,
        rerun: TrialRun,
        backtest: BacktestResult,
        adapter: tuple[GateResult, ...],
        traded: Sequence[TargetPosition],
    ) -> BacktestValidation:
        """Multi-instrument path (module docs): one ``OutcomeRequest`` per instrument, pooled +
        per-instrument G0 – G3 (``research.validation.instruments``), then G4."""
        setup, ctx = self._setup, self._setup.context
        tables: dict[str, OutcomeTable] = {}
        sides: dict[str, dict[str, int]] = {}
        for name in setup.validated_instruments:
            mine = [t for t in traded if t.instrument == name]
            bars = _outcome_bars(rerun.bars, name)
            if not mine or not bars:  # no labels: INCONCLUSIVE evidence for this instrument
                continue
            tables[name] = materialize(setup.outcome_provider, self._request(bars, mine))
            sides[name] = {_event_key(t): _side(t.target_weight) for t in mine}
        if not tables:
            gate = inconclusive_gate("G0.data_available", "computable_labels", 0.0)
            return self._answer(build_report(ctx, (*adapter, gate)), None, rerun)
        refs = tuple(spec.signals)
        pooled = InSampleInput(
            context=ctx,
            outcomes=pool_outcomes(tables),
            study=FixedSides(
                refs=refs, by_event={key: side for m in sides.values() for key, side in m.items()}
            ),
            seed=setup.seed,
            reproduce=lambda: rerun.backtest.result_hash,
            recorded_result_hash=backtest.result_hash,
            control_seeds=setup.control_seeds,
            benchmark=self._benchmark_source(rerun),
        )
        per_instrument = {  # the ADR-0060 benchmark is pooled only
            name: replace(
                pooled,
                outcomes=table,
                study=FixedSides(refs=refs, by_event=sides[name]),
                benchmark=None,
            )
            for name, table in tables.items()
        }
        run = run_multi_instrument_validation(
            pooled,
            per_instrument,
            setup.validated_instruments,
            lambda: self._robustness_input(spec, chosen, rerun),
        )
        report = build_report(ctx, (*adapter, *run.gates))
        return self._answer(report, run.robustness, rerun, run)

    def _request(
        self, bars: tuple[OutcomePriceBar, ...], traded: Sequence[TargetPosition]
    ) -> OutcomeRequest:
        """One instrument's ``OutcomeRequest`` (the single-instrument path builds the same)."""
        setup = self._setup
        return OutcomeRequest(
            label_spec=setup.context.label_spec,
            manifest_content_hash=setup.manifest_content_hash,
            price_cutoff=max(bar.available_time for bar in bars),
            events=tuple(
                OutcomeEvent(event_key=_event_key(t), event_time=t.decision_time) for t in traded
            ),
            bars=bars,
        )

    def _answer(
        self,
        report: ValidationReport,
        robustness: RobustnessResult | None,
        rerun: TrialRun,
        multi: MultiInstrumentRun | None = None,
    ) -> BacktestValidation:
        reason = None
        if report.verdict is Verdict.FAIL:
            failed = next(gate for gate in report.gates if gate.verdict is Verdict.FAIL)
            reason = reason_for_gate(failed.gate_id)[1]
        extra: dict[str, object] = {
            "adapter": "research.strategies.validation.PipelineBacktestValidator",
            "instrument": self._setup.instrument,
            "backtest_result_hash": rerun.backtest.result_hash,
            "price_binding": self._price_binding(rerun),
        }
        if self._setup.instruments is not None:  # absent on the single-instrument path
            extra["instruments"] = list(self._setup.validated_instruments)
            extra["per_instrument"] = None if multi is None else multi.view()
        view = report_view(report, robustness, extra=extra)
        return BacktestValidation(report=report, failure_reason=reason, view=view)

    def _price_binding(self, rerun: TrialRun) -> dict[str, object]:
        setup, proven, pair = self._setup, self._setup.dataset_bars, self._setup.manifest_pair
        if not _binding_declared(setup):
            return {
                "mode": PRICE_BINDING_SYNTHETIC,
                "manifest_content_hash": setup.manifest_content_hash,
                "verified": False,
            }
        mismatches = binding_mismatches(setup, rerun.bars)
        view: dict[str, object] = {
            "mode": PRICE_BINDING_DATASET,
            "manifest_content_hash": setup.manifest_content_hash
            if proven is None
            else proven.manifest_content_hash,
            "price_cutoff": None if proven is None else proven.price_cutoff.isoformat(),
            "verified": not mismatches,
            "mismatches": mismatches,
        }
        if pair is not None or setup.feature_manifest_hashes:
            view["manifest_pair"] = (
                None
                if pair is None
                else {
                    "feature_manifest_hash": pair.feature_manifest_hash,
                    "price_manifest_hash": pair.price_manifest_hash,
                    "pair_hash": pair.pair_hash,
                }
            )
            view["feature_manifest_hashes"] = list(setup.feature_manifest_hashes)
        return view

    def _robustness_input(
        self, spec: StrategySpec, chosen: ParamPoint, rerun: TrialRun
    ) -> RobustnessInput:
        setup, profile = self._setup, self._setup.context.profile
        runner, params = setup.trials, _request(spec, chosen)
        base = from_backtest(rerun.backtest)
        trials: list[TrialReturns] = []
        for point in _grid(spec):
            if param_key(point) == param_key(chosen):
                trials.append(TrialReturns(params=point, returns=base))
            else:
                run = runner.run(_request(spec, point))
                trials.append(TrialReturns(params=point, returns=from_backtest(run.backtest)))
        delay = profile.cost_stress.delay_stress_bars
        delayed = from_backtest(runner.run(params, delay_bars=delay).backtest) if delay else None
        shifted = {
            offset: from_backtest(runner.run(params, decision_offset=offset).backtest)
            for offset in profile.parameter_stability.time_alignment_offsets
        }
        # A multi-instrument base run is never one asset's: every declared asset is re-run alone.
        single = setup.instruments is None and setup.declared_instruments == (setup.instrument,)
        alone = {
            name: rerun if single else runner.run(params, instruments=(name,))
            for name in setup.declared_instruments
        }
        per_asset = {
            name: base if single else from_backtest(run.backtest) for name, run in alone.items()
        }
        # ADR-0059 A: a strategy *declared* cross-sectional (never inferred from these runs) is
        # also re-run on each disjoint sub-universe of its declared instruments.
        sub_universes = None
        if is_cross_sectional(spec):
            sub_universes = tuple(
                SubUniverse(
                    instruments=names,
                    returns=from_backtest(run.backtest),
                    exposed=_exposed(run),
                )
                for names in subuniverse_partition(setup.declared_instruments)
                for run in (runner.run(params, instruments=names),)
            )
        return RobustnessInput(
            profile=profile,
            family_trial_count=setup.context.metadata.family_trial_count,
            param_space={name: tuple(values) for name, values in spec.param_search_space.items()},
            chosen=chosen,
            trials=tuple(trials),
            delayed=delayed,
            time_shifted=shifted,
            state_trades=self._state_trades(rerun, base),
            capacity_fills=self._capacity_fills(rerun),
            per_asset=per_asset,
            declared_instruments=setup.declared_instruments,
            params=setup.robustness,
            holding_horizon=max(
                setup.context.label_spec.horizon, _longest_holding(rerun.targets, base)
            ),
            execution_impact_coefficient=self._execution_impact_coefficient(),
            per_asset_exposed={name: _exposed(run) for name, run in alone.items()},
            sub_universes=sub_universes,
        )

    def _benchmark_source(self, rerun: TrialRun) -> BenchmarkSource | None:
        """The ADR-0060 evidence source of ``rerun`` (module docs), or ``None`` when the setup
        does not opt in (``market_benchmark=False``: no item, byte-identical)."""
        if not self._setup.market_benchmark:
            return None
        backtester = self._declared_backtester() or BarBacktester()
        instruments = self._setup.validated_instruments

        def source(rule: MarketBenchmarkRule | None, inverse: bool) -> BenchmarkEvidence:
            strategy = from_backtest(rerun.backtest)
            if rule is None and not inverse:  # ``flat``: built from the strategy's own grid
                return BenchmarkEvidence(strategy=strategy)
            try:
                same = _backtest(backtester, rerun, rerun.targets).result_hash
            except (BacktestProviderError, ValueError):
                same = None
            if same != rerun.backtest.result_hash:  # never another execution model
                missing = Unavailable("execution_model_not_reproduced")
                return BenchmarkEvidence(
                    strategy=strategy,
                    benchmark=None if rule is None else missing,
                    inverse=missing if inverse else None,
                )
            return BenchmarkEvidence(
                strategy=strategy,
                benchmark=None
                if rule is None
                else _returns_of(backtester, rerun, _buy_and_hold(rule, rerun, instruments)),
                inverse=_returns_of(backtester, rerun, _negated(rerun.targets))
                if inverse
                else None,
            )

        return source

    def _execution_impact_coefficient(self) -> Decimal | None:
        """The declared execution model's impact coefficient, if any (implementation note,
        2026-09-26): ``None`` unless the setup declares ``backtester`` / ``execution`` with one.
        Passed as the model's own ``Decimal`` so G4 compares it exactly with an explicit
        ``RobustnessParams.impact_coefficient`` (review fixes 3; ``research.validation.g4``)."""
        execution = self._declared_execution()
        if execution is None or execution.impact_coefficient is None:
            return None
        return Decimal(execution.impact_coefficient)

    def _state_trades(self, rerun: TrialRun, base: PeriodReturns) -> tuple[StateTrade, ...] | None:
        state_of = self._setup.state_of
        if state_of is None:
            return None
        decisions = sorted({t.decision_time for t in rerun.targets})
        if self._setup.instruments is None:
            sides = {t.decision_time: _side(t.target_weight) for t in rerun.targets}
        else:  # several instruments per decision: the portfolio is exposed if any one is
            active = {t.decision_time for t in rerun.targets if _side(t.target_weight) != 0}
            sides = {time: int(time in active) for time in decisions}
        end_of_data = base.times[-1] if base.times else None
        trades: list[StateTrade] = []
        for index, start in enumerate(decisions):
            if sides[start] == 0 or end_of_data is None:
                continue
            end = decisions[index + 1] if index + 1 < len(decisions) else end_of_data
            if end <= start:
                continue
            part = base.window(start + timedelta(microseconds=1), end + timedelta(microseconds=1))
            trades.append(StateTrade(state_of(start), start, end, sum(part.net(), Decimal(0))))
        return tuple(trades)

    def _capacity_fills(self, rerun: TrialRun) -> tuple[CapacityFill, ...] | None:
        volumes = self._setup.bar_volume
        if volumes is None:
            return None
        curve = rerun.backtest.equity_curve
        out: list[CapacityFill] = []
        for fill in rerun.backtest.fills:
            before = [p.equity for p in curve if p.time <= fill.fill_time]
            equity = before[-1] if before else rerun.backtest.initial_equity
            volume = volumes.get((fill.instrument, fill.fill_time))
            out.append(
                CapacityFill(
                    time=fill.fill_time,
                    traded_fraction=abs(fill.quantity * fill.fill_price) / equity,
                    bar_volume_notional=None if volume is None else volume * fill.reference_price,
                )
            )
        return tuple(out)

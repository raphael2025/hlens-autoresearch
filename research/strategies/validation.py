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
   (``G0.backtest_cost_model``), and the adapter validates exactly one instrument
   (``G0.single_instrument_adapter``; several instruments are ``INCONCLUSIVE`` — the Outcome
   request is single-instrument, a known gap); when the setup declares the backtest's execution
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
  ``impact_coefficient_mismatch``) instead, reporting both values.

Neither field is set by any pre-existing caller (``bar_volume`` remains a separate, unrelated
field used for the causal state / capacity plumbing): the default path — plain ``BarBacktester()``,
no declared execution model — adds no new gate and is byte-identical, including the report hash.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from itertools import product
from typing import Protocol

from core.contracts.feature import ObservationScalar
from core.contracts.outcome import OutcomeEvent, OutcomePriceBar, OutcomeProvider, OutcomeRequest
from core.contracts.strategy import (
    BacktestCostModel,
    BacktestProvider,
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
from research.outcomes.table import materialize
from research.validation.controls import FixedSides
from research.validation.g4 import (
    RobustnessInput,
    RobustnessParams,
    RobustnessResult,
    run_robustness,
    run_validation,
)
from research.validation.gates import flag_gate, inconclusive_gate
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
from research.validation.robustness import CapacityFill, StateTrade

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

    Only the last five fields have defaults (``None`` / empty): they keep the synthetic callers
    and the pre-existing (no execution model) callers unchanged, and the view always labels the
    path taken.
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

    def __post_init__(self) -> None:
        if self.backtester is not None and self.execution is not None:
            raise ValueError("ValidatorSetup takes either backtester or execution, not both")


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
            "instrument_bars": any(bar.instrument == setup.instrument for bar in bars),
            "price_cutoff": all(bar.available_time <= proven.price_cutoff for bar in bars),
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
        single = instruments == {self._setup.instrument}
        gates = [
            flag_gate("G0.backtest_cost_model", "cost_rates_equal", same, float(same)),
            flag_gate("G0.single_instrument_adapter", "instruments", True, 1.0)
            if single
            else inconclusive_gate(
                "G0.single_instrument_adapter", "instruments", float(len(instruments))
            ),
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

    def _answer(
        self, report: ValidationReport, robustness: RobustnessResult | None, rerun: TrialRun
    ) -> BacktestValidation:
        reason = None
        if report.verdict is Verdict.FAIL:
            failed = next(gate for gate in report.gates if gate.verdict is Verdict.FAIL)
            reason = reason_for_gate(failed.gate_id)[1]
        view = report_view(
            report,
            robustness,
            extra={
                "adapter": "research.strategies.validation.PipelineBacktestValidator",
                "instrument": self._setup.instrument,
                "backtest_result_hash": rerun.backtest.result_hash,
                "price_binding": self._price_binding(rerun),
            },
        )
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
        single = setup.declared_instruments == (setup.instrument,)
        per_asset = {
            name: base
            if single
            else from_backtest(runner.run(params, instruments=(name,)).backtest)
            for name in setup.declared_instruments
        }
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
        )

    def _execution_impact_coefficient(self) -> float | None:
        """The declared execution model's impact coefficient, if any (implementation note,
        2026-09-26): ``None`` unless the setup declares ``backtester`` / ``execution`` with one."""
        execution = self._declared_execution()
        if execution is None or execution.impact_coefficient is None:
            return None
        return float(execution.impact_coefficient)

    def _state_trades(self, rerun: TrialRun, base: PeriodReturns) -> tuple[StateTrade, ...] | None:
        state_of = self._setup.state_of
        if state_of is None:
            return None
        decisions = sorted({t.decision_time for t in rerun.targets})
        sides = {t.decision_time: _side(t.target_weight) for t in rerun.targets}
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

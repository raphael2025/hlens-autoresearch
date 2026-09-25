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
   request is single-instrument, a known gap);
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
from core.contracts.strategy import BacktestCostModel, BacktestResult, PriceBar, TargetPosition
from core.domain.base import Ref
from core.domain.research import GateResult, ValidationReport, Verdict
from core.domain.specs import StrategySpec
from core.errors import ReasonCode
from research.outcomes.table import materialize
from research.validation.controls import FixedSides
from research.validation.g4 import (
    RobustnessInput,
    RobustnessParams,
    RobustnessResult,
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
    "PipelineBacktestValidator",
    "TrialRun",
    "TrialRunner",
    "ValidatorSetup",
]

Params = Mapping[str, ObservationScalar]
SpecScalar = str | int | float | bool


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
    - ``declared_instruments``: the declared scope for C-R3 (each is run on its own).
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
        adapter = self._adapter_gates(rerun)
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
        run = run_validation(in_sample, lambda: self._robustness(spec, chosen, rerun))
        report = build_report(ctx, (*adapter, *run.gates))
        return self._answer(report, run.robustness, rerun)

    # ----------------------------------------------------------------------------------

    def _adapter_gates(self, rerun: TrialRun) -> tuple[GateResult, ...]:
        spec = self._setup.context.cost_model
        same = (rerun.cost_model.fee_rate, rerun.cost_model.slippage_rate) == (
            spec.fee_rate_per_side,
            spec.slippage_rate_per_side,
        )
        instruments = {target.instrument for target in rerun.targets}
        single = instruments == {self._setup.instrument}
        return (
            flag_gate("G0.backtest_cost_model", "cost_rates_equal", same, float(same)),
            flag_gate("G0.single_instrument_adapter", "instruments", True, 1.0)
            if single
            else inconclusive_gate(
                "G0.single_instrument_adapter", "instruments", float(len(instruments))
            ),
        )

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
            },
        )
        return BacktestValidation(report=report, failure_reason=reason, view=view)

    def _robustness(
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
        )

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

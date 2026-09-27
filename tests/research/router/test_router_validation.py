"""P10 router self-validation: a router's net paper result through ``PipelineBacktestValidator``.

!!! TEST ONLY !!!  The Profile, cost model and robustness parameters are the deliberately lax,
**uncalibrated** TEST ONLY fixtures of ``tests/research/synthetic_lab/gate_fixtures.py``. These
tests assert the report's structure and bindings (subject, router spec hash, G0 reproducibility,
trial counting), never a verdict.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime
from decimal import Decimal

import pytest

from core.contracts.state import (
    StateInput,
    StateProviderDescriptor,
    StateRequest,
    StateResult,
    StateValue,
)
from core.contracts.strategy import (
    PriceBar,
    StrategyProviderDescriptor,
    StrategyRequest,
    StrategyResult,
    TargetPosition,
)
from core.contracts.synthetic import SyntheticMarket
from core.domain.base import FrozenMapping, Kind, Ref, content_hash
from core.domain.research import RunState, Verdict
from core.domain.specs import StrategySpec
from core.lifecycle.strategy import LifecycleState
from plugins.backtest import BarBacktester
from plugins.outcomes import ForwardReturnOutcome
from plugins.synthetic import RandomWalkMarket
from research.router.paper import ROUTER_PAPER_BACKTEST, RouterPaperRun
from research.router.router import RouterError, RouterSpec, StrategyRouter
from research.router.validation import (
    ROUTER_SPEC_HASH_PARAM,
    ROUTER_TRIALS_PER_SPEC,
    RouterTrialRunner,
    RouterValidation,
    router_strategy_spec,
    validate_router,
)
from research.strategies.validation import PipelineBacktestValidator, TrialRun, ValidatorSetup
from research.validation import ValidationContext
from tests import factories
from tests.research.synthetic_lab import gate_fixtures as lax

HOUR, MINUTE, SYMBOL = lax.HOUR, lax.MINUTE, lax.SYMBOL
A = Ref(kind=Kind.STRATEGY, name="hour_momentum", version="1.0.0")
B = Ref(kind=Kind.STRATEGY, name="hour_reversal", version="1.0.0")
STATE = Ref(kind=Kind.STATE, name="session_half", version="1.0.0")
LIFECYCLE = {A: LifecycleState.ACTIVE, B: LifecycleState.ACTIVE}


def _state_of(t: datetime) -> str:
    return "am" if t.hour < 12 else "pm"


def _spec(rate: str = "0.0001") -> RouterSpec:
    return RouterSpec(
        name="session_router",
        version="1.0.0",
        table={"am": {str(A): Decimal(1)}, "pm": {str(B): Decimal("0.5")}},
        fallback={},
        switching_cost_rate=Decimal(rate),
    )


def _market() -> SyntheticMarket:
    return RandomWalkMarket().generate(lax.BASE_SPEC.model_copy(update={"seed": 3}))


def _decisions() -> tuple[datetime, ...]:
    out: list[datetime] = []
    t = lax.T0 + 61 * MINUTE
    while t + HOUR < lax.BOUNDARY:  # every label ends before the sealed window
        out.append(t)
        t += HOUR
    return tuple(out)


def _close_at(bars: tuple[PriceBar, ...], t: datetime) -> Decimal:
    return max((b for b in bars if b.available_time <= t), key=lambda b: b.available_time).close


def _strategy(ref: Ref, bars: tuple[PriceBar, ...], sign: int) -> StrategyResult:
    """Hourly momentum (``sign=1``) or reversal (``-1``) from closes known at the decision."""
    times = _decisions()
    request = StrategyRequest(
        strategy=ref,
        spec_hash=content_hash({"fixture": ref.name}),
        instruments=(SYMBOL,),
        knowledge_cutoff=lax.BOUNDARY,
        decision_times=times,
        signals=(),
    )
    descriptor = StrategyProviderDescriptor(
        name="router_validation_fixtures",
        version="1.0.0",
        deterministic=True,
        supported_strategies=FrozenMapping({str(ref): request.spec_hash}),
    )
    positions = []
    for t in times:
        move = _close_at(bars, t) - _close_at(bars, t - HOUR)
        side = (move > 0) - (move < 0)
        positions.append(
            TargetPosition(
                decision_time=t,
                instrument=SYMBOL,
                target_weight=Decimal(sign * side),
                inputs_used=1,
                latest_input_available_time=t,
            )
        )
    return StrategyResult.build(request, descriptor, positions)


def _states() -> StateResult:
    feature = Ref(kind=Kind.FEATURE, name="clock_hour", version="1.0.0")
    times = _decisions()
    request = StateRequest(
        state=STATE,
        spec_hash=content_hash({"fixture": "session_half"}),
        evaluation_times=times,
        inputs=tuple(
            StateInput(
                feature=feature,
                evaluation_time=t,
                value=Decimal(t.hour),
                source_result_hash=content_hash({"fixture": "clock"}),
            )
            for t in times
        ),
    )
    descriptor = StateProviderDescriptor(
        name="router_validation_states",
        version="1.0.0",
        deterministic=True,
        supported_states=FrozenMapping({str(STATE): request.spec_hash}),
    )
    values = [
        StateValue(evaluation_time=t, state=_state_of(t), inputs_used=1, latest_input_time=t)
        for t in times
    ]
    return StateResult.build(request, descriptor, values)


def _runner(market: SyntheticMarket, spec: RouterSpec | None = None) -> RouterTrialRunner:
    bars = lax.inputs(market).bars
    return RouterTrialRunner(
        router=StrategyRouter(spec or _spec(), LIFECYCLE),
        states=_states(),
        strategies={A: _strategy(A, bars, 1), B: _strategy(B, bars, -1)},
        bars=bars,
        cost_model=lax.BACKTEST_COSTS,
        initial_equity=Decimal(1_000_000),
        backtester=BarBacktester(),
    )


def _context(spec: StrategySpec) -> ValidationContext:
    """``gate_fixtures.context`` for the router's spec, one trial (``ROUTER_TRIALS_PER_SPEC``)."""
    profile = lax.LAX_TEST_ONLY_PROFILE
    refs = {"hypothesis_ref": factories.hypothesis_ref(), "risk_policy_ref": None}
    repro = factories.repro_tuple(
        **refs,
        strategy_ref=spec.ref,
        outcome_ref=lax.LABEL_SPEC.outcome,
        cost_model_ref=lax.COST_MODEL.ref,
        validation_profile=profile.ref,
        validation_profile_hash=profile.content_hash(),
        dependency_hashes={
            str(refs["hypothesis_ref"]): factories.HASH_A,
            str(spec.ref): spec.content_hash(),
            str(lax.LABEL_SPEC.outcome): lax.LABEL_SPEC.outcome_spec_hash,
            str(lax.COST_MODEL.ref): lax.COST_MODEL.content_hash(),
        },
    )
    run = factories.experiment_run(repro=repro, state=RunState.COMPLETED)
    metadata = factories.experiment_metadata(
        experiment_hash=run.experiment_hash,
        validation_profile=profile.ref,
        validation_profile_hash=profile.content_hash(),
        hypothesis_family_id="session_router",
        family_trial_count=ROUTER_TRIALS_PER_SPEC,
    )
    return ValidationContext(
        report_id="rep-router-validation",
        subject=spec.ref,
        run=run,
        metadata=metadata,
        profile=profile,
        cost_model=lax.COST_MODEL,
        label_spec=lax.LABEL_SPEC,
        created_at=lax.T0,
    )


def _setup(market: SyntheticMarket, spec: StrategySpec, trials: object) -> ValidatorSetup:
    return ValidatorSetup(
        context=_context(spec),
        outcome_provider=ForwardReturnOutcome((lax.LABEL_SPEC,)),
        manifest_content_hash="7" * 64,
        instrument=SYMBOL,
        trials=trials,  # type: ignore[arg-type]
        chosen_params={},
        seed=11,
        robustness=lax.TEST_ONLY_PARAMS,
        state_of=_state_of,
        bar_volume={(SYMBOL, bar.interval_start): bar.volume for bar in market.bars},
        declared_instruments=(SYMBOL,),
    )


class _Counting:
    def __init__(self, inner: RouterTrialRunner) -> None:
        self.inner = inner
        self.calls: list[tuple[dict[str, object], dict[str, object]]] = []

    def run(self, params, **kwargs) -> TrialRun:  # type: ignore[no-untyped-def]
        self.calls.append((dict(params), dict(kwargs)))
        return self.inner.run(params, **kwargs)


class World:
    def __init__(self) -> None:
        self.market = _market()
        self.runner = _runner(self.market)
        self.spec = router_strategy_spec(_spec(), state=STATE, created_at=lax.T0)
        self.run: RouterPaperRun = self.runner.paper()
        self.counting = _Counting(self.runner)
        self.validator = PipelineBacktestValidator(_setup(self.market, self.spec, self.counting))
        self.first: RouterValidation = validate_router(self.validator, self.spec, self.run)


@pytest.fixture(scope="module")
def world() -> World:
    return World()


def _gate(validation: RouterValidation, gate_id: str) -> Verdict:
    return next(g.verdict for g in validation.validation.report.gates if g.gate_id == gate_id)


def test_the_router_spec_is_a_one_trial_strategy_spec_bound_to_the_router_hash() -> None:
    spec = router_strategy_spec(_spec(), state=STATE)
    assert spec.ref == Ref(kind=Kind.STRATEGY, name="session_router", version="1.0.0")
    assert spec.params[ROUTER_SPEC_HASH_PARAM] == _spec().spec_hash()
    assert spec.signals == (STATE,) and dict(spec.param_search_space) == {}
    assert ROUTER_TRIALS_PER_SPEC == 1
    other = router_strategy_spec(_spec("0.0002"), state=STATE)
    assert other.ref == spec.ref and other.content_hash() != spec.content_hash()
    with pytest.raises(RouterError, match="state"):
        router_strategy_spec(_spec(), state=A)


def test_the_plain_trial_is_exactly_paper_run(world: World) -> None:
    trial = world.runner.run({})
    assert trial.backtest == world.run.result and trial.targets == world.run.targets
    assert trial.backtest.provider_hash == ROUTER_PAPER_BACKTEST.content_hash()
    assert world.run.total_switching_cost > 0  # the validated result is net of switching costs
    # the stress path with nothing stressed reproduces paper_run bit for bit
    same = world.runner.run({}, instruments=(SYMBOL,))
    assert same.backtest.result_hash == world.run.result.result_hash
    delayed = world.runner.run({}, delay_bars=1)
    shifted = world.runner.run({}, decision_offset=MINUTE)
    assert delayed.backtest.result_hash != world.run.result.result_hash
    assert shifted.backtest.result_hash != world.run.result.result_hash
    assert {t.decision_time - MINUTE for t in delayed.targets} <= set(_decisions())
    with pytest.raises(ValueError, match="no search parameters"):
        world.runner.run({"lookback": 60})


def test_a_router_run_through_the_pipeline_is_a_report_bound_to_the_router(world: World) -> None:
    result = world.first
    report = result.validation.report
    assert report.subject == world.spec.ref
    assert result.router_spec_hash == _spec().spec_hash() == world.run.router_spec_hash
    assert result.router_strategy_spec_hash == world.spec.content_hash()
    assert result.paper_run_hash == world.run.run_hash
    context = world.validator._setup.context
    assert report.experiment_hash == context.run.experiment_hash
    assert context.run.repro.dependency_hashes[str(world.spec.ref)] == world.spec.content_hash()
    assert context.metadata.family_trial_count == ROUTER_TRIALS_PER_SPEC
    view = result.validation.view
    assert view is not None and view["subject"] == str(world.spec.ref)
    extra = view["extra"]
    assert isinstance(extra, dict)
    assert extra["backtest_result_hash"] == world.run.result.result_hash
    # structure only: the verdict is whatever derive_verdict says; no verdict is asserted
    assert report.verdict in set(Verdict)
    assert (result.validation.failure_reason is None) == (report.verdict is not Verdict.FAIL)
    assert _gate(result, "G0.reproducibility") is Verdict.PASS
    assert _gate(result, "G0.single_instrument_adapter") is Verdict.PASS


def test_trial_counting_is_one_trial_per_router_spec(world: World) -> None:
    # validate: the chosen (only) point is re-run once for G0; G4 runs only if G0 - G3 did not
    # fail, so its re-runs are checked through the public G4 input builder, whatever the verdict
    assert world.counting.calls[0] == ({}, {})
    counting = _Counting(world.runner)
    validator = PipelineBacktestValidator(_setup(world.market, world.spec, counting))
    g4 = validator.robustness_input(world.spec, world.run.result)
    assert len(g4.trials) == ROUTER_TRIALS_PER_SPEC == g4.family_trial_count
    assert g4.trials[0].params == dict(world.spec.params)
    profile = lax.LAX_TEST_ONLY_PROFILE
    offsets = profile.parameter_stability.time_alignment_offsets
    # the chosen point once, then only the Profile's declared stress re-runs: no other trial
    assert counting.calls == [
        ({}, {}),
        ({}, {"delay_bars": profile.cost_stress.delay_stress_bars}),
        *(({}, {"decision_offset": offset}) for offset in offsets),
    ]
    diagnostic = validator.robustness_diagnostic(world.spec, world.run.result)
    assert diagnostic.backtest_result_hash == world.run.result.result_hash and diagnostic.gates


def test_g0_reproducibility_passes_on_rerun(world: World) -> None:
    again = validate_router(world.validator, world.spec, world.runner.paper())
    assert again.validation.report.content_hash() == world.first.validation.report.content_hash()
    assert again.binding_hash == world.first.binding_hash
    assert _gate(again, "G0.reproducibility") is Verdict.PASS


def test_a_tampered_rerun_fails_g0(world: World) -> None:
    tampered = _runner(world.market, _spec("0.01"))  # same name, other switching rate
    validator = PipelineBacktestValidator(_setup(world.market, world.spec, tampered))
    answer = validate_router(validator, world.spec, world.run)
    assert _gate(answer, "G0.reproducibility") is Verdict.FAIL
    assert answer.validation.report.verdict is Verdict.FAIL
    assert answer.binding_hash != world.first.binding_hash


def test_a_foreign_spec_or_a_tampered_run_is_refused(world: World) -> None:
    other = router_strategy_spec(_spec("0.0002"), state=STATE)
    with pytest.raises(RouterError, match="router_spec_hash"):
        validate_router(world.validator, other, world.run)
    swapped = replace(world.run, result=world.runner.run({}, delay_bars=1).backtest)
    with pytest.raises(RouterError):
        validate_router(world.validator, world.spec, swapped)

"""ADR-0060 through ``PipelineBacktestValidator``: the C-T4 market benchmark and inverse control.

!!! TEST ONLY !!! The Profile, cost model and robustness parameters are the deliberately lax,
**uncalibrated** TEST ONLY values of ``tests/research/synthetic_lab/gate_fixtures.py`` (as in
``test_multi_instrument_validation``), with only the ``benchmark`` block's
``market_benchmark_rule`` / ``inverse_control_reported`` changed per test; never research values.
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from dataclasses import replace
from decimal import Decimal
from pathlib import Path

import pytest

from core.contracts.strategy import BacktestRequest, TargetPosition
from core.contracts.validation_profile import ValidationProfile
from core.domain.research import GateResult, Verdict, derive_verdict
from plugins.backtest import BarBacktester, ExecutionModel
from research.strategies.pipeline import CandidateTrialRunner, StrategyEvaluation
from research.strategies.validation import (
    BacktestValidation,
    PipelineBacktestValidator,
    TrialRun,
    TrialRunner,
)
from research.validation.benchmark import BENCHMARK_UNAVAILABLE, compounded_return
from research.validation.gates import CONFIGURATION_MISSING
from research.validation.instruments import INSTRUMENT_INFIX
from research.validation.report import to_json
from research.validation.returns import PeriodReturns, from_backtest
from tests.contract_version_support import PINNED_CONTRACT_VERSION, at_contract_version
from tests.research.strategies import test_backtest_validation as single
from tests.research.strategies import test_multi_instrument_validation as multi
from tests.research.synthetic_lab import gate_fixtures as lax

CHOSEN = lax.CHOSEN
BH = "G2.market_benchmark.buy_and_hold_equal_weight"

# =========================================================================================
# byte identity: without the opt-in nothing changes
# =========================================================================================

#: Report / view hashes with ``created_at`` fixed, computed on b3986da (before ADR-0060 was
#: implemented) and reproduced after it with ``market_benchmark`` unset. ``single`` is
#: ``test_backtest_validation``'s planted synthetic case (its report hash is also pinned in
#: ``test_multi_instrument_validation``); ``multi`` is ``test_multi_instrument_validation``'s
#: planted ``"p,p"`` pair. No pinned hash changed: no pre-existing caller opts in. Re-pinned for
#: ADR-0060 enforcement (2026-09-26): the TEST ONLY fixture Profiles' ``benchmark`` block now
#: names ``buy_and_hold_equal_weight`` + inverse control (was the placeholder ``"test-only"``),
#: which changes the Profile the reports bind; still no opt-in here, still no benchmark item.
#: Re-pinned for contract 2.1.0 (ADR-0052 §4, 2026-09-26): the intended envelope change only —
#: every newly built contract object is 2.1.0 and the envelope is part of each content hash.
#: The previous values still hold when the same test builds every object at 2.0.0
#: (verified by running it inside ``contract_schema_version_scope("2.0.0")``).
#: 2.0.0 values (evidence, git history): 6dd0103a…, 150814f2…, 53bb8fe3…, 2e445144…
#: Re-pinned for contract 2.2.0 (ADR-0055, 2026-09-26): envelope change only; the 2.1.0
#: values still hold when the test builds every object at 2.1.0 (verified: the unmodified
#: test passes inside ``contract_schema_version_scope("2.1.0")``).
#: 2.1.0 values (evidence, git history): d5ca922f…, ab660a9f…, c90fb699…, 679840de…
#: Recorded at contract 2.2.0: checked with every contract object built at 2.2.0
#: (``single.at_contract_version``) after the envelope-only 2.3.0 – 2.5.0 minors.
PINNED = {
    "single": (
        "f46de6b1b9c047e5743ff9d5676f3e640aea7f2f47b05f00092d7e43acdbbd76",
        "b05266ae6f1c584125a6b694b734d385e9f9e57e1f66b1176fc4df18598ca947",
    ),
    "multi": (
        "8294c4f65b17af845d07e237c00587fae6312d8aa8bc87f520ef7a98d3aa1b00",
        "85f8af38585a0b1cb161d35521e37cb318960709a443271b4fc1eac98ebfe793",
    ),
}


def _hashes(result: StrategyEvaluation) -> tuple[str, str]:
    assert result.validation is not None and result.validation.view is not None
    view = to_json(result.validation.view)
    return result.validation.report.content_hash(), hashlib.sha256(view.encode()).hexdigest()


def _without_the_opt_in_hashes(tmp: str) -> dict[str, tuple[str, str]]:
    tmp_path = Path(tmp)
    market = single._market(seed=7, planted=True)
    ctx = replace(single._context(multi._candidate()), created_at=single.T0)
    planted, _ = single._evaluate(market, tmp_path / "a", context=ctx)
    book = multi.Book.of("p,p")
    mctx = replace(lax.context(multi._candidate(), lax.LAX_TEST_ONLY_PROFILE), created_at=lax.T0)
    pair, _ = multi._evaluate(book, tmp_path / "b", context=mctx)
    for result in (planted, pair):
        assert result.validation is not None
        ids = [g.gate_id for g in result.validation.report.gates]
        assert not any(i.startswith(("G2.market_benchmark", "G2.inverse_control")) for i in ids)
    return {"single": _hashes(planted), "multi": _hashes(pair)}


def test_without_the_opt_in_every_report_is_byte_identical(tmp_path: Path) -> None:
    found = at_contract_version(
        PINNED_CONTRACT_VERSION, f"{__name__}:_without_the_opt_in_hashes", str(tmp_path)
    )
    assert {name: tuple(pair) for name, pair in found.items()} == PINNED


# =========================================================================================
# helpers
# =========================================================================================


def _profile(rule: str, inverse: bool) -> ValidationProfile:
    """TEST ONLY: the lax Profile with another C-T4 benchmark rule / inverse flag."""
    data = lax.LAX_TEST_ONLY_PROFILE.model_dump()
    data["benchmark"] = {
        **data["benchmark"],
        "market_benchmark_rule": rule,
        "inverse_control_reported": inverse,
    }
    return ValidationProfile.model_validate(data)


def _runner(book: multi.Book, backtester: BarBacktester | None = None) -> CandidateTrialRunner:
    return CandidateTrialRunner(multi._candidate(), book.inputs(), backtester or BarBacktester())


def _validate(
    book: multi.Book,
    profile: ValidationProfile,
    *,
    runner: TrialRunner | None = None,
    one: bool = False,
    **fields: object,
) -> BacktestValidation:
    """Validate the chosen point; ``one`` = the single-instrument path on ``book.names[0]``."""
    candidate = multi._candidate()
    run_with = runner or _runner(book)
    values: dict[str, object] = {"context": lax.context(candidate, profile), **fields}
    if one:
        values |= {"instruments": None, "declared_instruments": (book.names[0],)}
    setup = multi._setup(book, run_with, **values)
    backtest = run_with.run(CHOSEN).backtest
    return PipelineBacktestValidator(setup).validate(candidate.spec.ref, candidate.spec, backtest)


def _by_id(validation: BacktestValidation) -> dict[str, GateResult]:
    return {g.gate_id: g for g in validation.report.gates}


def _without_items(gates: Sequence[GateResult]) -> list[GateResult]:
    return [
        g for g in gates if not g.gate_id.startswith(("G2.market_benchmark", "G2.inverse_control"))
    ]


def _rerun_returns(rerun: TrialRun, targets: tuple[TargetPosition, ...]) -> PeriodReturns:
    request = BacktestRequest(
        cost_model=rerun.cost_model,
        initial_equity=rerun.backtest.initial_equity,
        bars=rerun.bars,
        targets=targets,
    )
    return from_backtest(BarBacktester().run(request))


def _hold(rerun: TrialRun, weights: dict[str, Decimal]) -> tuple[TargetPosition, ...]:
    start = min(t.decision_time for t in rerun.targets)
    return tuple(
        TargetPosition(
            decision_time=start,
            instrument=name,
            target_weight=weight,
            inputs_used=1,
            latest_input_available_time=start,
        )
        for name, weight in sorted(weights.items())
    )


# =========================================================================================
# single instrument
# =========================================================================================


@pytest.fixture(scope="module")
def book() -> multi.Book:
    return multi.Book.of("p")


def test_buy_and_hold_and_the_inverse_control_are_reported(book: multi.Book) -> None:
    profile = _profile("buy_and_hold_equal_weight", True)
    plain = _validate(book, profile, one=True)
    opted = _validate(book, profile, one=True, market_benchmark=True)
    gates = _by_id(opted)
    items = [g for g in opted.report.gates if g not in plain.report.gates]
    assert [g.gate_id for g in items] == [
        BH,
        f"{BH}.benchmark_net_return",
        f"{BH}.period_excess_mean",
        f"{BH}.period_excess_positive_fraction",
        "G2.inverse_control",
    ]
    assert all(g.verdict is Verdict.PASS and g.threshold is None for g in items)
    # every other gate, and the verdict, are unchanged: the items are reported only
    assert _without_items(opted.report.gates) == list(plain.report.gates)
    assert opted.report.verdict is plain.report.verdict is derive_verdict(opted.report.gates)

    # the numbers are the same backtester's, over the same bars, cost model and equity
    rerun = _runner(book).run(CHOSEN)
    strategy = from_backtest(rerun.backtest)
    hold = _rerun_returns(rerun, _hold(rerun, {book.names[0]: Decimal(1)}))
    negated = tuple(
        t.model_copy(update={"target_weight": -t.target_weight}) if t.target_weight else t
        for t in rerun.targets
    )
    inverse = _rerun_returns(rerun, negated)
    assert gates[f"{BH}.benchmark_net_return"].value == float(compounded_return(hold))
    assert gates[BH].value == float(compounded_return(strategy) - compounded_return(hold))
    assert gates["G2.inverse_control"].value == float(compounded_return(inverse))
    assert compounded_return(hold) != 0 and compounded_return(inverse) != 0
    # the benchmark really held the instrument; the inverse traded the opposite side
    start = min(t.decision_time for t in rerun.targets)
    entry = next(bar for bar in rerun.bars if bar.interval_start >= start)
    assert float(compounded_return(hold)) == pytest.approx(
        float(rerun.bars[-1].close / entry.open) - 1, abs=1e-3
    )


def test_flat_and_none(book: multi.Book) -> None:
    flat = _by_id(_validate(book, _profile("flat", False), one=True, market_benchmark=True))
    strategy = from_backtest(_runner(book).run(CHOSEN).backtest)
    assert flat["G2.market_benchmark.flat"].value == float(compounded_return(strategy))
    assert flat["G2.market_benchmark.flat.benchmark_net_return"].value == 0.0
    assert "G2.inverse_control" not in flat

    profile = _profile("none", False)
    none = _validate(book, profile, one=True, market_benchmark=True)
    assert none.report.gates == _validate(book, profile, one=True).report.gates  # no item


def test_an_unregistered_rule_is_inconclusive_never_pass(book: multi.Book) -> None:
    """The TEST ONLY placeholder ``"test-only"`` is not a registered rule (the lax Profile names
    ``buy_and_hold_equal_weight`` since ADR-0060 enforcement, so the placeholder is explicit)."""
    placeholder = _profile("test-only", False)
    plain = _validate(book, placeholder, one=True)
    opted = _validate(book, placeholder, one=True, market_benchmark=True)
    assert plain.report.verdict is Verdict.PASS
    assert opted.report.verdict is Verdict.INCONCLUSIVE
    (gap,) = [g for g in opted.report.gates if g.verdict is not Verdict.PASS]
    assert (gap.gate_id, gap.metric) == (
        "G2.market_benchmark",
        f"{CONFIGURATION_MISSING}:benchmark.market_benchmark_rule=test-only",
    )
    assert opted.failure_reason is None


class _Counting:
    def __init__(self, inner: TrialRunner) -> None:
        self.inner = inner
        self.calls: list[dict[str, object]] = []

    def run(self, params, **kwargs) -> TrialRun:  # type: ignore[no-untyped-def]
        self.calls.append(dict(kwargs))
        return self.inner.run(params, **kwargs)


def test_the_items_add_no_trial(book: multi.Book) -> None:
    """The benchmark and inverse re-runs are backtests of the same trial: the ``TrialRunner``
    sees exactly the same calls and ``family_trial_count`` is unchanged."""
    profile = _profile("buy_and_hold_equal_weight", True)
    plain, opted = _Counting(_runner(book)), _Counting(_runner(book))
    a = _validate(book, profile, runner=plain, one=True)
    b = _validate(book, profile, runner=opted, one=True, market_benchmark=True)
    assert opted.calls == plain.calls and plain.calls
    g3 = [g for g in b.report.gates if g.gate_id == "G3.adjusted_p_value"]
    assert g3 == [g for g in a.report.gates if g.gate_id == "G3.adjusted_p_value"]


# =========================================================================================
# execution model
# =========================================================================================

FUNDING = ExecutionModel(short_borrow_rate=Decimal("0.0001"))


def test_an_unreproduced_execution_model_is_inconclusive(book: multi.Book) -> None:
    """The runner used an execution model the setup does not declare: the items are never
    computed under a plain ``BarBacktester()`` instead."""
    profile = _profile("buy_and_hold_equal_weight", True)
    runner = _runner(book, BarBacktester(execution=FUNDING))
    opted = _validate(book, profile, runner=runner, one=True, market_benchmark=True)
    gates = _by_id(opted)
    reason = f"{BENCHMARK_UNAVAILABLE}:execution_model_not_reproduced"
    for gate_id in (BH, "G2.inverse_control"):
        assert (gates[gate_id].verdict, gates[gate_id].metric) == (Verdict.INCONCLUSIVE, reason)
    assert opted.report.verdict is not Verdict.PASS


def test_a_declared_execution_model_is_used(book: multi.Book) -> None:
    profile = _profile("buy_and_hold_equal_weight", True)
    runner = _runner(book, BarBacktester(execution=FUNDING))
    opted = _validate(
        book, profile, runner=runner, one=True, market_benchmark=True, execution=FUNDING
    )
    gates = _by_id(opted)
    assert gates["G0.execution_model"].verdict is Verdict.PASS
    assert gates[BH].verdict is gates["G2.inverse_control"].verdict is Verdict.PASS
    plain = _by_id(_validate(book, profile, one=True, market_benchmark=True))
    # funding on the (negated) short side changes the inverse control's result
    assert gates["G2.inverse_control"].value != plain["G2.inverse_control"].value


# =========================================================================================
# multi-instrument: pooled only, equal weight across the scope
# =========================================================================================


def test_the_multi_instrument_benchmark_is_pooled_and_equal_weight() -> None:
    pair = multi.Book.of("p,p")
    profile = _profile("buy_and_hold_equal_weight", True)
    plain = _validate(pair, profile)
    opted = _validate(pair, profile, market_benchmark=True)
    gates = _by_id(opted)
    assert _without_items(opted.report.gates) == list(plain.report.gates)
    assert opted.report.verdict is plain.report.verdict
    assert not any(
        INSTRUMENT_INFIX in g and g.startswith(("G2.market_benchmark", "G2.inverse_control"))
        for g in gates
    )
    rerun = _runner(pair).run(CHOSEN)
    half = Decimal(1) / Decimal(2)
    hold = _rerun_returns(rerun, _hold(rerun, dict.fromkeys(pair.names, half)))
    assert gates[f"{BH}.benchmark_net_return"].value == float(compounded_return(hold))
    alone = _rerun_returns(rerun, _hold(rerun, {pair.names[0]: Decimal(1)}))
    assert compounded_return(hold) != compounded_return(alone)  # both instruments are held

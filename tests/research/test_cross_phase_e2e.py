"""Cross-phase end-to-end smoke (W1): P9 -> F4 -> P2 -> P5 -> P6 (+ P7 ledger) -> P10 paper run.

Entirely in memory, one synthetic instrument, a few hundred 1-minute bars:

1. P9: a seeded random-walk market with a planted lag-1 return autocorrelation;
2. F4: ``bar_log_return`` and ``bar_realized_vol_10`` through ``run_feature`` (per-time truncation)
   over the synthetic bars adapted to ``FeatureObservation`` (lineage labels point at the market);
3. P2: a volatility regime through ``run_state`` over the realized-volatility feature values;
4. P5: ``tsmom_bars`` on the log-return feature values (as ``SignalObservation``) and the
   ``BarBacktester``;
5. P6: ``matrix_from_backtest``; every conditional researched is registered in a ``TrialLedger``;
6. P10: ``paper_run`` of a router over the volatility regime that routes to ``tsmom_bars``.

Checked: determinism (a re-run gives identical hashes), no look-ahead (perturbing bars after a cut
leaves every output up to the cut unchanged, and does change later outputs), and that every step's
output is bound to its inputs. It is a smoke test of the wiring: nothing here is a validation, no
number below is a threshold, and a synthetic market never supports a claim about a real one.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from core.contracts.feature import FeatureObservation, FeatureRequest, FeatureResult
from core.contracts.state import StateRequest, StateResult
from core.contracts.strategy import (
    BacktestCostModel,
    BacktestRequest,
    BacktestResult,
    PriceBar,
    SignalObservation,
    StrategyRequest,
    StrategyResult,
)
from core.contracts.synthetic import (
    PlantedEffect,
    SyntheticBar,
    SyntheticMarket,
    SyntheticMarketSpec,
)
from core.contracts.universe import SelectedRevisionLineage
from core.domain.base import FrozenMapping, content_hash
from core.domain.specs import FeatureSpec
from core.lifecycle.strategy import LifecycleState
from infrastructure.feature.runner import run_feature
from infrastructure.state import run_state, state_inputs, state_request
from plugins.backtest import BarBacktester
from plugins.features import BarLogReturnProvider, BarRealizedVolatilityProvider
from plugins.states import VOLATILITY_LABELS, VolatilityRegimeProvider
from plugins.synthetic import RandomWalkMarket
from research.experiments import (
    StateStrategyMatrix,
    backtest_returns,
    matrix_from_backtest,
    register_conditionals,
)
from research.hypotheses import TrialLedger
from research.router import RouterPaperRun, RouterSpec, StrategyRouter, paper_run
from research.strategies.signals import LOG_RETURN_SIGNAL
from research.strategies.time_series_momentum import TimeSeriesMomentumProvider, tsmom_spec

SYMBOL = "SYN-USD"
BARS = 420
CUT = 300  # bars after this index are perturbed in the look-ahead test
T0 = datetime(2026, 1, 5, tzinfo=UTC)
MARKET = SyntheticMarketSpec(
    name="e2e_market",
    version="1.0.0",
    symbol=SYMBOL,
    start=T0,
    minutes=BARS,
    seed=20260925,
    initial_price=Decimal(100),
    volatility=Decimal("0.001"),
    effects=(PlantedEffect(lag_minutes=1, strength=Decimal("0.3")),),
)
LOG_RETURN = BarLogReturnProvider.spec()
REALIZED_VOL = BarRealizedVolatilityProvider.spec(10)
VOL_REGIME = VolatilityRegimeProvider.spec(
    REALIZED_VOL.ref,
    cuts=("0.3333", "0.6667"),
    min_history=30,
    training_window=timedelta(minutes=120),
    seed=7,
)
TSMOM = tsmom_spec()
TSMOM_PARAMS = {"lookback": 60, "long_only": False}
COSTS = BacktestCostModel(
    name="e2e_costs", version="1.0.0", fee_rate=Decimal("0.0004"), slippage_rate=Decimal("0.0001")
)
EQUITY = Decimal(100_000)
FAMILY = "e2e_tsmom_bars_x_volatility_regime"
ROUTER = RouterSpec(
    name="e2e_vol_router",
    version="1.0.0",
    table={
        "low_vol": {str(TSMOM.ref): Decimal(1)},
        "mid_vol": {str(TSMOM.ref): Decimal("0.5")},
        "high_vol": {},
    },
    fallback={},
    switching_cost_rate=Decimal("0.0005"),
)
#: Test fixture only: the router refuses unvalidated strategies, so the smoke declares the
#: research TSMOM ACTIVE here. Nothing is promoted; no lifecycle transition happens anywhere.
LIFECYCLE = {TSMOM.ref: LifecycleState.ACTIVE}


@dataclass(frozen=True)
class Pipeline:
    source: str
    observations: tuple[FeatureObservation, ...]
    feature_requests: dict[str, FeatureRequest]
    features: dict[str, FeatureResult]
    state_request: StateRequest
    states: StateResult
    strategy_request: StrategyRequest
    strategy: StrategyResult
    price_bars: tuple[PriceBar, ...]
    backtest_request: BacktestRequest
    backtest: BacktestResult
    matrix: StateStrategyMatrix
    ledger: TrialLedger
    router: RouterPaperRun


def _observation(index: int, bar: SyntheticBar, source: str) -> FeatureObservation:
    values: dict[str, Decimal | int | str] = {
        "symbol": SYMBOL,
        "open": bar.open,
        "high": bar.high,
        "low": bar.low,
        "close": bar.close,
        "volume": bar.volume,
        "trade_count": bar.trade_count,
    }
    return FeatureObservation(
        observation_key=f"synthetic:{SYMBOL}:{bar.interval_start.isoformat()}",
        event_time=bar.interval_start,
        event_end_time=bar.interval_end,
        available_time=bar.interval_end,
        knowledge_time=bar.interval_end,
        values=FrozenMapping(values),
        lineage=SelectedRevisionLineage(
            canonical_table="canonical.bars_1m",
            canonical_revision_id=f"synthetic-{index}",
            raw_table="raw.synthetic_bars",
            raw_revision_id=f"synthetic-{index}",
            source_table="raw.synthetic_markets",
            source_revision_id=source,
        ),
    )


def _price_bar(bar: SyntheticBar) -> PriceBar:
    return PriceBar(
        instrument=SYMBOL,
        interval_start=bar.interval_start,
        interval_end=bar.interval_end,
        available_time=bar.interval_end,
        open=bar.open,
        high=bar.high,
        low=bar.low,
        close=bar.close,
    )


def _feature(
    provider: BarLogReturnProvider | BarRealizedVolatilityProvider,
    spec: FeatureSpec,
    observations: tuple[FeatureObservation, ...],
    times: tuple[datetime, ...],
    source: str,
) -> tuple[FeatureRequest, FeatureResult]:
    request = FeatureRequest(
        feature=spec.ref,
        spec_hash=spec.content_hash(),
        manifest_content_hash=source,  # an ad-hoc run: the market hash is the label
        knowledge_cutoff=times[-1],
        evaluation_times=times,
        observations=observations,
    )
    return request, run_feature(provider, spec, request)


def _signals(result: FeatureResult) -> tuple[SignalObservation, ...]:
    """F4 values -> P5 signal observations: a value evaluated at ``t`` is known at ``t``."""
    return tuple(
        SignalObservation(
            signal=LOG_RETURN_SIGNAL,
            instrument=SYMBOL,
            event_time=value.evaluation_time,
            available_time=value.evaluation_time,
            knowledge_time=value.evaluation_time,
            value=value.value,
        )
        for value in result.values
    )


def run_pipeline(bars: tuple[SyntheticBar, ...], source: str) -> Pipeline:
    times = tuple(bar.interval_end for bar in bars)
    observations = tuple(_observation(i, bar, source) for i, bar in enumerate(bars))

    # F4 features.
    lr_request, lr_result = _feature(
        BarLogReturnProvider((LOG_RETURN,)), LOG_RETURN, observations, times, source
    )
    vol_request, vol_result = _feature(
        BarRealizedVolatilityProvider((REALIZED_VOL,)), REALIZED_VOL, observations, times, source
    )

    # P2 states over the realized-volatility values.
    s_request = state_request(VOL_REGIME, times, state_inputs([(vol_request, vol_result)]))
    states = run_state(VolatilityRegimeProvider((VOL_REGIME,)), VOL_REGIME, s_request)

    # P5 strategy and backtest.
    provider = TimeSeriesMomentumProvider((TSMOM,))
    st_request = StrategyRequest(
        strategy=TSMOM.ref,
        spec_hash=TSMOM.content_hash(),
        params=FrozenMapping(TSMOM_PARAMS),
        instruments=(SYMBOL,),
        knowledge_cutoff=times[-1],
        decision_times=times,
        signals=_signals(lr_result),
    )
    strategy = provider.target_positions(st_request)
    strategy.check_answers(st_request, provider.descriptor)
    price_bars = tuple(_price_bar(bar) for bar in bars)
    bt_request = BacktestRequest(
        cost_model=COSTS, initial_equity=EQUITY, bars=price_bars, targets=strategy.positions
    )
    backtester = BarBacktester()
    backtest = backtester.run(bt_request)
    backtest.check_answers(bt_request, backtester.descriptor)

    # P6 matrix; every conditional researched counts as a trial (P7 ledger).
    matrix = matrix_from_backtest(TSMOM.ref, VOL_REGIME.ref, backtest, states)
    ledger = TrialLedger()
    register_conditionals(
        ledger,
        strategy=TSMOM.ref,
        state=VOL_REGIME.ref,
        labels=[cell.state for cell in matrix.cells if cell.state is not None],
        family_id=FAMILY,
        minimum_effect="0.1",
    )

    # P10 router paper run.
    router = paper_run(
        StrategyRouter(ROUTER, LIFECYCLE),
        states,
        {TSMOM.ref: strategy},
        bars=price_bars,
        cost_model=COSTS,
        initial_equity=EQUITY,
        backtester=BarBacktester(),
    )
    return Pipeline(
        source=source,
        observations=observations,
        feature_requests={"log_return": lr_request, "realized_vol": vol_request},
        features={"log_return": lr_result, "realized_vol": vol_result},
        state_request=s_request,
        states=states,
        strategy_request=st_request,
        strategy=strategy,
        price_bars=price_bars,
        backtest_request=bt_request,
        backtest=backtest,
        matrix=matrix,
        ledger=ledger,
        router=router,
    )


def _market() -> SyntheticMarket:
    return RandomWalkMarket().generate(MARKET)


def _hashes(run: Pipeline) -> dict[str, str]:
    return {
        "log_return": run.features["log_return"].result_hash,
        "realized_vol": run.features["realized_vol"].result_hash,
        "states": run.states.result_hash,
        "strategy": run.strategy.result_hash,
        "backtest": run.backtest.result_hash,
        "matrix": run.matrix.matrix_hash,
        "ledger": content_hash([h.content_hash() for h in run.ledger.hypotheses]),
        "router": run.router.run_hash,
        "router_result": run.router.result.result_hash,
    }


@pytest.fixture(scope="module")
def market() -> SyntheticMarket:
    return _market()


@pytest.fixture(scope="module")
def base(market: SyntheticMarket) -> Pipeline:
    return run_pipeline(market.bars, market.market_hash)


def test_the_market_carries_its_planted_truth(market: SyntheticMarket) -> None:
    assert len(market.bars) == BARS <= 2000
    assert market.truth == MARKET.effects


def test_every_step_is_bound_to_its_inputs(market: SyntheticMarket, base: Pipeline) -> None:
    # F4: observations point at the market; each result answers its request.
    assert {o.lineage.source_revision_id for o in base.observations} == {market.market_hash}
    for name, request in base.feature_requests.items():
        assert request.manifest_content_hash == market.market_hash
        assert base.features[name].request_hash == request.content_hash()
    # P2: every state input is a realized-vol value bound to that feature result.
    vol = base.features["realized_vol"]
    assert {i.source_result_hash for i in base.state_request.inputs} == {vol.result_hash}
    assert base.states.request_hash == base.state_request.content_hash()
    assert {v.state for v in base.states.values} - {None} <= set(VOLATILITY_LABELS)
    # P5: signals are exactly the log-return values; targets are the strategy's positions.
    lr = {v.evaluation_time: v.value for v in base.features["log_return"].values}
    assert {s.event_time: s.value for s in base.strategy_request.signals} == lr
    assert base.strategy.request_hash == base.strategy_request.content_hash()
    assert base.backtest_request.targets == base.strategy.positions
    assert base.backtest.request_hash == base.backtest_request.content_hash()
    assert any(p.target_weight != 0 for p in base.strategy.positions)
    assert base.backtest.fills
    # P6 / P7: the matrix names both inputs; one registered trial per conditional label.
    assert base.matrix.backtest_result_hash == base.backtest.result_hash
    assert base.matrix.state_result_hash == base.states.result_hash
    labels = [cell.state for cell in base.matrix.cells if cell.state is not None]
    assert len(labels) >= 2
    assert base.ledger.trials(FAMILY) == len(labels)
    for hypothesis in base.ledger.hypotheses:
        assert hypothesis.origin_refs == (TSMOM.ref, VOL_REGIME.ref)
    # P10: the router run binds the state, strategy and its own backtest request.
    run = base.router
    assert run.state_result_hash == base.states.result_hash
    assert run.strategy_result_hashes == {str(TSMOM.ref): base.strategy.result_hash}
    assert run.router_spec_hash == ROUTER.spec_hash()
    assert run.request.bars == base.price_bars
    assert run.gross.request_hash == run.request.content_hash()
    assert run.result.request_hash == run.request.content_hash()
    assert [d.at for d in run.decisions] == [v.evaluation_time for v in base.states.values]
    assert run.charges and run.total_switching_cost > 0
    assert run.result.final_equity == run.gross.final_equity - run.total_switching_cost


def test_a_rerun_is_identical(base: Pipeline) -> None:
    again = _market()
    assert again.market_hash == base.source
    assert _hashes(run_pipeline(again.bars, again.market_hash)) == _hashes(base)


def _perturbed(bars: tuple[SyntheticBar, ...]) -> tuple[SyntheticBar, ...]:
    out = list(bars[: CUT + 1])
    for index in range(CUT + 1, len(bars)):
        bar = bars[index]
        factor = Decimal(1) + Decimal((index * 7) % 11 - 5) / 100
        out.append(
            bar.model_copy(
                update={
                    "open": bar.open * factor,
                    "high": bar.high * factor,
                    "low": bar.low * factor,
                    "close": bar.close * factor,
                    "volume": bar.volume * 2,
                }
            )
        )
    return tuple(out)


def test_perturbing_future_bars_leaves_earlier_outputs_unchanged(
    market: SyntheticMarket, base: Pipeline
) -> None:
    cut = market.bars[CUT].interval_end
    other = run_pipeline(
        _perturbed(market.bars), content_hash({"perturbed": market.market_hash, "after": CUT})
    )

    def upto(items: tuple[object, ...], stamp: str, *, strict: bool = False) -> list[object]:
        return [
            item
            for item in items
            if (getattr(item, stamp) < cut if strict else getattr(item, stamp) <= cut)
        ]

    for name in ("log_return", "realized_vol"):
        assert upto(other.features[name].values, "evaluation_time") == upto(
            base.features[name].values, "evaluation_time"
        )
    assert upto(other.states.values, "evaluation_time") == upto(
        base.states.values, "evaluation_time"
    )
    assert upto(other.strategy.positions, "decision_time") == upto(
        base.strategy.positions, "decision_time"
    )
    assert upto(other.backtest.equity_curve, "time") == upto(base.backtest.equity_curve, "time")
    assert upto(other.backtest.fills, "fill_time", strict=True) == upto(
        base.backtest.fills, "fill_time", strict=True
    )
    before = {t: r for t, r in backtest_returns(base.backtest).items() if t < cut}
    assert {t: r for t, r in backtest_returns(other.backtest).items() if t < cut} == before
    assert upto(other.router.decisions, "at") == upto(base.router.decisions, "at")
    assert upto(other.router.targets, "decision_time") == upto(base.router.targets, "decision_time")
    assert upto(other.router.result.equity_curve, "time") == upto(
        base.router.result.equity_curve, "time"
    )
    paid = [c for c in base.router.charges if c.charged_at is not None and c.charged_at <= cut]
    assert [
        c for c in other.router.charges if c.charged_at is not None and c.charged_at <= cut
    ] == paid
    # ... and the perturbation does reach the later outputs (the test has teeth).
    assert other.features["log_return"].values != base.features["log_return"].values
    assert other.backtest.final_equity != base.backtest.final_equity
    assert other.router.result.final_equity != base.router.result.final_equity

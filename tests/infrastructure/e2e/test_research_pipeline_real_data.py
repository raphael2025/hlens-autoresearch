"""Real-data capability smoke: the whole research pipeline on a Binance-format Research Dataset.

Debugging pass, step D4 of ``docs/reviews/2026-09-25-framework-debug-backlog.md``. The goal is to
prove that the pipes carry real-format data end to end — **never a market conclusion**: Profile
numbers are not frozen, nothing here is validated, and the verdict is only checked to be one of the
defined verdicts (it is never asserted to be PASS).

Data. There is no real market data offline (the local warehouse holds table metadata only, and
network collectors are not run), so — like the G1 first slice (``test_phase1_first_slice.py``) —
the fixture is Binance-format 1m klines (the 12-slot archive / REST element) for BTCUSDT and
ETHUSDT, 300 minutes each, taken through the real ingestion path: archive store (D2), REST
collector over the mock venue + REST store (D3D / D3E), Canonical normalizer (E1), D-33 channel
reconciliation, quality reports (E3), exchangeInfo listings (E2) and the F3 ``DatasetBuilder``, on
the dedicated PostgreSQL **test** catalog with a ``tmp_path`` warehouse (``HLENS_TEST_CATALOG_URI``;
the real warehouse is never touched). Every table stays far below 20k rows.

Chain (one run; then a fresh process on the same catalog runs it again):

1. F3: two manifests over the same bars and the same upstream snapshots, both binding the ADR-0032
   archive assumption: an **interval** spec (F4 needs a PIT view at every evaluation time) and a
   **point** spec (the P4 / P5 bar path accepts only a point spec — backlog E1), bound together by
   ``pair_manifests`` (both verified; same snapshots, ADR-0032 choice, instruments and lineage; the
   point view at the end of the interval);
2. F4: ``bar_log_return`` and ``bar_realized_vol_10`` through ``feature_request_from_dataset`` (the
   interval manifest, verified) and ``run_feature``;
3. P2: a volatility regime (``run_state``) over the realized-volatility values;
4. P3: ``StateSwitchProvider`` events (``run_events``) over the state series;
5. P4: forward-return labels of those events through ``outcome_request_from_dataset`` (point
   manifest) and ``materialize``;
6. P5: research TSMOM on the F4 log returns (``signals_from_features``) and the ``BarBacktester`` on
   ``backtest_bars_from_dataset`` (point manifest), through ``evaluate_strategy``;
7. P4 / P8: ``PipelineBacktestValidator`` (G0 – G4) under a TEST ONLY profile, given the proven
   bars and the chain's ``ManifestPair`` (``G0.manifest_binding`` checks both, backlog E5 / E1);
8. P6: ``matrix_from_backtest`` over the backtest and the P2 states;
9. reports written by ``research.reports`` and read back through ``apps.api.store.ReportStore``.

Checked: every step's output is bound to its inputs by hash; a rerun is hash-identical; no step
sees data whose ``available_time`` is after its own cutoff; the verdict is a defined verdict.

!!! TEST ONLY !!!  ``SMOKE_TEST_ONLY_PROFILE`` and ``SMOKE_TEST_ONLY_PARAMS`` hold arbitrary,
uncalibrated numbers chosen only to let every gate run on five hours of minutes. They are not a
proposal, not a calibration result and must never be used for research.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, Final

import pytest

from apps.api.store import ReportKind, ReportStore
from core.contracts.cost_model import CostModelSpec
from core.contracts.event import EventInputPoint, EventRequest, EventResult
from core.contracts.feature import (
    FeatureObservation,
    FeatureRequest,
    FeatureResult,
)
from core.contracts.outcome import OutcomeEvent, OutcomeLabelSpec, OutcomeMethod, OutcomeRequest
from core.contracts.revision import PointInTimeSpec
from core.contracts.state import StateRequest, StateResult
from core.contracts.strategy import BacktestCostModel, SignalObservation
from core.contracts.validation_profile import (
    BenchmarkParams,
    CostStressParams,
    DataSplitParams,
    ParameterStabilityParams,
    SampleSizeParams,
    SignificanceParams,
    WalkForwardParams,
)
from core.domain.base import content_hash
from core.domain.research import RunState, ValidationReport, Verdict
from core.domain.specs import FeatureSpec, OutcomeSpec
from infrastructure.bars import (
    DatasetPriceBars,
    ManifestPair,
    backtest_bars_from_dataset,
    outcome_request_from_dataset,
    pair_manifests,
)
from infrastructure.dataset.builder import DatasetBuilt
from infrastructure.event.inputs import inputs_from_state_series, state_series_from_state_run
from infrastructure.event.runner import run_events
from infrastructure.feature.dataset import feature_request_from_dataset
from infrastructure.feature.observations import bar_observations
from infrastructure.feature.runner import run_feature
from infrastructure.pit.assumption import ASSUMPTION_BINDING, ASSUMPTION_LATENCY
from infrastructure.pit.selector import PitSelector
from infrastructure.state import run_state, state_inputs, state_request
from infrastructure.strategy.signals import signals_from_features
from plugins.backtest import BarBacktester
from plugins.events import StateSwitchProvider
from plugins.features import BarLogReturnProvider, BarRealizedVolatilityProvider
from plugins.outcomes import ForwardReturnOutcome
from plugins.states import VOLATILITY_LABELS, VolatilityRegimeProvider
from research.experiments import StateStrategyMatrix, matrix_from_backtest
from research.outcomes import materialize
from research.outcomes.table import OutcomeTable
from research.reports import (
    WrittenReport,
    write_state_strategy_matrix,
    write_validation_report,
)
from research.strategies.failure_registry import FailureRegistry
from research.strategies.library import library_entries
from research.strategies.pipeline import (
    CandidateTrialRunner,
    EvaluationInputs,
    EvaluationStatus,
    StrategyCandidate,
    StrategyEvaluation,
    evaluate_strategy,
)
from research.strategies.signals import LOG_RETURN_SIGNAL
from research.strategies.validation import (
    DIAGNOSTIC_MODE,
    PRICE_BINDING_DATASET,
    PipelineBacktestValidator,
    RobustnessDiagnostic,
    ValidatorSetup,
)
from research.validation import RobustnessParams, ValidationContext
from tests import factories
from tests.infrastructure.catalog.catalog_support import postgres_test_catalog_uri
from tests.infrastructure.dataset import dataset_support as ds
from tests.infrastructure.e2e.first_slice_support import BTC, DAY_END, DAY_START, ETH
from tests.infrastructure.e2e.first_slice_support import ingest_bars_for as ingest_klines
from tests.infrastructure.parser.rest_support import kline_item
from tests.infrastructure.redteam import redteam_support as rt
from tests.infrastructure.revision import rest_store_support as ss

pytestmark = pytest.mark.postgres

MINUTE: Final = timedelta(minutes=1)
#: 300 one-minute bars per symbol, 18:00 – 23:00 UTC of ``ss.DAY`` (2023-11-14).
BAR_COUNT: Final = 300
FIRST_BAR: Final = datetime(2023, 11, 14, 18, tzinfo=UTC)
BTC_C, ETH_C = "BTC-USDT", "ETH-USDT"  # Canonical symbols

LOG_RETURN: Final = BarLogReturnProvider.spec()
REALIZED_VOL: Final = BarRealizedVolatilityProvider.spec(10)
VOL_REGIME: Final = VolatilityRegimeProvider.spec(
    REALIZED_VOL.ref,
    cuts=("0.3333", "0.6667"),
    min_history=30,
    training_window=timedelta(minutes=120),
    seed=7,
)
SWITCH: Final = StateSwitchProvider.spec(VOL_REGIME.ref)
OUTCOME: Final = OutcomeSpec(
    name="smoke_forward_15m",
    version="1.0.0",
    created_at=datetime(2023, 11, 1, tzinfo=UTC),
    horizon=15 * MINUTE,
    label_definition="TEST ONLY: 15-minute forward return on dataset bars (capability smoke)",
)
LABEL: Final = OutcomeLabelSpec.bind(OUTCOME, OutcomeMethod.FORWARD_RETURN)
CHOSEN: Final = {"lookback": 60}
EQUITY: Final = Decimal(1_000_000)
FEE, SLIPPAGE = Decimal("0.0004"), Decimal("0.0001")
COST_MODEL: Final = CostModelSpec(
    name="cost_v1",
    version="1.0.0",
    created_at=datetime(2023, 11, 1, tzinfo=UTC),
    fee_rate_per_side=FEE,
    slippage_rate_per_side=SLIPPAGE,
)
BACKTEST_COSTS: Final = BacktestCostModel(
    name="cost_v1", version="1.0.0", fee_rate=FEE, slippage_rate=SLIPPAGE
)
#: The report stamp (a fixed, data-derived time; never the wall clock — backlog E3).
REPORT_TIME: Final = ds.SIM

#: TEST ONLY — arbitrary, uncalibrated numbers (see module docstring).
SMOKE_TEST_ONLY_PROFILE: Final = factories.validation_profile(
    name="test_only_real_data_smoke_uncalibrated",
    data_split=DataSplitParams(
        research_window_start=date(2023, 11, 14),
        sealed_oos_boundary=date(2023, 11, 15),
        sealed_oos_length=timedelta(days=1),
        sealed_oos_max_extension=timedelta(0),
        embargo=15 * MINUTE,
        walk_forward=WalkForwardParams(
            train_window=timedelta(hours=2),
            test_window=timedelta(hours=1),
            step=timedelta(hours=1),
            min_positive_window_fraction=0.5,
            max_single_window_pnl_share=0.6,
        ),
    ),
    sample_size=SampleSizeParams(
        min_effective_trades_in_sample=10,
        min_effective_trades_out_of_sample=5,
        min_effective_trades_per_state=5,
        effective_sample_method="overlap-clusters",
        min_regime_coverage="test-only",
    ),
    significance=SignificanceParams(
        multiple_testing_method="bonferroni",
        multiple_testing_threshold=0.05,
        overfitting_metric="pbo_cscv",
        overfitting_threshold=0.3,
        trial_count_scope="family",
    ),
    benchmark=BenchmarkParams(
        null_model="random-entry",
        null_model_simulations=50,
        null_model_percentile=90.0,
        # ADR-0060 enforcement: a registered rule (the "test-only" placeholder is INCONCLUSIVE)
        market_benchmark_rule="buy_and_hold_equal_weight",
        inverse_control_reported=True,
    ),
    parameter_stability=ParameterStabilityParams(
        neighborhood_definition="adjacent_grid",
        min_neighborhood_performance_ratio=0.3,
        min_positive_neighbor_fraction=0.5,
        time_alignment_offsets=(MINUTE,),
    ),
    cost_stress=CostStressParams(
        cost_model=COST_MODEL.ref,
        fill_assumption="next-bar-open",
        stress_multipliers=(2.0,),
        reported_only_multipliers=(3.0,),
        delay_stress_bars=1,
        min_breakeven_cost_multiple=1.5,
    ),
)
#: TEST ONLY — explicit parameters for rules without a Profile field.
SMOKE_TEST_ONLY_PARAMS: Final = RobustnessParams(
    cscv_partitions=4,
    max_participation_rate=0.01,
    min_capacity=None,
    impact_coefficient=0.1,
    cross_asset_min_positive_fraction=None,
    max_undersampled_pnl_share=None,
)


# =========================================================================================
# fixture data: Binance-format 1m klines through the real ingestion path
# =========================================================================================


def _klines(base: int, seed: int) -> list[list[Any]]:
    """``BAR_COUNT`` contiguous Binance 12-slot klines with trend flips and volatility clusters.

    Deterministic integer LCG, exact ``Decimal`` prices (cents), strings as the venue sends them.
    """
    first_ms = int(FIRST_BAR.timestamp() * 1000)
    state = seed
    price = Decimal(base)
    cent = Decimal("0.01")
    out: list[list[Any]] = []
    for index in range(BAR_COUNT):
        state = (state * 6364136223846793005 + 1442695040888963407) % 2**64
        drift = Decimal(base) / 20_000 * (1 if (index // 45) % 2 == 0 else -1)
        scale = Decimal(1 + (index // 70) % 3)  # volatility clusters
        shock = Decimal(int((state >> 33) % 2001) - 1000) / 1000 * Decimal(base) / 2_000 * scale
        opened = price
        closed = (price + drift + shock).quantize(cent)
        wick = (Decimal(int((state >> 12) % 400)) / 100 * scale).quantize(cent)
        volume = Decimal(5 + int((state >> 24) % 50)) / 10
        out.append(
            kline_item(
                first_ms + index * ss.MINUTE_MS,
                open_=f"{opened:.8f}",
                high=f"{max(opened, closed) + wick:.8f}",
                low=f"{min(opened, closed) - wick:.8f}",
                close=f"{closed:.8f}",
                volume=f"{volume:.8f}",
                quote_volume=f"{(volume * closed).quantize(cent):.8f}",
                trades=50 + index % 17,
                taker_base=f"{volume / 2:.8f}",
                taker_quote=f"{(volume * closed / 2).quantize(cent):.8f}",
            )
        )
        price = closed
    return out


def _ingest(w: ds.World) -> None:
    """E2 listings, then archive + REST klines of both symbols, reports; nothing is faked."""
    w.listed(ds.TRADING, ds.L1)
    ingest_klines(w, BTC, tag="btc", base="0", items=_klines(36_000, seed=20231114))
    ingest_klines(w, ETH, tag="eth", base="0", items=_klines(2_000, seed=20231115))
    w.report("klines_1m", symbols=(BTC, ETH), listing=True)


def _assumed(spec: PointInTimeSpec) -> PointInTimeSpec:
    return spec.model_copy(
        update={"availability_bindings": (*spec.availability_bindings, ASSUMPTION_BINDING)}
    )


@pytest.fixture
def pg(tmp_path: Path) -> Iterator[ds.World]:
    with ds.postgres_world(tmp_path / "catalog", postgres_test_catalog_uri()) as opened:
        yield opened


# =========================================================================================
# the chain
# =========================================================================================


@dataclass(frozen=True)
class Chain:
    interval: DatasetBuilt
    point: DatasetBuilt
    pair: ManifestPair
    observations: tuple[FeatureObservation, ...]
    feature_requests: dict[str, FeatureRequest]
    features: dict[str, FeatureResult]
    state_request: StateRequest
    states: StateResult
    event_inputs: tuple[EventInputPoint, ...]
    event_request: EventRequest
    events: EventResult
    outcome_request: OutcomeRequest
    outcomes: OutcomeTable
    price_bars: DatasetPriceBars
    signals: tuple[SignalObservation, ...]
    evaluation: StrategyEvaluation
    context: ValidationContext
    report: ValidationReport
    robustness: RobustnessDiagnostic
    matrix: StateStrategyMatrix
    written: tuple[WrittenReport, ...]


def _feature(
    w: ds.World,
    built: DatasetBuilt,
    provider: BarLogReturnProvider | BarRealizedVolatilityProvider,
    spec: FeatureSpec,
    observations: tuple[FeatureObservation, ...],
    times: tuple[datetime, ...],
) -> tuple[FeatureRequest, FeatureResult]:
    request = feature_request_from_dataset(
        w.h.adapter,
        w.h.storage,
        builder=w.builder(),
        manifest_content_hash=built.manifest.content_hash(),
        pit_spec=built.manifest.point_in_time,
        observations=observations,
        feature=spec,
        evaluation_times=times,
    )
    return request, run_feature(provider, spec, request)


def _context(candidate: StrategyCandidate, datasets: tuple[DatasetBuilt, ...]) -> ValidationContext:
    spec, profile = candidate.spec, SMOKE_TEST_ONLY_PROFILE
    refs = {"hypothesis_ref": factories.hypothesis_ref(), "risk_policy_ref": None}
    repro = factories.repro_tuple(
        **refs,
        strategy_ref=spec.ref,
        outcome_ref=LABEL.outcome,
        cost_model_ref=COST_MODEL.ref,
        dataset_snapshots=tuple(built.manifest.dataset for built in datasets),
        validation_profile=profile.ref,
        validation_profile_hash=profile.content_hash(),
        dependency_hashes={
            str(refs["hypothesis_ref"]): factories.HASH_A,
            str(spec.ref): spec.content_hash(),
            str(LABEL.outcome): LABEL.outcome_spec_hash,
            str(COST_MODEL.ref): COST_MODEL.content_hash(),
        },
    )
    run = factories.experiment_run(repro=repro, state=RunState.COMPLETED)
    metadata = factories.experiment_metadata(
        experiment_hash=run.experiment_hash,
        validation_profile=profile.ref,
        validation_profile_hash=profile.content_hash(),
        hypothesis_family_id=candidate.hypothesis_family_id,
        family_trial_count=len(spec.param_search_space["lookback"])
        * len(spec.param_search_space["long_only"]),
    )
    return ValidationContext(
        report_id="rep-real-data-smoke",
        subject=spec.ref,
        run=run,
        metadata=metadata,
        profile=profile,
        cost_model=COST_MODEL,
        label_spec=LABEL,
        created_at=REPORT_TIME,
    )


def run_chain(w: ds.World, report_root: Path, registry_path: Path) -> Chain:
    # ---- F3: interval + point manifests over the same bars (built or replayed) ----
    interval = rt.build(
        w,
        _assumed(w.spec(interval=(DAY_START, ds.SIM), skip=rt.OWN)),
        data_type="klines_1m",
        window=(DAY_START, DAY_END),
    )
    point = rt.build(
        w, _assumed(w.spec(skip=rt.OWN)), data_type="klines_1m", window=(DAY_START, DAY_END)
    )
    # E1: one chain, one verified pair (both manifests reloaded through the verifying store).
    pair = pair_manifests(
        w.builder(), interval.manifest.content_hash(), point.manifest.content_hash()
    )

    # ---- F4: features over the interval manifest's proven BTC bars ----
    spec = interval.manifest.point_in_time
    selection = PitSelector(
        w.h.adapter, w.h.storage, canonical_scratch_directory=w.h.canonical_scratch_directory
    ).select(spec, "klines_1m", BTC, DAY_START, DAY_END)
    observations = bar_observations(selection, spec)
    # The P6 matrix keys returns by equity-point time (= bar interval_end), so every step is
    # evaluated on the bar-close grid. Under ADR-0032 a bar is visible 5 s after its close, so
    # at interval_end only the previous bar is known: causal, one bar behind (backlog E2).
    times = tuple(sorted({o.event_end_time for o in observations if o.event_end_time is not None}))
    lr_request, lr_result = _feature(
        w, interval, BarLogReturnProvider((LOG_RETURN,)), LOG_RETURN, observations, times
    )
    vol_request, vol_result = _feature(
        w,
        interval,
        BarRealizedVolatilityProvider((REALIZED_VOL,)),
        REALIZED_VOL,
        observations,
        times,
    )

    # ---- P2: volatility regime over the realized-volatility values ----
    s_request = state_request(VOL_REGIME, times, state_inputs([(vol_request, vol_result)]))
    states = run_state(VolatilityRegimeProvider((VOL_REGIME,)), VOL_REGIME, s_request)

    # ---- P3: state-switch events ----
    event_inputs = inputs_from_state_series(
        VOL_REGIME.ref, state_series_from_state_run(s_request, states)
    )
    e_request = EventRequest(
        event=SWITCH.ref, spec_hash=SWITCH.content_hash(), as_of=times[-1], inputs=event_inputs
    )
    events = run_events(StateSwitchProvider((SWITCH,)), SWITCH, e_request)

    # ---- P4: forward-return labels of the events over the point manifest's bars ----
    o_request = outcome_request_from_dataset(
        w.h.adapter,
        w.h.storage,
        builder=w.builder(),
        manifest_content_hash=point.manifest.content_hash(),
        symbol=BTC_C,
        label_spec=LABEL,
        events=tuple(
            OutcomeEvent(event_key=event.event_id, event_time=event.event_time)
            for event in events.events
        ),
    )
    outcomes = materialize(ForwardReturnOutcome((LABEL,)), o_request)

    # ---- P5: TSMOM on F4 log returns, BarBacktester on the point manifest's bars ----
    price_bars = backtest_bars_from_dataset(
        w.h.adapter,
        w.h.storage,
        builder=w.builder(),
        manifest_content_hash=point.manifest.content_hash(),
        symbols=(BTC_C,),
    )
    signals = signals_from_features(
        lr_result, feature=LOG_RETURN.ref, instrument=BTC_C, knowledge_time=times[-1]
    )
    candidate = library_entries()[0].candidate()
    inputs = EvaluationInputs(
        instruments=(BTC_C,),
        bars=price_bars.bars,
        decision_times=times,
        knowledge_cutoff=times[-1],
        cost_model=BACKTEST_COSTS,
        initial_equity=EQUITY,
        signals=signals,
        params=CHOSEN,
    )
    by_time = {value.evaluation_time: value.state for value in states.values}
    volumes = {
        (BTC_C, o.event_time): o.values["volume"]
        for o in observations
        if isinstance(o.values["volume"], Decimal)
    }
    # ---- P4 / P8: G0 – G4 under the TEST ONLY profile ----
    context = _context(candidate, (interval, point))
    setup = ValidatorSetup(
        context=context,
        outcome_provider=ForwardReturnOutcome((LABEL,)),
        manifest_content_hash=point.manifest.content_hash(),
        instrument=BTC_C,
        trials=CandidateTrialRunner(candidate, inputs, BarBacktester()),
        chosen_params=CHOSEN,
        seed=11,
        robustness=SMOKE_TEST_ONLY_PARAMS,
        state_of=lambda at: by_time.get(at) or "unknown",
        bar_volume=volumes,
        declared_instruments=(BTC_C,),
        market_benchmark=True,  # ADR-0060 enforced (C-T4 reported-only items)
        dataset_bars=price_bars,  # G0.manifest_binding: the labels' manifest is the bars' (E5)
        # E1 follow-up: the chain's pair binds the bars' manifest to the features' (the
        # signals carry no manifest hash, so each feature request's is passed explicitly).
        manifest_pair=pair,
        feature_manifest_hashes=(
            lr_request.manifest_content_hash,
            vol_request.manifest_content_hash,
        ),
    )
    validator = PipelineBacktestValidator(setup)
    evaluation = evaluate_strategy(
        candidate,
        inputs,
        backtester=BarBacktester(),
        registry=FailureRegistry(registry_path),
        validator=validator,
    )
    assert evaluation.backtest is not None and evaluation.validation is not None, evaluation
    report = evaluation.validation.report

    # ---- P8: G4 on the dataset-backed trials. ``validate`` builds its G4 input lazily and only
    # when G0 - G3 did not fail, so on a failing fixture G4 would never run; the validator's
    # public builder (``robustness_input``) is run as a report-only diagnostic (backlog E4) ----
    robustness = validator.robustness_diagnostic(candidate.spec, evaluation.backtest)

    # ---- P6: state x strategy matrix ----
    matrix = matrix_from_backtest(candidate.spec.ref, VOL_REGIME.ref, evaluation.backtest, states)

    # ---- reports ----
    written = (
        write_validation_report(report_root, report),
        write_state_strategy_matrix(report_root, matrix),
    )
    return Chain(
        interval=interval,
        point=point,
        pair=pair,
        observations=observations,
        feature_requests={"log_return": lr_request, "realized_vol": vol_request},
        features={"log_return": lr_result, "realized_vol": vol_result},
        state_request=s_request,
        states=states,
        event_inputs=event_inputs,
        event_request=e_request,
        events=events,
        outcome_request=o_request,
        outcomes=outcomes,
        price_bars=price_bars,
        signals=signals,
        evaluation=evaluation,
        context=context,
        report=report,
        robustness=robustness,
        matrix=matrix,
        written=written,
    )


def _hashes(chain: Chain) -> dict[str, str]:
    evaluation = chain.evaluation
    assert evaluation.strategy_result is not None and evaluation.backtest is not None
    return {
        "interval_manifest": chain.interval.manifest.content_hash(),
        "point_manifest": chain.point.manifest.content_hash(),
        "manifest_pair": chain.pair.pair_hash,
        "log_return_request": chain.feature_requests["log_return"].content_hash(),
        "log_return": chain.features["log_return"].result_hash,
        "realized_vol": chain.features["realized_vol"].result_hash,
        "states": chain.states.result_hash,
        "events": chain.events.result_hash,
        "outcome_request": chain.outcome_request.content_hash(),
        "outcomes": chain.outcomes.result_hash,
        "price_bars": content_hash([bar.content_hash() for bar in chain.price_bars.bars]),
        "strategy": evaluation.strategy_result.result_hash,
        "backtest": evaluation.backtest.result_hash,
        "report": chain.report.content_hash(),
        "robustness": content_hash([gate.content_hash() for gate in chain.robustness.gates]),
        "matrix": chain.matrix.matrix_hash,
    }


# =========================================================================================
# checks
# =========================================================================================


def _check_bindings(chain: Chain, store: ReportStore) -> None:
    interval, point = chain.interval.manifest, chain.point.manifest
    # F3 / E1: the verified pair binds exactly these two manifests (same snapshots, the same
    # ADR-0032 choice — here bound —, instruments and lineage; the point view at the interval end).
    assert (chain.pair.feature_manifest_hash, chain.pair.price_manifest_hash) == (
        interval.content_hash(),
        point.content_hash(),
    )
    assert ASSUMPTION_BINDING in interval.point_in_time.availability_bindings
    # What the pair implies, observed: the bars both paths prove are the same bars.
    closes = {o.event_time: o.values["close"] for o in chain.observations}
    assert {bar.interval_start: bar.close for bar in chain.price_bars.bars} == closes
    assert len(closes) == BAR_COUNT

    # F4: each request carries the verified interval manifest; each result answers its request.
    for name, request in chain.feature_requests.items():
        assert request.manifest_content_hash == interval.content_hash()
        assert sorted(o.content_hash() for o in request.observations) == sorted(
            o.content_hash() for o in chain.observations
        )
        assert chain.features[name].request_hash == request.content_hash()
    lineage = set(interval.lineage)
    assert all(o.lineage in lineage for o in chain.observations)

    # P2: every state input is a realized-vol value of that result.
    vol = chain.features["realized_vol"]
    assert {i.source_result_hash for i in chain.state_request.inputs} == {vol.result_hash}
    assert chain.states.request_hash == chain.state_request.content_hash()
    assert {v.state for v in chain.states.values} - {None} <= set(VOLATILITY_LABELS)

    # P3: events answer their request and cite input points of the state series.
    assert chain.events.request_hash == chain.event_request.content_hash()
    assert chain.events.events, "the fixture must produce at least one state switch"
    point_ids = {item.point_id for item in chain.event_inputs}
    for event in chain.events.events:
        assert event.input_ids and set(event.input_ids) <= point_ids

    # P4: labels answer the point-manifest request for exactly the P3 events.
    assert chain.outcome_request.manifest_content_hash == point.content_hash()
    assert [e.event_key for e in chain.outcome_request.events] == [
        e.event_id for e in chain.events.events
    ]
    assert chain.outcomes.label_spec_hash == LABEL.content_hash()
    assert {label.event_key for label in chain.outcomes.labels} == {
        e.event_id for e in chain.events.events
    }

    # P5: the signals are exactly the log-return values; the backtest runs on the proven bars.
    evaluation = chain.evaluation
    strategy, backtest = evaluation.strategy_result, evaluation.backtest
    assert strategy is not None and backtest is not None
    assert LOG_RETURN.ref == LOG_RETURN_SIGNAL
    assert {s.event_time: s.value for s in chain.signals} == {
        v.evaluation_time: v.value for v in chain.features["log_return"].values
    }
    assert chain.price_bars.manifest_content_hash == point.content_hash()
    assert backtest.final_equity > 0 and backtest.fills
    assert any(p.target_weight != 0 for p in strategy.positions)

    # P4 / P8: the report binds the run, the Profile and the manifests' dataset snapshots.
    report, view = chain.report, evaluation.validation.view if evaluation.validation else None
    assert report.validation_profile_hash == SMOKE_TEST_ONLY_PROFILE.content_hash()
    assert report.subject == library_entries()[0].spec.ref
    assert view is not None and view["extra"] == {
        "adapter": "research.strategies.validation.PipelineBacktestValidator",
        "instrument": BTC_C,
        "backtest_result_hash": backtest.result_hash,
        "price_binding": {
            "mode": PRICE_BINDING_DATASET,
            "manifest_content_hash": point.content_hash(),
            "price_cutoff": chain.price_bars.price_cutoff.isoformat(),
            "verified": True,
            "mismatches": [],
            "manifest_pair": {
                "feature_manifest_hash": interval.content_hash(),
                "price_manifest_hash": point.content_hash(),
                "pair_hash": chain.pair.pair_hash,
            },
            "feature_manifest_hashes": [interval.content_hash(), interval.content_hash()],
        },
    }
    # E5 + E1 follow-up: the labels' manifest is verified to be the backtest bars', and the
    # chain's pair binds those bars' manifest to the features' (G0 adapter gate).
    binding = next(g for g in report.gates if g.gate_id == "G0.manifest_binding")
    assert binding.verdict is Verdict.PASS and binding.value == 0.0
    run = chain.context.run
    assert (report.run_id, report.experiment_hash) == (run.run_id, run.experiment_hash)
    assert run.repro.dataset_snapshots == (interval.dataset, point.dataset)
    assert report.created_at == REPORT_TIME  # the context's stamp, not the wall clock
    stages = {gate.gate_id.split(".")[0] for gate in report.gates}
    assert {"G0", "G1"} <= stages, sorted(g.gate_id for g in report.gates)
    # A FAIL is filed in the Failure Registry, bound to the report and to the backtest.
    if report.verdict is Verdict.FAIL:
        failure = evaluation.failure
        assert failure is not None and failure.subject_ref == report.subject
        assert f"validation_report:{report.report_id}" in failure.evidence
        assert f"backtest_result:{backtest.result_hash}" in failure.evidence
    else:
        assert evaluation.failure is None
    # P8 (G4 diagnostic, report only): only G4 gates, each with a defined verdict, about this
    # backtest; when ``validate`` ran G4 itself, it produced exactly these gates.
    assert chain.robustness.mode == DIAGNOSTIC_MODE
    assert chain.robustness.backtest_result_hash == backtest.result_hash
    g4 = chain.robustness.gates
    assert g4 and all(g.gate_id.startswith("G4.") and g.verdict in set(Verdict) for g in g4)
    if "G4" in stages:
        assert tuple(g for g in report.gates if g.gate_id.startswith("G4.")) == g4

    # P6: the matrix names both inputs.
    assert chain.matrix.backtest_result_hash == backtest.result_hash
    assert chain.matrix.state_result_hash == chain.states.result_hash

    # Reports: ids are the objects' own content hashes; the console store reads them back.
    written_report, written_matrix = chain.written
    assert written_report.id == report.content_hash()
    assert written_matrix.id == chain.matrix.matrix_hash
    got = store.get(ReportKind.VALIDATION_REPORT, written_report.id)
    assert ValidationReport.model_validate(got.payload).content_hash() == written_report.id
    assert got.payload["verdict"] == report.verdict.value
    got_matrix = store.get(ReportKind.STATE_STRATEGY_MATRIX, written_matrix.id)
    assert got_matrix.payload["backtest_result_hash"] == backtest.result_hash
    assert got_matrix.payload["state_result_hash"] == chain.states.result_hash
    listed = {env.id for env in store.list(ReportKind.VALIDATION_REPORT)}
    assert written_report.id in listed


def _check_cutoffs(chain: Chain) -> None:
    interval, point = chain.interval.manifest.point_in_time, chain.point.manifest.point_in_time
    # F3 / F4: every observation is known by the cutoff and entered at the assumption's time.
    for o in chain.observations:
        assert o.knowledge_time <= interval.knowledge_cutoff
        assert o.event_end_time is not None
        assert o.available_time == o.event_end_time + ASSUMPTION_LATENCY
    for name, request in chain.feature_requests.items():
        lag = LOG_RETURN.available_lag if name == "log_return" else REALIZED_VOL.available_lag
        for value in chain.features[name].values:
            visible = request.visible_at(value.evaluation_time, lag)
            assert all(o.available_time + lag <= value.evaluation_time for o in visible)
            if value.latest_input_available_time is not None:
                assert value.latest_input_available_time + lag <= value.evaluation_time
    # P2: no state uses an input later than its evaluation time.
    for state in chain.states.values:
        if state.latest_input_time is not None:
            assert state.latest_input_time <= state.evaluation_time
    # P3: every event is as of its request and cites only points available by its time.
    points = {item.point_id: item for item in chain.event_inputs}
    for event in chain.events.events:
        assert event.event_time <= chain.event_request.as_of
        assert all(points[i].available_time <= event.event_time for i in event.input_ids)
    # P4: bars and labels stop at the price cutoff, itself inside the point manifest's view.
    cutoff = chain.outcome_request.price_cutoff
    assert point.simulation_time is not None and cutoff <= point.simulation_time
    assert all(bar.available_time <= cutoff for bar in chain.outcome_request.bars)
    for label in chain.outcomes.labels:
        if label.available_time is not None:
            assert label.available_time <= cutoff
    # P5: targets use only signals available at the decision; fills never precede it.
    evaluation = chain.evaluation
    assert evaluation.strategy_result is not None and evaluation.backtest is not None
    assert all(bar.available_time <= chain.price_bars.price_cutoff for bar in chain.price_bars.bars)
    for position in evaluation.strategy_result.positions:
        if position.latest_input_available_time is not None:
            assert position.latest_input_available_time <= position.decision_time
    for fill in evaluation.backtest.fills:
        assert fill.fill_time >= fill.decision_time


# =========================================================================================
# the test
# =========================================================================================

_STATUS: Final = {
    Verdict.PASS: EvaluationStatus.PASSED,
    Verdict.INCONCLUSIVE: EvaluationStatus.INCONCLUSIVE,
    Verdict.FAIL: EvaluationStatus.REJECTED,
}


def test_research_pipeline_runs_end_to_end_on_a_real_format_dataset(
    pg: ds.World, tmp_path: Path
) -> None:
    _ingest(pg)
    reports = tmp_path / "reports"
    store = ReportStore(reports)

    first = run_chain(pg, reports, tmp_path / "failures-1.jsonl")
    assert not first.interval.replayed and not first.point.replayed
    _check_bindings(first, store)
    _check_cutoffs(first)
    # A defined verdict, and the pipeline status that verdict maps to. Never asserted to PASS.
    verdict = first.report.verdict
    assert verdict in set(Verdict)
    assert first.evaluation.status is _STATUS[verdict]
    # Whatever the verdict, nothing here is promotable: no sealed-OOS (G5) run exists.
    assert first.evaluation.promotion_blocked_reason is not None
    assert all(w.written for w in first.written)

    # A fresh process on the same catalog: the manifests replay; every hash is identical and
    # the reports are the same files (idempotent no-op writes).
    pg.h.reopen()
    second = run_chain(pg, reports, tmp_path / "failures-2.jsonl")
    assert second.interval.replayed and second.point.replayed
    assert _hashes(second) == _hashes(first)
    assert [w.id for w in second.written] == [w.id for w in first.written]
    assert not any(w.written for w in second.written)

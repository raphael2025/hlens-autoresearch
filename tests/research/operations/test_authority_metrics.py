"""ADR-0098 §3 / ADR-0100 item 3: the closed monitoring-metric registry of
``research.operations.authority`` (``MONITORING_METRIC_DEFINITIONS``).

A Profile metric is monitorable only through the one registry definition for the gate its
baseline cites; anything else is refused by name (``metric_undefined``; never a guessed rule). A
``window_returns`` definition is the same G4 validation function on the window's return series
(compared here with a direct call of that function — no formula is re-implemented by the
tests); a ``window_validation`` definition needs the baseline validator's inputs
(``metric_inputs_unavailable`` without them). Returns come from a real ``BarBacktester`` run on
TEST ONLY bars; every number is arbitrary.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from core.contracts.strategy import BacktestCostModel, BacktestRequest
from core.domain.research import GateResult, Verdict
from plugins.backtest import BarBacktester
from research.operations.authority import (
    BASELINE_BINDING_MISMATCH,
    EVIDENCE_WINDOW_RETURNS,
    EVIDENCE_WINDOW_VALIDATION,
    METRIC_INPUTS_UNAVAILABLE,
    METRIC_REFUSED,
    METRIC_REGISTRY_ID,
    METRIC_UNDEFINED,
    MONITORING_METRIC_DEFINITIONS,
    MONITORING_METRICS,
    REFUSED_METRIC_GATES,
    SCOPE_WALK_FORWARD_FOLDS,
    SCOPE_WALK_FORWARD_WINDOWS,
    AuthorityRefused,
    RuledMetric,
    _check_baseline_gates,
    _gate_value,
    _ruled_definitions,
    _WindowInputs,
)
from research.validation.returns import PeriodReturns, from_backtest
from research.validation.robustness import cost_stress_check
from tests.promotion.fixtures import toy_profile
from tests.research.operations import authority_fixtures as af

START = datetime(2023, 11, 14, 22, 5, tzinfo=UTC)
PROFILE = af.profile()
FLOAT_PROFILE = toy_profile()  # no exact field at all: float-only thresholds


def _returns(*, trade: bool = True) -> PeriodReturns:
    bars = af.bars(("BTC-USDT",), START, 12)
    request = BacktestRequest(
        cost_model=BacktestCostModel(
            name=af.COST_MODEL.name,
            version=af.COST_MODEL.version,
            fee_rate=af.COST_MODEL.fee_rate_per_side,
            slippage_rate=af.COST_MODEL.slippage_rate_per_side,
        ),
        initial_equity=af.INITIAL_EQUITY,
        bars=bars,
        targets=af.long_targets(bars) if trade else (),
    )
    return from_backtest(BarBacktester().run(request))


def _definition(metric: str, gate_id: str) -> RuledMetric:
    [definition] = [d for d in MONITORING_METRICS[metric] if d.matches(gate_id)]
    return RuledMetric(definition=definition, gate_id=gate_id)


def _ruled(
    gate_ids: Mapping[str, str], thresholds: Mapping[str, str] | None = None
) -> tuple[RuledMetric, ...]:
    bound = af.profile(thresholds or dict.fromkeys(gate_ids, "0.5"))
    run = af.baseline_run(bound)
    report = af.report_of(bound, run)
    return _ruled_definitions(bound, af.baseline_set(report, gate_ids))


def _refused(code: str, gate_ids: Mapping[str, str], **kwargs: object) -> str:
    with pytest.raises(AuthorityRefused) as refused:
        _ruled(gate_ids, **kwargs)  # type: ignore[arg-type]
    assert refused.value.code == code
    return str(refused.value)


# ---- the registry itself ------------------------------------------------------------------


def test_the_registry_is_closed_versioned_and_indexed_by_metric() -> None:
    assert METRIC_REGISTRY_ID == "hlens.p11.monitoring-metrics@2.0.0"
    refs = [d.definition_ref for d in MONITORING_METRIC_DEFINITIONS]
    assert len(refs) == len(set(refs))  # one versioned id per definition
    assert set(MONITORING_METRICS) == {d.metric_name for d in MONITORING_METRIC_DEFINITIONS}
    assert sum(len(v) for v in MONITORING_METRICS.values()) == len(MONITORING_METRIC_DEFINITIONS)
    with pytest.raises(TypeError):
        MONITORING_METRICS["invented_metric"] = ()  # type: ignore[index]
    for definition in MONITORING_METRIC_DEFINITIONS:
        assert definition.evidence in (EVIDENCE_WINDOW_RETURNS, EVIDENCE_WINDOW_VALIDATION)
        # the gate's exact metric label is the metric name with its comparison
        assert re.fullmatch(r"[a-z0-9_]+\[(<=|>=)\]", definition.baseline_gate_metric)
        assert definition.payload()["definition"] == definition.definition_ref
        # only the definitions over the Profile's fixed walk-forward windows / folds need the
        # observation window inside the research window (D-P11-WINDOW)
        fixed = definition.window_scope in (SCOPE_WALK_FORWARD_FOLDS, SCOPE_WALK_FORWARD_WINDOWS)
        assert definition.requires_research_window is fixed


def test_only_the_g4_functions_of_the_return_series_need_no_binding() -> None:
    returns_only = {
        (d.metric_name, d.baseline_gate_id)
        for d in MONITORING_METRIC_DEFINITIONS
        if d.evidence == EVIDENCE_WINDOW_RETURNS
    }
    assert returns_only == {
        ("breakeven_cost_multiple", "G4.cost_stress.breakeven"),
        ("breakeven_cost_multiple_vs_stress", "G4.cost_stress.<i>"),
        ("positive_window_fraction", "G4.walk_forward.positive_fraction"),
        ("max_window_pnl_share", "G4.walk_forward.max_window_share"),
    }


@pytest.mark.parametrize(
    ("metric", "gate_id", "matches"),
    [
        ("breakeven_cost_multiple", "G4.cost_stress.breakeven", True),
        ("breakeven_cost_multiple", "G2.breakeven_cost_multiple", True),
        ("breakeven_cost_multiple", "G2.breakeven_cost_multiple.instrument.BTC-USDT", True),
        ("breakeven_cost_multiple", "G4.cost_stress.breakeven.instrument.BTC-USDT", False),
        ("breakeven_cost_multiple", "G4.cost_stress.0", False),
        ("breakeven_cost_multiple_vs_stress", "G4.cost_stress.0", True),
        ("breakeven_cost_multiple_vs_stress", "G4.cost_stress.12", True),
        ("breakeven_cost_multiple_vs_stress", "G4.cost_stress.01", False),
        ("shuffle_timing_p_value", "G1.shuffle_control.seed.-3", True),
        ("shuffle_timing_p_value", "G1.shuffle_control.seed.4.instrument.ETH-USDT", True),
        ("shuffle_timing_p_value", "G1.shuffle_control.seed.x", False),
        ("positive_window_fraction", "G4.walk_forward.positive_fraction", True),
    ],
)
def test_a_definition_matches_exactly_its_gate_templates(
    metric: str, gate_id: str, matches: bool
) -> None:
    assert any(d.matches(gate_id) for d in MONITORING_METRICS[metric]) is matches


# ---- ruled definitions: known, unknown, refused gates ------------------------------------


def test_a_known_metric_is_bound_to_the_one_definition_of_its_baseline_gate() -> None:
    [ruled] = _ruled({af.METRIC: af.GATE_ID})
    assert isinstance(ruled, RuledMetric)
    assert ruled.gate_id == af.GATE_ID
    assert ruled.definition.definition_ref == "hlens.p11.metric.breakeven_cost_multiple@1.0.0"
    assert ruled.definition.evidence == EVIDENCE_WINDOW_RETURNS
    assert ruled.payload()["gate_id"] == af.GATE_ID


def test_the_same_metric_label_of_another_gate_selects_another_definition() -> None:
    [ruled] = _ruled({af.METRIC: "G2.breakeven_cost_multiple"})
    assert ruled.definition.baseline_gate_id == "G2.breakeven_cost_multiple"
    assert ruled.definition.evidence == EVIDENCE_WINDOW_VALIDATION
    assert ruled.definition.requires_research_window


def test_an_unknown_metric_is_refused_by_name() -> None:
    message = _refused(METRIC_UNDEFINED, {"invented_sharpe": "G4.invented"})
    assert "'invented_sharpe'" in message and METRIC_REGISTRY_ID in message


def test_a_known_metric_of_a_gate_the_registry_does_not_define_is_refused() -> None:
    message = _refused(METRIC_UNDEFINED, {af.METRIC: "G4.cost_stress.0"})
    assert "not for baseline gate 'G4.cost_stress.0'" in message


@pytest.mark.parametrize(
    "gate_id",
    [
        "G5.sealed_oos.breakeven",
        "G2.market_benchmark.buy_and_hold_equal_weight",
        "G2.inverse_control",
        "G2.cost_report.net_total",
    ],
)
def test_sealed_and_reported_only_gates_are_always_undefined(gate_id: str) -> None:
    message = _refused(METRIC_UNDEFINED, {af.METRIC: gate_id})
    [reason] = [reason for prefix, reason in REFUSED_METRIC_GATES if gate_id.startswith(prefix)]
    assert reason in message


def test_a_ruled_metric_the_baseline_set_does_not_cite_is_a_binding_mismatch() -> None:
    # the Profile rules two metrics; the baseline set cites a gate for only one of them
    bound = af.profile({af.METRIC: "0.5", "max_window_pnl_share[<=]": "0.1"})
    report = af.report_of(bound, af.baseline_run(bound))
    with pytest.raises(AuthorityRefused) as refused:
        _ruled_definitions(bound, af.baseline_set(report, {af.METRIC: af.GATE_ID}))
    assert refused.value.code == BASELINE_BINDING_MISMATCH
    assert "max_window_pnl_share" in str(refused.value)


def test_several_ruled_metrics_are_bound_in_metric_order() -> None:
    ruled = _ruled(
        {
            af.METRIC: af.GATE_ID,
            "breakeven_cost_multiple_vs_stress": "G4.cost_stress.0",
        }
    )
    assert [item.metric_name for item in ruled] == [
        "breakeven_cost_multiple",
        "breakeven_cost_multiple_vs_stress",
    ]


# ---- the baseline report must carry the cited gate --------------------------------------


def test_the_baseline_report_must_carry_the_cited_gate_with_its_label_and_exact_value() -> None:
    ruled = (_definition(af.METRIC, af.GATE_ID),)
    run = af.baseline_run(PROFILE)
    _check_baseline_gates(af.report_of(PROFILE, run), ruled)  # one exact gate: fine
    for gates in (
        (af.gate("3.5", gate_id="G4.cost_stress.0"),),  # no such gate
        (af.gate("3.5", label="no_cost_no_trades"),),  # another label: not the metric
        (GateResult(gate_id=af.GATE_ID, metric=af.GATE_LABEL, value=3.5, verdict=Verdict.PASS),),
    ):
        with pytest.raises(AuthorityRefused) as refused:
            _check_baseline_gates(af.report_of(PROFILE, run, gates=gates), ruled)
        assert refused.value.code == BASELINE_BINDING_MISMATCH


# ---- computing a defined metric -----------------------------------------------------------


def test_a_returns_definition_is_the_g4_function_on_the_window_series() -> None:
    returns = _returns()
    expected = {gate.gate_id: gate for gate in cost_stress_check(PROFILE, returns).gates}
    inputs = _WindowInputs(PROFILE, returns, None)
    breakeven = _definition(af.METRIC, af.GATE_ID)
    value = breakeven.definition.compute(inputs, af.GATE_ID)
    assert value is not None and value == expected[af.GATE_ID].value_exact
    stress = _definition("breakeven_cost_multiple_vs_stress", "G4.cost_stress.0")
    assert stress.definition.compute(inputs, "G4.cost_stress.0") == (
        expected["G4.cost_stress.0"].value_exact
    )


def test_the_functions_own_insufficient_evidence_variant_is_missing() -> None:
    returns = _returns(trade=False)  # no trade, no cost: cost_stress_check's no_cost_no_trades
    [gate] = [g for g in cost_stress_check(PROFILE, returns).gates if g.gate_id == af.GATE_ID]
    assert gate.metric == "no_cost_no_trades"
    breakeven = _definition(af.METRIC, af.GATE_ID)
    assert breakeven.definition.compute(_WindowInputs(PROFILE, returns, None), af.GATE_ID) is None


def test_a_float_only_profile_threshold_gives_no_exact_value_and_is_refused() -> None:
    assert FLOAT_PROFILE.cost_stress.min_breakeven_cost_multiple_exact is None
    breakeven = _definition(af.METRIC, af.GATE_ID)
    with pytest.raises(AuthorityRefused) as refused:
        breakeven.definition.compute(_WindowInputs(FLOAT_PROFILE, _returns(), None), af.GATE_ID)
    assert refused.value.code == METRIC_REFUSED
    assert "float-only" in str(refused.value)


def test_a_validation_definition_without_a_binding_is_inputs_unavailable() -> None:
    g2 = _definition(af.METRIC, "G2.breakeven_cost_multiple")
    with pytest.raises(AuthorityRefused) as refused:
        g2.definition.compute(_WindowInputs(PROFILE, _returns(), None), g2.gate_id)
    assert refused.value.code == METRIC_INPUTS_UNAVAILABLE


def test_a_validation_definition_reads_the_window_re_run_once() -> None:
    calls: list[int] = []
    gate_id = "G2.breakeven_cost_multiple"
    label = "breakeven_cost_multiple[>=]"

    def rerun() -> Mapping[str, GateResult]:
        calls.append(1)
        return {gate_id: af.gate("2.75", gate_id=gate_id, label=label)}

    inputs = _WindowInputs(PROFILE, _returns(), rerun)
    g2 = _definition(af.METRIC, gate_id)
    assert g2.definition.compute(inputs, gate_id) == Decimal("2.75")
    assert g2.definition.compute(inputs, gate_id) == Decimal("2.75")
    assert calls == [1]  # the re-run is computed once and shared by every definition
    # a gate the re-run did not produce (its stage did not run) is missing, as in validation
    g3 = _definition("net_mean_hac_p_greater_adjusted", "G3.adjusted_p_value")
    assert g3.definition.compute(inputs, "G3.adjusted_p_value") is None


def test_gate_value_semantics() -> None:
    exact = af.gate("1.5")
    assert _gate_value([exact], af.GATE_ID, af.GATE_LABEL, source="t", absent_is_missing=False) == (
        Decimal("1.5")
    )
    assert _gate_value({}, af.GATE_ID, af.GATE_LABEL, source="t", absent_is_missing=True) is None
    with pytest.raises(AuthorityRefused) as absent:
        _gate_value([], af.GATE_ID, af.GATE_LABEL, source="t", absent_is_missing=False)
    assert absent.value.code == METRIC_REFUSED
    with pytest.raises(AuthorityRefused) as twice:
        _gate_value([exact, exact], af.GATE_ID, af.GATE_LABEL, source="t", absent_is_missing=True)
    assert twice.value.code == METRIC_REFUSED
    float_only = GateResult(
        gate_id=af.GATE_ID, metric=af.GATE_LABEL, value=1.5, verdict=Verdict.PASS
    )
    with pytest.raises(AuthorityRefused) as inexact:  # a float value is never used
        _gate_value([float_only], af.GATE_ID, af.GATE_LABEL, source="t", absent_is_missing=False)
    assert inexact.value.code == METRIC_REFUSED and "no exact value" in str(inexact.value)
    other = af.gate("1.5", label="too_few_trades")
    assert _gate_value([other], af.GATE_ID, af.GATE_LABEL, source="t", absent_is_missing=False) is (
        None
    )

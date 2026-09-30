from __future__ import annotations

import dataclasses
import json
from datetime import UTC, datetime, timedelta
from typing import cast

import pytest

from core.domain.base import FrozenMapping, Kind, Ref, VersionedSpec, content_hash
from core.domain.specs import (
    ConditionedStrategy,
    EnsembleStrategy,
    EventSpec,
    FeatureSpec,
    Instrument,
    InstrumentType,
    NegatedStrategy,
    StateSpec,
    StrategySpec,
)
from research.hypotheses.plan_bindings import PlanBindingRefused, produce_lowered_output_bindings
from research.hypotheses.typed_plan import (
    PLAN_FORMAT_VERSION,
    SUPPORTED_PLAN_FORMAT_VERSIONS,
    PlanLimits,
    PlanRejected,
    TypedPlan,
    parse_plan_json,
)
from research.hypotheses.typed_plan_lowering import OperatorLoweringRefused, lower_typed_plan
from research.hypotheses.typed_plan_resolver import (
    DirectReferenceResolution,
    resolve_direct_references,
)

NOW = datetime(2026, 9, 28, tzinfo=UTC)
SOURCE_A = FeatureSpec(
    name="feature_a",
    version="1.0.0",
    created_at=NOW,
    definition="source a",
    inputs=(Ref(kind=Kind.REPRESENTATION, name="bars", version="1.0.0"),),
    params=FrozenMapping({}),
    available_lag=timedelta(0),
)
SOURCE_B = FeatureSpec(
    name="feature_b",
    version="1.0.0",
    created_at=NOW,
    definition="source b",
    inputs=(Ref(kind=Kind.REPRESENTATION, name="bars", version="1.0.0"),),
    params=FrozenMapping({}),
    available_lag=timedelta(0),
)
EV_FEATURE = Ref(kind=Kind.FEATURE, name="ev_feature", version="1.0.0")
EV_FEATURE_2 = Ref(kind=Kind.FEATURE, name="ev_feature_2", version="1.0.0")
BAR_1M = Ref(kind=Kind.REPRESENTATION, name="canonical_bar_1m", version="1.0.0")
BAR_5M = Ref(kind=Kind.REPRESENTATION, name="canonical_bar_5m", version="1.0.0")
#: Two distinct EventSpecs on the same declared bar spec (ADR-0088 decision 1). The second event
#: is observable later than the first, so the combined event's visibility is provable.
EVENT_A = EventSpec(
    name="event_a",
    version="1.0.0",
    created_at=NOW,
    trigger="event_a_trigger",
    features=(EV_FEATURE,),
    observable_lag=timedelta(minutes=1),
    bar_spec=BAR_1M,
)
EVENT_B = EventSpec(
    name="event_b",
    version="1.0.0",
    created_at=NOW,
    trigger="event_b_trigger",
    features=(EV_FEATURE, EV_FEATURE_2),
    observable_lag=timedelta(minutes=2),
    bar_spec=BAR_1M,
)
STATE_REGIME = StateSpec(
    name="regime",
    version="1.0.0",
    created_at=NOW,
    features=(EV_FEATURE,),
    state_space=("high", "low"),
    method="threshold",
)
RISK_A = Ref(kind=Kind.RISK, name="risk_a", version="1.0.0")
RISK_B = Ref(kind=Kind.RISK, name="risk_b", version="1.0.0")
BTC_SPOT = Instrument(
    venue="binance",
    symbol="BTCUSDT",
    instrument_type=InstrumentType.SPOT,
    base="BTC",
    quote="USDT",
)
ETH_SPOT = Instrument(
    venue="binance",
    symbol="ETHUSDT",
    instrument_type=InstrumentType.SPOT,
    base="ETH",
    quote="USDT",
)
STRATEGY_A = StrategySpec(
    name="strategy_a",
    version="1.0.0",
    created_at=NOW,
    signals=(EV_FEATURE,),
    risk_policy=RISK_A,
    applicable_instruments=(BTC_SPOT,),
)
STRATEGY_B = StrategySpec(
    name="strategy_b",
    version="1.0.0",
    created_at=NOW,
    signals=(EV_FEATURE_2, EV_FEATURE),
    risk_policy=RISK_A,
    applicable_instruments=(BTC_SPOT,),
)
LIMITS = PlanLimits(max_depth=4, max_nodes=6, max_json_bytes=8192, max_parameters_per_node=2)


class _Resolver:
    def __init__(self, specs: tuple[VersionedSpec, ...]) -> None:
        self.specs = {str(spec.ref): spec for spec in specs}

    def resolve(self, ref: Ref) -> VersionedSpec | None:
        return self.specs.get(str(ref))


def _plan(
    operator: str = "interaction",
    *,
    transform: str = "difference",
    window: int = 5,
    include_window: bool = True,
    buckets: object = None,
    schema_version: str = "1.1.0",
) -> tuple[TypedPlan, DirectReferenceResolution]:
    resolver_specs: tuple[VersionedSpec, ...]
    inputs: list[dict[str, object]]
    parameters: dict[str, object]
    if operator == "interaction":
        inputs = [
            {"ref": str(SOURCE_A.ref), "content_hash": SOURCE_A.content_hash()},
            {"ref": str(SOURCE_B.ref), "content_hash": SOURCE_B.content_hash()},
        ]
        parameters = {}
        resolver_specs = (SOURCE_A, SOURCE_B)
    elif operator == "transformation":
        inputs = [{"ref": str(SOURCE_A.ref), "content_hash": SOURCE_A.content_hash()}]
        parameters = {"transform": transform}
        if include_window:
            parameters["window"] = window
        if buckets is not None:
            parameters["buckets"] = buckets
        resolver_specs = (SOURCE_A, SOURCE_B)
    elif operator == "temporal":
        inputs = [
            {"ref": str(EVENT_A.ref), "content_hash": EVENT_A.content_hash()},
            {"ref": str(EVENT_B.ref), "content_hash": EVENT_B.content_hash()},
        ]
        parameters = {"window": 3, "time_unit": "bar"}
        resolver_specs = (EVENT_A, EVENT_B)
    elif operator == "negation":
        inputs = [{"ref": str(STRATEGY_A.ref), "content_hash": STRATEGY_A.content_hash()}]
        parameters = {}
        resolver_specs = (STRATEGY_A,)
    else:
        raise ValueError(f"unsupported test operator {operator!r}")
    payload = {
        "schema_version": schema_version,
        "root": "combined",
        "nodes": [
            {
                "id": "combined",
                "operator": operator,
                "inputs": inputs,
                "parameters": parameters,
            }
        ],
    }
    plan = parse_plan_json(
        json.dumps(payload, separators=(",", ":")),
        limits=PlanLimits(
            max_depth=2,
            max_nodes=2,
            max_json_bytes=2048,
            # transform + window + buckets (quantile, ADR-0099).
            max_parameters_per_node=3,
        ),
    )
    resolution = resolve_direct_references(plan, resolver=_Resolver(resolver_specs))
    return plan, resolution


def _spec_input(spec: VersionedSpec) -> dict[str, str]:
    return {"ref": str(spec.ref), "content_hash": spec.content_hash()}


def _node(
    node_id: str,
    operator: str,
    inputs: list[dict[str, str]],
    parameters: dict[str, object] | None = None,
) -> dict[str, object]:
    return {"id": node_id, "operator": operator, "inputs": inputs, "parameters": parameters or {}}


def _multi(
    nodes: list[dict[str, object]],
    specs: tuple[VersionedSpec, ...],
    *,
    root: str,
) -> tuple[TypedPlan, DirectReferenceResolution]:
    """Parse a multi-node plan and resolve its direct references against ``specs``."""
    payload = {"schema_version": "1.1.0", "root": root, "nodes": nodes}
    plan = parse_plan_json(json.dumps(payload, separators=(",", ":")), limits=LIMITS)
    return plan, resolve_direct_references(plan, resolver=_Resolver(specs))


def _temporal(
    first: EventSpec, second: EventSpec, *, window: int = 3, time_unit: str = "bar"
) -> tuple[TypedPlan, DirectReferenceResolution]:
    return _multi(
        [
            _node(
                "seq",
                "temporal",
                [_spec_input(first), _spec_input(second)],
                {"window": window, "time_unit": time_unit},
            )
        ],
        (first, second),
        root="seq",
    )


def test_interaction_lowers_to_deterministic_feature_spec() -> None:
    plan, resolution = _plan()

    first = lower_typed_plan(plan, resolution=resolution, created_at=NOW)
    second = lower_typed_plan(plan, resolution=resolution, created_at=NOW)

    assert tuple(first) == ("combined",)
    output = cast(FeatureSpec, first["combined"])
    assert type(output) is FeatureSpec
    assert output.ref == second["combined"].ref
    assert output.content_hash() == second["combined"].content_hash()
    later = lower_typed_plan(
        plan,
        resolution=resolution,
        created_at=datetime(2026, 9, 29, tzinfo=UTC),
    )["combined"]
    assert output.ref != later.ref
    assert output.definition == "p7.interaction.product@1.0.0"
    assert output.inputs == (SOURCE_A.ref, SOURCE_B.ref)
    assert output.lineage == (SOURCE_A.ref, SOURCE_B.ref)
    assert output.params == FrozenMapping(
        {
            "alignment": "exact_evaluation_time",
            "missing": "propagate_none",
            "numeric_domain": "decimal_or_int_excluding_bool",
            "operator": "product",
            "provider": "p7_interaction_product@1.0.0",
            "semantic_version": "1.0.0",
        }
    )
    assert output.available_lag == timedelta(0)
    assert plan.runnable is False
    bindings = produce_lowered_output_bindings(
        experiment_hash="a" * 64,
        plan=plan,
        specs_by_node=first,
    )
    assert len(bindings) == len(plan.nodes)
    assert bindings[0].node_id == "combined"
    assert bindings[0].spec.content_hash() == output.content_hash()


def test_open_operator_refuses_without_partial_output() -> None:
    # The first node (interaction) is lowerable; the second (rank in a "1.1.0" plan) is still OPEN
    # (ADR-0082; ADR-0099 decision 4 keeps old plans' meaning), so the whole plan is refused and
    # no partial node map escapes.
    plan, resolution = _multi(
        [
            _node("product", "interaction", [_spec_input(SOURCE_A), _spec_input(SOURCE_B)]),
            _node(
                "ranked",
                "transformation",
                [{"node": "product"}],
                {"transform": "rank", "window": 3},
            ),
        ],
        (SOURCE_A, SOURCE_B),
        root="ranked",
    )

    with pytest.raises(OperatorLoweringRefused, match="operator_open") as error:
        lower_typed_plan(plan, resolution=resolution, created_at=NOW)

    assert error.value.operator == "transformation"
    assert error.value.node_id == "ranked"
    assert plan.runnable is False


def test_lowering_requires_resolution_for_same_plan() -> None:
    plan, resolution = _plan()
    other_plan, _ = _plan("negation")

    with pytest.raises(OperatorLoweringRefused, match="plan_resolution_mismatch"):
        lower_typed_plan(other_plan, resolution=resolution, created_at=NOW)


# --- temporal (ADR-0088 decision 1, pending-decisions §3 option A) ---------------------------


def test_temporal_lowers_to_event_spec_with_exact_declaration() -> None:
    plan, resolution = _plan("temporal")

    outputs = lower_typed_plan(plan, resolution=resolution, created_at=NOW)

    output = cast(EventSpec, outputs["combined"])
    assert type(output) is EventSpec
    assert output.bar_spec == BAR_1M
    # Visible when the second event is visible: EVENT_B's lag (2 min), not EVENT_A's (1 min).
    assert output.observable_lag == timedelta(minutes=2)
    # Inputs: ordered union by target identity (EVENT_A's ev_feature, then EVENT_B's new one).
    assert output.features == (EV_FEATURE, EV_FEATURE_2)
    assert output.states == ()
    assert output.lineage == (EVENT_A.ref, EVENT_B.ref)
    assert json.loads(output.trigger) == {
        "bar_spec": "representation:canonical_bar_1m@1.0.0",
        "definition": "p7.temporal.sequence_within_bars@1.0.0",
        "event_time": "second_event_time",
        "first_event": "event:event_a@1.0.0",
        "interval": "left_open_right_closed",
        "missing": "no_event",
        "operator": "temporal_sequence",
        "provider": "p7_temporal_sequence@1.0.0",
        "second_event": "event:event_b@1.0.0",
        "semantic_version": "1.0.0",
        "visibility": "second_event_observable_time",
        "window_bars": 3,
    }
    assert plan.runnable is False
    bindings = produce_lowered_output_bindings(
        experiment_hash="c" * 64, plan=plan, specs_by_node=outputs
    )
    assert [binding.node_id for binding in bindings] == ["combined"]
    assert bindings[0].spec.content_hash() == output.content_hash()


def test_temporal_output_identity_is_hand_computable() -> None:
    plan, resolution = _plan("temporal")
    (node,) = plan.nodes

    output = lower_typed_plan(plan, resolution=resolution, created_at=NOW)["combined"]

    expected = content_hash(
        {
            "operator": "p7.temporal.sequence_within_bars@1.0.0",
            "plan_node": node.payload(),
            "input_refs": ["event:event_a@1.0.0", "event:event_b@1.0.0"],
            "input_hashes": [EVENT_A.content_hash(), EVENT_B.content_hash()],
            "created_at": "2026-09-28T00:00:00+00:00",
        }
    )
    assert output.name == f"p7_temporal_{expected}"
    assert output.version == "1.0.0"


@pytest.mark.parametrize("undeclared", ["first", "second", "both"])
def test_temporal_refuses_undeclared_bar_spec(undeclared: str) -> None:
    first = EVENT_A.model_copy(update={"bar_spec": None}) if undeclared != "second" else EVENT_A
    second = EVENT_B.model_copy(update={"bar_spec": None}) if undeclared != "first" else EVENT_B
    plan, resolution = _temporal(first, second)

    with pytest.raises(OperatorLoweringRefused, match="operator_open") as error:
        lower_typed_plan(plan, resolution=resolution, created_at=NOW)

    assert error.value.operator == "temporal"
    assert "bar_spec" in str(error.value)


def test_temporal_refuses_different_bar_specs() -> None:
    other_bar = EVENT_B.model_copy(update={"bar_spec": BAR_5M})
    plan, resolution = _temporal(EVENT_A, other_bar)

    with pytest.raises(OperatorLoweringRefused, match="operator_open") as error:
        lower_typed_plan(plan, resolution=resolution, created_at=NOW)

    assert "different bar specs" in str(error.value)


def test_temporal_refuses_non_bar_time_unit() -> None:
    plan, resolution = _temporal(EVENT_A, EVENT_B, time_unit="minute")

    with pytest.raises(OperatorLoweringRefused, match="operator_open") as error:
        lower_typed_plan(plan, resolution=resolution, created_at=NOW)

    assert "time_unit" in str(error.value)


def test_temporal_refuses_first_event_observable_after_second() -> None:
    """A first event with a longer observable lag could make the declared visibility too early."""
    late_first = EVENT_A.model_copy(update={"observable_lag": timedelta(minutes=5)})
    plan, resolution = _temporal(late_first, EVENT_B)

    with pytest.raises(OperatorLoweringRefused, match="temporal_visibility_unprovable"):
        lower_typed_plan(plan, resolution=resolution, created_at=NOW)


def test_temporal_equal_observable_lags_are_accepted() -> None:
    same_lag = EVENT_A.model_copy(update={"observable_lag": EVENT_B.observable_lag})
    plan, resolution = _temporal(same_lag, EVENT_B)

    output = cast(
        EventSpec, lower_typed_plan(plan, resolution=resolution, created_at=NOW)["seq"]
    )

    assert output.observable_lag == EVENT_B.observable_lag


def test_temporal_rejects_non_positive_window_at_parse_time() -> None:
    for bad_window in (0, -2):
        with pytest.raises(PlanRejected):
            _temporal(EVENT_A, EVENT_B, window=bad_window)


def test_temporal_window_and_order_are_part_of_identity() -> None:
    plan_3, resolution_3 = _temporal(EVENT_A, EVENT_B, window=3)
    plan_4, resolution_4 = _temporal(EVENT_A, EVENT_B, window=4)
    reversed_plan, reversed_resolution = _temporal(
        EVENT_B.model_copy(update={"observable_lag": timedelta(minutes=1)}),
        EVENT_A.model_copy(update={"observable_lag": timedelta(minutes=2)}),
    )

    out_3 = lower_typed_plan(plan_3, resolution=resolution_3, created_at=NOW)["seq"]
    out_4 = lower_typed_plan(plan_4, resolution=resolution_4, created_at=NOW)["seq"]
    out_rev = cast(
        EventSpec,
        lower_typed_plan(reversed_plan, resolution=reversed_resolution, created_at=NOW)["seq"],
    )

    assert out_3.ref != out_4.ref
    assert json.loads(cast(EventSpec, out_4).trigger)["window_bars"] == 4
    assert json.loads(out_rev.trigger)["first_event"] == "event:event_b@1.0.0"
    assert out_rev.ref not in (out_3.ref, out_4.ref)


@pytest.mark.parametrize(
    ("transform", "definition", "extra_param"),
    [
        (
            "standardize",
            "p7.transformation.standardize@1.0.0",
            ("fit_scope", "rolling_training_window"),
        ),
        ("difference", "p7.transformation.difference@1.0.0", None),
        ("smooth", "p7.transformation.smooth_sma@1.0.0", ("algorithm", "simple_moving_average")),
    ],
)
def test_transformation_accepted_transforms_lower_to_feature_spec(
    transform: str, definition: str, extra_param: tuple[str, str] | None
) -> None:
    plan, resolution = _plan("transformation", transform=transform, window=7)

    output = cast(
        FeatureSpec, lower_typed_plan(plan, resolution=resolution, created_at=NOW)["combined"]
    )

    assert type(output) is FeatureSpec
    assert output.definition == definition
    assert output.inputs == (SOURCE_A.ref,)
    assert output.lineage == (SOURCE_A.ref,)
    assert output.available_lag == timedelta(0)
    assert output.deterministic is True
    assert output.params["operator"] == transform
    assert output.params["window"] == 7
    assert output.params["direction"] == "backward_only"
    assert output.params["missing"] == "propagate_none"
    if extra_param is not None:
        key, value = extra_param
        assert output.params[key] == value
    assert plan.runnable is False


def test_transformation_lowering_is_deterministic_and_created_at_sensitive() -> None:
    plan, resolution = _plan("transformation", transform="standardize", window=10)

    first = lower_typed_plan(plan, resolution=resolution, created_at=NOW)["combined"]
    second = lower_typed_plan(plan, resolution=resolution, created_at=NOW)["combined"]
    later = lower_typed_plan(
        plan, resolution=resolution, created_at=datetime(2026, 9, 29, tzinfo=UTC)
    )["combined"]

    assert first.ref == second.ref
    assert first.content_hash() == second.content_hash()
    assert first.ref != later.ref


def test_transformation_rank_and_quantile_remain_operator_open_in_1_1_0_plans() -> None:
    """ADR-0099 decision 4: a "1.1.0" plan's rank / quantile node keeps its original meaning."""
    for transform in ("rank", "quantile"):
        plan, resolution = _plan("transformation", transform=transform, window=4)
        assert plan.schema_version == "1.1.0"

        with pytest.raises(OperatorLoweringRefused, match="operator_open") as error:
            lower_typed_plan(plan, resolution=resolution, created_at=NOW)

        assert error.value.operator == "transformation"
        assert plan.runnable is False


def test_transformation_window_must_be_positive() -> None:
    for bad_window in (0, -1):
        with pytest.raises(PlanRejected):
            _plan("transformation", transform="standardize", window=bad_window)


def test_transformation_standardize_requires_bound_window() -> None:
    """No 'window' at all == no training-window binding for standardize; parse-time reject."""
    with pytest.raises(PlanRejected):
        _plan("transformation", transform="standardize", include_window=False)


def test_transformation_output_binds_via_adr_0078() -> None:
    plan, resolution = _plan("transformation", transform="difference", window=3)
    outputs = lower_typed_plan(plan, resolution=resolution, created_at=NOW)

    bindings = produce_lowered_output_bindings(
        experiment_hash="b" * 64,
        plan=plan,
        specs_by_node=outputs,
    )

    assert len(bindings) == len(plan.nodes) == 1
    assert bindings[0].node_id == "combined"
    assert bindings[0].spec.content_hash() == outputs["combined"].content_hash()


# --- time-series rank / quantile (ADR-0099, plan format 1.2.0) --------------------------------


def test_plan_format_version_is_1_3_0_and_older_formats_still_parse() -> None:
    # 1.3.0: ADR-0100 §2 cross-sectional rank / quantile (see test_typed_plan_cross_sectional.py).
    assert PLAN_FORMAT_VERSION == "1.3.0"
    assert SUPPORTED_PLAN_FORMAT_VERSIONS == {"1.1.0", "1.2.0", "1.3.0"}
    for version in ("1.1.0", "1.2.0", "1.3.0"):
        plan, _ = _plan("transformation", transform="difference", schema_version=version)
        assert plan.schema_version == version
        assert plan.payload()["schema_version"] == version
    for unsupported in ("1.0.0", "1.4.0", "2.0.0"):
        with pytest.raises(PlanRejected, match="unsupported plan schema_version"):
            _plan("transformation", transform="difference", schema_version=unsupported)


def test_plan_schema_version_is_part_of_the_content_hash() -> None:
    """A "1.1.0" plan keeps its own version (and hence its original hash); it is not rewritten."""
    old, _ = _plan("transformation", transform="difference", window=3, schema_version="1.1.0")
    new, _ = _plan("transformation", transform="difference", window=3, schema_version="1.2.0")

    assert old.nodes == new.nodes
    assert old.content_hash() != new.content_hash()
    assert old.content_hash() == content_hash(old.payload())
    with pytest.raises(ValueError, match="schema_version"):
        dataclasses.replace(old, schema_version="1.0.0")


def test_transformation_rank_lowers_to_time_series_feature_spec() -> None:
    plan, resolution = _plan("transformation", transform="rank", window=20, schema_version="1.2.0")

    output = cast(
        FeatureSpec, lower_typed_plan(plan, resolution=resolution, created_at=NOW)["combined"]
    )

    assert type(output) is FeatureSpec
    assert output.definition == "p7.transformation.rank_ts@1.0.0"
    assert output.inputs == (SOURCE_A.ref,)
    assert output.lineage == (SOURCE_A.ref,)
    assert output.available_lag == timedelta(0)
    assert output.deterministic is True
    assert output.params == FrozenMapping(
        {
            "operator": "rank",
            "provider": "p7_transformation_rank@1.0.0",
            "semantic_version": "1.0.0",
            "window": 20,
            "direction": "backward_only",
            "missing": "propagate_none",
            "ties": "average",
            "scale": "unit_interval",
        }
    )
    assert plan.runnable is False


def test_transformation_quantile_lowers_to_time_series_feature_spec() -> None:
    plan, resolution = _plan(
        "transformation", transform="quantile", window=20, buckets=5, schema_version="1.2.0"
    )

    output = cast(
        FeatureSpec, lower_typed_plan(plan, resolution=resolution, created_at=NOW)["combined"]
    )

    assert type(output) is FeatureSpec
    assert output.definition == "p7.transformation.quantile_ts@1.0.0"
    assert output.inputs == (SOURCE_A.ref,)
    assert output.lineage == (SOURCE_A.ref,)
    assert output.available_lag == timedelta(0)
    assert output.deterministic is True
    assert output.params == FrozenMapping(
        {
            "operator": "quantile",
            "provider": "p7_transformation_quantile@1.0.0",
            "semantic_version": "1.0.0",
            "window": 20,
            "direction": "backward_only",
            "missing": "propagate_none",
            "buckets": 5,
            "ties": "average",
        }
    )
    assert plan.runnable is False


def test_transformation_quantile_buckets_is_part_of_identity() -> None:
    four, four_resolution = _plan(
        "transformation", transform="quantile", window=10, buckets=4, schema_version="1.2.0"
    )
    ten, ten_resolution = _plan(
        "transformation", transform="quantile", window=10, buckets=10, schema_version="1.2.0"
    )

    out_four = lower_typed_plan(four, resolution=four_resolution, created_at=NOW)["combined"]
    out_ten = lower_typed_plan(ten, resolution=ten_resolution, created_at=NOW)["combined"]

    assert out_four.ref != out_ten.ref
    assert out_four.content_hash() != out_ten.content_hash()


def test_transformation_accepted_transforms_lower_unchanged_in_1_2_0_plans() -> None:
    for transform in ("standardize", "difference", "smooth"):
        plan, resolution = _plan(
            "transformation", transform=transform, window=7, schema_version="1.2.0"
        )
        old_plan, old_resolution = _plan("transformation", transform=transform, window=7)

        output = lower_typed_plan(plan, resolution=resolution, created_at=NOW)["combined"]
        old = lower_typed_plan(old_plan, resolution=old_resolution, created_at=NOW)["combined"]

        assert type(output) is FeatureSpec
        assert type(old) is FeatureSpec
        assert output.definition == old.definition
        assert output.params == old.params


def test_transformation_quantile_requires_buckets() -> None:
    with pytest.raises(PlanRejected, match="buckets"):
        _plan("transformation", transform="quantile", window=10, schema_version="1.2.0")


@pytest.mark.parametrize("bad_buckets", [1, 0, -3, True, "5"])
def test_transformation_quantile_buckets_must_be_integer_at_least_two(bad_buckets: object) -> None:
    with pytest.raises(PlanRejected, match="buckets"):
        _plan(
            "transformation",
            transform="quantile",
            window=10,
            buckets=bad_buckets,
            schema_version="1.2.0",
        )


@pytest.mark.parametrize("transform", ["standardize", "difference", "smooth", "rank"])
def test_transformation_buckets_is_rejected_on_non_quantile_transforms(transform: str) -> None:
    with pytest.raises(PlanRejected, match="buckets"):
        _plan("transformation", transform=transform, window=10, buckets=5, schema_version="1.2.0")


def test_transformation_buckets_is_not_admitted_in_1_1_0_plans() -> None:
    with pytest.raises(PlanRejected, match="unknown field"):
        _plan("transformation", transform="quantile", window=10, buckets=5)


def test_transformation_rank_and_quantile_require_window_of_at_least_two() -> None:
    with pytest.raises(PlanRejected, match="window"):
        _plan("transformation", transform="rank", window=1, schema_version="1.2.0")
    with pytest.raises(PlanRejected, match="window"):
        _plan("transformation", transform="quantile", window=1, buckets=4, schema_version="1.2.0")
    # Other transforms keep the plain positive-integer window rule.
    plan, _ = _plan("transformation", transform="difference", window=1, schema_version="1.2.0")
    assert plan.nodes[0].parameters["window"] == 1
    # A "1.1.0" rank node with a one-bar window still parses; it is operator_open at lowering.
    old, old_resolution = _plan("transformation", transform="rank", window=1)
    with pytest.raises(OperatorLoweringRefused, match="operator_open"):
        lower_typed_plan(old, resolution=old_resolution, created_at=NOW)


def test_transformation_rank_output_binds_via_adr_0078() -> None:
    plan, resolution = _plan("transformation", transform="rank", window=5, schema_version="1.2.0")
    outputs = lower_typed_plan(plan, resolution=resolution, created_at=NOW)

    bindings = produce_lowered_output_bindings(
        experiment_hash="c" * 64,
        plan=plan,
        specs_by_node=outputs,
    )

    assert len(bindings) == len(plan.nodes) == 1
    assert bindings[0].node_id == "combined"
    assert bindings[0].spec.content_hash() == outputs["combined"].content_hash()


# --- conditioning / ensemble / negation (ADR-0088 decision 2) ---------------------------------


def _conditioning(
    strategy: StrategySpec, state: StateSpec, state_value: str
) -> tuple[TypedPlan, DirectReferenceResolution]:
    return _multi(
        [
            _node(
                "gated",
                "conditioning",
                [_spec_input(strategy), _spec_input(state)],
                {"state_value": state_value},
            )
        ],
        (strategy, state),
        root="gated",
    )


def _ensemble(*members: StrategySpec) -> tuple[TypedPlan, DirectReferenceResolution]:
    return _multi(
        [_node("blend", "ensemble", [_spec_input(member) for member in members])],
        members,
        root="blend",
    )


def test_conditioning_lowers_to_conditioned_strategy() -> None:
    plan, resolution = _conditioning(STRATEGY_A, STATE_REGIME, "high")

    outputs = lower_typed_plan(plan, resolution=resolution, created_at=NOW)

    output = cast(StrategySpec, outputs["gated"])
    assert type(output) is StrategySpec
    assert output.composition == ConditionedStrategy(
        base=STRATEGY_A.ref, state=STATE_REGIME.ref, state_value="high"
    )
    # Signals cover the base's signals plus the gating state (ADR-0088 decision 2).
    assert output.signals == (EV_FEATURE, STATE_REGIME.ref)
    assert output.risk_policy == RISK_A
    assert output.applicable_instruments == (BTC_SPOT,)
    assert output.param_search_space == FrozenMapping({})
    assert output.lineage == (STRATEGY_A.ref, STATE_REGIME.ref)
    assert output.params["unknown_state"] == "flat"
    assert output.params["unmatched_state"] == "flat"
    assert output.params["provider"] == "p7_conditioning_state_gate@1.0.0"
    assert plan.runnable is False
    bindings = produce_lowered_output_bindings(
        experiment_hash="d" * 64, plan=plan, specs_by_node=outputs
    )
    assert bindings[0].node_id == "gated"
    assert bindings[0].spec.content_hash() == output.content_hash()


def test_conditioning_state_value_is_part_of_identity() -> None:
    high_plan, high_resolution = _conditioning(STRATEGY_A, STATE_REGIME, "high")
    low_plan, low_resolution = _conditioning(STRATEGY_A, STATE_REGIME, "low")

    high = lower_typed_plan(high_plan, resolution=high_resolution, created_at=NOW)["gated"]
    low = lower_typed_plan(low_plan, resolution=low_resolution, created_at=NOW)["gated"]

    # One trial per (base, state, state_value): distinct values give distinct strategies.
    assert high.ref != low.ref
    assert cast(StrategySpec, low).composition == ConditionedStrategy(
        base=STRATEGY_A.ref, state=STATE_REGIME.ref, state_value="low"
    )


def test_conditioning_refuses_state_value_outside_state_space() -> None:
    plan, resolution = _conditioning(STRATEGY_A, STATE_REGIME, "sideways")

    with pytest.raises(OperatorLoweringRefused, match="unknown_state_value") as error:
        lower_typed_plan(plan, resolution=resolution, created_at=NOW)

    assert error.value.operator == "conditioning"


def test_conditioning_does_not_duplicate_a_state_already_in_base_signals() -> None:
    base = STRATEGY_A.model_copy(update={"signals": (EV_FEATURE, STATE_REGIME.ref)})
    plan, resolution = _conditioning(base, STATE_REGIME, "low")

    output = cast(
        StrategySpec, lower_typed_plan(plan, resolution=resolution, created_at=NOW)["gated"]
    )

    assert output.signals == (EV_FEATURE, STATE_REGIME.ref)


def test_ensemble_lowers_to_equal_weight_mean() -> None:
    plan, resolution = _ensemble(STRATEGY_A, STRATEGY_B)

    outputs = lower_typed_plan(plan, resolution=resolution, created_at=NOW)

    output = cast(StrategySpec, outputs["blend"])
    assert output.composition == EnsembleStrategy(
        members=(STRATEGY_A.ref, STRATEGY_B.ref), rule="equal_weight_mean"
    )
    # Ordered union by target identity: A's ev_feature, then B's new ev_feature_2.
    assert output.signals == (EV_FEATURE, EV_FEATURE_2)
    assert output.risk_policy == RISK_A
    assert output.applicable_instruments == (BTC_SPOT,)
    assert output.lineage == (STRATEGY_A.ref, STRATEGY_B.ref)
    assert output.params["rule"] == "equal_weight_mean"
    assert output.params["cost_basis"] == "net_combined_position_change"
    bindings = produce_lowered_output_bindings(
        experiment_hash="e" * 64, plan=plan, specs_by_node=outputs
    )
    assert len(bindings) == 1


def test_ensemble_member_order_is_part_of_identity() -> None:
    forward_plan, forward_resolution = _ensemble(STRATEGY_A, STRATEGY_B)
    backward_plan, backward_resolution = _ensemble(STRATEGY_B, STRATEGY_A)

    forward = lower_typed_plan(forward_plan, resolution=forward_resolution, created_at=NOW)
    backward = lower_typed_plan(backward_plan, resolution=backward_resolution, created_at=NOW)

    assert forward["blend"].ref != backward["blend"].ref
    assert cast(StrategySpec, backward["blend"]).signals == (EV_FEATURE_2, EV_FEATURE)


def test_ensemble_refuses_risk_policy_mismatch() -> None:
    other_risk = STRATEGY_B.model_copy(update={"risk_policy": RISK_B})
    plan, resolution = _ensemble(STRATEGY_A, other_risk)

    with pytest.raises(OperatorLoweringRefused, match="ensemble_risk_policy_mismatch"):
        lower_typed_plan(plan, resolution=resolution, created_at=NOW)


def test_ensemble_refuses_none_versus_declared_risk_policy() -> None:
    no_risk = STRATEGY_B.model_copy(update={"risk_policy": None})
    plan, resolution = _ensemble(STRATEGY_A, no_risk)

    with pytest.raises(OperatorLoweringRefused, match="ensemble_risk_policy_mismatch"):
        lower_typed_plan(plan, resolution=resolution, created_at=NOW)


def test_ensemble_refuses_applicable_instrument_mismatch() -> None:
    other_instruments = STRATEGY_B.model_copy(update={"applicable_instruments": (ETH_SPOT,)})
    plan, resolution = _ensemble(STRATEGY_A, other_instruments)

    with pytest.raises(OperatorLoweringRefused, match="ensemble_instruments_mismatch"):
        lower_typed_plan(plan, resolution=resolution, created_at=NOW)


def test_ensemble_rejects_duplicate_or_single_member_at_parse_time() -> None:
    with pytest.raises(PlanRejected):
        _ensemble(STRATEGY_A, STRATEGY_A)
    with pytest.raises(PlanRejected):
        _ensemble(STRATEGY_A)


def test_negation_lowers_to_negated_strategy() -> None:
    plan, resolution = _plan("negation")
    (node,) = plan.nodes

    outputs = lower_typed_plan(plan, resolution=resolution, created_at=NOW)

    output = cast(StrategySpec, outputs["combined"])
    assert output.composition == NegatedStrategy(base=STRATEGY_A.ref)
    assert output.signals == STRATEGY_A.signals
    assert output.risk_policy == RISK_A
    assert output.applicable_instruments == (BTC_SPOT,)
    assert output.lineage == (STRATEGY_A.ref,)
    # Not a validation negative control (ADR-0088 decision 2).
    assert output.params["validation_negative_control"] is False
    assert output.params["negates"] == "target_position"
    expected = content_hash(
        {
            "operator": "p7.negation.target_position@1.0.0",
            "plan_node": node.payload(),
            "input_refs": ["strategy:strategy_a@1.0.0"],
            "input_hashes": [STRATEGY_A.content_hash()],
            "created_at": "2026-09-28T00:00:00+00:00",
        }
    )
    assert output.name == f"p7_negation_{expected}"
    assert plan.runnable is False
    bindings = produce_lowered_output_bindings(
        experiment_hash="f" * 64, plan=plan, specs_by_node=outputs
    )
    assert len(bindings) == 1


def test_nested_strategy_plan_lowers_every_node_and_binds_completely() -> None:
    plan, resolution = _multi(
        [
            _node("blend", "ensemble", [_spec_input(STRATEGY_A), _spec_input(STRATEGY_B)]),
            _node("inverse", "negation", [{"node": "blend"}]),
            _node(
                "gated",
                "conditioning",
                [{"node": "inverse"}, _spec_input(STATE_REGIME)],
                {"state_value": "high"},
            ),
        ],
        (STRATEGY_A, STRATEGY_B, STATE_REGIME),
        root="gated",
    )

    outputs = lower_typed_plan(plan, resolution=resolution, created_at=NOW)

    assert tuple(outputs) == ("blend", "inverse", "gated")
    inverse = cast(StrategySpec, outputs["inverse"])
    gated = cast(StrategySpec, outputs["gated"])
    assert inverse.composition == NegatedStrategy(base=outputs["blend"].ref)
    assert gated.composition == ConditionedStrategy(
        base=inverse.ref, state=STATE_REGIME.ref, state_value="high"
    )
    assert gated.signals == (EV_FEATURE, EV_FEATURE_2, STATE_REGIME.ref)
    bindings = produce_lowered_output_bindings(
        experiment_hash="1" * 64, plan=plan, specs_by_node=outputs
    )
    assert [binding.node_id for binding in bindings] == ["blend", "inverse", "gated"]
    assert plan.runnable is False


def test_upstream_output_is_unchanged_by_a_later_downstream_node() -> None:
    """Appending a later node never perturbs an earlier node's lowered spec."""
    alone_plan, alone_resolution = _ensemble(STRATEGY_A, STRATEGY_B)
    extended_plan, extended_resolution = _multi(
        [
            _node("blend", "ensemble", [_spec_input(STRATEGY_A), _spec_input(STRATEGY_B)]),
            _node("inverse", "negation", [{"node": "blend"}]),
        ],
        (STRATEGY_A, STRATEGY_B),
        root="inverse",
    )

    alone = lower_typed_plan(alone_plan, resolution=alone_resolution, created_at=NOW)
    extended = lower_typed_plan(extended_plan, resolution=extended_resolution, created_at=NOW)

    assert extended["blend"].ref == alone["blend"].ref
    assert extended["blend"].content_hash() == alone["blend"].content_hash()


def test_producer_refuses_swapped_strategy_composition() -> None:
    """ADR-0078 producer: a composing node's StrategySpec must carry that node's composition."""
    gated_plan, _ = _conditioning(STRATEGY_A, STATE_REGIME, "high")
    negated_plan, negated_resolution = _plan("negation")
    negated = lower_typed_plan(negated_plan, resolution=negated_resolution, created_at=NOW)

    with pytest.raises(PlanBindingRefused, match="plan_output_composition_mismatch"):
        produce_lowered_output_bindings(
            experiment_hash="2" * 64,
            plan=gated_plan,
            specs_by_node={"gated": negated["combined"]},
        )
    with pytest.raises(PlanBindingRefused, match="plan_output_composition_mismatch"):
        produce_lowered_output_bindings(
            experiment_hash="2" * 64,
            plan=negated_plan,
            specs_by_node={"combined": STRATEGY_A},
        )

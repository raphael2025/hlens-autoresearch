"""Shared fixtures for the P7 compile / binding / evidence tests (not a test module)."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any

from core.contracts.universe import ResearchDatasetManifest
from core.domain.base import FrozenMapping, Kind, Ref, VersionedSpec
from core.domain.research import ExperimentSpec, Hypothesis, HypothesisOrigin, ReproducibilityTuple
from core.domain.selection import ProfileSelection, ProfileSelectionKey
from core.domain.specs import (
    DatasetRef,
    EventSpec,
    FeatureSpec,
    Instrument,
    InstrumentType,
    StateSpec,
    StrategySpec,
    Zone,
)
from research.hypotheses.p7_binding import (
    P7PlanRecord,
    p7_plan_condition,
    with_p7_plan,
    with_plan_dependency_hashes,
)
from research.hypotheses.typed_plan import PlanLimits, TypedPlan, parse_plan_json
from research.hypotheses.typed_plan_compiler import (
    P7_OPERATOR_ALLOWLIST,
    CompiledPlan,
    OperatorImplementation,
    P7ExecutionSwitch,
    compile_lowered_plan,
)
from research.hypotheses.typed_plan_resolver import (
    DirectReferenceResolution,
    resolve_direct_references,
)

NOW = datetime(2026, 10, 1, tzinfo=UTC)
ENABLED = P7ExecutionSwitch(enabled=True)
LIMITS = PlanLimits(max_depth=4, max_nodes=6, max_json_bytes=8192, max_parameters_per_node=3)

BARS = Ref(kind=Kind.REPRESENTATION, name="bars", version="1.0.0")
BAR_1M = Ref(kind=Kind.REPRESENTATION, name="canonical_bar_1m", version="1.0.0")
EV_FEATURE = Ref(kind=Kind.FEATURE, name="ev_feature", version="1.0.0")
EV_FEATURE_2 = Ref(kind=Kind.FEATURE, name="ev_feature_2", version="1.0.0")
RISK_A = Ref(kind=Kind.RISK, name="risk_a", version="1.0.0")


def _feature(name: str) -> FeatureSpec:
    return FeatureSpec(
        name=name,
        version="1.0.0",
        created_at=NOW,
        definition=f"source {name}",
        inputs=(BARS,),
        params=FrozenMapping({}),
        available_lag=timedelta(0),
    )


SOURCE_A = _feature("feature_a")
SOURCE_B = _feature("feature_b")
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
BTC_SPOT = Instrument(
    venue="binance",
    symbol="BTCUSDT",
    instrument_type=InstrumentType.SPOT,
    base="BTC",
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
ALL_SPECS: tuple[VersionedSpec, ...] = (
    SOURCE_A,
    SOURCE_B,
    EVENT_A,
    EVENT_B,
    STATE_REGIME,
    STRATEGY_A,
    STRATEGY_B,
)


class Resolver:
    def __init__(self, specs: tuple[VersionedSpec, ...] = ALL_SPECS) -> None:
        self.specs = {str(spec.ref): spec for spec in specs}

    def resolve(self, ref: Ref) -> VersionedSpec | None:
        return self.specs.get(str(ref))


def spec_input(spec: VersionedSpec) -> dict[str, str]:
    return {"ref": str(spec.ref), "content_hash": spec.content_hash()}


def node(
    node_id: str, operator: str, inputs: list[Any], parameters: dict[str, Any] | None = None
) -> dict[str, Any]:
    return {"id": node_id, "operator": operator, "inputs": inputs, "parameters": parameters or {}}


def parse(nodes: list[dict[str, Any]], root: str) -> tuple[TypedPlan, DirectReferenceResolution]:
    payload = {"schema_version": "1.3.0", "root": root, "nodes": nodes}
    plan = parse_plan_json(json.dumps(payload, separators=(",", ":")), limits=LIMITS)
    return plan, resolve_direct_references(plan, resolver=Resolver())


def feature_chain_nodes() -> list[dict[str, Any]]:
    """difference(feature_a, window 5) then interaction with feature_b (root, a feature)."""
    return [
        node(
            "diff",
            "transformation",
            [spec_input(SOURCE_A)],
            {"transform": "difference", "window": 5},
        ),
        node("prod", "interaction", [{"node": "diff"}, spec_input(SOURCE_B)]),
    ]


def strategy_chain_nodes() -> list[dict[str, Any]]:
    """ensemble(A, B) then negation (root, a strategy)."""
    return [
        node("blend", "ensemble", [spec_input(STRATEGY_A), spec_input(STRATEGY_B)]),
        node("inverse", "negation", [{"node": "blend"}]),
    ]


def temporal_nodes() -> list[dict[str, Any]]:
    return [
        node(
            "seq",
            "temporal",
            [spec_input(EVENT_A), spec_input(EVENT_B)],
            {"window": 3, "time_unit": "bar"},
        )
    ]


def cross_sectional_nodes(universe: ResearchDatasetManifest) -> list[dict[str, Any]]:
    """One ``rank_cs`` root over ``feature_a`` and the pinned ``universe`` (ADR-0100 §2)."""
    reference = f"research_dataset:{universe.dataset.table}@{universe.dataset.snapshot_id}"
    return [
        node(
            "xs",
            "transformation",
            [spec_input(SOURCE_A)],
            {
                "transform": "rank_cs",
                "universe": reference,
                "universe_hash": universe.content_hash(),
            },
        )
    ]


def compile_nodes(
    nodes: list[dict[str, Any]],
    root: str,
    *,
    allowlist: dict[str, OperatorImplementation] | None = None,
    universes: tuple[ResearchDatasetManifest, ...] = (),
) -> tuple[CompiledPlan, DirectReferenceResolution]:
    plan, resolution = parse(nodes, root)
    compiled = compile_lowered_plan(
        plan,
        resolution=resolution,
        created_at=NOW,
        allowlist=dict(P7_OPERATOR_ALLOWLIST) if allowlist is None else allowlist,
        switch=ENABLED,
        universes=universes,
    )
    return compiled, resolution


def feature_chain() -> tuple[CompiledPlan, DirectReferenceResolution]:
    return compile_nodes(feature_chain_nodes(), "prod")


def strategy_chain() -> tuple[CompiledPlan, DirectReferenceResolution]:
    return compile_nodes(strategy_chain_nodes(), "inverse")


def hypothesis_for(compiled: CompiledPlan, name: str = "h_plan") -> Hypothesis:
    return Hypothesis(
        name=name,
        version="1.0.0",
        created_at=NOW,
        family_id="family",
        statement="a compiled P7 plan holds",
        conditions=(p7_plan_condition(compiled.plan_hash),),
        expected_direction="higher",
        minimum_meaningful_effect="1 bp",
        origin=HypothesisOrigin.COMBINATION,
        origin_refs=(SOURCE_A.ref,),
    )


def experiment_for(
    compiled: CompiledPlan,
    hypothesis: Hypothesis,
    *,
    record: P7PlanRecord | None = None,
    with_record: bool = True,
    params: dict[str, Any] | None = None,
    dependencies: dict[str, str] | None = None,
    bind_outputs: bool = True,
) -> ExperimentSpec:
    record = P7PlanRecord.from_compiled(compiled) if record is None else record
    strategy_ref = Ref(kind=Kind.STRATEGY, name="strategy", version="1.0.0")
    cost_ref = Ref(kind=Kind.COST_MODEL, name="cost", version="1.0.0")
    profile_ref = Ref(kind=Kind.PROFILE, name="profile", version="1.0.0")
    selection_ref = Ref(kind=Kind.PROFILE_SELECTION_RULE, name="selection", version="1.0.0")
    base = {
        str(hypothesis.ref): hypothesis.content_hash(),
        str(strategy_ref): "f" * 64,
        str(cost_ref): "1" * 64,
        **(dependencies or {}),
    }
    merged = with_plan_dependency_hashes(base, record) if bind_outputs else base
    merged_params = dict(params or {})
    if with_record:
        merged_params = with_p7_plan(merged_params, record)
    repro = ReproducibilityTuple(
        hypothesis_ref=hypothesis.ref,
        strategy_ref=strategy_ref,
        risk_policy_ref=None,
        outcome_ref=None,
        dataset_snapshots=(
            DatasetRef(
                zone=Zone.CANONICAL,
                table="canonical.test",
                snapshot_id="snapshot-1",
                time_range_start=NOW,
                time_range_end=NOW + timedelta(days=1),
            ),
        ),
        code_commit="e" * 40,
        dependency_hashes=FrozenMapping(merged),
        params=FrozenMapping(merged_params),
        environment_lock="test-lock",
        constitution_version="1.0.0",
        validation_profile=profile_ref,
        validation_profile_hash="2" * 64,
        profile_selection=ProfileSelection(
            selection_rule=selection_ref,
            selection_rule_hash="3" * 64,
            key=ProfileSelectionKey(
                venue="test", symbol="TEST-USD", timeframe="1m", research_class="basic"
            ),
        ),
        split_spec="test split",
        cost_model_ref=cost_ref,
    )
    return ExperimentSpec(
        name=f"experiment_{hypothesis.name}", version="1.0.0", created_at=NOW, repro=repro
    )

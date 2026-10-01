from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from core.domain.base import FrozenMapping, Kind, Ref
from core.domain.research import ExperimentSpec, Hypothesis, HypothesisOrigin, ReproducibilityTuple
from core.domain.selection import ProfileSelection, ProfileSelectionKey
from core.domain.specs import (
    ConditionedStrategy,
    DatasetRef,
    FeatureSpec,
    NegatedStrategy,
    StrategySpec,
    Zone,
)
from research.hypotheses.plan_bindings import (
    PlanBindingRefused,
    produce_lowered_output_bindings,
    validate_complete_experiment_bindings,
)
from research.hypotheses.typed_plan import (
    PlanLimits,
    PlanNode,
    PlanOperator,
    SpecInput,
    TypedPlan,
)

FEATURE_A = Ref(kind=Kind.FEATURE, name="feature_a", version="1.0.0")
FEATURE_B = Ref(kind=Kind.FEATURE, name="feature_b", version="1.0.0")
NOW = datetime(2026, 9, 28, tzinfo=UTC)
EXPERIMENT_HASH = "a" * 64


def _plan() -> TypedPlan:
    return TypedPlan(
        root="combined",
        nodes=(
            PlanNode(
                node_id="combined",
                operator=PlanOperator.INTERACTION,
                inputs=(
                    SpecInput(FEATURE_A, "b" * 64),
                    SpecInput(FEATURE_B, "c" * 64),
                ),
                parameters=FrozenMapping({}),
            ),
        ),
        limits=PlanLimits(
            max_depth=2,
            max_nodes=4,
            max_json_bytes=2048,
            max_parameters_per_node=4,
        ),
    )


def _feature() -> FeatureSpec:
    return FeatureSpec(
        name="combined_feature",
        version="1.0.0",
        created_at=NOW,
        definition="test lowering output",
        inputs=(FEATURE_A, FEATURE_B),
        params=FrozenMapping({}),
        available_lag=timedelta(0),
    )


def _hypothesis() -> Hypothesis:
    return Hypothesis(
        name="h_plan",
        version="1.0.0",
        created_at=NOW,
        family_id="family",
        statement="test typed-plan binding",
        expected_direction="positive",
        minimum_meaningful_effect="1 bp",
        origin=HypothesisOrigin.HUMAN,
    )


def _experiment(*, output_hash: str) -> ExperimentSpec:
    hypothesis = _hypothesis()
    feature = _feature()
    strategy_ref = Ref(kind=Kind.STRATEGY, name="strategy", version="1.0.0")
    cost_ref = Ref(kind=Kind.COST_MODEL, name="cost", version="1.0.0")
    profile_ref = Ref(kind=Kind.PROFILE, name="profile", version="1.0.0")
    selection_ref = Ref(kind=Kind.PROFILE_SELECTION_RULE, name="selection", version="1.0.0")
    dependencies = FrozenMapping(
        {
            str(hypothesis.ref): hypothesis.content_hash(),
            str(strategy_ref): "f" * 64,
            str(cost_ref): "1" * 64,
            str(feature.ref): output_hash,
        }
    )
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
        dependency_hashes=dependencies,
        environment_lock="test-lock",
        constitution_version="1.0.0",
        validation_profile=profile_ref,
        validation_profile_hash="2" * 64,
        profile_selection=ProfileSelection(
            selection_rule=selection_ref,
            selection_rule_hash="3" * 64,
            key=ProfileSelectionKey(
                venue="test",
                symbol="TEST-USD",
                timeframe="1m",
                research_class="basic",
            ),
        ),
        split_spec="test split",
        cost_model_ref=cost_ref,
    )
    return ExperimentSpec(name="experiment", version="1.0.0", created_at=NOW, repro=repro)


def test_output_producer_requires_the_complete_ast_node_set() -> None:
    plan = _plan()
    assert (
        len(
            produce_lowered_output_bindings(
                experiment_hash=EXPERIMENT_HASH,
                plan=plan,
                specs_by_node={"combined": _feature()},
            )
        )
        == 1
    )

    with pytest.raises(PlanBindingRefused, match="missing_node_output"):
        produce_lowered_output_bindings(
            experiment_hash=EXPERIMENT_HASH,
            plan=plan,
            specs_by_node={},
        )
    with pytest.raises(PlanBindingRefused, match="extra_node_output"):
        produce_lowered_output_bindings(
            experiment_hash=EXPERIMENT_HASH,
            plan=plan,
            specs_by_node={"combined": _feature(), "unplanned": _feature()},
        )


def test_output_producer_rejects_wrong_nominal_output_type() -> None:
    with pytest.raises(PlanBindingRefused, match="plan_output_type_mismatch"):
        produce_lowered_output_bindings(
            experiment_hash=EXPERIMENT_HASH,
            plan=_plan(),
            specs_by_node={"combined": object()},  # type: ignore[dict-item]
        )


def test_output_producer_requires_conditioned_strategy_for_conditional_plan() -> None:
    """ADR-0088 decision 2: a conditional strategy plan's only core spec is a StrategySpec whose
    composition is ConditionedStrategy; anything else still fails closed."""
    strategy_ref = Ref(kind=Kind.STRATEGY, name="s", version="1.0.0")
    state_ref = Ref(kind=Kind.STATE, name="state", version="1.0.0")
    node = PlanNode(
        node_id="conditional",
        operator=PlanOperator.CONDITIONING,
        inputs=(
            SpecInput(strategy_ref, "d" * 64),
            SpecInput(state_ref, "e" * 64),
        ),
        parameters=FrozenMapping({"state_value": "high"}),
    )
    plan = TypedPlan(
        root="conditional",
        nodes=(node,),
        limits=PlanLimits(max_depth=2, max_nodes=2, max_json_bytes=2048, max_parameters_per_node=2),
    )
    with pytest.raises(PlanBindingRefused, match="plan_output_type_mismatch"):
        produce_lowered_output_bindings(
            experiment_hash=EXPERIMENT_HASH,
            plan=plan,
            specs_by_node={"conditional": _feature()},
        )
    plain = StrategySpec(
        name="plain",
        version="1.0.0",
        created_at=NOW,
        signals=(FEATURE_A, state_ref),
    )
    with pytest.raises(PlanBindingRefused, match="plan_output_composition_mismatch"):
        produce_lowered_output_bindings(
            experiment_hash=EXPERIMENT_HASH,
            plan=plan,
            specs_by_node={"conditional": plain},
        )
    negated = plain.model_copy(
        update={"name": "negated", "composition": NegatedStrategy(base=strategy_ref)}
    )
    with pytest.raises(PlanBindingRefused, match="plan_output_composition_mismatch"):
        produce_lowered_output_bindings(
            experiment_hash=EXPERIMENT_HASH,
            plan=plan,
            specs_by_node={"conditional": negated},
        )
    gated = plain.model_copy(
        update={
            "name": "gated",
            "composition": ConditionedStrategy(
                base=strategy_ref, state=state_ref, state_value="high"
            ),
        }
    )
    (binding,) = produce_lowered_output_bindings(
        experiment_hash=EXPERIMENT_HASH,
        plan=plan,
        specs_by_node={"conditional": gated},
    )
    assert binding.node_id == "conditional"
    assert binding.spec.content_hash() == gated.content_hash()


def test_complete_binding_validator_rejects_output_hash_mismatch() -> None:
    hypothesis = _hypothesis()
    feature = _feature()
    experiment = _experiment(output_hash="9" * 64)
    outputs = produce_lowered_output_bindings(
        experiment_hash=experiment.experiment_hash,
        plan=_plan(),
        specs_by_node={"combined": feature},
    )

    with pytest.raises(PlanBindingRefused, match="output_hash_mismatch"):
        validate_complete_experiment_bindings(
            experiment_specs=(experiment,),
            hypotheses=(hypothesis,),
            plans_by_experiment={experiment.experiment_hash: _plan()},
            lowered_outputs=outputs,
        )


def test_complete_binding_validator_rejects_missing_node_output() -> None:
    hypothesis = _hypothesis()
    experiment = _experiment(output_hash=_feature().content_hash())

    with pytest.raises(PlanBindingRefused, match="missing_node_output"):
        validate_complete_experiment_bindings(
            experiment_specs=(experiment,),
            hypotheses=(hypothesis,),
            plans_by_experiment={experiment.experiment_hash: _plan()},
            lowered_outputs=(),
        )

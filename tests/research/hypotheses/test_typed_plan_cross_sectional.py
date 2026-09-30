"""ADR-0100 §2: cross-sectional ``rank_cs`` / ``quantile_cs`` in plan format 1.3.0 and lowering."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any, cast

import pytest

from core.contracts.universe import ResearchDatasetManifest
from core.domain.base import FrozenMapping, Kind, Ref, VersionedSpec
from core.domain.specs import FeatureSpec
from research.hypotheses.plan_bindings import produce_lowered_output_bindings
from research.hypotheses.typed_plan import (
    PlanLimits,
    PlanRejected,
    TypedPlan,
    parse_plan_json,
    parse_universe_reference,
)
from research.hypotheses.typed_plan_lowering import OperatorLoweringRefused, lower_typed_plan
from research.hypotheses.typed_plan_resolver import (
    DirectReferenceResolution,
    resolve_direct_references,
)
from tests.test_universe_contracts import dataset, manifest

NOW = datetime(2026, 9, 30, tzinfo=UTC)
SOURCE = FeatureSpec(
    name="momentum_source",
    version="1.0.0",
    created_at=NOW,
    definition="source",
    inputs=(Ref(kind=Kind.REPRESENTATION, name="canonical_bar_1m", version="1.0.0"),),
    params=FrozenMapping({}),
    available_lag=timedelta(0),
)
UNIVERSE = manifest()
LIMITS = PlanLimits(max_depth=2, max_nodes=2, max_json_bytes=4096, max_parameters_per_node=4)


class _Resolver:
    def resolve(self, ref: Ref) -> VersionedSpec | None:
        return SOURCE if ref == SOURCE.ref else None


def _universe_text(universe: ResearchDatasetManifest) -> str:
    return f"research_dataset:{universe.dataset.table}@{universe.dataset.snapshot_id}"


def _cs_plan(
    transform: str = "rank_cs",
    *,
    pinned: ResearchDatasetManifest = UNIVERSE,
    schema_version: str = "1.3.0",
    **overrides: Any,
) -> tuple[TypedPlan, DirectReferenceResolution]:
    """A one-node cross-sectional plan; an override of ``None`` removes that parameter."""
    parameters: dict[str, Any] = {
        "transform": transform,
        "universe": _universe_text(pinned),
        "universe_hash": pinned.content_hash(),
    }
    for key, value in overrides.items():
        if value is None:
            parameters.pop(key, None)
        else:
            parameters[key] = value
    return _parse(parameters, schema_version)


def _parse(
    parameters: dict[str, Any], schema_version: str = "1.3.0"
) -> tuple[TypedPlan, DirectReferenceResolution]:
    payload = {
        "schema_version": schema_version,
        "root": "xs",
        "nodes": [
            {
                "id": "xs",
                "operator": "transformation",
                "inputs": [{"ref": str(SOURCE.ref), "content_hash": SOURCE.content_hash()}],
                "parameters": parameters,
            }
        ],
    }
    plan = parse_plan_json(json.dumps(payload, separators=(",", ":")), limits=LIMITS)
    return plan, resolve_direct_references(plan, resolver=_Resolver())


# --- plan format 1.3.0 grammar ----------------------------------------------------------------


def test_rank_cs_and_quantile_cs_parse_under_1_3_0() -> None:
    rank, _ = _cs_plan("rank_cs")
    quantile, _ = _cs_plan("quantile_cs", buckets=5)

    assert rank.schema_version == "1.3.0"
    assert dict(rank.nodes[0].parameters) == {
        "transform": "rank_cs",
        "universe": _universe_text(UNIVERSE),
        "universe_hash": UNIVERSE.content_hash(),
    }
    assert quantile.nodes[0].parameters["buckets"] == 5
    assert rank.runnable is False


@pytest.mark.parametrize("version", ["1.1.0", "1.2.0"])
def test_cross_sectional_transforms_are_unknown_in_older_formats(version: str) -> None:
    with pytest.raises(PlanRejected, match="unknown"):
        _cs_plan("rank_cs", schema_version=version)


def test_cross_sectional_transform_rejects_a_window() -> None:
    with pytest.raises(PlanRejected, match="window"):
        _cs_plan("rank_cs", window=20)


@pytest.mark.parametrize("missing", ["universe", "universe_hash"])
def test_cross_sectional_transform_requires_the_pinned_universe(missing: str) -> None:
    with pytest.raises(PlanRejected, match=missing):
        _cs_plan("rank_cs", **{missing: None})


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("universe", "research.first_slice_1m@9001"),  # no zone prefix
        ("universe", "research_dataset:research.first_slice_1m"),  # no snapshot
        ("universe", "research_dataset:Research.x@9001"),  # not namespace.table syntax
        ("universe", "research_dataset:research.first_slice_1m@"),
        ("universe_hash", "A" * 64),
        ("universe_hash", "1" * 63),
        ("universe_hash", 7),
    ],
)
def test_cross_sectional_universe_parameters_are_strict(field: str, value: object) -> None:
    with pytest.raises(PlanRejected, match="universe"):
        _cs_plan("rank_cs", **{field: value})


def test_quantile_cs_requires_buckets_of_at_least_two() -> None:
    with pytest.raises(PlanRejected, match="buckets"):
        _cs_plan("quantile_cs")
    for bad in (1, 0, True, "4"):
        with pytest.raises(PlanRejected, match="buckets"):
            _cs_plan("quantile_cs", buckets=bad)


def test_rank_cs_rejects_buckets() -> None:
    with pytest.raises(PlanRejected, match="buckets"):
        _cs_plan("rank_cs", buckets=4)


def test_time_series_transform_under_1_3_0_keeps_its_grammar() -> None:
    universe = {"universe": _universe_text(UNIVERSE), "universe_hash": UNIVERSE.content_hash()}
    with pytest.raises(PlanRejected, match="window"):
        _parse({"transform": "difference"})
    with pytest.raises(PlanRejected, match="universe"):
        _parse({"transform": "difference", "window": 5, **universe})
    with pytest.raises(PlanRejected, match="universe"):
        _parse({"transform": "rank", "window": 5, "universe": universe["universe"]})

    new, _ = _parse({"transform": "quantile", "window": 5, "buckets": 3})
    old, _ = _parse({"transform": "quantile", "window": 5, "buckets": 3}, "1.2.0")
    assert new.nodes == old.nodes
    assert new.content_hash() != old.content_hash()  # the format version is part of the hash


def test_parse_universe_reference_round_trips() -> None:
    reference = parse_universe_reference(_universe_text(UNIVERSE), UNIVERSE.content_hash())

    assert reference.table == UNIVERSE.dataset.table
    assert reference.snapshot_id == UNIVERSE.dataset.snapshot_id
    assert reference.manifest_hash == UNIVERSE.content_hash()
    assert reference.text() == _universe_text(UNIVERSE)


# --- lowering ---------------------------------------------------------------------------------


def test_rank_cs_lowers_to_a_feature_spec_bound_to_the_pinned_universe() -> None:
    plan, resolution = _cs_plan("rank_cs")

    output = cast(
        FeatureSpec,
        lower_typed_plan(plan, resolution=resolution, created_at=NOW, universes=(UNIVERSE,))["xs"],
    )

    assert type(output) is FeatureSpec
    assert output.definition == "p7.transformation.rank_cs@1.0.0"
    assert output.inputs == (SOURCE.ref, UNIVERSE.dataset)
    assert output.lineage == (SOURCE.ref,)
    assert output.available_lag == timedelta(0)
    assert output.deterministic is True
    spec_binding = UNIVERSE.universe_spec
    assert output.params == FrozenMapping(
        {
            "operator": "rank_cs",
            "provider": "p7_transformation_rank_cs@1.0.0",
            "semantic_version": "1.0.0",
            "population": "universe_snapshot_members_at_bar",
            "universe": _universe_text(UNIVERSE),
            "universe_manifest_hash": UNIVERSE.content_hash(),
            "universe_spec": f"{spec_binding.name}@{spec_binding.version}",
            "universe_spec_hash": spec_binding.spec_hash,
            "alignment": "bar_interval_end",
            "missing": "exclude_from_population",
            "min_population": 2,
            "ties": "average",
            "scale": "unit_interval",
            "output_decimal_places": 18,
            "rounding": "half_even",
        }
    )


def test_quantile_cs_lowers_with_explicit_buckets() -> None:
    plan, resolution = _cs_plan("quantile_cs", buckets=5)

    output = cast(
        FeatureSpec,
        lower_typed_plan(plan, resolution=resolution, created_at=NOW, universes=[UNIVERSE])["xs"],
    )

    assert output.definition == "p7.transformation.quantile_cs@1.0.0"
    assert output.params["buckets"] == 5
    assert output.params["provider"] == "p7_transformation_quantile_cs@1.0.0"
    assert "scale" not in output.params
    assert "window" not in output.params


def test_cross_sectional_lowering_is_deterministic_and_universe_is_part_of_identity() -> None:
    other = manifest(dataset=dataset(snapshot_id="9002"))
    plan, resolution = _cs_plan("rank_cs")
    other_plan, other_resolution = _cs_plan("rank_cs", pinned=other)

    first = lower_typed_plan(plan, resolution=resolution, created_at=NOW, universes=[UNIVERSE])
    again = lower_typed_plan(plan, resolution=resolution, created_at=NOW, universes=[UNIVERSE])
    moved = lower_typed_plan(
        other_plan, resolution=other_resolution, created_at=NOW, universes=[other]
    )

    assert first["xs"].ref == again["xs"].ref
    assert first["xs"].content_hash() == again["xs"].content_hash()
    assert first["xs"].ref != moved["xs"].ref


def test_cross_sectional_lowering_refuses_without_the_pinned_manifest() -> None:
    plan, resolution = _cs_plan("rank_cs")

    with pytest.raises(OperatorLoweringRefused) as refused:
        lower_typed_plan(plan, resolution=resolution, created_at=NOW)
    assert refused.value.code == "unresolved_universe"

    other = manifest(dataset=dataset(snapshot_id="9002"))
    with pytest.raises(OperatorLoweringRefused) as refused:
        lower_typed_plan(plan, resolution=resolution, created_at=NOW, universes=[other])
    assert refused.value.code == "unresolved_universe"


def test_cross_sectional_lowering_refuses_a_universe_text_naming_another_dataset() -> None:
    # The hash pins UNIVERSE but the text names another snapshot: the binding is inconsistent.
    plan, resolution = _cs_plan("rank_cs", universe="research_dataset:research.first_slice_1m@1")
    with pytest.raises(OperatorLoweringRefused) as refused:
        lower_typed_plan(plan, resolution=resolution, created_at=NOW, universes=[UNIVERSE])
    assert refused.value.code == "universe_binding_mismatch"


def test_cross_sectional_lowering_rejects_non_manifest_universes() -> None:
    plan, resolution = _cs_plan("rank_cs")
    with pytest.raises(TypeError, match="ResearchDatasetManifest"):
        lower_typed_plan(
            plan,
            resolution=resolution,
            created_at=NOW,
            universes=cast(Any, [UNIVERSE.dataset]),
        )


def test_cross_sectional_output_binds_via_adr_0078() -> None:
    plan, resolution = _cs_plan("quantile_cs", buckets=4)
    outputs = lower_typed_plan(plan, resolution=resolution, created_at=NOW, universes=[UNIVERSE])

    bindings = produce_lowered_output_bindings(
        experiment_hash="d" * 64, plan=plan, specs_by_node=outputs
    )

    assert len(bindings) == 1
    assert bindings[0].spec.content_hash() == outputs["xs"].content_hash()

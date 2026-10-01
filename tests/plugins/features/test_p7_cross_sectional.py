"""ADR-0100 §2: cross-sectional ``rank_cs`` / ``quantile_cs`` Providers over a pinned universe."""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any, cast

import pytest

from core.contracts.feature import FeatureInputError, UnsupportedFeature
from core.contracts.universe import DegradedEpisodeKey, ResearchDatasetManifest
from core.domain.base import FrozenMapping, Kind, Ref, VersionedSpec
from core.domain.specs import FeatureSpec
from plugins.features.p7_cross_sectional import (
    CrossSectionalEvaluation,
    CrossSectionalRequest,
    CrossSectionalResult,
    MemberBarValue,
    P7QuantileCsProvider,
    P7RankCsProvider,
)
from research.hypotheses.typed_plan import PlanLimits, parse_plan_json
from research.hypotheses.typed_plan_lowering import lower_typed_plan
from research.hypotheses.typed_plan_resolver import resolve_direct_references
from tests.test_universe_contracts import (
    BTC,
    ETH,
    SIM,
    SIM_END,
    SIM_MID,
    T0,
    degraded,
    interval_pit,
    manifest,
    member,
)

SOL = degraded("SOLUSDT", T0 + timedelta(days=2))
SOURCE = FeatureSpec(
    name="momentum_source",
    version="1.0.0",
    created_at=SIM,
    definition="source",
    inputs=(Ref(kind=Kind.REPRESENTATION, name="canonical_bar_1m", version="1.0.0"),),
    params=FrozenMapping({}),
    available_lag=timedelta(0),
)
#: Interval universe: BTC leaves at SIM_MID; ETH and SOL stay for the whole window.
UNIVERSE = manifest(
    point_in_time=interval_pit(),
    members=(
        member(BTC, "lr-1", effective_from=SIM, effective_until=SIM_MID),
        member(ETH, "lr-2", effective_from=SIM, effective_until=SIM_END),
        member(SOL, "lr-3", effective_from=SIM, effective_until=SIM_END),
    ),
)
BAR = SIM + timedelta(minutes=1)
LATE_BAR = SIM_MID + timedelta(minutes=1)
CUTOFF = SIM_END + timedelta(days=1)
BTC_KEY = BTC.observation_key()
ETH_KEY = ETH.observation_key()
SOL_KEY = SOL.observation_key()


class _Resolver:
    def resolve(self, ref: Ref) -> VersionedSpec | None:
        return SOURCE if ref == SOURCE.ref else None


def _lowered(
    transform: str, universe: ResearchDatasetManifest = UNIVERSE, **extra: Any
) -> FeatureSpec:
    parameters: dict[str, Any] = {
        "transform": transform,
        "universe": f"research_dataset:{universe.dataset.table}@{universe.dataset.snapshot_id}",
        "universe_hash": universe.content_hash(),
        **extra,
    }
    payload = {
        "schema_version": "1.3.0",
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
    plan = parse_plan_json(
        json.dumps(payload),
        limits=PlanLimits(max_depth=2, max_nodes=2, max_json_bytes=4096, max_parameters_per_node=4),
    )
    resolution = resolve_direct_references(plan, resolver=_Resolver())
    outputs = lower_typed_plan(plan, resolution=resolution, created_at=SIM, universes=[universe])
    return cast(FeatureSpec, outputs["xs"])


RANK_SPEC = _lowered("rank_cs")
QUANTILE_SPEC = _lowered("quantile_cs", buckets=4)


def _value(
    episode: DegradedEpisodeKey,
    value: Decimal | int | None,
    *,
    bar: datetime = BAR,
    available: datetime | None = None,
) -> MemberBarValue:
    at = bar if available is None else available
    return MemberBarValue(
        member=episode.observation_key(),
        interval_end=bar,
        available_time=at,
        knowledge_time=at,
        value=value,
    )


def _request(
    spec: FeatureSpec,
    observations: tuple[MemberBarValue, ...],
    *,
    evaluations: tuple[CrossSectionalEvaluation, ...] | None = None,
    universe: ResearchDatasetManifest = UNIVERSE,
) -> CrossSectionalRequest:
    return CrossSectionalRequest(
        feature=spec.ref,
        spec_hash=spec.content_hash(),
        source=SOURCE.ref,
        universe_manifest_hash=universe.content_hash(),
        knowledge_cutoff=CUTOFF,
        evaluations=evaluations or (CrossSectionalEvaluation(BAR, BAR),),
        observations=observations,
    )


def _by_member(result: CrossSectionalResult) -> dict[str, Decimal | int | None]:
    return {item.member: item.value for item in result.values}


def test_rank_cs_uses_average_ranks_for_ties() -> None:
    provider = P7RankCsProvider([RANK_SPEC], [UNIVERSE])
    result = provider.compute(
        _request(RANK_SPEC, (_value(BTC, 1), _value(ETH, Decimal("1")), _value(SOL, 3)))
    )

    # n = 3: BTC / ETH tie -> (0 + 0.5 * (2 - 1)) / 2 = 0.25; SOL -> (2 + 0) / 2 = 1.
    assert _by_member(result) == {
        BTC_KEY: Decimal("0.25"),
        ETH_KEY: Decimal("0.25"),
        SOL_KEY: Decimal(1),
    }
    assert {item.population for item in result.values} == {3}
    assert all(item.latest_input_available_time == BAR for item in result.values)
    assert provider.descriptor.plugin_key == "p7_transformation_rank_cs@1.0.0"


def test_rank_cs_is_emitted_at_the_declared_decimal_places() -> None:
    provider = P7RankCsProvider([RANK_SPEC], [UNIVERSE])
    values = (_value(BTC, 1), _value(ETH, 2), _value(SOL, 3))
    result = provider.compute(_request(RANK_SPEC, values))

    middle = _by_member(result)[ETH_KEY]
    assert isinstance(middle, Decimal)
    assert middle == Decimal("0.5")
    assert middle.as_tuple().exponent == -18


def test_quantile_cs_floors_the_exact_rank_and_clips_the_top_bucket() -> None:
    provider = P7QuantileCsProvider([QUANTILE_SPEC], [UNIVERSE])
    result = provider.compute(
        _request(QUANTILE_SPEC, (_value(BTC, 1), _value(ETH, 1), _value(SOL, 3)))
    )

    # rank 0.25 * 4 = 1 -> bucket 1; rank 1 * 4 = 4 -> clipped to buckets - 1 = 3.
    assert _by_member(result) == {BTC_KEY: 1, ETH_KEY: 1, SOL_KEY: 3}


def test_missing_values_are_excluded_from_the_population() -> None:
    provider = P7RankCsProvider([RANK_SPEC], [UNIVERSE])
    values = (_value(BTC, 5), _value(ETH, None), _value(SOL, 9))
    result = provider.compute(_request(RANK_SPEC, values))

    assert _by_member(result) == {BTC_KEY: Decimal(0), ETH_KEY: None, SOL_KEY: Decimal(1)}
    assert {item.population for item in result.values} == {2}


def test_fewer_than_two_valid_members_gives_missing_values() -> None:
    provider = P7RankCsProvider([RANK_SPEC], [UNIVERSE])
    result = provider.compute(_request(RANK_SPEC, (_value(BTC, 5),)))

    assert _by_member(result) == {BTC_KEY: None, ETH_KEY: None, SOL_KEY: None}
    assert all(item.latest_input_available_time is None for item in result.values)


def test_only_values_available_by_the_evaluation_time_are_used() -> None:
    provider = P7RankCsProvider([RANK_SPEC], [UNIVERSE])
    late = BAR + timedelta(seconds=30)
    values = (_value(BTC, 1), _value(ETH, 2), _value(SOL, 3, available=late))
    early = provider.compute(_request(RANK_SPEC, values))
    after = provider.compute(
        _request(RANK_SPEC, values, evaluations=(CrossSectionalEvaluation(late, BAR),))
    )

    assert _by_member(early) == {BTC_KEY: Decimal(0), ETH_KEY: Decimal(1), SOL_KEY: None}
    assert _by_member(after)[SOL_KEY] == Decimal(1)
    assert _by_member(after)[ETH_KEY] == Decimal("0.5")


def test_a_later_visible_revision_replaces_the_earlier_value() -> None:
    provider = P7RankCsProvider([RANK_SPEC], [UNIVERSE])
    revised = BAR + timedelta(seconds=10)
    values = (
        _value(BTC, 10),
        _value(BTC, 0, available=revised),
        _value(ETH, 5),
        _value(SOL, 7),
    )
    before = provider.compute(_request(RANK_SPEC, values))
    after = provider.compute(
        _request(RANK_SPEC, values, evaluations=(CrossSectionalEvaluation(revised, BAR),))
    )

    assert _by_member(before)[BTC_KEY] == Decimal(1)
    assert _by_member(after)[BTC_KEY] == Decimal(0)


def test_population_is_the_membership_effective_at_the_bar() -> None:
    provider = P7RankCsProvider([RANK_SPEC], [UNIVERSE])
    values = (
        _value(BTC, 1, bar=LATE_BAR),  # BTC left the universe at SIM_MID
        _value(ETH, 2, bar=LATE_BAR),
        _value(SOL, 3, bar=LATE_BAR),
        _value(ETH, 100),  # another bar: never mixed into LATE_BAR's cross-section
    )
    result = provider.compute(
        _request(RANK_SPEC, values, evaluations=(CrossSectionalEvaluation(LATE_BAR, LATE_BAR),))
    )

    assert _by_member(result) == {ETH_KEY: Decimal(0), SOL_KEY: Decimal(1)}


def test_point_simulation_universe_is_only_effective_at_its_simulation_time() -> None:
    point = manifest()  # simulation_time = SIM; members BTC and ETH
    spec = _lowered("rank_cs", point)
    provider = P7RankCsProvider([spec], [point])
    values = (_value(BTC, 1, bar=SIM), _value(ETH, 2, bar=SIM), _value(BTC, 1), _value(ETH, 2))
    result = provider.compute(
        _request(
            spec,
            values,
            universe=point,
            evaluations=(CrossSectionalEvaluation(SIM, SIM), CrossSectionalEvaluation(BAR, BAR)),
        )
    )

    assert [(item.evaluation_time, item.value) for item in result.values] == [
        (SIM, Decimal(0)),
        (SIM, Decimal(1)),
    ]


def test_a_value_for_a_non_member_is_refused() -> None:
    provider = P7RankCsProvider([RANK_SPEC], [UNIVERSE])
    stranger = MemberBarValue(
        member=degraded("DOGEUSDT", T0).observation_key(),
        interval_end=BAR,
        available_time=BAR,
        knowledge_time=BAR,
        value=1,
    )
    with pytest.raises(FeatureInputError, match="not a member"):
        provider.compute(_request(RANK_SPEC, (_value(ETH, 1), stranger)))


def test_unsupported_spec_or_universe_is_refused() -> None:
    provider = P7RankCsProvider([RANK_SPEC], [UNIVERSE])
    with pytest.raises(UnsupportedFeature):
        provider.compute(_request(QUANTILE_SPEC, (_value(ETH, 1),)))
    other = manifest()
    with pytest.raises(FeatureInputError, match="universe manifest"):
        provider.compute(_request(RANK_SPEC, (_value(ETH, 1),), universe=other))


def test_provider_refuses_a_spec_without_its_pinned_manifest_or_of_another_kind() -> None:
    with pytest.raises(ValueError, match="universe_manifest_hash"):
        P7RankCsProvider([RANK_SPEC], [manifest()])
    with pytest.raises(ValueError, match="definition"):
        P7RankCsProvider([QUANTILE_SPEC], [UNIVERSE])
    with pytest.raises(ValueError, match="at least one spec"):
        P7QuantileCsProvider([], [UNIVERSE])


def test_request_rejects_floats_bools_and_premature_availability() -> None:
    for bad in (cast(Any, 1.5), cast(Any, True), Decimal("NaN")):
        with pytest.raises(ValueError, match="value"):
            _value(ETH, bad)
    with pytest.raises(ValueError, match="before its bar closes"):
        _value(ETH, 1, available=BAR - timedelta(seconds=1))
    with pytest.raises(ValueError, match="interval_end"):
        CrossSectionalEvaluation(BAR, BAR + timedelta(minutes=1))
    with pytest.raises(ValueError, match="duplicate"):
        _request(RANK_SPEC, (_value(ETH, 1), _value(ETH, 2)))


def test_compute_is_deterministic_and_result_hash_is_recomputed() -> None:
    provider = P7QuantileCsProvider([QUANTILE_SPEC], [UNIVERSE])
    values = (_value(SOL, 3), _value(BTC, 1), _value(ETH, 2))
    first = provider.compute(_request(QUANTILE_SPEC, values))
    again = provider.compute(_request(QUANTILE_SPEC, tuple(reversed(values))))

    assert first == again
    assert first.result_hash == again.result_hash
    with pytest.raises(ValueError, match="result_hash"):
        CrossSectionalResult(
            request_hash=first.request_hash,
            provider=first.provider,
            provider_hash=first.provider_hash,
            values=first.values,
            result_hash="0" * 64,
        )

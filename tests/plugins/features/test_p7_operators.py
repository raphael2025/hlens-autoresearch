"""ADR-0082 / ADR-0099 / ADR-0100: P7 feature operator Providers, hand-checked values.

The upstream is ``bar_volume_sum_<n>`` (exact sum of the latest ``n`` bars' volume), so with
``n = 1`` the upstream series is just the per-bar volume.
"""

from __future__ import annotations

import json
import statistics
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from decimal import ROUND_HALF_EVEN, Context, Decimal
from typing import Any, cast

import pytest

from core.contracts.feature import (
    FeatureInputError,
    FeatureObservation,
    FeatureRequest,
    UnsupportedFeature,
)
from core.contracts.universe import SelectedRevisionLineage
from core.domain.base import FrozenMapping, Ref, VersionedSpec, content_hash
from core.domain.specs import FeatureSpec
from plugins.features import BarVolumeSumProvider
from plugins.features.p7_operators import (
    P7_FEATURE_PROVIDERS,
    P7DifferenceProvider,
    P7InteractionProductProvider,
    P7QuantileTsProvider,
    P7RankTsProvider,
    P7SmoothSmaProvider,
    P7StandardizeProvider,
    UpstreamFeature,
)
from research.hypotheses.typed_plan import PlanLimits, parse_plan_json
from research.hypotheses.typed_plan_lowering import lower_typed_plan
from research.hypotheses.typed_plan_resolver import resolve_direct_references

T0 = datetime(2024, 3, 1, 12, tzinfo=UTC)
MINUTE = timedelta(minutes=1)
CUTOFF = datetime(2024, 3, 2, tzinfo=UTC)
MANIFEST = content_hash({"manifest": "p7-operators"})
CREATED = datetime(2026, 10, 1, tzinfo=UTC)
CONTEXT = Context(prec=50, rounding=ROUND_HALF_EVEN)
#: Per-bar volumes of minutes 0..5; all six bars are visible at ``AT``.
VOLUMES = (10, 12, 15, 11, 20, 18)

VOL1 = BarVolumeSumProvider.spec(1)
VOL2 = BarVolumeSumProvider.spec(2)
LIMITS = PlanLimits(max_depth=2, max_nodes=2, max_json_bytes=4096, max_parameters_per_node=4)


def bar(
    minute: int, volume: int, *, delay: int = 0, symbol: str = "BTC-USDT"
) -> FeatureObservation:
    start = T0 + minute * MINUTE
    values: dict[str, Decimal | int | str] = {
        "symbol": symbol,
        "open": Decimal(100),
        "high": Decimal(101),
        "low": Decimal(99),
        "close": Decimal(100),
        "volume": Decimal(volume),
        "trade_count": 3,
    }
    return FeatureObservation(
        observation_key=f"bar:{symbol}:{minute}",
        event_time=start,
        event_end_time=start + MINUTE,
        available_time=start + MINUTE + delay * MINUTE,
        knowledge_time=start + MINUTE + (delay + 1) * MINUTE,
        values=FrozenMapping(values),
        lineage=SelectedRevisionLineage(
            canonical_table="canonical.bars_1m",
            canonical_revision_id=f"crev-{minute}",
            raw_table="raw.binance_spot_klines_1m",
            raw_revision_id=f"raw-{minute}",
            source_table="raw.binance_spot_archives",
            source_revision_id="archive-1",
        ),
    )


def bars(volumes: Sequence[int] = VOLUMES) -> tuple[FeatureObservation, ...]:
    return tuple(bar(index, volume) for index, volume in enumerate(volumes))


AT = T0 + 6 * MINUTE


class _Resolver:
    def __init__(self, *specs: VersionedSpec) -> None:
        self.specs = {str(spec.ref): spec for spec in specs}

    def resolve(self, ref: Ref) -> VersionedSpec | None:
        return self.specs.get(str(ref))


def lowered(operator: str, parameters: dict[str, Any], *sources: FeatureSpec) -> FeatureSpec:
    """The pure-lowering output of a one-node plan over ``sources``."""
    payload = {
        "schema_version": "1.3.0",
        "root": "x",
        "nodes": [
            {
                "id": "x",
                "operator": operator,
                "inputs": [
                    {"ref": str(source.ref), "content_hash": source.content_hash()}
                    for source in sources
                ],
                "parameters": parameters,
            }
        ],
    }
    plan = parse_plan_json(json.dumps(payload), limits=LIMITS)
    resolution = resolve_direct_references(plan, resolver=_Resolver(*sources))
    outputs = lower_typed_plan(plan, resolution=resolution, created_at=CREATED)
    return cast(FeatureSpec, outputs["x"])


def upstream(*sources: FeatureSpec) -> dict[str, UpstreamFeature]:
    return {
        str(source.ref): UpstreamFeature(source, BarVolumeSumProvider((source,)))
        for source in sources
    }


def transformation(transform: str, **parameters: Any) -> FeatureSpec:
    return lowered("transformation", {"transform": transform, **parameters}, VOL1)


def evaluate(
    provider: Any,
    spec: FeatureSpec,
    observations: Sequence[FeatureObservation],
    *times: datetime,
) -> list[Decimal | int | None]:
    request = FeatureRequest(
        feature=spec.ref,
        spec_hash=spec.content_hash(),
        manifest_content_hash=MANIFEST,
        knowledge_cutoff=CUTOFF,
        evaluation_times=times or (AT,),
        observations=tuple(observations),
    )
    result = provider.compute(request)
    result.check_answers(request, provider.descriptor, spec.available_lag)
    return [item.value for item in result.values]


def run(
    provider_class: Any,
    spec: FeatureSpec,
    observations: Sequence[FeatureObservation] | None = None,
    *times: datetime,
    sources: tuple[FeatureSpec, ...] = (VOL1,),
) -> list[Decimal | int | None]:
    provider = provider_class((spec,), upstream=upstream(*sources))
    return evaluate(provider, spec, bars() if observations is None else observations, *times)


# --- hand-checked values ----------------------------------------------------------------------


def test_difference_is_the_exact_window_bar_lag_difference() -> None:
    spec = transformation("difference", window=2)
    # window + 1 = 3 bars: minutes 3, 4, 5 -> 18 - 11
    assert run(P7DifferenceProvider, spec) == [Decimal(7)]
    # as of minute 4 (bar 4 closes at T0 + 5 min): bars 2, 3, 4 -> 20 - 15
    assert run(P7DifferenceProvider, spec, None, T0 + 5 * MINUTE) == [Decimal(5)]


def test_smooth_is_the_simple_moving_average() -> None:
    spec = transformation("smooth", window=3)
    # the provider's 50-digit half-even division of (11 + 20 + 18) by 3
    assert run(P7SmoothSmaProvider, spec) == [CONTEXT.divide(Decimal(49), Decimal(3))]
    assert run(P7SmoothSmaProvider, transformation("smooth", window=2)) == [Decimal(19)]


def test_standardize_is_the_rolling_population_z_score() -> None:
    spec = transformation("standardize", window=3)
    (value,) = run(P7StandardizeProvider, spec)
    series = [11.0, 20.0, 18.0]
    expected = (series[-1] - statistics.fmean(series)) / statistics.pstdev(series)
    assert isinstance(value, Decimal)
    assert float(value) == pytest.approx(expected, rel=1e-12)


def test_standardize_with_zero_dispersion_is_missing() -> None:
    spec = transformation("standardize", window=3)
    assert run(P7StandardizeProvider, spec, bars((10, 10, 10, 10, 10, 10))) == [None]


def test_rank_ts_is_the_percentile_rank_with_averaged_ties() -> None:
    spec = transformation("rank", window=4)
    # window values 15, 11, 20, 18; current 18: two smaller, no tie -> 2 / 3
    assert run(P7RankTsProvider, spec) == [CONTEXT.divide(Decimal(2), Decimal(3))]
    tied = transformation("rank", window=3)
    assert run(P7RankTsProvider, tied, bars((5, 5, 5, 5, 5, 5))) == [Decimal("0.5")]
    # the current value is the smallest / the largest of the window
    assert run(P7RankTsProvider, tied, bars((9, 9, 9, 9, 9, 1))) == [Decimal(0)]
    assert run(P7RankTsProvider, tied, bars((1, 1, 1, 1, 1, 9))) == [Decimal(1)]


def test_quantile_ts_is_the_floored_rank_bucket_clipped_to_the_last_one() -> None:
    spec = transformation("quantile", window=4, buckets=4)
    assert run(P7QuantileTsProvider, spec) == [2]  # floor(2/3 * 4)
    (top,) = run(P7QuantileTsProvider, spec, bars((1, 2, 3, 4, 5, 6)))
    assert top == 3 and type(top) is int  # rank 1 -> bucket 4, clipped to buckets - 1
    (bottom,) = run(P7QuantileTsProvider, spec, bars((6, 5, 4, 3, 2, 1)))
    assert bottom == 0 and type(bottom) is int


def test_interaction_product_is_exact_and_aligned_at_the_evaluation_time() -> None:
    spec = lowered("interaction", {}, VOL1, VOL2)
    # volume of the latest bar (18) times the sum of the latest two bars (20 + 18)
    assert run(P7InteractionProductProvider, spec, sources=(VOL1, VOL2)) == [Decimal(18 * 38)]
    # as of minute 4 the latest bar is 20 and the latest two bars sum to 11 + 20
    assert run(P7InteractionProductProvider, spec, None, T0 + 5 * MINUTE, sources=(VOL1, VOL2)) == [
        Decimal(20 * 31)
    ]


# --- missing values and the bar grid ----------------------------------------------------------


def test_too_little_history_is_missing() -> None:
    spec = transformation("difference", window=2)
    assert run(P7DifferenceProvider, spec, bars((10, 12))) == [None]
    # before any bar is visible
    assert run(P7DifferenceProvider, spec, None, T0) == [None]


def test_a_gap_in_the_trailing_run_is_missing_not_filled() -> None:
    spec = transformation("smooth", window=3)
    gapped = (bar(0, 10), bar(1, 12), bar(2, 11), bar(4, 20), bar(5, 18))  # minute 3 missing
    assert run(P7SmoothSmaProvider, spec, gapped) == [None]
    # a gap earlier than the trailing run does not matter
    gap_before = (bar(0, 10), bar(2, 11), bar(3, 20), bar(4, 18), bar(5, 12))
    assert run(P7SmoothSmaProvider, spec, gap_before) == [CONTEXT.divide(Decimal(50), Decimal(3))]


def test_an_upstream_gap_propagates_as_missing() -> None:
    # the upstream (sum of two bars) is missing wherever the two latest bars are not contiguous
    spec = lowered("transformation", {"transform": "smooth", "window": 2}, VOL2)
    gapped = (bar(0, 10), bar(1, 12), bar(3, 11), bar(4, 20), bar(5, 18))
    provider = P7SmoothSmaProvider((spec,), upstream=upstream(VOL2))
    # window 2 over bars 4, 5 -> upstream values at bars 4 and 5 are 11 + 20 and 20 + 18
    assert evaluate(provider, spec, gapped) == [Decimal((31 + 38) / 2)]
    broken = (bar(0, 10), bar(2, 12), bar(4, 11), bar(5, 20))
    assert evaluate(provider, spec, broken) == [None]


# --- point-in-time ----------------------------------------------------------------------------


def test_a_value_never_uses_data_available_after_the_evaluation_time() -> None:
    spec = transformation("difference", window=2)
    provider = P7DifferenceProvider((spec,), upstream=upstream(VOL1))
    early = evaluate(provider, spec, bars(), T0 + 5 * MINUTE)
    # a later bar, available only after the evaluation time, changes nothing
    later = (*bars(), bar(6, 1000))
    assert evaluate(provider, spec, later, T0 + 5 * MINUTE) == early
    # a bar that closed before but became available after the evaluation time is not visible
    delayed = (*bars(VOLUMES[:5]), bar(5, 999, delay=10))
    assert evaluate(provider, spec, delayed, T0 + 6 * MINUTE) == [Decimal(5)]


def test_two_evaluation_times_are_independent_of_each_other() -> None:
    spec = transformation("difference", window=2)
    provider = P7DifferenceProvider((spec,), upstream=upstream(VOL1))
    both = evaluate(provider, spec, bars(), T0 + 5 * MINUTE, AT)
    assert both == [
        evaluate(provider, spec, bars(), T0 + 5 * MINUTE)[0],
        evaluate(provider, spec, bars(), AT)[0],
    ]
    assert evaluate(provider, spec, bars(), AT) == evaluate(provider, spec, bars(), AT)


# --- refusals ---------------------------------------------------------------------------------


def test_two_symbols_in_one_request_are_refused() -> None:
    spec = transformation("difference", window=2)
    mixed = (bar(0, 10), bar(1, 12, symbol="ETH-USDT"), bar(2, 15))
    with pytest.raises(FeatureInputError, match="one symbol"):
        run(P7DifferenceProvider, spec, mixed)


def test_overlapping_bars_are_refused() -> None:
    spec = transformation("smooth", window=2)
    first = bar(0, 10)
    overlapping = bar(1, 12).model_copy(
        update={
            "event_time": T0 + timedelta(seconds=30),
            "event_end_time": T0 + 90 * timedelta(seconds=1),
        }
    )
    with pytest.raises(FeatureInputError, match="overlap"):
        run(P7SmoothSmaProvider, spec, (first, overlapping))


def test_a_request_for_another_feature_or_hash_is_unsupported() -> None:
    spec = transformation("difference", window=2)
    other = transformation("difference", window=3)
    provider = P7DifferenceProvider((spec,), upstream=upstream(VOL1))
    with pytest.raises(UnsupportedFeature):
        evaluate(provider, other, bars())
    request = FeatureRequest(
        feature=spec.ref,
        spec_hash="0" * 64,
        manifest_content_hash=MANIFEST,
        knowledge_cutoff=CUTOFF,
        evaluation_times=(AT,),
        observations=bars(),
    )
    with pytest.raises(UnsupportedFeature):
        provider.compute(request)


def test_construction_refuses_a_spec_that_is_not_the_lowered_form() -> None:
    spec = transformation("difference", window=2)
    table = upstream(VOL1)
    with pytest.raises(ValueError, match="definition"):
        P7SmoothSmaProvider((spec,), upstream=table)  # another Provider's definition
    with pytest.raises(ValueError, match="params differ"):
        P7DifferenceProvider(
            (
                spec.model_copy(
                    update={"params": FrozenMapping({**spec.params, "missing": "fill"})}
                ),
            ),
            upstream=table,
        )
    with pytest.raises(ValueError, match="available_lag"):
        P7DifferenceProvider(
            (spec.model_copy(update={"available_lag": timedelta(minutes=1)}),), upstream=table
        )
    with pytest.raises(ValueError, match="lineage"):
        P7DifferenceProvider((spec.model_copy(update={"lineage": ()}),), upstream=table)
    with pytest.raises(ValueError, match="at least one spec"):
        P7DifferenceProvider((), upstream=table)
    with pytest.raises(ValueError, match="declared twice"):
        P7DifferenceProvider((spec, spec), upstream=table)
    with pytest.raises(TypeError, match="FeatureSpec"):
        P7DifferenceProvider((object(),), upstream=table)  # type: ignore[arg-type]


def test_construction_refuses_a_missing_or_inconsistent_upstream_table() -> None:
    spec = transformation("difference", window=2)
    with pytest.raises(ValueError, match="not in the upstream table"):
        P7DifferenceProvider((spec,), upstream={})
    with pytest.raises(ValueError, match="does not name its spec"):
        P7DifferenceProvider(
            (spec,), upstream={"feature:other@1.0.0": upstream(VOL1)[str(VOL1.ref)]}
        )
    wrong_provider = {str(VOL1.ref): UpstreamFeature(VOL1, BarVolumeSumProvider((VOL2,)))}
    with pytest.raises(ValueError, match="does not support that spec"):
        P7DifferenceProvider((spec,), upstream=wrong_provider)
    with pytest.raises(ValueError, match="UpstreamFeature"):
        P7DifferenceProvider((spec,), upstream={str(VOL1.ref): object()})  # type: ignore[dict-item]


def test_a_product_arity_other_than_two_is_refused() -> None:
    spec = transformation("difference", window=2)  # one input
    with pytest.raises(ValueError):
        P7InteractionProductProvider((spec,), upstream=upstream(VOL1))


# --- registry ---------------------------------------------------------------------------------


def test_every_provider_class_declares_its_definition_and_key() -> None:
    assert len(P7_FEATURE_PROVIDERS) == 6
    for definition, cls in P7_FEATURE_PROVIDERS.items():
        assert cls.DEFINITION == definition
        assert cls.plugin_key() == f"{cls.NAME}@{cls.VERSION}"


def test_the_provider_key_equals_the_provider_the_lowering_writes_into_the_spec() -> None:
    cases: list[tuple[Any, FeatureSpec]] = [
        (P7StandardizeProvider, transformation("standardize", window=3)),
        (P7DifferenceProvider, transformation("difference", window=2)),
        (P7SmoothSmaProvider, transformation("smooth", window=3)),
        (P7RankTsProvider, transformation("rank", window=4)),
        (P7QuantileTsProvider, transformation("quantile", window=4, buckets=4)),
        (P7InteractionProductProvider, lowered("interaction", {}, VOL1, VOL2)),
    ]
    for cls, spec in cases:
        assert spec.definition == cls.DEFINITION
        assert spec.params["provider"] == cls.plugin_key()
        provider = cls((spec,), upstream=upstream(VOL1, VOL2))
        assert provider.descriptor.supports(spec.ref, spec.content_hash())
        assert provider.descriptor.name == cls.NAME


def test_provenance_reports_the_latest_visible_input_time() -> None:
    spec = transformation("difference", window=2)
    provider = P7DifferenceProvider((spec,), upstream=upstream(VOL1))
    request = FeatureRequest(
        feature=spec.ref,
        spec_hash=spec.content_hash(),
        manifest_content_hash=MANIFEST,
        knowledge_cutoff=CUTOFF,
        evaluation_times=(AT,),
        observations=bars(),
    )
    (answer,) = provider.compute(request).values
    assert answer.latest_input_available_time == T0 + 6 * MINUTE
    assert 1 <= answer.inputs_used <= len(VOLUMES)
    (missing,) = provider.compute(request.model_copy(update={"observations": bars()[:1]})).values
    assert missing.value is None and missing.inputs_used == 0


def test_a_bool_upstream_value_is_refused() -> None:
    from core.contracts.feature import FeatureResult, FeatureValue, ProviderDescriptor

    class _Text:
        def __init__(self, source: FeatureSpec, value: object) -> None:
            self.value = value
            self.descriptor = ProviderDescriptor(
                name="text_source",
                version="1.0.0",
                deterministic=True,
                supported_features=FrozenMapping({str(source.ref): source.content_hash()}),
            )

        def compute(self, request: FeatureRequest) -> FeatureResult:
            values = [
                FeatureValue(
                    evaluation_time=at,
                    value=self.value,  # type: ignore[arg-type]
                    inputs_used=len(request.visible_at(at, timedelta(0))),
                    latest_input_available_time=max(
                        item.available_time for item in request.visible_at(at, timedelta(0))
                    ),
                )
                for at in request.evaluation_times
            ]
            return FeatureResult.build(request, self.descriptor, values)

    for bad in (True, False):
        spec = transformation("smooth", window=2)
        table = {str(VOL1.ref): UpstreamFeature(VOL1, _Text(VOL1, bad))}
        provider = P7SmoothSmaProvider((spec,), upstream=table)
        with pytest.raises(FeatureInputError, match="not a Decimal or int"):
            evaluate(provider, spec, bars())

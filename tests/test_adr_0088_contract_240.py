"""ADR-0088: the additive contract 2.4.0 (composition, event bar spec, peak equity, synthetic
effects, the volatility-scaled triple barrier and ``UniverseMember.assumption``).

Contract layer only:

* the version registry (2.4.0 current, every earlier minor still published) and the five new
  models appended to the registry;
* the 2.4.0 version boundary: ``_MODEL_SINCE`` / ``_FIELDS_SINCE`` / ``_VALUES_SINCE`` and the
  widened ``SyntheticMarketSpec.effects`` refuse the new content in a 2.0.0 ~ 2.3.0 envelope and
  inside an older replay scope;
* old payloads (a 2.3.0 envelope, written before ADR-0088) read as recorded and keep their
  canonical bytes and content hash (golden pins taken before the bump);
* every structural rule of decisions 1 ~ 5;
* Schema: the new models' committed Schemas equal the export, and each changed published Schema
  differs from its 2.3.0 bytes only by the ADR-0088 additions and the envelope default.

Providers, lowering, Registry resolution and the generator are later batches (not here).
Every name, number and hash is a TEST ONLY fixture.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from core.contracts.outcome import OutcomeLabelSpec, OutcomeMethod
from core.contracts.registry import CONTRACT_MODELS, export_json_schemas
from core.contracts.revision import PolicyBinding, PolicyRole
from core.contracts.strategy import PortfolioState
from core.contracts.synthetic import (
    JumpEffect,
    PlantedEffect,
    SyntheticMarketSpec,
    VolatilityClusteringEffect,
)
from core.contracts.universe import (
    LISTING_BACKFILL_ASSUMPTION_ID,
    StableEpisodeKey,
    UniverseMember,
)
from core.domain.base import (
    CONTRACT_SCHEMA_VERSION,
    PUBLISHED_CONTRACT_SCHEMA_VERSIONS,
    Contract,
    Kind,
    Ref,
    canonical_json,
    content_hash,
    contract_schema_version_scope,
)
from core.domain.specs import (
    ADR_0088_VERSION,
    ConditionedStrategy,
    EnsembleStrategy,
    EventSpec,
    InstrumentType,
    NegatedStrategy,
    OutcomeSpec,
    StrategySpec,
)
from tests.contract_version_support import as_published_at

REPO = Path(__file__).resolve().parents[1]
CURRENT_SCHEMA_DIR = REPO / "schemas"

NEW_MODELS: tuple[type[Contract], ...] = (
    ConditionedStrategy,
    EnsembleStrategy,
    NegatedStrategy,
    VolatilityClusteringEffect,
    JumpEffect,
)
OLDER = ("2.0.0", "2.1.0", "2.2.0", "2.3.0")
REFUSED = r"自 2\.4\.0 引入"

T0 = datetime(2024, 6, 1, tzinfo=UTC)
HASH = "a" * 64

FEATURE = Ref(kind=Kind.FEATURE, name="bar_log_return", version="1.0.0")
VOL = Ref(kind=Kind.FEATURE, name="realized_vol", version="1.0.0")
REGIME = Ref(kind=Kind.STATE, name="trend_regime", version="1.0.0")
BARS = Ref(kind=Kind.REPRESENTATION, name="canonical_bar_1m", version="1.0.0")
MOMENTUM = Ref(kind=Kind.STRATEGY, name="momentum", version="1.0.0")
REVERSAL = Ref(kind=Kind.STRATEGY, name="reversal", version="1.0.0")


# ======================================================================================
# 构造辅助（TEST ONLY）
# ======================================================================================


def event(**overrides: Any) -> EventSpec:
    payload: dict[str, Any] = {
        "name": "breakout",
        "version": "1.0.0",
        "trigger": "cross_above",
        "features": (FEATURE,),
    }
    payload.update(overrides)
    return EventSpec(**payload)


def strategy(**overrides: Any) -> StrategySpec:
    payload: dict[str, Any] = {"name": "combo", "version": "1.0.0", "signals": (FEATURE,)}
    payload.update(overrides)
    return StrategySpec(**payload)


def conditioned(**overrides: Any) -> ConditionedStrategy:
    payload: dict[str, Any] = {"base": MOMENTUM, "state": REGIME, "state_value": "up"}
    payload.update(overrides)
    return ConditionedStrategy(**payload)


def ensemble(*members: Ref) -> EnsembleStrategy:
    return EnsembleStrategy(members=members or (MOMENTUM, REVERSAL))


def portfolio(**overrides: Any) -> PortfolioState:
    payload: dict[str, Any] = {"as_of": T0, "equity": Decimal("1000")}
    payload.update(overrides)
    return PortfolioState(**payload)


def outcome_spec() -> OutcomeSpec:
    return OutcomeSpec(
        name="fwd_1d", version="1.0.0", horizon=timedelta(days=1), label_definition="TEST ONLY"
    )


def label_spec(method: OutcomeMethod, **overrides: Any) -> OutcomeLabelSpec:
    payload: dict[str, Any] = {
        "outcome": outcome_spec().ref,
        "outcome_spec_hash": HASH,
        "method": method,
        "horizon": timedelta(days=1),
    }
    if method is OutcomeMethod.TRIPLE_BARRIER:
        payload.update(upper_barrier=Decimal("0.02"), lower_barrier=Decimal("0.01"))
    if method is OutcomeMethod.VOL_SCALED_TRIPLE_BARRIER:
        payload.update(volatility_feature=VOL, barrier_multiplier=Decimal("2"))
    payload.update(overrides)
    return OutcomeLabelSpec(**payload)


def garch(**overrides: Any) -> VolatilityClusteringEffect:
    payload: dict[str, Any] = {
        "omega": Decimal("0.000001"),
        "alpha": Decimal("0.1"),
        "beta": Decimal("0.85"),
    }
    payload.update(overrides)
    return VolatilityClusteringEffect(**payload)


def jump(**overrides: Any) -> JumpEffect:
    payload: dict[str, Any] = {
        "intensity_per_minute": Decimal("0.001"),
        "jump_scale": Decimal("0.02"),
    }
    payload.update(overrides)
    return JumpEffect(**payload)


def market(**overrides: Any) -> SyntheticMarketSpec:
    payload: dict[str, Any] = {
        "name": "planted",
        "version": "1.0.0",
        "symbol": "SYN",
        "start": T0,
        "minutes": 60,
        "seed": 7,
        "initial_price": Decimal("100"),
        "volatility": Decimal("0.001"),
    }
    payload.update(overrides)
    return SyntheticMarketSpec(**payload)


EPISODE = StableEpisodeKey(
    basis="stable_product_id",
    venue="binance",
    instrument_type=InstrumentType.SPOT,
    venue_product_id="BTCUSDT",
)


def binding(**overrides: Any) -> PolicyBinding:
    payload: dict[str, Any] = {
        "role": PolicyRole.AVAILABILITY,
        "policy_id": LISTING_BACKFILL_ASSUMPTION_ID,
        "version": "1.0.0",
        "policy_hash": HASH,
    }
    payload.update(overrides)
    return PolicyBinding(**payload)


def member(**overrides: Any) -> UniverseMember:
    payload: dict[str, Any] = {"episode": EPISODE, "listing_revision_id": "lr-1"}
    payload.update(overrides)
    return UniverseMember(**payload)


def valid_instances() -> dict[type[Contract], Contract]:
    return {
        ConditionedStrategy: conditioned(),
        EnsembleStrategy: ensemble(),
        NegatedStrategy: NegatedStrategy(base=MOMENTUM),
        VolatilityClusteringEffect: garch(),
        JumpEffect: jump(),
    }


def wire(instance: Contract) -> dict[str, Any]:
    payload: dict[str, Any] = json.loads(instance.model_dump_json())
    return payload


def round_trips(instance: Contract) -> None:
    model = type(instance)
    again = model.model_validate_json(instance.model_dump_json())
    assert again == instance
    assert again.content_hash() == instance.content_hash()
    assert model.model_validate(wire(instance)).content_hash() == instance.content_hash()


# ======================================================================================
# 版本与登记（ADR-0052 §4）
# ======================================================================================


def test_2_4_0_and_every_earlier_minor_stay_published() -> None:
    assert ADR_0088_VERSION == "2.4.0"
    assert CONTRACT_SCHEMA_VERSION == "2.5.0"
    assert PUBLISHED_CONTRACT_SCHEMA_VERSIONS == (
        "2.0.0", "2.1.0", "2.2.0", "2.3.0", "2.4.0", "2.5.0"
    )
    assert PUBLISHED_CONTRACT_SCHEMA_VERSIONS[-1] == CONTRACT_SCHEMA_VERSION


def test_the_new_content_is_declared_since_2_4_0() -> None:
    for model in NEW_MODELS:
        assert model._MODEL_SINCE == "2.4.0", model.__name__
        assert not model._FIELDS_SINCE and not model._VALUES_SINCE, model.__name__
    assert dict(EventSpec._FIELDS_SINCE) == {"bar_spec": "2.4.0"}
    assert dict(StrategySpec._FIELDS_SINCE) == {"composition": "2.4.0"}
    assert dict(PortfolioState._FIELDS_SINCE) == {"peak_equity": "2.4.0"}
    assert dict(UniverseMember._FIELDS_SINCE) == {"assumption": "2.4.0"}
    assert dict(OutcomeLabelSpec._FIELDS_SINCE) == {
        "volatility_feature": "2.4.0",
        "barrier_multiplier": "2.4.0",
    }
    assert dict(OutcomeLabelSpec._VALUES_SINCE["method"]) == {
        OutcomeMethod.VOL_SCALED_TRIPLE_BARRIER: "2.4.0"
    }
    # the published models keep no model-level boundary: only their new fields / values are new
    for model in (
        EventSpec,
        StrategySpec,
        PortfolioState,
        OutcomeLabelSpec,
        PlantedEffect,
        UniverseMember,
    ):
        assert model._MODEL_SINCE is None, model.__name__
    assert not SyntheticMarketSpec._FIELDS_SINCE and not PlantedEffect._FIELDS_SINCE


def test_the_new_optional_fields_default_to_none_and_are_omitted_from_the_payload() -> None:
    for model, name in (
        (EventSpec, "bar_spec"),
        (StrategySpec, "composition"),
        (PortfolioState, "peak_equity"),
        (OutcomeLabelSpec, "volatility_feature"),
        (OutcomeLabelSpec, "barrier_multiplier"),
        (UniverseMember, "assumption"),
    ):
        info = model.model_fields[name]
        assert info.default is None, (model.__name__, name)
        assert info.exclude_if is not None and info.exclude_if(None), (model.__name__, name)
    assert "bar_spec" not in wire(event())
    assert "composition" not in wire(strategy())
    assert "peak_equity" not in wire(portfolio())
    assert "assumption" not in wire(member())
    dumped = wire(label_spec(OutcomeMethod.TRIPLE_BARRIER))
    assert "volatility_feature" not in dumped and "barrier_multiplier" not in dumped


def test_the_new_models_are_appended_to_the_registry() -> None:
    names = tuple(model.__name__ for model in CONTRACT_MODELS)
    assert len(names) == 148
    assert names[:141][-1] == "ResearchDatasetEvidenceManifest"  # earlier models keep their place
    assert CONTRACT_MODELS[141:146] == NEW_MODELS
    assert tuple(model.__name__ for model in CONTRACT_MODELS[146:]) == (
        "PitConflictHeadEvidence",
        "PitConflictEvidenceResult",
    )


@pytest.mark.parametrize("model", NEW_MODELS, ids=lambda m: m.__name__)
def test_new_objects_carry_the_current_2_5_0_envelope_and_round_trip(model: type[Contract]) -> None:
    instance = valid_instances()[model]
    assert instance.schema_version == "2.5.0"
    round_trips(instance)


@pytest.mark.parametrize("old", OLDER)
@pytest.mark.parametrize("model", NEW_MODELS, ids=lambda m: m.__name__)
def test_an_older_envelope_cannot_carry_a_2_4_0_model(model: type[Contract], old: str) -> None:
    payload = {**wire(valid_instances()[model]), "schema_version": old}
    with pytest.raises(ValidationError, match=REFUSED):
        model.model_validate(payload)
    with pytest.raises(ValidationError, match=REFUSED):
        model.model_validate_json(json.dumps(payload))


@pytest.mark.parametrize("old", OLDER)
def test_an_older_envelope_cannot_carry_the_new_fields_or_values(old: str) -> None:
    cases: tuple[tuple[type[Contract], dict[str, Any]], ...] = (
        (EventSpec, wire(event(bar_spec=BARS))),
        (StrategySpec, wire(strategy(composition=NegatedStrategy(base=MOMENTUM)))),
        (PortfolioState, wire(portfolio(peak_equity=Decimal("1200")))),
        (OutcomeLabelSpec, wire(label_spec(OutcomeMethod.VOL_SCALED_TRIPLE_BARRIER))),
        (SyntheticMarketSpec, wire(market(effects=(garch(),)))),
        (SyntheticMarketSpec, wire(market(effects=(jump(),)))),
        (UniverseMember, wire(member(assumption=binding()))),
    )
    for model, payload in cases:
        with pytest.raises(ValidationError, match=REFUSED):
            model.model_validate({**payload, "schema_version": old})


def test_new_content_cannot_be_built_inside_an_older_replay_scope() -> None:
    for old in OLDER:
        with contract_schema_version_scope(old), pytest.raises(ValidationError, match=REFUSED):
            event(bar_spec=BARS)
        with contract_schema_version_scope(old), pytest.raises(ValidationError, match=REFUSED):
            portfolio(peak_equity=Decimal("1200"))
        with contract_schema_version_scope(old), pytest.raises(ValidationError, match=REFUSED):
            member(assumption=binding())
        negated = {"type": "negated", "base": wire(MOMENTUM)}
        with contract_schema_version_scope(old), pytest.raises(ValidationError, match=REFUSED):
            StrategySpec.model_validate({**wire(strategy()), "composition": negated})
        effect = {"kind": "jump", "intensity_per_minute": "0.001", "jump_scale": "1"}
        with contract_schema_version_scope(old), pytest.raises(ValidationError, match=REFUSED):
            recorded = {k: v for k, v in wire(market()).items() if k != "schema_version"}
            SyntheticMarketSpec.model_validate({**recorded, "effects": [effect]})
    with contract_schema_version_scope("2.4.0"):
        assert event(bar_spec=BARS).schema_version == "2.4.0"  # 2.4.0 is published


def test_without_the_new_content_an_older_envelope_still_builds() -> None:
    for old in OLDER:
        with contract_schema_version_scope(old):
            built = (
                event(),
                strategy(),
                portfolio(),
                label_spec(OutcomeMethod.TRIPLE_BARRIER),
                market(effects=(PlantedEffect(lag_minutes=1, strength=Decimal("0.2")),)),
                member(),
            )
        for obj in built:
            assert obj.schema_version == old
            round_trips(obj)


# ======================================================================================
# 旧载荷：按记录读取，规范字节与内容哈希不变（钉值取自 2.4.0 之前的规范 JSON）
# ======================================================================================

#: (model, canonical semantic payload written at 2.3.0, its content hash). TEST ONLY values.
GOLDEN_AT_2_3_0: tuple[tuple[type[Contract], str, str], ...] = (
    (
        EventSpec,
        '{"features":[{"kind":"feature","name":"bar_log_return","schema_version":"2.3.0",'
        '"version":"1.0.0"}],"kind":"event","lineage":[],"name":"breakout",'
        '"observable_lag":"PT0S","schema_version":"2.3.0","states":[],"trigger":"cross_above",'
        '"version":"1.0.0"}',
        "6ce49705c7074f434ec154fcb2ff5190d1254dc10481f9f1868061b86bf2ef7b",
    ),
    (
        StrategySpec,
        '{"applicable_instruments":[],"kind":"strategy","lineage":[],"name":"momentum",'
        '"param_search_space":{},"params":{},"risk_policy":null,"schema_version":"2.3.0",'
        '"signals":[{"kind":"feature","name":"bar_log_return","schema_version":"2.3.0",'
        '"version":"1.0.0"}],"version":"1.0.0"}',
        "e8218bc4bb7233a7aa0f1ce87e3b3a312533b6031d2504b59c7c49178c2a645f",
    ),
    (
        PortfolioState,
        '{"as_of":"2024-06-01T00:00:00Z","current_weights":{"BTCUSDT":"0.5"},"equity":"1000",'
        '"schema_version":"2.3.0"}',
        "448cae5b115cce3d301095bf3d5f043fe3efca9a0e509a80be8866f58a12220c",
    ),
    (
        OutcomeLabelSpec,
        '{"horizon":"P1D","lower_barrier":"0.01","method":"triple_barrier",'
        '"outcome":{"kind":"outcome","name":"fwd_1d","schema_version":"2.3.0","version":"1.0.0"},'
        '"outcome_spec_hash":"' + HASH + '","schema_version":"2.3.0","upper_barrier":"0.02"}',
        "57310fcdde886455288f865026eaa2802e6b4b3cd2a6fcdb480396659c0852f0",
    ),
    (
        SyntheticMarketSpec,
        '{"drift":"0","effects":[{"kind":"return_autocorrelation","lag_minutes":1,'
        '"schema_version":"2.3.0","strength":"0.2"}],"initial_price":"100","minutes":60,'
        '"name":"planted","schema_version":"2.3.0","seed":7,"start":"2024-06-01T00:00:00Z",'
        '"symbol":"SYN","version":"1.0.0","volatility":"0.001"}',
        "86eb327d4f675ecd185e136f170f016123ed4b9fc8f2cf387304be04c568ce02",
    ),
    (
        UniverseMember,
        '{"effective_from":null,"effective_until":null,"episode":{"basis":"stable_product_id",'
        '"instrument_type":"spot","schema_version":"2.3.0","venue":"binance",'
        '"venue_product_id":"BTCUSDT"},"listing_revision_id":"lr-1","schema_version":"2.3.0"}',
        "3badb51ba8e871971683d142f7b801f1d111fcdbe78210312eb72666dc38787d",
    ),
)


@pytest.mark.parametrize(
    ("model", "payload", "pinned"),
    GOLDEN_AT_2_3_0,
    ids=[model.__name__ for model, _, _ in GOLDEN_AT_2_3_0],
)
def test_a_2_3_0_payload_reads_as_recorded_with_its_hash_unchanged(
    model: type[Contract], payload: str, pinned: str
) -> None:
    assert content_hash(json.loads(payload)) == pinned  # the pin is the payload's own hash
    for read in (model.model_validate_json(payload), model.model_validate(json.loads(payload))):
        assert read.schema_version == "2.3.0"  # the recorded envelope is never rewritten
        semantic = read.model_dump(mode="json", exclude=read._non_semantic_fields())
        assert canonical_json(semantic) == payload  # no ADR-0088 key appears
        assert read.content_hash() == pinned
    with contract_schema_version_scope("2.3.0"):  # a replay of the recorded object
        without_envelope = {k: v for k, v in json.loads(payload).items() if k != "schema_version"}
        assert model.model_validate(without_envelope).content_hash() == pinned


def test_an_old_effect_without_a_kind_still_reads_as_return_autocorrelation() -> None:
    payload = json.loads(GOLDEN_AT_2_3_0[-1][1])
    del payload["effects"][0]["kind"]
    for read in (
        SyntheticMarketSpec.model_validate(payload),
        SyntheticMarketSpec.model_validate_json(json.dumps(payload)),
    ):
        (effect,) = read.effects
        assert isinstance(effect, PlantedEffect)
        assert effect.kind == "return_autocorrelation"
        assert read.content_hash() == GOLDEN_AT_2_3_0[-1][2]


def test_2_3_0_objects_can_sit_inside_current_2_5_0_objects() -> None:
    with contract_schema_version_scope("2.3.0"):
        old_ref = Ref(kind=Kind.STRATEGY, name="momentum", version="1.0.0")
        old_effect = PlantedEffect(lag_minutes=1, strength=Decimal("0.2"))
    spec = strategy(composition=NegatedStrategy(base=old_ref))
    assert isinstance(spec.composition, NegatedStrategy)
    assert spec.composition.base.schema_version == "2.3.0"
    built = market(effects=(old_effect, garch()))
    assert built.effects[0].schema_version == "2.3.0" and built.schema_version == "2.5.0"
    round_trips(spec)
    round_trips(built)


# ======================================================================================
# 决策 1：EventSpec.bar_spec
# ======================================================================================


def test_bar_spec_must_be_a_representation_and_round_trips() -> None:
    spec = event(bar_spec=BARS)
    assert spec.bar_spec == BARS
    assert wire(spec)["bar_spec"] == wire(BARS)
    round_trips(spec)
    assert spec.content_hash() != event().content_hash()  # declared bars are semantic
    for wrong in (FEATURE, REGIME, MOMENTUM):
        with pytest.raises(ValidationError, match="representation"):
            event(bar_spec=wrong)


# ======================================================================================
# 决策 2：StrategySpec.composition
# ======================================================================================


def test_each_composition_is_parsed_by_its_type() -> None:
    gated = {
        "type": "conditioned",
        "base": wire(MOMENTUM),
        "state": wire(REGIME),
        "state_value": "up",
    }
    averaged = {"type": "ensemble", "members": [wire(MOMENTUM), wire(REVERSAL)]}
    negated = {"type": "negated", "base": wire(MOMENTUM)}
    for composition, expected in (
        (gated, ConditionedStrategy),
        (averaged, EnsembleStrategy),
        (negated, NegatedStrategy),
    ):
        payload = {**wire(strategy(signals=(FEATURE, REGIME))), "composition": composition}
        for spec in (
            StrategySpec.model_validate(payload),
            StrategySpec.model_validate_json(json.dumps(payload)),
        ):
            assert isinstance(spec.composition, expected)
            round_trips(spec)


def test_an_unknown_or_missing_composition_type_is_refused() -> None:
    base = wire(strategy())
    for composition in (
        {"type": "stacked", "base": wire(MOMENTUM)},
        {"base": wire(MOMENTUM)},
        {"type": "negated", "base": wire(MOMENTUM), "weight": "1"},
    ):
        with pytest.raises(ValidationError):
            StrategySpec.model_validate({**base, "composition": composition})


def test_composition_references_must_have_the_right_kind() -> None:
    with pytest.raises(ValidationError, match="strategy"):
        conditioned(base=FEATURE)
    with pytest.raises(ValidationError, match="state"):
        conditioned(state=FEATURE)
    with pytest.raises(ValidationError, match="strategy"):
        ensemble(MOMENTUM, FEATURE)
    with pytest.raises(ValidationError, match="strategy"):
        NegatedStrategy(base=REGIME)
    with pytest.raises(ValidationError):
        conditioned(state_value="")


def test_an_ensemble_has_at_least_two_distinct_members() -> None:
    with pytest.raises(ValidationError):
        EnsembleStrategy(members=(MOMENTUM,))
    with pytest.raises(ValidationError):
        EnsembleStrategy(members=())
    with pytest.raises(ValidationError, match="重复"):
        ensemble(MOMENTUM, REVERSAL, MOMENTUM)
    with contract_schema_version_scope("2.3.0"):
        old_twin = Ref(kind=Kind.STRATEGY, name="momentum", version="1.0.0")
    with pytest.raises(ValidationError, match="重复"):  # same target, different envelope
        ensemble(MOMENTUM, old_twin)
    assert ensemble().rule == "equal_weight_mean"
    with pytest.raises(ValidationError):
        EnsembleStrategy(members=(MOMENTUM, REVERSAL), rule="median")


def test_a_composition_cannot_reference_its_own_strategy() -> None:
    own = Ref(kind=Kind.STRATEGY, name="combo", version="1.0.0")
    with pytest.raises(ValidationError, match="自身"):
        strategy(composition=NegatedStrategy(base=own))
    with pytest.raises(ValidationError, match="自身"):
        strategy(composition=ensemble(MOMENTUM, own))
    with pytest.raises(ValidationError, match="自身"):
        strategy(signals=(FEATURE, REGIME), composition=conditioned(base=own))
    other_version = Ref(kind=Kind.STRATEGY, name="combo", version="0.9.0")
    assert strategy(composition=NegatedStrategy(base=other_version)).composition is not None


def test_a_conditioned_strategy_declares_its_gating_state_as_a_signal() -> None:
    with pytest.raises(ValidationError, match="signals"):
        strategy(signals=(FEATURE,), composition=conditioned())
    spec = strategy(signals=(FEATURE, REGIME), composition=conditioned())
    assert isinstance(spec.composition, ConditionedStrategy)
    assert spec.composition.references() == (MOMENTUM,)
    assert ensemble().references() == (MOMENTUM, REVERSAL)
    assert NegatedStrategy(base=MOMENTUM).references() == (MOMENTUM,)


def test_the_composition_is_part_of_the_strategy_identity() -> None:
    plain = strategy()
    negated = strategy(composition=NegatedStrategy(base=MOMENTUM))
    averaged = strategy(composition=ensemble())
    assert len({plain.content_hash(), negated.content_hash(), averaged.content_hash()}) == 3


# ======================================================================================
# 决策 3：PortfolioState.peak_equity
# ======================================================================================


def test_peak_equity_is_never_below_equity() -> None:
    assert portfolio(peak_equity=Decimal("1000")).peak_equity == Decimal("1000")
    assert portfolio(peak_equity=Decimal("1500")).peak_equity == Decimal("1500")
    round_trips(portfolio(peak_equity=Decimal("1500")))
    with pytest.raises(ValidationError, match="peak_equity"):
        portfolio(peak_equity=Decimal("999.99"))
    with pytest.raises(ValidationError, match="equity"):
        portfolio(equity=None, peak_equity=Decimal("1000"))
    for wrong in (Decimal(0), Decimal(-1), 1000.0, Decimal("NaN")):
        with pytest.raises(ValidationError):
            portfolio(peak_equity=wrong)


# ======================================================================================
# 决策 4：合成效应
# ======================================================================================


def test_garch_parameters_are_constrained() -> None:
    assert garch().kind == "volatility_clustering"
    assert garch(alpha=Decimal(0), beta=Decimal(0)).alpha == Decimal(0)
    for bad in (
        {"omega": Decimal(0)},
        {"omega": Decimal("-0.1")},
        {"alpha": Decimal("-0.01")},
        {"beta": Decimal("-0.01")},
        {"alpha": Decimal("0.2"), "beta": Decimal("0.8")},  # alpha + beta == 1
        {"alpha": Decimal("0.5"), "beta": Decimal("0.6")},
    ):
        with pytest.raises(ValidationError):
            garch(**bad)


def test_jump_parameters_are_constrained() -> None:
    assert jump().kind == "jump"
    for bad in (
        {"intensity_per_minute": Decimal(0)},
        {"intensity_per_minute": Decimal(1)},
        {"intensity_per_minute": Decimal("-0.1")},
        {"jump_scale": Decimal(0)},
        {"jump_scale": Decimal("-0.01")},
    ):
        with pytest.raises(ValidationError):
            jump(**bad)


def test_effects_are_a_union_discriminated_by_kind() -> None:
    planted = PlantedEffect(lag_minutes=5, strength=Decimal("0.1"))
    spec = market(effects=(planted, garch(), jump()))
    assert [type(effect) for effect in spec.effects] == [
        PlantedEffect,
        VolatilityClusteringEffect,
        JumpEffect,
    ]
    round_trips(spec)
    payload = wire(spec)
    payload["effects"][1]["kind"] = "regime_switch"
    with pytest.raises(ValidationError):
        SyntheticMarketSpec.model_validate(payload)
    payload = wire(spec)
    payload["effects"][2]["lag_minutes"] = 1  # a field of another effect
    with pytest.raises(ValidationError):
        SyntheticMarketSpec.model_validate(payload)


# ======================================================================================
# 决策 5：OutcomeMethod.VOL_SCALED_TRIPLE_BARRIER
# ======================================================================================


def test_the_vol_scaled_triple_barrier_requires_its_scaling_fields() -> None:
    spec = label_spec(OutcomeMethod.VOL_SCALED_TRIPLE_BARRIER)
    assert spec.volatility_feature == VOL and spec.barrier_multiplier == Decimal("2")
    assert spec.upper_barrier is None and spec.lower_barrier is None
    round_trips(spec)
    method = OutcomeMethod.VOL_SCALED_TRIPLE_BARRIER
    with pytest.raises(ValidationError, match="volatility_feature"):
        label_spec(method, volatility_feature=None)
    with pytest.raises(ValidationError, match="barrier_multiplier"):
        label_spec(method, barrier_multiplier=None)
    with pytest.raises(ValidationError, match="feature"):
        label_spec(method, volatility_feature=REGIME)
    for wrong in (Decimal(0), Decimal("-1"), 2.0):
        with pytest.raises(ValidationError):
            label_spec(method, barrier_multiplier=wrong)
    with pytest.raises(ValidationError, match="固定屏障"):
        label_spec(method, upper_barrier=Decimal("0.02"))
    with pytest.raises(ValidationError, match="固定屏障"):
        label_spec(method, lower_barrier=Decimal("0.01"))


@pytest.mark.parametrize(
    "method", [OutcomeMethod.FORWARD_RETURN, OutcomeMethod.TRIPLE_BARRIER], ids=str
)
def test_other_methods_refuse_the_scaling_fields(method: OutcomeMethod) -> None:
    with pytest.raises(ValidationError, match="volatility_feature"):
        label_spec(method, volatility_feature=VOL)
    with pytest.raises(ValidationError, match="barrier_multiplier"):
        label_spec(method, barrier_multiplier=Decimal("2"))
    round_trips(label_spec(method))


def test_bind_carries_the_scaling_fields() -> None:
    spec = OutcomeLabelSpec.bind(
        outcome_spec(),
        OutcomeMethod.VOL_SCALED_TRIPLE_BARRIER,
        volatility_feature=VOL,
        barrier_multiplier=Decimal("1.5"),
    )
    assert spec.matches(outcome_spec())
    assert (spec.volatility_feature, spec.barrier_multiplier) == (VOL, Decimal("1.5"))
    old = OutcomeLabelSpec.bind(
        outcome_spec(),
        OutcomeMethod.TRIPLE_BARRIER,
        upper_barrier=Decimal("0.02"),
        lower_barrier=Decimal("0.01"),
    )
    assert "volatility_feature" not in wire(old)


# ======================================================================================
# 决策 6：UniverseMember.assumption（ADR-0051 §3）
# ======================================================================================


def test_an_assumed_member_binds_the_adr_0051_availability_policy() -> None:
    assumed = member(assumption=binding())
    assert assumed.assumption == binding()
    assert wire(assumed)["assumption"] == wire(binding())
    round_trips(assumed)
    assert assumed.content_hash() != member().content_hash()  # the assumption is semantic
    with pytest.raises(ValidationError, match=LISTING_BACKFILL_ASSUMPTION_ID):
        member(assumption=binding(policy_id="binance.spot.publication"))
    for role in (PolicyRole.PRECEDENCE, PolicyRole.POINT_IN_TIME, PolicyRole.PARSER):
        with pytest.raises(ValidationError, match="role"):
            member(assumption=binding(role=role))


def test_a_2_0_0_policy_binding_can_sit_inside_a_2_4_0_member() -> None:
    # the ADR-0051 binding constant is a Phase 1 registered identity pinned at 2.0.0 (V3)
    pinned = binding(schema_version="2.0.0")
    assumed = member(assumption=pinned)
    assert assumed.schema_version == "2.5.0"
    assert assumed.assumption is not None and assumed.assumption.schema_version == "2.0.0"
    round_trips(assumed)


# ======================================================================================
# Schema：新模型与导出一致；已发布 Schema 只有 ADR-0088 的新增内容与信封默认值变化
# ======================================================================================


def test_the_committed_schemas_of_the_new_models_match_the_contracts(tmp_path: Path) -> None:
    written = export_json_schemas(tmp_path)
    for model in NEW_MODELS:
        committed = json.loads(
            (CURRENT_SCHEMA_DIR / f"{model.__name__}.schema.json").read_text(encoding="utf-8")
        )
        exported = json.loads(written[model.__name__].read_text(encoding="utf-8"))
        assert committed == exported, model.__name__
        assert committed["properties"]["schema_version"]["default"] == "2.5.0"
        assert committed["additionalProperties"] is False


#: SHA-256 of the committed Schema bytes at contract 2.3.0 (before ADR-0088).
SCHEMA_SHA256_AT_2_3_0 = {
    "EventSpec": "ace1b7d5dce07a4f44f706172b2ce14fe449e061c6d315a9f877b4c00e9dfdd1",
    "StrategySpec": "8ab64ad2fd51ee04526767794112d4af98d96d180d33fcbff6ef245f4117c08f",
    "PortfolioState": "c8041c7f849947b486ad3557f09ff0da19b2a1cf6e854262d3da04f71324a5e4",
    "RiskRequest": "7d617c53fee53351184cd02840168a13509a0abab97ec1d879d5b3cb32631ec9",
    "OutcomeLabelSpec": "77d024693f4994c6f2b2fd8de39fc5ee02b29600249d68e2e0135ae42061b6b1",
    "OutcomeRequest": "2b9651364ef0b8b3f22621f7f496a3a496eeffeeec7f1e419477cc6687ac84bd",
    "SyntheticMarketSpec": "50f7aca5c57afed7e92d11d3a92b66ea9621549599dc0b5f8e9be24a608290d4",
    "PlantedEffect": "d383f1e66763f649da80d4cea94902a9c0d3b990988beaa1d469daee1da2d141",
    "SyntheticMarket": "7843ffd94909e179cdd3e29ff6376b5e3919861e527ae76928b7b2bd0c93582f",
    "UniverseMember": "8cb04aef577eb7f77aecfaa5663a0cd3cb4b3283e873a9d151f0eeb5bbbbdfdd",
    "ResearchDatasetManifest": "6fbfaa16d5ac50c7605e55a5e802fa81fcb5550a6898ee09381e85079b79f331",
}

VST = "vol_scaled_triple_barrier"


def _without_adr_0088(name: str, schema: dict[str, Any]) -> dict[str, Any]:
    """``schema`` with exactly the ADR-0088 additions removed (key order otherwise kept)."""
    defs = schema.get("$defs", {})
    if name == "EventSpec":
        schema["properties"].pop("bar_spec")
    elif name == "StrategySpec":
        schema["properties"].pop("composition")
        for model in ("ConditionedStrategy", "EnsembleStrategy", "NegatedStrategy"):
            defs.pop(model)
    elif name == "PortfolioState":
        schema["properties"].pop("peak_equity")
    elif name == "RiskRequest":
        defs["PortfolioState"]["properties"].pop("peak_equity")
    elif name in ("OutcomeLabelSpec", "OutcomeRequest"):
        label = schema if name == "OutcomeLabelSpec" else defs["OutcomeLabelSpec"]
        label["properties"].pop("volatility_feature")
        label["properties"].pop("barrier_multiplier")
        defs["OutcomeMethod"]["enum"].remove(VST)
    elif name == "SyntheticMarketSpec":
        defs.pop("JumpEffect")
        defs.pop("VolatilityClusteringEffect")
        schema["properties"]["effects"]["items"] = {"$ref": "#/$defs/PlantedEffect"}
    elif name == "SyntheticMarket":
        defs.pop("JumpEffect")
        defs.pop("VolatilityClusteringEffect")
        schema["properties"]["truth"]["items"] = {"$ref": "#/$defs/PlantedEffect"}
    elif name == "UniverseMember":
        schema["properties"].pop("assumption")
        defs.pop("PolicyBinding")  # only the new field needs these here
        defs.pop("PolicyRole")
    elif name == "ResearchDatasetManifest":
        defs["UniverseMember"]["properties"].pop("assumption")  # PolicyBinding was already here
    return schema


def _schema_bytes(schema: dict[str, Any]) -> bytes:
    # exactly how `export_json_schemas` writes a Schema
    return (json.dumps(schema, indent=2, ensure_ascii=False) + "\n").encode("utf-8")


@pytest.mark.parametrize("name", sorted(SCHEMA_SHA256_AT_2_3_0))
def test_a_published_schema_changes_only_by_the_adr_0088_additions(
    name: str, tmp_path: Path
) -> None:
    committed = (CURRENT_SCHEMA_DIR / f"{name}.schema.json").read_bytes()
    regenerated = export_json_schemas(tmp_path)[name].read_bytes()
    for schema in (committed, regenerated):
        stripped = _schema_bytes(_without_adr_0088(name, json.loads(schema)))
        digest = hashlib.sha256(as_published_at(stripped, "2.3.0")).hexdigest()
        assert digest == SCHEMA_SHA256_AT_2_3_0[name]


def test_the_new_schema_content_is_what_adr_0088_declares() -> None:
    def load(name: str) -> dict[str, Any]:
        text = (CURRENT_SCHEMA_DIR / f"{name}.schema.json").read_text(encoding="utf-8")
        loaded: dict[str, Any] = json.loads(text)
        return loaded

    composition = load("StrategySpec")["properties"]["composition"]
    union = composition["anyOf"][0]
    assert union["discriminator"]["propertyName"] == "type"
    assert set(union["discriminator"]["mapping"]) == {"conditioned", "ensemble", "negated"}
    assert composition["default"] is None
    effects = load("SyntheticMarketSpec")["properties"]["effects"]["items"]
    assert effects["discriminator"]["propertyName"] == "kind"
    assert set(effects["discriminator"]["mapping"]) == {
        "return_autocorrelation",
        "volatility_clustering",
        "jump",
    }
    assert VST in load("OutcomeLabelSpec")["$defs"]["OutcomeMethod"]["enum"]
    for name, field in (
        ("EventSpec", "bar_spec"),
        ("UniverseMember", "assumption"),
        ("PortfolioState", "peak_equity"),
        ("OutcomeLabelSpec", "volatility_feature"),
        ("OutcomeLabelSpec", "barrier_multiplier"),
    ):
        schema = load(name)
        assert schema["properties"][field]["default"] is None
        assert field not in schema["required"]


# ======================================================================================
# PM 决定（契约层遗留项，2026-09-28）：SyntheticMarket.truth 放宽为 SyntheticEffect 联合
# ======================================================================================


def test_a_2_3_0_synthetic_market_truth_payload_keeps_its_hash_after_the_truth_widening() -> None:
    """``SyntheticMarket.truth`` widened from ``tuple[PlantedEffect, ...]`` to
    ``tuple[SyntheticEffect, ...]`` (additive, PM decision on this ADR's open item 1, done by the
    synthetic-market generator batch). A ``truth`` payload recorded before the widening (only
    ``PlantedEffect``, 2.3.0 envelope) must still read back with its exact pre-widening content
    hash — the pin below is the payload's own canonical-JSON SHA-256, taken independently of any
    model code (``core.domain.base.content_hash`` of the literal dict)."""
    from core.contracts.synthetic import SyntheticMarket  # local: this file only appends

    provider = "hlens_synthetic_random_walk@1.0.0"
    pinned_market_hash = "d9476725b4cd0ad5d959446e57689683d6dc0524a4235a75c8171b382bee2769"
    payload = (
        '{"bars":[],"market_hash":"' + pinned_market_hash + '","provider":"' + provider + '",'
        '"schema_version":"2.3.0","spec_hash":"' + HASH + '","truth":[{"kind":'
        '"return_autocorrelation","lag_minutes":1,"schema_version":"2.3.0","strength":"0.2"}]}'
    )
    pinned = "7fa86c197e226ec1e2ddb0518e69df8de03cbe5c6604b3284d5d01ddccb97d94"
    assert content_hash(json.loads(payload)) == pinned  # the pin is the payload's own hash
    for read in (
        SyntheticMarket.model_validate_json(payload),
        SyntheticMarket.model_validate(json.loads(payload)),
    ):
        assert read.schema_version == "2.3.0"  # the recorded envelope is never rewritten
        assert isinstance(read.truth[0], PlantedEffect)
        semantic = read.model_dump(mode="json", exclude=read._non_semantic_fields())
        assert canonical_json(semantic) == payload  # no ADR-0088 key appears
        assert read.content_hash() == pinned
    with contract_schema_version_scope("2.3.0"):  # a replay of the recorded object
        without_envelope = {k: v for k, v in json.loads(payload).items() if k != "schema_version"}
        assert SyntheticMarket.model_validate(without_envelope).content_hash() == pinned

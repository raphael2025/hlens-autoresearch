"""ADR-0012（D-23）：信息流白名单与 kind 判别字段。

覆盖 ADR-0012「验收测试矩阵」1 ~ 17。三条主线：

1. 13 个具体规格的 `kind` 是**不可覆盖的字面量**，因此所有按 `kind` 做的既有校验不能被绕过；
2. Feature / State / Event / Strategy 的**直接输入**按白名单收紧，Outcome 引用与
   `zone = outcome` 的数据集不得进入；
3. `observable_lag` / `training_window` / `state_space` 的局部不变量。

**边界**：这里校验的只是**声明层面的直接引用**。传递依赖闭包、引用与实例一致、
物化数据的泄漏检测仍是未实现的 Registry / Runner 义务（ADR-0012「运行时延期义务」）。
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from core.contracts.profile_selection import ProfileSelectionRule
from core.contracts.validation_profile import ValidationProfile
from core.domain.artifact import StrategyArtifact
from core.domain.base import Contract, Kind, Ref, VersionedSpec
from core.domain.research import (
    EvidenceLevel,
    ExperimentSpec,
    Hypothesis,
    HypothesisOrigin,
    KnowledgeItem,
)
from core.domain.specs import (
    FEATURE_INPUT_KINDS,
    FEATURE_INPUT_ZONES,
    STRATEGY_SIGNAL_KINDS,
    DatasetRef,
    EventSpec,
    FeatureSpec,
    OutcomeSpec,
    RepresentationSpec,
    RiskPolicy,
    StateSpec,
    StrategySpec,
    Zone,
)
from tests import factories

REPO = Path(__file__).resolve().parents[1]
CURRENT_SCHEMA_DIR = REPO / "schemas"
LEGACY_SCHEMA_DIR = CURRENT_SCHEMA_DIR / "v1"
T0 = datetime(2024, 1, 1, tzinfo=UTC)


# ======================================================================================
# 最小合法构造器：每个具体规格一个，全部**不传** kind
# ======================================================================================


def _ref(kind: Kind, name: str = "x_example", version: str = "1.0.0") -> Ref:
    return Ref(kind=kind, name=name, version=version)


def _dataset(zone: Zone) -> DatasetRef:
    return DatasetRef(
        zone=zone,
        table="t",
        snapshot_id="snap-1",
        time_range_start=T0,
        time_range_end=T0 + timedelta(days=1),
    )


def representation(**kw: Any) -> RepresentationSpec:
    payload: dict[str, Any] = {
        "name": "rep_example",
        "version": "1.0.0",
        "method": "ohlcv_1h",
        "inputs": (_dataset(Zone.CANONICAL),),
        "event_time_semantics": "bar_close",
    }
    payload.update(kw)
    return RepresentationSpec(**payload)


def feature(**kw: Any) -> FeatureSpec:
    payload: dict[str, Any] = {
        "name": "f_example",
        "version": "1.0.0",
        "definition": "realized volatility",
        "inputs": (_ref(Kind.REPRESENTATION),),
        "available_lag": timedelta(0),
    }
    payload.update(kw)
    return FeatureSpec(**payload)


def state(**kw: Any) -> StateSpec:
    payload: dict[str, Any] = {
        "name": "st_example",
        "version": "1.0.0",
        "features": (_ref(Kind.FEATURE),),
        "state_space": ("calm", "stressed"),
        "method": "hmm",
    }
    payload.update(kw)
    return StateSpec(**payload)


def event(**kw: Any) -> EventSpec:
    payload: dict[str, Any] = {
        "name": "ev_example",
        "version": "1.0.0",
        "trigger": "breakout",
        "features": (_ref(Kind.FEATURE),),
    }
    payload.update(kw)
    return EventSpec(**payload)


def outcome(**kw: Any) -> OutcomeSpec:
    payload: dict[str, Any] = {
        "name": "o_example",
        "version": "1.0.0",
        "horizon": timedelta(hours=4),
        "label_definition": "forward return",
    }
    payload.update(kw)
    return OutcomeSpec(**payload)


def strategy(**kw: Any) -> StrategySpec:
    payload: dict[str, Any] = {
        "name": "s_example",
        "version": "1.0.0",
        "signals": (_ref(Kind.FEATURE),),
    }
    payload.update(kw)
    return StrategySpec(**payload)


def risk_policy(**kw: Any) -> RiskPolicy:
    payload: dict[str, Any] = {
        "name": "r_example",
        "version": "1.0.0",
        "rules": ("max_leverage",),
    }
    payload.update(kw)
    return RiskPolicy(**payload)


def knowledge_item(**kw: Any) -> KnowledgeItem:
    payload: dict[str, Any] = {
        "name": "k_example",
        "version": "1.0.0",
        "source": "https://example.invalid/paper",
        "license": "CC-BY-4.0",
        "claim": "momentum persists",
        "evidence_level": EvidenceLevel.E1_EXAMPLE,
    }
    payload.update(kw)
    return KnowledgeItem(**payload)


def hypothesis(**kw: Any) -> Hypothesis:
    payload: dict[str, Any] = {
        "name": "h_example",
        "version": "1.0.0",
        "family_id": "fam-1",
        "statement": "X 在条件 C 下导致 Y",
        "expected_direction": "positive",
        "minimum_meaningful_effect": "0.1 sharpe",
        "origin": HypothesisOrigin.HUMAN,
    }
    payload.update(kw)
    return Hypothesis(**payload)


def experiment_spec(**kw: Any) -> ExperimentSpec:
    return factories.experiment_spec(**kw)


def strategy_artifact(**kw: Any) -> StrategyArtifact:
    return factories.strategy_artifact(**kw)


def validation_profile(**kw: Any) -> ValidationProfile:
    return factories.validation_profile(**kw)


def selection_rule(**kw: Any) -> ProfileSelectionRule:
    payload: dict[str, Any] = {
        "name": "test_rule",
        "version": "1.0.0",
        "entries": factories.selection_rule().entries,
    }
    payload.update(kw)
    return ProfileSelectionRule(**payload)


#: ADR-0012 §D-23.1 列出的 13 个具体规格：构造器 + 它唯一允许的 kind。
CONCRETE_SPECS: tuple[tuple[str, Any, Kind], ...] = (
    ("RepresentationSpec", representation, Kind.REPRESENTATION),
    ("FeatureSpec", feature, Kind.FEATURE),
    ("StateSpec", state, Kind.STATE),
    ("EventSpec", event, Kind.EVENT),
    ("OutcomeSpec", outcome, Kind.OUTCOME),
    ("StrategySpec", strategy, Kind.STRATEGY),
    ("RiskPolicy", risk_policy, Kind.RISK),
    ("KnowledgeItem", knowledge_item, Kind.KNOWLEDGE),
    ("Hypothesis", hypothesis, Kind.HYPOTHESIS),
    ("ExperimentSpec", experiment_spec, Kind.EXPERIMENT),
    ("StrategyArtifact", strategy_artifact, Kind.ARTIFACT),
    ("ValidationProfile", validation_profile, Kind.PROFILE),
    ("ProfileSelectionRule", selection_rule, Kind.PROFILE_SELECTION_RULE),
)

_SPEC_IDS = [name for name, _, _ in CONCRETE_SPECS]


# ======================================================================================
# 矩阵 1 / 2：kind 是不可覆盖的字面量
# ======================================================================================


def test_all_thirteen_concrete_specs_are_covered() -> None:
    """ADR-0012 §D-23.1 点名 13 个模型；漏掉任何一个都应当让本测试失败。"""
    assert len(CONCRETE_SPECS) == 13
    assert len({kind for _, _, kind in CONCRETE_SPECS}) == 13


@pytest.mark.parametrize(("name", "build", "own_kind"), CONCRETE_SPECS, ids=_SPEC_IDS)
def test_default_kind_is_the_models_own_literal(name: str, build: Any, own_kind: Kind) -> None:
    """矩阵 2：不传 kind 时取自身字面量，且是真正的 `Kind` 枚举成员。"""
    spec = build()
    assert spec.kind is own_kind
    assert spec.ref.kind is own_kind
    assert spec.model_dump(mode="json")["kind"] == own_kind.value


@pytest.mark.parametrize(("name", "build", "own_kind"), CONCRETE_SPECS, ids=_SPEC_IDS)
def test_explicit_own_kind_is_still_accepted(name: str, build: Any, own_kind: Kind) -> None:
    """显式传自身取值不是覆盖，仍然接受（枚举成员与 wire 字符串两种写法）。"""
    assert build(kind=own_kind).kind is own_kind
    assert build(kind=own_kind.value).kind is own_kind


@pytest.mark.parametrize(("name", "build", "own_kind"), CONCRETE_SPECS, ids=_SPEC_IDS)
def test_every_other_kind_is_rejected(name: str, build: Any, own_kind: Kind) -> None:
    """矩阵 1：判别字段不能被伪造——**每一个**其它 Kind 都被拒绝。

    尤其是 `Kind.OUTCOME`：判别字段一旦可覆盖，按 kind 做的信息流校验全部可绕过。
    """
    for other in Kind:
        if other is own_kind:
            continue
        with pytest.raises(ValidationError):
            build(kind=other)
        with pytest.raises(ValidationError):
            build(kind=other.value)


@pytest.mark.parametrize(("name", "build", "own_kind"), CONCRETE_SPECS, ids=_SPEC_IDS)
def test_kind_cannot_be_smuggled_in_by_model_validate(
    name: str, build: Any, own_kind: Kind
) -> None:
    """JSON 载荷入口与构造函数同强度。"""
    model = type(build())
    payload = build().model_dump(mode="json")
    wrong = Kind.OUTCOME if own_kind is not Kind.OUTCOME else Kind.FEATURE
    with pytest.raises(ValidationError):
        model.model_validate({**payload, "kind": wrong.value})
    assert model.model_validate(payload).kind is own_kind


@pytest.mark.parametrize(("name", "build", "own_kind"), CONCRETE_SPECS, ids=_SPEC_IDS)
def test_model_copy_update_cannot_change_kind(name: str, build: Any, own_kind: Kind) -> None:
    """ADR-0010 §D-13 的重校验必须把错 kind 一并拦下（13 个模型逐个覆盖）。"""
    spec = build()
    wrong = Kind.OUTCOME if own_kind is not Kind.OUTCOME else Kind.FEATURE
    with pytest.raises(ValidationError):
        spec.model_copy(update={"kind": wrong})
    assert spec.model_copy(update={"kind": own_kind}).kind is own_kind


def test_shared_and_reference_kinds_stay_full_enums() -> None:
    """`Ref.kind` 与抽象基类的 `kind` 不收窄——引用必须能指向各种类型。"""
    for kind in Kind:
        assert Ref(kind=kind, name="x", version="1.0.0").kind is kind
    assert VersionedSpec.model_fields["kind"].annotation is Kind


# ======================================================================================
# 矩阵 3 ~ 7：FeatureSpec.inputs 的完整白名单矩阵
# ======================================================================================

_ALLOWED_INPUT_KINDS = (Kind.REPRESENTATION, Kind.FEATURE)
_REJECTED_INPUT_KINDS = tuple(k for k in Kind if k not in _ALLOWED_INPUT_KINDS)
_ALLOWED_INPUT_ZONES = (Zone.CANONICAL, Zone.FEATURE, Zone.RESEARCH_DATASET)
_REJECTED_INPUT_ZONES = tuple(z for z in Zone if z not in _ALLOWED_INPUT_ZONES)


def test_feature_input_whitelist_constants_match_the_adr() -> None:
    assert FEATURE_INPUT_KINDS == frozenset(_ALLOWED_INPUT_KINDS)
    assert FEATURE_INPUT_ZONES == frozenset(_ALLOWED_INPUT_ZONES)
    assert STRATEGY_SIGNAL_KINDS == frozenset({Kind.FEATURE, Kind.STATE, Kind.EVENT})


@pytest.mark.parametrize("kind", _ALLOWED_INPUT_KINDS)
def test_feature_accepts_whitelisted_ref_kinds(kind: Kind) -> None:
    """矩阵 6（引用侧）。"""
    assert feature(inputs=(_ref(kind),)).inputs[0].kind is kind  # type: ignore[union-attr]


@pytest.mark.parametrize("kind", _REJECTED_INPUT_KINDS, ids=lambda k: k.value)
def test_feature_rejects_non_whitelisted_ref_kinds(kind: Kind) -> None:
    """矩阵 3 的完整形式：`outcome` 只是被拒绝的 13 种之一。"""
    with pytest.raises(ValidationError):
        feature(inputs=(_ref(kind),))


@pytest.mark.parametrize("zone", _ALLOWED_INPUT_ZONES, ids=lambda z: z.value)
def test_feature_accepts_whitelisted_dataset_zones(zone: Zone) -> None:
    """矩阵 6（数据集侧）。"""
    assert feature(inputs=(_dataset(zone),)).inputs[0].zone is zone  # type: ignore[union-attr]


@pytest.mark.parametrize("zone", _REJECTED_INPUT_ZONES, ids=lambda z: z.value)
def test_feature_rejects_non_whitelisted_dataset_zones(zone: Zone) -> None:
    """矩阵 4（`outcome`）与矩阵 5（`raw` / `state` / `event`）。"""
    with pytest.raises(ValidationError):
        feature(inputs=(_dataset(zone),))


def test_feature_rejects_a_bad_input_hidden_among_good_ones() -> None:
    """白名单逐项检查，不是"只看第一个"。"""
    with pytest.raises(ValidationError):
        feature(inputs=(_ref(Kind.FEATURE), _dataset(Zone.CANONICAL), _ref(Kind.OUTCOME)))
    with pytest.raises(ValidationError):
        feature(inputs=(_ref(Kind.FEATURE), _dataset(Zone.OUTCOME)))


def test_feature_accepts_a_mixed_but_whitelisted_input_tuple() -> None:
    spec = feature(
        inputs=(
            _ref(Kind.REPRESENTATION),
            _ref(Kind.FEATURE),
            _dataset(Zone.CANONICAL),
            _dataset(Zone.RESEARCH_DATASET),
        )
    )
    assert len(spec.inputs) == 4


def test_feature_rejects_empty_inputs() -> None:
    """矩阵 7。"""
    with pytest.raises(ValidationError):
        feature(inputs=())


def test_representation_inputs_are_unchanged_this_round() -> None:
    """ADR-0012「明确不做」：本轮不收紧 Representation 的 zone，但仍只接受 DatasetRef 且非空。"""
    for zone in Zone:
        assert representation(inputs=(_dataset(zone),)).inputs[0].zone is zone
    with pytest.raises(ValidationError):
        representation(inputs=())
    with pytest.raises(ValidationError):
        representation(inputs=(_ref(Kind.FEATURE),))


# ======================================================================================
# 矩阵 8 ~ 11：State / Event / Strategy 白名单
# ======================================================================================


@pytest.mark.parametrize(
    "kind", tuple(k for k in Kind if k is not Kind.FEATURE), ids=lambda k: k.value
)
def test_state_rejects_non_feature_inputs(kind: Kind) -> None:
    """矩阵 8。"""
    with pytest.raises(ValidationError):
        state(features=(_ref(kind),))


def test_state_accepts_feature_inputs_and_rejects_empty() -> None:
    assert state(features=(_ref(Kind.FEATURE, "f_a"), _ref(Kind.FEATURE, "f_b"))).features[1].name
    with pytest.raises(ValidationError):
        state(features=())


@pytest.mark.parametrize(
    "kind", tuple(k for k in Kind if k is not Kind.FEATURE), ids=lambda k: k.value
)
def test_event_rejects_non_feature_in_features(kind: Kind) -> None:
    """矩阵 9（features 侧）。"""
    with pytest.raises(ValidationError):
        event(features=(_ref(kind),))


@pytest.mark.parametrize(
    "kind", tuple(k for k in Kind if k is not Kind.STATE), ids=lambda k: k.value
)
def test_event_rejects_non_state_in_states(kind: Kind) -> None:
    """矩阵 9（states 侧）：包括把 Feature 放进 states 这种错位。"""
    with pytest.raises(ValidationError):
        event(features=(), states=(_ref(kind),))


def test_event_still_requires_at_least_one_input() -> None:
    """ADR-0012 保持现状：features / states 至少其一非空。"""
    with pytest.raises(ValidationError):
        event(features=(), states=())
    assert event(features=(), states=(_ref(Kind.STATE),)).states
    assert event(features=(_ref(Kind.FEATURE),), states=(_ref(Kind.STATE),)).features


@pytest.mark.parametrize("kind", tuple(STRATEGY_SIGNAL_KINDS), ids=lambda k: k.value)
def test_strategy_accepts_whitelisted_signals(kind: Kind) -> None:
    assert strategy(signals=(_ref(kind),)).signals[0].kind is kind


@pytest.mark.parametrize(
    "kind", tuple(k for k in Kind if k not in STRATEGY_SIGNAL_KINDS), ids=lambda k: k.value
)
def test_strategy_rejects_non_whitelisted_signals(kind: Kind) -> None:
    """矩阵 10：`outcome` / `risk` / `dataset` 都在其中。"""
    with pytest.raises(ValidationError):
        strategy(signals=(_ref(kind),))


def test_strategy_rejects_empty_signals() -> None:
    with pytest.raises(ValidationError):
        strategy(signals=())


@pytest.mark.parametrize(
    "kind", tuple(k for k in Kind if k is not Kind.RISK), ids=lambda k: k.value
)
def test_strategy_rejects_non_risk_risk_policy(kind: Kind) -> None:
    """矩阵 11。"""
    with pytest.raises(ValidationError):
        strategy(risk_policy=_ref(kind))


def test_strategy_risk_policy_is_optional_and_accepts_risk() -> None:
    assert strategy().risk_policy is None
    assert strategy(risk_policy=_ref(Kind.RISK)).risk_policy is not None


def test_outcome_is_excluded_from_every_downstream_input() -> None:
    """ADR-0012 §D-23.2 的直接推论（Constitution C-L2、03-data.md §2）。

    Outcome 的 `Ref` 与 `zone = outcome` 的数据集都不得成为 Feature / State / Event /
    Strategy 的输入。这是**声明层面**的必要条件，不等于泄漏已被防住。
    """
    outcome_ref = _ref(Kind.OUTCOME)
    for build, field in (
        (feature, "inputs"),
        (state, "features"),
        (event, "features"),
        (event, "states"),
        (strategy, "signals"),
    ):
        with pytest.raises(ValidationError):
            build(**{field: (outcome_ref,)})
    with pytest.raises(ValidationError):
        feature(inputs=(_dataset(Zone.OUTCOME),))


# ======================================================================================
# 矩阵 12 ~ 14：局部不变量
# ======================================================================================


def test_event_observable_lag_rejects_negative_and_accepts_zero() -> None:
    """矩阵 12：负的可观测延迟即未来函数。"""
    with pytest.raises(ValidationError):
        event(observable_lag=timedelta(seconds=-1))
    with pytest.raises(ValidationError):
        event(observable_lag=timedelta(days=-1))
    assert event(observable_lag=timedelta(0)).observable_lag == timedelta(0)
    assert event().observable_lag == timedelta(0)
    assert event(observable_lag=timedelta(hours=1)).observable_lag == timedelta(hours=1)


def test_state_training_window_must_be_positive_when_present() -> None:
    """矩阵 13：`None` 表示不适用，仍然允许。"""
    with pytest.raises(ValidationError):
        state(training_window=timedelta(0))
    with pytest.raises(ValidationError):
        state(training_window=timedelta(seconds=-1))
    assert state(training_window=None).training_window is None
    assert state().training_window is None
    assert state(training_window=timedelta(days=30)).training_window == timedelta(days=30)


def test_state_space_labels_must_be_unique() -> None:
    """矩阵 14。"""
    with pytest.raises(ValidationError):
        state(state_space=("calm", "calm"))
    with pytest.raises(ValidationError):
        state(state_space=("a", "b", "a"))
    assert state(state_space=("calm",)).state_space == ("calm",)


def test_local_invariants_also_hold_on_copy_update() -> None:
    """复制更新不是绕过局部不变量的后门（ADR-0010 §D-13）。"""
    with pytest.raises(ValidationError):
        event().model_copy(update={"observable_lag": timedelta(seconds=-1)})
    with pytest.raises(ValidationError):
        state().model_copy(update={"training_window": timedelta(0)})
    with pytest.raises(ValidationError):
        state().model_copy(update={"state_space": ("calm", "calm")})
    with pytest.raises(ValidationError):
        feature().model_copy(update={"inputs": (_ref(Kind.OUTCOME),)})
    with pytest.raises(ValidationError):
        strategy().model_copy(update={"risk_policy": _ref(Kind.STRATEGY)})


# ======================================================================================
# 矩阵 15：lineage 是溯源，不是计算输入
# ======================================================================================


@pytest.mark.parametrize(("name", "build", "own_kind"), CONCRETE_SPECS, ids=_SPEC_IDS)
def test_lineage_accepts_outcome_references(name: str, build: Any, own_kind: Kind) -> None:
    """矩阵 15：ADR-0012 明确保留 lineage 现状——"定义曾参考过某个 Outcome"必须可记录。"""
    spec = build(lineage=(_ref(Kind.OUTCOME), _ref(Kind.EXPERIMENT)))
    assert spec.lineage[0].kind is Kind.OUTCOME


def test_lineage_accepts_every_kind_and_defaults_to_empty() -> None:
    assert feature().lineage == ()
    spec = feature(lineage=tuple(_ref(kind, f"n_{kind.value}") for kind in Kind))
    assert len(spec.lineage) == len(Kind)


def test_lineage_outcome_does_not_make_it_an_input() -> None:
    """溯源与计算图不混淆：lineage 放得进去，inputs 仍然放不进去。"""
    spec = feature(lineage=(_ref(Kind.OUTCOME),))
    assert spec.inputs[0].kind is Kind.REPRESENTATION  # type: ignore[union-attr]
    with pytest.raises(ValidationError):
        spec.model_copy(update={"inputs": (_ref(Kind.OUTCOME),)})


# ======================================================================================
# 矩阵 16 / 17：Schema 收窄与 v1 快照不变
# ======================================================================================


def _current_schema(model: type[Contract]) -> dict[str, Any]:
    path = CURRENT_SCHEMA_DIR / f"{model.__name__}.schema.json"
    return json.loads(path.read_text(encoding="utf-8"))  # type: ignore[no-any-return]


@pytest.mark.parametrize(("name", "build", "own_kind"), CONCRETE_SPECS, ids=_SPEC_IDS)
def test_current_schema_kind_is_single_valued(name: str, build: Any, own_kind: Kind) -> None:
    """矩阵 16：导出的 Schema 中 `kind` 是单值（`const`），不再是完整枚举。"""
    kind_schema = _current_schema(type(build()))["properties"]["kind"]
    assert kind_schema.get("const") == own_kind.value, kind_schema
    assert kind_schema.get("default") == own_kind.value
    assert "$ref" not in kind_schema
    assert kind_schema.get("enum", [own_kind.value]) == [own_kind.value]


def test_current_schema_keeps_ref_kind_as_the_full_enum() -> None:
    """引用侧不收窄：`Ref.kind` 仍指向完整的 `Kind` 枚举。"""
    schema = _current_schema(Ref)
    assert schema["properties"]["kind"] == {"$ref": "#/$defs/Kind"}
    assert set(schema["$defs"]["Kind"]["enum"]) == {kind.value for kind in Kind}


def test_current_feature_schema_declares_min_items() -> None:
    assert _current_schema(FeatureSpec)["properties"]["inputs"]["minItems"] == 1


def test_legacy_v1_snapshot_kind_stays_the_loose_enum() -> None:
    """矩阵 17：v1 是**只读历史快照**，不得被本轮收窄影响。"""
    touched = 0
    for path in LEGACY_SCHEMA_DIR.glob("*.schema.json"):
        properties = json.loads(path.read_text(encoding="utf-8"))["properties"]
        kind_schema = properties.get("kind")
        if kind_schema is None:
            continue
        touched += 1
        assert "const" not in kind_schema, path.name
        assert kind_schema["$ref"] == "#/$defs/Kind", path.name
    assert touched >= 13, "v1 快照中带 kind 的模型数量异常"
    legacy_feature = json.loads(
        (LEGACY_SCHEMA_DIR / "FeatureSpec.schema.json").read_text(encoding="utf-8")
    )
    assert "minItems" not in legacy_feature["properties"]["inputs"]


def test_contract_schema_version_is_unchanged_by_this_batch() -> None:
    """ADR-0012 §「为什么仍是 2.0.0」（D-25）：收窄的是同一个**尚未发布**的版本。"""
    from core.domain.base import CONTRACT_SCHEMA_VERSION

    assert CONTRACT_SCHEMA_VERSION == "2.0.0"
    assert feature().schema_version == "2.0.0"


# ======================================================================================
# 未做的事：把 ADR「明确不做」与「运行时延期义务」钉成可执行断言
# ======================================================================================


def test_no_point_in_time_revision_fields_were_invented() -> None:
    """ADR-0012「明确不做」：不引入 `revision` / `as_of` / `vintage` 等修订语义。"""
    forbidden = {"revision", "as_of", "vintage", "point_in_time"}
    for _, build, _ in CONCRETE_SPECS:
        fields = set(type(build()).model_fields)
        assert not (fields & forbidden), fields & forbidden


def test_contract_layer_does_not_claim_leakage_is_prevented() -> None:
    """契约层只校验**声明的**直接引用：合法声明照样可能在运行时泄漏。

    这里的 Feature 只引用 Representation，声明层面完全合法；上游 Representation 是否
    间接依赖了 Outcome、物化数据是否使用了 `available_time > t` 的行，契约层都看不见。
    """
    spec = feature(inputs=(_ref(Kind.REPRESENTATION, "rep_maybe_tainted"),))
    assert spec.inputs
    assert not hasattr(spec, "leakage_checked")
    assert not hasattr(spec, "inputs_verified")
    text = Path(REPO / "core" / "domain" / "specs.py").read_text(encoding="utf-8")
    assert "诚实边界" in text

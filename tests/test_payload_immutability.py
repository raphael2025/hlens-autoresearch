"""契约映射载荷的只读性与内容哈希载荷边界（ADR-0008）。

覆盖 ADR-0008 的 14 个映射字段：显式构造与默认值两条路径、全部变更入口、
嵌套结构、别名隔离、JSON 往返，以及逐模型的内容哈希排除表与规范化约定。

**只读性的诚实边界**（ADR-0008 §2）：这里验证的是契约使用层面的只读性，
不承诺抵御同进程内直接操作内部属性的恶意代码。
"""

from __future__ import annotations

import json
import operator
from collections.abc import Callable, Mapping, MutableMapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal

import pytest
from pydantic import Field, ValidationError

from core.contracts.profile_selection import ExperimentMetadata
from core.contracts.validation_profile import LifecycleParams, Provenance, ValidationProfile
from core.domain.artifact import StrategyArtifact
from core.domain.base import (
    Contract,
    FrozenMapping,
    Kind,
    Ref,
    canonical_json,
    content_hash,
)
from core.domain.research import ReproducibilityTuple
from core.domain.specs import FeatureSpec, RepresentationSpec, RiskPolicy, StrategySpec
from core.lifecycle.strategy import RiskGateRecord
from tests import factories

VECTOR_DIR = Path(__file__).resolve().parent / "vectors" / "v1"

T0 = datetime(2024, 1, 1, tzinfo=UTC)
SHA = "a" * 64


# --------------------------------------------------------------------------------------
# 14 个映射字段：显式构造与默认值两条路径
# --------------------------------------------------------------------------------------


def _feature_ref() -> Ref:
    return Ref(kind=Kind.FEATURE, name="f_example", version="1.0.0")


def _representation(**kw: Any) -> RepresentationSpec:
    return RepresentationSpec(
        name="rep_example",
        version="1.0.0",
        method="ohlcv_1h",
        inputs=(factories.dataset_ref(),),
        event_time_semantics="bar_close",
        **kw,
    )


def _feature(**kw: Any) -> FeatureSpec:
    return FeatureSpec(
        name="f_example",
        version="1.0.0",
        definition="realized volatility",
        inputs=(factories.dataset_ref(),),
        available_lag=timedelta(0),
        **kw,
    )


def _strategy(**kw: Any) -> StrategySpec:
    return StrategySpec(name="s_example", version="1.0.0", signals=(_feature_ref(),), **kw)


def _risk_policy(**kw: Any) -> RiskPolicy:
    return RiskPolicy(name="r_example", version="1.0.0", rules=("max_leverage",), **kw)


def _lifecycle_params(**kw: Any) -> LifecycleParams:
    return LifecycleParams(paper_period=timedelta(days=30), paper_acceptance_rule="rule", **kw)


def _experiment_metadata(**kw: Any) -> ExperimentMetadata:
    return ExperimentMetadata(
        experiment_hash="exp-hash",
        constitution_version="0.2.0-draft",
        validation_profile_version="vp:test_scope@1.0.0",
        validation_profile_hash="profile-hash",
        profile_selection_rule_version="1.0.0",
        profile_selection_key=factories.selection_key(),
        hypothesis_family_id="family-1",
        trial_index=1,
        family_trial_count=1,
        declared_research_class="swing",
        **kw,
    )


def _risk_gate(**kw: Any) -> RiskGateRecord:
    return RiskGateRecord(gate_id="RG1", passed=True, **kw)


#: B2 起这两个映射字段是必填的（内容绑定不得缺省），因此没有"空默认值"路径。
_DEPENDENCY_HASHES = factories.dependency_hashes(
    factories.hypothesis_ref(),
    factories.strategy_ref(),
    factories.risk_ref(),
    factories.outcome_ref(),
    factories.cost_model_ref(),
)
_ARTIFACT_DEPENDENCIES = factories.dependency_hashes(factories.strategy_ref())


#: 14 个映射字段：(标签, 构造器, 字段名, 代表性取值, 是否存在空默认值)。
#: ADR-0008 原列出的 `ReproducibilityTuple.profile_selection` 在 ADR-0009 中已改为结构化
#: 契约，其位置由新增的 `dependency_hashes` 接替，数量仍为 14。
MAPPING_FIELDS: tuple[tuple[str, Callable[..., Contract], str, Mapping[str, Any], bool], ...] = (
    ("RepresentationSpec.params", _representation, "params", {"window": 24}, True),
    ("FeatureSpec.params", _feature, "params", {"window": 24}, True),
    ("StrategySpec.params", _strategy, "params", {"window": 24}, True),
    (
        "StrategySpec.param_search_space",
        _strategy,
        "param_search_space",
        {"window": (12, 24)},
        True,
    ),
    ("RiskPolicy.params", _risk_policy, "params", {"max_leverage": 2}, True),
    (
        "ReproducibilityTuple.plugin_versions",
        factories.repro_tuple,
        "plugin_versions",
        {"backtest_ref@1.0.0": SHA},
        True,
    ),
    ("ReproducibilityTuple.params", factories.repro_tuple, "params", {"window": 24}, True),
    (
        "ReproducibilityTuple.param_search_space",
        factories.repro_tuple,
        "param_search_space",
        {"window": (12, 24)},
        True,
    ),
    (
        "ReproducibilityTuple.dependency_hashes",
        factories.repro_tuple,
        "dependency_hashes",
        _DEPENDENCY_HASHES,
        False,
    ),
    (
        "LifecycleParams.degradation_thresholds",
        _lifecycle_params,
        "degradation_thresholds",
        {"sharpe_drop": 0.5},
        True,
    ),
    (
        "ValidationProfile.inconclusive_bands",
        factories.validation_profile,
        "inconclusive_bands",
        {"multiple_testing_threshold": 0.05},
        True,
    ),
    (
        "ExperimentMetadata.realized_holding_stats",
        _experiment_metadata,
        "realized_holding_stats",
        {"median_hours": 12.0},
        True,
    ),
    (
        "StrategyArtifact.dependencies",
        factories.strategy_artifact,
        "dependencies",
        _ARTIFACT_DEPENDENCIES,
        False,
    ),
    ("RiskGateRecord.limits", _risk_gate, "limits", {"max_notional": "1000"}, True),
)

FIELD_IDS = tuple(entry[0] for entry in MAPPING_FIELDS)
_ARGS = ("label", "builder", "field", "value", "has_empty_default")


def _inplace_or(mapping: Any) -> object:
    mapping |= {"injected": "x"}
    return mapping


#: 全部原地变更入口。只读 Mapping 既不提供这些方法，也不支持下标赋值 / 删除。
MUTATIONS: dict[str, Callable[[Any], object]] = {
    "setitem": lambda m: operator.setitem(m, "injected", "x"),
    "delitem": lambda m: operator.delitem(m, next(iter(m))),
    "update": lambda m: m.update({"injected": "x"}),
    "pop": lambda m: m.pop(next(iter(m))),
    "popitem": lambda m: m.popitem(),
    "clear": lambda m: m.clear(),
    "setdefault": lambda m: m.setdefault("injected", "x"),
    "ior": _inplace_or,
}


def _built(builder: Callable[..., Contract], field: str, value: Mapping[str, Any]) -> Contract:
    return builder(**{field: dict(value)})


@pytest.mark.parametrize(_ARGS, MAPPING_FIELDS, ids=FIELD_IDS)
def test_mapping_field_is_read_only_mapping(
    label: str,
    builder: Callable[..., Contract],
    field: str,
    value: Mapping[str, Any],
    has_empty_default: bool,
) -> None:
    """显式传值时，对外只暴露 `collections.abc.Mapping` 语义。"""
    obtained = getattr(_built(builder, field, value), field)
    assert isinstance(obtained, Mapping), f"{label} 必须是 Mapping"
    assert not isinstance(obtained, MutableMapping), f"{label} 不得暴露可写 Mapping"
    assert dict(obtained) == dict(value)


@pytest.mark.parametrize(_ARGS, MAPPING_FIELDS, ids=FIELD_IDS)
def test_mapping_field_default_is_read_only_mapping(
    label: str,
    builder: Callable[..., Contract],
    field: str,
    value: Mapping[str, Any],
    has_empty_default: bool,
) -> None:
    """默认值与显式传值走同一校验路径（ADR-0008 决策 1）。

    内容绑定字段（`dependency_hashes` / `dependencies`）在 ADR-0009 中是**必填**的：
    它们没有空默认值，缺省即拒绝，而不是悄悄得到一个空映射。
    """
    if not has_empty_default:
        with pytest.raises(ValidationError):
            builder(**{field: {}})
        return
    obtained = getattr(builder(), field)
    assert isinstance(obtained, Mapping), f"{label} 默认值必须是 Mapping"
    assert not isinstance(obtained, MutableMapping), f"{label} 默认值不得可写"
    assert dict(obtained) == {}


@pytest.mark.parametrize("mutation", sorted(MUTATIONS), ids=sorted(MUTATIONS))
@pytest.mark.parametrize(_ARGS, MAPPING_FIELDS, ids=FIELD_IDS)
def test_mapping_field_rejects_every_mutation_entry(
    label: str,
    builder: Callable[..., Contract],
    field: str,
    value: Mapping[str, Any],
    has_empty_default: bool,
    mutation: str,
) -> None:
    contract = _built(builder, field, value)
    obtained = getattr(contract, field)
    before = dict(obtained)
    with pytest.raises((TypeError, AttributeError)):
        MUTATIONS[mutation](obtained)
    assert dict(getattr(contract, field)) == before, f"{label} 被 {mutation} 改变了"


@pytest.mark.parametrize(_ARGS, MAPPING_FIELDS, ids=FIELD_IDS)
def test_mapping_field_survives_json_round_trip_read_only(
    label: str,
    builder: Callable[..., Contract],
    field: str,
    value: Mapping[str, Any],
    has_empty_default: bool,
) -> None:
    """JSON 往返后：wire shape 仍是 object、内容与哈希一致、仍然只读。"""
    contract = _built(builder, field, value)
    wire = json.loads(contract.model_dump_json())
    assert isinstance(wire[field], dict), f"{label} 的 JSON 形状必须是 object"

    restored = type(contract).model_validate_json(contract.model_dump_json())
    assert restored.content_hash() == contract.content_hash()
    obtained = getattr(restored, field)
    assert not isinstance(obtained, MutableMapping), f"{label} 往返后不得可写"
    with pytest.raises((TypeError, AttributeError)):
        operator.setitem(obtained, "injected", "x")


@pytest.mark.parametrize(_ARGS, MAPPING_FIELDS, ids=FIELD_IDS)
def test_mapping_field_breaks_input_and_output_aliases(
    label: str,
    builder: Callable[..., Contract],
    field: str,
    value: Mapping[str, Any],
    has_empty_default: bool,
) -> None:
    """构造输入与导出结果都不得与契约内部共享可写状态。"""
    source = dict(value)
    contract = builder(**{field: source})
    before = dict(getattr(contract, field))

    source["injected"] = "x"  # 修改调用方保留的原引用
    assert dict(getattr(contract, field)) == before, f"{label} 与构造输入共享状态"

    dumped = contract.model_dump(mode="json")
    assert isinstance(dumped[field], dict)
    dumped[field]["injected"] = "x"  # 修改导出的普通 JSON 字典
    assert dict(getattr(contract, field)) == before, f"{label} 与导出结果共享状态"


@pytest.mark.parametrize(_ARGS, MAPPING_FIELDS, ids=FIELD_IDS)
def test_mapping_field_json_schema_stays_object(
    label: str,
    builder: Callable[..., Contract],
    field: str,
    value: Mapping[str, Any],
    has_empty_default: bool,
) -> None:
    model = type(_built(builder, field, value))
    modes: tuple[Literal["validation", "serialization"], ...] = ("validation", "serialization")
    for mode in modes:
        schema = model.model_json_schema(mode=mode)
        assert schema["properties"][field]["type"] == "object", (
            f"{label} 在 {mode} 模式下不是 object"
        )


# --------------------------------------------------------------------------------------
# 只读映射值类型本身：递归冻结与 Mapping 语义
# --------------------------------------------------------------------------------------


class _NestedCarrier(Contract):
    """仅用于测试递归冻结：生产契约中没有嵌套映射类型的字段。"""

    payload: FrozenMapping[str, Any] = Field(default_factory=dict, validate_default=True)


def test_frozen_mapping_recursively_freezes_nested_containers() -> None:
    raw: dict[str, Any] = {"outer": {"inner": [1, 2, {"deep": "v"}]}}
    carrier = _NestedCarrier.model_validate({"payload": raw})

    nested: Any = carrier.payload["outer"]
    assert isinstance(nested, Mapping)
    assert not isinstance(nested, MutableMapping)
    assert isinstance(nested["inner"], tuple), "嵌套序列必须转为 tuple"
    assert not isinstance(nested["inner"][2], MutableMapping), "序列内的映射同样冻结"

    for mutation in MUTATIONS.values():
        with pytest.raises((TypeError, AttributeError)):
            mutation(carrier.payload["outer"])

    raw["outer"]["inner"] = "mutated"
    assert carrier.payload["outer"]["inner"] == (1, 2, {"deep": "v"})


def test_frozen_mapping_equals_plain_mapping_and_is_not_hashable() -> None:
    mapping: FrozenMapping[str, int] = FrozenMapping({"a": 1})
    assert mapping == {"a": 1}
    assert {"a": 1} == mapping
    assert mapping != {"a": 2}
    assert list(mapping.items()) == [("a", 1)]
    with pytest.raises(TypeError):
        hash(mapping)  # Python 的 hash() 与本项目的 content_hash 是两件事（ADR-0008 §2）


# --------------------------------------------------------------------------------------
# 内容哈希载荷：逐模型显式排除表
# --------------------------------------------------------------------------------------


def test_default_exclusion_table_is_only_created_at() -> None:
    """基类默认只排除 `created_at`；不得出现全局 `*_id` / 审计时间排除（ADR-0008 决策 3）。"""
    assert Contract._non_semantic_fields() == {"created_at"}
    for model in (ReproducibilityTuple, StrategyArtifact, ExperimentMetadata):
        excluded = model._non_semantic_fields()
        assert not any(name.endswith("_id") for name in excluded), (
            f"{model.__name__} 排除了 ID 字段"
        )
        assert "recorded_at" not in excluded
        assert "occurred_at" not in excluded


def test_profile_status_is_not_part_of_content_hash() -> None:
    """`status` 是操作状态：draft → frozen 不得改变内容哈希（ADR-0008 决策 3）。"""
    provenance = Provenance(calibration_report="calib-1", approval_adr="ADR-XXXX")
    draft = factories.validation_profile(provenance=provenance, created_at=T0)
    frozen = factories.validation_profile(status="frozen", provenance=provenance, created_at=T0)

    assert draft.status.value == "draft"
    assert frozen.status.value == "frozen"
    assert draft.content_hash() == frozen.content_hash()
    assert ValidationProfile._non_semantic_fields() == {"created_at", "status"}


def test_profile_provenance_and_params_still_change_content_hash() -> None:
    """`provenance` 保留在哈希内；阈值变化必须改变哈希。"""
    base = factories.validation_profile(created_at=T0)
    other_provenance = factories.validation_profile(
        created_at=T0, provenance=Provenance(calibration_report="calib-2")
    )
    other_band = factories.validation_profile(
        created_at=T0, inconclusive_bands={"multiple_testing_threshold": 0.05}
    )
    assert base.content_hash() != other_provenance.content_hash()
    assert base.content_hash() != other_band.content_hash()


def test_mapping_key_insertion_order_does_not_change_content_hash() -> None:
    first = _strategy(params={"a": 1, "b": 2}, created_at=T0)
    second = _strategy(params={"b": 2, "a": 1}, created_at=T0)
    assert first.content_hash() == second.content_hash()
    assert content_hash({"a": 1, "b": 2}) == content_hash({"b": 2, "a": 1})


# --------------------------------------------------------------------------------------
# 规范化约定：禁止 NaN / Infinity 与未知类型静默字符串化
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
def test_canonical_json_rejects_non_finite_numbers(bad: float) -> None:
    """不得先把非法数值转成 null 或 NaN 再参与哈希（ADR-0008 决策 4）。"""
    with pytest.raises(ValueError):
        canonical_json({"a": bad})
    with pytest.raises(ValueError):
        content_hash({"a": bad})


def test_canonical_json_rejects_unknown_types() -> None:
    class Weird:
        def __str__(self) -> str:
            return "weird"

    with pytest.raises(TypeError):
        canonical_json({"a": Weird()})


def test_canonical_json_keeps_sorted_compact_non_ascii_form() -> None:
    assert canonical_json({"b": 1, "a": "中文"}) == '{"a":"中文","b":1}'


# --------------------------------------------------------------------------------------
# v1 固定向量：legacy 语义
# --------------------------------------------------------------------------------------

V1_VECTORS = sorted(VECTOR_DIR.glob("*.json"))


def test_v1_vector_directory_is_populated() -> None:
    assert {p.stem for p in V1_VECTORS} == {
        "strategy_spec",
        "reproducibility_tuple",
        "experiment_spec",
        "validation_profile_draft",
        "validation_profile_frozen",
    }


@pytest.mark.parametrize("path", V1_VECTORS, ids=lambda p: p.stem)
def test_v1_vector_legacy_hash_is_pinned(path: Path) -> None:
    """固定向量验证的是 **v1 legacy 语义**（v1 排除表 + 规范化 JSON），不是 v2 期望值。"""
    record = json.loads(path.read_text(encoding="utf-8"))
    assert record["contract_schema_version"] == "1.0.0"
    excluded = set(record["legacy_excluded_fields"])
    hash_payload = {k: v for k, v in record["payload"].items() if k not in excluded}
    assert content_hash(hash_payload) == record["legacy_content_hash"], (
        f"{path.name} 的 v1 哈希语义被改变"
    )


def test_v1_vector_hash_is_key_order_independent() -> None:
    record = json.loads((VECTOR_DIR / "reproducibility_tuple.json").read_text(encoding="utf-8"))
    excluded = set(record["legacy_excluded_fields"])
    payload = {k: v for k, v in record["payload"].items() if k not in excluded}
    reordered = dict(reversed(list(payload.items())))
    assert list(reordered) != list(payload)
    assert content_hash(reordered) == record["legacy_content_hash"]


def test_v1_profile_vectors_record_the_status_defect() -> None:
    """v1 下同内容、仅 status 不同的 Profile 哈希不同——这是被 ADR-0008 修正的缺陷。"""
    draft = json.loads((VECTOR_DIR / "validation_profile_draft.json").read_text(encoding="utf-8"))
    frozen = json.loads((VECTOR_DIR / "validation_profile_frozen.json").read_text(encoding="utf-8"))
    assert draft["payload"]["status"] == "draft"
    assert frozen["payload"]["status"] == "frozen"
    assert {k: v for k, v in draft["payload"].items() if k != "status"} == {
        k: v for k, v in frozen["payload"].items() if k != "status"
    }
    assert draft["legacy_content_hash"] != frozen["legacy_content_hash"]

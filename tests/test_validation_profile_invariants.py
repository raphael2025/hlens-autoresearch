"""ADR-0014（D-20.4）：Validation Profile 的普适结构不变量。

覆盖 ADR-0014「验收测试矩阵」1 ~ 13。三条主线：

1. **符号约束**：训练 / 检验 / 步长窗口、封存区长度、Paper 观察期必须严格为正；
   embargo 与封存区最大延长量非负（零是合法配置，负数不是）；
   两个成本压力倍数序列的**每一项**严格为正。
2. **跨字段语义**：`CostStressParams.cost_model` 必须指向 `cost_model` kind。
   这不是 JSON Schema 能表达的形状约束，只能由运行时校验保证。
3. **Schema 诚实性**：时长字段的线格式是 ISO 8601 duration 字符串，数值关键字
   （`minimum` / `exclusiveMinimum`）在字符串上没有意义，因此导出的 Schema 保持
   `type: string, format: duration`，**不**伪装数值下界；倍数是真数值，其单元素约束
   如实导出为 `items.exclusiveMinimum`。

**这里出现的所有数字都是测试数据，不是被批准的验证阈值。** 结构合法不等于校准合理：
窗口该多长、压力倍数取多少、观察期多久，都属于 Phase 4 校准（两步冻结 Step 2），
本批次一个数值也不选。既有的 `_boundary_after_start` 等校验行为保持不变。
"""

from __future__ import annotations

import json
import math
from collections.abc import Callable
from datetime import timedelta
from pathlib import Path

import pytest
from pydantic import ValidationError

from core.contracts.validation_profile import (
    CostStressParams,
    DataSplitParams,
    LifecycleParams,
    ValidationProfile,
    WalkForwardParams,
)
from core.domain.base import Contract, Kind, Ref
from tests import factories

REPO = Path(__file__).resolve().parents[1]
CURRENT_SCHEMA_DIR = REPO / "schemas"
LEGACY_SCHEMA_DIR = CURRENT_SCHEMA_DIR / "v1"

#: 基线 Profile：结构合法，数值只是测试夹具。
BASE = factories.validation_profile()

#: 非法倍数：零、负值与三个非有限值（后者由 ADR-0013 §D-20.1 拒绝）。
BAD_MULTIPLIERS = (0.0, -1.0, -0.5, float("nan"), float("inf"), float("-inf"))
#: 负时长的两种写法，避免只测"刚好负一秒"。
NEGATIVE_DURATIONS = (-timedelta(seconds=1), -timedelta(days=400))


def _rebuild[M: Contract](model: M, **overrides: object) -> M:
    """以合法实例为基线重建同类型模型，只替换指定字段（走完整校验）。"""
    payload: dict[str, object] = {**model.__dict__, **overrides}
    return type(model).model_validate(payload)


def _walk_forward(**overrides: object) -> WalkForwardParams:
    return _rebuild(BASE.data_split.walk_forward, **overrides)


def _data_split(**overrides: object) -> DataSplitParams:
    return _rebuild(BASE.data_split, **overrides)


def _cost_stress(**overrides: object) -> CostStressParams:
    return _rebuild(BASE.cost_stress, **overrides)


def _lifecycle(**overrides: object) -> LifecycleParams:
    return _rebuild(BASE.lifecycle, **overrides)


Builder = Callable[..., Contract]

#: 必须严格为正的时长字段：(构造器, 字段名)。
POSITIVE_DURATION_FIELDS: tuple[tuple[Builder, str], ...] = (
    (_walk_forward, "train_window"),
    (_walk_forward, "test_window"),
    (_walk_forward, "step"),
    (_data_split, "sealed_oos_length"),
    (_lifecycle, "paper_period"),
)
#: 允许为零的时长字段：零 = 不设隔离期 / 不允许延长。
NON_NEGATIVE_DURATION_FIELDS: tuple[tuple[Builder, str], ...] = (
    (_data_split, "embargo"),
    (_data_split, "sealed_oos_max_extension"),
)
#: 倍数序列字段；两者都逐元素约束。
MULTIPLIER_FIELDS = ("stress_multipliers", "reported_only_multipliers")

#: 时长字段在导出 Schema 中的位置：字段名 → `$defs` 里的模型名。
DURATION_FIELD_DEFS = {
    "train_window": "WalkForwardParams",
    "test_window": "WalkForwardParams",
    "step": "WalkForwardParams",
    "sealed_oos_length": "DataSplitParams",
    "sealed_oos_max_extension": "DataSplitParams",
    "embargo": "DataSplitParams",
    "paper_period": "LifecycleParams",
}
#: 数值关键字：字符串类型的字段上出现它们即为语义无效的 Schema。
NUMERIC_KEYWORDS = ("minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum")


def _ids(value: object) -> str | None:
    """参数化 id：用字段名命名，构造器本身不参与命名。"""
    return value if isinstance(value, str) else None


def _profile_schema() -> dict[str, object]:
    raw = (CURRENT_SCHEMA_DIR / "ValidationProfile.schema.json").read_text(encoding="utf-8")
    schema: dict[str, object] = json.loads(raw)
    return schema


def _defs(schema: dict[str, object], model: str) -> dict[str, dict[str, object]]:
    defs: dict[str, dict[str, dict[str, object]]] = schema["$defs"]  # type: ignore[assignment]
    return defs[model]["properties"]  # type: ignore[return-value]


# ======================================================================================
# 矩阵 1 ~ 3、9：严格为正的时长字段
# ======================================================================================


@pytest.mark.parametrize(("build", "field"), POSITIVE_DURATION_FIELDS, ids=_ids)
@pytest.mark.parametrize("bad", (timedelta(0), *NEGATIVE_DURATIONS), ids=repr)
def test_positive_duration_rejects_zero_and_negative(
    build: Builder, field: str, bad: timedelta
) -> None:
    """零长度或负长度的窗口 / 封存区 / 观察期不是校准选择，而是结构上无意义的配置。"""
    with pytest.raises(ValidationError) as exc:
        build(**{field: bad})
    assert {error["loc"] for error in exc.value.errors()} == {(field,)}


@pytest.mark.parametrize(("build", "field"), POSITIVE_DURATION_FIELDS, ids=_ids)
@pytest.mark.parametrize("good", (timedelta(seconds=1), timedelta(days=30)), ids=repr)
def test_positive_duration_accepts_positive(build: Builder, field: str, good: timedelta) -> None:
    """本 ADR 只约束符号：任何正长度都结构合法，合理性由 Phase 4 校准判断。"""
    assert getattr(build(**{field: good}), field) == good


# ======================================================================================
# 矩阵 4、5：非负的时长字段（零是合法配置）
# ======================================================================================


@pytest.mark.parametrize(("build", "field"), NON_NEGATIVE_DURATION_FIELDS, ids=_ids)
@pytest.mark.parametrize("good", (timedelta(0), timedelta(hours=1)), ids=repr)
def test_non_negative_duration_accepts_zero_and_positive(
    build: Builder, field: str, good: timedelta
) -> None:
    assert getattr(build(**{field: good}), field) == good


@pytest.mark.parametrize(("build", "field"), NON_NEGATIVE_DURATION_FIELDS, ids=_ids)
@pytest.mark.parametrize("bad", NEGATIVE_DURATIONS, ids=repr)
def test_non_negative_duration_rejects_negative(build: Builder, field: str, bad: timedelta) -> None:
    with pytest.raises(ValidationError) as exc:
        build(**{field: bad})
    assert {error["loc"] for error in exc.value.errors()} == {(field,)}


# ======================================================================================
# 矩阵 6、7：压力倍数逐元素严格为正且有限
# ======================================================================================


@pytest.mark.parametrize("field", MULTIPLIER_FIELDS)
@pytest.mark.parametrize("bad", BAD_MULTIPLIERS, ids=repr)
@pytest.mark.parametrize("index", (0, 2), ids=("first", "third"))
def test_multipliers_reject_non_positive_or_non_finite_elements(
    field: str, bad: float, index: int
) -> None:
    """约束作用于**每个元素**，不只是容器：非首元素同样被拒绝。"""
    values = [1.0, 2.0, 3.0]
    values[index] = bad
    with pytest.raises(ValidationError) as exc:
        _cost_stress(**{field: tuple(values)})
    assert {error["loc"] for error in exc.value.errors()} == {(field, index)}


@pytest.mark.parametrize("field", MULTIPLIER_FIELDS)
@pytest.mark.parametrize("good", ((0.5,), (1.0, 2.0), (0.25, 1.0, 100.0)), ids=repr)
def test_multipliers_accept_positive_elements(field: str, good: tuple[float, ...]) -> None:
    """小于 1 的倍数也结构合法：本 ADR 不选压力方向，也不选取值。"""
    assert getattr(_cost_stress(**{field: good}), field) == good


def test_multipliers_reject_non_finite_via_json_entry() -> None:
    """JSON 文本入口同样被拦住（ADR-0013 §D-20.1 的 `allow_inf_nan=False`）。"""
    payload = json.loads(BASE.cost_stress.model_dump_json())
    payload["stress_multipliers"] = [1.0, "__BAD__"]
    raw = json.dumps(payload).replace('"__BAD__"', "Infinity")
    with pytest.raises(ValidationError):
        CostStressParams.model_validate_json(raw)


def test_stress_multipliers_still_require_at_least_one_element() -> None:
    """既有的 `min_length=1` 不受本批次影响；`reported_only_multipliers` 可以为空。"""
    with pytest.raises(ValidationError):
        _cost_stress(stress_multipliers=())
    assert _cost_stress(reported_only_multipliers=()).reported_only_multipliers == ()


# ======================================================================================
# 矩阵 8：cost_model 的 kind
# ======================================================================================


@pytest.mark.parametrize("kind", [kind for kind in Kind if kind is not Kind.COST_MODEL])
def test_cost_model_rejects_other_kinds(kind: Kind) -> None:
    wrong = Ref(kind=kind, name="not_a_cost_model", version="1.0.0")
    with pytest.raises(ValidationError):
        _cost_stress(cost_model=wrong)


def test_cost_model_accepts_cost_model_kind() -> None:
    ref = factories.cost_model_ref(name="cost_v2", version="2.1.0")
    assert _cost_stress(cost_model=ref).cost_model == ref


# ======================================================================================
# 矩阵 10、11：合法 Profile 与既有校验
# ======================================================================================


def test_structurally_valid_profile_is_accepted() -> None:
    """完整 Profile 仍可构造，序列化往返保持相等（既有对象不被本批次破坏）。"""
    assert BASE.lifecycle.paper_period > timedelta(0)
    assert ValidationProfile.model_validate_json(BASE.model_dump_json()) == BASE


def test_zero_embargo_and_zero_extension_profile_is_accepted() -> None:
    """零 embargo + 零延长量是合法配置，必须能进入完整 Profile。"""
    data_split = _data_split(embargo=timedelta(0), sealed_oos_max_extension=timedelta(0))
    profile = factories.validation_profile(data_split=data_split)
    assert profile.data_split.embargo == timedelta(0)
    assert profile.data_split.sealed_oos_max_extension == timedelta(0)


def test_existing_boundary_validator_is_unchanged() -> None:
    """`_boundary_after_start` 行为不变：边界必须晚于研究窗口起点（矩阵 11）。"""
    with pytest.raises(ValidationError):
        _data_split(sealed_oos_boundary=BASE.data_split.research_window_start)
    later = BASE.data_split.research_window_start.replace(
        year=BASE.data_split.research_window_start.year + 1
    )
    assert _data_split(sealed_oos_boundary=later).sealed_oos_boundary == later


def test_independent_violations_are_aggregated_in_one_validation() -> None:
    """三处互不相关的违规在**同一次**校验中一起报出，而不是一次只报一个。"""
    payload = json.loads(BASE.model_dump_json())
    payload["data_split"]["walk_forward"]["train_window"] = "PT0S"
    payload["data_split"]["embargo"] = "-PT1S"
    payload["cost_stress"]["stress_multipliers"] = [1.0, 0.0]
    payload["lifecycle"]["paper_period"] = "-PT1S"
    with pytest.raises(ValidationError) as exc:
        ValidationProfile.model_validate_json(json.dumps(payload))
    assert {error["loc"] for error in exc.value.errors()} == {
        ("data_split", "walk_forward", "train_window"),
        ("data_split", "embargo"),
        ("cost_stress", "stress_multipliers", 1),
        ("lifecycle", "paper_period"),
    }


# ======================================================================================
# 矩阵 12、13：Schema 表达限制与 v1 只读路径
# ======================================================================================


@pytest.mark.parametrize(("field", "model"), sorted(DURATION_FIELD_DEFS.items()))
def test_duration_fields_stay_honest_strings_in_json_schema(field: str, model: str) -> None:
    """时长字段的 Schema 保持 `string` / `duration`，不得带语义无效的数值下界。

    `minimum` / `exclusiveMinimum` 只对数值有意义，不能表达 ISO 8601 duration 字符串的
    顺序关系；符号约束因此是运行时约束（07-validation.md §5.4）。
    """
    prop = _defs(_profile_schema(), model)[field]
    assert prop["type"] == "string"
    assert prop["format"] == "duration"
    assert not [keyword for keyword in NUMERIC_KEYWORDS if keyword in prop]


@pytest.mark.parametrize("field", MULTIPLIER_FIELDS)
def test_multiplier_item_bound_is_exported_to_json_schema(field: str) -> None:
    """倍数是真数值，逐元素下界如实出现在 `items` 上（不是容器上）。"""
    prop = _defs(_profile_schema(), "CostStressParams")[field]
    items: dict[str, object] = prop["items"]  # type: ignore[assignment]
    assert items["type"] == "number"
    assert items["exclusiveMinimum"] == 0
    assert not [keyword for keyword in NUMERIC_KEYWORDS if keyword in prop]


def test_cost_model_kind_is_not_claimed_in_json_schema() -> None:
    """跨字段语义不在 Schema 中伪装成形状约束：`cost_model` 仍是普通 `Ref` 引用。"""
    prop = _defs(_profile_schema(), "CostStressParams")["cost_model"]
    assert prop == {"$ref": "#/$defs/Ref"}


def test_v1_snapshot_did_not_inherit_the_new_bounds() -> None:
    """v1 快照是历史事实，不随 v2 收紧而变化（逐字节不变由 Git 与既有测试共同保证）。"""
    legacy: dict[str, object] = json.loads(
        (LEGACY_SCHEMA_DIR / "ValidationProfile.schema.json").read_text(encoding="utf-8")
    )
    for field in MULTIPLIER_FIELDS:
        items: dict[str, object] = _defs(legacy, "CostStressParams")[field]["items"]  # type: ignore[assignment]
        assert "exclusiveMinimum" not in items
    for field, model in DURATION_FIELD_DEFS.items():
        assert _defs(legacy, model)[field]["type"] == "string"


def test_positive_multiplier_relies_on_the_shared_finiteness_rule() -> None:
    """有限性不在本模块重复声明：`Contract` 基类的统一配置负责（ADR-0013 §D-20.1）。"""
    assert CostStressParams.model_config["allow_inf_nan"] is False
    assert math.isnan(float("nan"))  # 说明 BAD_MULTIPLIERS 里确实含 NaN

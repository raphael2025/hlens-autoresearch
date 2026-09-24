"""ADR-0013（D-19、D-20）：确定性判定函数与数值合法性。

覆盖 ADR-0013「验收测试矩阵」1 ~ 19。三条主线：

1. `ValidationReport.verdict` **精确等于**门结果集合的确定性函数，任何想影响判定的理由
   都必须物化为报告内的一个门；`gate_id` 在报告与实验元数据内唯一；
   `threshold` 与 `threshold_source` 成对出现；
2. 所有契约的所有浮点字段在**校验阶段**即拒绝 NaN / ±Infinity（标量、序列、映射、嵌套契约、
   Python 与 JSON 两条入口），`canonical_json` 作为第二道关口继续直接报错；
3. 两个概率型阈值的结构范围收紧为闭区间 `[0, 1]`，且契约中没有写死的门 ID 清单。

**边界**：这里校验的只是**结构与配对格式**。`threshold_source` 是否真的指向所绑定 Profile
版本中的字段、报告是否包含 Profile 要求的全部门、`value` 是否真由 `metric` 算出，
都是持有 Profile 实例的验证服务的义务（ADR-0013「运行时延期义务」），契约层不作声称。
"""

from __future__ import annotations

import ast
import inspect
import json
import math
import re
from itertools import product
from pathlib import Path
from typing import Any

import pytest
from pydantic import ConfigDict, ValidationError

from core.contracts.profile_selection import ExperimentMetadata
from core.contracts.registry import CONTRACT_MODELS
from core.contracts.validation_profile import (
    CostStressParams,
    LifecycleParams,
    SignificanceParams,
    ValidationProfile,
)
from core.domain.base import Contract, canonical_json
from core.domain.research import (
    GateResult,
    ValidationReport,
    Verdict,
    derive_verdict,
    require_unique_gate_ids,
)
from tests import factories

REPO = Path(__file__).resolve().parents[1]
CURRENT_SCHEMA_DIR = REPO / "schemas"
LEGACY_SCHEMA_DIR = CURRENT_SCHEMA_DIR / "v1"
CONTRACT_SOURCE_DIRS = (REPO / "core" / "domain", REPO / "core" / "contracts")
#: 门 ID 的典型写法（`G0`、`G3`…）。契约里不得出现这样的字面量（D-20.3）。
GATE_ID_LITERAL = re.compile(r"G[0-9]+")

#: 三个非法浮点值：两种无穷与 NaN。
BAD_FLOATS = (float("nan"), float("inf"), float("-inf"))
#: JSON 文本入口用的非法字面量（`json` / jiter 都会把它们解析成非法浮点）。
BAD_JSON_LITERALS = ("NaN", "Infinity", "-Infinity")


def _gates(*verdicts: Verdict) -> tuple[GateResult, ...]:
    """按给定判定构造 `gate_id` 互不相同的门序列。"""
    return tuple(
        factories.gate_result(verdict, gate_id=f"G{index}")
        for index, verdict in enumerate(verdicts)
    )


def _metadata(**overrides: object) -> ExperimentMetadata:
    payload: dict[str, object] = {
        "experiment_hash": "exp-hash",
        "constitution_version": "0.2.0-draft",
        "validation_profile_version": "vp:test_scope@1.0.0",
        "validation_profile_hash": "profile-hash",
        "profile_selection_rule_version": "1.0.0",
        "profile_selection_key": factories.selection_key(),
        "hypothesis_family_id": "family-1",
        "trial_index": 1,
        "family_trial_count": 1,
        "declared_research_class": "swing",
    }
    payload.update(overrides)
    return ExperimentMetadata(**payload)  # type: ignore[arg-type]


# ======================================================================================
# D-19.1：整体判定是门结果集合的精确确定性函数（矩阵 1 ~ 7）
# ======================================================================================


@pytest.mark.parametrize("wrong", [Verdict.PASS, Verdict.INCONCLUSIVE])
def test_failing_gate_forces_fail(wrong: Verdict) -> None:
    """有 FAIL 门时只能判 FAIL：PASS 与 INCONCLUSIVE 都被拒绝（矩阵 1、2）。"""
    gates = _gates(Verdict.PASS, Verdict.FAIL, Verdict.INCONCLUSIVE)
    with pytest.raises(ValidationError):
        factories.validation_report(verdict=wrong, gates=gates)
    assert factories.validation_report(verdict=Verdict.FAIL, gates=gates).verdict is Verdict.FAIL


@pytest.mark.parametrize("wrong", [Verdict.PASS, Verdict.FAIL])
def test_inconclusive_gate_without_failure_forces_inconclusive(wrong: Verdict) -> None:
    """无 FAIL 但有 INCONCLUSIVE 时只能判 INCONCLUSIVE（矩阵 3、4）。

    这是"证据不足不得等同于 PASS"的可执行形式，同时也堵住反方向的"证据不足直接判 FAIL"。
    """
    gates = _gates(Verdict.PASS, Verdict.INCONCLUSIVE)
    with pytest.raises(ValidationError):
        factories.validation_report(verdict=wrong, gates=gates)
    report = factories.validation_report(verdict=Verdict.INCONCLUSIVE, gates=gates)
    assert report.verdict is Verdict.INCONCLUSIVE


@pytest.mark.parametrize("wrong", [Verdict.FAIL, Verdict.INCONCLUSIVE])
def test_all_gates_passing_forces_pass(wrong: Verdict) -> None:
    """全部门 PASS 时只能判 PASS：这是双向检查，不是单向的"不得为 PASS"（矩阵 5、6）。"""
    gates = _gates(Verdict.PASS, Verdict.PASS)
    with pytest.raises(ValidationError):
        factories.validation_report(verdict=wrong, gates=gates)
    assert factories.validation_report(verdict=Verdict.PASS, gates=gates).verdict is Verdict.PASS


@pytest.mark.parametrize("size", [1, 2, 3])
def test_every_gate_combination_matches_the_verdict_function(size: int) -> None:
    """穷举 1 ~ 3 个门的全部判定组合 × 三个候选整体判定（矩阵 7）。

    每个组合恰好只有一个整体判定被接受，且它等于 `derive_verdict`。
    """
    for combination in product(Verdict, repeat=size):
        gates = _gates(*combination)
        expected = derive_verdict(gates)
        accepted = []
        for candidate in Verdict:
            try:
                report = factories.validation_report(verdict=candidate, gates=gates)
            except ValidationError:
                continue
            accepted.append(report.verdict)
        assert accepted == [expected], f"{combination} 应当只接受 {expected}，实际 {accepted}"


def test_verdict_function_is_total_and_deterministic() -> None:
    """判定函数覆盖所有可能的门结果集合，且与门的顺序无关。"""
    assert derive_verdict(_gates(Verdict.PASS)) is Verdict.PASS
    assert derive_verdict(_gates(Verdict.INCONCLUSIVE, Verdict.PASS)) is Verdict.INCONCLUSIVE
    assert derive_verdict(_gates(Verdict.PASS, Verdict.INCONCLUSIVE)) is Verdict.INCONCLUSIVE
    assert derive_verdict(_gates(Verdict.INCONCLUSIVE, Verdict.FAIL)) is Verdict.FAIL
    assert derive_verdict(_gates(Verdict.FAIL, Verdict.INCONCLUSIVE)) is Verdict.FAIL


def test_report_still_requires_at_least_one_gate() -> None:
    """判定必须有证据支撑：空门集合的报告不存在（现状不变）。"""
    with pytest.raises(ValidationError):
        factories.validation_report(gates=())


def test_verdict_cannot_be_detached_by_copy_update() -> None:
    """`model_copy(update=...)` 重新走完整校验，不能借此绕开判定函数（ADR-0010 D-13）。"""
    report = factories.validation_report(verdict=Verdict.PASS, gates=_gates(Verdict.PASS))
    with pytest.raises(ValidationError):
        report.model_copy(update={"verdict": Verdict.FAIL})
    with pytest.raises(ValidationError):
        report.model_copy(update={"gates": _gates(Verdict.FAIL)})


def test_verdict_function_is_enforced_through_json_input() -> None:
    payload = factories.validation_report(
        verdict=Verdict.FAIL, gates=_gates(Verdict.FAIL)
    ).model_dump_json()
    assert ValidationReport.model_validate_json(payload).verdict is Verdict.FAIL
    detached = json.loads(payload)
    detached["verdict"] = Verdict.PASS.value
    with pytest.raises(ValidationError):
        ValidationReport.model_validate_json(json.dumps(detached))


# ======================================================================================
# D-19.2：gate_id 在报告与实验元数据内唯一（矩阵 8）
# ======================================================================================


def test_report_rejects_duplicate_gate_ids() -> None:
    duplicated = (
        factories.gate_result(Verdict.PASS, gate_id="G0"),
        factories.gate_result(Verdict.PASS, gate_id="G0"),
    )
    with pytest.raises(ValidationError):
        factories.validation_report(verdict=Verdict.PASS, gates=duplicated)
    distinct = factories.validation_report(verdict=Verdict.PASS, gates=_gates(*[Verdict.PASS] * 2))
    assert len(distinct.gates) == 2


def test_duplicate_gate_ids_are_rejected_even_with_different_results() -> None:
    """同一检查有两个结果时判定函数不再良定义，因此重复 ID 本身就非法。"""
    conflicting = (
        factories.gate_result(Verdict.PASS, gate_id="G1"),
        factories.gate_result(Verdict.FAIL, gate_id="G1"),
    )
    with pytest.raises(ValidationError):
        factories.validation_report(verdict=Verdict.FAIL, gates=conflicting)


def test_metadata_rejects_duplicate_gate_ids() -> None:
    duplicated = (
        factories.gate_result(Verdict.PASS, gate_id="G0"),
        factories.gate_result(Verdict.INCONCLUSIVE, gate_id="G0"),
    )
    with pytest.raises(ValidationError):
        _metadata(gate_results=duplicated)
    assert len(_metadata(gate_results=_gates(Verdict.PASS, Verdict.FAIL)).gate_results) == 2


def test_metadata_gate_results_may_be_empty_but_never_duplicated() -> None:
    """元数据的门记录是可选的（现状不变）；一旦出现就必须唯一。"""
    assert _metadata().gate_results == ()
    require_unique_gate_ids((), "empty")


# ======================================================================================
# D-19.3：threshold 与 threshold_source 成对出现（矩阵 9 ~ 11）
# ======================================================================================


def _gate(**overrides: object) -> GateResult:
    payload: dict[str, object] = {
        "gate_id": "G0",
        "metric": "m",
        "value": 1.0,
        "verdict": Verdict.PASS,
    }
    payload.update(overrides)
    return GateResult(**payload)  # type: ignore[arg-type]


def test_threshold_without_source_is_rejected() -> None:
    with pytest.raises(ValidationError):
        _gate(threshold=1.0)


def test_source_without_threshold_is_rejected() -> None:
    with pytest.raises(ValidationError):
        _gate(threshold_source="significance.overfitting_threshold")


def test_both_present_or_both_absent_is_accepted() -> None:
    paired = _gate(threshold=1.0, threshold_source="significance.overfitting_threshold")
    assert paired.threshold == 1.0 and paired.threshold_source
    reported_only = _gate()
    assert reported_only.threshold is None and reported_only.threshold_source is None


@pytest.mark.parametrize("blank", ["", "   "])
def test_blank_source_is_not_a_source(blank: str) -> None:
    """空白来源不算来源：有阈值时拒绝；无阈值时它仍是非 `None` 的来源，同样拒绝。"""
    with pytest.raises(ValidationError):
        _gate(threshold=1.0, threshold_source=blank)
    with pytest.raises(ValidationError):
        _gate(threshold_source=blank)


def test_pairing_survives_copy_update() -> None:
    paired = _gate(threshold=1.0, threshold_source="significance.overfitting_threshold")
    with pytest.raises(ValidationError):
        paired.model_copy(update={"threshold": None})
    with pytest.raises(ValidationError):
        paired.model_copy(update={"threshold_source": None})


# ======================================================================================
# D-20.1：全局拒绝 NaN / ±Infinity（矩阵 12 ~ 15）
# ======================================================================================


def _float_field_paths(schema: Any) -> list[str]:
    """收集 core schema 中全部 `float` 节点的路径（用于覆盖面审计）。"""
    found: list[str] = []
    seen: set[int] = set()

    def walk(node: Any, path: str) -> None:
        if id(node) in seen:
            return
        seen.add(id(node))
        if isinstance(node, dict):
            if node.get("type") == "float":
                found.append(path)
            for key, value in node.items():
                walk(value, f"{path}/{key}")
        elif isinstance(node, list):
            for index, value in enumerate(node):
                walk(value, f"{path}[{index}]")

    walk(schema, "")
    return found


def _model_configs(schema: Any) -> list[tuple[str, dict[str, Any]]]:
    """收集 core schema 中全部 `model` 节点的 `(类名, config)`。"""
    found: list[tuple[str, dict[str, Any]]] = []
    seen: set[int] = set()

    def walk(node: Any) -> None:
        if id(node) in seen:
            return
        seen.add(id(node))
        if isinstance(node, dict):
            if node.get("type") == "model":
                cls = node.get("cls")
                found.append((getattr(cls, "__name__", str(cls)), dict(node.get("config", {}))))
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk(schema)
    return found


def test_every_contract_model_inherits_the_finite_number_config() -> None:
    """统一机制：`Contract` 基类的 `allow_inf_nan=False`，且无任何模型把它改回去。"""
    assert Contract.model_config["allow_inf_nan"] is False
    for model in CONTRACT_MODELS:
        assert model.model_config.get("allow_inf_nan") is False, model.__name__


def test_finite_number_config_reaches_every_nested_contract() -> None:
    """审计而非抽样：每个注册契约的 core schema 里，所有 model 节点都带该配置。"""
    checked = 0
    for model in CONTRACT_MODELS:
        for name, config in _model_configs(model.__pydantic_core_schema__):
            assert config.get("allow_inf_nan") is False, f"{model.__name__} → {name}"
            checked += 1
    assert checked > len(CONTRACT_MODELS), "审计应当穿透到嵌套契约，而不是只看顶层"


def test_the_audit_would_catch_a_model_that_opts_out() -> None:
    """反向验证审计有效：故意退出该配置的模型会被上面的检查抓到。"""

    class Escaped(Contract):
        model_config = ConfigDict(
            frozen=True, extra="forbid", str_strip_whitespace=True, allow_inf_nan=True
        )

        value: float

    assert math.isnan(Escaped(value=float("nan")).value)
    configs = dict(_model_configs(Escaped.__pydantic_core_schema__))
    assert configs["Escaped"].get("allow_inf_nan") is not False


def test_every_registered_contract_has_its_float_fields_audited() -> None:
    """浮点节点确实存在于被审计的 core schema 中（审计不是对空集生效）。"""
    with_floats = {
        model.__name__
        for model in CONTRACT_MODELS
        if _float_field_paths(model.__pydantic_core_schema__)
    }
    assert {"GateResult", "ValidationReport", "ValidationProfile", "ExperimentMetadata"} <= (
        with_floats
    )


@pytest.mark.parametrize("bad", BAD_FLOATS)
def test_scalar_float_fields_reject_illegal_numbers(bad: float) -> None:
    """标量：`value`、`threshold`，以及 Profile 中的其它比例 / 倍数字段（矩阵 12）。"""
    with pytest.raises(ValidationError):
        _gate(value=bad)
    with pytest.raises(ValidationError):
        _gate(value=1.0, threshold=bad, threshold_source="significance.overfitting_threshold")
    stability = factories.validation_profile().parameter_stability
    with pytest.raises(ValidationError):
        stability.model_copy(update={"min_neighborhood_performance_ratio": bad})


@pytest.mark.parametrize("bad", BAD_FLOATS)
def test_float_tuples_reject_illegal_numbers(bad: float) -> None:
    """序列：tuple 中的 float 同样被拒绝。"""
    profile = factories.validation_profile()
    with pytest.raises(ValidationError):
        profile.cost_stress.model_copy(update={"stress_multipliers": (2.0, bad)})
    with pytest.raises(ValidationError):
        profile.cost_stress.model_copy(update={"reported_only_multipliers": (bad,)})


@pytest.mark.parametrize("bad", BAD_FLOATS)
def test_float_mappings_reject_illegal_numbers(bad: float) -> None:
    """映射：三个 `FrozenMapping[str, float]` 字段都在校验阶段拒绝（矩阵 13）。"""
    with pytest.raises(ValidationError):
        factories.validation_profile(inconclusive_bands={"g0": 0.1, "g1": bad})
    with pytest.raises(ValidationError):
        LifecycleParams.model_validate(
            factories.validation_profile().lifecycle.model_dump()
            | {"degradation_thresholds": {"sharpe": bad}}
        )
    with pytest.raises(ValidationError):
        _metadata(realized_holding_stats={"median_bars": 4.0, "p95_bars": bad})


@pytest.mark.parametrize("bad", BAD_FLOATS)
def test_nested_contracts_reject_illegal_numbers(bad: float) -> None:
    """嵌套契约：非法数值出现在深层子模型里同样被拒绝（顶层模型不放行）。"""
    payload = factories.validation_profile().model_dump()
    payload["data_split"]["walk_forward"]["min_positive_window_fraction"] = bad
    with pytest.raises(ValidationError):
        ValidationProfile.model_validate(payload)

    report_payload = factories.validation_report().model_dump()
    report_payload["gates"][0]["value"] = bad
    with pytest.raises(ValidationError):
        ValidationReport.model_validate(report_payload)


@pytest.mark.parametrize("bad", BAD_FLOATS)
def test_union_typed_mapping_values_reject_illegal_numbers(bad: float) -> None:
    """`params` 的值是 `str | int | float | bool` 联合类型，非法浮点不得靠联合逃逸。"""
    with pytest.raises(ValidationError):
        factories.repro_tuple(params={"lookback": bad})
    with pytest.raises(ValidationError):
        factories.repro_tuple(param_search_space={"lookback": (1.0, bad)})


@pytest.mark.parametrize("literal", BAD_JSON_LITERALS)
def test_json_text_input_rejects_illegal_numbers(literal: str) -> None:
    """JSON 文本入口（`model_validate_json`）与 Python 入口一样拒绝（矩阵 14）。

    三种形状各取一处：标量字段、tuple 元素、映射值。
    """
    gate = f'{{"gate_id":"G0","metric":"m","value":{literal},"verdict":"PASS"}}'
    with pytest.raises(ValidationError):
        GateResult.model_validate_json(gate)

    stress = json.loads(factories.validation_profile().cost_stress.model_dump_json())
    stress_text = json.dumps(stress).replace("[2.0]", f"[{literal}]")
    with pytest.raises(ValidationError):
        CostStressParams.model_validate_json(stress_text)

    lifecycle = json.loads(factories.validation_profile().lifecycle.model_dump_json())
    lifecycle["degradation_thresholds"] = {"sharpe": 0.0}
    lifecycle_text = json.dumps(lifecycle).replace('"sharpe": 0.0', f'"sharpe": {literal}')
    with pytest.raises(ValidationError):
        LifecycleParams.model_validate_json(lifecycle_text)


@pytest.mark.parametrize("bad", BAD_FLOATS)
def test_canonical_json_still_rejects_illegal_numbers(bad: float) -> None:
    """第二道关口：`canonical_json` 继续直接报错，**不得**产出 `null`（矩阵 15）。

    校验阶段与序列化 / 哈希阶段是同一条规则的两道关口，不是互相替代。
    """
    for payload in ({"value": bad}, [bad], {"nested": {"deep": [bad]}}):
        with pytest.raises(ValueError):
            canonical_json(payload)


def test_illegal_numbers_never_become_null_anywhere() -> None:
    """非法数值不会被静默转成 `null` 后进入哈希：它在构造阶段就无法存在于契约中。

    合法的 `0.0` 照常渲染为数字（不是 `null`）；`null` 只用于**显式缺省**的可选字段。
    """
    with pytest.raises(ValidationError):
        _gate(value=math.nan)
    legal = _gate(value=0.0)
    rendered = canonical_json(legal.model_dump(mode="json"))
    assert '"value":0.0' in rendered
    assert '"threshold":null' in rendered  # 可选字段缺省，与非法数值无关
    assert legal.content_hash() == _gate(value=0.0).content_hash()


# ======================================================================================
# D-20.2 / D-20.3：unit interval 范围与"不写死门 ID 清单"（矩阵 16 ~ 18）
# ======================================================================================


def _significance(**overrides: object) -> SignificanceParams:
    payload: dict[str, object] = {
        "multiple_testing_method": "test-method",
        "multiple_testing_threshold": 0.5,
        "overfitting_metric": "test-metric",
        "overfitting_threshold": 0.5,
        "trial_count_scope": "family",
    }
    payload.update(overrides)
    return SignificanceParams(**payload)  # type: ignore[arg-type]


UNIT_INTERVAL_FIELDS = ("multiple_testing_threshold", "overfitting_threshold")


@pytest.mark.parametrize("field", UNIT_INTERVAL_FIELDS)
@pytest.mark.parametrize("bad", [-0.1, 1.1])
def test_unit_interval_thresholds_reject_out_of_range(field: str, bad: float) -> None:
    with pytest.raises(ValidationError):
        _significance(**{field: bad})


@pytest.mark.parametrize("field", UNIT_INTERVAL_FIELDS)
@pytest.mark.parametrize("good", [0.0, 0.5, 1.0])
def test_unit_interval_thresholds_accept_the_closed_interval(field: str, good: float) -> None:
    """端点保留是刻意的：`0` 与 `1` 是否合理属于校准判断，不属于结构约束。"""
    assert getattr(_significance(**{field: good}), field) == good


@pytest.mark.parametrize("field", UNIT_INTERVAL_FIELDS)
def test_unit_interval_bounds_are_exported_to_json_schema(field: str) -> None:
    schema = json.loads(
        (CURRENT_SCHEMA_DIR / "ValidationProfile.schema.json").read_text(encoding="utf-8")
    )
    prop = schema["$defs"]["SignificanceParams"]["properties"][field]
    assert prop["minimum"] == 0
    assert prop["maximum"] == 1


def test_contract_layer_declares_no_gate_id_catalogue() -> None:
    """D-20.3：门集合由 Profile 决定，契约里不得出现写死的门 ID 清单（矩阵 18）。

    契约源码不出现 `G0` 这类门 ID；`gate_id` 在 Schema 中是自由字符串，没有 `enum`；
    也没有任何字段要求某个门必须出现。
    """
    for directory in CONTRACT_SOURCE_DIRS:
        for path in sorted(directory.glob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            literals = [
                node.value
                for node in ast.walk(tree)
                if isinstance(node, ast.Constant) and isinstance(node.value, str)
            ]
            assert not [text for text in literals if GATE_ID_LITERAL.fullmatch(text)], (
                f"{path.relative_to(REPO)} 出现了写死的门 ID 字面量"
            )

    gate_id = json.loads(
        (CURRENT_SCHEMA_DIR / "GateResult.schema.json").read_text(encoding="utf-8")
    )["properties"]["gate_id"]
    assert gate_id["type"] == "string" and "enum" not in gate_id and "const" not in gate_id
    assert gate_id["minLength"] == 1  # 只要求非空，不要求属于某个清单

    report = json.loads(
        (CURRENT_SCHEMA_DIR / "ValidationReport.schema.json").read_text(encoding="utf-8")
    )
    assert "enum" not in json.dumps(report["properties"]["gates"])


def test_gate_result_validator_does_not_reference_specific_gates() -> None:
    """判定函数与门结构都与具体门无关：它们只看 `verdict`，不看 `gate_id` 的取值。"""
    for source in (inspect.getsource(GateResult), inspect.getsource(derive_verdict)):
        literals = re.findall(r'"([^"\n]*)"', source)
        assert not [text for text in literals if GATE_ID_LITERAL.fullmatch(text)]


# ======================================================================================
# 矩阵 19：v1 只读路径不受本批次影响
# ======================================================================================


def test_v1_snapshot_did_not_inherit_the_new_bounds() -> None:
    """v1 快照是历史事实，不随 v2 收紧而变化（逐字节不变由 Git 与既有测试共同保证）。"""
    legacy = json.loads(
        (LEGACY_SCHEMA_DIR / "ValidationProfile.schema.json").read_text(encoding="utf-8")
    )
    for field in UNIT_INTERVAL_FIELDS:
        prop = legacy["$defs"]["SignificanceParams"]["properties"][field]
        assert "minimum" not in prop and "maximum" not in prop
    assert legacy["properties"]["schema_version"]["default"] == "1.0.0"


def test_v1_gate_result_snapshot_keeps_its_own_shape() -> None:
    """v1 的 `GateResult` 快照仍是 1.0.0，且不带任何本批次新增的约束。"""
    legacy = json.loads((LEGACY_SCHEMA_DIR / "GateResult.schema.json").read_text(encoding="utf-8"))
    assert legacy["properties"]["schema_version"]["default"] == "1.0.0"
    assert "enum" not in legacy["properties"]["gate_id"]

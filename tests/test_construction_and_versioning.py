"""复制更新、规范版本语法、v1 shape gate 与 Schema 格式表达（ADR-0010）。

对应 D-13 ~ D-16。本文件只验证**公开支持的**入口：
`model_construct` 是 Pydantic 面向可信数据的低层逃生口，不是受支持的外部载荷入口，
因此**不**在此断言它安全（ADR-0010 §D-13）。
"""

from __future__ import annotations

import json
from collections.abc import Mapping, MutableMapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from core.compat.v1 import V1_MODEL_NAMES, read_v1
from core.domain.base import (
    CONTRACT_SCHEMA_VERSION,
    SEMVER_PATTERN,
    Contract,
    FrozenMapping,
    Kind,
    Ref,
)
from core.domain.research import ReproducibilityTuple
from core.errors import ContractViolation
from core.lifecycle.strategy import LifecycleState, LifecycleTransition
from tests import factories
from tests.test_payload_immutability import FIELD_IDS, MAPPING_FIELDS

REPO = Path(__file__).resolve().parents[1]
VECTOR_DIR = REPO / "tests" / "vectors" / "v1"
LEGACY_SCHEMA_DIR = REPO / "schemas" / "v1"
T0 = datetime(2024, 1, 1, tzinfo=UTC)
SHA = "a" * 64


# ======================================================================================
# D-13：model_copy(update=...) 必须重新走完整校验
# ======================================================================================


@pytest.mark.parametrize(
    ("label", "builder", "field", "value", "_has_default"), MAPPING_FIELDS, ids=FIELD_IDS
)
def test_model_copy_update_keeps_mappings_read_only_and_unaliased(
    label: str,
    builder: Any,
    field: str,
    value: Mapping[str, Any],
    _has_default: bool,
) -> None:
    """复制更新不得产生可写 dict，也不得与调用方保留的原引用共享状态。"""
    original = builder(**{field: dict(value)})
    raw = dict(value)
    copied = original.model_copy(update={field: raw})

    obtained = getattr(copied, field)
    assert isinstance(obtained, FrozenMapping), f"{label} 经 model_copy 后不是只读映射"
    assert not isinstance(obtained, MutableMapping)
    assert type(copied) is type(original), "复制更新必须返回同一具体模型类型"

    raw["injected"] = "x"
    assert "injected" not in obtained, f"{label} 与 model_copy 的输入共享状态"


def test_model_copy_update_rejects_invalid_kind() -> None:
    repro = factories.repro_tuple()
    with pytest.raises(ValidationError):
        repro.model_copy(
            update={"strategy_ref": Ref(kind=Kind.FEATURE, name="f_x", version="1.0.0")}
        )
    run = factories.experiment_run()
    with pytest.raises(ValidationError):
        run.model_copy(update={"experiment": Ref(kind=Kind.FEATURE, name="f_x", version="1.0.0")})


def test_model_copy_update_rejects_missing_dependency_binding() -> None:
    repro = factories.repro_tuple()
    other = factories.strategy_ref("s_unbound")
    with pytest.raises(ValidationError):
        repro.model_copy(update={"strategy_ref": other})
    with pytest.raises(ValidationError):
        repro.model_copy(update={"dependency_hashes": {}})


def test_model_copy_update_rejects_empty_run_id() -> None:
    for model in (factories.experiment_run(), factories.validation_report()):
        with pytest.raises(ValidationError):
            model.model_copy(update={"run_id": ""})


def test_model_copy_update_rejects_bad_version_and_unknown_field() -> None:
    ref = factories.strategy_ref()
    with pytest.raises(ValidationError):
        ref.model_copy(update={"version": "1.0"})
    with pytest.raises(ValidationError):
        ref.model_copy(update={"not_a_field": 1})


def test_model_copy_without_update_keeps_pydantic_behaviour() -> None:
    repro = factories.repro_tuple()
    assert repro.model_copy() == repro
    assert repro.model_copy(deep=True) == repro
    assert repro.model_copy(deep=True).experiment_hash == repro.experiment_hash


def test_created_at_copy_update_still_works() -> None:
    """tests/test_contracts.py 依赖的既有用法不得被破坏。"""
    spec = factories.experiment_spec()
    later = spec.model_copy(update={"created_at": datetime(2030, 1, 1, tzinfo=UTC)})
    assert later.created_at.year == 2030
    assert later.content_hash() == spec.content_hash()


def test_lifecycle_history_append_still_works() -> None:
    """`LifecycleHistory.append` 内部使用 model_copy(update=...)。"""
    subject = factories.strategy_ref()
    history = factories.lifecycle_history(subject=subject)
    moved = history.append(
        LifecycleTransition(
            subject=subject,
            from_state=LifecycleState.IDEA,
            to_state=LifecycleState.CANDIDATE,
            reason="ok",
            evidence=("test-evidence:pre-registration",),
            triggered_by="test",
        )
    )
    assert moved.current_state is LifecycleState.CANDIDATE
    assert history.current_state is LifecycleState.IDEA


def test_model_construct_is_documented_as_untrusted_escape_hatch() -> None:
    """不把 `model_construct` 伪装成安全校验入口（ADR-0010 §D-13）。"""
    adr = REPO / "docs" / "adr" / "0010-contract-construction-and-canonical-versioning.md"
    text = adr.read_text(encoding="utf-8")
    assert "model_construct" in text
    assert "不是受支持的外部载荷入口" in text


# ======================================================================================
# D-14：唯一、ASCII、完整 SemVer 2.0.0 语法
# ======================================================================================

VALID_VERSIONS = [
    "0.0.0",
    "1.0.0",
    "10.20.30",
    "1.0.0-alpha",
    "1.0.0-alpha.1",
    "1.0.0-0.3.7",
    "1.0.0-x.7.z.92",
    "1.0.0-alpha+001",
    "1.0.0+20130313144700",
    "1.0.0+exp.sha.5114f85",
    "1.0.0-rc.1+build.1",
]

INVALID_VERSIONS = [
    "1",
    "1.0",
    "1.0.0.0",
    "01.0.0",
    "1.01.0",
    "1.0.01",
    "1.0.0-01",
    "1.0.0-",
    "1.0.0-alpha..1",
    "1.0.0+",
    "1.0.0+exp..1",
    "v1.0.0",
    "١.٠.٠",
    "1.0.٠",
    "1.0.0-alpha_beta",
    "",
]


@pytest.mark.parametrize("version", VALID_VERSIONS)
def test_canonical_semver_accepts_valid_versions(version: str) -> None:
    assert Ref(kind=Kind.STRATEGY, name="s_x", version=version).version == version
    spec = factories.experiment_spec(version=version)
    assert spec.version == version


@pytest.mark.parametrize("version", INVALID_VERSIONS)
def test_canonical_semver_rejects_invalid_versions(version: str) -> None:
    with pytest.raises(ValidationError):
        Ref(kind=Kind.STRATEGY, name="s_x", version=version)
    with pytest.raises(ValidationError):
        factories.experiment_spec(version=version)


@pytest.mark.parametrize("version", ["2.0.0", "2.1.0", "2.0.0-rc.1", "2.0.0+build.1"])
def test_schema_version_accepts_valid_same_major(version: str) -> None:
    assert (
        Ref(kind=Kind.STRATEGY, name="s_x", version="1.0.0", schema_version=version).schema_version
        == version
    )


@pytest.mark.parametrize("version", ["02.0.0", "2.0", "٢.0.0", "2.0.0-", "1.0.0", "3.0.0"])
def test_schema_version_rejects_invalid_or_foreign_major(version: str) -> None:
    with pytest.raises(ValidationError):
        Ref(kind=Kind.STRATEGY, name="s_x", version="1.0.0", schema_version=version)


@pytest.mark.parametrize(
    "bad",
    [
        "backtest_ref@1.0",
        "backtest_ref@01.0.0",
        "backtest_ref@١.٠.٠",
        "backtest_ref@1.0.0-",
        "backtest_ref@1.0.0+",
    ],
)
def test_plugin_key_uses_the_same_canonical_semver(bad: str) -> None:
    with pytest.raises(ValidationError):
        factories.repro_tuple(plugin_versions={bad: SHA})


def test_plugin_key_accepts_build_metadata() -> None:
    tuple_ = factories.repro_tuple(plugin_versions={"backtest_ref@1.0.0+build.1": SHA})
    assert "backtest_ref@1.0.0+build.1" in tuple_.plugin_versions


@pytest.mark.parametrize(
    "bad", ["strategy:s_example@01.0.0", "strategy:s_example@1.0", "strategy:s_example@١.٠.٠"]
)
def test_dependency_key_uses_the_same_canonical_semver(bad: str) -> None:
    full = factories.repro_tuple()
    with pytest.raises(ValidationError):
        factories.repro_tuple(dependency_hashes={**full.dependency_hashes, bad: SHA})


def test_surrounding_whitespace_is_stripped_before_validation_not_accepted_raw() -> None:
    """契约的 `str_strip_whitespace=True` 会先剥离首尾空白，剥离后仍须是规范版本。

    解析层本身不接受带空白的版本串。
    """
    from core.domain.base import parse_semver

    for raw in ("1.0.0 ", " 1.0.0", "1.0.0\n"):
        with pytest.raises(ValueError):
            parse_semver(raw)
    assert Ref(kind=Kind.STRATEGY, name="s_x", version=" 1.0.0 ").version == "1.0.0"
    with pytest.raises(ValidationError):
        Ref(kind=Kind.STRATEGY, name="s_x", version=" 1.0 ")


def test_semver_pattern_is_ascii_only_and_has_no_leading_zeros() -> None:
    """正则本身必须是唯一来源，且显式禁止 Unicode 数字与前导零。"""
    assert "0-9" in SEMVER_PATTERN, "必须用显式 ASCII 字符类，不能用会匹配 Unicode 的 \\d"
    assert "\\d" not in SEMVER_PATTERN
    # ADR-0052 §4 raised the minor to 2.1.0, ADR-0055 to 2.2.0, ADR-0077 to 2.3.0 and ADR-0088
    # to 2.4.0.
    assert CONTRACT_SCHEMA_VERSION == "2.4.0"


def test_major_is_read_from_the_validated_regex_group() -> None:
    """major 不得用宽松的 int(split()) 读取（ADR-0010 §D-14）。"""
    source = (REPO / "core" / "domain" / "base.py").read_text(encoding="utf-8")
    assert 'int(value.split(".", 1)[0])' not in source


# ======================================================================================
# D-15：v1 只读入口的顶层 shape gate
# ======================================================================================


def _vector(slug: str) -> dict[str, Any]:
    record: dict[str, Any] = json.loads((VECTOR_DIR / f"{slug}.json").read_text(encoding="utf-8"))
    return record


VECTOR_SLUGS = (
    "strategy_spec",
    "reproducibility_tuple",
    "experiment_spec",
    "validation_profile_draft",
    "validation_profile_frozen",
)


@pytest.mark.parametrize("slug", VECTOR_SLUGS)
def test_v1_gate_still_accepts_the_committed_vectors(slug: str) -> None:
    record = _vector(slug)
    legacy = read_v1(record["payload"], model=record["model"])
    assert legacy.content_hash == record["legacy_content_hash"], "固定旧哈希必须保持不变"


def test_v1_gate_rejects_an_arbitrary_dict() -> None:
    for payload in ({}, {"schema_version": "1.0.0"}, {"schema_version": "1.0.0", "x": 1}):
        with pytest.raises(ContractViolation):
            read_v1(payload, model="StrategySpec")


def test_v1_gate_rejects_the_wrong_model_for_a_real_vector() -> None:
    """用真实向量做反例：StrategySpec 载荷不得被当作 ValidationProfile 读取。"""
    payload = _vector("strategy_spec")["payload"]
    with pytest.raises(ContractViolation):
        read_v1(payload, model="ValidationProfile")
    with pytest.raises(ContractViolation):
        read_v1(_vector("reproducibility_tuple")["payload"], model="ExperimentSpec")


def test_v1_gate_rejects_missing_required_and_extra_top_level_fields() -> None:
    payload = dict(_vector("strategy_spec")["payload"])
    without_name = {k: v for k, v in payload.items() if k != "name"}
    with pytest.raises(ContractViolation):
        read_v1(without_name, model="StrategySpec")
    with pytest.raises(ContractViolation):
        read_v1({**payload, "unknown_top_level": 1}, model="StrategySpec")


@pytest.mark.parametrize("version", ["1", "1.x", "1.0", "01.0.0", "1.0.0.0", "١.٠.٠", "2.0.0"])
def test_v1_gate_rejects_bad_or_foreign_schema_version(version: str) -> None:
    payload = dict(_vector("strategy_spec")["payload"])
    with pytest.raises(ContractViolation):
        read_v1({**payload, "schema_version": version}, model="StrategySpec")


@pytest.mark.parametrize("version", ["1.0.0", "1.4.0", "1.0.1", "1.2.3-rc.1"])
def test_v1_gate_accepts_legacy_same_major_versions(version: str) -> None:
    payload = dict(_vector("strategy_spec")["payload"])
    assert read_v1({**payload, "schema_version": version}, model="StrategySpec").schema_version == (
        version
    )


def test_v1_gate_rejects_a_v2_payload_relabelled_as_v1() -> None:
    """把 v2 载荷改个 schema_version 冒充 v1，必须被顶层 shape gate 拦住。"""
    v2_payload = json.loads(factories.repro_tuple().model_dump_json())
    v2_payload["schema_version"] = "1.0.0"
    with pytest.raises(ContractViolation):
        read_v1(v2_payload, model="ReproducibilityTuple")


def test_v1_gate_fails_closed_when_the_snapshot_is_missing() -> None:
    from core.compat import v1 as compat

    with pytest.raises(ContractViolation):
        compat.read_v1(
            _vector("strategy_spec")["payload"],
            model="StrategySpec",
            snapshot_dir=REPO / "schemas" / "does-not-exist",
        )


def test_v1_gate_documents_that_it_is_not_full_json_schema_validation() -> None:
    text = (REPO / "core" / "compat" / "v1.py").read_text(encoding="utf-8")
    assert "不是完整" in text and "递归" in text
    assert set(V1_MODEL_NAMES) == {
        p.name.removesuffix(".schema.json") for p in LEGACY_SCHEMA_DIR.glob("*.schema.json")
    }


# ======================================================================================
# D-16：current Schema 必须表达运行时已执行的格式约束
# ======================================================================================


def _key_value_constraints(schema: Mapping[str, Any], field: str) -> tuple[str, str]:
    """从 `patternProperties` 读出键 / 值的可机读 pattern（ADR-0010 §D-16 允许的等价形式）。"""
    prop = schema["properties"][field]
    assert prop["type"] == "object"
    pattern_properties = prop.get("patternProperties")
    assert pattern_properties, f"{field} 缺少可机读的键约束"
    assert len(pattern_properties) == 1, f"{field} 的键约束必须唯一"
    assert prop.get("additionalProperties") is False, f"{field} 必须禁止不匹配的键"
    key_pattern, value_schema = next(iter(pattern_properties.items()))
    value_pattern = value_schema.get("pattern")
    assert value_pattern, f"{field} 的值缺少可机读 pattern"
    return key_pattern, value_pattern


def test_schema_expresses_dependency_and_plugin_key_value_patterns() -> None:
    repro = json.loads((REPO / "schemas" / "ReproducibilityTuple.schema.json").read_text("utf-8"))
    artifact = json.loads((REPO / "schemas" / "StrategyArtifact.schema.json").read_text("utf-8"))

    for schema, field in (
        (repro, "dependency_hashes"),
        (repro, "plugin_versions"),
        (artifact, "dependencies"),
    ):
        key_pattern, value_pattern = _key_value_constraints(schema, field)
        assert value_pattern == "^[0-9a-f]{64}$", f"{field} 的值必须是 64 位小写 SHA-256"
        assert "0-9" in key_pattern


def test_schema_patterns_come_from_the_same_source_as_runtime() -> None:
    """运行时与 Schema 必须同源，避免两套规则漂移。"""
    from core.domain import base

    repro = json.loads((REPO / "schemas" / "ReproducibilityTuple.schema.json").read_text("utf-8"))
    artifact = json.loads((REPO / "schemas" / "StrategyArtifact.schema.json").read_text("utf-8"))

    assert _key_value_constraints(repro, "plugin_versions")[0] == base.PLUGIN_KEY_PATTERN
    assert _key_value_constraints(repro, "dependency_hashes")[0] == base.REF_KEY_PATTERN
    assert _key_value_constraints(artifact, "dependencies")[0] == base.REF_KEY_PATTERN
    assert _key_value_constraints(repro, "plugin_versions")[1] == base.SHA256_PATTERN


@pytest.mark.parametrize(
    ("model", "field"),
    [
        (ReproducibilityTuple, "dependency_hashes"),
        (ReproducibilityTuple, "plugin_versions"),
    ],
)
def test_schema_patterns_match_runtime_acceptance(model: type[Contract], field: str) -> None:
    """Schema 里声明的键 pattern 必须与运行时实际接受 / 拒绝的集合一致。"""
    import re as _re

    schema = model.model_json_schema(mode="serialization")
    key_pattern, _value = _key_value_constraints(schema, field)
    compiled = _re.compile(key_pattern)

    good = "backtest_ref@1.0.0" if field == "plugin_versions" else "strategy:s_example@1.0.0"
    assert compiled.match(good)
    for bad in ("backtest_ref@01.0.0", "strategy:s_example@1.0", "١.٠.٠"):
        assert not compiled.match(bad), f"{key_pattern} 不应接受 {bad}"


# ======================================================================================
# 文档边界（Codex 裁决第 5 条）
# ======================================================================================


def test_docs_record_frozen_profile_obligation_and_minor_boundary() -> None:
    experiment = (REPO / "docs" / "architecture" / "06-experiment.md").read_text(encoding="utf-8")
    assert "frozen" in experiment and "ValidationProfile" in experiment

    domain = (REPO / "docs" / "architecture" / "02-domain.md").read_text(encoding="utf-8")
    assert "未知字段" in domain, "必须写清同 major 更高 minor 的 fail-closed 边界"


def test_unknown_fields_still_fail_closed_for_higher_minor() -> None:
    """版本号可识别 ≠ 前向兼容：未知字段仍然拒绝（mypy 也会静态拒绝该关键字）。"""
    payload = {"schema_version": "2.9.0", "kind": "strategy", "name": "s_x", "version": "1.0.0"}
    assert Ref.model_validate(payload).schema_version == "2.9.0"
    with pytest.raises(ValidationError):
        Ref.model_validate({**payload, "brand_new_field": 1})


def test_compat_package_is_covered_by_architecture_boundary_tests() -> None:
    source = (REPO / "tests" / "test_architecture_boundaries.py").read_text(encoding="utf-8")
    assert "core/compat" in source


def test_remaining_b3_gaps_are_still_listed() -> None:
    status = (REPO / "PROJECT_STATUS.md").read_text(encoding="utf-8")
    assert "LlmCall" in status
    assert "传递依赖" in status or "传递闭包" in status

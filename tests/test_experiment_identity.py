"""实验规格身份、运行标识与依赖内容绑定（ADR-0009），以及契约 2.0.0 的发布验收。

覆盖 ADR-0009 验收矩阵全部 13 项、v1 只读兼容边界、Schema current/legacy 分离。

**不在本轮实现**（ADR-0009 §6，仅在文档中写下义务）：传递依赖闭包解析、
`params` 默认值展开完整性、trial 权威账本、Registry 存在性校验与
`run.repro` ↔ Spec 一致性校验。本文件不得假装已经验证这些。
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from core.compat.v1 import (
    V1_MODEL_NAMES,
    V1_NON_SEMANTIC_FIELDS,
    V1_SCHEMA_MAJOR,
    LegacyV1Record,
    read_v1,
)
from core.contracts.profile_selection import ExperimentMetadata, SelectionEntry
from core.contracts.registry import CONTRACT_MODELS, export_json_schemas
from core.domain.artifact import StrategyArtifact
from core.domain.base import (
    CONTRACT_SCHEMA_MAJOR,
    CONTRACT_SCHEMA_VERSION,
    Contract,
    Kind,
    Ref,
)
from core.domain.research import (
    ExperimentRun,
    ExperimentSpec,
    ReproducibilityTuple,
    ValidationReport,
    Verdict,
)
from core.domain.selection import ProfileSelection, ProfileSelectionKey
from core.errors import ContractViolation
from tests import factories

REPO = Path(__file__).resolve().parents[1]
VECTOR_DIR = REPO / "tests" / "vectors" / "v1"
CURRENT_SCHEMA_DIR = REPO / "schemas"
LEGACY_SCHEMA_DIR = CURRENT_SCHEMA_DIR / "v1"

T0 = datetime(2024, 1, 1, tzinfo=UTC)
SHA = "a" * 64
OTHER_SHA = "9" * 64


# ======================================================================================
# 矩阵 1：策略 / 风控 / Outcome 引用与其直接内容绑定进入实验身份
# ======================================================================================


@pytest.mark.parametrize(
    ("field", "changed"),
    [
        ("strategy_ref", factories.strategy_ref("s_other")),
        ("risk_policy_ref", factories.risk_ref("r_other")),
        ("outcome_ref", factories.outcome_ref("o_other")),
    ],
)
def test_changing_a_direct_reference_changes_experiment_hash(field: str, changed: Ref) -> None:
    """v1 的哈希碰撞（引用不同策略却同 hash）在 v2 不再可能。"""
    base = factories.repro_tuple()
    other = factories.repro_tuple(**{field: changed})
    assert base.experiment_hash != other.experiment_hash


@pytest.mark.parametrize(
    "ref_factory",
    [factories.hypothesis_ref, factories.strategy_ref, factories.risk_ref, factories.outcome_ref],
    ids=["hypothesis", "strategy", "risk", "outcome"],
)
def test_changing_a_direct_content_binding_changes_experiment_hash(ref_factory: Any) -> None:
    """引用不变、被引用对象的内容哈希变化，实验身份同样必须变化。"""
    base = factories.repro_tuple()
    mutated = dict(base.dependency_hashes)
    mutated[str(ref_factory())] = OTHER_SHA
    other = factories.repro_tuple(dependency_hashes=mutated)
    assert base.experiment_hash != other.experiment_hash


def test_nulling_out_an_optional_reference_changes_experiment_hash() -> None:
    base = factories.repro_tuple()
    without_risk = factories.repro_tuple(risk_policy_ref=None)
    assert base.experiment_hash != without_risk.experiment_hash
    assert without_risk.risk_policy_ref is None


# ======================================================================================
# 矩阵 2 / 3：相同完整规格同哈希；仅 seeds 不同则是新的规格变体
# ======================================================================================


def test_identical_specifications_share_one_experiment_hash() -> None:
    """相同完整规格（含相同 seeds）重复运行 → 同一 experiment_hash（复现检查）。"""
    first = factories.repro_tuple(seeds=(7, 11))
    second = factories.repro_tuple(seeds=(7, 11))
    assert first is not second
    assert first.experiment_hash == second.experiment_hash


def test_different_seeds_produce_a_different_specification_variant() -> None:
    """ADR-0009 §2：实际 seeds 留在哈希内；不同 seeds = 新的不可变规格变体。"""
    first = factories.repro_tuple(seeds=(7,))
    second = factories.repro_tuple(seeds=(8,))
    assert first.experiment_hash != second.experiment_hash
    # 族内关联由 hypothesis_family_id 与 trial 计数承担，而不是靠哈希相等
    assert first.hypothesis_ref == second.hypothesis_ref


def test_seed_order_is_semantic_not_incidental() -> None:
    assert factories.repro_tuple(seeds=(7, 8)).experiment_hash != (
        factories.repro_tuple(seeds=(8, 7)).experiment_hash
    )


# ======================================================================================
# 矩阵 4：kind 校验
# ======================================================================================


@pytest.mark.parametrize(
    ("field", "bad"),
    [
        ("hypothesis_ref", Ref(kind=Kind.FEATURE, name="f_x", version="1.0.0")),
        ("strategy_ref", Ref(kind=Kind.FEATURE, name="f_x", version="1.0.0")),
        ("risk_policy_ref", Ref(kind=Kind.STRATEGY, name="s_x", version="1.0.0")),
        ("outcome_ref", Ref(kind=Kind.FEATURE, name="f_x", version="1.0.0")),
        ("cost_model_ref", Ref(kind=Kind.FEATURE, name="f_x", version="1.0.0")),
    ],
)
def test_reproducibility_tuple_rejects_wrong_reference_kind(field: str, bad: Ref) -> None:
    with pytest.raises(ValidationError):
        factories.repro_tuple(**{field: bad})


def test_experiment_run_requires_experiment_kind() -> None:
    with pytest.raises(ValidationError):
        factories.experiment_run(experiment=Ref(kind=Kind.FEATURE, name="f_x", version="1.0.0"))
    assert factories.experiment_run().experiment.kind is Kind.EXPERIMENT


def test_profile_selection_requires_rule_kind() -> None:
    with pytest.raises(ValidationError):
        factories.profile_selection(
            selection_rule=Ref(kind=Kind.PROFILE, name="test_rule", version="1.0.0")
        )


# ======================================================================================
# 矩阵 5：dependency_hashes 的直接覆盖规则
# ======================================================================================


@pytest.mark.parametrize(
    "omitted",
    ["hypothesis_ref", "strategy_ref", "risk_policy_ref", "outcome_ref", "cost_model_ref"],
)
def test_dependency_hashes_must_cover_every_non_null_reference(omitted: str) -> None:
    full = factories.repro_tuple()
    dropped = str(getattr(full, omitted))
    partial = {k: v for k, v in full.dependency_hashes.items() if k != dropped}
    with pytest.raises(ValidationError):
        factories.repro_tuple(dependency_hashes=partial)


def test_dependency_hashes_is_required_and_has_no_empty_default() -> None:
    with pytest.raises(ValidationError):
        factories.repro_tuple(dependency_hashes={})


def test_null_reference_needs_no_binding() -> None:
    """不适用时明确填 null；null 引用不要求（也不应有）内容绑定。"""
    full = factories.repro_tuple()
    outcome_key = str(full.outcome_ref)
    without_outcome = {k: v for k, v in full.dependency_hashes.items() if k != outcome_key}
    tuple_ = factories.repro_tuple(outcome_ref=None, dependency_hashes=without_outcome)
    assert tuple_.outcome_ref is None


@pytest.mark.parametrize(
    "bad_key",
    ["s_example@1.0.0", "strategy:s_example", "STRATEGY:s_example@1.0.0", "nope:s_example@1.0.0"],
)
def test_dependency_hashes_rejects_non_canonical_ref_keys(bad_key: str) -> None:
    full = factories.repro_tuple()
    with pytest.raises(ValidationError):
        factories.repro_tuple(dependency_hashes={**full.dependency_hashes, bad_key: SHA})


@pytest.mark.parametrize("bad_value", ["", "nope", "A" * 64, "a" * 63, "a" * 65])
def test_dependency_hashes_requires_lowercase_sha256_values(bad_value: str) -> None:
    full = factories.repro_tuple()
    broken = dict(full.dependency_hashes)
    broken[str(full.strategy_ref)] = bad_value
    with pytest.raises(ValidationError):
        factories.repro_tuple(dependency_hashes=broken)


# ======================================================================================
# 矩阵 6：plugin_versions 的键值格式
# ======================================================================================


@pytest.mark.parametrize(
    "bad_key",
    ["backtest_ref", "backtest_ref@1.0", "Backtest@1.0.0", "backtest ref@1.0.0", "plugin:x@1.0.0"],
)
def test_plugin_versions_rejects_bad_keys(bad_key: str) -> None:
    with pytest.raises(ValidationError):
        factories.repro_tuple(plugin_versions={bad_key: SHA})


def test_plugin_versions_rejects_non_sha256_values() -> None:
    with pytest.raises(ValidationError):
        factories.repro_tuple(plugin_versions={"backtest_ref@1.0.0": "not-a-hash"})


def test_plugin_versions_accepts_name_at_semver_and_stays_a_json_object() -> None:
    tuple_ = factories.repro_tuple(plugin_versions={"backtest_ref@1.0.0-rc.1": SHA})
    wire = json.loads(tuple_.model_dump_json())
    assert isinstance(wire["plugin_versions"], dict)
    assert wire["plugin_versions"] == {"backtest_ref@1.0.0-rc.1": SHA}


# ======================================================================================
# 矩阵 7：profile_selection 必填且含唯一规则引用 + 规则内容哈希
# ======================================================================================


def test_profile_selection_is_required_without_empty_default() -> None:
    payload = factories.repro_tuple().model_dump()
    payload.pop("profile_selection")
    with pytest.raises(ValidationError):
        ReproducibilityTuple(**payload)


@pytest.mark.parametrize("missing", ["selection_rule", "selection_rule_hash", "key"])
def test_profile_selection_requires_all_three_parts(missing: str) -> None:
    payload = factories.profile_selection().model_dump()
    payload.pop(missing)
    with pytest.raises(ValidationError):
        ProfileSelection(**payload)


def test_profile_selection_rule_hash_must_be_sha256() -> None:
    with pytest.raises(ValidationError):
        factories.profile_selection(selection_rule_hash="rule-hash")


def test_changing_selection_rule_hash_changes_experiment_hash() -> None:
    base = factories.repro_tuple()
    other = factories.repro_tuple(
        profile_selection=factories.profile_selection(selection_rule_hash=OTHER_SHA)
    )
    assert base.experiment_hash != other.experiment_hash


def test_shared_selection_value_objects_avoid_circular_import() -> None:
    """共享值对象定义在 domain 层，contracts 层重导出；Schema 名称保持稳定。"""
    from core.contracts import profile_selection as contracts_module
    from core.domain import selection as domain_module

    assert contracts_module.ProfileSelectionKey is domain_module.ProfileSelectionKey
    assert contracts_module.ProfileSelection is domain_module.ProfileSelection
    assert ProfileSelectionKey.__name__ == "ProfileSelectionKey"
    assert ProfileSelection.__name__ == "ProfileSelection"


# ======================================================================================
# 矩阵 8 / 11：ValidationReport ↔ Run 绑定，且 ID 不被全局排除吞掉
# ======================================================================================


def test_validation_report_requires_run_id() -> None:
    payload = factories.validation_report().model_dump()
    payload.pop("run_id")
    with pytest.raises(ValidationError):
        ValidationReport(**payload)


def test_report_binds_both_run_and_experiment_hash() -> None:
    repro = factories.repro_tuple()
    run = factories.experiment_run(repro=repro)
    report = factories.validation_report(run_id=run.run_id, experiment_hash=run.experiment_hash)
    assert report.run_id == run.run_id
    assert report.experiment_hash == repro.experiment_hash


def test_run_id_and_report_id_are_not_excluded_from_content_hash() -> None:
    """ADR-0009 §4：不新增通用 `*_id` 排除规则。"""
    for model in (ValidationReport, ExperimentRun):
        excluded = model._non_semantic_fields()
        assert "run_id" not in excluded
        assert "report_id" not in excluded
        assert excluded == {"created_at"}

    base = factories.validation_report()
    assert base.content_hash() != factories.validation_report(run_id="run-2").content_hash()
    assert base.content_hash() != factories.validation_report(report_id="rep-2").content_hash()

    run = factories.experiment_run()
    assert run.content_hash() != factories.experiment_run(run_id="run-2").content_hash()


def test_run_id_is_an_opaque_per_attempt_string() -> None:
    """契约不冻结 run_id 的生成算法，只要求每次尝试唯一、非空、不透明。"""
    repro = factories.repro_tuple()
    first = factories.experiment_run(run_id="whatever-shape-1", repro=repro)
    second = factories.experiment_run(run_id="d41d8cd98f00", repro=repro)
    assert first.run_id != second.run_id
    assert first.experiment_hash == second.experiment_hash  # 同一规格的两次尝试
    with pytest.raises(ValidationError):
        factories.experiment_run(run_id="")


# ======================================================================================
# 矩阵 9：SelectionEntry 的 profile 一致性
# ======================================================================================


def test_selection_entry_has_a_single_version_source() -> None:
    """ADR-0015 §D-22.4 起版本只有一处表达：重复的 `profile_version` 字段已删除。

    ADR-0009 §矩阵 9 原先要求"两个副本必须一致"，现在该不一致**在结构上不可表达**，
    这比事后校验更强；旧字段作为多余字段被 `extra="forbid"` 拒绝。
    """
    entry = SelectionEntry(
        key=factories.selection_key(),
        profile=Ref(kind=Kind.PROFILE, name="test_scope", version="1.0.0"),
    )
    assert entry.profile.version == "1.0.0"
    assert "profile_version" not in SelectionEntry.model_fields
    with pytest.raises(ValidationError):
        SelectionEntry(
            key=factories.selection_key(),
            profile=Ref(kind=Kind.PROFILE, name="test_scope", version="1.0.0"),
            profile_version="2.0.0",  # type: ignore[call-arg]
        )


def test_selection_entry_rejects_non_profile_kind() -> None:
    with pytest.raises(ValidationError):
        SelectionEntry(
            key=factories.selection_key(),
            profile=Ref(kind=Kind.FEATURE, name="test_scope", version="1.0.0"),
        )


def test_selection_rule_still_selects_deterministically() -> None:
    rule = factories.selection_rule()
    assert rule.select(factories.selection_key()).profile.version == "1.0.0"


# ======================================================================================
# 矩阵 10 / 13：JSON 往返与"只改一项就换身份"
# ======================================================================================


def test_experiment_hash_survives_json_round_trip_bitwise() -> None:
    tuple_ = factories.repro_tuple(seeds=(7, 11))
    restored = ReproducibilityTuple.model_validate_json(tuple_.model_dump_json())
    assert restored.experiment_hash == tuple_.experiment_hash
    assert restored == tuple_


def _variants() -> dict[str, ReproducibilityTuple]:
    other_dataset = factories.dataset_ref().model_copy(update={"snapshot_id": "snap-2"})
    full = factories.repro_tuple()
    changed_plugin = factories.repro_tuple(plugin_versions={"backtest_ref@1.0.0": OTHER_SHA})
    return {
        "params": factories.repro_tuple(params={"window": 48}),
        "param_search_space": factories.repro_tuple(param_search_space={"window": (12, 48)}),
        "dataset_snapshot": factories.repro_tuple(dataset_snapshots=(other_dataset,)),
        "cost_model_ref": factories.repro_tuple(
            cost_model_ref=factories.cost_model_ref("cost_v2"),
            dependency_hashes=factories.dependency_hashes(
                factories.hypothesis_ref(),
                factories.strategy_ref(),
                factories.risk_ref(),
                factories.outcome_ref(),
                factories.cost_model_ref("cost_v2"),
            ),
        ),
        "validation_profile_hash": factories.repro_tuple(validation_profile_hash=OTHER_SHA),
        "validation_profile_ref": factories.repro_tuple(
            validation_profile=factories.profile_ref(version="2.0.0")
        ),
        "selection_rule_hash": factories.repro_tuple(
            profile_selection=factories.profile_selection(selection_rule_hash=OTHER_SHA)
        ),
        "selection_rule_ref": factories.repro_tuple(
            profile_selection=factories.profile_selection(
                selection_rule=factories.selection_rule_ref("2.0.0")
            )
        ),
        "plugin_hash": changed_plugin,
        "seeds": factories.repro_tuple(seeds=(1,)),
        "code_commit": factories.repro_tuple(code_commit=factories.OTHER_GIT_COMMIT_OID),
        "split_spec": factories.repro_tuple(split_spec="train/oos"),
        "environment_lock": factories.repro_tuple(environment_lock="other-lock"),
        "constitution_version": factories.repro_tuple(constitution_version="1.0.0"),
        "_base": full,
    }


@pytest.mark.parametrize("variant", sorted(set(_variants()) - {"_base"}))
def test_every_semantic_change_changes_experiment_hash(variant: str) -> None:
    variants = _variants()
    assert variants[variant].experiment_hash != variants["_base"].experiment_hash


def test_all_variants_have_pairwise_distinct_hashes() -> None:
    variants = _variants()
    hashes = {name: value.experiment_hash for name, value in variants.items()}
    assert len(set(hashes.values())) == len(hashes), "存在哈希碰撞的语义变化"


def test_mapping_insertion_order_does_not_change_experiment_hash() -> None:
    full = factories.repro_tuple()
    reversed_deps = dict(reversed(list(full.dependency_hashes.items())))
    assert list(reversed_deps) != list(full.dependency_hashes)
    assert factories.repro_tuple(dependency_hashes=reversed_deps).experiment_hash == (
        full.experiment_hash
    )


# ======================================================================================
# ExperimentSpec：派生只读引用，不重复存放
# ======================================================================================


def test_experiment_spec_derives_references_from_repro() -> None:
    repro = factories.repro_tuple()
    spec = factories.experiment_spec(repro=repro)
    assert spec.strategy == repro.strategy_ref
    assert spec.risk_policy == repro.risk_policy_ref
    assert spec.outcome == repro.outcome_ref
    assert spec.experiment_hash == repro.experiment_hash


def test_experiment_spec_does_not_duplicate_derived_fields_in_json_or_schema() -> None:
    wire = json.loads(factories.experiment_spec().model_dump_json())
    properties = ExperimentSpec.model_json_schema(mode="serialization")["properties"]
    for name in ("strategy", "risk_policy", "outcome"):
        assert name not in wire, f"JSON 重复发出派生字段 {name}"
        assert name not in properties, f"Schema 重复声明派生字段 {name}"
        assert name not in ExperimentSpec.model_fields
    assert "repro" in wire


def test_experiment_spec_rejects_legacy_duplicated_fields() -> None:
    """v1 的并列字段在 v2 属于未声明字段（extra="forbid"）。"""
    with pytest.raises(ValidationError):
        factories.experiment_spec(strategy=factories.strategy_ref())


def test_derived_references_are_read_only() -> None:
    spec = factories.experiment_spec()
    with pytest.raises((AttributeError, ValidationError)):
        spec.strategy = factories.strategy_ref("s_other")  # type: ignore[misc]


# ======================================================================================
# StrategyArtifact 的依赖绑定（ADR-0009 §5）
# ======================================================================================


def test_artifact_dependencies_must_bind_the_direct_strategy_spec() -> None:
    with pytest.raises(ValidationError):
        factories.strategy_artifact(dependencies={})
    with pytest.raises(ValidationError):
        factories.strategy_artifact(
            dependencies={str(Ref(kind=Kind.FEATURE, name="f_x", version="1.0.0")): SHA}
        )


@pytest.mark.parametrize("bad_key", ["s_example@1.0.0", "s_example", "strategy:s_example"])
def test_artifact_dependencies_require_kind_prefixed_keys(bad_key: str) -> None:
    with pytest.raises(ValidationError):
        factories.strategy_artifact(dependencies={str(factories.strategy_ref()): SHA, bad_key: SHA})


def test_artifact_dependencies_require_sha256_values() -> None:
    with pytest.raises(ValidationError):
        factories.strategy_artifact(dependencies={str(factories.strategy_ref()): "nope"})


def test_artifact_dependencies_distinguish_same_name_different_kind() -> None:
    """键带 kind，Feature / Strategy 同名对象不会互相冒充。"""
    strategy = factories.strategy_ref("shared_name")
    feature = Ref(kind=Kind.FEATURE, name="shared_name", version="1.0.0")
    artifact = factories.strategy_artifact(
        strategy_spec=strategy,
        dependencies={str(strategy): SHA, str(feature): OTHER_SHA},
    )
    assert artifact.dependencies[str(strategy)] != artifact.dependencies[str(feature)]


# ======================================================================================
# 契约 2.0.0：未知 major 拒绝、同 major 更高 minor 接受
# ======================================================================================


def test_contract_schema_version_is_two_zero_zero() -> None:
    # ADR-0052 §4 raised the minor to 2.1.0 and ADR-0055 to 2.2.0; this batch changed no version.
    assert CONTRACT_SCHEMA_VERSION == "2.2.0"
    assert CONTRACT_SCHEMA_MAJOR == 2
    for model in CONTRACT_MODELS:
        assert model.model_fields["schema_version"].default == CONTRACT_SCHEMA_VERSION


@pytest.mark.parametrize("version", ["1.0.0", "0.9.0", "3.0.0", "10.0.0"])
def test_unknown_major_is_rejected(version: str) -> None:
    with pytest.raises(ValidationError):
        Ref(kind=Kind.STRATEGY, name="s_example", version="1.0.0", schema_version=version)


@pytest.mark.parametrize("version", ["2.0.0", "2.1.0", "2.3.7", "2.1.0-rc.1"])
def test_same_major_higher_minor_is_readable(version: str) -> None:
    ref = Ref(kind=Kind.STRATEGY, name="s_example", version="1.0.0", schema_version=version)
    assert ref.schema_version == version


def test_v1_payload_cannot_be_validated_as_a_v2_model() -> None:
    """旧载荷不得静默变成 v2 模型：major 不符 + v1 的并列字段已不被接受。"""
    record = _vector("experiment_spec")
    with pytest.raises(ValidationError):
        ExperimentSpec.model_validate(dict(record["payload"]))


# ======================================================================================
# v1 只读兼容入口（ADR-0008 §6、ADR-0009 §7）
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
def test_v1_reader_preserves_legacy_identity(slug: str) -> None:
    record = _vector(slug)
    legacy = read_v1(record["payload"], model=record["model"])
    assert legacy.content_hash == record["legacy_content_hash"]
    assert legacy.schema_version == "1.0.0"
    assert legacy.model == record["model"]


def test_v1_reader_exposes_legacy_experiment_hash_only_for_the_tuple() -> None:
    record = _vector("reproducibility_tuple")
    legacy = read_v1(record["payload"], model="ReproducibilityTuple")
    assert legacy.experiment_hash == record["legacy_content_hash"]
    other = read_v1(_vector("strategy_spec")["payload"], model="StrategySpec")
    with pytest.raises(ContractViolation):
        _ = other.experiment_hash


def test_v1_reader_result_is_read_only() -> None:
    legacy = read_v1(_vector("strategy_spec")["payload"], model="StrategySpec")
    with pytest.raises(TypeError):
        legacy.payload["name"] = "mutated"  # type: ignore[index]
    with pytest.raises(AttributeError):
        legacy.content_hash = "x"  # type: ignore[misc]
    with pytest.raises((TypeError, AttributeError)):
        legacy.payload["params"].update({"injected": 1})


def test_v1_reader_result_is_not_a_v2_contract() -> None:
    """类型 / 接口边界：读取结果不是 Contract，不能当作 v2 模型或登记输入使用。"""
    legacy = read_v1(_vector("experiment_spec")["payload"], model="ExperimentSpec")
    assert isinstance(legacy, LegacyV1Record)
    assert not isinstance(legacy, Contract)
    assert not hasattr(legacy, "model_dump")
    assert not hasattr(legacy, "model_validate")
    # mypy 也会把 `LegacyV1Record in CONTRACT_MODELS` 判为不重叠比较——类型层面已经隔离。
    assert LegacyV1Record.__name__ not in {m.__name__ for m in CONTRACT_MODELS}
    assert not issubclass(LegacyV1Record, Contract)


def test_legacy_experiment_lacks_v2_bindings_and_cannot_be_promoted() -> None:
    """缺 dependency_hashes / run_id / 结构化 profile_selection 的旧记录没有 v2 资格。"""
    legacy = read_v1(_vector("reproducibility_tuple")["payload"], model="ReproducibilityTuple")
    assert "dependency_hashes" not in legacy.payload
    assert "strategy_ref" not in legacy.payload
    assert isinstance(legacy.payload["profile_selection"], Mapping)  # v1 的扁平字符串映射
    with pytest.raises(ValidationError):
        ReproducibilityTuple.model_validate(dict(legacy.payload))


def test_v1_reader_rejects_unknown_major_and_unknown_model() -> None:
    payload = dict(_vector("strategy_spec")["payload"])
    for version in ("2.0.0", "0.9.0", "3.1.0"):
        with pytest.raises(ContractViolation):
            read_v1({**payload, "schema_version": version}, model="StrategySpec")
    with pytest.raises(ContractViolation):
        read_v1(payload, model="ProfileSelection")
    with pytest.raises(ContractViolation):
        read_v1({k: v for k, v in payload.items() if k != "schema_version"}, model="StrategySpec")


def test_v1_reader_accepts_higher_minor_within_major_one() -> None:
    payload = dict(_vector("strategy_spec")["payload"])
    legacy = read_v1({**payload, "schema_version": "1.4.0"}, model="StrategySpec")
    assert legacy.schema_version == "1.4.0"
    assert V1_SCHEMA_MAJOR == 1


def test_v1_reader_does_not_recompute_with_v2_rules() -> None:
    """ADR-0008 §6：不得把旧载荷按新算法重算并赋值。"""
    draft = _vector("validation_profile_draft")
    frozen = _vector("validation_profile_frozen")
    assert draft["legacy_content_hash"] != frozen["legacy_content_hash"]
    assert read_v1(draft["payload"], model="ValidationProfile").content_hash != (
        read_v1(frozen["payload"], model="ValidationProfile").content_hash
    )
    assert V1_NON_SEMANTIC_FIELDS == frozenset({"content_hash", "created_at"})


# ======================================================================================
# Schema：current 与 legacy 分离
# ======================================================================================


def test_current_schemas_match_the_registry(tmp_path: Path) -> None:
    fresh = export_json_schemas(tmp_path)
    assert len(fresh) == len(CONTRACT_MODELS)
    committed = {p.name for p in CURRENT_SCHEMA_DIR.glob("*.schema.json")}
    assert committed == {p.name for p in fresh.values()}
    for name, path in fresh.items():
        expected = json.loads(path.read_text(encoding="utf-8"))
        actual = json.loads((CURRENT_SCHEMA_DIR / f"{name}.schema.json").read_text("utf-8"))
        assert actual == expected, f"{name} 的 Schema 已过期，请重新导出"
        assert expected["properties"]["schema_version"]["default"] == "2.2.0"  # ADR-0055


def test_legacy_snapshot_is_complete_and_pinned_to_v1() -> None:
    names = {p.stem.removesuffix(".schema") for p in LEGACY_SCHEMA_DIR.glob("*.schema.json")}
    assert names == set(V1_MODEL_NAMES)
    assert len(names) == 35
    for path in LEGACY_SCHEMA_DIR.glob("*.schema.json"):
        schema = json.loads(path.read_text(encoding="utf-8"))
        assert schema["properties"]["schema_version"]["default"] == "1.0.0", path.name


def test_current_export_cannot_overwrite_the_legacy_snapshot(tmp_path: Path) -> None:
    """导出只写目标目录顶层，版本化快照目录不受影响。"""
    snapshot_dir = tmp_path / "v1"
    snapshot_dir.mkdir()
    guarded = snapshot_dir / "Ref.schema.json"
    guarded.write_text('{"pinned": true}\n', encoding="utf-8")

    written = export_json_schemas(tmp_path)
    assert all(path.parent == tmp_path for path in written.values())
    assert json.loads(guarded.read_text(encoding="utf-8")) == {"pinned": True}


def test_registry_covers_the_new_selection_value_object() -> None:
    names = {model.__name__ for model in CONTRACT_MODELS}
    assert "ProfileSelection" in names
    assert "ProfileSelectionKey" in names
    assert names >= set(V1_MODEL_NAMES), "v2 不得悄悄丢掉 v1 已有的契约"


# ======================================================================================
# ExperimentMetadata：与复现元组共用同一个选择输入类型
# ======================================================================================


def test_metadata_and_tuple_share_the_selection_value_object() -> None:
    """ADR-0015 §D-22.3 起两处共用**整个** `ProfileSelection`，不只是选择输入。"""
    repro = factories.repro_tuple()
    meta = ExperimentMetadata(
        experiment_hash=repro.experiment_hash,
        constitution_version=repro.constitution_version,
        validation_profile=repro.validation_profile,
        validation_profile_hash=repro.validation_profile_hash,
        profile_selection=repro.profile_selection,
        hypothesis_family_id="family-1",
        trial_index=1,
        family_trial_count=1,
        declared_research_class=repro.profile_selection.key.research_class,
    )
    assert meta.profile_selection == repro.profile_selection
    assert meta.profile_selection.key == repro.profile_selection.key
    assert meta.experiment_hash == repro.experiment_hash


# ======================================================================================
# LlmCall：登记结构已由 ADR-0016 补齐，存储 / 取回缺口仍未关闭
# ======================================================================================


def test_llm_call_registers_content_references_not_bare_hashes() -> None:
    """ADR-0009 §5 记录的登记缺口由 ADR-0016 在**结构层面**关闭。

    三个自由字符串哈希已被三项必填的 `ContentBlobRef`（取回引用 + `ContentHash`）取代，
    并加上显式必填的 `called_at`。完整边界见 `tests/test_llm_call_bindings.py`。
    """
    from core.domain.base import ContentBlobRef
    from core.domain.research import LlmCall

    assert set(LlmCall.model_fields) == {
        "schema_version",
        "provider",
        "model",
        "prompt",
        "input",
        "output",
        "called_at",
    }
    for field in ("prompt", "input", "output"):
        assert LlmCall.model_fields[field].annotation is ContentBlobRef
        assert LlmCall.model_fields[field].is_required()
    assert not {"prompt_hash", "input_hash", "output_hash"} & set(LlmCall.model_fields)


def test_llm_call_storage_and_retrieval_gap_is_still_open() -> None:
    """**结构完整 ≠ 内容可复核**：存储 / 取回 / 一致性仍是未实现的延期义务。

    契约层拿不到 `uri` 指向的内容，因此不校验它是否存在、是否哈希成 `sha256`，
    也无法判断一次实验是否登记了**所有**发生过的调用（ADR-0016 §D-18.3）。
    这些义务必须继续以缺口形式写在文档里，不得被描述为已满足。
    """
    from core.domain.base import ContentBlobRef
    from core.domain.research import LlmCall

    for model in (LlmCall, ContentBlobRef):
        for name, field in model.model_fields.items():
            assert field.annotation is not bool, f"{model.__name__}.{name} 是自报布尔标志"
        assert not hasattr(model, "verify")
        assert not hasattr(model, "fetch")
    doc = (REPO / "docs" / "architecture" / "06-experiment.md").read_text(encoding="utf-8")
    assert "登记缺口" in doc, "06-experiment.md 必须保留 LLM 完整输入输出的缺口说明"
    assert "仍未完全满足" in doc, "不得把结构化登记描述为已满足完整输入输出"


# ======================================================================================
# 本轮明确不实现的内容（避免验收被夸大）
# ======================================================================================


def test_runner_and_registry_obligations_are_documented_not_implemented() -> None:
    doc = (REPO / "docs" / "architecture" / "06-experiment.md").read_text(encoding="utf-8")
    for obligation in ("传递依赖", "trial", "Registry"):
        assert obligation in doc
    assert not hasattr(ReproducibilityTuple, "resolve_transitive_dependencies")
    assert not hasattr(ExperimentRun, "verify_against_spec")
    assert not (REPO / "core" / "registry").exists()
    assert not (REPO / "core" / "runner").exists()


def test_no_self_reported_completeness_flags() -> None:
    """ADR-0009 §6：不引入任何自报布尔标志。"""
    for model in (ReproducibilityTuple, ExperimentRun, StrategyArtifact):
        for name, field in model.model_fields.items():
            if field.annotation is bool:
                pytest.fail(f"{model.__name__}.{name} 是自报布尔标志")


def test_seed_policy_dsl_was_not_introduced() -> None:
    assert "seed_policy" not in ReproducibilityTuple.model_fields
    assert set(ReproducibilityTuple.model_fields) >= {"seeds"}


def test_verdict_enum_is_unchanged() -> None:
    assert {v.value for v in Verdict} == {"PASS", "FAIL", "INCONCLUSIVE"}


def test_dataset_snapshots_still_required() -> None:
    with pytest.raises(ValidationError):
        factories.repro_tuple(dataset_snapshots=())
    assert factories.repro_tuple().dataset_snapshots[0].time_range_end > T0 - timedelta(days=1)

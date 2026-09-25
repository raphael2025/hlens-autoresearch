"""审计身份类型与版本绑定（ADR-0015，D-21 / D-22）。

覆盖 ADR-0015 验收矩阵 1 ~ 19：内容哈希、Git OID、结构化生产代码修订、
刻意保留为不透明的字段、Constitution 版本语法、Profile 的 `Ref` 绑定、
完整 `ProfileSelection` 与重复字段的删除，以及 current / v1 Schema 的边界。

**本文件断言的是公开行为**：线载荷的接受 / 拒绝、导出的 JSON Schema 与运行时同源、
跨字段不变量。不断言实现细节（校验器名称、私有属性、内部数据结构）。

**不在本轮范围**（ADR-0015「明确不做」与「运行时延期义务」）：不校验任何哈希是否与真实
内容一致、不访问 Git、不校验被引用的 Profile 是否已登记或 frozen、不定义
`run_id` / `report_id` / `deployment_id` 的生成算法、不定义 `environment_lock` 的结构化表达。
本文件不得暗示上述任何一项已经实现。
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel, ValidationError

from core.compat.v1 import V1_MODEL_NAMES
from core.contracts.profile_selection import ExperimentMetadata, SelectionEntry
from core.contracts.registry import CONTRACT_MODELS, export_json_schemas
from core.domain import base
from core.domain.artifact import DeploymentRecord, EquivalenceCheck, GoldenOutputs, StrategyArtifact
from core.domain.base import GitCodeRevision, Kind, Ref
from core.domain.research import ReproducibilityTuple, ValidationReport
from tests import factories

REPO = Path(__file__).resolve().parents[1]
CURRENT_SCHEMA_DIR = REPO / "schemas"
LEGACY_SCHEMA_DIR = CURRENT_SCHEMA_DIR / "v1"
VECTOR_DIR = REPO / "tests" / "vectors" / "v1"

#: 合法的 64 位小写十六进制 SHA-256（取值无语义）。
GOOD_HASH = "4" * 64

#: 非法的内容哈希：大写、63 / 65 位、非十六进制字符、空串，以及旧测试里的自由字符串。
BAD_HASHES = (
    "A" * 64,
    "a" * 63,
    "a" * 65,
    "g" * 64,
    "",
    "profile-hash",
    "exp-hash",
)

#: 合法的 Git OID：40 位（SHA-1）与 64 位（SHA-256），都必须是小写。
GOOD_OIDS = (factories.GIT_COMMIT_OID, factories.GIT_COMMIT_OID_SHA256)

#: 非法的 Git OID：短 SHA、大写、39 / 41 / 63 / 65 位、非十六进制、空串。
BAD_OIDS = (
    "0123456",
    "0123456789abcdef0123456789abcdef0123456",
    "0123456789abcdef0123456789abcdef012345678",
    "0123456789ABCDEF0123456789abcdef01234567",
    "0123456789abcdef" * 4 + "0",
    ("0123456789abcdef" * 4)[:63],
    "z" * 40,
    "",
)


# ======================================================================================
# 矩阵 1、2：D-21.1 表中**每一个** ContentHash 字段的合法 / 非法边界
# ======================================================================================


def _deployment_with_artifact_id(value: str) -> DeploymentRecord:
    """`artifact_id` 在部署与 Equivalence 两处必须一致，因此同时设置两处。"""
    return factories.deployment_record(
        artifact_id=value, equivalence=factories.equivalence_check(artifact_id=value)
    )


#: `(标签, 用给定值构造对应模型)`；标签即 D-21.1 表中的"模型.字段"。
CONTENT_HASH_SLOTS: tuple[tuple[str, Callable[[str], BaseModel]], ...] = (
    (
        "ReproducibilityTuple.validation_profile_hash",
        lambda v: factories.repro_tuple(validation_profile_hash=v),
    ),
    ("ValidationReport.experiment_hash", lambda v: factories.validation_report(experiment_hash=v)),
    (
        "ValidationReport.validation_profile_hash",
        lambda v: factories.validation_report(validation_profile_hash=v),
    ),
    (
        "ExperimentMetadata.experiment_hash",
        lambda v: factories.experiment_metadata(experiment_hash=v),
    ),
    (
        "ExperimentMetadata.validation_profile_hash",
        lambda v: factories.experiment_metadata(validation_profile_hash=v),
    ),
    (
        "StrategyArtifact.experiment_hashes[*]",
        lambda v: factories.strategy_artifact(experiment_hashes=(v,)),
    ),
    ("GoldenOutputs.signals_hash", lambda v: factories.golden_outputs(signals_hash=v)),
    ("GoldenOutputs.positions_hash", lambda v: factories.golden_outputs(positions_hash=v)),
    ("EquivalenceCheck.artifact_id", lambda v: factories.equivalence_check(artifact_id=v)),
    ("DeploymentRecord.artifact_id", _deployment_with_artifact_id),
    ("DeploymentRecord.config_hash", lambda v: factories.deployment_record(config_hash=v)),
)

CONTENT_HASH_IDS = tuple(label for label, _ in CONTENT_HASH_SLOTS)


@pytest.mark.parametrize(("label", "build"), CONTENT_HASH_SLOTS, ids=CONTENT_HASH_IDS)
def test_content_hash_slot_accepts_canonical_sha256(
    label: str, build: Callable[[str], BaseModel]
) -> None:
    """矩阵 2：64 位小写十六进制被接受，且原样保留在线载荷中。"""
    model = build(GOOD_HASH)
    payload = json.loads(model.model_dump_json())
    assert GOOD_HASH in json.dumps(payload), label


@pytest.mark.parametrize("bad", BAD_HASHES)
@pytest.mark.parametrize(("label", "build"), CONTENT_HASH_SLOTS, ids=CONTENT_HASH_IDS)
def test_content_hash_slot_rejects_non_sha256(
    label: str, build: Callable[[str], BaseModel], bad: str
) -> None:
    """矩阵 1：大写、63 / 65 位、非十六进制、空串、自由字符串逐字段被拒绝（含容器元素）。"""
    with pytest.raises(ValidationError):
        build(bad)


def test_content_hash_slot_rejects_non_sha256_from_json_text() -> None:
    """JSON 文本入口与 Python 入口同样收紧（两条入口、同一个 pattern）。"""
    payload = json.loads(factories.validation_report().model_dump_json())
    assert ValidationReport.model_validate_json(json.dumps(payload)).experiment_hash
    with pytest.raises(ValidationError):
        ValidationReport.model_validate_json(json.dumps({**payload, "experiment_hash": "exp-hash"}))


#: `(模型, 属性路径)`：路径最后一段为 `[*]` 表示序列元素。
CONTENT_HASH_SCHEMA_SLOTS: tuple[tuple[type[BaseModel], tuple[str, ...]], ...] = (
    (ReproducibilityTuple, ("validation_profile_hash",)),
    (ValidationReport, ("experiment_hash",)),
    (ValidationReport, ("validation_profile_hash",)),
    (ExperimentMetadata, ("experiment_hash",)),
    (ExperimentMetadata, ("validation_profile_hash",)),
    (StrategyArtifact, ("experiment_hashes", "[*]")),
    (GoldenOutputs, ("signals_hash",)),
    (GoldenOutputs, ("positions_hash",)),
    (EquivalenceCheck, ("artifact_id",)),
    (DeploymentRecord, ("artifact_id",)),
    (DeploymentRecord, ("config_hash",)),
)


def _field_schema(model: type[BaseModel], path: tuple[str, ...]) -> dict[str, Any]:
    """从导出的序列化 Schema 中取出某个字段（或其元素）的子 Schema。"""
    node: dict[str, Any] = model.model_json_schema(mode="serialization")["properties"][path[0]]
    for segment in path[1:]:
        node = node["items"] if segment == "[*]" else node["properties"][segment]
    return node


@pytest.mark.parametrize(
    ("model", "path"),
    CONTENT_HASH_SCHEMA_SLOTS,
    ids=[f"{m.__name__}.{'.'.join(p)}" for m, p in CONTENT_HASH_SCHEMA_SLOTS],
)
def test_content_hash_schema_pattern_comes_from_the_runtime_source(
    model: type[BaseModel], path: tuple[str, ...]
) -> None:
    """导出的 Schema 必须带**同一个** `SHA256_PATTERN`，而不是 `minLength: 1`。"""
    node = _field_schema(model, path)
    assert node["pattern"] == base.SHA256_PATTERN
    assert "minLength" not in node


# ======================================================================================
# 矩阵 3 ~ 6：Git OID 与 GitCodeRevision
# ======================================================================================

GIT_OID_SLOTS: tuple[tuple[str, Callable[[str], BaseModel]], ...] = (
    ("ReproducibilityTuple.code_commit", lambda v: factories.repro_tuple(code_commit=v)),
    (
        "StrategyArtifact.research_code_commit",
        lambda v: factories.strategy_artifact(research_code_commit=v),
    ),
    (
        "StrategyArtifact.research_code_tree_hash",
        lambda v: factories.strategy_artifact(research_code_tree_hash=v),
    ),
    ("GitCodeRevision.commit_oid", lambda v: factories.git_code_revision(commit_oid=v)),
    ("GitCodeRevision.tree_oid", lambda v: factories.git_code_revision(tree_oid=v)),
)

GIT_OID_IDS = tuple(label for label, _ in GIT_OID_SLOTS)


@pytest.mark.parametrize("good", GOOD_OIDS)
@pytest.mark.parametrize(("label", "build"), GIT_OID_SLOTS, ids=GIT_OID_IDS)
def test_git_oid_accepts_full_lowercase_hex(
    label: str, build: Callable[[str], BaseModel], good: str
) -> None:
    """矩阵 4：40 位与 64 位小写十六进制都被接受。"""
    assert good in build(good).model_dump_json(), label


@pytest.mark.parametrize("bad", BAD_OIDS)
@pytest.mark.parametrize(("label", "build"), GIT_OID_SLOTS, ids=GIT_OID_IDS)
def test_git_oid_rejects_short_uppercase_and_wrong_length(
    label: str, build: Callable[[str], BaseModel], bad: str
) -> None:
    """矩阵 3、5、6：7 位短 SHA、大写、39 / 41 / 63 / 65 位、非十六进制、空串一律拒绝。"""
    with pytest.raises(ValidationError):
        build(bad)


def test_git_oid_is_not_a_content_hash_namespace() -> None:
    """两套命名空间刻意分开：64 位十六进制两边都合法，但 40 位只对 Git OID 合法。"""
    assert base.GIT_OID_PATTERN != base.SHA256_PATTERN
    with pytest.raises(ValidationError):
        factories.golden_outputs(signals_hash=factories.GIT_COMMIT_OID)  # 40 位不是 SHA-256
    assert factories.repro_tuple(code_commit=GOOD_HASH).code_commit == GOOD_HASH


GIT_OID_SCHEMA_SLOTS: tuple[tuple[type[BaseModel], tuple[str, ...]], ...] = (
    (ReproducibilityTuple, ("code_commit",)),
    (StrategyArtifact, ("research_code_commit",)),
    (StrategyArtifact, ("research_code_tree_hash",)),
    (GitCodeRevision, ("commit_oid",)),
    (GitCodeRevision, ("tree_oid",)),
)


@pytest.mark.parametrize(
    ("model", "path"),
    GIT_OID_SCHEMA_SLOTS,
    ids=[f"{m.__name__}.{'.'.join(p)}" for m, p in GIT_OID_SCHEMA_SLOTS],
)
def test_git_oid_schema_pattern_matches_runtime(
    model: type[BaseModel], path: tuple[str, ...]
) -> None:
    node = _field_schema(model, path)
    assert node["pattern"] == base.GIT_OID_PATTERN
    assert "minLength" not in node


@pytest.mark.parametrize("value", (*GOOD_OIDS, *BAD_OIDS))
def test_git_oid_schema_pattern_accepts_exactly_what_runtime_accepts(value: str) -> None:
    """Schema 里声明的 pattern 与运行时实际接受 / 拒绝的集合必须一致。"""
    import re

    compiled = re.compile(_field_schema(GitCodeRevision, ("commit_oid",))["pattern"])
    accepted_by_schema = compiled.match(value) is not None
    try:
        factories.git_code_revision(commit_oid=value)
        accepted_by_runtime = True
    except ValidationError:
        accepted_by_runtime = False
    assert accepted_by_schema is accepted_by_runtime, value


def test_git_code_revision_validates_both_fields_independently() -> None:
    """矩阵 6：commit 与 tree 各自独立校验，缺一或非法都被拒绝。"""
    revision = factories.git_code_revision()
    assert revision.commit_oid != revision.tree_oid
    assert set(GitCodeRevision.model_fields) == {"schema_version", "commit_oid", "tree_oid"}
    for missing in ("commit_oid", "tree_oid"):
        payload = revision.model_dump()
        payload.pop(missing)
        with pytest.raises(ValidationError):
            GitCodeRevision(**payload)


# ======================================================================================
# 矩阵 7、16、17：结构化生产代码修订与新契约模型的登记边界
# ======================================================================================


def test_deployment_accepts_identical_production_code_revision() -> None:
    record = factories.deployment_record()
    assert record.production_code_hash == record.equivalence.production_code_hash
    assert isinstance(record.production_code_hash, GitCodeRevision)


@pytest.mark.parametrize("differing", ("commit_oid", "tree_oid"))
def test_deployment_rejects_differing_production_code_revision(differing: str) -> None:
    """矩阵 7：commit 或 tree 任一不同即拒绝——比较的是值对象，不是字符串前缀。"""
    other = factories.git_code_revision(**{differing: factories.OTHER_GIT_COMMIT_OID})
    with pytest.raises(ValidationError):
        factories.deployment_record(
            production_code_hash=factories.git_code_revision(),
            equivalence=factories.equivalence_check(production_code_hash=other),
        )


def test_deployment_still_rejects_failed_equivalence_and_wrong_artifact() -> None:
    """既有的部署门行为不因类型变化而削弱（ADR-0005 §4）。"""
    with pytest.raises(ValidationError):
        factories.deployment_record(
            equivalence=factories.equivalence_check(signals_match=False),
        )
    with pytest.raises(ValidationError):
        factories.deployment_record(artifact_id=GOOD_HASH)  # Equivalence 仍指向另一个 artifact


def test_production_code_revision_keeps_its_wire_field_name() -> None:
    """线字段名沿用 `production_code_hash`，值是 `{commit_oid, tree_oid}`（未批准重命名）。"""
    for model in (EquivalenceCheck, DeploymentRecord):
        assert "production_code_hash" in model.model_fields
        node = model.model_json_schema(mode="serialization")["properties"]["production_code_hash"]
        assert node["$ref"].endswith("/GitCodeRevision")
    payload = json.loads(factories.deployment_record().model_dump_json())
    assert set(payload["production_code_hash"]) >= {"commit_oid", "tree_oid"}


def test_git_code_revision_is_registered_and_exported(tmp_path: Path) -> None:
    """矩阵 16：新契约模型必须在注册表内并导出独立 Schema。"""
    assert GitCodeRevision in CONTRACT_MODELS
    written = export_json_schemas(tmp_path)
    assert "GitCodeRevision" in written
    committed = CURRENT_SCHEMA_DIR / "GitCodeRevision.schema.json"
    assert committed.is_file()
    assert json.loads(committed.read_text(encoding="utf-8")) == json.loads(
        written["GitCodeRevision"].read_text(encoding="utf-8")
    )


def test_git_code_revision_is_not_a_v1_model() -> None:
    """矩阵 17：它是 v2 才出现的模型，不进入 v1 只读路径。"""
    assert "GitCodeRevision" not in V1_MODEL_NAMES
    assert not (LEGACY_SCHEMA_DIR / "GitCodeRevision.schema.json").exists()


# ======================================================================================
# 矩阵 8：D-21.3 明确保留为不透明 / 复合描述的字段
# ======================================================================================

#: `(标签, 用非哈希字符串构造)`：这些字段**必须**继续接受非哈希取值。
OPAQUE_SLOTS: tuple[tuple[str, Callable[[str], BaseModel]], ...] = (
    ("ExperimentRun.run_id", lambda v: factories.experiment_run(run_id=v)),
    ("ExperimentRun.trace_id", lambda v: factories.experiment_run(trace_id=v)),
    ("ValidationReport.run_id", lambda v: factories.validation_report(run_id=v)),
    ("ValidationReport.report_id", lambda v: factories.validation_report(report_id=v)),
    ("DeploymentRecord.deployment_id", lambda v: factories.deployment_record(deployment_id=v)),
    ("ReproducibilityTuple.environment_lock", lambda v: factories.repro_tuple(environment_lock=v)),
    ("ReproducibilityTuple.split_spec", lambda v: factories.repro_tuple(split_spec=v)),
    (
        "GoldenOutputs.dataset_snapshot_id",
        lambda v: factories.golden_outputs(dataset_snapshot_id=v),
    ),
    ("GoldenOutputs.signals_uri", lambda v: factories.golden_outputs(signals_uri=v)),
    ("GoldenOutputs.positions_uri", lambda v: factories.golden_outputs(positions_uri=v)),
    (
        "StrategyArtifact.validation_reports",
        lambda v: factories.strategy_artifact(validation_reports=(v,)),
    ),
    ("ExperimentMetadata.trace_id", lambda v: factories.experiment_metadata(trace_id=v)),
)


@pytest.mark.parametrize(
    "value", ["run-1", "s3://bucket/signals", "snap-1", "lock-hash+py3.13+linux", "rep-1"]
)
@pytest.mark.parametrize(
    ("label", "build"), OPAQUE_SLOTS, ids=tuple(label for label, _ in OPAQUE_SLOTS)
)
def test_opaque_identifier_still_accepts_non_hash_strings(
    label: str, build: Callable[[str], BaseModel], value: str
) -> None:
    """不按名称机械收紧：这些槽位的格式由外部系统或未定方案决定（ADR-0015 §D-21.3）。"""
    payload = json.loads(build(value).model_dump_json())
    assert value in json.dumps(payload, ensure_ascii=False), label


def test_dataset_snapshot_id_is_not_tightened() -> None:
    """`DatasetRef.snapshot_id` 是 Iceberg 的标识，格式由外部系统决定。"""
    assert factories.dataset_ref().snapshot_id == "snap-1"
    tuple_ = factories.repro_tuple(
        dataset_snapshots=(factories.dataset_ref().model_copy(update={"snapshot_id": "snap-42"}),)
    )
    assert tuple_.dataset_snapshots[0].snapshot_id == "snap-42"


def test_opaque_identifiers_have_no_hash_pattern_in_schema() -> None:
    """Schema 也必须诚实：这些字段不得声明成 SHA-256 形状。"""
    checks = (
        (ValidationReport, "run_id"),
        (ValidationReport, "report_id"),
        (DeploymentRecord, "deployment_id"),
        (ReproducibilityTuple, "environment_lock"),
        (GoldenOutputs, "dataset_snapshot_id"),
        (GoldenOutputs, "signals_uri"),
        (GoldenOutputs, "positions_uri"),
    )
    for model, field in checks:
        node = _field_schema(model, (field,))
        assert node.get("pattern") != base.SHA256_PATTERN, f"{model.__name__}.{field}"


# ======================================================================================
# 矩阵 9、10：constitution_version 使用唯一 ASCII SemVer（D-22.1）
# ======================================================================================

CONSTITUTION_VERSION_SLOTS: tuple[tuple[str, Callable[[str], BaseModel]], ...] = (
    (
        "ReproducibilityTuple.constitution_version",
        lambda v: factories.repro_tuple(constitution_version=v),
    ),
    (
        "ValidationReport.constitution_version",
        lambda v: factories.validation_report(constitution_version=v),
    ),
    (
        "ExperimentMetadata.constitution_version",
        lambda v: factories.experiment_metadata(constitution_version=v),
    ),
)

CONSTITUTION_IDS = tuple(label for label, _ in CONSTITUTION_VERSION_SLOTS)


@pytest.mark.parametrize("bad", ["1.0", "v1.0.0", "١.٠.٠", "01.0.0", "1.0.0-", "", "draft"])
@pytest.mark.parametrize(("label", "build"), CONSTITUTION_VERSION_SLOTS, ids=CONSTITUTION_IDS)
def test_constitution_version_rejects_non_canonical_semver(
    label: str, build: Callable[[str], BaseModel], bad: str
) -> None:
    """矩阵 9：缺 patch、`v` 前缀、Unicode 数字、前导零、空标识符全部拒绝。"""
    with pytest.raises(ValidationError):
        build(bad)


@pytest.mark.parametrize("good", ["0.2.0-draft", "1.0.0", "1.2.3-rc.1+build.5"])
@pytest.mark.parametrize(("label", "build"), CONSTITUTION_VERSION_SLOTS, ids=CONSTITUTION_IDS)
def test_constitution_version_accepts_canonical_semver(
    label: str, build: Callable[[str], BaseModel], good: str
) -> None:
    """矩阵 10：当前的 `0.2.0-draft` 合法；本约束不预判它何时成为 `1.0.0`。"""
    assert good in build(good).model_dump_json(), label


@pytest.mark.parametrize(
    "model", (ReproducibilityTuple, ValidationReport, ExperimentMetadata), ids=lambda m: m.__name__
)
def test_constitution_version_schema_uses_the_single_semver_pattern(
    model: type[BaseModel],
) -> None:
    node = _field_schema(model, ("constitution_version",))
    assert node["pattern"] == base.SEMVER_PATTERN
    schema_version = _field_schema(model, ("schema_version",))
    assert node["pattern"] == schema_version["pattern"], "全项目只有一套版本语法"


# ======================================================================================
# 矩阵 11、12：Profile 绑定改为 Ref(kind=profile) + ContentHash（D-22.2）
# ======================================================================================

PROFILE_BINDING_SLOTS: tuple[tuple[str, Callable[..., BaseModel]], ...] = (
    ("ReproducibilityTuple", factories.repro_tuple),
    ("ValidationReport", factories.validation_report),
    ("ExperimentMetadata", factories.experiment_metadata),
)

PROFILE_BINDING_IDS = tuple(label for label, _ in PROFILE_BINDING_SLOTS)


@pytest.mark.parametrize(("label", "build"), PROFILE_BINDING_SLOTS, ids=PROFILE_BINDING_IDS)
def test_profile_binding_accepts_profile_ref_paired_with_content_hash(
    label: str, build: Callable[..., BaseModel]
) -> None:
    """矩阵 11 正例：`kind = profile` 的引用 + 内容哈希被接受，且规范串可读。"""
    model = build(
        validation_profile=factories.profile_ref("other_scope", "2.3.4"),
        validation_profile_hash=GOOD_HASH,
    )
    assert str(model.validation_profile) == "profile:other_scope@2.3.4"  # type: ignore[attr-defined]
    assert model.validation_profile_hash == GOOD_HASH  # type: ignore[attr-defined]


@pytest.mark.parametrize(
    "wrong_kind", [Kind.FEATURE, Kind.STRATEGY, Kind.PROFILE_SELECTION_RULE, Kind.EXPERIMENT]
)
@pytest.mark.parametrize(("label", "build"), PROFILE_BINDING_SLOTS, ids=PROFILE_BINDING_IDS)
def test_profile_binding_rejects_wrong_kind(
    label: str, build: Callable[..., BaseModel], wrong_kind: Kind
) -> None:
    """矩阵 11：三个模型都必须拒绝指向别的对象类型的引用。"""
    with pytest.raises(ValidationError):
        build(validation_profile=Ref(kind=wrong_kind, name="test_scope", version="1.0.0"))


@pytest.mark.parametrize(("label", "build"), PROFILE_BINDING_SLOTS, ids=PROFILE_BINDING_IDS)
def test_old_profile_version_string_is_rejected_as_extra(
    label: str, build: Callable[..., BaseModel]
) -> None:
    """矩阵 12：旧的自由字符串字段已删除，作为多余字段被 `extra="forbid"` 拒绝。"""
    with pytest.raises(ValidationError):
        build(validation_profile_version="vp:test_scope@1.0.0")


@pytest.mark.parametrize(
    "model", (ReproducibilityTuple, ValidationReport, ExperimentMetadata), ids=lambda m: m.__name__
)
def test_profile_binding_fields_are_a_ref_and_a_hash(model: type[BaseModel]) -> None:
    assert "validation_profile_version" not in model.model_fields
    assert {"validation_profile", "validation_profile_hash"} <= set(model.model_fields)
    properties = model.model_json_schema(mode="serialization")["properties"]
    assert properties["validation_profile"]["$ref"].endswith("/Ref")
    assert properties["validation_profile_hash"]["pattern"] == base.SHA256_PATTERN


def test_profile_binding_does_not_claim_registry_checks() -> None:
    """契约层不校验被引用的 Profile 是否存在或 frozen（ADR-0015 运行时延期义务）。

    因此一个**未登记**的 Profile 版本在契约层是可接受的：这不是漏洞，而是刻意划清的边界，
    存在性与 frozen 状态属 Control Plane。
    """
    assert (
        factories.repro_tuple(
            validation_profile=factories.profile_ref("never_registered", "9.9.9")
        ).validation_profile.name
        == "never_registered"
    )


# ======================================================================================
# 矩阵 13、14：ExperimentMetadata 使用完整 ProfileSelection（D-22.3）
# ======================================================================================


def test_metadata_requires_the_full_profile_selection() -> None:
    meta = factories.experiment_metadata()
    assert "profile_selection" in ExperimentMetadata.model_fields
    assert meta.profile_selection.selection_rule.kind is Kind.PROFILE_SELECTION_RULE
    assert meta.profile_selection.selection_rule_hash == factories.HASH_RULE
    payload = meta.model_dump()
    payload.pop("profile_selection")
    with pytest.raises(ValidationError):
        ExperimentMetadata(**payload)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("profile_selection_rule_version", "1.0.0"),
        ("profile_selection_key", factories.selection_key()),
    ],
)
def test_metadata_rejects_the_old_selection_fields_as_extra(field: str, value: object) -> None:
    """矩阵 13：两个较弱的旧字段已被完整值对象取代，出现即拒绝。"""
    assert field not in ExperimentMetadata.model_fields
    with pytest.raises(ValidationError):
        factories.experiment_metadata(**{field: value})


def test_metadata_still_rejects_research_class_switching() -> None:
    """矩阵 14：既有校验保留，读取路径改为 `profile_selection.key.research_class`。"""
    with pytest.raises(ValidationError):
        factories.experiment_metadata(declared_research_class="intraday")
    with pytest.raises(ValidationError):
        factories.experiment_metadata(
            profile_selection=factories.profile_selection(key=factories.selection_key("intraday"))
        )
    consistent = factories.experiment_metadata(
        declared_research_class="intraday",
        profile_selection=factories.profile_selection(key=factories.selection_key("intraday")),
    )
    assert consistent.declared_research_class == consistent.profile_selection.key.research_class


def test_metadata_selection_rule_kind_is_still_enforced() -> None:
    """完整值对象自带的规则 kind 校验没有丢失。"""
    with pytest.raises(ValidationError):
        factories.experiment_metadata(
            profile_selection=factories.profile_selection(selection_rule=factories.strategy_ref())
        )


# ======================================================================================
# 矩阵 15：SelectionEntry 删除重复的 profile_version（D-22.4）
# ======================================================================================


def test_selection_entry_rejects_the_removed_profile_version_field() -> None:
    assert "profile_version" not in SelectionEntry.model_fields
    with pytest.raises(ValidationError):
        SelectionEntry(
            key=factories.selection_key(),
            profile=factories.profile_ref(),
            profile_version="1.0.0",  # type: ignore[call-arg]
        )


def test_selection_entry_reads_the_version_from_the_profile_ref() -> None:
    entry = SelectionEntry(key=factories.selection_key(), profile=factories.profile_ref())
    assert entry.profile.version == "1.0.0"
    with pytest.raises(ValidationError):
        SelectionEntry(key=factories.selection_key(), profile=factories.strategy_ref())


def test_selection_rule_selection_is_still_deterministic() -> None:
    """删除重复字段不改变选择规则的确定性（Constitution C-A4）。"""
    rule = factories.selection_rule()
    assert rule.select(factories.selection_key()).profile == factories.profile_ref()


# ======================================================================================
# 矩阵 18、19：current 与 v1 Schema 的边界
# ======================================================================================


def test_current_schema_export_is_complete_and_committed(tmp_path: Path) -> None:
    """current 全量导出与已提交内容逐字段一致。

    模型数：ADR-0015 把它从 36 提到 37（新增 `GitCodeRevision`），
    ADR-0016 再提到 38（新增 `ContentBlobRef`），Phase 1 B1（ADR-0023）再提到 46，
    B2（ADR-0024）再提到 59，B3（Data Plane Adapter DTO）再提到 74，
    F4（ADR-0030 FeatureProvider DTO）再提到 79，
    Phase 2（ADR-0035 StateProvider DTO）再提到 84。这里跟随当前事实，
    `GitCodeRevision` 本身的登记由 `test_git_code_revision_is_registered_and_exported` 断言。
    """
    written = export_json_schemas(tmp_path)
    assert len(CONTRACT_MODELS) == 126
    assert len(written) == len(CONTRACT_MODELS)
    committed = {path.name for path in CURRENT_SCHEMA_DIR.glob("*.schema.json")}
    assert committed == {path.name for path in written.values()}
    for name, path in written.items():
        actual = json.loads((CURRENT_SCHEMA_DIR / f"{name}.schema.json").read_text("utf-8"))
        assert actual == json.loads(path.read_text(encoding="utf-8")), f"{name} 的 Schema 已过期"


def test_legacy_v1_snapshots_keep_the_v1_field_names() -> None:
    """矩阵 18：v1 快照仍是 35 份，且保留 v1 当时的字段名——旧身份不被新规则重写。"""
    names = {p.stem.removesuffix(".schema") for p in LEGACY_SCHEMA_DIR.glob("*.schema.json")}
    assert names == set(V1_MODEL_NAMES)
    assert len(names) == 35
    for model in ("ReproducibilityTuple", "ValidationReport", "ExperimentMetadata"):
        schema = json.loads((LEGACY_SCHEMA_DIR / f"{model}.schema.json").read_text("utf-8"))
        assert "validation_profile_version" in schema["properties"], model
        assert "validation_profile" not in schema["properties"], model
    entry = json.loads((LEGACY_SCHEMA_DIR / "SelectionEntry.schema.json").read_text("utf-8"))
    assert "profile_version" in entry["properties"]


def test_v1_vectors_keep_the_v1_payload_shape() -> None:
    """固定向量仍是 v1 语义的载荷（短 commit、`vp:` 字符串），不得按 v2 规则重写。"""
    payload = json.loads((VECTOR_DIR / "reproducibility_tuple.json").read_text("utf-8"))["payload"]
    assert payload["validation_profile_version"].startswith("vp:")
    assert "validation_profile" not in payload
    with pytest.raises(ValidationError):
        ReproducibilityTuple(**payload)  # v1 载荷不是 v2 模型的合法输入


def test_v1_and_v2_identities_are_documented_as_incomparable() -> None:
    """矩阵 19：不对 v1 / v2 的同名哈希做相等断言；不可比较这件事写在文档与模块里。

    这里**刻意不比较**两个哈希值：即使它们偶然相等也不说明任何事，
    ADR-0009 §7 已判定它们不可比较，本 ADR 只是扩大了 v2 侧的字段变化。
    """
    doc = (REPO / "docs" / "architecture" / "02-domain.md").read_text(encoding="utf-8")
    assert "不可比较" in doc
    assert "不可比较" in (REPO / "core" / "compat" / "v1.py").read_text(encoding="utf-8")


# ======================================================================================
# 既有语义的回归：实验身份的稳定性、敏感性与 JSON 往返
# ======================================================================================


def test_experiment_hash_is_stable_across_equal_tuples() -> None:
    assert factories.repro_tuple().experiment_hash == factories.repro_tuple().experiment_hash


@pytest.mark.parametrize(
    ("label", "changed"),
    [
        ("code_commit", {"code_commit": factories.OTHER_GIT_COMMIT_OID}),
        ("validation_profile", {"validation_profile": factories.profile_ref(version="2.0.0")}),
        ("validation_profile_hash", {"validation_profile_hash": GOOD_HASH}),
        ("constitution_version", {"constitution_version": "1.0.0"}),
    ],
)
def test_new_identity_fields_are_part_of_the_experiment_hash(
    label: str, changed: dict[str, object]
) -> None:
    """收紧类型没有把任何身份槽位挪出内容哈希。"""
    assert factories.repro_tuple(**changed).experiment_hash != (
        factories.repro_tuple().experiment_hash
    ), label


@pytest.mark.parametrize(
    ("model", "build"),
    [
        (ReproducibilityTuple, factories.repro_tuple),
        (ValidationReport, factories.validation_report),
        (ExperimentMetadata, factories.experiment_metadata),
        (StrategyArtifact, factories.strategy_artifact),
        (DeploymentRecord, factories.deployment_record),
        (GitCodeRevision, factories.git_code_revision),
    ],
    ids=lambda value: getattr(value, "__name__", str(value)),
)
def test_new_shapes_survive_json_round_trip(
    model: type[BaseModel], build: Callable[[], BaseModel]
) -> None:
    original = build()
    restored = model.model_validate_json(original.model_dump_json())
    assert restored == original
    assert restored.content_hash() == original.content_hash()  # type: ignore[attr-defined]

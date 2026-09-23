"""契约 Schema v1 的只读读取入口（ADR-0008 §6、ADR-0009 §7）。

v1 与 v2 的 `content_hash` / `experiment_hash` **不可比较**：v2 的覆盖面更宽，
且 `ValidationProfile.status` 已退出哈希载荷。本模块按 **v1 当时的语义**读取旧载荷，
使旧记录在至少一个 major 内仍可被识别与追溯。

边界（不得被表述为迁移完成）：

* 读取结果是 `LegacyV1Record`，**不是** `Contract` 子类，不能当作 v2 模型使用，
  也不能作为 v2 登记 / 晋升的输入。缺 `dependency_hashes` / `run_id` /
  `profile_selection` 绑定的旧实验若需晋升，必须按 v2 路径重新登记。
* 未知 major 一律拒绝；同 major 的更高 minor 可以读取。
* 不做 JSON Schema 校验（不引入 `jsonschema` 依赖），不建数据库，也没有在线迁移服务。
* 固定载荷与旧哈希向量在 `tests/vectors/v1/`，v1 Schema 快照在 `schemas/v1/`。
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, final

from core.domain.base import FrozenMapping, content_hash
from core.errors import ContractViolation

__all__ = [
    "V1_MODEL_NAMES",
    "V1_NON_SEMANTIC_FIELDS",
    "V1_SCHEMA_MAJOR",
    "LegacyV1Record",
    "read_v1",
]

#: v1 的契约 major。
V1_SCHEMA_MAJOR = 1

#: v1 `Contract._non_semantic_fields()` 的**实际**取值（其中 `content_hash` 在 v1 中
#: 并不对应任何字段，保留它是为了逐字复现 v1 的哈希语义，而不是追认它是好设计）。
V1_NON_SEMANTIC_FIELDS: frozenset[str] = frozenset({"content_hash", "created_at"})

#: v1 导出的 35 个契约模型（与 `schemas/v1/` 快照一一对应）。
V1_MODEL_NAMES: frozenset[str] = frozenset(
    {
        "AuthorizationRecord",
        "DatasetRef",
        "DeploymentRecord",
        "EquivalenceCheck",
        "EventSpec",
        "ExecutionModeChange",
        "ExperimentMetadata",
        "ExperimentRun",
        "ExperimentSpec",
        "FailureRecord",
        "FeatureSpec",
        "GateResult",
        "GoldenOutputs",
        "Hypothesis",
        "Instrument",
        "KnowledgeItem",
        "LifecycleHistory",
        "LifecycleTransition",
        "LlmCall",
        "OosUnsealing",
        "OutcomeSpec",
        "ProfileSelectionKey",
        "ProfileSelectionRule",
        "Ref",
        "RepresentationSpec",
        "ReproducibilityTuple",
        "RetirementRecord",
        "RiskGateRecord",
        "RiskPolicy",
        "SelectionEntry",
        "StateSpec",
        "StrategyArtifact",
        "StrategySpec",
        "ValidationProfile",
        "ValidationReport",
    }
)


@final
class LegacyV1Record:
    """一条 v1 记录的只读读取结果。

    **刻意不是** `Contract` / `BaseModel`：类型系统本身就阻止它被当作 v2 模型传递、
    校验或登记。它只回答"这份旧载荷是什么、v1 当时的内容身份是多少"。
    """

    __slots__ = ("_content_hash", "_model", "_payload", "_schema_version")

    def __init__(
        self, *, model: str, schema_version: str, payload: Mapping[str, Any], content_hash: str
    ) -> None:
        self._model = model
        self._schema_version = schema_version
        self._payload: FrozenMapping[str, Any] = FrozenMapping(payload)
        self._content_hash = content_hash

    @property
    def model(self) -> str:
        """v1 模型名（仅标识用途，不解析为任何 v2 类型）。"""
        return self._model

    @property
    def schema_version(self) -> str:
        return self._schema_version

    @property
    def payload(self) -> FrozenMapping[str, Any]:
        """原样保留的 v1 载荷（只读，递归冻结）；**未**按 v2 算法重算任何字段。"""
        return self._payload

    @property
    def content_hash(self) -> str:
        """**v1 语义**下的内容哈希；不得与 v2 的 `content_hash()` 相互比较。"""
        return self._content_hash

    @property
    def experiment_hash(self) -> str:
        """v1 语义下的实验哈希；仅对 v1 `ReproducibilityTuple` 有意义。"""
        if self._model != "ReproducibilityTuple":
            raise ContractViolation(f"{self._model} 没有 v1 experiment_hash 语义")
        return self._content_hash

    def __repr__(self) -> str:
        return f"LegacyV1Record(model={self._model!r}, content_hash={self._content_hash!r})"


def read_v1(payload: Mapping[str, Any], *, model: str) -> LegacyV1Record:
    """按 v1 语义只读地读取一份旧载荷。

    未知 major、未知模型名或缺 `schema_version` 一律拒绝；同 major 的更高 minor 可读取。
    读取**不会**补造 v2 缺失的依赖绑定，也**不会**赋予任何 v2 资格。
    """
    if model not in V1_MODEL_NAMES:
        raise ContractViolation(f"{model!r} 不是 v1 契约模型")

    raw_version = payload.get("schema_version")
    if not isinstance(raw_version, str):
        raise ContractViolation("v1 载荷必须带字符串 schema_version")
    try:
        major = int(raw_version.split(".", 1)[0])
    except ValueError as exc:
        raise ContractViolation(f"无法解析 schema_version：{raw_version!r}") from exc
    if major != V1_SCHEMA_MAJOR:
        raise ContractViolation(
            f"不支持的 major：{raw_version}；v1 只读入口只接受 {V1_SCHEMA_MAJOR}.x"
        )

    hash_payload = {k: v for k, v in payload.items() if k not in V1_NON_SEMANTIC_FIELDS}
    return LegacyV1Record(
        model=model,
        schema_version=raw_version,
        payload=payload,
        content_hash=content_hash(hash_payload),
    )

"""契约 Schema v1 的只读读取入口（ADR-0008 §6、ADR-0009 §7）。

v1 与 v2 的 `content_hash` / `experiment_hash` **不可比较**：v2 的覆盖面更宽，
且 `ValidationProfile.status` 已退出哈希载荷。本模块按 **v1 当时的语义**读取旧载荷，
使旧记录在至少一个 major 内仍可被识别与追溯。

边界（不得被表述为迁移完成）：

* 读取结果是 `LegacyV1Record`，**不是** `Contract` 子类，不能当作 v2 模型使用，
  也不能作为 v2 登记 / 晋升的输入。缺 `dependency_hashes` / `run_id` /
  `profile_selection` 绑定的旧实验若需晋升，必须按 v2 路径重新登记。
* 未知 major 一律拒绝；同 major 的更高 minor 可以读取。
* **顶层 shape gate**（ADR-0010 §D-15）：读取前先用已提交的 `schemas/v1/<Model>.schema.json`
  做确定性的顶层检查——模型快照必须存在、`required` 字段必须齐全、
  当快照声明 `additionalProperties: false` 时拒绝未知顶层字段。
  这**不是完整的 JSON Schema 递归校验**：不校验嵌套对象的结构、类型、取值范围或数组元素，
  也不引入 `jsonschema` 依赖。它只保证"这份载荷在顶层形状上确实是该 v1 模型"，
  足以拒掉任意字典、错模型、缺必填、多余字段与改标签的 v2 载荷。
* 读取器依赖仓库中的 v1 快照；快照缺失时 **fail closed**（拒绝，而不是放行）。
* 不建数据库，也没有在线迁移服务。
* 固定载荷与旧哈希向量在 `tests/vectors/v1/`，v1 Schema 快照在 `schemas/v1/`。
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from functools import cache
from pathlib import Path
from typing import Any, final

from core.domain.base import FrozenMapping, content_hash
from core.errors import ContractViolation

__all__ = [
    "V1_MODEL_NAMES",
    "V1_NON_SEMANTIC_FIELDS",
    "V1_SCHEMA_MAJOR",
    "V1_SCHEMA_VERSION_PATTERN",
    "V1_SNAPSHOT_DIR",
    "LegacyV1Record",
    "read_v1",
]

#: v1 的契约 major。
V1_SCHEMA_MAJOR = 1

#: **v1 当时已发布的**版本语法，收紧为纯 ASCII 且 major 固定为 1。
#: 刻意不套用 v2 的新语法（v2 禁止前导零、支持 build metadata）——旧身份不能被新规则重写。
V1_SCHEMA_VERSION_PATTERN = r"^1\.[0-9]+\.[0-9]+(?:-[0-9A-Za-z.-]+)?$"
_V1_SCHEMA_VERSION_RE = re.compile(V1_SCHEMA_VERSION_PATTERN)

#: 已提交的 v1 Schema 快照目录（只读）。
V1_SNAPSHOT_DIR = Path(__file__).resolve().parents[2] / "schemas" / "v1"

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


@cache
def _top_level_shape(snapshot_dir: Path, model: str) -> tuple[frozenset[str], frozenset[str], bool]:
    """从已提交的 v1 快照读出顶层形状：(已声明字段, 必填字段, 是否禁止未知字段)。

    快照缺失 / 无法解析 → `ContractViolation`（fail closed）。
    """
    path = snapshot_dir / f"{model}.schema.json"
    if not path.is_file():
        raise ContractViolation(f"缺少 v1 Schema 快照：{path}；无法确定 {model} 的 v1 顶层形状")
    try:
        schema = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ContractViolation(f"无法读取 v1 Schema 快照：{path}") from exc
    properties = schema.get("properties")
    if not isinstance(properties, dict):
        raise ContractViolation(f"v1 Schema 快照缺少 properties：{path}")
    required = schema.get("required", [])
    closed = schema.get("additionalProperties") is False
    return frozenset(properties), frozenset(required), closed


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


def read_v1(
    payload: Mapping[str, Any], *, model: str, snapshot_dir: Path = V1_SNAPSHOT_DIR
) -> LegacyV1Record:
    """按 v1 语义只读地读取一份旧载荷。

    先过顶层 shape gate（模型已知、快照存在、`required` 齐全、未知顶层字段被拒），
    再校验 `schema_version` 是 v1 已发布语法的 `1.x.y[-...]`，**最后**才计算 v1 哈希。
    未通过 gate 的载荷不会得到任何 legacy 身份。

    读取**不会**补造 v2 缺失的依赖绑定，也**不会**赋予任何 v2 登记 / 晋升资格。
    """
    if model not in V1_MODEL_NAMES:
        raise ContractViolation(f"{model!r} 不是 v1 契约模型")
    if not isinstance(payload, Mapping):
        raise ContractViolation("v1 载荷必须是映射")

    declared, required, closed = _top_level_shape(snapshot_dir, model)

    raw_version = payload.get("schema_version")
    if not isinstance(raw_version, str):
        raise ContractViolation("v1 载荷必须带字符串 schema_version")
    if _V1_SCHEMA_VERSION_RE.fullmatch(raw_version) is None:
        raise ContractViolation(
            f"非法的 v1 schema_version：{raw_version!r}；"
            f"v1 只读入口只接受 {V1_SCHEMA_MAJOR}.x.y[-prerelease]"
        )

    keys = set(payload)
    missing = sorted(required - keys)
    if missing:
        raise ContractViolation(f"v1 {model} 载荷缺少必填顶层字段：{missing}")
    if closed:
        unknown = sorted(keys - declared)
        if unknown:
            raise ContractViolation(f"v1 {model} 载荷含未知顶层字段：{unknown}")

    hash_payload = {k: v for k, v in payload.items() if k not in V1_NON_SEMANTIC_FIELDS}
    return LegacyV1Record(
        model=model,
        schema_version=raw_version,
        payload=payload,
        content_hash=content_hash(hash_payload),
    )

"""Strategy Artifact 与生产部署记录（ADR-0005）。

Artifact 是"研究结论"的不可变打包；生产运行时只能加载 Registry 中登记、
且通过 Equivalence Gate 的 Artifact。研究代码永远不被生产运行时加载。
"""

from __future__ import annotations

from typing import Literal

from pydantic import Field, model_validator

from core.domain.base import (
    ContentHash,
    Contract,
    FrozenMapping,
    GitCodeRevision,
    GitOid,
    Kind,
    Ref,
    RefKey,
    VersionedSpec,
    validate_ref_keyed_hashes,
)
from core.domain.specs import Instrument

__all__ = ["DeploymentRecord", "EquivalenceCheck", "GoldenOutputs", "StrategyArtifact"]


class GoldenOutputs(Contract):
    """固定数据快照上的参考信号 / 仓位序列，用于 Equivalence Gate。

    两个 `*_hash` 是被引用内容的 `ContentHash`（ADR-0015 §D-21.1）；
    `dataset_snapshot_id` 与两个 `*_uri` **不**收紧：前者的格式由外部系统（Iceberg）决定，
    后者取决于尚未决定的存储方案（D-01、D-02），按名字里有 `id` / `uri` 就套哈希类型
    只会得到错误的契约（ADR-0015 §D-21.3）。
    """

    dataset_snapshot_id: str = Field(min_length=1)
    signals_uri: str = Field(min_length=1)
    signals_hash: ContentHash
    positions_uri: str = Field(min_length=1)
    positions_hash: ContentHash


class StrategyArtifact(VersionedSpec):
    """不可变、内容寻址的策略结论打包（ADR-0005 §2）。

    任何参数、依赖或代码变化 = 新 Artifact = 需重新验证。
    """

    kind: Literal[Kind.ARTIFACT] = Kind.ARTIFACT
    strategy_spec: Ref
    #: `kind:name@semver → SHA-256 内容哈希`（ADR-0009 §5）。键带 kind，避免 Feature /
    #: State 等同名对象混淆。**完整传递依赖闭包**由未来 Registry / 打包器解析并检查。
    dependencies: FrozenMapping[RefKey, ContentHash]
    #: 研究代码的 Git 身份：完整 OID，不接受短 SHA（ADR-0015 §D-21.2）。
    research_code_commit: GitOid
    research_code_tree_hash: GitOid
    #: 支撑该结论的实验身份，逐项都是 `experiment_hash`（ADR-0015 §D-21.1）。
    experiment_hashes: tuple[ContentHash, ...] = Field(min_length=1)
    #: 报告 ID 列表（即 `report_id`），是外部赋予的不透明标识而非内容身份，不收紧。
    validation_reports: tuple[str, ...] = Field(min_length=1)
    golden_outputs: GoldenOutputs
    applicable_instruments: tuple[Instrument, ...] = ()

    @property
    def artifact_id(self) -> str:
        return self.content_hash()

    @model_validator(mode="after")
    def _strategy_kind(self) -> StrategyArtifact:
        if self.strategy_spec.kind is not Kind.STRATEGY:
            raise ValueError("strategy_spec 必须指向 strategy")
        return self

    @model_validator(mode="after")
    def _direct_strategy_spec_is_bound(self) -> StrategyArtifact:
        validate_ref_keyed_hashes(self.dependencies, "dependencies")
        if str(self.strategy_spec) not in self.dependencies:
            raise ValueError(f"dependencies 必须绑定直接引用的 {self.strategy_spec} 的内容哈希")
        return self


class EquivalenceCheck(Contract):
    """生产实现与 golden outputs 的一致性检查（ADR-0005 §5）。

    `artifact_id` 是 Artifact manifest 规范化 JSON 的 SHA-256（ADR-0005 §3），
    因此它是 `ContentHash` 而不是不透明 ID（ADR-0015 §D-21.1）。
    `production_code_hash` 沿用既有线字段名，值已是结构化的 `GitCodeRevision`
    （commit + tree），不再是单个字符串（ADR-0015 §D-21.2）。
    """

    artifact_id: ContentHash
    production_code_hash: GitCodeRevision
    signals_match: bool
    positions_match: bool
    tolerance: str | None = None

    @property
    def passed(self) -> bool:
        return self.signals_match and self.positions_match


class DeploymentRecord(Contract):
    """生产部署记录：可追溯链的终点（ADR-0005 §4）。"""

    #: 部署标识是不透明的：生成算法未冻结（ADR-0009 §4、ADR-0015 §D-21.3）。
    deployment_id: str = Field(min_length=1)
    artifact_id: ContentHash
    production_code_hash: GitCodeRevision
    config_hash: ContentHash
    equivalence: EquivalenceCheck

    @model_validator(mode="after")
    def _must_pass_equivalence(self) -> DeploymentRecord:
        if not self.equivalence.passed:
            raise ValueError("未通过 Equivalence Gate 的实现不得部署（ADR-0005 §4）")
        if self.equivalence.artifact_id != self.artifact_id:
            raise ValueError("Equivalence 检查与部署的 artifact_id 不一致")
        # 比较代码身份 (commit, tree)，不比较信封版本（ADR-0015 §D-21.2、ADR-0018 §D-26.5）。
        equivalence_code = self.equivalence.production_code_hash.code_identity()
        if equivalence_code != self.production_code_hash.code_identity():
            raise ValueError("Equivalence 检查与部署的生产代码修订不一致（commit + tree）")
        return self

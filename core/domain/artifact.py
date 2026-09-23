"""Strategy Artifact 与生产部署记录（ADR-0005）。

Artifact 是"研究结论"的不可变打包；生产运行时只能加载 Registry 中登记、
且通过 Equivalence Gate 的 Artifact。研究代码永远不被生产运行时加载。
"""

from __future__ import annotations

from pydantic import Field, model_validator

from core.domain.base import Contract, Kind, Ref, VersionedSpec
from core.domain.specs import Instrument

__all__ = ["DeploymentRecord", "EquivalenceCheck", "GoldenOutputs", "StrategyArtifact"]


class GoldenOutputs(Contract):
    """固定数据快照上的参考信号 / 仓位序列，用于 Equivalence Gate。"""

    dataset_snapshot_id: str = Field(min_length=1)
    signals_uri: str = Field(min_length=1)
    signals_hash: str = Field(min_length=1)
    positions_uri: str = Field(min_length=1)
    positions_hash: str = Field(min_length=1)


class StrategyArtifact(VersionedSpec):
    """不可变、内容寻址的策略结论打包（ADR-0005 §2）。

    任何参数、依赖或代码变化 = 新 Artifact = 需重新验证。
    """

    kind: Kind = Kind.ARTIFACT
    strategy_spec: Ref
    dependencies: dict[str, str] = Field(default_factory=dict)
    research_code_commit: str = Field(min_length=7)
    research_code_tree_hash: str = Field(min_length=7)
    experiment_hashes: tuple[str, ...] = Field(min_length=1)
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


class EquivalenceCheck(Contract):
    """生产实现与 golden outputs 的一致性检查（ADR-0005 §5）。"""

    artifact_id: str = Field(min_length=1)
    production_code_hash: str = Field(min_length=7)
    signals_match: bool
    positions_match: bool
    tolerance: str | None = None

    @property
    def passed(self) -> bool:
        return self.signals_match and self.positions_match


class DeploymentRecord(Contract):
    """生产部署记录：可追溯链的终点（ADR-0005 §4）。"""

    deployment_id: str = Field(min_length=1)
    artifact_id: str = Field(min_length=1)
    production_code_hash: str = Field(min_length=7)
    config_hash: str = Field(min_length=1)
    equivalence: EquivalenceCheck

    @model_validator(mode="after")
    def _must_pass_equivalence(self) -> DeploymentRecord:
        if not self.equivalence.passed:
            raise ValueError("未通过 Equivalence Gate 的实现不得部署（ADR-0005 §4）")
        if self.equivalence.artifact_id != self.artifact_id:
            raise ValueError("Equivalence 检查与部署的 artifact_id 不一致")
        if self.equivalence.production_code_hash != self.production_code_hash:
            raise ValueError("Equivalence 检查与部署的生产实现哈希不一致")
        return self

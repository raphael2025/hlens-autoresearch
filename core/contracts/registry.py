"""契约注册表与 JSON Schema 导出（02-domain.md §3、roadmap Phase 0）。

注册表是"所有核心实体都有契约与 Schema 导出"这一验收标准的唯一来源：
新增核心实体必须登记到 `CONTRACT_MODELS`，否则契约测试会失败。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING

from core.contracts.profile_selection import (
    ExperimentMetadata,
    OosUnsealing,
    ProfileSelectionKey,
    ProfileSelectionRule,
    SelectionEntry,
)
from core.contracts.validation_profile import ValidationProfile
from core.domain.artifact import (
    DeploymentRecord,
    EquivalenceCheck,
    GoldenOutputs,
    StrategyArtifact,
)
from core.domain.base import Ref
from core.domain.research import (
    ExperimentRun,
    ExperimentSpec,
    FailureRecord,
    GateResult,
    Hypothesis,
    KnowledgeItem,
    LlmCall,
    ReproducibilityTuple,
    RetirementRecord,
    ValidationReport,
)
from core.domain.specs import (
    DatasetRef,
    EventSpec,
    FeatureSpec,
    Instrument,
    OutcomeSpec,
    RiskPolicy,
    StateSpec,
    StrategySpec,
)
from core.lifecycle.strategy import (
    AuthorizationRecord,
    ExecutionModeChange,
    LifecycleHistory,
    LifecycleTransition,
    RiskGateRecord,
)

if TYPE_CHECKING:
    from core.domain.base import Contract

__all__ = ["CONTRACT_MODELS", "export_json_schemas"]

#: 全部核心契约模型。顺序即导出顺序。
CONTRACT_MODELS: tuple[type[Contract], ...] = (
    # 基础
    Ref,
    Instrument,
    DatasetRef,
    # 研究对象规格
    FeatureSpec,
    StateSpec,
    EventSpec,
    OutcomeSpec,
    StrategySpec,
    RiskPolicy,
    # 知识与假设
    KnowledgeItem,
    Hypothesis,
    # 实验
    LlmCall,
    ReproducibilityTuple,
    ExperimentSpec,
    ExperimentRun,
    # 验证与终态记录
    GateResult,
    ValidationReport,
    FailureRecord,
    RetirementRecord,
    # 三层验证架构
    ValidationProfile,
    ProfileSelectionKey,
    SelectionEntry,
    ProfileSelectionRule,
    OosUnsealing,
    ExperimentMetadata,
    # 生命周期
    LifecycleTransition,
    LifecycleHistory,
    RiskGateRecord,
    AuthorizationRecord,
    ExecutionModeChange,
    # 研究 / 生产边界
    GoldenOutputs,
    StrategyArtifact,
    EquivalenceCheck,
    DeploymentRecord,
)


def export_json_schemas(out_dir: Path) -> dict[str, Path]:
    """把全部契约导出为 JSON Schema，返回 {模型名: 文件路径}。"""
    out_dir.mkdir(parents=True, exist_ok=True)
    written: dict[str, Path] = {}
    for model in CONTRACT_MODELS:
        schema = model.model_json_schema(mode="serialization")
        path = out_dir / f"{model.__name__}.schema.json"
        path.write_text(json.dumps(schema, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        written[model.__name__] = path
    return written


if __name__ == "__main__":  # pragma: no cover - 手动导出入口
    target = Path(__file__).resolve().parents[2] / "schemas"
    files = export_json_schemas(target)
    print(f"exported {len(files)} schemas to {target}")

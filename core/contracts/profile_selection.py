"""Profile 选择规则与 Experiment Metadata（ADR-0007 第三层）。

选择规则是确定性的、版本化的；研究者不能自选 Profile（Constitution C-A4）。
没有匹配项时**报错**，绝不回退到更宽松的 Profile。
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal

from pydantic import Field, model_validator

from core.domain.base import Contract, FrozenMapping, Kind, Ref, UtcDatetime, VersionedSpec
from core.domain.research import GateResult, LlmCall, require_unique_gate_ids
from core.domain.selection import ProfileSelection, ProfileSelectionKey
from core.errors import ProfileViolation

__all__ = [
    "ExperimentMetadata",
    "OosUnsealing",
    "ProfileSelection",
    "ProfileSelectionKey",
    "ProfileSelectionRule",
    "SelectionEntry",
]

#: `ProfileSelectionKey` / `ProfileSelection` 的定义在 `core.domain.selection`
#: （避免与复现元组形成循环导入）；此处重导出，Schema 名称与对外引用路径保持稳定。


class SelectionEntry(Contract):
    key: ProfileSelectionKey
    profile: Ref
    profile_version: str = Field(min_length=1)

    @model_validator(mode="after")
    def _profile_is_consistent(self) -> SelectionEntry:
        if self.profile.kind is not Kind.PROFILE:
            raise ValueError("profile 必须指向 profile")
        if self.profile.version != self.profile_version:
            raise ValueError(
                f"profile_version（{self.profile_version}）与 profile 引用的版本"
                f"（{self.profile.version}）不一致"
            )
        return self


class ProfileSelectionRule(VersionedSpec):
    """`(标的, 周期, 研究类别) → Profile` 的确定性映射。"""

    kind: Literal[Kind.PROFILE_SELECTION_RULE] = Kind.PROFILE_SELECTION_RULE
    entries: tuple[SelectionEntry, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _unique_keys(self) -> ProfileSelectionRule:
        keys = [entry.key.model_dump_json() for entry in self.entries]
        if len(set(keys)) != len(keys):
            raise ValueError("选择规则存在重复 key：映射必须确定唯一")
        return self

    def select(self, key: ProfileSelectionKey) -> SelectionEntry:
        """返回唯一匹配项；无匹配时抛 `ProfileViolation`（不得回退到更宽松的 Profile）。"""
        matches = [entry for entry in self.entries if entry.key == key]
        if not matches:
            raise ProfileViolation(
                f"没有匹配的 Validation Profile：{key.venue}/{key.symbol}/"
                f"{key.timeframe}/{key.research_class}；不得回退到其他 Profile"
            )
        if len(matches) > 1:  # pragma: no cover - 由 _unique_keys 保证
            raise ProfileViolation("选择规则不确定：同一 key 匹配到多个 Profile")
        return matches[0]


class OosUnsealing(Contract):
    """封存样本外的开封记录（Constitution C-S2）；不可撤销。"""

    unsealed_at: UtcDatetime
    approved_by: str = Field(min_length=1)
    family_unseal_count: int = Field(gt=0)


class ExperimentMetadata(Contract):
    """每个实验实际使用的规则版本与配置（06-experiment.md §3）。

    追加式、不可修改；与 ValidationReport 一起构成审计证据。
    """

    experiment_hash: str = Field(min_length=1)
    constitution_version: str = Field(min_length=1)
    validation_profile_version: str = Field(min_length=1)
    validation_profile_hash: str = Field(min_length=1)
    profile_selection_rule_version: str = Field(min_length=1)
    profile_selection_key: ProfileSelectionKey
    hypothesis_family_id: str = Field(min_length=1)
    trial_index: int = Field(gt=0)
    family_trial_count: int = Field(gt=0)
    declared_research_class: str = Field(min_length=1)
    realized_holding_stats: FrozenMapping[str, float] = Field(
        default_factory=dict, validate_default=True
    )
    gate_results: tuple[GateResult, ...] = ()
    oos_unsealing: OosUnsealing | None = None
    llm_calls: tuple[LlmCall, ...] = ()
    trace_id: str | None = None
    recorded_at: UtcDatetime = Field(default_factory=lambda: datetime.now(UTC))

    @model_validator(mode="after")
    def _gate_ids_are_unique(self) -> ExperimentMetadata:
        """同一份元数据内 `gate_id` 不得重复（ADR-0013 D-19.2）。"""
        require_unique_gate_ids(self.gate_results, "ExperimentMetadata.gate_results")
        return self

    @model_validator(mode="after")
    def _trial_index_within_count(self) -> ExperimentMetadata:
        if self.trial_index > self.family_trial_count:
            raise ValueError("trial_index 不得大于 family_trial_count（失败尝试也必须计数）")
        if self.declared_research_class != self.profile_selection_key.research_class:
            raise ValueError(
                "预登记的研究类别与 Profile 选择输入不一致（不得换到更宽松的 Profile）"
            )
        return self

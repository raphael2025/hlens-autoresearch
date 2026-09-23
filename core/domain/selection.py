"""Profile 选择的共享值对象（ADR-0007 第三层、ADR-0009 §5）。

放在 domain 层而不是 `core/contracts/profile_selection.py`，是为了让复现元组
（`core/domain/research.py`）与选择规则契约都能引用它们而**不形成循环导入**。
`core/contracts/profile_selection.py` 继续对外重导出同名类型，Schema 名称保持稳定。
"""

from __future__ import annotations

from pydantic import Field, model_validator

from core.domain.base import SHA256_PATTERN, Contract, Kind, Ref

__all__ = ["ProfileSelection", "ProfileSelectionKey"]


class ProfileSelectionKey(Contract):
    """选择输入：标的、周期、预登记的研究类别。"""

    venue: str = Field(min_length=1)
    symbol: str = Field(min_length=1)
    timeframe: str = Field(min_length=1)
    research_class: str = Field(min_length=1)


class ProfileSelection(Contract):
    """实验实际使用的 Profile 选择依据（ADR-0009 §5）。

    必须同时给出**唯一的规则引用**、**该规则的内容哈希**与**选择输入**：
    只有规则版本不足以在事后重建"当时按哪条规则、按什么输入选中了哪个 Profile"。
    取消空默认值——缺绑定的实验不得进入 Validation。
    """

    selection_rule: Ref
    selection_rule_hash: str = Field(pattern=SHA256_PATTERN)
    key: ProfileSelectionKey

    @model_validator(mode="after")
    def _rule_kind(self) -> ProfileSelection:
        if self.selection_rule.kind is not Kind.PROFILE_SELECTION_RULE:
            raise ValueError("selection_rule 必须指向 profile_selection_rule")
        return self

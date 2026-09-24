"""Profile 选择的共享值对象（ADR-0007 第三层、ADR-0009 §5）。

放在 domain 层而不是 `core/contracts/profile_selection.py`，是为了让复现元组
（`core/domain/research.py`）与选择规则契约都能引用它们而**不形成循环导入**。
`core/contracts/profile_selection.py` 继续对外重导出同名类型，Schema 名称保持稳定。
"""

from __future__ import annotations

from pydantic import Field, model_validator

from core.domain.base import RESEARCH_CLASS_PATTERN, SHA256_PATTERN, Contract, Kind, Ref

__all__ = ["ProfileSelection", "ProfileSelectionKey", "SelectionKeyIdentity"]

#: Profile 选择键身份 `(venue, symbol, timeframe, research_class)`（ADR-0018 §D-26.1）。
type SelectionKeyIdentity = tuple[str, str, str, str]


class ProfileSelectionKey(Contract):
    """选择输入：标的、周期、预登记的研究类别。"""

    venue: str = Field(min_length=1)
    symbol: str = Field(min_length=1)
    timeframe: str = Field(min_length=1)
    research_class: str = Field(pattern=RESEARCH_CLASS_PATTERN)

    def selection_identity(self) -> SelectionKeyIdentity:
        """选择键身份 `(venue, symbol, timeframe, research_class)`（ADR-0018 §D-26.1）。

        不含 Contract 信封 `schema_version`：同一业务键不能靠改写信封版本选出另一个 Profile。
        `venue` / `symbol` / `timeframe` 是区分大小写的精确不透明值，**不做**大小写折叠或
        Unicode 规范化（§D-26.4）。结构相等与 `content_hash()` 保持不变（§D-26.7）。
        """
        return (self.venue, self.symbol, self.timeframe, self.research_class)


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

"""`KnowledgeProvider`：公开研究知识检索的 Protocol 与 DTO（ADR-0034；Phase 0.5）。

对应 docs/architecture/05-plugin.md §3（"检索知识：query → KnowledgeItem[]（带出处）；可不确定（需
记录）"）与 docs/research/knowledge-base.md。本模块只定义可执行 Protocol、可序列化 DTO 与它们在契约
层可证明的不变量；**不含任何 Provider 实现**（首个实现在 `plugins/knowledge/`）。

| 成员 | 输入 → 输出 |
|---|---|
| `descriptor` | → `KnowledgeProviderDescriptor`（`name@version`、是否确定性、声明的来源） |
| `search` | `KnowledgeQuery` → `KnowledgeResult` |

**知识不是结论**：结果中的每个 `KnowledgeItem` 都是待检验主张（`status` 默认 `unverified`）；
检索结果不得被当作验证证据，也不得进入任何裁决（Constitution、05-plugin.md §3）。

**出处必填**：结果中的每个条目都必须带非空的 `source` 与 `license`（无出处条目为零，roadmap P0.5
验收标准）；`KnowledgeResult` 构造时强制检查。

**可复现**：`query_hash` 绑定查询内容；确定性 Provider 对同一查询给出同一 `result_hash`，非确定性
Provider（如外部检索）必须在 descriptor 中声明 `deterministic=False`，其结果仍记录 `result_hash`
以便审计。
"""

from __future__ import annotations

from typing import Protocol

from pydantic import Field, model_validator

from core.domain.base import (
    NAME_PATTERN,
    SEMVER_PATTERN,
    ContentHash,
    Contract,
    PluginKey,
    content_hash,
)
from core.domain.research import EvidenceLevel, KnowledgeItem, KnowledgeStatus

__all__ = [
    "EVIDENCE_ORDER",
    "KnowledgeProvider",
    "KnowledgeProviderDescriptor",
    "KnowledgeProviderError",
    "KnowledgeQuery",
    "KnowledgeResult",
]

#: 证据等级由弱到强（E0 传闻 … E4 多市场复现），供 `evidence_at_least` 过滤。
EVIDENCE_ORDER: tuple[EvidenceLevel, ...] = (
    EvidenceLevel.E0_ANECDOTE,
    EvidenceLevel.E1_EXAMPLE,
    EvidenceLevel.E2_IN_SAMPLE,
    EvidenceLevel.E3_OUT_OF_SAMPLE,
    EvidenceLevel.E4_REPLICATED,
)


class KnowledgeProviderError(Exception):
    """Provider 无法诚实地回答（来源不可读、条目缺出处等）；fail closed。"""


class KnowledgeQuery(Contract):
    """一次知识检索。

    - `terms`：全部须出现在条目 `name` / `claim` / `conditions` 中（不区分大小写，AND 语义）；
    - `name_prefix`：库前缀，如 `strategy_`、`factor_`、`state_`（空 = 不限）；
    - `evidence_at_least`：只返回证据等级不低于该值的条目（空 = 不限）；
    - `statuses`：只返回这些状态（空 = 不限）；
    - `limit`：结果上限。
    """

    terms: tuple[str, ...] = ()
    name_prefix: str = ""
    evidence_at_least: EvidenceLevel | None = None
    statuses: tuple[KnowledgeStatus, ...] = ()
    limit: int = Field(default=50, ge=1, le=1000, strict=True)

    @model_validator(mode="after")
    def _terms_non_empty(self) -> KnowledgeQuery:
        if any(not term.strip() for term in self.terms):
            raise ValueError("检索词不得为空白")
        return self


class KnowledgeProviderDescriptor(Contract):
    """KnowledgeProvider 的身份与声明（05-plugin.md §1 / §3）。"""

    name: str = Field(pattern=NAME_PATTERN)
    version: str = Field(pattern=SEMVER_PATTERN)
    deterministic: bool
    #: 该 Provider 检索的来源说明（如本地条目库路径的逻辑名），非空。
    sources: tuple[str, ...] = Field(min_length=1)

    @property
    def plugin_key(self) -> str:
        return f"{self.name}@{self.version}"


class KnowledgeResult(Contract):
    """一次检索的结果：条目按 `name`、`version` 规范排序；每条都有出处与许可。"""

    query_hash: ContentHash
    provider: PluginKey
    items: tuple[KnowledgeItem, ...]
    result_hash: ContentHash

    @model_validator(mode="after")
    def _provenance_and_hash(self) -> KnowledgeResult:
        for item in self.items:
            if not item.source.strip() or not item.license.strip():
                raise ValueError(f"知识条目 {item.name}@{item.version} 缺出处或许可")
        keys = [(item.name, item.version) for item in self.items]
        if keys != sorted(keys) or len(set(keys)) != len(keys):
            raise ValueError("items 必须按 (name, version) 唯一且升序")
        if self.result_hash != _result_hash(self.query_hash, self.provider, self.items):
            raise ValueError("result_hash 与结果内容不符")
        return self

    @classmethod
    def build(
        cls, query: KnowledgeQuery, provider: str, items: tuple[KnowledgeItem, ...]
    ) -> KnowledgeResult:
        ordered = tuple(sorted(items, key=lambda item: (item.name, item.version)))
        query_hash = query.content_hash()
        return cls(
            query_hash=query_hash,
            provider=provider,
            items=ordered,
            result_hash=_result_hash(query_hash, provider, ordered),
        )


def _result_hash(query_hash: str, provider: str, items: tuple[KnowledgeItem, ...]) -> str:
    return content_hash(
        {
            "query_hash": query_hash,
            "provider": provider,
            "items": [item.content_hash() for item in items],
        }
    )


class KnowledgeProvider(Protocol):
    """知识检索（ADR-0034）。语义见模块文档。"""

    @property
    def descriptor(self) -> KnowledgeProviderDescriptor:
        """Provider 身份与声明；实例生命周期内不变。"""
        ...

    def search(self, query: KnowledgeQuery) -> KnowledgeResult:
        """检索；来源不可读或条目不合规 → `KnowledgeProviderError`。"""
        ...

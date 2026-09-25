"""`LLMProvider`：结构化生成的 Protocol 与 DTO（ADR-0040；Phase 7）。

LLM 只产出**数据**，永不裁决（09-security.md §3、Constitution）；
每次调用都登记为 `LlmCall`（ADR-0016），
输出必须通过调用方给定的结构校验后才能使用，由 LLM 产出的假设须经人工审阅才能登记（roadmap P7）。
"""

from __future__ import annotations

from typing import Any, Protocol

from pydantic import Field

from core.domain.base import NAME_PATTERN, SEMVER_PATTERN, Contract, FrozenMapping
from core.domain.research import LlmCall

__all__ = ["LlmProviderDescriptor", "LlmRequest", "LlmResponse", "LLMProvider"]


class LlmRequest(Contract):
    #: 提示文本（外部内容只作数据嵌入 `input`，不得拼进指令，09-security.md §3）。
    prompt: str = Field(min_length=1)
    input: FrozenMapping[str, Any]
    #: 期望输出的 JSON Schema（调用方另行以其结构模型校验）。
    output_schema: FrozenMapping[str, Any]


class LlmResponse(Contract):
    output: FrozenMapping[str, Any]
    call: LlmCall


class LlmProviderDescriptor(Contract):
    name: str = Field(pattern=NAME_PATTERN)
    version: str = Field(pattern=SEMVER_PATTERN)
    model: str = Field(min_length=1)
    deterministic: bool
    #: 是否访问网络（05-plugin.md §6：LLMProvider 可以联网，但必须声明）。
    network: bool

    @property
    def plugin_key(self) -> str:
        return f"{self.name}@{self.version}"


class LLMProvider(Protocol):
    """结构化生成（ADR-0040）。"""

    @property
    def descriptor(self) -> LlmProviderDescriptor: ...

    def complete(self, request: LlmRequest) -> LlmResponse: ...

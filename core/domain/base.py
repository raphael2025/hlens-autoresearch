"""领域基础类型：标识、版本化、不可变契约基类。

对应 docs/architecture/02-domain.md §1、§3。本模块只依赖标准库与 Pydantic。
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import UTC, datetime
from enum import StrEnum
from typing import Annotated, Any, Self

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, field_validator

__all__ = [
    "CONTRACT_SCHEMA_VERSION",
    "Contract",
    "Kind",
    "Ref",
    "UtcDatetime",
    "VersionedSpec",
    "canonical_json",
    "content_hash",
]

#: 本次发布的契约 Schema 版本（SemVer）。破坏性变更 = major + ADR。
CONTRACT_SCHEMA_VERSION = "1.0.0"

SEMVER_PATTERN = r"^\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?$"
NAME_PATTERN = r"^[a-z][a-z0-9_]*$"
REF_PATTERN = re.compile(r"^(?P<kind>[a-z_]+):(?P<name>[a-z][a-z0-9_]*)@(?P<version>.+)$")


def _require_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("时间必须带时区（UTC）；禁止 naive datetime")
    return value.astimezone(UTC)


#: 所有时间戳都是 UTC（docs/architecture/03-data.md §4）。naive datetime 会被拒绝。
UtcDatetime = Annotated[datetime, AfterValidator(_require_utc)]


class Kind(StrEnum):
    """可版本化对象的类型（02-domain.md §1）。"""

    DATASET = "dataset"
    REPRESENTATION = "representation"
    FEATURE = "feature"
    STATE = "state"
    EVENT = "event"
    OUTCOME = "outcome"
    STRATEGY = "strategy"
    RISK = "risk"
    COST_MODEL = "cost_model"
    EXPERIMENT = "experiment"
    HYPOTHESIS = "hypothesis"
    KNOWLEDGE = "knowledge"
    ARTIFACT = "artifact"
    PROFILE = "profile"
    PROFILE_SELECTION_RULE = "profile_selection_rule"


def canonical_json(payload: Any) -> str:
    """规范化 JSON：排序键、无多余空白、非 ASCII 原样保留。"""
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)


def content_hash(payload: Any) -> str:
    """规范化 JSON 的 SHA-256（02-domain.md §1）。"""
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


class Contract(BaseModel):
    """所有契约模型的基类：不可变、禁止未声明字段、带 schema_version。"""

    model_config = ConfigDict(frozen=True, extra="forbid", str_strip_whitespace=True)

    schema_version: str = Field(default=CONTRACT_SCHEMA_VERSION, pattern=SEMVER_PATTERN)

    def content_hash(self) -> str:
        """按语义内容计算哈希；排除 `created_at` 等非语义字段。"""
        payload = self.model_dump(mode="json", exclude=self._non_semantic_fields())
        return content_hash(payload)

    @classmethod
    def _non_semantic_fields(cls) -> set[str]:
        return {"created_at", "content_hash"}


class Ref(Contract):
    """对象引用：`{kind}:{name}@{version}`。"""

    kind: Kind
    name: str = Field(pattern=NAME_PATTERN)
    version: str = Field(pattern=SEMVER_PATTERN)

    @classmethod
    def parse(cls, raw: str) -> Self:
        match = REF_PATTERN.match(raw)
        if match is None:
            raise ValueError(f"非法引用格式：{raw!r}，应为 kind:name@version")
        return cls(
            kind=Kind(match.group("kind")),
            name=match.group("name"),
            version=match.group("version"),
        )

    def __str__(self) -> str:
        return f"{self.kind.value}:{self.name}@{self.version}"


class VersionedSpec(Contract):
    """可版本化对象的公共字段（02-domain.md §1）。

    已发布版本不可变：修改 = 新版本。
    """

    kind: Kind
    name: str = Field(pattern=NAME_PATTERN)
    version: str = Field(pattern=SEMVER_PATTERN)
    created_at: UtcDatetime = Field(default_factory=lambda: datetime.now(UTC))
    lineage: tuple[Ref, ...] = ()

    @property
    def ref(self) -> Ref:
        return Ref(kind=self.kind, name=self.name, version=self.version)

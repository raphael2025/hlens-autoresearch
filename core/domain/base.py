"""领域基础类型：标识、版本化、不可变契约基类。

对应 docs/architecture/02-domain.md §1、§3。本模块只依赖标准库与 Pydantic。
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable, Iterator, Mapping, Sequence
from datetime import UTC, datetime
from enum import StrEnum
from typing import Annotated, Any, Self, get_args

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    GetCoreSchemaHandler,
    PlainSerializer,
    field_validator,
)

__all__ = [
    "CONTRACT_SCHEMA_MAJOR",
    "CONTRACT_SCHEMA_VERSION",
    "SHA256_PATTERN",
    "Contract",
    "FrozenMapping",
    "Kind",
    "Ref",
    "UtcDatetime",
    "VersionedSpec",
    "canonical_json",
    "content_hash",
    "validate_plugin_version_hashes",
    "validate_ref_keyed_hashes",
]

#: 本次发布的契约 Schema 版本（SemVer）。破坏性变更 = major + ADR。
#: 2.0.0 由 ADR-0008 与 ADR-0009 共同定义；与 1.x 的内容哈希**不可比较**。
CONTRACT_SCHEMA_VERSION = "2.0.0"

#: 当前实现能够作为**模型**校验的 major。其他 major 一律拒绝（旧载荷走 core/compat）。
CONTRACT_SCHEMA_MAJOR = 2

SEMVER_PATTERN = r"^\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?$"
NAME_PATTERN = r"^[a-z][a-z0-9_]*$"
#: 内容哈希一律是 64 位小写十六进制 SHA-256。
SHA256_PATTERN = r"^[0-9a-f]{64}$"
REF_PATTERN = re.compile(r"^(?P<kind>[a-z_]+):(?P<name>[a-z][a-z0-9_]*)@(?P<version>.+)$")
_SEMVER_RE = re.compile(SEMVER_PATTERN)
_SHA256_RE = re.compile(SHA256_PATTERN)
#: `plugin_versions` 的键：`name@semver`（06-experiment.md §2）。
_PLUGIN_KEY_RE = re.compile(rf"^(?P<name>[a-z][a-z0-9_]*)@(?P<version>{SEMVER_PATTERN[1:-1]})$")


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


def _to_builtin(value: Any) -> Any:
    """把只读视图还原为普通容器（序列化出口，wire shape 保持 object）。

    序列的具体类型保持不变，交给声明类型自身的序列化器处理。
    """
    if isinstance(value, Mapping):
        return {key: _to_builtin(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return tuple(_to_builtin(item) for item in value)
    if isinstance(value, list):
        return [_to_builtin(item) for item in value]
    return value


def _freeze_value(value: Any) -> Any:
    """递归冻结：映射 → 只读视图，序列 → tuple。嵌套 Contract 自身已 frozen。"""
    if isinstance(value, FrozenMapping):
        return value
    if isinstance(value, Mapping):
        return FrozenMapping({key: _freeze_value(item) for key, item in value.items()})
    if isinstance(value, str | bytes | BaseModel):
        return value
    if isinstance(value, Sequence):
        return tuple(_freeze_value(item) for item in value)
    return value


class FrozenMapping[K, V](Mapping[K, V]):
    """契约映射字段的只读表示（ADR-0008 决策 1）。

    对外只有 `collections.abc.Mapping` 语义：没有 `__setitem__` / `__delitem__`，
    也没有 `update` / `pop` / `popitem` / `clear` / `setdefault` / `__ior__`。
    构造时复制输入并递归冻结内层，因此与调用方保留的原引用不共享状态。

    **诚实边界**（ADR-0008 决策 2）：这是契约使用层面的只读性，用于阻止误用与意外修改，
    **不**承诺抵御同进程内直接操作 `_data` 的恶意 Python。Python 的 `hash()` 与本项目的
    `content_hash` 是两件不同的事，本类型刻意不可 `hash()`。
    """

    __slots__ = ("_data",)

    def __init__(self, data: Mapping[K, V] | Iterable[tuple[K, V]] = ()) -> None:
        self._data: dict[K, V] = {key: _freeze_value(value) for key, value in dict(data).items()}

    def __getitem__(self, key: K) -> V:
        return self._data[key]

    def __iter__(self) -> Iterator[K]:
        return iter(self._data)

    def __len__(self) -> int:
        return len(self._data)

    def __repr__(self) -> str:
        return f"{type(self).__name__}({self._data!r})"

    def __eq__(self, other: object) -> bool:
        if isinstance(other, Mapping):
            return dict(self._data) == dict(other)
        return NotImplemented

    #: 内容可能不可 hash；且 Python 的 hash() 不是契约身份（ADR-0008 决策 2）。
    __hash__ = None  # type: ignore[assignment]

    @classmethod
    def _validate(cls, value: Mapping[K, V]) -> FrozenMapping[K, V]:
        return cls(value)

    @classmethod
    def __get_pydantic_core_schema__(cls, source_type: Any, handler: GetCoreSchemaHandler) -> Any:
        """以 `dict[K, V]` 为校验与序列化基础：JSON wire shape 与 Schema 保持 `object`。"""
        args = get_args(source_type)
        key_type, value_type = args if len(args) == 2 else (Any, Any)
        builtin = dict[key_type, value_type]  # type: ignore[valid-type]
        return handler.generate_schema(
            Annotated[
                builtin,
                AfterValidator(cls._validate),
                PlainSerializer(_to_builtin, return_type=builtin),
            ]
        )


def canonical_json(payload: Any) -> str:
    """规范化 JSON：排序键、无多余空白、非 ASCII 原样保留（02-domain.md §3）。

    `allow_nan=False`：NaN / ±Infinity 不是合法 JSON，参与内容身份前必须被拒绝，
    不得先转成 null 再哈希。未知类型直接抛 `TypeError`，不做静默 `str()` 兜底。
    这是 v2 明确的 Python/JSON 规范，不宣称跨语言浮点规范化保证。
    """
    return json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    )


def content_hash(payload: Any) -> str:
    """规范化 JSON 的 SHA-256（02-domain.md §1）。"""
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


class Contract(BaseModel):
    """所有契约模型的基类：不可变、禁止未声明字段、带 schema_version。"""

    model_config = ConfigDict(frozen=True, extra="forbid", str_strip_whitespace=True)

    schema_version: str = Field(default=CONTRACT_SCHEMA_VERSION, pattern=SEMVER_PATTERN)

    def content_hash(self) -> str:
        """按语义内容计算哈希；排除逐模型声明的非语义字段。"""
        payload = self.model_dump(mode="json", exclude=self._non_semantic_fields())
        return content_hash(payload)

    @field_validator("schema_version")
    @classmethod
    def _supported_major(cls, value: str) -> str:
        """未知 major 一律拒绝；同 major 的更高 minor 可以读取（ADR-0008 §6、ADR-0009 §7）。

        旧 major 的载荷只能经 `core.compat` 的只读入口读取，不能作为本版本的模型使用，
        也不因此获得登记 / 晋升资格。
        """
        major = int(value.split(".", 1)[0])
        if major != CONTRACT_SCHEMA_MAJOR:
            raise ValueError(
                f"不支持的契约 major：{value}（当前为 {CONTRACT_SCHEMA_VERSION}）；"
                "旧 major 请使用 core.compat 的只读入口"
            )
        return value

    @classmethod
    def _non_semantic_fields(cls) -> set[str]:
        """内容哈希的排除表：**逐模型显式声明**（ADR-0008 决策 3）。

        基类默认只排除 `created_at`。**不得**在此加入全局的 `*_id` / `recorded_at` /
        `occurred_at` 规则：`run_id`、`subject`、授权与审计时间都可能是语义。
        """
        return {"created_at"}


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


def _require_sha256(label: str, key: str, value: str) -> None:
    if _SHA256_RE.match(value) is None:
        raise ValueError(f"{label}[{key!r}] 必须是 64 位小写十六进制 SHA-256 内容哈希：{value!r}")


def validate_plugin_version_hashes(mapping: Mapping[str, str], label: str) -> None:
    """`name@semver → content_hash`（ADR-0009 §5；06-experiment.md §2）。"""
    for key, value in mapping.items():
        if _PLUGIN_KEY_RE.match(key) is None:
            raise ValueError(f"{label} 的键必须是 name@semver 规范串：{key!r}")
        _require_sha256(label, key, value)


def validate_ref_keyed_hashes(mapping: Mapping[str, str], label: str) -> dict[str, Ref]:
    """`kind:name@semver → content_hash`；返回解析后的引用表。

    键必须是 `Ref` 的**规范字符串**（`str(ref)`），避免 Feature / State 等同名对象混淆。
    """
    parsed: dict[str, Ref] = {}
    for key, value in mapping.items():
        match = REF_PATTERN.match(key)
        if (
            match is None
            or match.group("kind") not in set(Kind)
            or (_SEMVER_RE.match(match.group("version")) is None)
        ):
            raise ValueError(f"{label} 的键必须是 kind:name@semver 规范串：{key!r}")
        ref = Ref.parse(key)
        if str(ref) != key:  # pragma: no cover - REF_PATTERN 已保证规范形式
            raise ValueError(f"{label} 的键不是规范形式：{key!r}")
        _require_sha256(label, key, value)
        parsed[key] = ref
    return parsed

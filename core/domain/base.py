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
    "GIT_OID_PATTERN",
    "PLUGIN_KEY_PATTERN",
    "REF_KEY_PATTERN",
    "SEMVER_PATTERN",
    "SHA256_PATTERN",
    "Contract",
    "ContentHash",
    "FrozenMapping",
    "GitCodeRevision",
    "GitOid",
    "Kind",
    "PluginKey",
    "Ref",
    "RefKey",
    "UtcDatetime",
    "VersionedSpec",
    "canonical_json",
    "content_hash",
    "parse_semver",
    "validate_ref_keyed_hashes",
]

#: 本次发布的契约 Schema 版本（SemVer）。破坏性变更 = major + ADR。
#: 2.0.0 由 ADR-0008 与 ADR-0009 共同定义；与 1.x 的内容哈希**不可比较**。
CONTRACT_SCHEMA_VERSION = "2.0.0"

#: 当前实现能够作为**模型**校验的 major。其他 major 一律拒绝（旧载荷走 core/compat）。
CONTRACT_SCHEMA_MAJOR = 2

# ---------------------------------------------------------------------------------------
# 规范版本语法（ADR-0010 §D-14）
#
# 全项目**唯一**的版本语法 = 完整 SemVer 2.0.0，且只接受 ASCII：
# 刻意不用 `\d`（在 Python 中会匹配 Unicode 数字，例如 `١`），一律用显式 `[0-9]`。
# core 的 major/minor/patch 禁止前导零；prerelease 的数字标识符同样禁止前导零，
# 也不允许空标识符；build metadata（`+...`）被正确支持。
# ---------------------------------------------------------------------------------------

#: 数字标识符：0 或不带前导零的正整数。
_NUM_ID = r"(?:0|[1-9][0-9]*)"
#: prerelease 标识符：数字标识符，或含字母 / 连字符的字母数字标识符（不得为空）。
_PRE_ID = r"(?:0|[1-9][0-9]*|[0-9]*[a-zA-Z-][0-9a-zA-Z-]*)"
#: build metadata 标识符：非空字母数字连字符。
_BUILD_ID = r"[0-9a-zA-Z-]+"
_PRERELEASE = rf"(?:-{_PRE_ID}(?:\.{_PRE_ID})*)?"
_BUILD = rf"(?:\+{_BUILD_ID}(?:\.{_BUILD_ID})*)?"

#: `major.minor.patch`，无前导零。
SEMVER_CORE_PATTERN = rf"{_NUM_ID}\.{_NUM_ID}\.{_NUM_ID}"
#: 版本号主体（不带锚点），供拼接进复合键的 pattern 使用。
_VERSION_BODY = rf"{SEMVER_CORE_PATTERN}{_PRERELEASE}{_BUILD}"
#: 完整 SemVer 2.0.0（带锚点）。全部为非捕获组，可安全用于 Pydantic / JSON Schema。
SEMVER_PATTERN = rf"^{_VERSION_BODY}$"

NAME_PATTERN = r"^[a-z][a-z0-9_]*$"
_NAME_BODY = r"[a-z][a-z0-9_]*"
#: 内容哈希一律是 64 位小写十六进制 SHA-256。
SHA256_PATTERN = r"^[0-9a-f]{64}$"

# ---------------------------------------------------------------------------------------
# Git 对象 ID（ADR-0015 §D-21.2）
#
# Git OID 与本项目的内容哈希是**两套命名空间**，刻意不共用一个类型：前者由 Git 生成并指向
# Git 对象，后者是规范化 JSON 的 SHA-256。长度只接受 40（SHA-1）或 64（SHA-256）——
# 短 SHA 在仓库增长后会碰撞，也无法唯一定位对象，因此不是可审计的代码身份。
# 只接受小写：Git 自身输出小写，混用大小写会让"同一个 commit"出现两种写法。
# 这**只是格式约束**：不访问任何仓库，也不校验对象是否存在（属 Runner 与打包器）。
# ---------------------------------------------------------------------------------------

#: Git 对象 ID：40 位（SHA-1）或 64 位（SHA-256）小写十六进制。
GIT_OID_PATTERN = r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$"

REF_PATTERN = re.compile(rf"^(?P<kind>[a-z_]+):(?P<name>{_NAME_BODY})@(?P<version>.+)$")
#: 解析用：与 `SEMVER_PATTERN` 由同一组件拼成，只是多了命名捕获组。
#: major 必须从**这里**读取，不得用宽松的 `int(value.split(".")[0])`。
_SEMVER_PARSE_RE = re.compile(
    rf"^(?P<major>{_NUM_ID})\.(?P<minor>{_NUM_ID})\.(?P<patch>{_NUM_ID}){_PRERELEASE}{_BUILD}$"
)
_SHA256_RE = re.compile(SHA256_PATTERN)


def parse_semver(value: str) -> re.Match[str]:
    """按唯一的规范 SemVer 语法解析；不合法直接报错。

    调用方从返回的 `Match` 读取 `major` / `minor` / `patch` 分组，
    避免"校验用一套、解析用另一套"的漂移（ADR-0010 §D-14）。
    """
    match = _SEMVER_PARSE_RE.fullmatch(value)
    if match is None:
        raise ValueError(f"非法版本号：{value!r}；必须是 ASCII 的完整 SemVer 2.0.0")
    return match


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


#: 复合键的机读 pattern（ADR-0010 §D-16）。**运行时校验与导出的 JSON Schema 用的是同一个
#: 字符串**：键类型直接标注在字段上，Pydantic 既用它校验、又把它写进 `patternProperties`，
#: 因此不存在"运行时一套、Schema 另一套"的漂移。
_KIND_ALTERNATION = "|".join(sorted(kind.value for kind in Kind))
#: `plugin_versions` 的键：`name@semver`（06-experiment.md §2）。
PLUGIN_KEY_PATTERN = rf"^{_NAME_BODY}@{_VERSION_BODY}$"
#: 依赖表的键：`Ref` 的规范串 `kind:name@semver`，kind 必须是已知取值。
REF_KEY_PATTERN = rf"^(?:{_KIND_ALTERNATION}):{_NAME_BODY}@{_VERSION_BODY}$"

_REF_KEY_RE = re.compile(REF_KEY_PATTERN)

#: 标注类型：直接把格式约束带进字段声明。
PluginKey = Annotated[str, Field(pattern=PLUGIN_KEY_PATTERN)]
RefKey = Annotated[str, Field(pattern=REF_KEY_PATTERN)]
ContentHash = Annotated[str, Field(pattern=SHA256_PATTERN)]
#: Git 对象 ID（40 / 64 位小写十六进制）；与 `ContentHash` 是两个不同的命名空间。
GitOid = Annotated[str, Field(pattern=GIT_OID_PATTERN)]


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
    def __get_pydantic_json_schema__(cls, schema: Any, handler: Any) -> Any:
        """键有格式约束时，补上 `additionalProperties: false`。

        Pydantic 会把受约束的键写成 `patternProperties`，但默认不禁止其它键；
        运行时是禁止的，所以这里把 Schema 收紧到与运行时一致（ADR-0010 §D-16）。
        """
        json_schema: dict[str, Any] = handler(schema)
        if "patternProperties" in json_schema:
            json_schema.setdefault("additionalProperties", False)
        return json_schema

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
    """所有契约模型的基类：不可变、禁止未声明字段、拒绝非法浮点数、带 schema_version。

    `allow_inf_nan=False` 是 ADR-0013 §D-20.1 的**统一机制**：它作用于本模型下**全部**
    浮点校验器——标量字段、序列元素、映射的键与值、`Annotated` 别名，以及 Python 与
    `model_validate_json` 两条入口。嵌套契约自身也是 `Contract`，因此同样继承该配置。
    不逐字段重复声明 `allow_inf_nan`，避免新增字段时漏配；覆盖面由
    `tests/test_deterministic_validation.py` 对全部注册契约的 core schema 做审计。

    这与 `canonical_json(allow_nan=False)` 是**同一条规则的两道关口**，不是互相替代：
    校验阶段挡住非法数值的进入，序列化 / 哈希阶段再挡一次，且任何一处都**不得**把
    NaN / ±Infinity 转成 `null`（ADR-0008 决策 4）。
    """

    model_config = ConfigDict(
        frozen=True, extra="forbid", str_strip_whitespace=True, allow_inf_nan=False
    )

    schema_version: str = Field(default=CONTRACT_SCHEMA_VERSION, pattern=SEMVER_PATTERN)

    def content_hash(self) -> str:
        """按语义内容计算哈希；排除逐模型声明的非语义字段。"""
        payload = self.model_dump(mode="json", exclude=self._non_semantic_fields())
        return content_hash(payload)

    def model_copy(self, *, update: Mapping[str, Any] | None = None, deep: bool = False) -> Self:
        """复制契约；**带 `update` 时重新走完整校验**（ADR-0010 §D-13）。

        Pydantic 原生的 `model_copy(update=...)` 直接写入 `__dict__`，会绕过全部校验：
        可以塞进可写 `dict`、与调用方共享别名、错误的 `kind`、缺失的依赖绑定或空 `run_id`。
        这里改为重新构造并校验同一具体模型类型，使公开的复制更新路径与正常构造等价。

        无 `update` 的普通 / 深复制保持 Pydantic 行为（输入已经是校验过的实例）。

        **边界**：`model_construct()` 是 Pydantic 面向**可信数据**的低层逃生口，
        它仍然不做校验；本项目不把它当作受支持的外部载荷入口，也不声称它是安全的。
        """
        if not update:
            return super().model_copy(deep=deep)
        payload: dict[str, Any] = {**self.__dict__, **dict(update)}
        return type(self).model_validate(payload)

    @field_validator("schema_version")
    @classmethod
    def _supported_major(cls, value: str) -> str:
        """未知 major 一律拒绝（ADR-0008 §6、ADR-0009 §7、ADR-0010 §D-14）。

        同 major 的更高 minor **版本号可识别**，但这不是前向兼容承诺：
        载荷里出现当前实现未知的字段仍然 fail closed（`extra="forbid"`）。
        旧 major 的载荷只能经 `core.compat` 的只读入口读取，不能作为本版本的模型使用，
        也不因此获得登记 / 晋升资格。
        """
        major = int(parse_semver(value).group("major"))
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


class GitCodeRevision(Contract):
    """一份代码的 Git 身份：commit 与 tree 两个对象 ID（ADR-0015 §D-21.2）。

    ADR-0005 §3 把生产代码身份定义为"commit + tree hash"的复合概念，因此它不是一个
    字符串槽位：单独的 commit 说明"哪次提交"，tree 说明"实际内容是什么"，
    两者分开才能区分"同一 commit 的不同工作区内容"。

    因为是值对象，相等性比较是**结构化**比较：两处生产代码身份是否一致，比较的是
    `(commit_oid, tree_oid)` 这一对，而不是可能只改了一半的字符串。

    **诚实边界**：只约束格式。commit / tree 是否真的存在于某个仓库、工作区是否干净、
    tree 是否真的属于该 commit，都需要访问 Git，属 Runner 与打包器（ADR-0015 运行时延期义务）。
    """

    commit_oid: GitOid
    tree_oid: GitOid


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


def validate_ref_keyed_hashes(mapping: Mapping[str, str], label: str) -> dict[str, Ref]:
    """`kind:name@semver → content_hash`；返回解析后的引用表。

    键值的**格式**由字段上的 `RefKey` / `ContentHash` 标注类型负责（同一 pattern 也进了
    JSON Schema）。这里只做解析与形式复核，供覆盖规则使用。
    """
    parsed: dict[str, Ref] = {}
    for key, value in mapping.items():
        if _REF_KEY_RE.fullmatch(key) is None:
            raise ValueError(f"{label} 的键必须是 kind:name@semver 规范串：{key!r}")
        if _SHA256_RE.fullmatch(value) is None:
            raise ValueError(f"{label}[{key!r}] 必须是 64 位小写十六进制 SHA-256：{value!r}")
        parsed[key] = Ref.parse(key)
    return parsed

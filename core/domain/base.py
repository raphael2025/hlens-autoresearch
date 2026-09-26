"""领域基础类型：标识、版本化、不可变契约基类。

对应 docs/architecture/02-domain.md §1、§3。本模块只依赖标准库与 Pydantic。
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
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
    model_validator,
)

__all__ = [
    "CONTRACT_SCHEMA_MAJOR",
    "CONTRACT_SCHEMA_VERSION",
    "PUBLISHED_CONTRACT_SCHEMA_VERSIONS",
    "GIT_OID_PATTERN",
    "PLUGIN_KEY_PATTERN",
    "REF_KEY_PATTERN",
    "RESEARCH_CLASS_PATTERN",
    "SEMVER_PATTERN",
    "SHA256_PATTERN",
    "ContentBlobRef",
    "ContentHash",
    "Contract",
    "FrozenMapping",
    "GitCodeIdentity",
    "GitCodeRevision",
    "GitOid",
    "Kind",
    "PluginKey",
    "Ref",
    "RefKey",
    "RefTargetIdentity",
    "UtcDatetime",
    "VersionedSpec",
    "canonical_json",
    "content_hash",
    "contract_schema_version_scope",
    "parse_semver",
    "scoped_contract_schema_version",
    "validate_ref_keyed_hashes",
]

#: 本次发布的契约 Schema 版本（SemVer）。破坏性变更 = major + ADR。
#: 2.0.0 由 ADR-0008 与 ADR-0009 共同定义；与 1.x 的内容哈希**不可比较**。
CONTRACT_SCHEMA_VERSION = "2.0.0"

#: 当前实现能够作为**模型**校验的 major。其他 major 一律拒绝（旧载荷走 core/compat）。
CONTRACT_SCHEMA_MAJOR = 2

#: major 2 内**已发布**的版本（升序，最后一项即 `CONTRACT_SCHEMA_VERSION`）。持久化对象按其
#: 记录版本重放时，记录版本必须在此之中（ADR-0052 Implementation note — versioned replay，V1）。
PUBLISHED_CONTRACT_SCHEMA_VERSIONS: tuple[str, ...] = ("2.0.0",)

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
#: 预登记研究类别的标识符约束（ADR-0018 §D-26.3）。`ProfileSelectionKey.research_class` 与
#: `ProfileScope.research_class` **共用**这一个常量，不各写一份；取值集合仍未决定（D-09 H-7）。
RESEARCH_CLASS_PATTERN = r"^[a-z][a-z0-9_]*$"
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

#: 语义身份的返回类型（ADR-0018 §D-26）：显式、固定形状的元组，不含 Contract 信封版本。
#: `Ref` 目标身份 `(kind, name, version)`。
type RefTargetIdentity = tuple[Kind, str, str]
#: `GitCodeRevision` 代码身份 `(commit_oid, tree_oid)`。
type GitCodeIdentity = tuple[str, str]


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
    return _sha256_text(canonical_json(payload))


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _canonical_object_with_fragments(
    payload: Mapping[str, Any], fragments: Mapping[str, str]
) -> str:
    """`canonical_json({**payload, **fragments 所代表的值})`，其中 `fragments` 已是规范 JSON 文本。

    与 `canonical_json` **逐字节相同**：紧凑分隔符下 JSON 值的编码与其所在位置无关，
    `sort_keys` 对 `str` 键就是按键排序（键唯一），键本身用同一编码器编码。
    只供 `Contract` 在已校验的嵌套实例上复用其规范 JSON（G3-P2，纯性能）。
    """
    encoded = {key: canonical_json(value) for key, value in payload.items()}
    if encoded.keys() & fragments.keys():
        raise ValueError("fragment 键与载荷键重复")
    encoded.update(fragments)
    body = ",".join(f"{canonical_json(key)}:{encoded[key]}" for key in sorted(encoded))
    return "{" + body + "}"


def _memo_get(slot: Any, model: BaseModel) -> str | None:
    """读取一个契约实例上的记忆值；实例的顶层字段在记忆之后被换过则视为无效。

    记忆值与 `(__dict__ 对象, 各字段值对象)` 的**身份**绑定：`__init__` 重入、`__setstate__`
    （二者都换掉 `__dict__`）、`object.__setattr__` / `__dict__[...]` 改写顶层字段都会使其失效。
    """
    try:
        fields_dict, fields, value = slot.__get__(model)
    except AttributeError:
        return None
    current = model.__dict__
    if fields_dict is not current or len(fields) != len(current):
        return None
    for remembered, now in zip(fields, current.values(), strict=True):
        if remembered is not now:
            return None
    return value  # type: ignore[no-any-return]


def _memo_set(slot: Any, model: BaseModel, value: str) -> str:
    current = model.__dict__
    slot.__set__(model, (current, tuple(current.values()), value))
    return value


#: 重建作用域的契约版本（ADR-0052 versioned replay，机制）；`None` = 不在任何重建作用域内。
_SCOPED_SCHEMA_VERSION: ContextVar[str | None] = ContextVar(
    "hlens_contract_schema_version", default=None
)


@contextmanager
def contract_schema_version_scope(version: str) -> Iterator[str]:
    """重建 / 校验**已持久化**对象时，按其记录版本构造契约对象（ADR-0052 versioned replay）。

    只用于重建已提交的对象（Codex M0 复核条件 1）：作用域内构造、且**缺省** `schema_version`
    的契约对象（含嵌套的字典形式子对象）取 `version`；显式给出的版本与已构造的嵌套对象一律不改写
    （条件 2）。离开作用域即由 context manager 恢复（`ContextVar` 的 token，`finally` 中复位；
    线程 / 协程各自独立，可嵌套）。无作用域时构造行为与没有本机制时逐位相同，字段默认值与
    JSON Schema 不变。

    `version` 必须是已发布版本（`PUBLISHED_CONTRACT_SCHEMA_VERSIONS`），否则拒绝（条件 3）：
    本代码从未发布的版本无法被它忠实重建。新写入组的写入者不得在作用域内运行（见
    `scoped_contract_schema_version`，作用域泄漏由写入者 fail closed）。
    """
    if not isinstance(version, str) or version not in PUBLISHED_CONTRACT_SCHEMA_VERSIONS:
        raise ValueError(
            f"契约版本 {version!r} 不是已发布版本 {list(PUBLISHED_CONTRACT_SCHEMA_VERSIONS)}："
            "无法按它重建"
        )
    token = _SCOPED_SCHEMA_VERSION.set(version)
    try:
        yield version
    finally:
        _SCOPED_SCHEMA_VERSION.reset(token)


def scoped_contract_schema_version() -> str | None:
    """当前生效的重建作用域版本；不在任何重建作用域内时为 `None`。

    新写入组的写入者据此检测作用域泄漏：新对象永不继承历史版本（Codex M0 复核条件 1 / 3）。
    """
    return _SCOPED_SCHEMA_VERSION.get()


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

    #: 内容哈希与规范 JSON 的逐实例记忆（G3-P2，纯性能，不改变任何哈希 / 相等 / 校验语义）。
    #: 用普通 slot 而**不是** Pydantic 私有属性：私有属性参与 `__eq__`、`__copy__` 与 pickle，
    #: 会让"算过哈希"的实例与内容相同的实例不相等；slot 不参与三者，复制 / 反序列化得到的
    #: 实例一律从空记忆开始。`__weakref__` 保留基类原有的弱引用能力。
    __slots__ = ("__weakref__", "_content_hash_memo", "_canonical_dump_memo")

    schema_version: str = Field(default=CONTRACT_SCHEMA_VERSION, pattern=SEMVER_PATTERN)

    def content_hash(self) -> str:
        """按语义内容计算哈希；排除逐模型声明的非语义字段。

        每个实例只计算一次（契约 frozen，ADR-0008；`model_copy(update=...)` 重新构造，ADR-0010）。
        记忆值与实例顶层字段的对象身份绑定（见 `_memo_get`），顶层字段被绕过 frozen 改写后
        会重新计算。**诚实边界**同 ADR-0008 决策 2：直接改写嵌套对象内部（例如
        `FrozenMapping._data`，或对嵌套契约 `object.__setattr__`）不在只读承诺之内，
        也不被记忆检测。
        """
        memo = _memo_get(_CONTENT_HASH_MEMO, self)
        if memo is not None:
            return memo
        return _memo_set(_CONTENT_HASH_MEMO, self, _sha256_text(self._semantic_canonical_json()))

    def _semantic_canonical_json(self) -> str:
        """内容哈希的输入：排除非语义字段后的 `model_dump(mode="json")` 的规范 JSON。

        子类只可为**性能**覆写，结果必须与本实现逐字节相同（见 `FeatureRequest`）。
        """
        return canonical_json(self.model_dump(mode="json", exclude=self._non_semantic_fields()))

    def _canonical_dump_json(self) -> str:
        """完整 `model_dump(mode="json")`（不排除任何字段）的规范 JSON，逐实例记忆。

        这正是该实例作为**同一声明类型**的嵌套字段出现在父契约 dump 中的片段，
        供父契约在构造内容哈希输入时复用（`_canonical_object_with_fragments`）。
        """
        memo = _memo_get(_CANONICAL_DUMP_MEMO, self)
        if memo is not None:
            return memo
        return _memo_set(_CANONICAL_DUMP_MEMO, self, canonical_json(self.model_dump(mode="json")))

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

    @model_validator(mode="before")
    @classmethod
    def _scoped_schema_version(cls, data: Any) -> Any:
        """在 `contract_schema_version_scope` 内，缺省的信封取作用域版本（否则不改动输入）。"""
        scoped = _SCOPED_SCHEMA_VERSION.get()
        if scoped is not None and isinstance(data, dict) and "schema_version" not in data:
            return {**data, "schema_version": scoped}
        return data

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


#: `Contract.__slots__` 的描述符：直接经描述符读写，绕开 Pydantic 的 `__getattr__` / `__setattr__`。
_CONTENT_HASH_MEMO = Contract.__dict__["_content_hash_memo"]
_CANONICAL_DUMP_MEMO = Contract.__dict__["_canonical_dump_memo"]


class Ref(Contract):
    """对象引用：`{kind}:{name}@{version}`。"""

    kind: Kind
    name: str = Field(pattern=NAME_PATTERN)
    version: str = Field(pattern=SEMVER_PATTERN)

    def target_identity(self) -> RefTargetIdentity:
        """目标身份 `(kind, name, version)`：是否指向**同一个对象**（ADR-0018 §D-26.5）。

        不含 Contract 信封 `schema_version`。跨对象判断"是否是同一目标"（生命周期 subject、
        LIVE 授权 / Risk Gate subject）必须用它，而不是 Pydantic 全结构相等。
        结构相等与 `content_hash()` 保持不变，仍包含信封版本（§D-26.7）。
        """
        return (self.kind, self.name, self.version)

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

    def code_identity(self) -> GitCodeIdentity:
        """代码身份 `(commit_oid, tree_oid)`：是否是**同一份代码**（ADR-0018 §D-26.5）。

        这是 ADR-0015 §D-21.2"生产代码身份 = commit + tree"的准确落点，不含 Contract 信封
        `schema_version`。结构相等与 `content_hash()` 保持不变，仍包含信封版本（§D-26.7）。
        """
        return (self.commit_oid, self.tree_oid)


class ContentBlobRef(Contract):
    """一份内容的取回引用 + 内容哈希（ADR-0016 §D-18.1）。

    把"内容在哪里"与"内容是什么"放进同一个值对象：`uri` 给出取回位置，
    `sha256` 给出该内容规范的 SHA-256（与 `ContentHash` 同一命名空间，ADR-0015 §D-21.1）。
    `media_type` 与 `byte_size` 是可选的描述性槽位。

    `uri` **只做非空约束**：URI 方案取决于尚未决定的存储选择（D-01、D-02），
    现在冻结方案等于把一个没做的决定写进契约。

    空白语义：`Contract` 基类开启了 `str_strip_whitespace`，因此 `uri` 与 `media_type`
    的纯空白取值都会先被去空白、再被 `min_length=1` 拒绝——两者**用同一条规则**，
    不存在"空串拒绝、空白放行"的不一致。

    **诚实边界**：本类型只约束**形状**。`uri` 是否可取回、取回的内容是否真的哈希成
    `sha256`、`media_type` / `byte_size` 是否与实际内容相符，都必须由持有内容的
    存储层 / Registry 核验（ADR-0016 §D-18.3）。本契约不打开 `uri`、不做任何取回，
    也**不提供**任何自报"已验证"的布尔标志。
    """

    uri: str = Field(min_length=1)
    sha256: ContentHash
    #: 提供时不得为空（或纯空白）；不提供时为 `None`。
    media_type: Annotated[str, Field(min_length=1)] | None = None
    #: 提供时 `>= 0`；零字节内容是合法的，因此允许 `0`。
    byte_size: Annotated[int, Field(ge=0)] | None = None


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

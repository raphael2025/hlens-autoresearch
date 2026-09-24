"""`CollectorAdapter`：有界、可重放的公共 market-data 获取契约（ADR-0021 D-10、ADR-0022；B3）。

对应 docs/architecture/03-data.md §6、§7.1、§7.3 与 05-plugin.md §6。本模块只定义可执行
Protocol 与可序列化 DTO；**不含任何实现**（公共归档下载壳属批次 D0，解析与 revision 语义属
D1 / D2）。

| 成员 | 输入 → 输出 | 首个消费者 |
|---|---|---|
| `descriptor` | → `CollectorDescriptor`（身份、所服务的 source、声明的网络 origin） | D0 |
| `collect` | `CollectionRequest` → `CollectionResult` | D0 归档下载、D3 REST 补尾 |

调用语义（由 `tests/contract_suites/collector.py` 检查）：

- 实现在构造时获得一个 `StorageAdapter`；`collect` 产出的每个对象都必须已经由它**发布**，
  结果只携带 `ObjectRef`（key、URI、SHA-256、字节数），不携带内容字节。来源给出校验和时
  （例如 `.CHECKSUM`），必须先经 `StageRequest.expected_sha256` 校验再发布，并记入
  `source_sha256`；
- 请求有界：UTC 半开区间 `[coverage_start, coverage_end)` × 非空 symbol 集合。结果对每个
  symbol 用对象与显式缺口**恰好覆盖**请求区间：不得越界，缺口之间及缺口与对象之间不得重叠，
  不得遗漏（缺口不推断填补）；
- 可重放：同一请求再次执行（包括重建 collector 之后）得到同一组对象身份（key、URI、SHA-256、
  字节数）与同一组缺口；`retrieved_at` 可以不同。来源内容变化时不得覆盖已发布对象：要么由
  新 key 承载新 source revision，要么以存储冲突 fail closed（revision 语义属 D2）；
- 不服务的 source 绑定 → `UnsupportedRequest`；暂时性失败 → `CollectionFailed`（可按同一
  请求重试，已发布对象由存储层幂等去重）。

**网络能力**：三类 Data Plane Adapter 中只有 Collector 可以联网（05-plugin.md §6），并在
`descriptor` 中声明 HTTPS origin；结果中网络来源的 origin 必须在声明内。这种声明只用于审计与
一致性核对，**不是安全控制**：端点限制由 D0 的类型化设置与端点静态检查执行。契约中没有交易、
账户、订单、API key 或私有端点字段；来源 URI 与元数据中凭据形状的名称被拒绝（同样只是形状
拒绝）。本模块不硬编码任何 venue、symbol、URL 或 HTTP 客户端。

**诚实边界**：DTO 只证明形状与请求 / 结果之间的覆盖关系。对象是否真的已发布并与引用一致、
来源是否真的是所述 URI、`retrieved_at` 是否真实、重放是否稳定，都是实现的行为义务，由
contract suite 对具体实现检查。
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from itertools import pairwise
from typing import Annotated, Protocol

from pydantic import BeforeValidator, Field, field_validator, model_validator

from core.contracts.revision import BINDING_ID_PATTERN
from core.contracts.storage import (
    ObjectRef,
    has_dot_segment,
    is_host_port,
    is_object_path,
    split_uri,
)
from core.domain.base import (
    NAME_PATTERN,
    SEMVER_PATTERN,
    ContentHash,
    Contract,
    FrozenMapping,
    UtcDatetime,
)

__all__ = [
    "HEADER_NAME_PATTERN",
    "NETWORK_ORIGIN_PATTERN",
    "REQUEST_ID_PATTERN",
    "SOURCE_URI_PATTERN",
    "SOURCE_URI_SCHEMES",
    "SYMBOL_PATTERN",
    "CollectedObject",
    "CollectionFailed",
    "CollectionRequest",
    "CollectionResult",
    "CollectorAdapter",
    "CollectorDescriptor",
    "CollectorError",
    "CoverageGap",
    "GapReason",
    "SourceBinding",
    "UnsupportedRequest",
    "validate_source_uri",
]

#: 请求的稳定身份：ASCII、不透明（例如由 source、数据类型、symbol 与区间形成）。
REQUEST_ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._:=@+-]{0,255}$"
#: venue 原生 symbol：原样保留、区分大小写，不做规范化（ADR-0018 边界、ADR-0022 §2）。
SYMBOL_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$"
_HOST_LABEL = r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
#: 声明的网络 origin：只允许 `https://host[:port]`，无路径、查询、凭据。
NETWORK_ORIGIN_PATTERN = rf"^https://{_HOST_LABEL}(?:\.{_HOST_LABEL})*(?::[0-9]{{1,5}})?$"
#: 来源 URI 的 JSON Schema 近似：`https://host[:port]` 后接可选路径 / 查询，或 `file:///绝对路径`。
#: 运行时（`validate_source_uri`）更严：端口范围、逐段检查、查询参数的凭据形状、`file` 无查询；
#: 这些 JSON Schema 表达不了（02-domain.md §3.7）。
SOURCE_URI_PATTERN = r"^(?:https://[a-z0-9.-]+(?::[0-9]{1,5})?(?:[/?][!-~]*)?|file:///[!-~]+)$"
#: 本阶段允许的来源 scheme：`https` 网络来源与 `file` 只读导入。
SOURCE_URI_SCHEMES = frozenset({"https", "file"})
#: 来源元数据的键（例如 HTTP 响应头 `etag`、`last-modified`）：小写 token。
HEADER_NAME_PATTERN = r"^[a-z0-9][a-z0-9-]{0,127}$"

#: 凭据形状的名称片段：出现在查询参数名或元数据键中即拒绝。形状拒绝，不是安全控制。
_SECRET_NAME_PARTS = (
    "auth",
    "cookie",
    "credential",
    "key",
    "password",
    "secret",
    "signature",
    "token",
)


class CollectorError(Exception):
    """CollectorAdapter 契约错误的基类。"""


class UnsupportedRequest(CollectorError):
    """请求的 source 绑定或数据类型不由该 collector 服务。"""


class CollectionFailed(CollectorError):
    """暂时性获取失败；可按同一请求重试。"""


def _has_secret_shape(name: str) -> bool:
    lowered = name.lower()
    return any(part in lowered for part in _SECRET_NAME_PARTS)


def validate_source_uri(value: object) -> str:
    """复核来源 URI；原样返回。不访问网络。按 RFC 3986 组件逐项检查：

    - 只接受 ASCII 可见字符（无空白、反斜杠），不得有片段；scheme 只能是 `https` 或 `file`；
    - `https`：authority 必须是非空小写主机名 + 可选端口（1 ~ 65535），不得含凭据；路径可以为空或
      以 `/` 开头，不得含 `.` / `..`（含 `%2e` 编码）段；查询参数名呈凭据形状即拒绝；
    - `file`：必须是 `file:///绝对路径`（authority 存在且为空，无远程主机），路径为非空的绝对对象
      路径，不得有查询。
    """
    if not isinstance(value, str) or not value.isascii() or not value.isprintable():
        raise ValueError(f"非法来源 URI：{value!r}；只接受 ASCII 可见字符")
    if " " in value or "\\" in value:
        raise ValueError(f"来源 URI 不得含空白或反斜杠：{value!r}")
    parts = split_uri(value)
    if parts.scheme not in SOURCE_URI_SCHEMES or parts.authority is None:
        raise ValueError(f"来源 URI 只能是 https://… 或 file:///…：{value!r}")
    if parts.fragment is not None:
        raise ValueError(f"来源 URI 不得含片段：{value!r}")
    if parts.scheme == "file":
        if parts.authority != "":
            raise ValueError(
                f"file 来源 URI 不得带 authority（必须是 file:///绝对路径）：{value!r}"
            )
        if parts.query is not None:
            raise ValueError(f"file 来源 URI 不得含查询：{value!r}")
        if not is_object_path(parts.path):
            raise ValueError(f"file 来源 URI 必须是不含空段或 . / .. 段的绝对路径：{value!r}")
        return value
    if not is_host_port(parts.authority):
        raise ValueError(f"https 来源 URI 必须有合法主机名（可带端口），不得含凭据：{value!r}")
    if parts.path and (not parts.path.startswith("/") or has_dot_segment(parts.path)):
        raise ValueError(f"https 来源 URI 的路径不得含 . / .. 段：{value!r}")
    for part in parts.query.split("&") if parts.query else ():
        name = part.split("=", 1)[0]
        if _has_secret_shape(name):
            raise ValueError(f"来源 URI 的查询参数呈凭据形状：{name!r}")
    return value


def _check_header_name(value: str) -> str:
    if _has_secret_shape(value):
        raise ValueError(f"来源元数据不得含凭据形状的键：{value!r}")
    return value


RequestId = Annotated[str, Field(pattern=REQUEST_ID_PATTERN)]
Symbol = Annotated[str, Field(pattern=SYMBOL_PATTERN)]
NetworkOrigin = Annotated[str, Field(pattern=NETWORK_ORIGIN_PATTERN)]
SourceUri = Annotated[str, Field(pattern=SOURCE_URI_PATTERN), BeforeValidator(validate_source_uri)]
HeaderName = Annotated[str, Field(pattern=HEADER_NAME_PATTERN), BeforeValidator(_check_header_name)]
NonEmptyStr = Annotated[str, Field(min_length=1)]


def _canonical_unique_strings(values: tuple[str, ...], label: str) -> tuple[str, ...]:
    """集合语义：重复即拒绝；规范排序只为内容哈希唯一，不表示先后。"""
    if len(set(values)) != len(values):
        raise ValueError(f"{label} 不得包含重复项")
    return tuple(sorted(values))


def _require_half_open(start: datetime, end: datetime, label: str) -> None:
    if start >= end:
        raise ValueError(f"{label} 必须是非空 UTC 半开区间：start < end")


class SourceBinding(Contract):
    """版本化来源绑定：`source_id` + SemVer（标识符格式与 03-data.md §7.3 一致）。"""

    source_id: str = Field(pattern=BINDING_ID_PATTERN)
    version: str = Field(pattern=SEMVER_PATTERN)


class CollectorDescriptor(Contract):
    """collector 的身份与能力声明。

    `sources` 非空、去重、按 `(source_id, version)` 规范排序；`network_origins` 去重排序，
    可为空（离线只读导入不联网）。声明只用于审计与一致性核对，不是安全控制。
    """

    collector_id: str = Field(pattern=BINDING_ID_PATTERN)
    version: str = Field(pattern=SEMVER_PATTERN)
    sources: tuple[SourceBinding, ...] = Field(min_length=1)
    network_origins: tuple[NetworkOrigin, ...] = Field(json_schema_extra={"uniqueItems": True})

    @field_validator("sources")
    @classmethod
    def _canonical_sources(cls, value: tuple[SourceBinding, ...]) -> tuple[SourceBinding, ...]:
        keys = [(item.source_id, item.version) for item in value]
        if len(set(keys)) != len(keys):
            raise ValueError("sources 不得包含重复的 source_id@version")
        return tuple(sorted(value, key=lambda item: (item.source_id, item.version)))

    @field_validator("network_origins")
    @classmethod
    def _canonical_origins(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _canonical_unique_strings(value, "network_origins")


class CollectionRequest(Contract):
    """一次有界、可重放的获取请求。

    `request_id` 是调用方给出的稳定身份；`symbols` 非空、去重、规范排序；覆盖区间为 UTC 半开区间。
    """

    request_id: RequestId
    source: SourceBinding
    data_type: str = Field(pattern=NAME_PATTERN)
    symbols: tuple[Symbol, ...] = Field(min_length=1, json_schema_extra={"uniqueItems": True})
    coverage_start: UtcDatetime
    coverage_end: UtcDatetime

    @field_validator("symbols")
    @classmethod
    def _canonical_symbols(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _canonical_unique_strings(value, "symbols")

    @model_validator(mode="after")
    def _bounded(self) -> CollectionRequest:
        _require_half_open(self.coverage_start, self.coverage_end, "覆盖区间")
        return self


class CollectedObject(Contract):
    """一个已发布对象及其最小来源证据。

    - `ref`：经 `StorageAdapter` 发布的不可变对象引用；
    - `symbol` 与 UTC 半开区间 `[coverage_start, coverage_end)`：该对象覆盖的范围；
    - `source_uri`、`retrieved_at`：从哪里、何时取得（`retrieved_at` 属知识轴证据，
      不是 `available_time`）；
    - `source_sha256`：来源声明的校验和；给出时必须等于 `ref.sha256`；
    - `source_metadata`：原样保留的来源元数据（例如 HTTP `etag`），键为小写 token，
      凭据形状的键被拒绝。
    """

    ref: ObjectRef
    symbol: Symbol
    coverage_start: UtcDatetime
    coverage_end: UtcDatetime
    source_uri: SourceUri
    retrieved_at: UtcDatetime
    source_sha256: ContentHash | None = None
    source_metadata: FrozenMapping[HeaderName, str] = Field(
        default_factory=dict, validate_default=True
    )

    @model_validator(mode="after")
    def _shape(self) -> CollectedObject:
        _require_half_open(self.coverage_start, self.coverage_end, "对象覆盖区间")
        if self.source_sha256 is not None and self.source_sha256 != self.ref.sha256:
            raise ValueError("source_sha256 必须等于已发布对象的 sha256")
        return self


class GapReason(StrEnum):
    """显式缺口的原因。"""

    #: 来源声明该区间没有数据或尚未发布（例如归档不存在）；不推断填补。
    SOURCE_ABSENT = "source_absent"


class CoverageGap(Contract):
    """某个 symbol 在 UTC 半开区间内没有获取到对象的显式记录；`detail` 为非空证据描述。"""

    symbol: Symbol
    coverage_start: UtcDatetime
    coverage_end: UtcDatetime
    reason: GapReason
    detail: NonEmptyStr

    @model_validator(mode="after")
    def _shape(self) -> CoverageGap:
        _require_half_open(self.coverage_start, self.coverage_end, "缺口区间")
        return self


class CollectionResult(Contract):
    """一次 `collect` 的结果：原请求 + collector 身份 + 对象 + 显式缺口。

    `objects` 与 `gaps` 必须显式给出（可为空）。不变量：

    - `objects` 按 `ref.key` 规范排序且 key 唯一；`gaps` 按 `(symbol, coverage_start)` 规范排序；
    - 每个对象 / 缺口的 symbol 属于请求，区间落在请求区间内；
    - 对每个 symbol：缺口之间、缺口与对象之间不重叠，对象与缺口的并集恰好等于请求区间
      （同一 symbol 的多个对象可以覆盖同一区间，例如归档与其校验和文件）。
    """

    request: CollectionRequest
    collector_id: str = Field(pattern=BINDING_ID_PATTERN)
    collector_version: str = Field(pattern=SEMVER_PATTERN)
    objects: tuple[CollectedObject, ...]
    gaps: tuple[CoverageGap, ...]

    @field_validator("objects")
    @classmethod
    def _canonical_objects(cls, value: tuple[CollectedObject, ...]) -> tuple[CollectedObject, ...]:
        keys = [item.ref.key for item in value]
        if len(set(keys)) != len(keys):
            raise ValueError("objects 的 ref.key 不得重复")
        return tuple(sorted(value, key=lambda item: item.ref.key))

    @field_validator("gaps")
    @classmethod
    def _canonical_gaps(cls, value: tuple[CoverageGap, ...]) -> tuple[CoverageGap, ...]:
        return tuple(sorted(value, key=lambda item: (item.symbol, item.coverage_start)))

    @model_validator(mode="after")
    def _accounts_for_request(self) -> CollectionResult:
        request = self.request
        spans: dict[str, list[tuple[datetime, datetime]]] = {s: [] for s in request.symbols}
        gap_spans: dict[str, list[tuple[datetime, datetime]]] = {s: [] for s in request.symbols}
        items: list[tuple[str, datetime, datetime, bool]] = [
            (o.symbol, o.coverage_start, o.coverage_end, False) for o in self.objects
        ] + [(g.symbol, g.coverage_start, g.coverage_end, True) for g in self.gaps]
        for symbol, start, end, is_gap in items:
            if symbol not in spans:
                raise ValueError(f"symbol {symbol!r} 不在请求内")
            if start < request.coverage_start or end > request.coverage_end:
                raise ValueError(f"{symbol!r} 的区间 [{start}, {end}) 越出请求区间")
            (gap_spans if is_gap else spans)[symbol].append((start, end))
        for symbol in request.symbols:
            gaps = sorted(gap_spans[symbol])
            for (_, prev_end), (next_start, _) in pairwise(gaps):
                if next_start < prev_end:
                    raise ValueError(f"{symbol!r} 的缺口互相重叠")
            for gap_start, gap_end in gaps:
                if any(s < gap_end and gap_start < e for s, e in spans[symbol]):
                    raise ValueError(f"{symbol!r} 的缺口与已获取对象重叠")
            cursor = request.coverage_start
            for start, end in sorted(spans[symbol] + gaps):
                if start > cursor:
                    raise ValueError(f"{symbol!r} 在 [{cursor}, {start}) 既无对象也无显式缺口")
                cursor = max(cursor, end)
            if cursor != request.coverage_end:
                raise ValueError(
                    f"{symbol!r} 在 [{cursor}, {request.coverage_end}) 既无对象也无显式缺口"
                )
        return self


class CollectorAdapter(Protocol):
    """公共 market-data 获取边界（ADR-0022）。语义见模块文档。"""

    @property
    def descriptor(self) -> CollectorDescriptor:
        """collector 身份、所服务的 source 与声明的网络 origin；实例生命周期内不变。"""
        ...

    def collect(self, request: CollectionRequest) -> CollectionResult:
        """执行一次有界获取；只返回已发布对象的引用与显式缺口。"""
        ...

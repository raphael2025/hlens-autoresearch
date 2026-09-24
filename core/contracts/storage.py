"""`StorageAdapter`：不可变对象的 staging → 校验 → 原子发布契约（ADR-0021 D-02；Phase 1 B3）。

对应 docs/architecture/03-data.md §1、§6、§7.1（归档字节以不可变对象存于 warehouse）。本模块只定义
可执行 Protocol、可序列化 DTO 与键 / URI 的格式约束；**不含任何实现**（`file://` 实现属批次 C1）。

| 方法 | 输入 → 输出 | 首个消费者 |
|---|---|---|
| `stage` | `StageRequest` + 字节块流 → `StagedObject` | D0 归档下载 |
| `publish` | `StagedObject` → `PublishResult` | D0 |
| `lookup` | 逻辑 key → `ObjectRef` 或 `None` | D0 重放、D2 Raw 引用核对 |
| `open_read` | `ObjectRef` → 只读二进制 handle | D1 parser |

调用语义（未来任一实现都必须满足，由 `tests/contract_suites/storage.py` 检查）：

- 调用者只给**逻辑相对 key**（`validate_object_key`），不给本机路径。实现必须在**每个**入口
  复核 key（Python 边界不经过 DTO 校验，`model_construct` 也会绕过它），并拒绝映射进自身
  私有区域（例如 staging）的 key，一律以 `ObjectKeyViolation` 失败；
- `stage` 单遍消费字节块流，边写入 staging 边计算 SHA-256 与长度；与 `expected_sha256` /
  `expected_size` 不符即 `IntegrityViolation`，且不返回 `StagedObject`；staging 内容在
  `publish` 前**不可见**；
- `publish` 原子地使对象在 key 下可见：目标不存在 → `created`；目标已存在且 SHA-256 与长度
  相同 → `already_present`（幂等，包括对同一 `StagedObject` 的重试）；目标已存在且内容不同 →
  `ObjectConflict`，已有对象不变。实现**不得信任** `StagedObject` 的自报字段：实现从未
  签发与之完全一致（key、SHA-256、长度、`staging_id`）的条目即 `StagingViolation`；
- 任一步失败都不得留下可见的半成品；没有覆盖或删除 API。未发布的 staging 残留与 orphan
  只由未来的显式 maintenance 批次清理；
- `open_read` 只在 key 下的对象与 `ref` 的 SHA-256 与长度一致时返回只读 handle
  （`writable()` 为假）；不存在 → `ObjectNotFound`，不一致 → `IntegrityViolation`。

**诚实边界**：DTO 只证明形状。对象是否存在、字节是否真的哈希为 `sha256`、`uri` 是否指向
该对象、发布是否原子、不同 key 是否互不影响，都是实现的行为义务，由 contract suite 对具体
实现检查，不是 DTO 可证明的不变量。
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from enum import StrEnum
from typing import Annotated, BinaryIO, Protocol

from pydantic import BeforeValidator, Field

from core.contracts import _uri
from core.domain.base import ContentHash, Contract

__all__ = [
    "OBJECT_KEY_MAX_LENGTH",
    "OBJECT_KEY_PATTERN",
    "OBJECT_URI_PATTERN",
    "STAGING_ID_PATTERN",
    "IntegrityViolation",
    "ObjectConflict",
    "ObjectKeyViolation",
    "ObjectNotFound",
    "ObjectRef",
    "PublishOutcome",
    "PublishResult",
    "StageRequest",
    "StagedObject",
    "StagingViolation",
    "StorageAdapter",
    "StorageError",
    "validate_object_key",
    "validate_object_uri",
]

# ---------------------------------------------------------------------------------------
# 逻辑对象 key
#
# 只接受 ASCII：每段以字母或数字开头，其后为字母、数字与 `.` `_` `=` `-`。因此 `.` / `..` /
# 隐藏段 / 以 `-` 开头的段都无法构成；`/` 只作分隔，不得开头、结尾或连续出现（无空段）。
# 反斜杠、`:`（URI scheme、Windows 盘符）、`%`（编码绕过）、`~`、空白、NUL 与任何非 ASCII
# 字符都不在字符集内。
# 这组约束由正则一次表达，同一字符串既用于运行时校验，也写进 JSON Schema。
# ---------------------------------------------------------------------------------------

_KEY_SEGMENT = r"[A-Za-z0-9][A-Za-z0-9._=-]{0,254}"
#: 逻辑相对对象 key，例如 `raw/archives/AAAUSD/2024-01-01/archive.zip`。
OBJECT_KEY_PATTERN = rf"^{_KEY_SEGMENT}(?:/{_KEY_SEGMENT})*$"
OBJECT_KEY_MAX_LENGTH = 1024
_KEY_RE = re.compile(OBJECT_KEY_PATTERN)

#: 发布对象持久 URI 的 JSON Schema 近似：`file:///…`，或 `scheme://host[:port]/…`。
#: 运行时（`validate_object_uri`）更严：逐段检查、端口范围、`file` 不得带 authority、无查询 /
#: 片段；这些 JSON Schema 表达不了（02-domain.md §3.7）。
OBJECT_URI_PATTERN = r"^(?:file:///|[a-z][a-z0-9+.-]*://[a-z0-9.-]+(?::[0-9]{1,5})?/)[!-~]+$"

#: 实现分配的 staging 条目标识：不透明，不是路径。
STAGING_ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._:=@+-]{0,255}$"


class StorageError(Exception):
    """StorageAdapter 契约错误的基类。"""


class ObjectKeyViolation(StorageError, ValueError):
    """key 不是合法的逻辑相对 key，或会映射进实现的私有区域。"""


class IntegrityViolation(StorageError):
    """实际内容的 SHA-256 / 长度与预期或引用不一致。"""


class ObjectConflict(StorageError):
    """key 下已有不同内容；已有对象保持不变（fail closed）。"""


class ObjectNotFound(StorageError):
    """key 下没有已发布对象。"""


class StagingViolation(StorageError):
    """`StagedObject` 与实现签发过的任何条目都不完全一致（未知或被篡改）。"""


def validate_object_key(value: object) -> str:
    """复核逻辑对象 key；原样返回，**不做任何规范化**（不去空白、不改大小写、不解码）。

    实现必须在每个入口调用它；非法即 `ObjectKeyViolation`。
    """
    if not isinstance(value, str):
        raise ObjectKeyViolation(f"对象 key 必须是字符串：{type(value).__name__}")
    if len(value) > OBJECT_KEY_MAX_LENGTH or _KEY_RE.fullmatch(value) is None:
        raise ObjectKeyViolation(
            f"非法对象 key：{value!r}；必须是逻辑相对 key（{OBJECT_KEY_PATTERN}）"
        )
    return value


def validate_object_uri(value: object) -> str:
    """复核发布对象 URI；原样返回。只检查 RFC 3986 组件，不打开、不解析到本机路径，不固定
    warehouse 根。

    - 只接受 ASCII 可见字符（无空白、反斜杠）；scheme 为小写；不得有查询或片段；
    - `file`：必须是 `file:///绝对路径`，即 authority 存在且为空（无远程主机）；
    - 其它 scheme：authority 必须是非空主机名 + 可选端口（1 ~ 65535，无前导零），不得含凭据；
    - 路径必须是非空的绝对对象路径：每段非空，每个 `%` 都是合法的两位十六进制 escape 且不解码为
      `/`、`\\`、控制字符或 DEL，解码后不是 `.` / `..` 段。
    """
    if not isinstance(value, str) or not _uri.is_visible_ascii(value):
        raise ValueError(f"非法对象 URI：{value!r}；只接受无空白、无反斜杠的 ASCII 可见字符")
    parts = _uri.split(value)
    if not _uri.is_scheme(parts.scheme):
        raise ValueError(f"对象 URI 必须带小写 scheme：{value!r}")
    if parts.query is not None or parts.fragment is not None:
        raise ValueError(f"对象 URI 不得含查询或片段：{value!r}")
    if parts.authority is None:
        raise ValueError(f"对象 URI 必须是 `scheme://` 形式的绝对 URI：{value!r}")
    if parts.scheme == "file":
        if parts.authority != "":
            raise ValueError(
                f"file 对象 URI 不得带 authority（必须是 file:///绝对路径）：{value!r}"
            )
    elif not _uri.is_host_port(parts.authority):
        raise ValueError(
            f"对象 URI 的 authority 必须是非空主机名（可带端口），不得含凭据：{value!r}"
        )
    if not _uri.is_object_path(parts.path):
        raise ValueError(
            f"对象 URI 必须有非空的绝对对象路径，无空段、. / .. 段或危险 escape：{value!r}"
        )
    return value


#: 字段级在 Contract 去空白**之前**复核原始值：非法 key 一律拒绝，而不是被规范化成另一个合法 key。
ObjectKey = Annotated[
    str,
    Field(pattern=OBJECT_KEY_PATTERN, max_length=OBJECT_KEY_MAX_LENGTH),
    BeforeValidator(validate_object_key),
]
ObjectUri = Annotated[str, Field(pattern=OBJECT_URI_PATTERN), BeforeValidator(validate_object_uri)]
StagingId = Annotated[str, Field(pattern=STAGING_ID_PATTERN)]
ByteSize = Annotated[int, Field(ge=0, strict=True)]


class StageRequest(Contract):
    """一次 staging 请求：目标 key + 预期 SHA-256（必填）+ 可选的预期字节数。

    内容本身不在 DTO 内：字节块流作为 `StorageAdapter.stage` 的独立参数传入，不把完整文件放进模型。
    """

    key: ObjectKey
    expected_sha256: ContentHash
    expected_size: ByteSize | None = None


class StagedObject(Contract):
    """已校验、尚不可见的 staging 条目。`staging_id` 由实现分配，不透明。

    这是调用凭据而不是事实：实现在 `publish` 时必须用自身签发记录核对它，不得信任其中的自报
    字段。同一凭据在发布成功后重试，得到 `already_present`。
    """

    key: ObjectKey
    sha256: ContentHash
    size: ByteSize
    staging_id: StagingId


class ObjectRef(Contract):
    """已发布不可变对象的引用：逻辑 key + 持久 URI + SHA-256 + 字节数。

    URI 由实现生成（ADR-0021：业务代码不拼接绝对路径）。`ContentBlobRef` 仍是通用内容引用；
    本类型额外要求 key 与长度，供 Raw 记录与 `open_read` 核对。
    """

    key: ObjectKey
    uri: ObjectUri
    sha256: ContentHash
    size: ByteSize


class PublishOutcome(StrEnum):
    """发布结果：新建，或目标已含相同内容（幂等重放）。"""

    CREATED = "created"
    ALREADY_PRESENT = "already_present"


class PublishResult(Contract):
    """一次 `publish` 的结果。"""

    ref: ObjectRef
    outcome: PublishOutcome


class StorageAdapter(Protocol):
    """Data Plane 对象存储边界（ADR-0021 D-02）。语义见模块文档与 03-data.md §1。"""

    def stage(self, request: StageRequest, content: Iterable[bytes]) -> StagedObject:
        """单遍消费 `content` 写入 staging 并校验；不符 `IntegrityViolation`，流的异常原样传播。"""
        ...

    def publish(self, staged: StagedObject) -> PublishResult:
        """原子发布；相同内容幂等，不同内容 `ObjectConflict`，凭据不符 `StagingViolation`。"""
        ...

    def lookup(self, key: str) -> ObjectRef | None:
        """已发布对象的引用；不存在返回 `None`；staging 中的条目不可见。"""
        ...

    def open_read(self, ref: ObjectRef) -> BinaryIO:
        """只读 handle（调用方负责关闭，可作 context manager）；对象须与 `ref` 完全一致。"""
        ...

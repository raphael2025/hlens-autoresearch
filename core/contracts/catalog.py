"""`CatalogAdapter`：Iceberg catalog 能力的中立边界（ADR-0021 D-01、ADR-0023 §7；Phase 1 B3）。

对应 docs/architecture/03-data.md §1、§7.1、§7.2。它**只**隔离 Iceberg catalog 能力（表身份、
表定义、snapshot 与幂等提交），不是 Control Plane Registry。本模块只定义可执行 Protocol 与
可序列化 DTO；**不含任何实现**（PyIceberg SQL Catalog on PostgreSQL 属批次 C2，首批八张表的
列 / 分区属 C3）。

| 方法 | 输入 → 输出 | 首个消费者 |
|---|---|---|
| `load_table` | `namespace.table` → `TableInfo` 或 `None` | C2 |
| `create_table` | `TableDefinition` → `TableInfo` | C2 / C3 建表 |
| `get_snapshot` | 表 + snapshot ID → `SnapshotInfo` | C2 时间旅行、F 的 snapshot 绑定 |
| `commit_batch` | `CommitRequest` + 实现专用 batch → `CommitResult` | C3 / D2 写入 |

三种身份分开（ADR-0023 §7）：

- **表身份**：`namespace.table`（与 `PointInTimeSpec.snapshot_bindings` 的键同一格式）；
- **snapshot 身份**：`(table, snapshot_id)`；`SnapshotInfo` 总是绑定其所属表；
- **batch / 幂等身份**：`(table, batch_id)` + 调用方声明的 `batch_fingerprint`。

调用语义（由 `tests/contract_suites/catalog.py` 检查）：

- `create_table` 按 `TableDefinition` 的绑定（`definition_id` + SemVer + `definition_hash`）
  解析实现侧已登记的表定义；未登记或哈希不符 → `UnknownTableDefinition`。同表同绑定重放
  幂等；同表不同绑定 → `TableDefinitionConflict`，已有表不变（Schema / 分区演进属 C3 的
  显式操作）。需要时隐式创建 namespace；
- `commit_batch` 只做 append。先按 `(table, batch_id)` 查已有提交：指纹相同则返回**同一个**
  snapshot（`already_committed`，不产生新 snapshot，与 `expected_parent_snapshot_id` 是否已
  过期无关），指纹不同 → `BatchConflict`。否则 `expected_parent_snapshot_id` 必须等于当前
  snapshot（首个提交为 `None`），不等 → `CommitConflict`（乐观并发，调用方重读后以同一
  `batch_id` 重试）。batch 的实际行数必须等于 `row_count`，不等 → `BatchRejected`。
  失败的提交不产生 snapshot；已写未引用的文件是 orphan，只由显式 maintenance 清理；
- 提交状态跨进程重启保持：重建的 adapter 对同一 `batch_id` 重放得到同一 snapshot；
- `get_snapshot` 只返回该表真实存在的 snapshot；其它表的或不存在的 ID →
  `SnapshotNotFound`。历史 snapshot 的元数据在后续提交后不变（时间旅行的元数据面）。

`arrival_seq` 不参与任何 catalog 提交或优先级；本模块不含该字段。`committed_at` 只是审计
元数据，不得用于 revision 选择。

`BatchT` 是实现专用的 batch 类型（C2 为有界 `pyarrow.Table` microbatch，ADR-0023 §7）；
核心契约不 import 它，也不承载行数据。

**诚实边界**：`batch_fingerprint` 是调用方按版本化规则声明的幂等指纹，adapter 在重放时比较
声明值；是否由 batch 内容重新计算并核对属 C2 / C3。按 snapshot 读取表数据（带投影 / 谓词
的 scan）不在本批：它的形状取决于 F 的 PIT 执行器，届时以增量方法交付。表定义的列级
Schema 与 partition spec 属 C3，本批没有验证任何具体表定义。
"""

from __future__ import annotations

import re
from enum import StrEnum
from typing import Annotated, Protocol

from pydantic import BeforeValidator, Field, model_validator

from core.contracts.revision import BINDING_ID_PATTERN, SNAPSHOT_TABLE_PATTERN
from core.domain.base import SEMVER_PATTERN, ContentHash, Contract, UtcDatetime

__all__ = [
    "BATCH_ID_PATTERN",
    "SNAPSHOT_ID_PATTERN",
    "TABLE_NAME_PATTERN",
    "BatchConflict",
    "BatchRejected",
    "CatalogAdapter",
    "CatalogError",
    "CommitConflict",
    "CommitOutcome",
    "CommitRequest",
    "CommitResult",
    "SnapshotInfo",
    "SnapshotNotFound",
    "TableDefinition",
    "TableDefinitionConflict",
    "TableInfo",
    "TableNameViolation",
    "TableNotFound",
    "UnknownTableDefinition",
    "validate_table_name",
]

#: 逻辑表身份 `namespace.table`：与 B1 的 snapshot 绑定键同一 pattern（03-data.md §7.1）。
TABLE_NAME_PATTERN = SNAPSHOT_TABLE_PATTERN
_TABLE_RE = re.compile(TABLE_NAME_PATTERN)
#: snapshot ID 与 batch ID：ASCII、不透明，不是路径。Iceberg snapshot ID 以十进制串表示。
SNAPSHOT_ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._:=@+-]{0,255}$"
BATCH_ID_PATTERN = SNAPSHOT_ID_PATTERN


class CatalogError(Exception):
    """CatalogAdapter 契约错误的基类。"""


class TableNameViolation(CatalogError, ValueError):
    """表名不是 `namespace.table`。"""


class UnknownTableDefinition(CatalogError):
    """表定义绑定未在实现侧登记，或其哈希与登记内容不符。"""


class TableDefinitionConflict(CatalogError):
    """表已以另一个定义绑定存在；已有表不变。"""


class TableNotFound(CatalogError):
    """表不存在。"""


class SnapshotNotFound(CatalogError):
    """该表没有这个 snapshot。"""


class CommitConflict(CatalogError):
    """`expected_parent_snapshot_id` 不是当前 snapshot（并发提交）；未产生 snapshot。"""


class BatchConflict(CatalogError):
    """同表同 `batch_id` 已以不同指纹提交；未产生 snapshot。"""


class BatchRejected(CatalogError):
    """batch 与请求不一致（例如行数）；未产生 snapshot。"""


def validate_table_name(value: object) -> str:
    """复核 `namespace.table`；原样返回。实现必须在接收裸字符串的入口调用它。"""
    if not isinstance(value, str) or _TABLE_RE.fullmatch(value) is None:
        raise TableNameViolation(
            f"非法表名：{value!r}；必须是 namespace.table（{TABLE_NAME_PATTERN}）"
        )
    return value


TableName = Annotated[str, Field(pattern=TABLE_NAME_PATTERN), BeforeValidator(validate_table_name)]
SnapshotId = Annotated[str, Field(pattern=SNAPSHOT_ID_PATTERN)]
BatchId = Annotated[str, Field(pattern=BATCH_ID_PATTERN)]
RowCount = Annotated[int, Field(ge=0, strict=True)]


class TableDefinition(Contract):
    """建表请求：表身份 + 实现侧表定义的绑定（`definition_id` + SemVer + `definition_hash`）。

    列级 Schema、partition spec 与表属性由实现侧的版本化定义文档承载（C3），这里只以稳定、可审计、
    非代码的绑定引用它；`definition_hash` 是该文档内容的 SHA-256。
    """

    table: TableName
    definition_id: str = Field(pattern=BINDING_ID_PATTERN)
    version: str = Field(pattern=SEMVER_PATTERN)
    definition_hash: ContentHash


class SnapshotInfo(Contract):
    """一个表 snapshot 的中立元数据。

    - `parent_snapshot_id` 必须显式给出，`null` 表示该表的首个 snapshot；不得等于自身；
    - `batch_id` 与 `batch_fingerprint` 同时给出或同时为空（非 batch 产生的 snapshot，
      例如未来的 maintenance）；
    - `added_rows <= total_rows`；
    - `committed_at` 是 catalog 记录的提交时间，只作审计。
    """

    table: TableName
    snapshot_id: SnapshotId
    parent_snapshot_id: SnapshotId | None
    committed_at: UtcDatetime
    batch_id: BatchId | None
    batch_fingerprint: ContentHash | None
    added_rows: RowCount
    total_rows: RowCount

    @model_validator(mode="after")
    def _shape(self) -> SnapshotInfo:
        if self.parent_snapshot_id == self.snapshot_id:
            raise ValueError("parent_snapshot_id 不得等于 snapshot_id")
        if (self.batch_id is None) != (self.batch_fingerprint is None):
            raise ValueError("batch_id 与 batch_fingerprint 必须同时给出或同时为空")
        if self.added_rows > self.total_rows:
            raise ValueError("added_rows 不得大于 total_rows")
        return self


class TableInfo(Contract):
    """一张已存在的表：创建它的定义绑定 + 当前 snapshot（显式 `null` = 尚无提交）。"""

    definition: TableDefinition
    current_snapshot: SnapshotInfo | None

    @model_validator(mode="after")
    def _bound_to_table(self) -> TableInfo:
        if (
            self.current_snapshot is not None
            and self.current_snapshot.table != self.definition.table
        ):
            raise ValueError("current_snapshot 必须属于同一张表")
        return self


class CommitRequest(Contract):
    """一次 append 提交请求。batch 数据作为 `commit_batch` 的独立参数传入，不进入 DTO。

    - `batch_id`：稳定的幂等身份（例如由 Raw revision 与表名形成），重试时可从 Raw / staging
      重建同一批；
    - `batch_fingerprint`：调用方按版本化规则声明的 batch 内容指纹；
    - `row_count >= 1`：空 batch 不提交（避免"reader 被消费后写入零行"被当成成功，ADR-0023 §7）；
    - `expected_parent_snapshot_id` 必须显式给出：`null` 表示期望该表尚无 snapshot。
    """

    table: TableName
    batch_id: BatchId
    batch_fingerprint: ContentHash
    row_count: int = Field(ge=1, strict=True)
    expected_parent_snapshot_id: SnapshotId | None


class CommitOutcome(StrEnum):
    """提交结果：新 snapshot，或同一 batch 已提交（幂等重放）。"""

    COMMITTED = "committed"
    ALREADY_COMMITTED = "already_committed"


class CommitResult(Contract):
    """`commit_batch` 的结果：请求 + 该 batch 的 snapshot + 结果类别。

    snapshot 必须属于请求的表、携带请求的 `batch_id` 与指纹、`added_rows == row_count`；
    `committed` 时其父 snapshot 必须就是请求期望的父 snapshot。`already_committed` 时 snapshot
    是该 batch **首次**提交产生的那一个，其父 snapshot 可以与本次请求的期望不同。
    """

    request: CommitRequest
    snapshot: SnapshotInfo
    outcome: CommitOutcome

    @model_validator(mode="after")
    def _bound_to_request(self) -> CommitResult:
        request, snapshot = self.request, self.snapshot
        if snapshot.table != request.table:
            raise ValueError("snapshot 必须属于请求的表")
        if snapshot.batch_id != request.batch_id:
            raise ValueError("snapshot 必须携带请求的 batch_id")
        if snapshot.batch_fingerprint != request.batch_fingerprint:
            raise ValueError("snapshot 必须携带请求的 batch_fingerprint")
        if snapshot.added_rows != request.row_count:
            raise ValueError("snapshot 的 added_rows 必须等于请求的 row_count")
        if (
            self.outcome is CommitOutcome.COMMITTED
            and snapshot.parent_snapshot_id != request.expected_parent_snapshot_id
        ):
            raise ValueError("committed 的 snapshot 必须以请求期望的 snapshot 为父")
        return self


class CatalogAdapter[BatchT](Protocol):
    """Iceberg catalog 边界（ADR-0021 D-01）。`BatchT` 由实现绑定；语义见模块文档。"""

    def load_table(self, table: str) -> TableInfo | None:
        """定义绑定与当前 snapshot；表不存在返回 `None`；表名非法 `TableNameViolation`。"""
        ...

    def create_table(self, definition: TableDefinition) -> TableInfo:
        """建表；同绑定幂等，异绑定 `TableDefinitionConflict`，未登记 `UnknownTableDefinition`。"""
        ...

    def get_snapshot(self, table: str, snapshot_id: str) -> SnapshotInfo:
        """该表的指定 snapshot；无表 `TableNotFound`，snapshot 不属于该表 `SnapshotNotFound`。"""
        ...

    def commit_batch(self, request: CommitRequest, batch: BatchT) -> CommitResult:
        """按 `batch_id` 幂等的 append；冲突与拒绝见模块文档。"""
        ...

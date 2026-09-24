"""`CatalogAdapter` 的 provider-agnostic contract suite（core/contracts/catalog.py，ADR-0021）。

实现方提供 `CatalogSubject`：

- `open`：每次调用返回一个**新** adapter 实例，与此前实例共享同一持久 catalog（例如同一 PostgreSQL
  test database + warehouse），用来模拟进程重启与第二个并发 writer；
- `make_batch(rows, tag)`：构造恰好 `rows` 行的实现专用 batch；不同 `tag` 产生不同内容；
- `definitions`：两个已在实现侧登记、表身份不同的 `TableDefinition`；
- `conflicting_definition`：同样已登记、与 `definitions[0]` 同表但绑定不同的定义。

suite 只检查 catalog 元数据面（表、snapshot、batch 幂等、并发冲突、重启）；按 snapshot 读取行数据
不在 B3 的接口内。以内存或 SQLite 通过本 suite 不构成 PostgreSQL 集成证据（ADR-0021 D-01 第 6 条）。
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from dataclasses import dataclass
from functools import partial
from typing import Any

import pytest

from core.contracts.catalog import (
    BatchConflict,
    BatchRejected,
    CatalogAdapter,
    CommitConflict,
    CommitOutcome,
    CommitRequest,
    CommitResult,
    SnapshotInfo,
    SnapshotNotFound,
    TableDefinition,
    TableDefinitionConflict,
    TableInfo,
    TableNameViolation,
    TableNotFound,
    UnknownTableDefinition,
)
from tests.contract_suites._support import (
    ContractSuiteFailure,
    call_ok,
    expect_error,
    require,
    revalidated,
)

__all__ = [
    "CATALOG_CHECKS",
    "INVALID_TABLE_NAMES",
    "CatalogAdapterContract",
    "CatalogCheck",
    "CatalogSubject",
]


@dataclass(frozen=True)
class CatalogSubject[BatchT]:
    """被测 `CatalogAdapter` 的接入点（见模块文档）。"""

    open: Callable[[], CatalogAdapter[BatchT]]
    make_batch: Callable[[int, str], BatchT]
    definitions: tuple[TableDefinition, TableDefinition]
    conflicting_definition: TableDefinition


type CatalogCheck = Callable[[CatalogSubject[Any]], None]

#: 必须被拒绝的表名：非 `namespace.table`、路径形状、大写、空白、多级、注入形状。
INVALID_TABLE_NAMES: tuple[str, ...] = (
    "",
    "trades",
    "raw.",
    ".trades",
    "raw.trades.extra",
    "raw/trades",
    "../raw.trades",
    "Raw.Trades",
    "raw.trades ",
    "raw. trades",
    "raw.trades;drop",
    "raw.tr\x00ades",
)


def _fingerprint(tag: str) -> str:
    return hashlib.sha256(tag.encode("utf-8")).hexdigest()


def _subject_tables(subject: CatalogSubject[Any]) -> tuple[str, str]:
    first, second = subject.definitions
    require(first.table != second.table, "subject.definitions 必须是两张不同的表")
    conflicting = subject.conflicting_definition
    require(
        conflicting.table == first.table and conflicting != first,
        "subject.conflicting_definition 必须与 definitions[0] 同表且绑定不同",
    )
    return first.table, second.table


def _load(catalog: CatalogAdapter[Any], table: str) -> TableInfo | None:
    found = call_ok(f"load_table({table!r})", lambda: catalog.load_table(table))
    if found is None:
        return None
    info = revalidated(TableInfo, found, f"load_table({table!r}) 的结果")
    require(info.definition.table == table, "load_table 返回了别的表")
    return info


def _create(catalog: CatalogAdapter[Any], definition: TableDefinition) -> TableInfo:
    created = call_ok(
        f"create_table({definition.table!r})", lambda: catalog.create_table(definition)
    )
    info = revalidated(TableInfo, created, f"create_table({definition.table!r}) 的结果")
    require(info.definition == definition, "create_table 返回的定义绑定与请求不同")
    return info


def _snapshot(catalog: CatalogAdapter[Any], table: str, snapshot_id: str) -> SnapshotInfo:
    found = call_ok(
        f"get_snapshot({table!r}, {snapshot_id!r})",
        lambda: catalog.get_snapshot(table, snapshot_id),
    )
    info = revalidated(SnapshotInfo, found, "get_snapshot 的结果")
    require(
        info.table == table and info.snapshot_id == snapshot_id, "get_snapshot 返回了别的 snapshot"
    )
    return info


def _current(catalog: CatalogAdapter[Any], table: str) -> SnapshotInfo | None:
    info = _load(catalog, table)
    if info is None:
        raise ContractSuiteFailure(f"表 {table!r} 应当存在")
    return info.current_snapshot


def _request(table: str, batch_id: str, rows: int, tag: str, parent: str | None) -> CommitRequest:
    return CommitRequest(
        table=table,
        batch_id=batch_id,
        batch_fingerprint=_fingerprint(tag),
        row_count=rows,
        expected_parent_snapshot_id=parent,
    )


def _commit[B](
    subject: CatalogSubject[B],
    catalog: CatalogAdapter[B],
    request: CommitRequest,
    tag: str,
    outcome: CommitOutcome,
) -> SnapshotInfo:
    batch = subject.make_batch(request.row_count, tag)
    raw = call_ok(
        f"commit_batch({request.batch_id!r})", lambda: catalog.commit_batch(request, batch)
    )
    result = revalidated(CommitResult, raw, f"commit_batch({request.batch_id!r}) 的结果")
    require(result.request == request, "CommitResult.request 必须原样回显请求")
    require(result.outcome is outcome, f"提交结果应为 {outcome.value}，实际为 {result.outcome}")
    return result.snapshot


def _commit_error[B](
    subject: CatalogSubject[B],
    catalog: CatalogAdapter[B],
    request: CommitRequest,
    batch_rows: int,
    tag: str,
    error: type[Exception],
) -> None:
    batch = subject.make_batch(batch_rows, tag)
    expect_error(
        error, f"commit_batch({request.batch_id!r})", partial(catalog.commit_batch, request, batch)
    )


# ======================================================================================
# 检查
# ======================================================================================


def check_create_and_load_table(subject: CatalogSubject[Any]) -> None:
    table, _ = _subject_tables(subject)
    definition = subject.definitions[0]
    catalog = subject.open()
    require(_load(catalog, table) is None, "建表前 load_table 必须返回 None")
    info = _create(catalog, definition)
    require(info.current_snapshot is None, "新表不得有 snapshot")
    require(_load(catalog, table) == info, "load_table 必须返回建表结果")
    require(_create(catalog, definition) == info, "同绑定重复建表必须幂等")
    require(_load(subject.open(), table) == info, "重建实例后表必须仍在")


def check_unknown_definition_is_rejected(subject: CatalogSubject[Any]) -> None:
    table, _ = _subject_tables(subject)
    definition = subject.definitions[0]
    catalog = subject.open()
    unknown = {
        "哈希不符": definition.model_copy(
            update={"definition_hash": _fingerprint(definition.definition_hash)}
        ),
        "未登记版本": definition.model_copy(update={"version": "999999.0.0"}),
    }
    for label, forged in unknown.items():
        expect_error(
            UnknownTableDefinition, f"create_table({label})", partial(catalog.create_table, forged)
        )
        require(_load(catalog, table) is None, f"{label} 的建表失败后不得留下表")


def check_definition_conflict_is_rejected(subject: CatalogSubject[Any]) -> None:
    _subject_tables(subject)
    catalog = subject.open()
    info = _create(catalog, subject.definitions[0])
    expect_error(
        TableDefinitionConflict,
        "以不同定义重复建表",
        partial(catalog.create_table, subject.conflicting_definition),
    )
    require(_load(catalog, info.definition.table) == info, "定义冲突后已有表必须不变")


def check_invalid_table_names_are_rejected(subject: CatalogSubject[Any]) -> None:
    """裸字符串入口与未经 DTO 校验的请求（`model_construct`）都以 `TableNameViolation` 失败。"""
    catalog = subject.open()
    definition = subject.definitions[0]
    for name in INVALID_TABLE_NAMES:
        expect_error(TableNameViolation, f"load_table({name!r})", partial(catalog.load_table, name))
        expect_error(
            TableNameViolation, f"get_snapshot({name!r})", partial(catalog.get_snapshot, name, "1")
        )
        forged_definition = TableDefinition.model_construct(
            table=name,
            definition_id=definition.definition_id,
            version=definition.version,
            definition_hash=definition.definition_hash,
        )
        expect_error(
            TableNameViolation,
            f"create_table({name!r})",
            partial(catalog.create_table, forged_definition),
        )
        forged_request = CommitRequest.model_construct(
            table=name,
            batch_id="b-invalid",
            batch_fingerprint=_fingerprint("invalid"),
            row_count=1,
            expected_parent_snapshot_id=None,
        )
        _commit_error(subject, catalog, forged_request, 1, "invalid", TableNameViolation)


def check_commit_binds_snapshot_to_table_and_batch(subject: CatalogSubject[Any]) -> None:
    table, _ = _subject_tables(subject)
    catalog = subject.open()
    _create(catalog, subject.definitions[0])
    request = _request(table, "batch-a", 3, "a", None)
    snapshot = _commit(subject, catalog, request, "a", CommitOutcome.COMMITTED)
    require(snapshot.parent_snapshot_id is None, "首个 snapshot 不得有父 snapshot")
    require(snapshot.total_rows == 3, "首个 snapshot 的 total_rows 应为 3")
    require(_current(catalog, table) == snapshot, "当前 snapshot 必须是刚提交的 snapshot")
    require(
        _snapshot(catalog, table, snapshot.snapshot_id) == snapshot,
        "get_snapshot 必须返回同一元数据",
    )


def check_snapshot_history_is_immutable(subject: CatalogSubject[Any]) -> None:
    """时间旅行的元数据面：旧 snapshot 在后续提交与重启后保持原样，新 snapshot 以它为父。"""
    table, _ = _subject_tables(subject)
    catalog = subject.open()
    _create(catalog, subject.definitions[0])
    first = _commit(
        subject, catalog, _request(table, "batch-a", 3, "a", None), "a", CommitOutcome.COMMITTED
    )
    second = _commit(
        subject,
        catalog,
        _request(table, "batch-b", 2, "b", first.snapshot_id),
        "b",
        CommitOutcome.COMMITTED,
    )
    require(second.snapshot_id != first.snapshot_id, "新提交必须产生新 snapshot")
    require(second.parent_snapshot_id == first.snapshot_id, "新 snapshot 必须以前一个为父")
    require(second.total_rows == first.total_rows + 2, "total_rows 必须累加本次 added_rows")
    require(_current(catalog, table) == second, "当前 snapshot 必须是最新提交")
    require(_snapshot(catalog, table, first.snapshot_id) == first, "旧 snapshot 的元数据不得改变")
    restarted = subject.open()
    require(_snapshot(restarted, table, first.snapshot_id) == first, "重启后旧 snapshot 必须不变")
    require(_snapshot(restarted, table, second.snapshot_id) == second, "重启后新 snapshot 必须不变")


def check_replayed_batch_returns_the_same_commit(subject: CatalogSubject[Any]) -> None:
    """同 `batch_id` + 同指纹：返回首次提交的同一 snapshot，不产生新 snapshot（含重启与过期父）。"""
    table, _ = _subject_tables(subject)
    catalog = subject.open()
    _create(catalog, subject.definitions[0])
    request_a = _request(table, "batch-a", 3, "a", None)
    first = _commit(subject, catalog, request_a, "a", CommitOutcome.COMMITTED)
    second = _commit(
        subject,
        catalog,
        _request(table, "batch-b", 2, "b", first.snapshot_id),
        "b",
        CommitOutcome.COMMITTED,
    )
    for label, adapter, parent in (
        ("过期父 snapshot", catalog, None),
        ("当前父 snapshot", catalog, second.snapshot_id),
        ("重建实例", subject.open(), None),
    ):
        replay = request_a.model_copy(update={"expected_parent_snapshot_id": parent})
        again = _commit(subject, adapter, replay, "a", CommitOutcome.ALREADY_COMMITTED)
        require(again == first, f"{label}的重放必须返回首次提交的同一 snapshot")
        require(_current(adapter, table) == second, f"{label}的重放不得产生新 snapshot")


def check_batch_fingerprint_conflict_fails_closed(subject: CatalogSubject[Any]) -> None:
    table, _ = _subject_tables(subject)
    catalog = subject.open()
    _create(catalog, subject.definitions[0])
    first = _commit(
        subject, catalog, _request(table, "batch-a", 3, "a", None), "a", CommitOutcome.COMMITTED
    )
    for parent in (first.snapshot_id, None):
        changed = _request(table, "batch-a", 3, "a-changed", parent)
        _commit_error(subject, catalog, changed, 3, "a-changed", BatchConflict)
    require(_current(catalog, table) == first, "指纹冲突不得产生新 snapshot")


def check_stale_parent_conflict_fails_closed(subject: CatalogSubject[Any]) -> None:
    """两个 writer 以同一父 snapshot 提交不同 batch：后者 `CommitConflict`，重读后重试成功。"""
    table, _ = _subject_tables(subject)
    writer_one, writer_two = subject.open(), subject.open()
    _create(writer_one, subject.definitions[0])
    require(_current(writer_two, table) is None, "第二个 writer 应看到空表")
    winner = _commit(
        subject, writer_one, _request(table, "batch-x", 2, "x", None), "x", CommitOutcome.COMMITTED
    )
    _commit_error(
        subject, writer_two, _request(table, "batch-y", 2, "y", None), 2, "y", CommitConflict
    )
    require(_current(writer_two, table) == winner, "冲突失败不得产生 snapshot")
    require(_current(writer_one, table) == winner, "冲突失败不得改变当前 snapshot")
    retried = _commit(
        subject,
        writer_two,
        _request(table, "batch-y", 2, "y", winner.snapshot_id),
        "y",
        CommitOutcome.COMMITTED,
    )
    require(retried.parent_snapshot_id == winner.snapshot_id, "重试提交必须以胜者为父")


def check_row_count_mismatch_is_rejected_without_trace(subject: CatalogSubject[Any]) -> None:
    table, _ = _subject_tables(subject)
    catalog = subject.open()
    _create(catalog, subject.definitions[0])
    _commit_error(subject, catalog, _request(table, "batch-a", 3, "a", None), 2, "a", BatchRejected)
    require(_current(catalog, table) is None, "被拒绝的 batch 不得产生 snapshot")
    snapshot = _commit(
        subject, catalog, _request(table, "batch-a", 2, "a", None), "a", CommitOutcome.COMMITTED
    )
    require(snapshot.added_rows == 2, "被拒绝的 batch_id 不得留下提交记录")


def check_unknown_table_and_foreign_snapshot_are_rejected(subject: CatalogSubject[Any]) -> None:
    table, other = _subject_tables(subject)
    catalog = subject.open()
    _create(catalog, subject.definitions[0])
    snapshot = _commit(
        subject, catalog, _request(table, "batch-a", 1, "a", None), "a", CommitOutcome.COMMITTED
    )
    _commit_error(subject, catalog, _request(other, "batch-a", 1, "a", None), 1, "a", TableNotFound)
    expect_error(
        TableNotFound,
        "get_snapshot(未建表)",
        partial(catalog.get_snapshot, other, snapshot.snapshot_id),
    )
    _create(catalog, subject.definitions[1])
    expect_error(
        SnapshotNotFound,
        "get_snapshot(别的表的 snapshot)",
        partial(catalog.get_snapshot, other, snapshot.snapshot_id),
    )
    expect_error(
        SnapshotNotFound,
        "get_snapshot(不存在的 ID)",
        partial(catalog.get_snapshot, table, "no-such-snapshot"),
    )


def check_batch_ids_are_scoped_per_table(subject: CatalogSubject[Any]) -> None:
    table, other = _subject_tables(subject)
    catalog = subject.open()
    _create(catalog, subject.definitions[0])
    _create(catalog, subject.definitions[1])
    first = _commit(
        subject, catalog, _request(table, "batch-1", 2, "t0", None), "t0", CommitOutcome.COMMITTED
    )
    second = _commit(
        subject, catalog, _request(other, "batch-1", 3, "t1", None), "t1", CommitOutcome.COMMITTED
    )
    require(first.table == table and second.table == other, "snapshot 必须属于各自的表")
    require(
        _current(catalog, table) == first and _current(catalog, other) == second, "两表互不影响"
    )


CATALOG_CHECKS: tuple[CatalogCheck, ...] = (
    check_create_and_load_table,
    check_unknown_definition_is_rejected,
    check_definition_conflict_is_rejected,
    check_invalid_table_names_are_rejected,
    check_commit_binds_snapshot_to_table_and_batch,
    check_snapshot_history_is_immutable,
    check_replayed_batch_returns_the_same_commit,
    check_batch_fingerprint_conflict_fails_closed,
    check_stale_parent_conflict_fails_closed,
    check_row_count_mismatch_is_rejected_without_trace,
    check_unknown_table_and_foreign_snapshot_are_rejected,
    check_batch_ids_are_scoped_per_table,
)


class CatalogAdapterContract:
    """pytest 复用入口：子类以 `Test*` 命名并提供 `catalog_subject` fixture。"""

    @pytest.fixture
    def catalog_subject(self) -> CatalogSubject[Any]:
        raise NotImplementedError("子类必须提供 catalog_subject fixture")

    @pytest.mark.parametrize("check", CATALOG_CHECKS, ids=lambda check: check.__name__)
    def test_catalog_contract(
        self, catalog_subject: CatalogSubject[Any], check: CatalogCheck
    ) -> None:
        check(catalog_subject)

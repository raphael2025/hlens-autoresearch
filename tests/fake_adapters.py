"""B3 用来证明 contract suite 有效的**测试替身**，不是任何 Adapter 实现。

每个 Adapter 有两个刻意不同的合规替身（证明 suite 接受不同的合规实现方式），以及一组各带
**一处**故障的变体（证明 suite 能杀死该类错误行为）。全部只用内存或测试临时文件，
不访问网络、数据库、PyIceberg 或真实文件系统布局；不计入验收 #7 / #8 / #10。
"""

from __future__ import annotations

import hashlib
import io
import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import BinaryIO

from core.contracts.catalog import (
    BatchConflict,
    BatchRejected,
    CommitConflict,
    CommitOutcome,
    CommitRequest,
    CommitResult,
    SnapshotInfo,
    SnapshotNotFound,
    TableDefinition,
    TableDefinitionConflict,
    TableInfo,
    TableNotFound,
    UnknownTableDefinition,
    validate_table_name,
)
from core.contracts.collector import (
    CollectedObject,
    CollectionRequest,
    CollectionResult,
    CollectorDescriptor,
    CoverageGap,
    GapReason,
    SourceBinding,
    UnsupportedRequest,
)
from core.contracts.storage import (
    IntegrityViolation,
    ObjectConflict,
    ObjectKeyViolation,
    ObjectNotFound,
    ObjectRef,
    PublishOutcome,
    PublishResult,
    StagedObject,
    StageRequest,
    StagingViolation,
    StorageAdapter,
    validate_object_key,
)
from core.domain.base import FrozenMapping


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _read_only(data: bytes) -> BinaryIO:
    return io.BufferedReader(io.BytesIO(data))


# ======================================================================================
# StorageAdapter 替身
# ======================================================================================


@dataclass
class DictStorageState:
    objects: dict[str, bytes] = field(default_factory=dict)
    pending: dict[str, bytes] = field(default_factory=dict)
    issued: dict[str, tuple[str, str, int]] = field(default_factory=dict)
    counter: int = 0


class DictStorage:
    """合规替身 A：key → bytes；staging 为签发编号 → bytes，发布时整体搬入。"""

    uri_base = "memory://dict-storage"

    def __init__(self, state: DictStorageState) -> None:
        self._state = state

    def _key(self, value: object) -> str:
        return validate_object_key(value)

    def _verify(self, request: StageRequest, data: bytes) -> None:
        if sha256(data) != request.expected_sha256:
            raise IntegrityViolation("sha256 mismatch")
        if request.expected_size is not None and len(data) != request.expected_size:
            raise IntegrityViolation("size mismatch")

    def _ref(self, key: str, data: bytes) -> ObjectRef:
        return ObjectRef(key=key, uri=f"{self.uri_base}/{key}", sha256=sha256(data), size=len(data))

    def stage(self, request: StageRequest, content: Iterable[bytes]) -> StagedObject:
        key = self._key(request.key)
        data = b"".join(content)
        self._verify(request, data)
        self._state.counter += 1
        staging_id = f"stg-{self._state.counter}"
        self._state.pending[staging_id] = data
        self._state.issued[staging_id] = (key, sha256(data), len(data))
        return StagedObject(key=key, sha256=sha256(data), size=len(data), staging_id=staging_id)

    def publish(self, staged: StagedObject) -> PublishResult:
        key = self._key(staged.key)
        if self._state.issued.get(staged.staging_id) != (key, staged.sha256, staged.size):
            raise StagingViolation("staged object was never issued")
        existing = self._state.objects.get(key)
        if existing is not None:
            if sha256(existing) != staged.sha256 or len(existing) != staged.size:
                raise ObjectConflict(key)
            self._state.pending.pop(staged.staging_id, None)
            return PublishResult(
                ref=self._ref(key, existing), outcome=PublishOutcome.ALREADY_PRESENT
            )
        data = self._state.pending.pop(staged.staging_id, None)
        if data is None:
            raise StagingViolation("staged object was consumed but its target is missing")
        self._state.objects[key] = data
        return PublishResult(ref=self._ref(key, data), outcome=PublishOutcome.CREATED)

    def lookup(self, key: str) -> ObjectRef | None:
        valid = self._key(key)
        data = self._state.objects.get(valid)
        return None if data is None else self._ref(valid, data)

    def open_read(self, ref: ObjectRef) -> BinaryIO:
        key = self._key(ref.key)
        data = self._state.objects.get(key)
        if data is None:
            raise ObjectNotFound(key)
        if sha256(data) != ref.sha256 or len(data) != ref.size:
            raise IntegrityViolation(f"{key} does not match the reference")
        return _read_only(data)


@dataclass
class BlobStorageState:
    blobs: dict[str, bytes] = field(default_factory=dict)
    index: dict[str, str] = field(default_factory=dict)
    tickets: dict[str, tuple[str, str, int]] = field(default_factory=dict)
    sequence: int = 0


class BlobStorage:
    """合规替身 B：内容寻址。staging 把字节写入 blob 池（按 key 不可见），发布只更新 key 索引；
    `staging/` 前缀被保留为私有区域（演示 `forbidden_keys`）。"""

    reserved_prefix = "staging/"

    def __init__(self, state: BlobStorageState) -> None:
        self._state = state

    def _key(self, value: object) -> str:
        key = validate_object_key(value)
        if key.startswith(self.reserved_prefix) or key == self.reserved_prefix.rstrip("/"):
            raise ObjectKeyViolation(f"{key!r} maps into the private staging area")
        return key

    def _ref(self, key: str, digest: str) -> ObjectRef:
        return ObjectRef(
            key=key,
            uri=f"cas://blob-store/{key}",
            sha256=digest,
            size=len(self._state.blobs[digest]),
        )

    def stage(self, request: StageRequest, content: Iterable[bytes]) -> StagedObject:
        key = self._key(request.key)
        hasher = hashlib.sha256()
        buffer = bytearray()
        for chunk in content:
            hasher.update(chunk)
            buffer.extend(chunk)
        if request.expected_size is not None and len(buffer) != request.expected_size:
            raise IntegrityViolation("size mismatch")
        digest = hasher.hexdigest()
        if digest != request.expected_sha256:
            raise IntegrityViolation("sha256 mismatch")
        self._state.blobs.setdefault(digest, bytes(buffer))
        self._state.sequence += 1
        ticket = f"{digest[:12]}.{self._state.sequence}"
        self._state.tickets[ticket] = (key, digest, len(buffer))
        return StagedObject(key=key, sha256=digest, size=len(buffer), staging_id=ticket)

    def publish(self, staged: StagedObject) -> PublishResult:
        key = self._key(staged.key)
        if self._state.tickets.get(staged.staging_id) != (key, staged.sha256, staged.size):
            raise StagingViolation("unknown or altered staging ticket")
        current = self._state.index.get(key)
        if current is None:
            self._state.index[key] = staged.sha256
            return PublishResult(ref=self._ref(key, staged.sha256), outcome=PublishOutcome.CREATED)
        if current != staged.sha256:
            raise ObjectConflict(key)
        return PublishResult(ref=self._ref(key, current), outcome=PublishOutcome.ALREADY_PRESENT)

    def lookup(self, key: str) -> ObjectRef | None:
        valid = self._key(key)
        digest = self._state.index.get(valid)
        return None if digest is None else self._ref(valid, digest)

    def open_read(self, ref: ObjectRef) -> BinaryIO:
        key = self._key(ref.key)
        digest = self._state.index.get(key)
        if digest is None:
            raise ObjectNotFound(key)
        data = self._state.blobs[digest]
        if digest != ref.sha256 or len(data) != ref.size:
            raise IntegrityViolation(f"{key} does not match the reference")
        return _read_only(data)


# --- 故障变体（各只有一处错误） --------------------------------------------------------


class AcceptsAnyKeyStorage(DictStorage):
    """不复核 key：路径逃逸与私有区域 key 都被接受。"""

    def _key(self, value: object) -> str:
        return str(value)


class IgnoresChecksumStorage(DictStorage):
    def _verify(self, request: StageRequest, data: bytes) -> None:
        if request.expected_size is not None and len(data) != request.expected_size:
            raise IntegrityViolation("size mismatch")


class IgnoresSizeStorage(DictStorage):
    def _verify(self, request: StageRequest, data: bytes) -> None:
        if sha256(data) != request.expected_sha256:
            raise IntegrityViolation("sha256 mismatch")


class VisibleStagingStorage(DictStorage):
    """staging 直接写到目标 key：发布前可见（非原子）。"""

    def stage(self, request: StageRequest, content: Iterable[bytes]) -> StagedObject:
        staged = super().stage(request, content)
        self._state.objects.setdefault(staged.key, self._state.pending[staged.staging_id])
        return staged


class PartialWriteStorage(DictStorage):
    """边读流边写目标 key；流中断时留下可见的半成品。"""

    def stage(self, request: StageRequest, content: Iterable[bytes]) -> StagedObject:
        key = self._key(request.key)
        if key in self._state.objects:
            return super().stage(request, content)
        written = bytearray()
        for chunk in content:
            written.extend(chunk)
            self._state.objects[key] = bytes(written)
        del self._state.objects[key]
        return super().stage(request, [bytes(written)])


class OverwritingStorage(DictStorage):
    """同 key 不同内容时覆盖已有对象。"""

    def publish(self, staged: StagedObject) -> PublishResult:
        existing = self._state.objects.get(staged.key)
        if existing is not None and sha256(existing) != staged.sha256:
            del self._state.objects[staged.key]
        return super().publish(staged)


class TrustingStorage(DictStorage):
    """发布时信任 `StagedObject` 的自报字段，不核对签发记录。"""

    def publish(self, staged: StagedObject) -> PublishResult:
        key = self._key(staged.key)
        data = self._state.pending.pop(staged.staging_id, None)
        if data is None:
            data = self._state.objects.get(key, b"")
        existing = self._state.objects.get(key)
        if existing is not None and existing != data:
            raise ObjectConflict(key)
        self._state.objects[key] = data
        outcome = PublishOutcome.CREATED if existing is None else PublishOutcome.ALREADY_PRESENT
        return PublishResult(ref=self._ref(key, data), outcome=outcome)


class WritableHandleStorage(DictStorage):
    def open_read(self, ref: ObjectRef) -> BinaryIO:
        super().open_read(ref).close()
        return io.BytesIO(self._state.objects[ref.key])


class RefBlindReadStorage(DictStorage):
    """open_read 不核对引用的 sha256 / size。"""

    def open_read(self, ref: ObjectRef) -> BinaryIO:
        data = self._state.objects.get(self._key(ref.key))
        if data is None:
            raise ObjectNotFound(ref.key)
        return _read_only(data)


class ConflictOnReplayStorage(DictStorage):
    """目标已存在即报冲突，哪怕内容相同（非幂等）。"""

    def publish(self, staged: StagedObject) -> PublishResult:
        if staged.key in self._state.objects:
            raise ObjectConflict(staged.key)
        return super().publish(staged)


# ======================================================================================
# CatalogAdapter 替身
# ======================================================================================

_CATALOG_EPOCH = datetime(2026, 1, 1, tzinfo=UTC)


@dataclass
class MemoryCatalogState:
    tables: dict[str, TableDefinition] = field(default_factory=dict)
    history: dict[str, list[SnapshotInfo]] = field(default_factory=dict)
    batches: dict[tuple[str, str], str] = field(default_factory=dict)
    counter: int = 0


class MemoryCatalog:
    """合规替身 A：batch 为行元组；snapshot ID 为递增十进制串。"""

    def __init__(self, state: MemoryCatalogState, known: Iterable[TableDefinition]) -> None:
        self._state = state
        self._known = frozenset(known)

    def _table(self, value: object) -> str:
        return validate_table_name(value)

    def _info(self, table: str) -> TableInfo:
        history = self._state.history[table]
        return TableInfo(
            definition=self._state.tables[table], current_snapshot=history[-1] if history else None
        )

    def load_table(self, table: str) -> TableInfo | None:
        name = self._table(table)
        return self._info(name) if name in self._state.tables else None

    def create_table(self, definition: TableDefinition) -> TableInfo:
        name = self._table(definition.table)
        if definition not in self._known:
            raise UnknownTableDefinition(definition.definition_id)
        existing = self._state.tables.get(name)
        if existing is not None and existing != definition:
            raise TableDefinitionConflict(name)
        if existing is None:
            self._state.tables[name] = definition
            self._state.history[name] = []
        return self._info(name)

    def _find(self, table: str, snapshot_id: str) -> SnapshotInfo | None:
        return next((s for s in self._state.history[table] if s.snapshot_id == snapshot_id), None)

    def get_snapshot(self, table: str, snapshot_id: str) -> SnapshotInfo:
        name = self._table(table)
        if name not in self._state.tables:
            raise TableNotFound(name)
        found = self._find(name, snapshot_id)
        if found is None:
            raise SnapshotNotFound(snapshot_id)
        return found

    def _replay(self, request: CommitRequest, table: str) -> CommitResult | None:
        existing_id = self._state.batches.get((table, request.batch_id))
        if existing_id is None:
            return None
        snapshot = self._find(table, existing_id)
        if snapshot is None or snapshot.batch_fingerprint != request.batch_fingerprint:
            raise BatchConflict(request.batch_id)
        return CommitResult(
            request=request, snapshot=snapshot, outcome=CommitOutcome.ALREADY_COMMITTED
        )

    def _check_parent(self, request: CommitRequest, current: SnapshotInfo | None) -> None:
        current_id = None if current is None else current.snapshot_id
        if request.expected_parent_snapshot_id != current_id:
            raise CommitConflict(f"expected {request.expected_parent_snapshot_id}, is {current_id}")

    def _check_rows(self, request: CommitRequest, batch: tuple[str, ...]) -> None:
        if len(batch) != request.row_count:
            raise BatchRejected("row count mismatch")

    def _parent_id(self, request: CommitRequest, current: SnapshotInfo | None) -> str | None:
        return None if current is None else current.snapshot_id

    def commit_batch(self, request: CommitRequest, batch: tuple[str, ...]) -> CommitResult:
        table = self._table(request.table)
        if table not in self._state.tables:
            raise TableNotFound(table)
        replay = self._replay(request, table)
        if replay is not None:
            return replay
        history = self._state.history[table]
        current = history[-1] if history else None
        self._check_parent(request, current)
        self._check_rows(request, batch)
        self._state.counter += 1
        snapshot = SnapshotInfo(
            table=table,
            snapshot_id=str(self._state.counter),
            parent_snapshot_id=self._parent_id(request, current),
            committed_at=_CATALOG_EPOCH + timedelta(seconds=self._state.counter),
            batch_id=request.batch_id,
            batch_fingerprint=request.batch_fingerprint,
            added_rows=request.row_count,
            total_rows=(0 if current is None else current.total_rows) + request.row_count,
        )
        history.append(snapshot)
        self._state.batches[(table, request.batch_id)] = snapshot.snapshot_id
        return CommitResult(request=request, snapshot=snapshot, outcome=CommitOutcome.COMMITTED)


def make_row_batch(rows: int, tag: str) -> tuple[str, ...]:
    return tuple(f"{tag}:{index}" for index in range(rows))


class JournalCatalog:
    """合规替身 B：每次操作都从 JSON 日志文件读取并写回（跨实例即跨"进程"）；batch 为整数列表；
    snapshot ID 由表名与 batch ID 的哈希派生。只保存元数据，不是 Iceberg 实现。"""

    def __init__(self, journal: Path, known: Iterable[TableDefinition]) -> None:
        self._journal = journal
        self._known = frozenset(known)

    def _load(self) -> dict[str, dict[str, object]]:
        if not self._journal.exists():
            return {}
        loaded: dict[str, dict[str, object]] = json.loads(self._journal.read_text("utf-8"))
        return loaded

    def _save(self, data: Mapping[str, object]) -> None:
        self._journal.write_text(json.dumps(data, sort_keys=True), "utf-8")

    @staticmethod
    def _snapshots(entry: Mapping[str, object]) -> list[SnapshotInfo]:
        raw = entry["snapshots"]
        assert isinstance(raw, list)
        return [SnapshotInfo.model_validate_json(item) for item in raw]

    def _info(self, entry: Mapping[str, object]) -> TableInfo:
        snapshots = self._snapshots(entry)
        definition = TableDefinition.model_validate_json(str(entry["definition"]))
        return TableInfo(
            definition=definition, current_snapshot=snapshots[-1] if snapshots else None
        )

    def load_table(self, table: str) -> TableInfo | None:
        entry = self._load().get(validate_table_name(table))
        return None if entry is None else self._info(entry)

    def create_table(self, definition: TableDefinition) -> TableInfo:
        name = validate_table_name(definition.table)
        if definition not in self._known:
            raise UnknownTableDefinition(definition.definition_id)
        data = self._load()
        entry = data.get(name)
        if entry is None:
            entry = {"definition": definition.model_dump_json(), "snapshots": []}
            data[name] = entry
            self._save(data)
        elif TableDefinition.model_validate_json(str(entry["definition"])) != definition:
            raise TableDefinitionConflict(name)
        return self._info(entry)

    def get_snapshot(self, table: str, snapshot_id: str) -> SnapshotInfo:
        name = validate_table_name(table)
        entry = self._load().get(name)
        if entry is None:
            raise TableNotFound(name)
        for snapshot in self._snapshots(entry):
            if snapshot.snapshot_id == snapshot_id:
                return snapshot
        raise SnapshotNotFound(snapshot_id)

    def commit_batch(self, request: CommitRequest, batch: list[int]) -> CommitResult:
        name = validate_table_name(request.table)
        data = self._load()
        entry = data.get(name)
        if entry is None:
            raise TableNotFound(name)
        snapshots = self._snapshots(entry)
        for snapshot in snapshots:
            if snapshot.batch_id == request.batch_id:
                if snapshot.batch_fingerprint != request.batch_fingerprint:
                    raise BatchConflict(request.batch_id)
                return CommitResult(
                    request=request, snapshot=snapshot, outcome=CommitOutcome.ALREADY_COMMITTED
                )
        if len(batch) != request.row_count:
            raise BatchRejected("row count mismatch")
        current = snapshots[-1] if snapshots else None
        if request.expected_parent_snapshot_id != (
            None if current is None else current.snapshot_id
        ):
            raise CommitConflict("stale parent")
        digest = sha256(f"{name}\x00{request.batch_id}".encode())
        snapshot = SnapshotInfo(
            table=name,
            snapshot_id=str(int(digest[:15], 16)),
            parent_snapshot_id=None if current is None else current.snapshot_id,
            committed_at=_CATALOG_EPOCH + timedelta(minutes=len(snapshots) + 1),
            batch_id=request.batch_id,
            batch_fingerprint=request.batch_fingerprint,
            added_rows=request.row_count,
            total_rows=(0 if current is None else current.total_rows) + request.row_count,
        )
        raw = entry["snapshots"]
        assert isinstance(raw, list)
        raw.append(snapshot.model_dump_json())
        self._save(data)
        return CommitResult(request=request, snapshot=snapshot, outcome=CommitOutcome.COMMITTED)


def make_int_batch(rows: int, tag: str) -> list[int]:
    seed = int(sha256(tag.encode())[:8], 16)
    return [seed + index for index in range(rows)]


# --- 故障变体 --------------------------------------------------------------------------


class DuplicateOnReplayCatalog(MemoryCatalog):
    """不查 batch 索引：重放产生新 snapshot（或因父过期而冲突）。"""

    def _replay(self, request: CommitRequest, table: str) -> CommitResult | None:
        return None


class IgnoresFingerprintCatalog(MemoryCatalog):
    def _replay(self, request: CommitRequest, table: str) -> CommitResult | None:
        existing_id = self._state.batches.get((table, request.batch_id))
        if existing_id is None:
            return None
        snapshot = self._find(table, existing_id)
        assert snapshot is not None
        forged = request.model_copy(update={"batch_fingerprint": snapshot.batch_fingerprint})
        return CommitResult(
            request=forged, snapshot=snapshot, outcome=CommitOutcome.ALREADY_COMMITTED
        )


class IgnoresParentCatalog(MemoryCatalog):
    """不检测并发：以 writer 声明的父 snapshot 记账（丢失更新）。"""

    def _check_parent(self, request: CommitRequest, current: SnapshotInfo | None) -> None:
        return None

    def _parent_id(self, request: CommitRequest, current: SnapshotInfo | None) -> str | None:
        return request.expected_parent_snapshot_id


class IgnoresRowCountCatalog(MemoryCatalog):
    def _check_rows(self, request: CommitRequest, batch: tuple[str, ...]) -> None:
        return None


class RedefiningCatalog(MemoryCatalog):
    """同表不同定义时静默替换。"""

    def create_table(self, definition: TableDefinition) -> TableInfo:
        if definition in self._known:
            self._state.tables.pop(definition.table, None)
        return super().create_table(definition)


class AcceptsUnknownDefinitionCatalog(MemoryCatalog):
    def create_table(self, definition: TableDefinition) -> TableInfo:
        self._known = self._known | {definition}
        return super().create_table(definition)


class AcceptsAnyTableNameCatalog(MemoryCatalog):
    def _table(self, value: object) -> str:
        return str(value)


class ForgingSnapshotCatalog(MemoryCatalog):
    """未知 snapshot ID 时伪造一个元数据，而不是 `SnapshotNotFound`。"""

    def get_snapshot(self, table: str, snapshot_id: str) -> SnapshotInfo:
        try:
            return super().get_snapshot(table, snapshot_id)
        except SnapshotNotFound:
            return SnapshotInfo(
                table=table,
                snapshot_id=snapshot_id,
                parent_snapshot_id=None,
                committed_at=_CATALOG_EPOCH,
                batch_id=None,
                batch_fingerprint=None,
                added_rows=0,
                total_rows=0,
            )


class TableBlindSnapshotCatalog(MemoryCatalog):
    """在所有表中查找 snapshot ID：别的表的 snapshot 可被冒充。"""

    def get_snapshot(self, table: str, snapshot_id: str) -> SnapshotInfo:
        name = self._table(table)
        if name not in self._state.tables:
            raise TableNotFound(name)
        for history in self._state.history.values():
            for snapshot in history:
                if snapshot.snapshot_id == snapshot_id:
                    return snapshot.model_copy(update={"table": name})
        raise SnapshotNotFound(snapshot_id)


class RewritesHistoryCatalog(MemoryCatalog):
    """每次提交后把旧 snapshot 的 total_rows 改成最新值（历史不再不可变）。"""

    def commit_batch(self, request: CommitRequest, batch: tuple[str, ...]) -> CommitResult:
        result = super().commit_batch(request, batch)
        history = self._state.history[result.snapshot.table]
        total = history[-1].total_rows
        history[:-1] = [
            s.model_copy(update={"total_rows": max(total, s.total_rows)}) for s in history[:-1]
        ]
        return result


class ReplayReturnsCurrentCatalog(MemoryCatalog):
    """重放时返回当前 snapshot（而不是该 batch 首次提交的 snapshot），并绕过结果校验。"""

    def _replay(self, request: CommitRequest, table: str) -> CommitResult | None:
        result = super()._replay(request, table)
        if result is None:
            return None
        current = self._state.history[table][-1]
        return CommitResult.model_construct(
            request=request, snapshot=current, outcome=CommitOutcome.ALREADY_COMMITTED
        )


# ======================================================================================
# CollectorAdapter 替身
# ======================================================================================

ARCHIVE_SOURCE = SourceBinding(source_id="fake.public.archive", version="1.0.0")
ARCHIVE_ORIGIN = "https://archive.example.test"
IMPORT_SOURCES = (
    SourceBinding(source_id="fake.local.import", version="2.0.0"),
    SourceBinding(source_id="fake.local.import", version="2.1.0"),
)


class _Clock:
    """确定性的取得时间：每次调用前进一秒（重放时 `retrieved_at` 可以不同）。"""

    def __init__(self) -> None:
        self._now = datetime(2026, 6, 1, tzinfo=UTC)

    def __call__(self) -> datetime:
        self._now += timedelta(seconds=1)
        return self._now


def _publish_bytes(storage: StorageAdapter, key: str, data: bytes) -> ObjectRef:
    staged = storage.stage(StageRequest(key=key, expected_sha256=sha256(data)), [data])
    return storage.publish(staged).ref


class DailyArchiveCollector:
    """合规替身 A：按 UTC 日取"归档"；每个 (symbol, 日) 一个对象，缺失的日子记为显式缺口。
    来源是内存夹具，模拟 HTTPS 归档站点与其校验和。"""

    def __init__(self, storage: StorageAdapter, archive: Mapping[tuple[str, date], bytes]) -> None:
        self._storage = storage
        self._archive = dict(archive)
        self._clock = _Clock()

    @property
    def descriptor(self) -> CollectorDescriptor:
        return CollectorDescriptor(
            collector_id="fake.daily-archive",
            version="1.0.0",
            sources=(ARCHIVE_SOURCE,),
            network_origins=(ARCHIVE_ORIGIN,),
        )

    def _supported(self, request: CollectionRequest) -> None:
        if request.source not in self.descriptor.sources:
            raise UnsupportedRequest(f"source {request.source.source_id}@{request.source.version}")
        for edge in (request.coverage_start, request.coverage_end):
            if edge != datetime.combine(edge.date(), datetime.min.time(), UTC):
                raise UnsupportedRequest("coverage must be aligned to UTC days")

    def _key(self, request: CollectionRequest, symbol: str, day: date) -> str:
        return f"fake-archive/{request.data_type}/{symbol}/{day.isoformat()}.bin"

    def _source_uri(self, request: CollectionRequest, symbol: str, day: date) -> str:
        return f"{ARCHIVE_ORIGIN}/{request.data_type}/{symbol}/{day.isoformat()}.bin"

    def _ref(self, key: str, data: bytes) -> ObjectRef:
        return _publish_bytes(self._storage, key, data)

    def _result(
        self, request: CollectionRequest, objects: list[CollectedObject], gaps: list[CoverageGap]
    ) -> CollectionResult:
        descriptor = self.descriptor
        return CollectionResult(
            request=request,
            collector_id=descriptor.collector_id,
            collector_version=descriptor.version,
            objects=tuple(objects),
            gaps=tuple(gaps),
        )

    def collect(self, request: CollectionRequest) -> CollectionResult:
        self._supported(request)
        objects: list[CollectedObject] = []
        gaps: list[CoverageGap] = []
        for symbol in request.symbols:
            start = request.coverage_start
            while start < request.coverage_end:
                end = start + timedelta(days=1)
                day = start.date()
                data = self._archive.get((symbol, day))
                if data is None:
                    gaps.append(
                        CoverageGap(
                            symbol=symbol,
                            coverage_start=start,
                            coverage_end=end,
                            reason=GapReason.SOURCE_ABSENT,
                            detail=f"no archive for {symbol} {day.isoformat()}",
                        )
                    )
                else:
                    ref = self._ref(self._key(request, symbol, day), data)
                    objects.append(
                        CollectedObject(
                            ref=ref,
                            symbol=symbol,
                            coverage_start=start,
                            coverage_end=end,
                            source_uri=self._source_uri(request, symbol, day),
                            retrieved_at=self._clock(),
                            source_sha256=sha256(data),
                            source_metadata=FrozenMapping({"etag": sha256(data)[:16]}),
                        )
                    )
                start = end
        return self._result(request, objects, gaps)


class RangeImportCollector:
    """合规替身 B：离线只读导入；每个 symbol 一个覆盖整个请求区间的对象，不声明任何网络 origin。"""

    def __init__(self, storage: StorageAdapter, files: Mapping[str, bytes]) -> None:
        self._storage = storage
        self._files = dict(files)
        self._clock = _Clock()

    @property
    def descriptor(self) -> CollectorDescriptor:
        return CollectorDescriptor(
            collector_id="fake.range-import",
            version="2.1.0",
            sources=IMPORT_SOURCES,
            network_origins=(),
        )

    def collect(self, request: CollectionRequest) -> CollectionResult:
        if request.source not in IMPORT_SOURCES:
            raise UnsupportedRequest(request.source.source_id)
        objects: list[CollectedObject] = []
        gaps: list[CoverageGap] = []
        for symbol in request.symbols:
            data = self._files.get(symbol)
            if data is None:
                gaps.append(
                    CoverageGap(
                        symbol=symbol,
                        coverage_start=request.coverage_start,
                        coverage_end=request.coverage_end,
                        reason=GapReason.SOURCE_ABSENT,
                        detail=f"no local file for {symbol}",
                    )
                )
                continue
            key = f"fake-import/{request.request_id}/{symbol}.bin"
            objects.append(
                CollectedObject(
                    ref=_publish_bytes(self._storage, key, data),
                    symbol=symbol,
                    coverage_start=request.coverage_start,
                    coverage_end=request.coverage_end,
                    source_uri=f"file:///fixtures/{symbol}.bin",
                    retrieved_at=self._clock(),
                )
            )
        return CollectionResult(
            request=request,
            collector_id="fake.range-import",
            collector_version="2.1.0",
            objects=tuple(objects),
            gaps=tuple(gaps),
        )


# --- 故障变体 --------------------------------------------------------------------------


class UnpublishedCollector(DailyArchiveCollector):
    """只 stage 不 publish，却返回看似有效的引用。"""

    def _ref(self, key: str, data: bytes) -> ObjectRef:
        self._storage.stage(StageRequest(key=key, expected_sha256=sha256(data)), [data])
        return ObjectRef(
            key=key, uri=f"memory://dict-storage/{key}", sha256=sha256(data), size=len(data)
        )


class MismatchedRefCollector(DailyArchiveCollector):
    """发布的是一份内容，结果引用（与来源校验和）却是另一份。"""

    def collect(self, request: CollectionRequest) -> CollectionResult:
        result = super().collect(request)
        altered = tuple(
            item.model_copy(
                update={
                    "ref": item.ref.model_copy(update={"sha256": sha256(b"other")}),
                    "source_sha256": sha256(b"other"),
                }
            )
            for item in result.objects
        )
        return result.model_copy(update={"objects": altered})


class EchoAlteringCollector(DailyArchiveCollector):
    def collect(self, request: CollectionRequest) -> CollectionResult:
        result = super().collect(request)
        altered = request.model_copy(update={"request_id": f"{request.request_id}-altered"})
        return result.model_copy(update={"request": altered})


class NonReplayableCollector(DailyArchiveCollector):
    """key 中带运行计数：每次重放产生新对象。"""

    def __init__(self, storage: StorageAdapter, archive: Mapping[tuple[str, date], bytes]) -> None:
        super().__init__(storage, archive)
        self._run = 0

    def collect(self, request: CollectionRequest) -> CollectionResult:
        self._run += 1
        return super().collect(request)

    def _key(self, request: CollectionRequest, symbol: str, day: date) -> str:
        return f"fake-archive/run-{self._run}/{symbol}/{day.isoformat()}.bin"


class UndeclaredOriginCollector(DailyArchiveCollector):
    def _source_uri(self, request: CollectionRequest, symbol: str, day: date) -> str:
        return f"https://mirror.example.test/{symbol}/{day.isoformat()}.bin"


class AnySourceCollector(DailyArchiveCollector):
    def _supported(self, request: CollectionRequest) -> None:
        return None


class ForgedResultCollector(DailyArchiveCollector):
    """绕过校验返回漏掉缺口的结果：请求区间未被对象与缺口完整覆盖。"""

    def _result(
        self, request: CollectionRequest, objects: list[CollectedObject], gaps: list[CoverageGap]
    ) -> CollectionResult:
        return CollectionResult.model_construct(
            request=request,
            collector_id="fake.daily-archive",
            collector_version="1.0.0",
            objects=tuple(sorted(objects, key=lambda item: item.ref.key)),
            gaps=(),
        )

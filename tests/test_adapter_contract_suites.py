"""证明 Data Plane Adapter contract suite 有效（Phase 1 B3，roadmap 验收 #6）。

1. 两个刻意不同的合规替身分别通过 Storage / Catalog / Collector 的**全部**检查——它们以未来实现
   相同的方式接入（继承 `*AdapterContract` 并提供 subject fixture）；
2. 每个只带一处故障的变体都被指定的检查以 `ContractSuiteFailure` 判为不合规——suite 检查的是
   行为，不是 `isinstance` 或字段是否存在。

替身与变体见 `tests/fake_adapters.py`；它们不是 Adapter 实现，不计入验收 #7 / #8 / #10。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import pytest

from core.contracts.catalog import CatalogAdapter, TableDefinition
from core.contracts.collector import CollectionRequest, CollectorAdapter
from core.contracts.storage import StorageAdapter
from tests.contract_suites import catalog as catalog_suite
from tests.contract_suites import collector as collector_suite
from tests.contract_suites import storage as storage_suite
from tests.contract_suites._support import ContractSuiteFailure
from tests.contract_suites.catalog import CatalogAdapterContract, CatalogSubject
from tests.contract_suites.collector import CollectorAdapterContract, CollectorSubject
from tests.contract_suites.storage import StorageAdapterContract, StorageSubject
from tests.fake_adapters import (
    ARCHIVE_SOURCE,
    IMPORT_SOURCES,
    AcceptsAnyKeyStorage,
    AcceptsAnyTableNameCatalog,
    AcceptsUnknownDefinitionCatalog,
    AnySourceCollector,
    BlobStorage,
    BlobStorageState,
    ConflictOnReplayStorage,
    DailyArchiveCollector,
    DictStorage,
    DictStorageState,
    DuplicateOnReplayCatalog,
    EchoAlteringCollector,
    ForgedResultCollector,
    ForgingSnapshotCatalog,
    IgnoresChecksumStorage,
    IgnoresFingerprintCatalog,
    IgnoresParentCatalog,
    IgnoresRowCountCatalog,
    IgnoresSizeStorage,
    JournalCatalog,
    MemoryCatalog,
    MemoryCatalogState,
    MismatchedRefCollector,
    NonReplayableCollector,
    OverwritingStorage,
    PartialWriteStorage,
    RangeImportCollector,
    RedefiningCatalog,
    RefBlindReadStorage,
    ReplayFastPathCatalog,
    ReplayReturnsCurrentCatalog,
    RewritesHistoryCatalog,
    TableBlindSnapshotCatalog,
    TrustingStorage,
    TrustsDeclaredFingerprintCatalog,
    UndeclaredOriginCollector,
    UnpublishedCollector,
    VisibleStagingStorage,
    WritableHandleStorage,
    int_batch_fingerprint,
    make_int_batch,
    make_row_batch,
    row_batch_fingerprint,
    sha256,
)

# ======================================================================================
# 夹具
# ======================================================================================


def _definition(table: str, definition_id: str, version: str) -> TableDefinition:
    return TableDefinition(
        table=table,
        definition_id=definition_id,
        version=version,
        definition_hash=sha256(f"{definition_id}@{version}".encode()),
    )


ALPHA = _definition("test_ns.alpha", "test.alpha", "1.0.0")
BETA = _definition("test_ns.beta", "test.beta", "1.0.0")
ALPHA_V2 = _definition("test_ns.alpha", "test.alpha", "2.0.0")
KNOWN_DEFINITIONS = (ALPHA, BETA, ALPHA_V2)

ARCHIVE = {
    ("AAAUSD", date(2024, 1, 1)): b"aaa day one",
    ("AAAUSD", date(2024, 1, 2)): b"aaa day two",
    ("BBBUSD", date(2024, 1, 1)): b"bbb day one",
}
ARCHIVE_REQUEST = CollectionRequest(
    request_id="req-archive-2024-01-01",
    source=ARCHIVE_SOURCE,
    data_type="agg_trades",
    symbols=("BBBUSD", "AAAUSD"),
    coverage_start=datetime(2024, 1, 1, tzinfo=UTC),
    coverage_end=datetime(2024, 1, 3, tzinfo=UTC),
)
IMPORT_FILES = {"AAAUSD": b"imported aaa history"}
IMPORT_REQUEST = CollectionRequest(
    request_id="req-import-1",
    source=IMPORT_SOURCES[0],
    data_type="klines_1m",
    symbols=("AAAUSD", "CCCUSD"),
    coverage_start=datetime(2023, 12, 31, 12, tzinfo=UTC),
    coverage_end=datetime(2024, 1, 1, 12, tzinfo=UTC),
)


def dict_storage_subject(
    factory: Callable[[DictStorageState], DictStorage], forbidden_keys: tuple[str, ...] = ()
) -> StorageSubject:
    state = DictStorageState()
    return StorageSubject(open=lambda: factory(state), forbidden_keys=forbidden_keys)


def memory_catalog_subject(
    factory: Callable[[MemoryCatalogState, tuple[TableDefinition, ...]], MemoryCatalog],
) -> CatalogSubject[tuple[str, ...]]:
    state = MemoryCatalogState()
    return CatalogSubject(
        open=lambda: factory(state, KNOWN_DEFINITIONS),
        make_batch=make_row_batch,
        fingerprint=row_batch_fingerprint,
        definitions=(ALPHA, BETA),
        conflicting_definition=ALPHA_V2,
    )


def batch_amnesia_catalog_subject() -> CatalogSubject[tuple[str, ...]]:
    """表与 snapshot 历史跨重启保留，batch 幂等索引却在每次重建实例时丢失。"""
    state = MemoryCatalogState()
    return CatalogSubject(
        open=lambda: MemoryCatalog(replace(state, batches={}), KNOWN_DEFINITIONS),
        make_batch=make_row_batch,
        fingerprint=row_batch_fingerprint,
        definitions=(ALPHA, BETA),
        conflicting_definition=ALPHA_V2,
    )


def archive_collector_subject(
    factory: Callable[[StorageAdapter, dict[tuple[str, date], bytes]], DailyArchiveCollector],
) -> CollectorSubject:
    storage = DictStorage(DictStorageState())
    return CollectorSubject(
        storage=storage, open=lambda: factory(storage, ARCHIVE), request=ARCHIVE_REQUEST
    )


# ======================================================================================
# 1. 合规替身通过全部检查（与未来实现使用同一复用入口）
# ======================================================================================


class TestDictStorageContract(StorageAdapterContract):
    @pytest.fixture
    def storage_subject(self) -> StorageSubject:
        return dict_storage_subject(DictStorage)


class TestBlobStorageContract(StorageAdapterContract):
    @pytest.fixture
    def storage_subject(self) -> StorageSubject:
        state = BlobStorageState()
        return StorageSubject(
            open=lambda: BlobStorage(state), forbidden_keys=("staging", "staging/escape.bin")
        )


class TestMemoryCatalogContract(CatalogAdapterContract):
    @pytest.fixture
    def catalog_subject(self) -> CatalogSubject[Any]:
        return memory_catalog_subject(MemoryCatalog)


class TestJournalCatalogContract(CatalogAdapterContract):
    """每个新实例都从日志文件重新读取：重启等价由测试临时文件证明（不是 PostgreSQL 证据）。"""

    @pytest.fixture
    def catalog_subject(self, tmp_path: Path) -> CatalogSubject[Any]:
        journal = tmp_path / "catalog-journal.json"
        return CatalogSubject(
            open=lambda: JournalCatalog(journal, KNOWN_DEFINITIONS),
            make_batch=make_int_batch,
            fingerprint=int_batch_fingerprint,
            definitions=(ALPHA, BETA),
            conflicting_definition=ALPHA_V2,
        )


class TestDailyArchiveCollectorContract(CollectorAdapterContract):
    @pytest.fixture
    def collector_subject(self) -> CollectorSubject:
        return archive_collector_subject(DailyArchiveCollector)


class TestRangeImportCollectorContract(CollectorAdapterContract):
    @pytest.fixture
    def collector_subject(self) -> CollectorSubject:
        storage = BlobStorage(BlobStorageState())
        return CollectorSubject(
            storage=storage,
            open=lambda: RangeImportCollector(storage, IMPORT_FILES),
            request=IMPORT_REQUEST,
        )


def test_fixture_requests_exercise_gaps_and_objects() -> None:
    """夹具本身覆盖了对象与显式缺口两种结果，杀死测试才有意义。"""
    for collector, request in (
        (DailyArchiveCollector(DictStorage(DictStorageState()), ARCHIVE), ARCHIVE_REQUEST),
        (RangeImportCollector(BlobStorage(BlobStorageState()), IMPORT_FILES), IMPORT_REQUEST),
    ):
        result = collector.collect(request)
        assert result.objects and result.gaps


# ======================================================================================
# 2. suite 杀死故障变体
# ======================================================================================

STORAGE_KILLS: tuple[tuple[str, Callable[[], StorageSubject], storage_suite.StorageCheck], ...] = (
    (
        "path-escape",
        lambda: dict_storage_subject(AcceptsAnyKeyStorage),
        storage_suite.check_invalid_keys_rejected_at_every_entry,
    ),
    (
        "private-area-key",
        lambda: dict_storage_subject(DictStorage, forbidden_keys=("staging/escape.bin",)),
        storage_suite.check_invalid_keys_rejected_at_every_entry,
    ),
    (
        "checksum-ignored",
        lambda: dict_storage_subject(IgnoresChecksumStorage),
        storage_suite.check_checksum_mismatch_is_not_published,
    ),
    (
        "size-ignored",
        lambda: dict_storage_subject(IgnoresSizeStorage),
        storage_suite.check_size_mismatch_is_not_published,
    ),
    (
        "staging-visible",
        lambda: dict_storage_subject(VisibleStagingStorage),
        storage_suite.check_stage_publish_read_round_trip,
    ),
    (
        "partial-write-visible",
        lambda: dict_storage_subject(PartialWriteStorage),
        storage_suite.check_interrupted_stream_leaves_nothing_visible,
    ),
    (
        "overwrites-different-content",
        lambda: dict_storage_subject(OverwritingStorage),
        storage_suite.check_conflicting_content_fails_closed,
    ),
    (
        "trusts-staged-dto",
        lambda: dict_storage_subject(TrustingStorage),
        storage_suite.check_forged_staged_object_is_rejected,
    ),
    (
        "writable-read-handle",
        lambda: dict_storage_subject(WritableHandleStorage),
        storage_suite.check_read_requires_matching_ref_and_is_read_only,
    ),
    (
        "read-ignores-ref",
        lambda: dict_storage_subject(RefBlindReadStorage),
        storage_suite.check_read_requires_matching_ref_and_is_read_only,
    ),
    (
        "replay-not-idempotent",
        lambda: dict_storage_subject(ConflictOnReplayStorage),
        storage_suite.check_republishing_same_content_is_idempotent,
    ),
    (
        "forgets-on-restart",
        lambda: StorageSubject(open=lambda: DictStorage(DictStorageState())),
        storage_suite.check_restart_keeps_published_and_hides_staged,
    ),
)

CATALOG_KILLS: tuple[
    tuple[str, Callable[[], CatalogSubject[Any]], catalog_suite.CatalogCheck], ...
] = (
    (
        "duplicate-commit-on-replay",
        lambda: memory_catalog_subject(DuplicateOnReplayCatalog),
        catalog_suite.check_replayed_batch_returns_the_same_commit,
    ),
    (
        "replay-returns-wrong-snapshot",
        lambda: memory_catalog_subject(ReplayReturnsCurrentCatalog),
        catalog_suite.check_replayed_batch_returns_the_same_commit,
    ),
    (
        "declared-fingerprint-trusted",
        lambda: memory_catalog_subject(TrustsDeclaredFingerprintCatalog),
        catalog_suite.check_swapped_content_on_first_commit_is_rejected,
    ),
    (
        "replay-skips-content-check",
        lambda: memory_catalog_subject(ReplayFastPathCatalog),
        catalog_suite.check_swapped_content_on_replay_is_rejected,
    ),
    (
        "fingerprint-ignored",
        lambda: memory_catalog_subject(IgnoresFingerprintCatalog),
        catalog_suite.check_batch_fingerprint_conflict_fails_closed,
    ),
    (
        "no-concurrency-conflict",
        lambda: memory_catalog_subject(IgnoresParentCatalog),
        catalog_suite.check_stale_parent_conflict_fails_closed,
    ),
    (
        "row-count-ignored",
        lambda: memory_catalog_subject(IgnoresRowCountCatalog),
        catalog_suite.check_row_count_mismatch_is_rejected_without_trace,
    ),
    (
        "silent-redefinition",
        lambda: memory_catalog_subject(RedefiningCatalog),
        catalog_suite.check_definition_conflict_is_rejected,
    ),
    (
        "unknown-definition-accepted",
        lambda: memory_catalog_subject(AcceptsUnknownDefinitionCatalog),
        catalog_suite.check_unknown_definition_is_rejected,
    ),
    (
        "table-name-unchecked",
        lambda: memory_catalog_subject(AcceptsAnyTableNameCatalog),
        catalog_suite.check_invalid_table_names_are_rejected,
    ),
    (
        "forged-snapshot",
        lambda: memory_catalog_subject(ForgingSnapshotCatalog),
        catalog_suite.check_unknown_table_and_foreign_snapshot_are_rejected,
    ),
    (
        "cross-table-snapshot",
        lambda: memory_catalog_subject(TableBlindSnapshotCatalog),
        catalog_suite.check_unknown_table_and_foreign_snapshot_are_rejected,
    ),
    (
        "mutable-history",
        lambda: memory_catalog_subject(RewritesHistoryCatalog),
        catalog_suite.check_snapshot_history_is_immutable,
    ),
    (
        "forgets-on-restart",
        lambda: CatalogSubject(
            open=lambda: MemoryCatalog(MemoryCatalogState(), KNOWN_DEFINITIONS),
            make_batch=make_row_batch,
            fingerprint=row_batch_fingerprint,
            definitions=(ALPHA, BETA),
            conflicting_definition=ALPHA_V2,
        ),
        catalog_suite.check_create_and_load_table,
    ),
    (
        "forgets-batches-on-restart",
        lambda: batch_amnesia_catalog_subject(),
        catalog_suite.check_replayed_batch_returns_the_same_commit,
    ),
)

COLLECTOR_KILLS: tuple[
    tuple[str, Callable[[], CollectorSubject], collector_suite.CollectorCheck], ...
] = (
    (
        "unpublished-object",
        lambda: archive_collector_subject(UnpublishedCollector),
        collector_suite.check_objects_are_published_and_intact,
    ),
    (
        "mismatched-object-ref",
        lambda: archive_collector_subject(MismatchedRefCollector),
        collector_suite.check_objects_are_published_and_intact,
    ),
    (
        "altered-request-echo",
        lambda: archive_collector_subject(EchoAlteringCollector),
        collector_suite.check_result_echoes_request_and_collector_identity,
    ),
    (
        "forged-incomplete-result",
        lambda: archive_collector_subject(ForgedResultCollector),
        collector_suite.check_result_echoes_request_and_collector_identity,
    ),
    (
        "non-replayable-keys",
        lambda: archive_collector_subject(NonReplayableCollector),
        collector_suite.check_replay_is_idempotent,
    ),
    (
        "undeclared-network-origin",
        lambda: archive_collector_subject(UndeclaredOriginCollector),
        collector_suite.check_network_sources_are_declared,
    ),
    (
        "undeclared-source-accepted",
        lambda: archive_collector_subject(AnySourceCollector),
        collector_suite.check_undeclared_source_is_rejected,
    ),
)


@pytest.mark.parametrize(
    ("make_subject", "check"),
    [pytest.param(make, check, id=name) for name, make, check in STORAGE_KILLS],
)
def test_storage_suite_kills_faulty_implementation(
    make_subject: Callable[[], StorageSubject], check: storage_suite.StorageCheck
) -> None:
    with pytest.raises(ContractSuiteFailure):
        check(make_subject())


@pytest.mark.parametrize(
    ("make_subject", "check"),
    [pytest.param(make, check, id=name) for name, make, check in CATALOG_KILLS],
)
def test_catalog_suite_kills_faulty_implementation(
    make_subject: Callable[[], CatalogSubject[Any]], check: catalog_suite.CatalogCheck
) -> None:
    with pytest.raises(ContractSuiteFailure):
        check(make_subject())


@pytest.mark.parametrize(
    ("make_subject", "check"),
    [pytest.param(make, check, id=name) for name, make, check in COLLECTOR_KILLS],
)
def test_collector_suite_kills_faulty_implementation(
    make_subject: Callable[[], CollectorSubject], check: collector_suite.CollectorCheck
) -> None:
    with pytest.raises(ContractSuiteFailure):
        check(make_subject())


def test_every_kill_uses_a_published_suite_check() -> None:
    """被杀死的检查都是公开 suite 的成员，不是为变体临时写的断言。"""
    assert {check for _, _, check in STORAGE_KILLS} <= set(storage_suite.STORAGE_CHECKS)
    assert {check for _, _, check in CATALOG_KILLS} <= set(catalog_suite.CATALOG_CHECKS)
    assert {check for _, _, check in COLLECTOR_KILLS} <= set(collector_suite.COLLECTOR_CHECKS)


def test_faulty_variants_are_otherwise_plausible() -> None:
    """每个故障变体都能通过 round-trip / 建表 / 回显类的基础检查：它只错在被杀死的那一处，
    因而 suite 杀死它依靠的是针对性的行为检查，而不是变体整体坏掉。"""
    for name, make, _ in STORAGE_KILLS:
        if name in {"staging-visible", "forgets-on-restart"}:
            continue
        storage_suite.check_zero_byte_object(make())
    for name, make_catalog, _ in CATALOG_KILLS:
        if name.startswith("forgets"):
            continue
        catalog_suite.check_commit_binds_snapshot_to_table_and_batch(make_catalog())
    for name, make_collector, _ in COLLECTOR_KILLS:
        if name in {"altered-request-echo", "forged-incomplete-result"}:
            continue
        collector_suite.check_result_echoes_request_and_collector_identity(make_collector())


# ======================================================================================
# 3. 静态替换性：替身满足 Protocol（由 mypy 检查下列标注）
# ======================================================================================


def test_fakes_are_statically_substitutable(tmp_path: Path) -> None:
    storages: list[StorageAdapter] = [
        DictStorage(DictStorageState()),
        BlobStorage(BlobStorageState()),
    ]
    row_catalog: CatalogAdapter[tuple[str, ...]] = MemoryCatalog(
        MemoryCatalogState(), KNOWN_DEFINITIONS
    )
    int_catalog: CatalogAdapter[list[int]] = JournalCatalog(tmp_path / "j.json", KNOWN_DEFINITIONS)
    collectors: list[CollectorAdapter] = [
        DailyArchiveCollector(storages[0], ARCHIVE),
        RangeImportCollector(storages[1], IMPORT_FILES),
    ]
    assert len(storages) == 2 and len(collectors) == 2
    assert row_catalog.load_table(ALPHA.table) is None
    assert int_catalog.load_table(ALPHA.table) is None

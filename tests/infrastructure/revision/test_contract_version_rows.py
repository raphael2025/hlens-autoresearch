"""Raw row builders under versioned replay (ADR-0052 "Implementation note — versioned replay").

Each single row builder takes the write group's contract version explicitly: ``None`` for a new
group (``new_group_version``: the current version, refused inside a replay scope), otherwise a
committed row's recorded version, which must be published. The row's ``contract_schema_version``
column is the envelope of the contract object the builder constructs from it (one source).
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from core.contracts.catalog import CommitRequest
from core.domain.base import (
    CONTRACT_SCHEMA_VERSION,
    PUBLISHED_CONTRACT_SCHEMA_VERSIONS,
    contract_schema_version_scope,
)
from infrastructure.catalog.iceberg_adapter import CatalogIntegrityError
from infrastructure.contract_version import ContractVersionScopeLeak
from infrastructure.revision.channel_precedence import build_channel_edge, compare_channels
from infrastructure.revision.exchange_info_store import snapshot_batch_id, snapshot_columns
from infrastructure.revision.row_integrity import batch
from tests.infrastructure.revision import exchange_info_support as xs
from tests.infrastructure.revision.exchange_info_support import EXCHANGE_INFO, T1, TRADING
from tests.infrastructure.revision.test_channel_precedence import K_REST, agg_pair

UNPUBLISHED = "2.99.0"


@pytest.fixture
def h(tmp_path: Path) -> Iterator[xs.Harness]:
    with xs.harness(tmp_path) as opened:
        yield opened


def test_a_snapshot_row_carries_the_version_it_is_built_at(h: xs.Harness) -> None:
    snapshot = h.collect("snap-1", TRADING, T1)
    row = snapshot_columns(snapshot, arrival_seq=0, knowledge_time=xs.KNOWLEDGE)
    assert row["contract_schema_version"] == CONTRACT_SCHEMA_VERSION
    for version in PUBLISHED_CONTRACT_SCHEMA_VERSIONS:
        rebuilt = snapshot_columns(
            snapshot, arrival_seq=0, knowledge_time=xs.KNOWLEDGE, contract_schema_version=version
        )
        assert rebuilt == dict(row, contract_schema_version=version)
    with pytest.raises(CatalogIntegrityError, match="not one of the published"):
        snapshot_columns(
            snapshot,
            arrival_seq=0,
            knowledge_time=xs.KNOWLEDGE,
            contract_schema_version=UNPUBLISHED,
        )


def test_a_committed_snapshot_at_an_unpublished_version_is_never_adopted(h: xs.Harness) -> None:
    snapshot = h.collect("snap-1", TRADING, T1)
    row = snapshot_columns(snapshot, arrival_seq=0, knowledge_time=xs.KNOWLEDGE)
    forged = batch(EXCHANGE_INFO, [dict(row, contract_schema_version=UNPUBLISHED)])
    h.adapter.commit_batch(
        CommitRequest(
            table=EXCHANGE_INFO.table,
            batch_id=snapshot_batch_id(row["revision_id"], 0),
            batch_fingerprint=EXCHANGE_INFO.fingerprint_rule.fingerprint(forged),
            row_count=1,
            expected_parent_snapshot_id=h.head(EXCHANGE_INFO.table),
        ),
        forged,
    )
    with pytest.raises(CatalogIntegrityError, match="not one of the published"):
        h.ingest("snap-1")
    deriver = h.deriver()
    try:
        with pytest.raises(CatalogIntegrityError, match="not one of the published"):
            deriver.derive()
    finally:
        deriver.close()


def test_a_new_snapshot_is_never_written_inside_a_replay_scope(h: xs.Harness) -> None:
    h.collect("snap-1", TRADING, T1)
    with contract_schema_version_scope(PUBLISHED_CONTRACT_SCHEMA_VERSIONS[0]):  # noqa: SIM117
        with pytest.raises(ContractVersionScopeLeak, match="replay scope"):
            h.ingest("snap-1")
    assert h.rows(EXCHANGE_INFO.table) == []
    assert h.ingest("snap-1").first_delivery  # outside the scope: written at the current version
    [row] = h.rows(EXCHANGE_INFO.table)
    assert row["contract_schema_version"] == CONTRACT_SCHEMA_VERSION


def test_an_edge_carries_the_version_it_is_built_at() -> None:
    comparison = compare_channels(*agg_pair())
    edge = build_channel_edge(comparison, knowledge_time=K_REST)
    assert edge.evidence.schema_version == CONTRACT_SCHEMA_VERSION
    assert edge.row()["contract_schema_version"] == edge.evidence.schema_version
    for version in PUBLISHED_CONTRACT_SCHEMA_VERSIONS:
        rebuilt = build_channel_edge(
            comparison, knowledge_time=K_REST, contract_schema_version=version
        )
        assert rebuilt.row() == dict(edge.row(), contract_schema_version=version)
    with pytest.raises(CatalogIntegrityError, match="not one of the published"):
        build_channel_edge(comparison, knowledge_time=K_REST, contract_schema_version=UNPUBLISHED)

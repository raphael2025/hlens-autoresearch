"""External PIT graph validation retains the RevisionGraph rejection rules."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

from core.contracts.revision import PrecedenceEvidence, RevisionGraph, RevisionRecord
from infrastructure.pit import graph_runs
from infrastructure.pit import selector as selector_module
from infrastructure.pit.graph_runs import PITGraphInvariantError, validate_pit_graph_runs
from infrastructure.pit.runs import RunLimits
from infrastructure.pit.selector import PitRunParams
from tests.infrastructure.revision import rest_store_support as storage_support
from tests.infrastructure.revision.test_precedence import KEY, T0, edge, record

_LIMITS = RunLimits(leaf_max_records=2, leaf_max_bytes=8192, fanout=2)


def _chain(size: int) -> tuple[tuple[RevisionRecord, ...], tuple[PrecedenceEvidence, ...]]:
    records = tuple(
        record(
            f"pit-revision-{index:04d}",
            supersedes=(f"pit-revision-{index - 1:04d}",) if index else (),
            seq=index,
        )
        for index in range(size)
    )
    edges = tuple(edge(item.revision_id, item.supersedes[0]) for item in records if item.supersedes)
    return records, edges


def test_graph_validation_spills_high_cardinality_chain_and_matches_contract(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with storage_support.sqlite_harness(tmp_path) as harness:
        records, edges = _chain(48)
        RevisionGraph(revisions=records, precedence_evidence=edges)
        maximum_pending = 0
        base_builder = graph_runs.RunSetBuilder

        class ObservedRunSetBuilder(base_builder):  # type: ignore[misc, valid-type]
            def add(self, row: Any) -> None:
                nonlocal maximum_pending
                super().add(row)
                maximum_pending = max(maximum_pending, len(self._rows))

        monkeypatch.setattr(graph_runs, "RunSetBuilder", ObservedRunSetBuilder)
        validate_pit_graph_runs(
            harness.storage,
            records,
            edges,
            cutoff=T0,
            capacity=2,
            merge_fanout=2,
            limits=_LIMITS,
        )
        assert maximum_pending <= 2


def test_run_backed_heads_prototype_matches_materialized_contract(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with storage_support.sqlite_harness(tmp_path) as harness:
        records, edges = _chain(16)
        available = {item.revision_id: T0 for item in records}
        expected = selector_module._heads(records, edges, T0, T0, available)
        calls = 0
        original = selector_module.validate_pit_graph_runs

        def observed(*args: Any, **kwargs: Any) -> None:
            nonlocal calls
            calls += 1
            original(*args, **kwargs)

        def no_materialized_contract_graph(*_args: Any, **_kwargs: Any) -> None:
            raise AssertionError("bounded _heads must use external RevisionGraph validation")

        monkeypatch.setattr(selector_module, "validate_pit_graph_runs", observed)
        monkeypatch.setattr(selector_module, "RevisionGraph", no_materialized_contract_graph)
        params = PitRunParams(
            row_batch_rows=2,
            edge_batch_rows=2,
            merge_fanout=2,
            key_history_buffer=2,
            limits=_LIMITS,
        )
        actual = selector_module._heads(
            records,
            edges,
            T0,
            T0,
            available,
            run_storage=harness.storage,
            run_params=params,
        )
        assert calls == 1
        assert actual == expected == (records[-1].revision_id,)


def test_graph_validator_rejects_each_duplicate_index(tmp_path: Path) -> None:
    with storage_support.sqlite_harness(tmp_path) as harness:
        first = record("duplicate", seq=1)
        duplicate_id = record("duplicate", seq=2)
        with pytest.raises(PITGraphInvariantError, match="revision_id 重复"):
            validate_pit_graph_runs(
                harness.storage,
                (first, duplicate_id),
                (),
                cutoff=T0,
                capacity=1,
                merge_fanout=2,
                limits=_LIMITS,
            )

        duplicate_arrival = record("other-id", seq=1)
        with pytest.raises(PITGraphInvariantError, match="arrival_seq 重复"):
            validate_pit_graph_runs(
                harness.storage,
                (first, duplicate_arrival),
                (),
                cutoff=T0,
                capacity=1,
                merge_fanout=2,
                limits=_LIMITS,
            )

        duplicate_payload = record("payload-id", seq=3).model_copy(
            update={"payload_hash": first.payload_hash}
        )
        with pytest.raises(PITGraphInvariantError, match="重复 payload"):
            validate_pit_graph_runs(
                harness.storage,
                (first, duplicate_payload),
                (),
                cutoff=T0,
                capacity=1,
                merge_fanout=2,
                limits=_LIMITS,
            )


def test_graph_validator_rejects_cross_key_claims_and_missing_or_late_evidence(
    tmp_path: Path,
) -> None:
    with storage_support.sqlite_harness(tmp_path) as harness:
        other_key = f"{KEY}:other"
        left = record("claim-left", seq=1)
        right = record("claim-right", supersedes=(left.revision_id,), seq=2).model_copy(
            update={"observation_key": other_key}
        )
        cross_key_edge = edge(right.revision_id, left.revision_id).model_copy(
            update={"observation_key": other_key}
        )
        with pytest.raises(PITGraphInvariantError, match="跨 observation_key"):
            validate_pit_graph_runs(
                harness.storage,
                (left, right),
                (cross_key_edge,),
                cutoff=T0,
                capacity=1,
                merge_fanout=2,
                limits=_LIMITS,
            )

        newer = record("needs-evidence", supersedes=(left.revision_id,), seq=4)
        with pytest.raises(PITGraphInvariantError, match="缺少不晚于该 revision"):
            validate_pit_graph_runs(
                harness.storage,
                (left, newer),
                (),
                cutoff=T0,
                capacity=1,
                merge_fanout=2,
                limits=_LIMITS,
            )

        late = edge(newer.revision_id, left.revision_id).model_copy(
            update={"knowledge_time": T0 + timedelta(seconds=1)}
        )
        with pytest.raises(PITGraphInvariantError, match="缺少不晚于该 revision"):
            validate_pit_graph_runs(
                harness.storage,
                (left, newer),
                (late,),
                cutoff=T0 + timedelta(seconds=1),
                capacity=1,
                merge_fanout=2,
                limits=_LIMITS,
            )


def test_graph_validator_rejects_cycles_after_spilling(tmp_path: Path) -> None:
    with storage_support.sqlite_harness(tmp_path) as harness:
        left = record("cycle-left", supersedes=("cycle-right",), seq=1)
        right = record("cycle-right", supersedes=("cycle-left",), seq=2)
        with pytest.raises(PITGraphInvariantError, match="supersedes 图不得成环"):
            validate_pit_graph_runs(
                harness.storage,
                (left, right),
                (
                    edge(left.revision_id, right.revision_id),
                    edge(right.revision_id, left.revision_id),
                ),
                cutoff=T0,
                capacity=1,
                merge_fanout=2,
                limits=_LIMITS,
            )

"""Direct semantic and lifecycle coverage for the SQLite PIT graph scratch slice."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from core.contracts.revision import RevisionGraph
from infrastructure.pit.graph_runs import PITGraphInvariantError
from infrastructure.pit.sqlite_graph import SQLitePitGraph
from infrastructure.revision.precedence import maximal_heads
from tests.infrastructure.revision.test_precedence import KEY, T0, edge, record


def _available(records: tuple) -> dict[str, datetime]:
    return {item.revision_id: item.availability.times.available_time for item in records}


def _validate(root: Path, records: tuple, evidence: tuple, cutoff=T0) -> None:
    with SQLitePitGraph(root, cutoff=cutoff) as graph:
        graph.build(records, evidence, _available(records))


def _oracle_heads(
    records: tuple, evidence: tuple, available: dict, at: datetime
) -> tuple[str, ...]:
    known_records = tuple(row for row in records if row.availability.times.knowledge_time <= T0)
    known_evidence = tuple(row for row in evidence if row.knowledge_time <= T0)
    RevisionGraph(revisions=known_records, precedence_evidence=known_evidence)
    candidates = tuple(row for row in known_records if available[row.revision_id] <= at)
    return maximal_heads(candidates, known_evidence)


def test_sqlite_graph_uses_invocation_directory_and_cleans_after_success(tmp_path: Path) -> None:
    with SQLitePitGraph(tmp_path / "scratch", cutoff=T0) as graph:
        owned = graph.directory
        assert owned.parent == tmp_path / "scratch"
        assert (owned / ".hlens-pit-graph-owner").is_file()
        rows = (record("one", seq=1),)
        graph.build(rows, (), _available(rows))
        assert graph.database.execute("PRAGMA mmap_size").fetchone() == (0,)
        assert graph.database.execute("PRAGMA cache_size").fetchone() == (-4096,)
        assert graph.database.execute("PRAGMA temp_store").fetchone() == (1,)
        plans = (
            graph.database.execute(
                "EXPLAIN QUERY PLAN SELECT revision_id, MIN(observation_key), "
                "MAX(observation_key) FROM claims INDEXED BY sqlite_autoindex_claims_1 "
                "GROUP BY revision_id ORDER BY revision_id"
            ).fetchall(),
            graph.database.execute(
                "EXPLAIN QUERY PLAN SELECT knowledge_us FROM evidence "
                "INDEXED BY sqlite_autoindex_evidence_1 WHERE newer=? AND older=? "
                "AND knowledge_us<=? ORDER BY knowledge_us LIMIT 1",
                ("n", "o", 1),
            ).fetchall(),
            graph.database.execute(
                "EXPLAIN QUERY PLAN SELECT DISTINCT available_us FROM revisions "
                "INDEXED BY revisions_by_availability WHERE available_us>? "
                "AND available_us<? ORDER BY available_us",
                (0, 1),
            ).fetchall(),
            graph.database.execute(
                "EXPLAIN QUERY PLAN SELECT c.revision_id FROM candidates AS c "
                "INDEXED BY sqlite_autoindex_candidates_1 WHERE NOT EXISTS "
                "(SELECT 1 FROM eliminated AS e WHERE e.revision_id=c.revision_id) "
                "ORDER BY c.revision_id",
            ).fetchall(),
            graph.database.execute(
                "EXPLAIN QUERY PLAN SELECT older FROM edges "
                "INDEXED BY sqlite_autoindex_edges_1 WHERE newer=? ORDER BY older",
                ("n",),
            ).fetchall(),
            graph.database.execute(
                "EXPLAIN QUERY PLAN SELECT revision_id FROM frontier ORDER BY revision_id LIMIT 1",
            ).fetchall(),
        )
        assert "SCAN claims" in str(plans[0])
        assert "PRIMARY KEY" in str(plans[1])
        assert "revisions_by_availability" in str(plans[2])
        assert "PRIMARY KEY" in str(plans[3])
        assert "PRIMARY KEY" in str(plans[4])
        assert "SCAN frontier" in str(plans[5])
        assert all("TEMP B-TREE" not in str(plan).upper() for plan in plans)
    assert not owned.exists()


def test_sqlite_graph_cleans_owned_directory_after_validation_error(tmp_path: Path) -> None:
    graph = SQLitePitGraph(tmp_path / "scratch", cutoff=T0)
    with graph:
        owned = graph.directory
        duplicate_rows = (record("same", seq=1), record("same", seq=2))
        with pytest.raises(PITGraphInvariantError, match="revision_id 重复"):
            graph.build(duplicate_rows, (), _available(duplicate_rows))
        with pytest.raises(RuntimeError, match="cannot be retried"):
            graph.build(duplicate_rows, (), _available(duplicate_rows))
    assert not owned.exists()


def test_sqlite_graph_rejects_nested_enter_without_disturbing_current_owner(
    tmp_path: Path,
) -> None:
    graph = SQLitePitGraph(tmp_path / "scratch", cutoff=T0)
    with graph:
        owned = graph.directory
        connection = graph.database
        with pytest.raises(RuntimeError, match="already open"):
            with graph:
                pytest.fail("nested context must not acquire a second directory")
        assert graph.directory == owned
        assert graph.database is connection
        assert connection.execute("SELECT 1").fetchone() == (1,)
        assert owned.is_dir()
    assert not owned.exists()


@pytest.mark.parametrize("failure_point", ["open", "write"])
def test_sqlite_graph_marker_failure_cleans_owned_directory_and_preserves_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure_point: str,
) -> None:
    real_open = Path.open

    class FailedWriter:
        def __init__(self, handle: Any) -> None:
            self.handle = handle

        def __enter__(self) -> FailedWriter:
            return self

        def __exit__(self, *_args: object) -> None:
            self.handle.close()

        def write(self, _value: str) -> None:
            raise OSError("marker-write-failure")

    def injected_open(path: Path, *args: Any, **kwargs: Any) -> Any:
        mode = args[0] if args else kwargs.get("mode", "r")
        if path.name == ".hlens-pit-graph-owner" and mode == "x":
            if failure_point == "open":
                raise OSError("marker-open-failure")
            return FailedWriter(real_open(path, *args, **kwargs))
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", injected_open)
    scratch = tmp_path / "scratch"
    graph = SQLitePitGraph(scratch, cutoff=T0)
    message = f"marker-{failure_point}-failure"
    with pytest.raises(OSError, match=message):
        graph._open()
    assert graph._db is None
    assert graph._directory is None
    assert list(scratch.iterdir()) == []


def test_sqlite_graph_schema_setup_failure_closes_database_and_cleans_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    connections: list[sqlite3.Connection] = []
    graph = SQLitePitGraph(tmp_path / "scratch", cutoff=T0)

    def fail_schema(instance: SQLitePitGraph) -> None:
        connections.append(instance.database)
        raise RuntimeError("schema-setup-failure")

    monkeypatch.setattr(SQLitePitGraph, "_schema", fail_schema)
    with pytest.raises(RuntimeError, match="schema-setup-failure"):
        graph._open()
    assert graph._db is None
    assert len(connections) == 1
    with pytest.raises(sqlite3.ProgrammingError, match="closed database"):
        connections[0].execute("SELECT 1")
    assert list((tmp_path / "scratch").iterdir()) == []


@pytest.mark.parametrize(
    ("rows", "message"),
    [
        ((record("same", seq=1), record("same", seq=2)), "revision_id 重复"),
        ((record("left", seq=4), record("right", seq=4)), "arrival_seq 重复"),
        (
            (
                record("left", seq=1),
                record("right", seq=2).model_copy(
                    update={"payload_hash": record("left", seq=1).payload_hash}
                ),
            ),
            "重复 payload",
        ),
    ],
)
def test_sqlite_graph_rejects_duplicate_record_indexes(
    tmp_path: Path, rows: tuple, message: str
) -> None:
    with pytest.raises(PITGraphInvariantError, match=message):
        _validate(tmp_path / "scratch", rows, ())


def test_sqlite_graph_rejects_cross_key_claims_and_missing_or_late_evidence(
    tmp_path: Path,
) -> None:
    first = record("first", seq=1)
    other_key = record("other", supersedes=(first.revision_id,), seq=2).model_copy(
        update={"observation_key": f"{KEY}:other"}
    )
    cross = edge(other_key.revision_id, first.revision_id).model_copy(
        update={"observation_key": f"{KEY}:other"}
    )
    with pytest.raises(PITGraphInvariantError, match="跨 observation_key"):
        _validate(tmp_path / "claims", (first, other_key), (cross,))

    newer = record("needs-edge", supersedes=(first.revision_id,), seq=3)
    with pytest.raises(PITGraphInvariantError, match="缺少不晚于该 revision"):
        _validate(tmp_path / "missing", (first, newer), ())
    late = edge(newer.revision_id, first.revision_id).model_copy(
        update={"knowledge_time": T0 + timedelta(seconds=1)}
    )
    with pytest.raises(PITGraphInvariantError, match="缺少不晚于该 revision"):
        _validate(tmp_path / "late", (first, newer), (late,), T0 + timedelta(seconds=1))


def test_sqlite_graph_rejects_cycles_and_accepts_dangling_older_endpoint(tmp_path: Path) -> None:
    left = record("left", supersedes=("right",), seq=1)
    right = record("right", supersedes=("left",), seq=2)
    with pytest.raises(PITGraphInvariantError, match="supersedes 图不得成环"):
        _validate(
            tmp_path / "cycle",
            (left, right),
            (edge("left", "right"), edge("right", "left")),
        )

    # RevisionGraph allows dangling predecessor ids when their evidence is valid.
    child = record("child", supersedes=("not-loaded",), seq=3)
    _validate(tmp_path / "dangling", (child,), (edge("child", "not-loaded"),))


def test_sqlite_head_summary_matches_oracle_for_long_chain_and_unavailable_intermediate(
    tmp_path: Path,
) -> None:
    chain = tuple(
        record(
            f"chain-{index:04d}",
            supersedes=(f"chain-{index - 1:04d}",) if index else (),
            seq=index,
        )
        for index in range(80)
    )
    chain_edges = tuple(
        edge(item.revision_id, item.supersedes[0]) for item in chain if item.supersedes
    )
    chain_available = _available(chain)
    with SQLitePitGraph(tmp_path / "chain", cutoff=T0) as graph:
        graph.build(chain, chain_edges, chain_available)
        actual = graph.head_summary(T0, emit=lambda *_: pytest.fail("single head emitted conflict"))
    assert actual == (1, _oracle_heads(chain, chain_edges, chain_available, T0)[0])

    old = record("old", seq=1)
    hidden = record("hidden", supersedes=("old",), seq=2)
    top = record("top", supersedes=("hidden",), seq=3)
    records = (old, hidden, top)
    edges = (edge("hidden", "old"), edge("top", "hidden"))
    available = {"old": T0, "hidden": T0 + timedelta(seconds=2), "top": T0}
    with SQLitePitGraph(tmp_path / "intermediate", cutoff=T0) as graph:
        graph.build(records, edges, available)
        actual = graph.head_summary(T0, emit=lambda *_: pytest.fail("single head emitted conflict"))
    assert actual == (1, _oracle_heads(records, edges, available, T0)[0]) == (1, "top")


def test_sqlite_head_summary_emits_all_conflict_heads_in_canonical_order(tmp_path: Path) -> None:
    base = record("base", seq=0)
    head_ids = tuple(f"head-{index:04d}" for index in reversed(range(64)))
    heads = tuple(record(head, supersedes=("base",), seq=i + 1) for i, head in enumerate(head_ids))
    records = (base, *heads)
    edges = tuple(edge(head.revision_id, "base") for head in heads)
    available = {item.revision_id: T0 for item in records}
    emitted: list[tuple[int, int, str]] = []
    with SQLitePitGraph(tmp_path / "wide", cutoff=T0) as graph:
        graph.build(records, edges, available)
        actual = graph.head_summary(T0, emit=lambda *row: emitted.append(row))
    expected = _oracle_heads(records, edges, available, T0)
    assert actual == (len(expected), None)
    assert emitted == [
        (len(expected), ordinal, revision) for ordinal, revision in enumerate(expected)
    ]


def test_sqlite_graph_builds_once_and_reuses_availability_for_multiple_cutoffs(
    tmp_path: Path,
) -> None:
    older = record("older", seq=1)
    newer = record("newer", supersedes=("older",), seq=2)
    rows = (older, newer)
    evidence = (edge("newer", "older"),)
    effective = {"older": T0, "newer": T0 + timedelta(seconds=2)}

    class CountedAvailability(dict[str, datetime]):
        def __init__(self, values: dict[str, datetime]) -> None:
            super().__init__(values)
            self.reads = {"older": 0, "newer": 0}

        def __getitem__(self, key: str) -> datetime:
            self.reads[key] += 1
            return super().__getitem__(key)

    available = CountedAvailability(effective)
    with SQLitePitGraph(tmp_path / "timeline", cutoff=T0) as graph:
        graph.build(rows, evidence, available)
        assert available.reads == {"older": 1, "newer": 1}
        with graph.availability_times(
            T0 - timedelta(seconds=1), T0 + timedelta(seconds=3)
        ) as times:
            instants = tuple(times)
        assert instants == (T0, T0 + timedelta(seconds=2))
        observed = [
            graph.head_summary(at, emit=lambda *_: None)
            for at in (T0 - timedelta(seconds=1), *instants)
        ]

    expected = [
        _oracle_heads(rows, evidence, effective, at)
        for at in (T0 - timedelta(seconds=1), *instants)
    ]
    assert observed == [(len(heads), heads[0] if len(heads) == 1 else None) for heads in expected]


def test_sqlite_head_summary_cleans_after_emitter_failure(tmp_path: Path) -> None:
    rows = (record("a", seq=1), record("b", seq=2))
    available = _available(rows)
    owned: Path
    with pytest.raises(RuntimeError, match="emit-failure"):
        with SQLitePitGraph(tmp_path / "scratch", cutoff=T0) as graph:
            owned = graph.directory
            graph.build(rows, (), available)

            def fail_emit(*_args: object) -> None:
                raise RuntimeError("emit-failure")

            graph.head_summary(T0, emit=fail_emit)
    assert not owned.exists()

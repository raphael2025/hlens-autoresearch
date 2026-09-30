"""Direct semantic and lifecycle coverage for the SQLite PIT graph scratch slice."""

from __future__ import annotations

import sqlite3
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

from infrastructure.pit.graph_runs import PITGraphInvariantError
from infrastructure.pit.sqlite_graph import SQLitePitGraph
from tests.infrastructure.revision.test_precedence import KEY, T0, edge, record


def _validate(root: Path, records: tuple, evidence: tuple, cutoff=T0) -> None:
    with SQLitePitGraph(root, cutoff=cutoff) as graph:
        graph.build(records, evidence)


def test_sqlite_graph_uses_invocation_directory_and_cleans_after_success(tmp_path: Path) -> None:
    with SQLitePitGraph(tmp_path / "scratch", cutoff=T0) as graph:
        owned = graph.directory
        assert owned.parent == tmp_path / "scratch"
        assert (owned / ".hlens-pit-graph-owner").is_file()
        graph.build((record("one", seq=1),), ())
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
        )
        assert "SCAN claims" in str(plans[0])
        assert "PRIMARY KEY" in str(plans[1])
        assert all("TEMP B-TREE" not in str(plan).upper() for plan in plans)
    assert not owned.exists()


def test_sqlite_graph_cleans_owned_directory_after_validation_error(tmp_path: Path) -> None:
    graph = SQLitePitGraph(tmp_path / "scratch", cutoff=T0)
    with graph:
        owned = graph.directory
        duplicate_rows = (record("same", seq=1), record("same", seq=2))
        with pytest.raises(PITGraphInvariantError, match="revision_id 重复"):
            graph.build(duplicate_rows, ())
        with pytest.raises(RuntimeError, match="cannot be retried"):
            graph.build(duplicate_rows, ())
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

"""Direct semantic and lifecycle coverage for the SQLite PIT graph scratch slice."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

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
    assert not owned.exists()


def test_sqlite_graph_cleans_owned_directory_after_validation_error(tmp_path: Path) -> None:
    graph = SQLitePitGraph(tmp_path / "scratch", cutoff=T0)
    with pytest.raises(PITGraphInvariantError, match="revision_id 重复"):
        with graph:
            owned = graph.directory
            graph.build((record("same", seq=1), record("same", seq=2)), ())
    assert not owned.exists()


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

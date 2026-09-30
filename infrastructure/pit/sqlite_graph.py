"""Invocation-owned SQLite scratch index for validating and traversing one PIT revision graph.

The input is streamed into indexed tables; graph validation and maximal-head traversal retain no
graph-sized Python maps. Scratch disk and the rest of process RSS remain subject to E1-CAP-1
measurement.
"""

from __future__ import annotations

import shutil
import sqlite3
import uuid
from collections.abc import Callable, Iterable, Iterator, Mapping
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path

from core.contracts.revision import PrecedenceEvidence, RevisionRecord
from infrastructure.pit.graph_runs import PITGraphInvariantError

_MARKER = ".hlens-pit-graph-owner"
_FORMAT = "hlens.pit.sqlite-graph@1"
_DB = "graph.sqlite3"
_CACHE_KIB = 4096
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


def _us(value: datetime) -> int:
    if value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise PITGraphInvariantError("PIT graph timestamps must be UTC")
    delta = value.astimezone(UTC) - _EPOCH
    return (delta.days * 86_400 + delta.seconds) * 1_000_000 + delta.microseconds


class SQLitePitGraph:
    """Own and validate a cutoff-specific graph in a private SQLite scratch directory."""

    def __init__(self, scratch_directory: Path, *, cutoff: datetime) -> None:
        if not isinstance(scratch_directory, Path) or not scratch_directory.is_absolute():
            raise ValueError("scratch_directory must be an explicit absolute Path")
        self._root = scratch_directory.resolve(strict=False)
        self._cutoff = cutoff
        self._directory: Path | None = None
        self._token: str | None = None
        self._directory_created = False
        self._initializing = False
        self._db: sqlite3.Connection | None = None
        self._build_started = False

    def __enter__(self) -> SQLitePitGraph:
        self._open()
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        self.close()

    @property
    def directory(self) -> Path:
        if self._directory is None:
            raise RuntimeError("SQLite PIT graph is not open")
        return self._directory

    @property
    def database(self) -> sqlite3.Connection:
        if self._db is None:
            raise RuntimeError("SQLite PIT graph is closed")
        return self._db

    def _open(self) -> None:
        if self._directory is not None or self._db is not None:
            raise RuntimeError("SQLite PIT graph is already open")
        self._root.mkdir(parents=True, exist_ok=True)
        if not self._root.is_dir():
            raise OSError("configured PIT scratch path is not a directory")
        for _ in range(8):
            token = uuid.uuid4().hex
            owned = self._root / f"pit-graph-{token}"
            try:
                owned.mkdir(mode=0o700)
            except FileExistsError:
                continue
            self._directory, self._token = owned, token
            self._directory_created = True
            self._initializing = True
            try:
                with (owned / _MARKER).open("x", encoding="ascii") as marker:
                    marker.write(f"{_FORMAT}\n{token}\n")
                self._db = sqlite3.connect(str(owned / _DB), isolation_level=None)
                self._configure()
                self._schema()
                self._initializing = False
                return
            except BaseException:
                # Keep the setup exception primary if closing/removing the just-created
                # directory also encounters an error.
                try:
                    self.close()
                except BaseException:
                    pass
                raise
        raise OSError("cannot allocate a unique PIT graph scratch directory")

    @contextmanager
    def _cursor(self) -> Iterator[sqlite3.Cursor]:
        cursor = self.database.cursor()
        try:
            yield cursor
        finally:
            cursor.close()

    def _set_pragma(self, name: str, value: int) -> None:
        with self._cursor() as cursor:
            cursor.execute(f"PRAGMA {name}={value}")
            cursor.fetchone()
            cursor.execute(f"PRAGMA {name}")
            result = cursor.fetchone()
        if result is None or type(result[0]) is not int or result[0] != value:
            raise RuntimeError(f"SQLite did not honor PRAGMA {name}={value}: {result!r}")

    def _configure(self) -> None:
        self._set_pragma("mmap_size", 0)
        self._set_pragma("cache_size", -_CACHE_KIB)
        self._set_pragma("automatic_index", 0)
        self._set_pragma("temp_store", 1)
        with self._cursor() as cursor:
            cursor.execute("PRAGMA journal_mode=OFF")
            if cursor.fetchone() != ("off",):
                raise RuntimeError("SQLite journal could not be disabled")
            cursor.execute("PRAGMA synchronous=OFF")
            cursor.fetchone()

    def _schema(self) -> None:
        self.database.executescript(
            """
            CREATE TABLE revisions (
                revision_id TEXT PRIMARY KEY,
                arrival_seq INTEGER NOT NULL UNIQUE,
                observation_key TEXT NOT NULL,
                source_id TEXT NOT NULL,
                payload_hash TEXT NOT NULL,
                knowledge_us INTEGER NOT NULL,
                available_us INTEGER NOT NULL,
                UNIQUE(observation_key, source_id, payload_hash)
            ) WITHOUT ROWID;
            CREATE INDEX revisions_by_availability ON revisions(available_us, revision_id);
            CREATE TABLE claims (
                revision_id TEXT NOT NULL,
                observation_key TEXT NOT NULL,
                PRIMARY KEY(revision_id, observation_key)
            ) WITHOUT ROWID;
            CREATE TABLE required_evidence (
                newer TEXT NOT NULL, older TEXT NOT NULL, knowledge_us INTEGER NOT NULL,
                PRIMARY KEY(newer, older, knowledge_us)
            ) WITHOUT ROWID;
            CREATE TABLE evidence (
                newer TEXT NOT NULL, older TEXT NOT NULL, knowledge_us INTEGER NOT NULL,
                PRIMARY KEY(newer, older, knowledge_us)
            ) WITHOUT ROWID;
            CREATE TABLE edges (
                newer TEXT NOT NULL, older TEXT NOT NULL, PRIMARY KEY(newer, older)
            ) WITHOUT ROWID;
            CREATE INDEX edges_by_older ON edges(older, newer);
            CREATE TABLE colors (revision_id TEXT PRIMARY KEY, color TEXT NOT NULL)
                WITHOUT ROWID;
            CREATE TABLE dfs (depth INTEGER PRIMARY KEY, revision_id TEXT NOT NULL,
                              after_older TEXT);
            CREATE TABLE candidates (revision_id TEXT PRIMARY KEY) WITHOUT ROWID;
            CREATE TABLE frontier (revision_id TEXT PRIMARY KEY) WITHOUT ROWID;
            CREATE TABLE visited (revision_id TEXT PRIMARY KEY) WITHOUT ROWID;
            CREATE TABLE eliminated (revision_id TEXT PRIMARY KEY) WITHOUT ROWID;
            """
        )

    def build(
        self,
        records: Iterable[RevisionRecord],
        evidence: Iterable[PrecedenceEvidence],
        available: Mapping[str, datetime],
    ) -> None:
        """Stream known rows into indexed tables, then check RevisionGraph invariants."""
        if self._build_started:
            raise RuntimeError("SQLite PIT graph build cannot be retried")
        self._build_started = True
        cutoff = _us(self._cutoff)
        db = self.database
        db.execute("BEGIN IMMEDIATE").close()
        try:
            with (
                self._cursor() as rev,
                self._cursor() as claim,
                self._cursor() as required,
                self._cursor() as ev,
                self._cursor() as edge,
            ):
                for row in records:
                    know = _us(row.availability.times.knowledge_time)
                    if know > cutoff:
                        continue
                    available_us = _us(available[row.revision_id])
                    # Keep RevisionGraph's deterministic diagnostic precedence: id, arrival,
                    # then payload. SQLite may otherwise report whichever UNIQUE index wins.
                    if self._exists(
                        "SELECT 1 FROM revisions WHERE revision_id=?", (row.revision_id,)
                    ):
                        raise PITGraphInvariantError(f"revision_id 重复：{row.revision_id!r}")
                    if self._exists(
                        "SELECT 1 FROM revisions WHERE arrival_seq=?", (row.arrival_seq,)
                    ):
                        raise PITGraphInvariantError(f"arrival_seq 重复：{row.arrival_seq}")
                    if self._exists(
                        "SELECT 1 FROM revisions WHERE observation_key=? AND source_id=? "
                        "AND payload_hash=?",
                        (row.observation_key, row.source_id, row.payload_hash),
                    ):
                        raise PITGraphInvariantError(
                            "重复 payload（同一 observation_key 下 source_id + payload_hash "
                            f"相同）：{row.revision_id!r} 不得成为新 revision"
                        )
                    try:
                        rev.execute(
                            "INSERT INTO revisions VALUES (?, ?, ?, ?, ?, ?, ?)",
                            (
                                row.revision_id,
                                row.arrival_seq,
                                row.observation_key,
                                row.source_id,
                                row.payload_hash,
                                know,
                                available_us,
                            ),
                        )
                    except sqlite3.IntegrityError as exc:
                        detail = str(exc)
                        if "revision_id" in detail:
                            message = f"revision_id 重复：{row.revision_id!r}"
                        elif "arrival_seq" in detail:
                            message = f"arrival_seq 重复：{row.arrival_seq}"
                        elif "observation_key, revisions.source_id" in detail:
                            message = (
                                "重复 payload（同一 observation_key 下 source_id + "
                                "payload_hash 相同）"
                            )
                        else:
                            raise
                        raise PITGraphInvariantError(message) from None
                    claim.execute(
                        "INSERT OR IGNORE INTO claims VALUES (?, ?)",
                        (row.revision_id, row.observation_key),
                    )
                    for older in row.supersedes:
                        claim.execute(
                            "INSERT OR IGNORE INTO claims VALUES (?, ?)",
                            (older, row.observation_key),
                        )
                        required.execute(
                            "INSERT OR IGNORE INTO required_evidence VALUES (?, ?, ?)",
                            (row.revision_id, older, know),
                        )
                        edge.execute(
                            "INSERT OR IGNORE INTO edges VALUES (?, ?)", (row.revision_id, older)
                        )
                for item in evidence:
                    know = _us(item.knowledge_time)
                    if know > cutoff:
                        continue
                    claim.execute(
                        "INSERT OR IGNORE INTO claims VALUES (?, ?)",
                        (item.revision_id, item.observation_key),
                    )
                    claim.execute(
                        "INSERT OR IGNORE INTO claims VALUES (?, ?)",
                        (item.superseded_revision_id, item.observation_key),
                    )
                    ev.execute(
                        "INSERT OR IGNORE INTO evidence VALUES (?, ?, ?)",
                        (item.revision_id, item.superseded_revision_id, know),
                    )
                    edge.execute(
                        "INSERT OR IGNORE INTO edges VALUES (?, ?)",
                        (item.revision_id, item.superseded_revision_id),
                    )
            self._check_claims()
            self._check_required_evidence()
            self._check_cycles()
            db.commit()
        except BaseException:
            db.rollback()
            raise

    @contextmanager
    def availability_times(self, start: datetime, end: datetime) -> Iterator[Iterator[datetime]]:
        """Stream distinct effective instants from the caller-sized availability index."""
        low, high = _us(start), _us(end)
        cursor = self.database.cursor()
        try:
            cursor.execute(
                "SELECT DISTINCT available_us FROM revisions "
                "INDEXED BY revisions_by_availability "
                "WHERE available_us>? AND available_us<? ORDER BY available_us",
                (low, high),
            )

            def times() -> Iterator[datetime]:
                while (row := cursor.fetchone()) is not None:
                    yield _EPOCH + timedelta(microseconds=int(row[0]))

            yield times()
        finally:
            cursor.close()

    def head_summary(
        self,
        at: datetime,
        *,
        emit: Callable[[int, int, str], None],
    ) -> tuple[int, str | None]:
        """Return the complete maximal-head summary for one instant using indexed reachability."""
        at_us = _us(at)
        for table in ("candidates", "frontier", "visited", "eliminated"):
            self.database.execute(f"DELETE FROM {table}").close()
        self.database.execute(
            "INSERT INTO candidates SELECT revision_id FROM revisions "
            "INDEXED BY revisions_by_availability WHERE available_us<=?",
            (at_us,),
        ).close()

        # All candidates seed one shared traversal. Each reachable node is expanded once per
        # cutoff; graph edges remain available through non-candidate intermediate revisions.
        with self._cursor() as candidates:
            candidates.execute("SELECT revision_id FROM candidates")
            for (candidate,) in candidates:
                with self._cursor() as adjacent:
                    adjacent.execute(
                        "SELECT older FROM edges INDEXED BY sqlite_autoindex_edges_1 "
                        "WHERE newer=? ORDER BY older",
                        (candidate,),
                    )
                    while (row := adjacent.fetchone()) is not None:
                        self._enqueue(str(row[0]))

        while (node := self._pop_frontier()) is not None:
            if self._exists("SELECT 1 FROM candidates WHERE revision_id=?", (node,)):
                self.database.execute(
                    "INSERT OR IGNORE INTO eliminated VALUES (?)", (node,)
                ).close()
            with self._cursor() as adjacent:
                adjacent.execute(
                    "SELECT older FROM edges INDEXED BY sqlite_autoindex_edges_1 "
                    "WHERE newer=? ORDER BY older",
                    (node,),
                )
                while (row := adjacent.fetchone()) is not None:
                    self._enqueue(str(row[0]))

        count = self._scalar(
            "SELECT COUNT(*) FROM candidates AS c WHERE NOT EXISTS "
            "(SELECT 1 FROM eliminated AS e WHERE e.revision_id=c.revision_id)"
        )
        if count == 0:
            return 0, None
        first: str | None = None
        ordinal = 0
        with self._cursor() as heads:
            heads.execute(
                "SELECT c.revision_id FROM candidates AS c "
                "INDEXED BY sqlite_autoindex_candidates_1 WHERE NOT EXISTS "
                "(SELECT 1 FROM eliminated AS e WHERE e.revision_id=c.revision_id) "
                "ORDER BY c.revision_id"
            )
            while (row := heads.fetchone()) is not None:
                revision_id = str(row[0])
                if ordinal == 0:
                    first = revision_id
                if count > 1:
                    emit(count, ordinal, revision_id)
                ordinal += 1
        if ordinal != count:
            raise PITGraphInvariantError("SQLite maximal-head count changed during emission")
        return count, first if count == 1 else None

    def _scalar(self, sql: str, parameters: tuple[object, ...] = ()) -> int:
        with self._cursor() as cursor:
            cursor.execute(sql, parameters)
            row = cursor.fetchone()
        if row is None or type(row[0]) is not int:
            raise PITGraphInvariantError("SQLite graph scalar query returned no integer")
        return row[0]

    def availability_for(self, revision_id: str) -> datetime:
        with self._cursor() as cursor:
            cursor.execute("SELECT available_us FROM revisions WHERE revision_id=?", (revision_id,))
            row = cursor.fetchone()
        if row is None:
            raise PITGraphInvariantError("selected revision is absent from the SQLite PIT graph")
        return _EPOCH + timedelta(microseconds=int(row[0]))

    def _enqueue(self, revision_id: str) -> None:
        with self._cursor() as cursor:
            cursor.execute("INSERT OR IGNORE INTO visited VALUES (?)", (revision_id,))
            if cursor.rowcount == 0:
                return
            cursor.execute("INSERT OR IGNORE INTO frontier VALUES (?)", (revision_id,))

    def _pop_frontier(self) -> str | None:
        with self._cursor() as cursor:
            cursor.execute("SELECT revision_id FROM frontier ORDER BY revision_id LIMIT 1")
            row = cursor.fetchone()
            if row is None:
                return None
            revision_id = str(row[0])
            cursor.execute("DELETE FROM frontier WHERE revision_id=?", (revision_id,))
        return revision_id

    def _check_claims(self) -> None:
        sql = (
            "SELECT revision_id, MIN(observation_key), MAX(observation_key) FROM claims "
            "INDEXED BY sqlite_autoindex_claims_1 GROUP BY revision_id "
            "HAVING MIN(observation_key)<>MAX(observation_key) "
            "ORDER BY revision_id LIMIT 1"
        )
        with self._cursor() as cursor:
            cursor.execute(sql)
            conflict = cursor.fetchone()
        if conflict is not None:
            rid, left, right = conflict
            raise PITGraphInvariantError(
                f"revision_id {rid!r} 跨 observation_key 归属冲突："
                f"同时被 {sorted((left, right))} 认领"
            )

    def _exists(self, sql: str, parameters: tuple[object, ...]) -> bool:
        with self._cursor() as cursor:
            cursor.execute(sql, parameters)
            return cursor.fetchone() is not None

    def _check_required_evidence(self) -> None:
        sql = (
            "SELECT r.newer, r.older FROM required_evidence AS r "
            "WHERE NOT EXISTS (SELECT 1 FROM evidence AS e "
            "INDEXED BY sqlite_autoindex_evidence_1 WHERE e.newer=r.newer AND e.older=r.older "
            "AND e.knowledge_us<=r.knowledge_us) "
            "ORDER BY r.newer, r.older, r.knowledge_us LIMIT 1"
        )
        with self._cursor() as cursor:
            cursor.execute(sql)
            missing = cursor.fetchone()
        if missing is not None:
            raise PITGraphInvariantError(
                f"supersedes 边 {missing[0]!r} → {missing[1]!r} 缺少不晚于该 revision "
                "knowledge_time 的 precedence 证据"
            )

    def _color(self, revision_id: str) -> str | None:
        with self._cursor() as cursor:
            cursor.execute("SELECT color FROM colors WHERE revision_id=?", (revision_id,))
            row = cursor.fetchone()
        return None if row is None else str(row[0])

    def _set_color(self, revision_id: str, color: str) -> None:
        with self._cursor() as cursor:
            cursor.execute(
                "INSERT INTO colors VALUES (?, ?) ON CONFLICT(revision_id) "
                "DO UPDATE SET color=excluded.color",
                (revision_id, color),
            )

    def _check_cycles(self) -> None:
        with self._cursor() as roots:
            roots.execute(
                "SELECT newer FROM edges INDEXED BY sqlite_autoindex_edges_1 "
                "GROUP BY newer ORDER BY newer"
            )
            for (root,) in roots:
                if self._color(str(root)) is not None:
                    continue
                self._set_color(str(root), "g")
                depth = 0
                self._put_frame(depth, str(root), None)
                while depth >= 0:
                    node, after = self._frame(depth)
                    older = self._successor(node, after)
                    if older is None:
                        self._set_color(node, "b")
                        self._delete_frame(depth)
                        depth -= 1
                        continue
                    self._put_frame(depth, node, older)
                    color = self._color(older)
                    if color == "g":
                        raise PITGraphInvariantError(
                            f"supersedes 图不得成环：回边 {node!r} → {older!r}"
                        )
                    if color == "b":
                        continue
                    self._set_color(older, "g")
                    depth += 1
                    self._put_frame(depth, older, None)

    def _successor(self, node: str, after: str | None) -> str | None:
        sql: str
        params: tuple[str, ...]
        if after is None:
            sql, params = "SELECT older FROM edges WHERE newer=? ORDER BY older LIMIT 1", (node,)
        else:
            sql, params = (
                ("SELECT older FROM edges WHERE newer=? AND older>? ORDER BY older LIMIT 1"),
                (node, after),
            )
        with self._cursor() as cursor:
            cursor.execute(sql, params)
            row = cursor.fetchone()
        return None if row is None else str(row[0])

    def _put_frame(self, depth: int, node: str, after: str | None) -> None:
        with self._cursor() as cursor:
            cursor.execute(
                "INSERT INTO dfs VALUES (?, ?, ?) ON CONFLICT(depth) DO UPDATE SET "
                "revision_id=excluded.revision_id, after_older=excluded.after_older",
                (depth, node, after),
            )

    def _frame(self, depth: int) -> tuple[str, str | None]:
        with self._cursor() as cursor:
            cursor.execute("SELECT revision_id, after_older FROM dfs WHERE depth=?", (depth,))
            row = cursor.fetchone()
        if row is None:
            raise PITGraphInvariantError("SQLite graph DFS frame is missing")
        return str(row[0]), None if row[1] is None else str(row[1])

    def _delete_frame(self, depth: int) -> None:
        with self._cursor() as cursor:
            cursor.execute("DELETE FROM dfs WHERE depth=?", (depth,))

    def close(self) -> None:
        db, self._db = self._db, None
        if db is not None:
            db.close()
        directory, token = self._directory, self._token
        directory_created, initializing = self._directory_created, self._initializing
        self._directory = self._token = None
        self._directory_created = self._initializing = False
        if directory is None or token is None:
            return
        marker = directory / _MARKER
        try:
            valid = marker.read_text(encoding="ascii") == f"{_FORMAT}\n{token}\n"
        except OSError:
            valid = False
        if directory.parent != self._root or not directory_created:
            raise OSError(
                "PIT graph scratch path is not owned by this invocation; directory retained"
            )
        if not valid and not initializing:
            raise OSError("PIT graph scratch ownership marker mismatch; directory retained")
        shutil.rmtree(directory)

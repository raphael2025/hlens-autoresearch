"""G3-P: the key-closure read at scale equals the single ``In(keys)`` scan it replaces.

PyIceberg stops pruning by an ``In`` set above 200 literals and converts the whole set per data
file, so ``PitSelector`` locates the window's keys per UTC day and fetches only the hours holding
them. These tests pin that the rows are exactly those of the pre-G3-P read (one scan filtered by
``In(observation_key, keys)``), with more than 200 keys, several data files, a key whose
revisions lie ten hours apart, windows on and off hour boundaries, and fetches split to one hour.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta
from typing import Any

import pyarrow as pa  # type: ignore[import-untyped]
import pytest
from pyiceberg.expressions import And, BooleanExpression, EqualTo, GreaterThanOrEqual, In, LessThan

from infrastructure.canonical import rules
from infrastructure.pit import selector as selector_module
from infrastructure.pit.selector import PitSelector
from infrastructure.pit.view import PinnedCatalogView
from tests.infrastructure.canonical import canonical_support as c
from tests.infrastructure.pit.test_selector import FAR, K_A, K_R, N_A, N_R, _spec
from tests.infrastructure.revision import rest_store_support as ss
from tests.infrastructure.revision.rest_store_support import SYMBOL, RestHarness, StepClock, utc

HOUR = timedelta(hours=1)
TRADES = c.TRADES.table
COLUMNS = tuple(field.name for field in c.TRADES.arrow_schema)


def _window(low: datetime, high: datetime) -> BooleanExpression:
    return And(
        EqualTo("symbol", rules.SYMBOLS[SYMBOL].symbol),  # type: ignore[call-arg, arg-type]
        And(
            GreaterThanOrEqual("event_time", low),  # type: ignore[call-arg, arg-type]
            LessThan("event_time", high),  # type: ignore[call-arg, arg-type]
        ),
    )


def _in_scan(
    view: PinnedCatalogView, start: datetime, end: datetime, *, touching: bool
) -> tuple[list[Mapping[str, Any]], int]:
    """The pre-G3-P read, verbatim: every revision of the window's keys by one ``In`` scan."""
    keys = view.scan_columns(TRADES, columns=("observation_key",), row_filter=_window(start, end))
    wanted = set(keys.column("observation_key").to_pylist())
    if not wanted:
        return [], 0

    def read(low: datetime, high: datetime) -> list[Mapping[str, Any]]:
        found: list[Mapping[str, Any]] = view.scan_columns(
            TRADES,
            columns=COLUMNS,
            row_filter=And(In("observation_key", wanted), _window(low, high)),  # type: ignore[call-arg, arg-type]
        ).to_pylist()
        return found

    rows = selector_module._key_closure(read, "event_time", start, end, touching=touching)
    return rows, len(wanted)


def _by_revision(rows: Sequence[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    return sorted(rows, key=lambda row: row["revision_id"])


def _many_keys(h: RestHarness) -> None:
    """300 archive trades a minute apart from 12:14 (trade 100 first) in eight data files, and a
    REST copy of trade 100 at 22:14: one key's revisions ten hours apart (one chain)."""
    items = ss.agg_items(300, first_ms=ss.T0 - 600 * ss.MINUTE_MS, ms_step=ss.MINUTE_MS)
    archive = c.ingest_archive(h, "agg_trades", ss.archive_agg_lines(items), knowledge=K_A)
    [response] = c.ingest_rest(h, "agg_trades", ss.agg_items(1), knowledge=K_R)
    c.normalizer(h, clock=StepClock(start=N_A), microbatch_rows=40).normalize_unit(
        c.ARCHIVE_AGGS.table, archive
    )
    c.normalizer(h, clock=StepClock(start=N_R)).normalize_unit(c.REST_AGGS.table, response)


WINDOWS = [
    (utc(2023, 11, 14), utc(2023, 11, 15)),  # all 300 keys
    (utc(2023, 11, 14, 12), utc(2023, 11, 14, 17, 30)),  # 300 keys, the REST copy in the margin
    (utc(2023, 11, 14, 12), utc(2023, 11, 14, 13)),  # owns trade 100, fetches 22:00 too
    (utc(2023, 11, 14, 12, 30), utc(2023, 11, 14, 14, 7)),  # off the hour grid
    (utc(2023, 11, 14, 22), utc(2023, 11, 14, 23)),  # only the REST copy: owned elsewhere
    (utc(2023, 11, 15, 3), utc(2023, 11, 15, 4)),  # empty
]


@pytest.mark.parametrize("fetch_rows", [None, 1])
def test_the_key_closure_equals_the_single_in_scan(
    h: RestHarness, monkeypatch: pytest.MonkeyPatch, fetch_rows: int | None
) -> None:
    _many_keys(h)
    if fetch_rows is not None:  # every hour fetched on its own
        monkeypatch.setattr(selector_module, "_FETCH_ROWS", fetch_rows)
    spec = _spec(h, cutoff=FAR)
    view = PinnedCatalogView(h.adapter, spec.snapshot_bindings)
    selector = PitSelector(
        h.adapter, h.storage, canonical_scratch_directory=h.canonical_scratch_directory
    )
    symbol = rules.SYMBOLS[SYMBOL].symbol
    sizes = []
    for start, end in WINDOWS:
        for touching in (False, True):
            expected, keys = _in_scan(view, start, end, touching=touching)
            got = selector._canonical_rows(view, TRADES, "agg_trades", symbol, start, end, touching)
            assert _by_revision(got) == _by_revision(expected), (start, end, touching)
            assert len({row["revision_id"] for row in got}) == len(got)
            sizes.append(keys)
    assert max(sizes) == 300 > 200  # the In set PyIceberg no longer prunes by
    # The REST copy in the margin is read with its key: 301 revisions for 12:00-17:30.
    rows = selector._canonical_rows(view, TRADES, "agg_trades", symbol, *WINDOWS[1], False)
    assert len(rows) == 301


def test_a_selection_does_not_depend_on_how_the_read_is_fetched(
    h: RestHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    _many_keys(h)
    spec = _spec(h, cutoff=FAR)
    whole = PitSelector(
        h.adapter, h.storage, canonical_scratch_directory=h.canonical_scratch_directory
    ).select(spec, "agg_trades", SYMBOL, *WINDOWS[1])
    monkeypatch.setattr(selector_module, "_FETCH_ROWS", 1)
    hourly = PitSelector(
        h.adapter, h.storage, canonical_scratch_directory=h.canonical_scratch_directory
    ).select(spec, "agg_trades", SYMBOL, *WINDOWS[1])
    assert hourly == whole
    assert len(whole.records) == 300 and len(whole.conflicts) == 1  # trade 100's two copies


# ------------------------------------------------------------------ _wanted_rows, in memory

_TYPES = {
    "observation_key": pa.string(),
    "event_time": pa.timestamp("us", tz="UTC"),
    "revision_id": pa.string(),
}
BASE = utc(2023, 11, 13)


def _rows() -> list[dict[str, Any]]:
    """Three days of revisions, uneven in time; keys k0..k39, some with several revisions."""
    rows = []
    for i in range(400):
        at = BASE + timedelta(minutes=(i * 37) % (3 * 24 * 60), microseconds=i)
        rows.append({"observation_key": f"k{i % 40}", "event_time": at, "revision_id": f"r{i}"})
    return rows


@pytest.mark.parametrize("fetch_rows", [100_000, 7, 1])
@pytest.mark.parametrize(
    ("low", "high"),
    [
        (BASE, BASE + timedelta(days=3)),
        (BASE + timedelta(hours=5, minutes=13), BASE + timedelta(days=1, hours=2, seconds=1)),
        (BASE + timedelta(hours=23, minutes=59), BASE + timedelta(days=1, minutes=1)),
        (BASE - timedelta(days=1), BASE),
    ],
)
def test_wanted_rows_are_exactly_the_member_rows_of_the_range(
    monkeypatch: pytest.MonkeyPatch, low: datetime, high: datetime, fetch_rows: int
) -> None:
    monkeypatch.setattr(selector_module, "_FETCH_ROWS", fetch_rows)
    rows = _rows()
    members = {f"k{i}" for i in range(0, 40, 3)}
    fetches: list[tuple[datetime, datetime, int]] = []

    def scan(names: Sequence[str], a: datetime, b: datetime) -> pa.Table:
        assert low <= a < b <= high  # every piece lies in the range
        found = [{name: row[name] for name in names} for row in rows if a <= row["event_time"] < b]
        table = pa.Table.from_pylist(found, schema=pa.schema([(n, _TYPES[n]) for n in names]))
        if len(names) == len(_TYPES):
            fetches.append((a, b, table.num_rows))
        return table

    got = selector_module._wanted_rows(
        scan,
        pa.array(sorted(members), pa.string()),
        tuple(_TYPES),
        "event_time",
        low,
        high,
    )
    expected = [
        row for row in rows if low <= row["event_time"] < high and row["observation_key"] in members
    ]
    assert sorted(item["revision_id"] for item in got) == sorted(r["revision_id"] for r in expected)
    assert len(got) == len(expected)  # each row once
    # Fetches are disjoint and each holds at most the budget, unless it is a single hour.
    for (_, first_end, _), (second_start, _, _) in zip(fetches, fetches[1:], strict=False):
        assert first_end <= second_start
    for a, b, count in fetches:
        assert count <= fetch_rows or b - a <= HOUR

"""``python -m infrastructure.tools.rest_tail_cli`` (ADR-0101 §6): collect → ingest → normalize →
reconcile over a strict in-memory venue and a SQLite catalog (no network, no DSN connected)."""

from __future__ import annotations

import ast
import json
from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

from infrastructure.catalog.phase1_tables import (
    BINANCE_SPOT_PRECEDENCE_EVIDENCE,
    BINANCE_SPOT_REST_AGG_TRADES,
    CANONICAL_TRADES,
)
from infrastructure.revision.channel_reconcile import ChannelReconciler
from infrastructure.settings import Settings
from infrastructure.tools import cli_support, rest_tail_cli
from tests.infrastructure.collector import rest_support as cs
from tests.infrastructure.revision import rest_store_support as ss
from tests.infrastructure.revision.rest_store_support import DAY, SYMBOL, T0, RestHarness, StepClock
from tests.infrastructure.tools.upstream_cli_support import (
    assert_no_secret,
    patch_catalog,
    refuse_catalog,
    settings_over,
    unreachable_catalog,
)

REPO = Path(__file__).resolve().parents[3]
START = cs.at_ms(T0)
END = cs.at_ms(T0 + 5 * cs.MINUTE_MS)
REQUEST = [
    "--request-id",
    "rest-tail-1",
    "--data-type",
    "agg_trades",
    "--symbol",
    SYMBOL,
    "--start",
    START.isoformat(),
    "--end",
    END.isoformat(),
]


@pytest.fixture
def h(tmp_path: Path) -> Iterator[RestHarness]:
    with ss.sqlite_harness(tmp_path / "h") as opened:
        yield opened


@pytest.fixture
def settings(h: RestHarness, tmp_path: Path) -> Settings:
    return settings_over(h.storage, tmp_path / "scratch", ss.ORIGIN)


def _run(
    capsys: pytest.CaptureFixture[str], h: RestHarness, settings: Settings, *argv: str
) -> tuple[int, dict[str, Any] | None, str]:
    code = rest_tail_cli.main(list(argv), settings=settings, http_transport=h.venue.transport())
    captured = capsys.readouterr()
    return code, (json.loads(captured.out) if captured.out else None), captured.err


def _items() -> list[dict[str, Any]]:
    return ss.agg_items(3, first_ms=T0)


def test_without_a_subcommand_the_plan_is_printed_and_nothing_is_opened(
    h: RestHarness,
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    refuse_catalog(monkeypatch)
    code, plan, err = _run(capsys, h, settings, *REQUEST)
    assert code == 0, err
    assert plan is not None and plan["command"] == "plan"
    assert plan["symbols"] == [SYMBOL] and plan["data_type"] == "agg_trades"
    assert plan["start"] == START.isoformat() and plan["end"] == END.isoformat()
    assert plan["element_table"] == BINANCE_SPOT_REST_AGG_TRADES.table
    assert plan["partitions"] == [{"symbol": SYMBOL, "day": DAY.isoformat()}]
    assert [step["step"] for step in plan["steps"] if step["network"]] == ["collect"]
    assert h.venue.requests == []


def test_the_window_partitions_cover_every_touched_utc_day() -> None:
    start = cs.at_ms(T0)
    assert rest_tail_cli._days(start, start + timedelta(days=1)) == [
        DAY,
        DAY + timedelta(days=1),
    ]
    midnight = start.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
    assert rest_tail_cli._days(start, midnight) == [DAY]


def test_collect_ingest_normalize_reconcile_then_every_step_replays(
    h: RestHarness,
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    items = _items()
    cs.queue_agg_chain(h.venue, SYMBOL, T0, [items])
    # The archive channel of the same trades, so the reconcile step has an equal pair.
    h.ingest_archive(
        "agg_trades",
        ss.archive_agg_lines(items),
        clock=StepClock(start=ss.utc(2023, 12, 1)),
        retrieved_at=ss.utc(2023, 11, 16),
    )
    refuse_catalog(monkeypatch)  # collecting needs the warehouse only
    code, collected, err = _run(capsys, h, settings, "collect", *REQUEST)
    assert code == 0, err
    assert collected is not None and collected["objects"] >= 1
    requests = len(h.venue.requests)
    code, again, err = _run(capsys, h, settings, "collect", *REQUEST)
    assert code == 0 and again == collected, err
    assert len(h.venue.requests) == requests  # a committed attempt replays offline

    opened = patch_catalog(monkeypatch, h.adapter)
    code, ingested, err = _run(capsys, h, settings, "ingest", *REQUEST)
    assert code == 0, err
    assert ingested is not None and ingested["pages"] >= 1 and not ingested["replayed"]
    assert ingested["collection_outcome"] == "succeeded"

    code, normalized, err = _run(capsys, h, settings, "normalize", *REQUEST)
    assert code == 0, err
    assert normalized is not None and normalized["ingest_replayed"]
    assert normalized["canonical_table"] == CANONICAL_TRADES.table
    assert sum(unit["revision_count"] for unit in normalized["units"]) == len(items)
    assert len(h.rows(CANONICAL_TRADES)) == len(items)

    code, reconciled, err = _run(capsys, h, settings, "reconcile", *REQUEST)
    assert code == 0, err
    assert reconciled is not None
    (partition,) = reconciled["partitions"]
    assert partition["symbol"] == SYMBOL and partition["day"] == DAY.isoformat()
    assert partition["edges"] == len(items) and partition["new_edges"] == len(items)
    assert len(h.rows(BINANCE_SPOT_PRECEDENCE_EVIDENCE)) == len(items)

    replays: dict[str, dict[str, Any]] = {}
    for step in ("ingest", "normalize", "reconcile"):
        code, replayed, err = _run(capsys, h, settings, step, *REQUEST)
        assert code == 0 and replayed is not None, err
        replays[step] = replayed
    assert replays["ingest"]["replayed"] and replays["ingest"]["new_snapshot_ids"] == []
    assert all(
        unit["replayed_batch_count"] == unit["batch_count"]
        for unit in replays["normalize"]["units"]
    )
    assert replays["reconcile"]["partitions"][0]["new_edges"] == 0
    assert len(h.rows(CANONICAL_TRADES)) == len(items)
    assert len(h.venue.requests) == requests and len(opened) == 6


def test_the_same_request_id_with_other_content_fails_closed(
    h: RestHarness,
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    cs.queue_agg_chain(h.venue, SYMBOL, T0, [_items()])
    refuse_catalog(monkeypatch)
    assert _run(capsys, h, settings, "collect", *REQUEST)[0] == 0
    other = list(REQUEST)
    other[other.index("--end") + 1] = cs.at_ms(T0 + 4 * cs.MINUTE_MS).isoformat()
    requests = len(h.venue.requests)
    code, out, err = _run(capsys, h, settings, "collect", *other)
    assert code == cli_support.EXIT_FAILED and out is None
    assert err.startswith("error: collect failed (")
    assert len(h.venue.requests) == requests


def test_an_unknown_failure_prints_its_type_only(
    h: RestHarness,
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    patch_catalog(monkeypatch, h.adapter)

    def explode(self: ChannelReconciler, *args: Any) -> Any:
        raise RuntimeError(f"boom at {settings.catalog_uri.get_secret_value()}")

    monkeypatch.setattr(ChannelReconciler, "reconcile", explode)
    code, out, err = _run(capsys, h, settings, "reconcile", *REQUEST)
    assert code == cli_support.EXIT_FAILED and out is None
    assert err.strip() == "error: reconcile failed (RuntimeError)"


def test_an_unreachable_catalog_exits_4_with_the_dsn_redacted(
    h: RestHarness,
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    unreachable_catalog(monkeypatch)
    code, out, err = _run(capsys, h, settings, "ingest", *REQUEST)
    assert code == cli_support.EXIT_ENVIRONMENT and out is None
    assert "<redacted>" in err
    assert_no_secret(err)


def test_an_inverted_window_is_an_invalid_request(
    h: RestHarness, settings: Settings, capsys: pytest.CaptureFixture[str]
) -> None:
    argv = list(REQUEST)
    argv[argv.index("--start") + 1], argv[argv.index("--end") + 1] = (
        END.isoformat(),
        START.isoformat(),
    )
    code, out, err = _run(capsys, h, settings, "plan", *argv)
    assert code == cli_support.EXIT_USAGE and out is None
    assert err.startswith("error: invalid request")


@pytest.mark.parametrize(
    ("flag", "value"),
    [
        ("--symbol", "DOGEUSDT"),
        ("--data-type", "trades_raw"),
        ("--start", "2023-11-14T22:14:00"),
        ("--request-id", "bad id"),
    ],
)
def test_bad_arguments_exit_2(flag: str, value: str) -> None:
    argv = ["collect", *REQUEST]
    argv[argv.index(flag) + 1] = value
    with pytest.raises(SystemExit) as exit_:
        rest_tail_cli.main(argv)
    assert exit_.value.code == cli_support.EXIT_USAGE


def test_only_the_collect_step_builds_a_network_client() -> None:
    tree = ast.parse((REPO / "infrastructure" / "tools" / "rest_tail_cli.py").read_text("utf-8"))
    owners = [
        function.name
        for function in ast.walk(tree)
        if isinstance(function, ast.FunctionDef)
        for node in ast.walk(function)
        if isinstance(node, ast.Attribute) and node.attr == "from_settings"
        if isinstance(node.value, ast.Name) and node.value.id.endswith("Collector")
    ]
    assert owners == ["_collect"]

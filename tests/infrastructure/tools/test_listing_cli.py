"""``python -m infrastructure.tools.listing_cli`` (ADR-0101 §6): plan, collect, ingest, derive."""

from __future__ import annotations

import ast
import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from infrastructure.canonical.listings import ListingDeriver
from infrastructure.collector.binance_exchange_info import checkpoint_key
from infrastructure.settings import Settings
from infrastructure.tools import cli_support, listing_cli
from tests.infrastructure.collector.rest_support import Answer
from tests.infrastructure.revision import exchange_info_support as ex
from tests.infrastructure.tools.upstream_cli_support import (
    assert_no_secret,
    patch_catalog,
    refuse_catalog,
    settings_over,
    unreachable_catalog,
)

REPO = Path(__file__).resolve().parents[3]


@pytest.fixture
def h(tmp_path: Path) -> Iterator[ex.Harness]:
    with ex.harness(tmp_path / "h") as opened:
        yield opened


@pytest.fixture
def settings(h: ex.Harness, tmp_path: Path) -> Settings:
    return settings_over(h.storage, tmp_path / "scratch", ex.ORIGIN)


def _run(
    capsys: pytest.CaptureFixture[str], h: ex.Harness, settings: Settings, *argv: str
) -> tuple[int, dict[str, Any] | None, str]:
    code = listing_cli.main(list(argv), settings=settings, http_transport=h.venue.transport())
    captured = capsys.readouterr()
    return code, (json.loads(captured.out) if captured.out else None), captured.err


def test_without_a_subcommand_the_plan_is_printed_and_nothing_is_opened(
    h: ex.Harness,
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    refuse_catalog(monkeypatch)
    code, plan, err = _run(capsys, h, settings, "--request-id", "listing-1")
    assert code == cli_support.EXIT_OK, err
    assert plan is not None and plan["command"] == "plan"
    assert plan["origin"] == ex.ORIGIN and plan["endpoint"] == "/api/v3/exchangeInfo"
    assert plan["symbols"] == ["BTCUSDT", "ETHUSDT"]
    assert plan["checkpoint_key"] == checkpoint_key("listing-1")
    assert [step["step"] for step in plan["steps"] if step["network"]] == ["collect"]
    assert h.venue.requests == []
    assert h.storage.lookup(checkpoint_key("listing-1")) is None


def test_collect_ingest_derive_then_every_step_replays(
    h: ex.Harness,
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    h.serve(ex.body(ex.TRADING))
    refuse_catalog(monkeypatch)  # collecting needs the warehouse only
    code, collected, err = _run(capsys, h, settings, "collect", "--request-id", "listing-1")
    assert code == 0, err
    assert collected is not None and len(h.venue.requests) == 1
    assert h.storage.lookup(checkpoint_key("listing-1")) is not None
    assert {item["symbol"] for item in collected["symbols"]} == {"BTCUSDT", "ETHUSDT"}
    assert collected["missing_symbols"] == []
    # A committed attempt replays without any request (the venue has nothing queued).
    code, again, err = _run(capsys, h, settings, "collect", "--request-id", "listing-1")
    assert code == 0, err
    assert again == collected and len(h.venue.requests) == 1

    opened = patch_catalog(monkeypatch, h.adapter)
    code, ingested, err = _run(capsys, h, settings, "ingest", "--request-id", "listing-1")
    assert code == 0, err
    assert ingested is not None and not ingested["replayed"] and ingested["first_delivery"]
    assert ingested["snapshot_id"] == h.head(ex.EXCHANGE_INFO.table)
    code, derived, err = _run(capsys, h, settings, "derive")
    assert code == 0, err
    assert derived is not None and derived["new_revision_ids"] and not derived["replayed"]
    assert derived["listing_snapshot_id"] == h.head(ex.LISTINGS.table)
    assert len(h.rows(ex.LISTINGS.table)) == len(derived["new_revision_ids"])

    code, ingested_again, err = _run(capsys, h, settings, "ingest", "--request-id", "listing-1")
    assert code == 0 and ingested_again is not None and ingested_again["replayed"], err
    code, derived_again, err = _run(capsys, h, settings, "derive")
    assert code == 0 and derived_again is not None and derived_again["replayed"], err
    assert derived_again["new_revision_ids"] == []
    assert len(h.venue.requests) == 1 and len(opened) == 4


def test_ingest_of_an_attempt_never_collected_fails_without_any_request(
    h: ex.Harness,
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    patch_catalog(monkeypatch, h.adapter)
    code, out, err = _run(capsys, h, settings, "ingest", "--request-id", "never-collected")
    assert code == cli_support.EXIT_FAILED and out is None
    assert err.startswith("error: ingest failed (") and h.venue.requests == []
    assert h.head(ex.EXCHANGE_INFO.table) is None


def test_a_failed_collection_commits_nothing(
    h: ex.Harness,
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    h.venue.answers.setdefault(ex.url_key(), []).append(Answer(status=503))
    refuse_catalog(monkeypatch)
    code, out, err = _run(capsys, h, settings, "collect", "--request-id", "listing-1")
    assert code == cli_support.EXIT_FAILED and out is None
    assert err.startswith("error: collect failed (CollectionFailed")
    assert_no_secret(err)
    h.serve(ex.body(ex.TRADING))  # a later, successful attempt of the same id
    code, collected, err = _run(capsys, h, settings, "collect", "--request-id", "listing-1")
    assert code == 0 and collected is not None, err


def test_an_unknown_failure_prints_its_type_only(
    h: ex.Harness,
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    patch_catalog(monkeypatch, h.adapter)

    def explode(self: ListingDeriver) -> Any:
        raise RuntimeError(f"boom at {settings.catalog_uri.get_secret_value()}")

    monkeypatch.setattr(ListingDeriver, "derive", explode)
    code, out, err = _run(capsys, h, settings, "derive")
    assert code == cli_support.EXIT_FAILED and out is None
    assert err.strip() == "error: derive failed (RuntimeError)"


def test_an_unreachable_catalog_exits_4_with_the_dsn_redacted(
    h: ex.Harness,
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    unreachable_catalog(monkeypatch)
    code, out, err = _run(capsys, h, settings, "derive")
    assert code == cli_support.EXIT_ENVIRONMENT and out is None
    assert "<redacted>" in err
    assert_no_secret(err)


def test_invalid_settings_name_fields_only(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(
        cli_support,
        "load_settings",
        lambda: Settings(_env_file=None),  # type: ignore[call-arg]
    )
    monkeypatch.delenv("HLENS_CATALOG_URI", raising=False)
    code = listing_cli.main(["derive"])
    err = capsys.readouterr().err
    assert code == cli_support.EXIT_ENVIRONMENT
    assert err.strip() == "error: settings are invalid: catalog_uri"


@pytest.mark.parametrize(
    "argv",
    [["collect"], ["ingest"], ["collect", "--request-id", "bad id"], ["bogus"], ["derive", "-x"]],
)
def test_usage_errors_exit_2(argv: list[str]) -> None:
    with pytest.raises(SystemExit) as exit_:
        listing_cli.main(argv)
    assert exit_.value.code == cli_support.EXIT_USAGE


def test_only_the_collect_step_builds_a_network_client() -> None:
    """Static guard: the collector is constructed in ``_collect`` and nowhere else."""
    tree = ast.parse((REPO / "infrastructure" / "tools" / "listing_cli.py").read_text("utf-8"))
    owners = [
        function.name
        for function in ast.walk(tree)
        if isinstance(function, ast.FunctionDef)
        for node in ast.walk(function)
        if isinstance(node, ast.Attribute) and node.attr == "from_settings"
        if isinstance(node.value, ast.Name) and node.value.id.endswith("Collector")
    ]
    assert owners == ["_collect"]

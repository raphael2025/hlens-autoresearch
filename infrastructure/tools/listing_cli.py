"""Operator entry of the exchangeInfo → listing history stage (ADR-0101 §6).

``python -m infrastructure.tools.listing_cli [plan|collect|ingest|derive]``:

- ``plan`` (the default when no subcommand is named): print what the stage would do for
  ``--request-id`` (origin, endpoint, symbols, checkpoint key, steps). No network, no catalog;
- ``collect``: the **only** step that reaches the network: one
  ``BinanceSpotExchangeInfoCollector.collect`` of the first-slice ``GET /api/v3/exchangeInfo``
  (a committed ``--request-id`` replays without any request);
- ``ingest``: ``ExchangeInfoSnapshotStore.ingest_snapshot`` of the committed attempt (replays its
  checkpoint, never fetches) into ``binance_spot.exchange_info``;
- ``derive``: ``ListingDeriver.derive`` of every missing ``canonical.instrument_listings``
  revision (idempotent; no network).

Every step is the existing component unchanged; this module only composes it from ``Settings``
(``HLENS_*``) and prints a fixed-size JSON summary. Errors never print settings or DSNs.

Exit codes: 0 ok; 1 the step failed; 2 usage; 4 settings or data plane unavailable.
"""

from __future__ import annotations

import argparse
import re
import sys
from collections import Counter
from collections.abc import Sequence
from contextlib import ExitStack
from typing import Any, Final

import httpx
from pydantic import ValidationError

from core.contracts.catalog import CatalogError
from core.contracts.collector import REQUEST_ID_PATTERN, CollectionFailed, UnsupportedRequest
from infrastructure.canonical.listings import ListingDeriveError, ListingDeriver
from infrastructure.collector.binance_exchange_info import (
    EXCHANGE_INFO_SOURCE,
    BinanceSpotExchangeInfoCollector,
    ExchangeInfoRequest,
    checkpoint_key,
)
from infrastructure.dataset.factory import market_data_origin
from infrastructure.revision.exchange_info_identity import (
    EXCHANGE_INFO_PATH,
    EXCHANGE_INFO_SYMBOLS,
)
from infrastructure.revision.exchange_info_store import (
    ExchangeInfoSnapshotStore,
    ExchangeInfoStoreError,
)
from infrastructure.settings import Settings
from infrastructure.storage import LocalFileStorageAdapter
from infrastructure.tools import cli_support as support

__all__ = ["COMMANDS", "main"]

COMMANDS: Final = ("plan", "collect", "ingest", "derive")
_REQUEST_ID: Final = re.compile(REQUEST_ID_PATTERN)
#: Step failures whose message is printed (redacted); anything else prints its type only.
_KNOWN_FAILURES: Final = (
    CollectionFailed,
    UnsupportedRequest,
    ExchangeInfoStoreError,
    ListingDeriveError,
    CatalogError,
)


def _request_id(text: str) -> str:
    if _REQUEST_ID.match(text) is None:
        raise argparse.ArgumentTypeError(f"not a request id: {text!r}")
    return text


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m infrastructure.tools.listing_cli",
        description="exchangeInfo collect → ingest → listing derive (ADR-0101 §6). "
        "Default: print the plan; only 'collect' reaches the network.",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    attempt = argparse.ArgumentParser(add_help=False)
    attempt.add_argument(
        "--request-id",
        required=True,
        type=_request_id,
        help="the logical snapshot attempt (a committed id replays)",
    )
    commands.add_parser("plan", parents=[attempt], help="print the plan (default); no I/O")
    commands.add_parser("collect", parents=[attempt], help="fetch one snapshot (network)")
    commands.add_parser("ingest", parents=[attempt], help="persist the committed snapshot")
    commands.add_parser("derive", help="derive missing listing revisions")
    return parser


def _plan(args: argparse.Namespace, settings: Settings) -> dict[str, Any]:
    return {
        "command": "plan",
        "request_id": args.request_id,
        "source": f"{EXCHANGE_INFO_SOURCE.source_id}@{EXCHANGE_INFO_SOURCE.version}",
        "origin": market_data_origin(settings),
        "endpoint": EXCHANGE_INFO_PATH,
        "symbols": list(EXCHANGE_INFO_SYMBOLS),
        "checkpoint_key": checkpoint_key(args.request_id),
        "steps": [
            {"step": "collect", "network": True},
            {"step": "ingest", "network": False},
            {"step": "derive", "network": False},
        ],
    }


def _collect(
    args: argparse.Namespace,
    settings: Settings,
    storage: LocalFileStorageAdapter,
    transport: httpx.BaseTransport | None,
) -> dict[str, Any]:
    with BinanceSpotExchangeInfoCollector.from_settings(
        settings, storage, http_transport=transport
    ) as collector:
        snapshot = collector.collect(ExchangeInfoRequest(args.request_id))
    return {
        "command": "collect",
        "request_id": args.request_id,
        "checkpoint_key": snapshot.checkpoint_key,
        "body_sha256": snapshot.body.sha256,
        "requested_at": snapshot.requested_at.isoformat(),
        "retrieved_at": snapshot.retrieved_at.isoformat(),
        "symbols": [
            {"symbol": item.symbol, "status": item.status} for item in snapshot.decoded.symbols
        ],
        "missing_symbols": list(snapshot.decoded.missing_symbols),
    }


def _ingest(args: argparse.Namespace, adapter: Any, storage: Any, origin: str) -> dict[str, Any]:
    with ExchangeInfoSnapshotStore(adapter, storage, market_data_base_url=origin) as store:
        stored = store.ingest_snapshot(ExchangeInfoRequest(args.request_id))
    return {
        "command": "ingest",
        "request_id": stored.request_id,
        "revision_id": stored.revision_id,
        "arrival_seq": stored.arrival_seq,
        "knowledge_time": stored.knowledge_time.isoformat(),
        "first_delivery": stored.first_delivery,
        "missing_symbols": list(stored.missing_symbols),
        "snapshot_id": stored.commit.snapshot_id,
        "replayed": stored.replayed,
    }


def _derive(adapter: Any, storage: Any, origin: str) -> dict[str, Any]:
    deriver = ListingDeriver(adapter, storage, market_data_base_url=origin)
    try:
        derived = deriver.derive()
    finally:
        deriver.close()
    return {
        "command": "derive",
        "raw_snapshot_id": derived.raw_snapshot_id,
        "listing_snapshot_id": derived.listing_snapshot_id,
        "new_revision_ids": list(derived.new_revision_ids),
        "replayed": derived.replayed,
        "diverged": list(derived.diverged),
        "findings": dict(sorted(Counter(item.code for item in derived.findings).items())),
    }


def _run(
    args: argparse.Namespace, settings: Settings, transport: httpx.BaseTransport | None
) -> int:
    if args.command == "plan":
        support.emit(_plan(args, settings))
        return support.EXIT_OK
    origin = market_data_origin(settings)
    secrets = support.secrets_of(settings)
    with ExitStack() as stack:
        try:
            if args.command == "collect":
                storage = stack.enter_context(LocalFileStorageAdapter.from_settings(settings))
            else:
                adapter, storage = stack.enter_context(support.open_data_plane(settings))
        except (CatalogError, OSError, ValueError) as exc:
            message = support.redact(f"{type(exc).__name__}: {exc}", secrets)
            print(f"error: the data plane did not open ({message})", file=sys.stderr)
            return support.EXIT_ENVIRONMENT
        try:
            if args.command == "collect":
                summary = _collect(args, settings, storage, transport)
            elif args.command == "ingest":
                summary = _ingest(args, adapter, storage, origin)
            else:
                summary = _derive(adapter, storage, origin)
        except _KNOWN_FAILURES as exc:
            message = support.redact(f"{type(exc).__name__}: {exc}", secrets)
            print(f"error: {args.command} failed ({message})", file=sys.stderr)
            return support.EXIT_FAILED
        except Exception as exc:  # never echo an unknown failure's text: it may carry a credential
            print(f"error: {args.command} failed ({type(exc).__name__})", file=sys.stderr)
            return support.EXIT_FAILED
    support.emit(summary)
    return support.EXIT_OK


def main(
    argv: Sequence[str] | None = None,
    *,
    settings: Settings | None = None,
    http_transport: httpx.BaseTransport | None = None,
) -> int:
    """Run one step; ``settings`` / ``http_transport`` are test seams (default: environment)."""
    args = _parser().parse_args(support.default_to_plan(argv, COMMANDS))
    try:
        resolved = support.load_settings() if settings is None else settings
    except ValidationError as exc:
        print(f"error: {support.settings_error(exc)}", file=sys.stderr)
        return support.EXIT_ENVIRONMENT
    return _run(args, resolved, http_transport)


if __name__ == "__main__":
    raise SystemExit(main())

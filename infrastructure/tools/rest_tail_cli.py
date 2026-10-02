"""Operator entry of the REST tail stage (ADR-0101 §6).

``python -m infrastructure.tools.rest_tail_cli [plan|collect|ingest|normalize|reconcile]`` over one
logical REST attempt (``--request-id``, ``--data-type``, ``--symbol`` ..., ``--start``, ``--end``):

- ``plan`` (the default when no subcommand is named): print the request, its partitions and the
  steps. No network, no catalog;
- ``collect``: the **only** step that reaches the network: ``BinanceSpotRestCollector.collect``
  of the window (a committed ``--request-id`` replays without any request; the same id with
  other request content fails closed);
- ``ingest``: ``RestRevisionStore.ingest_collection`` of the committed attempt (replays its
  checkpoints, never fetches) into the REST response / element tables;
- ``normalize``: ``CanonicalNormalizer.normalize_unit`` of every page's response revision. The
  pages are read back through the same idempotent ingest (a replay when ``ingest`` already ran);
- ``reconcile``: ``ChannelReconciler.reconcile`` of every ``(symbol, UTC day)`` partition the
  window touches (archive / REST evidence-only edges; idempotent).

Every step is the existing component unchanged, composed from ``Settings`` (``HLENS_*``); a
fixed-size JSON summary is printed. Errors never print settings or DSNs.

Exit codes: 0 ok; 1 the step failed; 2 usage or an invalid request; 4 settings or data plane
unavailable.
"""

from __future__ import annotations

import argparse
import re
import sys
from collections import Counter
from collections.abc import Sequence
from contextlib import ExitStack
from datetime import UTC, date, datetime, time, timedelta
from typing import Any, Final

import httpx
from pydantic import ValidationError

from core.contracts.catalog import CatalogError
from core.contracts.collector import (
    REQUEST_ID_PATTERN,
    CollectionFailed,
    CollectionRequest,
    UnsupportedRequest,
)
from infrastructure.canonical import rules
from infrastructure.canonical.normalizer import CanonicalNormalizeError, CanonicalNormalizer
from infrastructure.catalog.definitions import RegisteredTableDefinition
from infrastructure.catalog.phase1_tables import (
    BINANCE_SPOT_REST_AGG_TRADES,
    BINANCE_SPOT_REST_KLINES_1M,
)
from infrastructure.collector.binance_rest import REST_SOURCE, BinanceSpotRestCollector
from infrastructure.dataset.factory import market_data_origin
from infrastructure.revision.channel_reconcile import ChannelReconcileError, ChannelReconciler
from infrastructure.revision.rest_store import (
    RestCollectionStored,
    RestRevisionStore,
    RestRevisionStoreError,
)
from infrastructure.settings import Settings
from infrastructure.storage import LocalFileStorageAdapter
from infrastructure.tools import cli_support as support

__all__ = ["COMMANDS", "REST_ELEMENT_TABLES", "main"]

COMMANDS: Final = ("plan", "collect", "ingest", "normalize", "reconcile")
#: The REST element table (normalizer unit table) of each data type.
REST_ELEMENT_TABLES: Final[dict[str, RegisteredTableDefinition]] = {
    "agg_trades": BINANCE_SPOT_REST_AGG_TRADES,
    "klines_1m": BINANCE_SPOT_REST_KLINES_1M,
}
_REQUEST_ID: Final = re.compile(REQUEST_ID_PATTERN)
_DAY: Final = timedelta(days=1)
#: Step failures whose message is printed (redacted); anything else prints its type only.
_KNOWN_FAILURES: Final = (
    CollectionFailed,
    UnsupportedRequest,
    RestRevisionStoreError,
    CanonicalNormalizeError,
    ChannelReconcileError,
    CatalogError,
)


def _request_id(text: str) -> str:
    if _REQUEST_ID.match(text) is None:
        raise argparse.ArgumentTypeError(f"not a request id: {text!r}")
    return text


def _symbol(text: str) -> str:
    if text not in rules.SYMBOLS:
        raise argparse.ArgumentTypeError(
            f"{text!r} is not a first-slice symbol ({', '.join(sorted(rules.SYMBOLS))})"
        )
    return text


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m infrastructure.tools.rest_tail_cli",
        description="REST tail collect → ingest → normalize → reconcile (ADR-0101 §6). "
        "Default: print the plan; only 'collect' reaches the network.",
    )
    request = argparse.ArgumentParser(add_help=False)
    request.add_argument("--request-id", required=True, type=_request_id)
    request.add_argument("--data-type", required=True, choices=sorted(REST_ELEMENT_TABLES))
    request.add_argument(
        "--symbol", required=True, action="append", type=_symbol, help="repeat for each symbol"
    )
    request.add_argument("--start", required=True, type=support.instant)
    request.add_argument("--end", required=True, type=support.instant)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("plan", parents=[request], help="print the plan (default); no I/O")
    commands.add_parser("collect", parents=[request], help="fetch the window (network)")
    commands.add_parser("ingest", parents=[request], help="persist the committed attempt")
    commands.add_parser("normalize", parents=[request], help="normalize the ingested pages")
    commands.add_parser("reconcile", parents=[request], help="reconcile archive / REST edges")
    return parser


def _collection_request(args: argparse.Namespace) -> CollectionRequest:
    """The one logical attempt every step names; refused before any I/O when malformed."""
    return CollectionRequest(
        request_id=args.request_id,
        source=REST_SOURCE,
        data_type=args.data_type,
        symbols=tuple(args.symbol),
        coverage_start=args.start,
        coverage_end=args.end,
    )


def _days(start: datetime, end: datetime) -> list[date]:
    """Every UTC day the half-open window ``[start, end)`` touches."""
    day = start.astimezone(UTC).date()
    days: list[date] = []
    while datetime.combine(day, time(), tzinfo=UTC) < end:
        days.append(day)
        day += _DAY
    return days


def _request_document(request: CollectionRequest) -> dict[str, Any]:
    return {
        "request_id": request.request_id,
        "source": f"{request.source.source_id}@{request.source.version}",
        "data_type": request.data_type,
        "symbols": list(request.symbols),
        "start": request.coverage_start.isoformat(),
        "end": request.coverage_end.isoformat(),
    }


def _plan(request: CollectionRequest, settings: Settings) -> dict[str, Any]:
    days = _days(request.coverage_start, request.coverage_end)
    return {
        "command": "plan",
        **_request_document(request),
        "origin": market_data_origin(settings),
        "element_table": REST_ELEMENT_TABLES[request.data_type].table,
        "partitions": [
            {"symbol": symbol, "day": day.isoformat()} for symbol in request.symbols for day in days
        ],
        "steps": [
            {"step": "collect", "network": True},
            {"step": "ingest", "network": False},
            {"step": "normalize", "network": False},
            {"step": "reconcile", "network": False},
        ],
    }


def _collect(
    request: CollectionRequest,
    settings: Settings,
    storage: LocalFileStorageAdapter,
    transport: httpx.BaseTransport | None,
) -> dict[str, Any]:
    with BinanceSpotRestCollector.from_settings(
        settings, storage, http_transport=transport
    ) as collector:
        result = collector.collect(request)
    return {
        "command": "collect",
        **_request_document(request),
        "objects": len(result.objects),
        "gaps": [
            {
                "symbol": gap.symbol,
                "start": gap.coverage_start.isoformat(),
                "end": gap.coverage_end.isoformat(),
                "reason": gap.reason.value,
            }
            for gap in result.gaps
        ],
    }


def _ingested(request: CollectionRequest, adapter: Any, storage: Any, origin: str) -> Any:
    with RestRevisionStore(adapter, storage, market_data_base_url=origin) as store:
        return store.ingest_collection(request)


def _ingest_summary(request: CollectionRequest, stored: RestCollectionStored) -> dict[str, Any]:
    return {
        "command": "ingest",
        **_request_document(request),
        "collection_outcome": stored.collection_outcome,
        "pages": len(stored.pages),
        "new_snapshot_ids": list(stored.new_snapshot_ids),
        "replayed": stored.replayed,
        "findings": dict(sorted(Counter(item.code for item in stored.findings).items())),
    }


def _normalize(
    request: CollectionRequest, adapter: Any, storage: Any, origin: str, settings: Settings
) -> dict[str, Any]:
    stored: RestCollectionStored = _ingested(request, adapter, storage, origin)
    table = REST_ELEMENT_TABLES[request.data_type].table
    units = []
    with CanonicalNormalizer(
        adapter, storage, scratch_directory=settings.canonical_scratch_path
    ) as normalizer:
        for page in stored.pages:
            unit = normalizer.normalize_unit(table, page.response_revision_id)
            units.append(
                {
                    "symbol": page.symbol,
                    "page_index": page.page_index,
                    "response_revision_id": page.response_revision_id,
                    "revision_count": unit.revision_count,
                    "batch_count": unit.batch_count,
                    "replayed_batch_count": unit.replayed_batch_count,
                }
            )
    return {
        "command": "normalize",
        **_request_document(request),
        "ingest_replayed": stored.replayed,
        "canonical_table": rules.CANONICAL_TABLES[request.data_type].table,
        "units": units,
    }


def _reconcile(request: CollectionRequest, adapter: Any, storage: Any) -> dict[str, Any]:
    reconciler = ChannelReconciler(adapter, storage)
    partitions = []
    for symbol in request.symbols:
        for day in _days(request.coverage_start, request.coverage_end):
            done = reconciler.reconcile(request.data_type, symbol, day)
            partitions.append(
                {
                    "symbol": symbol,
                    "day": day.isoformat(),
                    "edges": len(done.edges),
                    "new_edges": len(done.new_edge_ids),
                    "evidence_snapshot_id": done.evidence_snapshot_id,
                    "findings": dict(sorted(Counter(item.code for item in done.findings).items())),
                }
            )
    return {"command": "reconcile", **_request_document(request), "partitions": partitions}


def _run(
    args: argparse.Namespace,
    request: CollectionRequest,
    settings: Settings,
    transport: httpx.BaseTransport | None,
) -> int:
    if args.command == "plan":
        support.emit(_plan(request, settings))
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
                summary = _collect(request, settings, storage, transport)
            elif args.command == "ingest":
                summary = _ingest_summary(request, _ingested(request, adapter, storage, origin))
            elif args.command == "normalize":
                summary = _normalize(request, adapter, storage, origin, settings)
            else:
                summary = _reconcile(request, adapter, storage)
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
        request = _collection_request(args)
    except ValidationError as exc:
        fields = sorted(
            {".".join(str(p) for p in error["loc"]) or "request" for error in exc.errors()}
        )
        print(f"error: invalid request: {', '.join(fields)}", file=sys.stderr)
        return support.EXIT_USAGE
    try:
        resolved = support.load_settings() if settings is None else settings
    except ValidationError as exc:
        print(f"error: {support.settings_error(exc)}", file=sys.stderr)
        return support.EXIT_ENVIRONMENT
    return _run(args, request, resolved, http_transport)


if __name__ == "__main__":
    raise SystemExit(main())

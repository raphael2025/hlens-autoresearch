"""Operator entry of the v3 canonical-partition and listing-history Quality reports (ADR-0101 §6).

``python -m infrastructure.quality.report_cli [plan|report|verify] --profile P --data-type T
--symbol S ... --day YYYY-MM-DD ...``:

- ``plan`` (the default when no subcommand is named): print the partitions and the profile
  identity. Opens nothing;
- ``report``: ``QualityReporterV3.report`` of every ``(symbol, day)`` partition: append the
  report, or verify and reuse the committed one;
- ``verify``: the same with ``existing_only=True``: every partition must already have its report,
  which is re-derived and compared; nothing is written to the catalog.

``python -m infrastructure.quality.report_cli listing-{plan,report,verify} --profile P``
(ADR-0101 修订 1 §2): the same three meanings for the one listing-history v2 report
(``ListingHistoryQualityReporterV2``) at the current heads of ``canonical.instrument_listings``
and its exchangeInfo Raw table. Its bounds are the profile's ``quality`` section plus its
``listing_quality`` section; a profile without that section is refused (exit 3) before anything
opens. ``listing-plan`` opens nothing.

Every limit comes from the ``DatasetBuildProfile`` (``--profile``; ADR-0101 D1): the PIT run
bounds and the ``quality`` section; nothing is defaulted here (DQ-9 stays OPEN). The run scratch
is the isolated Quality scratch of ADR-0101 D4. No network is reached by any subcommand. Errors
never print settings or DSNs.

Exit codes: 0 ok; 1 a report failed; 2 usage; 3 invalid profile; 4 settings or data plane
unavailable.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Callable, Sequence
from contextlib import ExitStack
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any, Final

from pydantic import ValidationError

from core.contracts.catalog import CatalogError
from core.contracts.storage import StorageAdapter
from infrastructure.canonical import rules
from infrastructure.catalog.phase1_tables import (
    BINANCE_SPOT_EXCHANGE_INFO,
    CANONICAL_INSTRUMENT_LISTINGS,
)
from infrastructure.dataset.factory import market_data_origin, open_quality_scratch
from infrastructure.dataset.job_port import profile_identity
from infrastructure.dataset.profile import (
    DatasetBuildProfile,
    DatasetProfileError,
    load_dataset_profile,
)
from infrastructure.quality.listing_report_v2 import (
    ListingHistoryQualityReportError,
    ListingHistoryQualityReporterV2,
)
from infrastructure.quality.report_v3 import (
    QualityReporterV3,
    QualityReportV3Error,
)
from infrastructure.revision.store import RevisionCatalog
from infrastructure.settings import Settings
from infrastructure.tools import cli_support as support

__all__ = [
    "COMMANDS",
    "LISTING_COMMANDS",
    "listing_reporter_from_profile",
    "main",
    "reporter_from_profile",
]

LISTING_COMMANDS: Final = ("listing-plan", "listing-report", "listing-verify")
COMMANDS: Final = ("plan", "report", "verify", *LISTING_COMMANDS)
_LISTING_TABLES: Final = (CANONICAL_INSTRUMENT_LISTINGS.table, BINANCE_SPOT_EXCHANGE_INFO.table)
_LISTING_STREAMS: Final = ("events", "event_revisions", "evidence_gaps")
_STREAMS: Final = ("events", "event_revisions", "evidence_gaps")
#: Report failures whose message is printed (redacted); anything else prints its type only.
_KNOWN_FAILURES: Final = (QualityReportV3Error, ListingHistoryQualityReportError, CatalogError)


def reporter_from_profile(
    adapter: RevisionCatalog,
    storage: StorageAdapter,
    scratch: StorageAdapter,
    profile: DatasetBuildProfile,
    *,
    canonical_scratch_directory: Path,
    clock: Callable[[], datetime],
) -> QualityReporterV3:
    """A ``QualityReporterV3`` whose every bound is the profile's (the capacity probe's mapping)."""
    quality = profile.quality
    reporter = quality.reporter
    return QualityReporterV3(
        adapter,
        storage,
        canonical_scratch_directory=canonical_scratch_directory,
        scratch_storage=scratch,
        clock=clock,
        pit_params=profile.pit,
        run_capacity=quality.run_capacity,
        merge_fanout=quality.merge_fanout,
        run_limits=quality.run_limits,
        stream_limits=quality.stream_limits,
        max_event_record_bytes=reporter.max_event_record_bytes,
        max_revision_record_bytes=reporter.max_revision_record_bytes,
        max_gap_record_bytes=reporter.max_gap_record_bytes,
        max_input_record_bytes=reporter.max_input_record_bytes,
        max_manifest_record_bytes=reporter.max_manifest_record_bytes,
        max_identity_bytes=quality.max_identity_bytes,
        retries=reporter.retries,
    )


def listing_reporter_from_profile(
    adapter: RevisionCatalog,
    storage: StorageAdapter,
    scratch: StorageAdapter,
    profile: DatasetBuildProfile,
    *,
    market_data_base_url: str,
    clock: Callable[[], datetime],
) -> ListingHistoryQualityReporterV2:
    """A listing-history v2 reporter whose every bound is the profile's (修订 1 §2).

    ``quality`` supplies what the partition reporter shares with it (the capacity probe's
    mapping); ``listing_quality`` the rest. Raises ``DatasetProfileError`` without that section.
    """
    listing = profile.listing_quality
    if listing is None:
        raise DatasetProfileError(
            "the profile has no listing_quality section (ADR-0101 修订 1 §2: schema_version "
            ">= 1.1.0 with listing_quality is required for the listing-history report)"
        )
    quality = profile.quality
    reporter = quality.reporter
    return ListingHistoryQualityReporterV2(
        adapter,
        storage,
        scratch_storage=scratch,
        market_data_base_url=market_data_base_url,
        clock=clock,
        metadata_limits=listing.metadata_limits,
        capacity=quality.run_capacity,
        merge_fanout=quality.merge_fanout,
        run_limits=quality.run_limits,
        stream_limits=quality.stream_limits,
        max_record_bytes=listing.max_record_bytes,
        max_run_object_bytes=quality.max_run_object_bytes,
        prefix_leaf_max_records=listing.prefix_leaf_max_records,
        prefix_fanout=listing.prefix_fanout,
        prefix_max_node_bytes=listing.prefix_max_node_bytes,
        prefix_max_record_bytes=listing.prefix_max_record_bytes,
        row_chunk_capacity=listing.row_chunk_capacity,
        max_hash_chunk_bytes=listing.max_hash_chunk_bytes,
        max_event_record_bytes=reporter.max_event_record_bytes,
        max_revision_record_bytes=reporter.max_revision_record_bytes,
        max_gap_record_bytes=reporter.max_gap_record_bytes,
        max_manifest_record_bytes=reporter.max_manifest_record_bytes,
        max_identity_bytes=quality.max_identity_bytes,
        retries=reporter.retries,
    )


def _symbol(text: str) -> str:
    if text not in rules.SYMBOLS:
        raise argparse.ArgumentTypeError(
            f"{text!r} is not a first-slice symbol ({', '.join(sorted(rules.SYMBOLS))})"
        )
    return text


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m infrastructure.quality.report_cli",
        description="Write or verify v3 canonical-partition Quality reports (ADR-0101 §6). "
        "Default: print the plan.",
    )
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--profile", required=True, help="path of the DatasetBuildProfile JSON")
    common.add_argument("--data-type", required=True, choices=sorted(rules.CANONICAL_TABLES))
    common.add_argument("--symbol", required=True, action="append", type=_symbol)
    common.add_argument("--day", required=True, action="append", type=support.utc_day)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("plan", parents=[common], help="print the partitions (default)")
    commands.add_parser("report", parents=[common], help="append or reuse each report")
    commands.add_parser("verify", parents=[common], help="re-derive committed reports only")
    listing = argparse.ArgumentParser(add_help=False)
    listing.add_argument("--profile", required=True, help="path of the DatasetBuildProfile JSON")
    commands.add_parser("listing-plan", parents=[listing], help="print the listing report plan")
    commands.add_parser(
        "listing-report", parents=[listing], help="append or reuse the listing-history report"
    )
    commands.add_parser(
        "listing-verify", parents=[listing], help="re-derive the committed listing report only"
    )
    return parser


def _partitions(args: argparse.Namespace) -> list[tuple[str, date]]:
    return [(symbol, day) for symbol in sorted(set(args.symbol)) for day in sorted(set(args.day))]


def _identity(
    command: str, args: argparse.Namespace, profile: DatasetBuildProfile
) -> dict[str, Any]:
    return {
        "command": command,
        **profile_identity(profile),
        "data_type": args.data_type,
        "canonical_table": rules.CANONICAL_TABLES[args.data_type].table,
    }


def _listing_identity(command: str, profile: DatasetBuildProfile) -> dict[str, Any]:
    return {"command": command, **profile_identity(profile), "tables": list(_LISTING_TABLES)}


def _listing_report(
    args: argparse.Namespace,
    settings: Settings,
    profile: DatasetBuildProfile,
    adapter: RevisionCatalog,
    storage: StorageAdapter,
    scratch: StorageAdapter,
) -> dict[str, Any]:
    heads: dict[str, str] = {}
    for table in _LISTING_TABLES:
        info = adapter.load_table(table)
        if info is None or info.current_snapshot is None:
            raise ListingHistoryQualityReportError(f"{table} has no committed snapshot")
        heads[table] = info.current_snapshot.snapshot_id
    reporter = listing_reporter_from_profile(
        adapter,
        storage,
        scratch,
        profile,
        market_data_base_url=market_data_origin(settings),
        clock=lambda: datetime.now(UTC),
    )
    out = reporter.report(heads, existing_only=args.command == "listing-verify")
    return {
        **_listing_identity(args.command, profile),
        "snapshots": heads,
        "report_id": out.report_id,
        "reused": out.reused,
        "record_counts": {name: out.manifest[name]["record_count"] for name in _LISTING_STREAMS},
    }


def _report(
    args: argparse.Namespace,
    settings: Settings,
    profile: DatasetBuildProfile,
    adapter: RevisionCatalog,
    storage: StorageAdapter,
    scratch: StorageAdapter,
) -> dict[str, Any]:
    reporter = reporter_from_profile(
        adapter,
        storage,
        scratch,
        profile,
        canonical_scratch_directory=settings.canonical_scratch_path,
        clock=lambda: datetime.now(UTC),
    )
    reports = []
    for symbol, day in _partitions(args):
        out = reporter.report(args.data_type, symbol, day, existing_only=args.command == "verify")
        reports.append(
            {
                "symbol": symbol,
                "day": day.isoformat(),
                "report_id": out.report_id,
                "reused": out.reused,
                "subject_snapshot_id": out.manifest["subject_snapshot_id"],
                "record_counts": {name: out.manifest[name]["record_count"] for name in _STREAMS},
            }
        )
    return {**_identity(args.command, args, profile), "reports": reports}


def main(argv: Sequence[str] | None = None, *, settings: Settings | None = None) -> int:
    """Run one subcommand; ``settings`` default to the environment (tests inject their own)."""
    args = _parser().parse_args(support.default_to_plan(argv, COMMANDS))
    try:
        profile = load_dataset_profile(args.profile)
    except DatasetProfileError as exc:
        print(f"error: invalid profile: {exc}", file=sys.stderr)
        return support.EXIT_PROFILE
    listing = args.command in LISTING_COMMANDS
    if listing and profile.listing_quality is None:
        print(
            "error: invalid profile: no listing_quality section (ADR-0101 修订 1 §2: the "
            "listing-history report needs schema_version >= 1.1.0 with listing_quality)",
            file=sys.stderr,
        )
        return support.EXIT_PROFILE
    if args.command == "listing-plan":
        support.emit({**_listing_identity("listing-plan", profile), "network": False})
        return support.EXIT_OK
    if args.command == "plan":
        support.emit(
            {
                **_identity("plan", args, profile),
                "partitions": [
                    {"symbol": symbol, "day": day.isoformat()} for symbol, day in _partitions(args)
                ],
                "network": False,
            }
        )
        return support.EXIT_OK
    try:
        resolved = support.load_settings() if settings is None else settings
    except ValidationError as exc:
        print(f"error: {support.settings_error(exc)}", file=sys.stderr)
        return support.EXIT_ENVIRONMENT
    secrets = support.secrets_of(resolved)
    with ExitStack() as stack:
        try:
            adapter, storage = stack.enter_context(support.open_data_plane(resolved))
            scratch = stack.enter_context(open_quality_scratch(resolved.canonical_scratch_path))
        except (CatalogError, OSError, ValueError) as exc:
            message = support.redact(f"{type(exc).__name__}: {exc}", secrets)
            print(f"error: the data plane did not open ({message})", file=sys.stderr)
            return support.EXIT_ENVIRONMENT
        try:
            run = _listing_report if listing else _report
            summary = run(args, resolved, profile, adapter, storage, scratch)
        except _KNOWN_FAILURES as exc:
            message = support.redact(f"{type(exc).__name__}: {exc}", secrets)
            print(f"error: {args.command} failed ({message})", file=sys.stderr)
            return support.EXIT_FAILED
        except Exception as exc:  # never echo an unknown failure's text: it may carry a credential
            print(f"error: {args.command} failed ({type(exc).__name__})", file=sys.stderr)
            return support.EXIT_FAILED
    support.emit(summary)
    return support.EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())

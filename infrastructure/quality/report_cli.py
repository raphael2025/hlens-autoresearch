"""Operator entry of the v3 canonical-partition Quality report (ADR-0101 §6).

``python -m infrastructure.quality.report_cli [plan|report|verify] --profile P --data-type T
--symbol S ... --day YYYY-MM-DD ...``:

- ``plan`` (the default when no subcommand is named): print the partitions and the profile
  identity. Opens nothing;
- ``report``: ``QualityReporterV3.report`` of every ``(symbol, day)`` partition: append the
  report, or verify and reuse the committed one;
- ``verify``: the same with ``existing_only=True``: every partition must already have its report,
  which is re-derived and compared; nothing is written to the catalog.

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
from infrastructure.dataset.factory import open_quality_scratch
from infrastructure.dataset.job_port import profile_identity
from infrastructure.dataset.profile import (
    DatasetBuildProfile,
    DatasetProfileError,
    load_dataset_profile,
)
from infrastructure.quality.report_v3 import (
    QualityReporterV3,
    QualityReportV3Error,
)
from infrastructure.revision.store import RevisionCatalog
from infrastructure.settings import Settings
from infrastructure.tools import cli_support as support

__all__ = ["COMMANDS", "main", "reporter_from_profile"]

COMMANDS: Final = ("plan", "report", "verify")
_STREAMS: Final = ("events", "event_revisions", "evidence_gaps")
#: Report failures whose message is printed (redacted); anything else prints its type only.
_KNOWN_FAILURES: Final = (QualityReportV3Error, CatalogError)


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
            summary = _report(args, resolved, profile, adapter, storage, scratch)
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

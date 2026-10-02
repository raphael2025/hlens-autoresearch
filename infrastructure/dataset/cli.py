"""Operator entry of the v3 Dataset path (ADR-0101 D2): ``python -m infrastructure.dataset.cli``.

Subcommands, every one needing ``--profile`` (a strict ``DatasetBuildProfile`` JSON file):

- ``build``: pin a point-in-time spec to the catalog's current heads, build one v3 Dataset through
  ``DatasetBuildPipeline.build`` and print a fixed-size JSON summary;
- ``show``: the same request arguments, read-only: print what ``build`` would do (the pinned PIT
  spec hash and bindings, the ``selection_id``, whether a manifest of it already exists);
- ``verify``: ``--manifest-hash``; load the persisted v3 manifest through
  ``DatasetBuildPipeline.load_manifest``, which re-derives and merge-compares every stream.

Only the bounded v3 path is used: ``DatasetBuildPipeline`` / ``StreamingEvidenceVerifier``; never
the v2 ``DatasetBuilder`` select / build, an unbounded PIT selection or ``UniverseBuilder.build``
(fixed by ``tests/infrastructure/dataset/test_cli.py``). Settings come from the environment
(``HLENS_*``); the catalog DSN is never printed. The profile's ``profile_hash`` and its
``capacity_evidence`` (``none`` when absent: the profile is then not an accepted configuration)
are part of every summary.

Exit codes: 0 ok; 1 the build / verification failed; 2 usage or an invalid request; 3 invalid
profile; 4 settings or data plane unavailable; 5 ``verify``: no manifest with that hash.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections.abc import Mapping, Sequence
from contextlib import ExitStack
from datetime import UTC, datetime
from typing import Any, Final
from urllib.parse import urlsplit

from pydantic import ValidationError

from core.contracts.catalog import CatalogError
from core.contracts.revision import PointInTimeSpec
from core.contracts.universe import UniverseSelectionSpec
from infrastructure.canonical import rules
from infrastructure.dataset.builder import (
    DatasetBuildError,
    DatasetBuildSummary,
    DatasetEvidenceRequest,
    DatasetSpecError,
)
from infrastructure.dataset.factory import OpenedDatasetPipeline, open_dataset_pipeline
from infrastructure.dataset.pinning import pin_dataset_pit_spec
from infrastructure.dataset.profile import (
    DatasetBuildProfile,
    DatasetProfileError,
    load_dataset_profile,
)
from infrastructure.settings import Settings
from infrastructure.universe.builder import REGISTERED_UNIVERSES

__all__ = ["main"]

EXIT_OK: Final = 0
EXIT_FAILED: Final = 1
EXIT_USAGE: Final = 2
EXIT_PROFILE: Final = 3
EXIT_ENVIRONMENT: Final = 4
EXIT_NOT_FOUND: Final = 5

#: Identity of the spec this entry pins (its content hash, with the pinned heads, is the PIT id).
PIT_SPEC_NAME: Final = "hlens.dataset.cli-pit"
PIT_SPEC_VERSION: Final = "1.0.0"

_URL_CREDENTIALS: Final = re.compile(r"(?i)\b([a-z][a-z0-9+.\-]*://)[^\s/@]*@")


# ------------------------------------------------------------------------- argument parsing


def _instant(text: str) -> datetime:
    try:
        value = datetime.fromisoformat(text)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"not an ISO 8601 timestamp: {text!r}") from exc
    if value.tzinfo is None or value.utcoffset() is None:
        raise argparse.ArgumentTypeError(
            f"{text!r} must carry a UTC offset (e.g. 2025-06-01T00:00:00Z)"
        )
    return value.astimezone(UTC)


def _universe(text: str) -> UniverseSelectionSpec:
    name, separator, version = text.rpartition("@")
    spec = REGISTERED_UNIVERSES.get((name, version)) if separator else None
    if spec is None:
        registered = ", ".join(f"{n}@{v}" for n, v in sorted(REGISTERED_UNIVERSES))
        raise argparse.ArgumentTypeError(
            f"{text!r} is not a registered universe (registered: {registered})"
        )
    return spec


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m infrastructure.dataset.cli",
        description="Build, show and verify v3 Research Datasets (ADR-0101).",
    )
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--profile", required=True, help="path of the DatasetBuildProfile JSON")
    request = argparse.ArgumentParser(add_help=False)
    request.add_argument("--data-type", required=True, choices=sorted(rules.CANONICAL_TABLES))
    request.add_argument("--start", required=True, type=_instant, help="window start (UTC offset)")
    request.add_argument("--end", required=True, type=_instant, help="window end (UTC offset)")
    request.add_argument("--simulation-time", required=True, type=_instant)
    request.add_argument("--knowledge-cutoff", required=True, type=_instant)
    request.add_argument("--universe", required=True, type=_universe, metavar="NAME@VERSION")
    request.add_argument(
        "--listing-assumption",
        action="store_true",
        help="bind the ADR-0051 listing backfill assumption (off unless given)",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("build", parents=[common, request], help="build one v3 Dataset")
    commands.add_parser("show", parents=[common, request], help="print the plan; write nothing")
    verify = commands.add_parser("verify", parents=[common], help="re-verify a persisted manifest")
    verify.add_argument("--manifest-hash", required=True)
    return parser


# ------------------------------------------------------------------------- output


def _redact(text: str, secrets: Sequence[str]) -> str:
    for secret in secrets:
        if secret:
            text = text.replace(secret, "<redacted>")
    return _URL_CREDENTIALS.sub(r"\1<redacted>@", text)


def _secrets(settings: Settings) -> list[str]:
    """Every credential-bearing string of the settings: the DSN and its user / password parts."""
    dsn = settings.catalog_uri.get_secret_value()
    parts = urlsplit(dsn)
    return [dsn, *(part for part in (parts.password, parts.username) if part)]


def _emit(document: Mapping[str, Any]) -> None:
    print(json.dumps(document, sort_keys=True, indent=2))


def _identity(command: str, profile: DatasetBuildProfile) -> dict[str, Any]:
    return {
        "command": command,
        "profile_hash": profile.profile_hash(),
        "capacity_evidence": "none"
        if profile.capacity_evidence is None
        else profile.capacity_evidence,
    }


def _build_summary(
    profile: DatasetBuildProfile, pit: PointInTimeSpec, summary: DatasetBuildSummary
) -> dict[str, Any]:
    return {
        **_identity("build", profile),
        "selection_id": summary.selection_id,
        "manifest_hash": summary.manifest_hash,
        "pit_content_hash": pit.content_hash(),
        "dataset": {"table": summary.dataset.table, "snapshot_id": summary.dataset.snapshot_id},
        "row_count": summary.row_count,
        "chunk_count": summary.chunk_count,
        "replayed_chunk_count": summary.replayed_chunk_count,
        "manifest_replayed": summary.manifest_replayed,
        "replayed": summary.replayed,
        "evidence": [
            {"stream": ref.stream.value, "record_count": ref.record_count}
            for ref in summary.evidence
        ],
    }


# ------------------------------------------------------------------------- commands


def _request(args: argparse.Namespace, opened: OpenedDatasetPipeline) -> DatasetEvidenceRequest:
    pit = pin_dataset_pit_spec(
        opened.adapter,
        name=PIT_SPEC_NAME,
        version=PIT_SPEC_VERSION,
        simulation_time=args.simulation_time,
        knowledge_cutoff=args.knowledge_cutoff,
        listing_assumption=args.listing_assumption,
    )
    return DatasetEvidenceRequest(
        universe=args.universe,
        pit=pit,
        data_type=args.data_type,
        start=args.start,
        end=args.end,
    )


def _run(args: argparse.Namespace, opened: OpenedDatasetPipeline) -> int:
    profile = opened.profile
    pipeline = opened.pipeline
    if args.command == "verify":
        manifest = pipeline.load_manifest(args.manifest_hash)
        if manifest is None:
            print(f"error: no v3 manifest with hash {args.manifest_hash}", file=sys.stderr)
            return EXIT_NOT_FOUND
        _emit(
            {
                **_identity("verify", profile),
                "manifest_hash": args.manifest_hash,
                "verified": True,
                "selection_id": manifest.selection_id,
                "data_type": manifest.data_type,
                "row_count": manifest.row_count,
                "chunk_count": manifest.chunk_count,
            }
        )
        return EXIT_OK
    request = _request(args, opened)
    if args.command == "show":
        selection_id = pipeline.builder.selection_id(request)
        _emit(
            {
                **_identity("show", profile),
                "rule_hash": profile.rule.rule_hash,
                "selection_id": selection_id,
                "pit_content_hash": request.pit.content_hash(),
                "snapshot_bindings": dict(request.pit.snapshot_bindings),
                "data_type": request.data_type,
                "start": request.start.isoformat(),
                "end": request.end.isoformat(),
                "universe": f"{request.universe.name}@{request.universe.version}",
                "listing_assumption": args.listing_assumption,
                "manifest_exists": pipeline.manifests.recorded_version(selection_id) is not None,
            }
        )
        return EXIT_OK
    summary = pipeline.build(request)
    _emit(_build_summary(profile, request.pit, summary))
    return EXIT_OK


def _load_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]


def _settings_error(exc: ValidationError) -> str:
    fields = sorted({".".join(str(part) for part in error["loc"]) for error in exc.errors()})
    return f"settings are invalid: {', '.join(fields)}"


def main(argv: Sequence[str] | None = None, *, settings: Settings | None = None) -> int:
    """Run one subcommand; ``settings`` default to the environment (tests inject their own)."""
    args = _parser().parse_args(argv)
    try:
        profile = load_dataset_profile(args.profile)
    except DatasetProfileError as exc:
        print(f"error: invalid profile: {exc}", file=sys.stderr)
        return EXIT_PROFILE
    try:
        resolved = _load_settings() if settings is None else settings
    except ValidationError as exc:
        print(f"error: {_settings_error(exc)}", file=sys.stderr)
        return EXIT_ENVIRONMENT
    secrets = _secrets(resolved)
    with ExitStack() as stack:
        try:
            opened = stack.enter_context(open_dataset_pipeline(resolved, profile))
        except (CatalogError, OSError, ValueError) as exc:
            message = _redact(f"{type(exc).__name__}: {exc}", secrets)
            print(f"error: the data plane did not open ({message})", file=sys.stderr)
            return EXIT_ENVIRONMENT
        try:
            return _run(args, opened)
        except DatasetSpecError as exc:
            if args.command == "verify":
                print(f"error: verification failed: {_redact(str(exc), secrets)}", file=sys.stderr)
                return EXIT_FAILED
            print(f"error: invalid request: {_redact(str(exc), secrets)}", file=sys.stderr)
            return EXIT_USAGE
        except (DatasetBuildError, CatalogError, ValueError) as exc:
            message = _redact(f"{type(exc).__name__}: {exc}", secrets)
            print(f"error: {args.command} failed ({message})", file=sys.stderr)
            return EXIT_FAILED
        except Exception as exc:  # never echo an unknown failure's text: it may carry a credential
            print(f"error: {args.command} failed ({type(exc).__name__})", file=sys.stderr)
            return EXIT_FAILED


if __name__ == "__main__":
    raise SystemExit(main())

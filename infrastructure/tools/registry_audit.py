"""Read-only integrity snapshots for the file-backed registries (ADR-0091).

Run with ``python -m infrastructure.tools.registry_audit`` and pass every registry path
explicitly. The four original registries are required; the ADR-0098 Lifecycle Registry
(``--lifecycle-root`` / optional ``--lifecycle-anchor``, ADR-0098 修订 1) is audited when its root
is given and is then part of the overall status. The command never opens a normal registry instance,
creates directories or locks, or repairs an anchor. Failure Registry output is structural evidence
only.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from core.domain.research import FailureRecord
from infrastructure.registry.lifecycle import verify_integrity_snapshot as audit_lifecycle
from infrastructure.registry.profile_freeze import (
    verify_integrity_snapshot as audit_profile_freeze,
)
from infrastructure.registry.registry import verify_integrity_snapshot as audit_strategy
from infrastructure.registry.retirement import verify_integrity_snapshot as audit_retirement

__all__ = ["main"]


def _audit_one(
    audit: Callable[..., dict[str, object]], *args: object, **kwargs: object
) -> dict[str, object]:
    try:
        return audit(*args, **kwargs)
    except Exception as exc:
        return {
            "status": "FAILED",
            "error_type": type(exc).__name__,
            "error": str(exc),
        }


def _audit_failure(path: Path) -> dict[str, object]:
    """Validate FailureRecord rows without importing the Research Plane write path."""
    if not path.is_file():
        raise ValueError(f"the Failure Registry does not exist: {path}")
    before = path.stat()
    data = path.read_bytes()
    after = path.stat()
    stamp = (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
    if stamp != (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns):
        raise ValueError(f"{path} changed while the audit snapshot was read")
    if data and not data.endswith(b"\n"):
        raise ValueError(f"{path} ends in a partial trailing line")
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError(f"{path} is not UTF-8") from exc
    records = 0
    for number, line in enumerate(text.splitlines(), 1):
        if not line:
            raise ValueError(f"{path}:{number} is a blank line")
        try:
            FailureRecord.model_validate_json(line)
        except ValidationError as exc:
            raise ValueError(f"{path}:{number} is not a FailureRecord") from exc
        records += 1
    if path.read_bytes() != data or path.stat().st_mtime_ns != stamp[3]:
        raise ValueError(f"{path} changed during the audit")
    return {
        "status": "OK",
        "evidence": "STRUCTURAL_ONLY",
        "path": str(path.resolve()),
        "records": records,
        "snapshot_sha256": hashlib.sha256(data).hexdigest(),
        "limitation": (
            "no hash chain or anchor; valid historical rewrites and full-line removal are "
            "undetectable"
        ),
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m infrastructure.tools.registry_audit",
        description=(
            "Read-only integrity audit of Strategy, Profile Freeze, Retirement, Failure and "
            "(when given) Lifecycle registries. Every path is explicit; no registry is created "
            "or repaired."
        ),
    )
    parser.add_argument("--strategy-root", type=Path, required=True)
    parser.add_argument("--strategy-anchor", type=Path)
    parser.add_argument("--profile-freeze-root", type=Path, required=True)
    parser.add_argument("--profile-freeze-anchor", type=Path, required=True)
    parser.add_argument("--retirement-root", type=Path, required=True)
    parser.add_argument("--retirement-anchor", type=Path)
    parser.add_argument("--failure-registry", type=Path, required=True)
    parser.add_argument("--lifecycle-root", type=Path)
    parser.add_argument("--lifecycle-anchor", type=Path)
    return parser


def audit_registries(args: argparse.Namespace) -> dict[str, Any]:
    """Return an independent result for each explicitly named registry."""
    results: dict[str, dict[str, object]] = {
        "strategy": _audit_one(audit_strategy, args.strategy_root, anchor=args.strategy_anchor),
        "profile_freeze": _audit_one(
            audit_profile_freeze,
            args.profile_freeze_root,
            anchor=args.profile_freeze_anchor,
        ),
        "retirement": _audit_one(
            audit_retirement, args.retirement_root, anchor=args.retirement_anchor
        ),
        "failure": _audit_one(_audit_failure, args.failure_registry),
    }
    if args.lifecycle_root is not None:
        results["lifecycle"] = _audit_one(
            audit_lifecycle, args.lifecycle_root, anchor=args.lifecycle_anchor
        )
    overall = "FAILED" if any(item["status"] != "OK" for item in results.values()) else "OK"
    return {"schema_version": "1.1.0", "status": overall, "registries": results}


def main(argv: Sequence[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    if args.lifecycle_anchor is not None and args.lifecycle_root is None:
        parser.error("--lifecycle-anchor needs --lifecycle-root")
    report = audit_registries(args)
    json.dump(report, sys.stdout, ensure_ascii=False, indent=2, sort_keys=True)
    sys.stdout.write("\n")
    return 0 if report["status"] == "OK" else 1


if __name__ == "__main__":  # pragma: no cover - CLI entry point
    raise SystemExit(main())

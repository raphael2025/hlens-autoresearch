"""Explicit batch driver of the P11 degradation operation (ADR-0105 §4, ADR-0049 §7).

``run_batch(manifest, reports_root=...)`` runs the existing degradation CLI logic
(``research.operations.degradation_cli.main``, called in-process — no subprocess) once per entry
of an explicit TOML manifest, in file order, writing each entry's report with the existing
append-only writer. The caller (an external scheduler, ADR-0049) decides *when* to call it; this
module decides nothing else:

- no discovery: the entries are exactly the manifest's; no ACTIVE-set expansion, no directory
  scan, no "latest" file or head selection beyond what an entry itself says
  (``authority_head = "latest"`` is the CLI's own explicit value);
- no clock: every window and ``authority_as_of`` is written in the manifest;
- one failure is recorded and the batch continues; the batch exit code is non-zero if any entry
  failed. A *degraded* result is still a successful entry (evidence only, ADR-0049).

**Manifest** (strict TOML; unknown or missing keys, wrong types, an empty entry list, a missing
or different ``format`` are refused before anything runs)::

    format = "hlens.p11.degradation-batch@1.0.0"

    [[entry]]
    subject = "strategy:name@1.0.0"
    profile = "profile.json"                 # paths are relative to the manifest's directory
    baseline_report = "baseline_report.json"
    baseline_set = "baseline_set.json"
    window_start = "2026-01-01T00:00:00Z"    # quoted strings (explicit UTC offset)
    window_end = "2026-02-01T00:00:00Z"
    window_label = "2026-01"
    freeze_registry = "freeze_registry"
    freeze_anchor = "freeze_anchor.jsonl"
    # exactly one input mode (ADR-0098 §4):
    lifecycle = "lifecycle.json"             #  caller-declared: lifecycle + recent_manifest
    recent_manifest = "recent.json"
    # authority_registry, authority_head, dataset_id, manifest_hash, authority_as_of
    #   (+ optional authority_environment, authority_evidence_verifier, authority_anchor)

``reports_root`` is the one ``run_batch`` argument (never in the manifest). An embedding caller
may pass ``authority_environment`` (used by authority-mode entries only, as ``degradation_cli.main``
does); command line entries use ``authority_environment = "MODULE:CALLABLE"``.

Run with ``python -m research.operations.degradation_batch --manifest <toml> --reports-root
<dir>``. Exit codes: ``0`` every entry succeeded; ``1`` at least one entry failed; ``2`` the
manifest was refused (nothing ran). Only one batch (or CLI) may use a reports root at a time.
"""

from __future__ import annotations

import argparse
import contextlib
import io
import sys
import tomllib
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

from research.operations import degradation_cli

if TYPE_CHECKING:
    from research.operations.authority import AuthorityEnvironment

__all__ = [
    "MANIFEST_FORMAT",
    "BatchEntry",
    "BatchEntryResult",
    "BatchManifestError",
    "BatchResult",
    "load_manifest",
    "main",
    "run_batch",
]

MANIFEST_FORMAT: Final = "hlens.p11.degradation-batch@1.0.0"
EXIT_FAILED: Final = 1
EXIT_MANIFEST: Final = 2

#: key -> (CLI flag, is a path); required for every entry.
_COMMON: Final[Mapping[str, tuple[str, bool]]] = {
    "subject": ("--subject", False),
    "profile": ("--profile", True),
    "baseline_report": ("--baseline-report", True),
    "baseline_set": ("--baseline-set", True),
    "window_start": ("--window-start", False),
    "window_end": ("--window-end", False),
    "window_label": ("--window-label", False),
    "freeze_registry": ("--freeze-registry", True),
    "freeze_anchor": ("--freeze-anchor", True),
}
_EXPLICIT: Final[Mapping[str, tuple[str, bool]]] = {
    "lifecycle": ("--lifecycle", True),
    "recent_manifest": ("--recent-manifest", True),
}
_AUTHORITY: Final[Mapping[str, tuple[str, bool]]] = {
    "authority_registry": ("--authority-registry", True),
    "authority_head": ("--authority-head", False),
    "dataset_id": ("--dataset-id", False),
    "manifest_hash": ("--manifest-hash", False),
    "authority_as_of": ("--authority-as-of", False),
}
_AUTHORITY_OPTIONAL: Final[Mapping[str, tuple[str, bool]]] = {
    "authority_environment": ("--authority-environment", False),
    "authority_evidence_verifier": ("--authority-evidence-verifier", False),
    "authority_anchor": ("--authority-anchor", True),
}


class BatchManifestError(ValueError):
    """The manifest is not a valid explicit batch manifest; nothing ran."""


@dataclass(frozen=True, slots=True)
class BatchEntry:
    """One validated manifest entry: its position, subject, window label and the CLI arguments
    (without ``--reports-root``) the existing degradation CLI is called with."""

    index: int
    subject: str
    window_label: str
    authority: bool
    argv: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class BatchEntryResult:
    """The outcome of one entry: the CLI's exit code, its ``key=value`` stdout fields (e.g.
    ``status``, ``check_hash``, ``report``) and its refusal / failure message (``stderr``)."""

    index: int
    subject: str
    window_label: str
    exit_code: int
    fields: tuple[tuple[str, str], ...]
    error: str

    @property
    def succeeded(self) -> bool:
        return self.exit_code == 0


@dataclass(frozen=True, slots=True)
class BatchResult:
    """Every entry's outcome, in manifest order."""

    entries: tuple[BatchEntryResult, ...]

    @property
    def failed(self) -> tuple[BatchEntryResult, ...]:
        return tuple(entry for entry in self.entries if not entry.succeeded)

    @property
    def exit_code(self) -> int:
        """``0`` when every entry succeeded, else ``1``."""
        return 0 if not self.failed else EXIT_FAILED


def _string(value: object, key: str, where: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise BatchManifestError(f"{where}: {key!r} must be a non-empty string")
    return value


def _entry(raw: object, index: int, base: Path) -> BatchEntry:
    where = f"entry {index + 1}"
    if not isinstance(raw, dict):
        raise BatchManifestError(f"{where} must be a table")
    explicit = [key for key in _EXPLICIT if key in raw]
    authority = [key for key in _AUTHORITY if key in raw]
    optional = [key for key in _AUTHORITY_OPTIONAL if key in raw]
    if len(explicit) == len(_EXPLICIT) and not authority:
        if optional:
            raise BatchManifestError(f"{where}: {sorted(optional)} need the authority mode")
        mode = _EXPLICIT
    elif len(authority) == len(_AUTHORITY) and not explicit:
        mode = {**_AUTHORITY, **_AUTHORITY_OPTIONAL}
    else:
        raise BatchManifestError(
            f"{where}: give exactly one complete input mode: {sorted(_EXPLICIT)} (caller-declared) "
            f"or {sorted(_AUTHORITY)} (authority)"
        )
    allowed = {**_COMMON, **mode}
    unknown = sorted(set(raw) - set(allowed))
    if unknown:
        raise BatchManifestError(f"{where}: unknown keys {unknown}")
    missing = sorted(
        key
        for key in {**_COMMON, **(_EXPLICIT if mode is _EXPLICIT else _AUTHORITY)}
        if key not in raw
    )
    if missing:
        raise BatchManifestError(f"{where}: missing keys {missing}")
    argv: list[str] = []
    for key in allowed:
        if key not in raw:
            continue
        flag, is_path = allowed[key]
        text = _string(raw[key], key, where)
        argv += [flag, str(base / text) if is_path else text]
    return BatchEntry(
        index=index,
        subject=raw["subject"],
        window_label=raw["window_label"],
        authority=mode is not _EXPLICIT,
        argv=tuple(argv),
    )


def load_manifest(manifest: Path) -> tuple[BatchEntry, ...]:
    """Parse and validate ``manifest`` strictly (module docs); ``BatchManifestError`` otherwise."""
    try:
        text = Path(manifest).read_text(encoding="utf-8")
        document = tomllib.loads(text)  # refuses duplicate keys and redefined tables
    except (tomllib.TOMLDecodeError, UnicodeError) as exc:
        raise BatchManifestError("the manifest is not valid TOML") from exc
    except OSError as exc:
        raise BatchManifestError(f"the manifest cannot be read ({type(exc).__name__})") from exc
    unknown = sorted(set(document) - {"format", "entry"})
    if unknown:
        raise BatchManifestError(f"unknown top-level keys {unknown}")
    if document.get("format") != MANIFEST_FORMAT:
        raise BatchManifestError(f"format must be {MANIFEST_FORMAT!r}")
    raw_entries: Any = document.get("entry")
    if not isinstance(raw_entries, list) or not raw_entries:
        raise BatchManifestError("the manifest needs at least one [[entry]]")
    base = Path(manifest).resolve().parent
    return tuple(_entry(raw, index, base) for index, raw in enumerate(raw_entries))


def _fields(stdout: str) -> tuple[tuple[str, str], ...]:
    pairs: list[tuple[str, str]] = []
    for line in stdout.splitlines():
        key, separator, value = line.partition("=")
        if separator:
            pairs.append((key, value))
    return tuple(pairs)


def _invoke(
    entry: BatchEntry, reports_root: Path, environment: AuthorityEnvironment | None
) -> BatchEntryResult:
    run: Callable[..., int] = degradation_cli.main
    argv = [*entry.argv, "--reports-root", str(reports_root)]
    out, err = io.StringIO(), io.StringIO()
    code: int
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            if entry.authority and environment is not None:
                code = run(argv, authority_environment=environment)
            else:
                code = run(argv)
        except SystemExit as exc:  # argparse usage errors
            code = exc.code if isinstance(exc.code, int) and exc.code != 0 else 2
        except Exception as exc:  # one entry's failure never stops the batch
            code = 3
            err.write(f"P11 degradation batch entry failed ({type(exc).__name__})\n")
    return BatchEntryResult(
        index=entry.index,
        subject=entry.subject,
        window_label=entry.window_label,
        exit_code=code,
        fields=_fields(out.getvalue()),
        error=err.getvalue().strip(),
    )


def run_batch(
    manifest: Path,
    *,
    reports_root: Path,
    authority_environment: AuthorityEnvironment | None = None,
) -> BatchResult:
    """Run every manifest entry in order (module docs). The manifest is fully validated first
    (``BatchManifestError``, nothing runs); then each entry runs through the degradation CLI
    logic and a failing entry is recorded without stopping the rest."""
    entries = load_manifest(manifest)
    return BatchResult(
        entries=tuple(
            _invoke(entry, Path(reports_root), authority_environment) for entry in entries
        )
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run the explicit P11 degradation checks of a TOML manifest (ADR-0105 §4)."
    )
    parser.add_argument("--manifest", required=True, type=Path, help="batch manifest TOML")
    parser.add_argument(
        "--reports-root", required=True, type=Path, help="existing report directory"
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        result = run_batch(args.manifest, reports_root=args.reports_root)
    except BatchManifestError as exc:
        print(f"P11 degradation batch refused the manifest ({exc})", file=sys.stderr)
        return EXIT_MANIFEST
    for item in result.entries:
        fields = " ".join(f"{key}={value}" for key, value in item.fields if key != "report")
        state = "ok" if item.succeeded else f"failed exit={item.exit_code}"
        line = f"entry={item.index + 1} subject={item.subject} window={item.window_label}"
        print(f"{line} {state} {fields}".rstrip())
        if item.error:
            print(f"  {item.error}", file=sys.stderr)
    print(f"entries={len(result.entries)} failed={len(result.failed)}")
    return result.exit_code


if __name__ == "__main__":
    raise SystemExit(main())

"""Explicit local writer / reader CLI of the Lifecycle Registry (ADR-0105 §2, ADR-0098 §1).

``python -m infrastructure.registry.lifecycle_cli append`` is the **only** production write path
of the ADR-0098 Lifecycle Registry. The research loop, the ADR-0074 operator and the API never
import this module (an architecture test pins it); an external scheduler or a human invokes it.

- ``show-head --registry <root> (--anchor <path> | --no-anchor)``: open the registry as a
  read-only snapshot (no lock, nothing created, no anchor repair write) and print its head.
- ``append --registry <root> --transition <json> --expected-head <hash> (--anchor <path> |
  --no-anchor) [--create] [--commit]``: validate one ``LifecycleTransition`` against the registry
  head and, **only with ``--commit``**, append it. Without ``--commit`` nothing is written (a dry
  run on the read-only snapshot: head, legality and the strategy's history rules are checked and
  reported). ``--expected-head`` is the record hash of the head the caller read (the all-zero
  genesis hash for an empty registry); a mismatch is refused and nothing is written.

**Explicit anchor (ADR-0098 修订 1 item 2).** Exactly one of ``--anchor <path>`` and
``--no-anchor`` is required; without an anchor the output says ``anchor=absent`` (whole trailing
records dropped by a rollback cannot be detected). The anchor must live outside the registry root.
A missing anchor file is only accepted for an empty / not yet created registry (the anchor then
starts with the first record); next to existing history a missing anchor is refused rather than
silently starting a new one.

**No clock, no invented history.** The transition JSON must contain ``occurred_at`` (a UTC
timestamp): the command never fills it from the clock. Backfilling is the caller appending the
**real** history record by record (ADR-0098); nothing here fabricates a transition. A registry
that does not exist is only created with ``--create`` (and only for the genesis head).

Exit codes: ``0`` success (including a dry run); ``1`` the request was refused (bad input, head
mismatch, illegal transition, missing registry / anchor); ``3`` an I/O or registry-integrity
failure (corrupted chain or anchor, a held writer lock, ``OSError``). Output lines are
``key=value`` and contain no secrets.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any, Final

from pydantic import ValidationError

from core.errors import LifecycleViolation
from core.lifecycle.strategy import LifecycleHistory, LifecycleTransition, validate_transition
from infrastructure.event_bus.journal import GENESIS_HASH
from infrastructure.registry.lifecycle import (
    JOURNAL_NAME,
    HeadMismatch,
    LifecycleHead,
    LifecycleRegistry,
)
from infrastructure.registry.registry import (
    RegistryCorrupted,
    RegistryError,
    RegistryLocked,
    RegistryRefused,
)

__all__ = ["main"]

_SHA256: Final = re.compile(r"^[0-9a-f]{64}$")
EXIT_REFUSED: Final = 1
EXIT_FAILED: Final = 3


class _InputError(ValueError):
    """A flag or the transition file is malformed or refused before any registry access."""


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise _InputError("JSON object has a duplicate key")
        result[key] = value
    return result


def _reject_constant(_: str) -> None:
    raise _InputError("JSON contains a non-finite number")


def _read_transition(path: Path) -> LifecycleTransition:
    """The transition file: strict JSON (no duplicate keys, no NaN) with an explicit
    ``occurred_at`` (the model would otherwise default it from the clock)."""
    try:
        raw = json.loads(
            path.read_text(encoding="utf-8"),
            object_pairs_hook=_unique_object,
            parse_constant=_reject_constant,
        )
    except (json.JSONDecodeError, UnicodeError, RecursionError) as exc:
        raise _InputError("the transition file is not valid JSON") from exc
    if not isinstance(raw, dict):
        raise _InputError("the transition file must be a JSON object")
    if "occurred_at" not in raw:
        raise _InputError("the transition must state occurred_at (no clock default is used)")
    try:
        return LifecycleTransition.model_validate(raw)
    except ValidationError as exc:
        raise _InputError("the transition does not match LifecycleTransition") from exc


def _expected_head(text: str) -> str:
    if not _SHA256.fullmatch(text):
        raise _InputError("--expected-head is a lowercase SHA-256 record hash")
    return text


def _anchor_arg(parser: argparse.ArgumentParser, args: argparse.Namespace) -> Path | None:
    if (args.anchor is None) == (not args.no_anchor):
        parser.error("give exactly one of --anchor <path> and --no-anchor")
    return None if args.no_anchor else args.anchor


def _registry_exists(root: Path) -> bool:
    return root.is_dir() and (root / JOURNAL_NAME).is_file()


def _anchor_path_checks(root: Path, anchor: Path | None) -> None:
    if anchor is not None and anchor.resolve().is_relative_to(root.resolve()):
        raise _InputError("the anchor must live outside the registry root")


def _open_view(root: Path, anchor: Path | None, *, create: bool) -> LifecycleRegistry | None:
    """The read-only snapshot of an existing registry (anchor verified when it exists), or
    ``None`` for a registry that does not exist yet and ``create`` allows (genesis)."""
    _anchor_path_checks(root, anchor)
    if _registry_exists(root):
        if anchor is not None and not anchor.is_file():
            # only an empty registry may start its anchor; history without one is refused
            unanchored = LifecycleRegistry.open_snapshot(root)
            if unanchored.head.record_count > 0:
                unanchored.close()
                raise _InputError(
                    "the anchor file does not exist next to an existing registry; a missing "
                    "anchor would silently start a new one"
                )
            return unanchored
        return LifecycleRegistry.open_snapshot(root, anchor=anchor)
    if not create:
        raise _InputError("the Lifecycle Registry does not exist (use --create for genesis)")
    if root.exists() and not root.is_dir():
        raise _InputError("the registry root is not a directory")
    if anchor is not None and anchor.exists():
        raise _InputError("an anchor already exists for a registry that does not")
    return None


def _head_lines(head: LifecycleHead, anchor: Path | None) -> list[str]:
    return [
        f"record_count={head.record_count}",
        f"head={head.last_record_hash}",
        f"anchor={'absent' if anchor is None else 'present'}",
    ]


def _show_head(args: argparse.Namespace, anchor: Path | None) -> int:
    root: Path = args.registry
    _anchor_path_checks(root, anchor)
    if not _registry_exists(root):
        raise _InputError("the Lifecycle Registry does not exist")
    if anchor is not None and not anchor.is_file():
        raise _InputError("the anchor file does not exist")
    with LifecycleRegistry.open_snapshot(root, anchor=anchor) as snapshot:
        for line in _head_lines(snapshot.head, anchor):
            print(line)
    return 0


def _dry_run(
    view: LifecycleRegistry | None, transition: LifecycleTransition, expected: LifecycleHead
) -> LifecycleHead:
    """The registry's own legality rules on the read-only view; nothing is written."""
    head = LifecycleHead(0, GENESIS_HASH) if view is None else view.head
    if head != expected:
        raise HeadMismatch(
            f"expected head {expected.record_count}/{expected.last_record_hash} but the "
            f"registry is at {head.record_count}/{head.last_record_hash}"
        )
    try:
        previous = None if view is None else view.lifecycle_of(transition.subject, head).history
        history = LifecycleHistory(subject=transition.subject) if previous is None else previous
        if transition.from_state is not history.current_state:
            raise RegistryRefused(
                f"{transition.subject} replays to {history.current_state}, not the "
                f"transition's from_state {transition.from_state}"
            )
        validate_transition(
            transition.from_state, transition.to_state, approved_by=transition.approved_by
        )
        LifecycleHistory(subject=history.subject, transitions=(*history.transitions, transition))
    except (LifecycleViolation, ValidationError, ValueError) as exc:
        raise RegistryRefused(f"illegal lifecycle transition: {exc}") from exc
    return head


def _append(args: argparse.Namespace, anchor: Path | None) -> int:
    root: Path = args.registry
    transition = _read_transition(args.transition)
    expected_hash = _expected_head(args.expected_head)
    view = _open_view(root, anchor, create=args.create)
    try:
        if view is None:
            if expected_hash != GENESIS_HASH:
                raise HeadMismatch("a registry that does not exist only has the genesis head")
            expected = LifecycleHead(0, GENESIS_HASH)
        else:
            expected = view.head_for_hash(expected_hash)
        head = _dry_run(view, transition, expected)
    finally:
        if view is not None:
            view.close()
    print(f"subject={transition.subject}")
    print(f"transition={transition.from_state.value}->{transition.to_state.value}")
    print(f"transition_hash={transition.content_hash()}")
    if not args.commit:
        print("committed=false (dry run; pass --commit to append)")
        for line in _head_lines(head, anchor):
            print(line)
        return 0
    with LifecycleRegistry(root, anchor=anchor) as registry:
        new_head = registry.append(transition, expected_head=expected)
    print("committed=true")
    for line in _head_lines(new_head, anchor):
        print(line)
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Explicit Lifecycle Registry writer / reader (ADR-0105 §2). Nothing is "
        "written unless 'append --commit' is given."
    )
    sub = parser.add_subparsers(dest="command", required=True)

    def common(p: argparse.ArgumentParser) -> None:
        p.add_argument("--registry", required=True, type=Path, help="Lifecycle Registry root")
        p.add_argument("--anchor", type=Path, help="external anchor file (outside the root)")
        p.add_argument("--no-anchor", action="store_true", help="explicitly run without an anchor")

    show = sub.add_parser("show-head", help="print the registry head (read-only snapshot)")
    common(show)
    append = sub.add_parser("append", help="validate (and with --commit append) one transition")
    common(append)
    append.add_argument(
        "--transition", required=True, type=Path, help="LifecycleTransition JSON file"
    )
    append.add_argument(
        "--expected-head",
        required=True,
        help="record hash of the head the caller read (all zeros: the empty registry)",
    )
    append.add_argument(
        "--create", action="store_true", help="allow creating a registry that does not exist"
    )
    append.add_argument(
        "--commit", action="store_true", help="actually append; the default writes nothing"
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    anchor = _anchor_arg(parser, args)
    try:
        if args.command == "show-head":
            return _show_head(args, anchor)
        return _append(args, anchor)
    except (_InputError, HeadMismatch, RegistryRefused) as exc:
        print(f"lifecycle CLI refused ({type(exc).__name__}: {exc})", file=sys.stderr)
        return EXIT_REFUSED
    except (RegistryCorrupted, RegistryLocked, RegistryError, OSError, ValueError) as exc:
        print(f"lifecycle CLI failed ({type(exc).__name__}: {exc})", file=sys.stderr)
        return EXIT_FAILED


if __name__ == "__main__":
    raise SystemExit(main())

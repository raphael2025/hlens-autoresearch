"""Read back and list stored State runs from the command line (ADR-0102; Phase 2).

``python -m infrastructure.state.run_cli`` has two read-only sub-commands:

- ``show``: read one run from a ``StateResultStore`` by ``result_hash`` (content and canonical form
  verified; ``--values`` also prints every state value);
- ``list``: the ``result_hash`` of every run in a store.

Neither creates a missing store directory nor reads settings. Running a state (``compute``, which
resolves providers from ``plugins``) lives on the research side: ``research.states.run_cli``.
This module also holds the helpers that command and ``research.states.report_cli`` share
(``read_contract``, ``open_store``, ``summary_lines``, ``CliError``).

Errors never print settings, DSNs or exception arguments of unexpected failures: only the failing
step and the exception type are written to stderr.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from core.contracts.state import StateResult
from core.domain.base import Contract, contract_schema_version_scope
from infrastructure.state.store import StateResultStore, StateStoreCorrupted

__all__ = [
    "CliError",
    "main",
    "open_store",
    "read_contract",
    "summary_lines",
]


class CliError(Exception):
    """A failure whose message is safe to print (it never carries settings or credentials)."""


def read_contract[C: Contract](model: type[C], path: Path, what: str) -> C:
    """Rebuild a persisted contract object from a JSON file (at its recorded schema version).

    Validated in JSON mode: exact-decimal values are stored as text and must parse as ``Decimal``.
    """
    try:
        data = Path(path).read_bytes()
        document = json.loads(data)
    except (OSError, ValueError):
        raise CliError(f"cannot read {what} from {path}: not a readable JSON file") from None
    if not isinstance(document, dict):
        raise CliError(f"{what} {path} is not a JSON object")
    version = document.get("schema_version")
    try:
        if isinstance(version, str):
            with contract_schema_version_scope(version):
                return model.model_validate_json(data)
        return model.model_validate_json(data)
    except (ValidationError, ValueError, TypeError):
        raise CliError(f"{what} {path} is not a valid {model.__name__}") from None


def summary_lines(result: StateResult) -> list[str]:
    """A deterministic ``key<TAB>value`` summary of one run."""
    counts = Counter(item.state for item in result.values)
    lines = [
        f"result_hash\t{result.result_hash}",
        f"request_hash\t{result.request_hash}",
        f"provider\t{result.provider}",
        f"provider_hash\t{result.provider_hash}",
        f"evaluations\t{len(result.values)}",
        f"first_time\t{result.values[0].evaluation_time.isoformat()}",
        f"last_time\t{result.values[-1].evaluation_time.isoformat()}",
        f"not_computable\t{counts.get(None, 0)}",
    ]
    lines.extend(
        f"state\t{label}\t{count}"
        for label, count in sorted(
            ((label, count) for label, count in counts.items() if label is not None)
        )
    )
    return lines


def open_store(path: Path) -> StateResultStore:
    if not Path(path).is_dir():
        raise CliError(f"{path} is not an existing state store directory")
    return StateResultStore(path)


def _show(args: argparse.Namespace) -> int:
    store = open_store(args.store)
    try:
        result = store.get(args.result_hash)
    except KeyError:
        raise CliError(f"no state run {args.result_hash} in {args.store}") from None
    except ValueError:
        raise CliError("result_hash must be 64 lowercase hexadecimal characters") from None
    for line in summary_lines(result):
        print(line)
    print("verified\tyes")
    if args.values:
        for item in result.values:
            label = "" if item.state is None else item.state
            print(f"value\t{item.evaluation_time.isoformat()}\t{label}\t{item.inputs_used}")
    return 0


def _list(args: argparse.Namespace) -> int:
    for result_hash in open_store(args.store).hashes():
        print(result_hash)
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Read back and list stored State runs (ADR-0102). Read-only."
    )
    commands = parser.add_subparsers(dest="command", required=True)

    show = commands.add_parser("show", help="read one run back from a store and verify it")
    show.add_argument("--store", type=Path, required=True)
    show.add_argument("result_hash")
    show.add_argument("--values", action="store_true", help="also print every state value")
    show.set_defaults(handler=_show)

    listing = commands.add_parser("list", help="list the result hashes in a store")
    listing.add_argument("--store", type=Path, required=True)
    listing.set_defaults(handler=_list)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    handler: Any = args.handler
    try:
        return int(handler(args))
    except (CliError, StateStoreCorrupted) as exc:
        print(f"state {args.command} failed: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:
        # Storage errors can embed paths: name the type only.
        print(f"state {args.command} failed: {type(exc).__name__}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

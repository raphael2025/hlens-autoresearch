"""Command line for the reviewed knowledge write path (Phase 0.5; see ``plugins.knowledge.store``).

    uv run python -m plugins.knowledge.cli add --reviewed-by NAME [--items-dir DIR] FILE.json
    uv run python -m plugins.knowledge.cli verify [--items-dir DIR]

``add`` reads one ``KnowledgeItem`` object or a list of them. Every item is checked (contract,
source / licence, reviewer, conflicts with the directory and within the batch) before anything is
written, so an invalid batch writes nothing. ``verify`` loads the directory with the provider rules
and re-verifies every review record. Exit status: 0 on success, 1 on a refused or invalid input.
The HTTP API stays read-only (ADR-0048).
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from core.contracts.knowledge import KnowledgeProviderError
from core.domain.research import KnowledgeItem
from plugins.knowledge.local import DEFAULT_ITEMS_DIR, LocalKnowledgeProvider
from plugins.knowledge.store import KnowledgeWriteError, LocalKnowledgeStore

__all__ = ["main"]


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="plugins.knowledge.cli")
    commands = parser.add_subparsers(dest="command", required=True)
    add = commands.add_parser("add", help="add reviewed knowledge items")
    add.add_argument("file", type=Path)
    add.add_argument("--reviewed-by", required=True)
    add.add_argument("--items-dir", type=Path, default=DEFAULT_ITEMS_DIR)
    verify = commands.add_parser("verify", help="load the items and verify review records")
    verify.add_argument("--items-dir", type=Path, default=DEFAULT_ITEMS_DIR)
    return parser


def _read(path: Path) -> list[Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise KnowledgeWriteError(f"{path}: unreadable: {exc}") from None
    return payload if isinstance(payload, list) else [payload]


def _add(store: LocalKnowledgeStore, file: Path, reviewed_by: str) -> list[str]:
    checked: dict[tuple[str, str], KnowledgeItem] = {}
    for raw in _read(file):
        item = store.check(raw, reviewed_by)
        key = (item.name, item.version)
        if key in checked and checked[key].content_hash() != item.content_hash():
            raise KnowledgeWriteError(
                f"{item.name}@{item.version} appears twice with different content"
            )
        checked[key] = item
    lines = []
    for key in sorted(checked):
        result = store.add(checked[key], reviewed_by=reviewed_by)
        state = "added" if result.created else "unchanged"
        lines.append(f"{state} {key[0]}@{key[1]}")
    return lines


def _verify(store: LocalKnowledgeStore) -> list[str]:
    provider = LocalKnowledgeProvider(store.items_dir)
    lines = []
    for item in provider.items:
        item_path, review_path = store.paths_for(item.name, item.version)
        if item_path.exists():
            review = store.review_of(item.name, item.version)
            lines.append(f"reviewed {review.item} by {review.reviewed_by}")
    for item_path in sorted(store.items_dir.glob("item-*.json")):
        if not item_path.with_suffix(".review").exists():
            raise KnowledgeWriteError(f"{item_path.name} has no review record")
    for review_path in sorted(store.items_dir.glob("item-*.review")):
        if not review_path.with_suffix(".json").exists():
            lines.append(f"incomplete add (review without item): {review_path.name}")
    lines.append(f"{len(provider.items)} items load cleanly")
    return lines


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    store = LocalKnowledgeStore(args.items_dir)
    try:
        if args.command == "add":
            lines = _add(store, args.file, args.reviewed_by)
        else:
            lines = _verify(store)
    except KnowledgeProviderError as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 1
    for line in lines:
        print(line)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

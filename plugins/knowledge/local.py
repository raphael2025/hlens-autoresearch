"""A deterministic, offline KnowledgeProvider over versioned JSON item files (Phase 0.5).

Items live as reviewed JSON in ``docs/research/knowledge/*.json`` (one list of ``KnowledgeItem``
payloads per file; the repository keeps summaries and citations only, never copyrighted full text,
docs/research/knowledge-base.md). Loading fails closed on any item without a source or licence, on
duplicate ``name@version`` and on unparsable payloads. ``search`` is a pure function of the loaded
items and the query: case-insensitive AND over ``name`` / ``claim`` / ``conditions``, optional name
prefix (the library), minimum evidence level and statuses, ordered by ``(name, version)``.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from pathlib import Path
from typing import Final

from pydantic import ValidationError

from core.contracts.knowledge import (
    EVIDENCE_ORDER,
    KnowledgeProviderDescriptor,
    KnowledgeProviderError,
    KnowledgeQuery,
    KnowledgeResult,
)
from core.domain.research import KnowledgeItem

__all__ = ["DEFAULT_ITEMS_DIR", "LocalKnowledgeProvider", "load_items"]

DEFAULT_ITEMS_DIR: Final = Path(__file__).resolve().parents[2] / "docs" / "research" / "knowledge"
_NAME: Final = "hlens_knowledge_local"
_VERSION: Final = "1.0.0"


class LocalKnowledgeProvider:
    """Offline retrieval over reviewed item files; deterministic."""

    def __init__(self, items_dir: Path = DEFAULT_ITEMS_DIR) -> None:
        self._items = load_items(sorted(Path(items_dir).glob("*.json")))
        self._descriptor = KnowledgeProviderDescriptor(
            name=_NAME,
            version=_VERSION,
            deterministic=True,
            sources=(f"file:{Path(items_dir).name}",),
        )

    @property
    def descriptor(self) -> KnowledgeProviderDescriptor:
        return self._descriptor

    @property
    def items(self) -> tuple[KnowledgeItem, ...]:
        return self._items

    def search(self, query: KnowledgeQuery) -> KnowledgeResult:
        if not isinstance(query, KnowledgeQuery):
            raise KnowledgeProviderError("search needs a KnowledgeQuery")
        terms = [term.strip().lower() for term in query.terms]
        floor = (
            None
            if query.evidence_at_least is None
            else EVIDENCE_ORDER.index(query.evidence_at_least)
        )
        hits = []
        for item in self._items:
            if query.name_prefix and not item.name.startswith(query.name_prefix):
                continue
            if floor is not None and EVIDENCE_ORDER.index(item.evidence_level) < floor:
                continue
            if query.statuses and item.status not in query.statuses:
                continue
            text = " ".join((item.name, item.claim, *item.conditions)).lower()
            if all(term in text for term in terms):
                hits.append(item)
        return KnowledgeResult.build(query, self._descriptor.plugin_key, tuple(hits[: query.limit]))


def load_items(paths: Iterable[Path]) -> tuple[KnowledgeItem, ...]:
    """Load and validate item files (the fail-closed rules of the module docstring)."""
    items: dict[tuple[str, str], KnowledgeItem] = {}
    for path in paths:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise KnowledgeProviderError(f"{path.name}: unreadable item file: {exc}") from None
        if not isinstance(payload, list):
            raise KnowledgeProviderError(f"{path.name}: expected a list of knowledge items")
        for raw in payload:
            try:
                item = KnowledgeItem.model_validate_json(json.dumps(raw))
            except ValidationError as exc:
                raise KnowledgeProviderError(
                    f"{path.name}: invalid knowledge item: {exc}"
                ) from None
            if not item.source.strip() or not item.license.strip():
                raise KnowledgeProviderError(f"{path.name}: {item.name} has no source or licence")
            key = (item.name, item.version)
            if key in items:
                raise KnowledgeProviderError(f"{path.name}: duplicate knowledge item {key}")
            items[key] = item
    return tuple(items[key] for key in sorted(items))

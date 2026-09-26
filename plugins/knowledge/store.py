"""The reviewed knowledge write path (Phase 0.5, ADR-0034 §4; knowledge-base.md).

``LocalKnowledgeStore(items_dir).add(item, reviewed_by=...)`` is the only way this package writes
knowledge. The HTTP API stays read-only (ADR-0048); ``plugins.knowledge.cli`` wraps this API.

Rules, all fail closed with ``KnowledgeWriteError`` (a ``KnowledgeProviderError``):

- the item is validated as a ``KnowledgeItem`` exactly as ``LocalKnowledgeProvider`` loads it
  (JSON round trip, unknown fields refused), and must carry a non-blank ``source`` and ``license``;
- every add names a non-blank human reviewer. ``KnowledgeItem`` has no "extracted by an LLM" field
  (and forbids extra fields), so the store cannot tell a hand-written item from an LLM-extracted
  one; it therefore requires a reviewer for **every** add (knowledge-base.md: LLM-extracted items
  need human review before they become searchable);
- the directory as it stands must load cleanly (``LocalKnowledgeProvider`` rules);
- ``(name, version)`` already present with **different** content is refused (a new claim needs a new
  version); the identical item is an idempotent no-op (nothing is written, the review record of the
  first add stays).

Layout (in ``items_dir``, next to the hand-curated seed files)::

    item-<name>-<version>.json     a one-item list, the format LocalKnowledgeProvider loads
    item-<name>-<version>.review   write-once review record (not matched by the ``*.json`` glob)

Both files are written append-only: temporary file, ``fsync``, ``os.link`` (never replaces an
existing file), directory ``fsync``. The review record is written first, so a crash can leave a
review without its item (the next identical add reuses it) but never a searchable unreviewed item.
The review record binds the reviewer to the item's ``content_hash`` and the item file's SHA-256;
``review_of`` re-verifies both.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final

from pydantic import ValidationError

from core.contracts.knowledge import KnowledgeProviderError
from core.domain.research import KnowledgeItem
from plugins.knowledge.local import load_items

__all__ = ["KnowledgeReview", "KnowledgeWriteError", "LocalKnowledgeStore", "WriteResult"]

REVIEW_FORMAT: Final = "hlens.knowledge_review"
REVIEW_SCHEMA_VERSION: Final = "1.0.0"


class KnowledgeWriteError(KnowledgeProviderError):
    """A knowledge item cannot be added (invalid, unreviewed, conflicting or corrupt store)."""


@dataclass(frozen=True, slots=True)
class KnowledgeReview:
    item: str
    item_hash: str
    file_sha256: str
    reviewed_by: str
    reviewed_at: str


@dataclass(frozen=True, slots=True)
class WriteResult:
    item: KnowledgeItem
    #: The file that holds the item (``None`` when an identical item already lives in a seed file).
    path: Path | None
    created: bool


def _validated(item: KnowledgeItem | Mapping[str, Any]) -> KnowledgeItem:
    raw = item.model_dump(mode="json") if isinstance(item, KnowledgeItem) else item
    if not isinstance(raw, Mapping):
        raise KnowledgeWriteError("a knowledge item must be a JSON object")
    try:
        validated = KnowledgeItem.model_validate_json(json.dumps(dict(raw)))
    except (ValidationError, TypeError, ValueError) as exc:
        raise KnowledgeWriteError(f"invalid knowledge item: {exc}") from None
    if not validated.source.strip() or not validated.license.strip():
        raise KnowledgeWriteError(f"{validated.name} has no source or licence")
    return validated


def _item_bytes(item: KnowledgeItem) -> bytes:
    text = json.dumps([item.model_dump(mode="json")], indent=2, ensure_ascii=False, sort_keys=True)
    return (text + "\n").encode("utf-8")


def _fsync_directory(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _write_once(target: Path, data: bytes) -> None:
    fd, tmp_name = tempfile.mkstemp(dir=target.parent, prefix=".tmp-")
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(tmp, target)
        except FileExistsError:
            raise KnowledgeWriteError(f"{target.name} already exists; not overwritten") from None
        _fsync_directory(target.parent)
    finally:
        tmp.unlink(missing_ok=True)


class LocalKnowledgeStore:
    """Append-only, reviewed writes into a ``LocalKnowledgeProvider`` items directory."""

    def __init__(self, items_dir: Path, *, clock: Callable[[], datetime] | None = None) -> None:
        self._dir = Path(items_dir)
        self._clock = clock or (lambda: datetime.now(UTC))

    @property
    def items_dir(self) -> Path:
        return self._dir

    def paths_for(self, name: str, version: str) -> tuple[Path, Path]:
        stem = f"item-{name}-{version}"
        return self._dir / f"{stem}.json", self._dir / f"{stem}.review"

    def check(self, item: KnowledgeItem | Mapping[str, Any], reviewed_by: str) -> KnowledgeItem:
        """Everything ``add`` checks, without writing. Returns the validated item."""
        if not isinstance(reviewed_by, str) or not reviewed_by.strip():
            raise KnowledgeWriteError("every knowledge item needs a named human reviewer")
        validated = _validated(item)
        existing = self._existing().get((validated.name, validated.version))
        if existing is not None and existing.content_hash() != validated.content_hash():
            raise KnowledgeWriteError(
                f"{validated.name}@{validated.version} already exists with different content; "
                "publish a new version instead"
            )
        return validated

    def add(self, item: KnowledgeItem | Mapping[str, Any], *, reviewed_by: str) -> WriteResult:
        validated = self.check(item, reviewed_by)
        key = (validated.name, validated.version)
        item_path, review_path = self.paths_for(*key)
        if key in self._existing():
            return WriteResult(
                item=validated, path=item_path if item_path.exists() else None, created=False
            )
        if not self._dir.is_dir():
            raise KnowledgeWriteError(f"{self._dir} is not a directory")
        if item_path.exists():
            raise KnowledgeWriteError(f"{item_path.name} already exists; not overwritten")
        data = _item_bytes(validated)
        if review_path.exists():
            # a crash left the review record of this exact item without its item file
            previous = self._read_review(review_path)
            if previous.item_hash != validated.content_hash() or previous.file_sha256 != (
                hashlib.sha256(data).hexdigest()
            ):
                raise KnowledgeWriteError(f"{review_path.name} reviews a different item")
        else:
            review = {
                "format": REVIEW_FORMAT,
                "schema_version": REVIEW_SCHEMA_VERSION,
                "item": f"{validated.name}@{validated.version}",
                "item_hash": validated.content_hash(),
                "file_sha256": hashlib.sha256(data).hexdigest(),
                "reviewed_by": reviewed_by.strip(),
                "reviewed_at": self._clock().astimezone(UTC).isoformat(),
            }
            _write_once(review_path, (json.dumps(review, sort_keys=True, indent=2) + "\n").encode())
        _write_once(item_path, data)
        return WriteResult(item=validated, path=item_path, created=True)

    def review_of(self, name: str, version: str) -> KnowledgeReview:
        """The verified review record of an item added through this store."""
        item_path, review_path = self.paths_for(name, version)
        review = self._read_review(review_path)
        try:
            data = item_path.read_bytes()
        except OSError as exc:
            raise KnowledgeWriteError(f"{item_path.name}: unreadable: {exc}") from None
        if hashlib.sha256(data).hexdigest() != review.file_sha256:
            raise KnowledgeWriteError(f"{item_path.name} changed after its review")
        items = load_items((item_path,))
        if len(items) != 1:
            raise KnowledgeWriteError(f"{item_path.name} must hold exactly one item")
        item = items[0]
        if (
            (item.name, item.version) != (name, version)
            or item.content_hash() != review.item_hash
            or review.item != f"{name}@{version}"
        ):
            raise KnowledgeWriteError(f"{review_path.name} does not review {item_path.name}")
        return review

    def _existing(self) -> dict[tuple[str, str], KnowledgeItem]:
        if not self._dir.exists():
            return {}
        items = load_items(sorted(self._dir.glob("*.json")))
        return {(item.name, item.version): item for item in items}

    @staticmethod
    def _read_review(path: Path) -> KnowledgeReview:
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise KnowledgeWriteError(f"{path.name}: unreadable review record: {exc}") from None
        expected = {
            "format",
            "schema_version",
            "item",
            "item_hash",
            "file_sha256",
            "reviewed_by",
            "reviewed_at",
        }
        if (
            not isinstance(raw, dict)
            or set(raw) != expected
            or raw["format"] != REVIEW_FORMAT
            or raw["schema_version"] != REVIEW_SCHEMA_VERSION
            or not all(isinstance(raw[key], str) and raw[key].strip() for key in expected)
        ):
            raise KnowledgeWriteError(f"{path.name}: not a knowledge review record")
        return KnowledgeReview(
            item=raw["item"],
            item_hash=raw["item_hash"],
            file_sha256=raw["file_sha256"],
            reviewed_by=raw["reviewed_by"],
            reviewed_at=raw["reviewed_at"],
        )

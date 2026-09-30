"""Private content-addressed key-range tree for bounded point lookup and copy-on-write state."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Generator, Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final

from core.contracts.storage import ObjectRef, StageRequest, StorageAdapter
from core.domain.base import canonical_json

_FORMAT: Final = "hlens.content-key-tree@1.0.0"
_PREFIX: Final = "research/content-key-tree/v1/"


class ContentKeyTreeError(ValueError):
    """A key tree cannot be encoded within its explicit bounds or fails integrity checks."""


@dataclass(frozen=True, slots=True)
class KeyTreeParams:
    """Explicit encoded page and shape limits; callers choose values after capacity measurement."""

    page_max_bytes: int
    leaf_max_records: int
    fanout: int

    def __post_init__(self) -> None:
        if (
            isinstance(self.page_max_bytes, bool)
            or not isinstance(self.page_max_bytes, int)
            or self.page_max_bytes <= 0
        ):
            raise ContentKeyTreeError("page_max_bytes must be a positive integer")
        if (
            isinstance(self.leaf_max_records, bool)
            or not isinstance(self.leaf_max_records, int)
            or self.leaf_max_records <= 0
        ):
            raise ContentKeyTreeError("leaf_max_records must be a positive integer")
        if isinstance(self.fanout, bool) or not isinstance(self.fanout, int) or self.fanout < 2:
            raise ContentKeyTreeError("fanout must be at least 2")


Key = tuple[str, ...]


@dataclass(frozen=True, slots=True)
class _NodeRef:
    object_ref: ObjectRef
    level: int
    min_key: Key
    max_key: Key
    count: int


@dataclass(frozen=True, slots=True)
class ContentKeyTreeRoot:
    """Durable root descriptor; `ObjectRef` alone omits the range/level needed for safe reopen."""

    key: str
    sha256: str
    size: int
    level: int
    min_key: Key
    max_key: Key
    count: int
    format: str = _FORMAT

    def to_dict(self) -> dict[str, Any]:
        return {
            "format": self.format,
            "key": self.key,
            "sha256": self.sha256,
            "size": self.size,
            "level": self.level,
            "min_key": list(self.min_key),
            "max_key": list(self.max_key),
            "count": self.count,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> ContentKeyTreeRoot:
        try:
            if set(value) != {
                "format",
                "key",
                "sha256",
                "size",
                "level",
                "min_key",
                "max_key",
                "count",
            }:
                raise ContentKeyTreeError("tree root descriptor fields are not exact")
            result = cls(
                key=value["key"],
                sha256=value["sha256"],
                size=value["size"],
                level=value["level"],
                min_key=ContentKeyTree._key(value["min_key"]),
                max_key=ContentKeyTree._key(value["max_key"]),
                count=value["count"],
                format=value["format"],
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ContentKeyTreeError("tree root descriptor is malformed") from exc
        if (
            result.format != _FORMAT
            or not isinstance(result.key, str)
            or not isinstance(result.sha256, str)
            or not isinstance(result.size, int)
            or isinstance(result.size, bool)
            or result.size <= 0
            or not isinstance(result.level, int)
            or isinstance(result.level, bool)
            or result.level < 0
            or not isinstance(result.count, int)
            or isinstance(result.count, bool)
            or result.count <= 0
            or result.min_key > result.max_key
        ):
            raise ContentKeyTreeError("tree root descriptor fields are invalid")
        return result


class ContentKeyTree:
    """Immutable sorted key/value tree; ``put`` returns a copy-on-write root.

    Leaves and index pages are individually bounded by ``page_max_bytes``. Keys are tuples of
    strings so callers can use fixed-width hash prefixes where their domain permits it; payload
    identity remains in leaf values and must be checked by the caller.
    """

    def __init__(
        self,
        storage: StorageAdapter,
        params: KeyTreeParams,
        root: _NodeRef | None = None,
    ) -> None:
        self._storage = storage
        self._params = params
        self._root = root

    @property
    def root_ref(self) -> ContentKeyTreeRoot | None:
        root = self._root
        if root is None:
            return None
        return ContentKeyTreeRoot(
            key=root.object_ref.key,
            sha256=root.object_ref.sha256,
            size=root.object_ref.size,
            level=root.level,
            min_key=root.min_key,
            max_key=root.max_key,
            count=root.count,
        )

    @classmethod
    def open(
        cls,
        storage: StorageAdapter,
        params: KeyTreeParams,
        descriptor: ContentKeyTreeRoot | Mapping[str, Any],
    ) -> ContentKeyTree:
        """Reopen a persisted tree after validating descriptor against its root page."""
        root_info = (
            ContentKeyTreeRoot.from_dict(descriptor.to_dict())
            if isinstance(descriptor, ContentKeyTreeRoot)
            else ContentKeyTreeRoot.from_dict(descriptor)
        )
        looked_up = storage.lookup(root_info.key)
        if looked_up is None or (looked_up.sha256, looked_up.size) != (
            root_info.sha256,
            root_info.size,
        ):
            raise ContentKeyTreeError("tree root object is missing or differs from its descriptor")
        root = _NodeRef(
            looked_up,
            root_info.level,
            root_info.min_key,
            root_info.max_key,
            root_info.count,
        )
        tree = cls(storage, params, root)
        tree._read_page(root)
        return tree

    @classmethod
    def build(
        cls,
        storage: StorageAdapter,
        rows: Iterable[tuple[Key, str]],
        *,
        params: KeyTreeParams,
    ) -> ContentKeyTree:
        tree = cls(storage, params)
        levels: list[list[_NodeRef]] = []
        pending: list[tuple[Key, str]] = []
        previous: Key | None = None
        for key, value in rows:
            tree._validate_entry(key, value)
            if previous is not None and key <= previous:
                raise ContentKeyTreeError("build input keys must be strictly increasing")
            candidate = [*pending, (key, value)]
            if pending and (
                len(candidate) > params.leaf_max_records
                or len(tree._encode_leaf(candidate)) > params.page_max_bytes
            ):
                tree._push(levels, tree._write_leaf(pending))
                pending = []
                candidate = [(key, value)]
            if len(tree._encode_leaf(candidate)) > params.page_max_bytes:
                raise ContentKeyTreeError("one leaf entry exceeds page_max_bytes")
            pending = candidate
            previous = key
        if pending:
            tree._push(levels, tree._write_leaf(pending))
        root = tree._finish_levels(levels)
        return cls(storage, params, root)

    def get(self, key: Key) -> str | None:
        self._validate_key(key)
        item = self.successor(key)
        return item[1] if item is not None and item[0] == key else None

    def successor(self, key: Key, *, inclusive: bool = True) -> tuple[Key, str] | None:
        """Return the least key at/after ``key`` (or strictly after when requested)."""
        self._validate_key(key)
        if self._root is None:
            return None
        rows = self.iter_from(key)
        try:
            for item in rows:
                if inclusive or item[0] != key:
                    return item
        finally:
            rows.close()
        return None

    def iter_from(self, key: Key | None = None) -> Generator[tuple[Key, str]]:
        """Yield in key order from a lower bound; closing the iterator releases parsed pages."""
        if key is not None:
            self._validate_key(key)
        root = self._root
        if root is None:
            return
        yield from self._iter_node(root, key)

    def put(self, key: Key, value: str) -> ContentKeyTree:
        """Insert or replace one value, returning a new root and preserving the old version."""
        self._validate_entry(key, value)
        if self._root is None:
            root = self._write_leaf([(key, value)])
            return ContentKeyTree(self._storage, self._params, root)
        replacements = self._put_node(self._root, key, value)
        if len(replacements) == 1:
            root = replacements[0]
        else:
            root = self._write_index(self._root.level + 1, replacements)
        return ContentKeyTree(self._storage, self._params, root)

    def _put_node(self, node: _NodeRef, key: Key, value: str) -> list[_NodeRef]:
        page = self._read_page(node)
        if page["node"] == "leaf":
            entries = [(tuple(item["key"]), item["value"]) for item in page["entries"]]
            index = 0
            while index < len(entries) and entries[index][0] < key:
                index += 1
            if index < len(entries) and entries[index][0] == key:
                entries[index] = (key, value)
            else:
                entries.insert(index, (key, value))
            return self._write_leaf_pages(entries)

        children = [self._child_ref(item, level=node.level - 1) for item in page["children"]]
        index = next(
            (i for i, child in enumerate(children) if child.max_key >= key), len(children) - 1
        )
        children[index : index + 1] = self._put_node(children[index], key, value)
        return self._write_index_pages(node.level, children)

    def _write_leaf_pages(self, entries: Sequence[tuple[Key, str]]) -> list[_NodeRef]:
        pages: list[list[tuple[Key, str]]] = []
        current: list[tuple[Key, str]] = []
        for entry in entries:
            candidate = [*current, entry]
            if current and (
                len(candidate) > self._params.leaf_max_records
                or len(self._encode_leaf(candidate)) > self._params.page_max_bytes
            ):
                pages.append(current)
                current = []
                candidate = [entry]
            if len(self._encode_leaf(candidate)) > self._params.page_max_bytes:
                raise ContentKeyTreeError("one leaf entry exceeds page_max_bytes")
            current = candidate
        if current:
            pages.append(current)
        return [self._write_leaf(page) for page in pages]

    def _write_index_pages(self, level: int, children: Sequence[_NodeRef]) -> list[_NodeRef]:
        groups: list[list[_NodeRef]] = []
        current: list[_NodeRef] = []
        for child in children:
            candidate = [*current, child]
            if current and (
                len(candidate) > self._params.fanout
                or len(self._encode_index(level, candidate)) > self._params.page_max_bytes
            ):
                groups.append(current)
                current = []
                candidate = [child]
            if len(self._encode_index(level, candidate)) > self._params.page_max_bytes:
                raise ContentKeyTreeError("one index child exceeds page_max_bytes")
            current = candidate
        if current:
            groups.append(current)
        return [self._write_index(level, group) for group in groups]

    def _push(self, levels: list[list[_NodeRef]], node: _NodeRef) -> None:
        while len(levels) <= node.level:
            levels.append([])
        bucket = levels[node.level]
        bucket.append(node)
        if len(bucket) >= self._params.fanout:
            group = list(bucket)
            bucket.clear()
            self._push(levels, self._write_index(node.level + 1, group))

    def _finish_levels(self, levels: list[list[_NodeRef]]) -> _NodeRef | None:
        while True:
            nonempty = [level for level, bucket in enumerate(levels) if bucket]
            count = sum(len(levels[level]) for level in nonempty)
            if count == 0:
                return None
            if count == 1:
                return levels[nonempty[0]][0]
            level = nonempty[0]
            group = list(levels[level])
            levels[level].clear()
            self._push(levels, self._write_index(level + 1, group))

    def _write_leaf(self, entries: Sequence[tuple[Key, str]]) -> _NodeRef:
        page = {
            "format": _FORMAT,
            "node": "leaf",
            "count": len(entries),
            "entries": [{"key": list(key), "value": value} for key, value in entries],
        }
        body = self._encode_page(page)
        return _NodeRef(
            self._publish(body),
            0,
            entries[0][0],
            entries[-1][0],
            len(entries),
        )

    def _write_index(self, level: int, children: Sequence[_NodeRef]) -> _NodeRef:
        page = {
            "format": _FORMAT,
            "node": "index",
            "level": level,
            "count": sum(child.count for child in children),
            "children": [
                {
                    "key_min": list(child.min_key),
                    "key_max": list(child.max_key),
                    "count": child.count,
                    "ref": {
                        "key": child.object_ref.key,
                        "sha256": child.object_ref.sha256,
                        "size": child.object_ref.size,
                    },
                }
                for child in children
            ],
        }
        body = self._encode_page(page)
        return _NodeRef(
            self._publish(body),
            level,
            children[0].min_key,
            children[-1].max_key,
            sum(child.count for child in children),
        )

    def _encode_leaf(self, entries: Sequence[tuple[Key, str]]) -> bytes:
        return self._encode_page(
            {
                "format": _FORMAT,
                "node": "leaf",
                "count": len(entries),
                "entries": [{"key": list(key), "value": value} for key, value in entries],
            }
        )

    def _encode_index(self, level: int, children: Sequence[_NodeRef]) -> bytes:
        return self._encode_page(
            {
                "format": _FORMAT,
                "node": "index",
                "level": level,
                "count": sum(child.count for child in children),
                "children": [
                    {
                        "key_min": list(child.min_key),
                        "key_max": list(child.max_key),
                        "count": child.count,
                        "ref": {
                            "key": child.object_ref.key,
                            "sha256": child.object_ref.sha256,
                            "size": child.object_ref.size,
                        },
                    }
                    for child in children
                ],
            }
        )

    def _encode_page(self, page: Mapping[str, Any]) -> bytes:
        if not _fits_json_bytes(page, self._params.page_max_bytes):
            raise ContentKeyTreeError("encoded tree page exceeds page_max_bytes")
        body = canonical_json(page).encode("utf-8")
        if len(body) > self._params.page_max_bytes:
            raise ContentKeyTreeError("encoded tree page exceeds page_max_bytes")
        return body

    def _publish(self, body: bytes) -> ObjectRef:
        digest = hashlib.sha256(body).hexdigest()
        key = f"{_PREFIX}{digest}.json"
        staged = self._storage.stage(
            StageRequest(key=key, expected_sha256=digest, expected_size=len(body)), [body]
        )
        return self._storage.publish(staged).ref

    def _read_page(self, node: _NodeRef) -> dict[str, Any]:
        looked_up = self._storage.lookup(node.object_ref.key)
        if looked_up is None or looked_up != node.object_ref:
            raise ContentKeyTreeError("tree page is missing or differs from its reference")
        with self._storage.open_read(looked_up) as handle:
            body = handle.read(self._params.page_max_bytes + 1)
        if len(body) > self._params.page_max_bytes:
            raise ContentKeyTreeError("tree page exceeds page_max_bytes")
        if hashlib.sha256(body).hexdigest() != looked_up.sha256 or len(body) != looked_up.size:
            raise ContentKeyTreeError("tree page bytes fail hash or size verification")
        try:
            page = json.loads(body, object_pairs_hook=_reject_duplicate_json_keys)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ContentKeyTreeError("tree page is not valid JSON") from exc
        if not isinstance(page, dict) or page.get("format") != _FORMAT:
            raise ContentKeyTreeError("tree page format is unrecognized")
        page_count = page.get("count")
        if (
            not isinstance(page_count, int)
            or isinstance(page_count, bool)
            or page_count != node.count
        ):
            raise ContentKeyTreeError("tree page count differs from its reference")
        if page.get("node") == "leaf":
            if set(page) != {"format", "node", "count", "entries"}:
                raise ContentKeyTreeError("leaf page fields are not exact")
            if node.level != 0:
                raise ContentKeyTreeError("leaf page appears above level zero")
            entries = page.get("entries")
            if (
                not isinstance(entries, list)
                or len(entries) != node.count
                or len(entries) > self._params.leaf_max_records
            ):
                raise ContentKeyTreeError("leaf entries differ from page count")
            keys = [self._entry(item)[0] for item in entries]
            if (
                keys != sorted(set(keys))
                or not keys
                or keys[0] != node.min_key
                or keys[-1] != node.max_key
            ):
                raise ContentKeyTreeError(
                    "leaf keys are unordered or outside their reference range"
                )
        elif page.get("node") == "index":
            if set(page) != {"format", "node", "level", "count", "children"}:
                raise ContentKeyTreeError("index page fields are not exact")
            page_level = page.get("level")
            if (
                not isinstance(page_level, int)
                or isinstance(page_level, bool)
                or page_level != node.level
                or node.level <= 0
            ):
                raise ContentKeyTreeError("index level differs from its reference")
            children = page.get("children")
            if (
                not isinstance(children, list)
                or not children
                or len(children) > self._params.fanout
            ):
                raise ContentKeyTreeError("index child count exceeds its shape")
            parsed = [self._child_ref(item, level=node.level - 1) for item in children]
            if (
                parsed != sorted(parsed, key=lambda child: child.min_key)
                or parsed[0].min_key != node.min_key
                or parsed[-1].max_key != node.max_key
                or sum(child.count for child in parsed) != node.count
                or any(child.level != node.level - 1 for child in parsed)
                or any(
                    left.max_key >= right.min_key
                    for left, right in zip(parsed, parsed[1:], strict=False)
                )
            ):
                raise ContentKeyTreeError("index child ranges are inconsistent")
        else:
            raise ContentKeyTreeError("tree page node type is unrecognized")
        return page

    def _iter_node(self, node: _NodeRef, start: Key | None) -> Generator[tuple[Key, str]]:
        page = self._read_page(node)
        if page["node"] == "leaf":
            for item in page["entries"]:
                key, value = self._entry(item)
                if start is None or key >= start:
                    yield key, value
            return
        for item in page["children"]:
            child = self._child_ref(item, level=node.level - 1)
            if start is None or child.max_key >= start:
                yield from self._iter_node(child, start)

    def _child_ref(self, item: Mapping[str, Any], *, level: int) -> _NodeRef:
        try:
            if set(item) != {"key_min", "key_max", "count", "ref"}:
                raise ContentKeyTreeError("index child fields are not exact")
            key_min = self._key(item["key_min"])
            key_max = self._key(item["key_max"])
            count = item["count"]
            object_data = item["ref"]
            if not isinstance(object_data, Mapping) or set(object_data) != {
                "key",
                "sha256",
                "size",
            }:
                raise ContentKeyTreeError("index child object fields are not exact")
            object_key = object_data["key"]
            object_sha256 = object_data["sha256"]
            object_size = object_data["size"]
        except (KeyError, TypeError, ValueError) as exc:
            raise ContentKeyTreeError("index child reference is malformed") from exc
        if (
            not isinstance(object_key, str)
            or not isinstance(object_sha256, str)
            or not isinstance(object_size, int)
            or isinstance(object_size, bool)
            or object_size <= 0
        ):
            raise ContentKeyTreeError("index child object reference is invalid")
        ref = self._storage.lookup(object_key)
        if ref is None or (ref.sha256, ref.size) != (object_sha256, object_size):
            raise ContentKeyTreeError("index child object is missing or differs from its reference")
        if not isinstance(count, int) or isinstance(count, bool) or count <= 0 or key_min > key_max:
            raise ContentKeyTreeError("index child range or count is invalid")
        return _NodeRef(ref, level, key_min, key_max, count)

    def _entry(self, item: Mapping[str, Any]) -> tuple[Key, str]:
        try:
            if set(item) != {"key", "value"}:
                raise ContentKeyTreeError("leaf entry fields are not exact")
            key = self._key(item["key"])
            value = item["value"]
        except (KeyError, TypeError, ValueError) as exc:
            raise ContentKeyTreeError("leaf entry is malformed") from exc
        if not isinstance(value, str):
            raise ContentKeyTreeError("leaf value must be a string")
        return key, value

    @staticmethod
    def _key(value: Any) -> Key:
        if (
            not isinstance(value, list)
            or not value
            or any(not isinstance(part, str) for part in value)
        ):
            raise ContentKeyTreeError("tree key must be a non-empty string tuple")
        return tuple(value)

    @staticmethod
    def _validate_key(key: Key) -> None:
        if not isinstance(key, tuple) or not key or any(not isinstance(part, str) for part in key):
            raise ContentKeyTreeError("tree key must be a non-empty string tuple")

    @classmethod
    def _validate_entry(cls, key: Key, value: str) -> None:
        cls._validate_key(key)
        if not isinstance(value, str):
            raise ContentKeyTreeError("tree value must be a string")


def _fits_json_bytes(value: Any, max_bytes: int) -> bool:
    """Preflight canonical JSON size without constructing escaped copies of input strings."""
    budget = max_bytes

    def take(count: int) -> bool:
        nonlocal budget
        budget -= count
        return budget >= 0

    def string_size(text: str) -> bool:
        if not take(2):
            return False
        short_escapes = {'"': 2, "\\": 2, "\b": 2, "\f": 2, "\n": 2, "\r": 2, "\t": 2}
        for char in text:
            codepoint = ord(char)
            if codepoint in (0xD800, 0xDFFF) or 0xD800 <= codepoint <= 0xDFFF:
                return False
            if char in short_escapes:
                length = short_escapes[char]
            elif codepoint < 0x20:
                length = 6
            else:
                length = len(char.encode("utf-8"))
            if not take(length):
                return False
        return True

    def visit(item: Any) -> bool:
        if isinstance(item, str):
            return string_size(item)
        if item is None:
            return take(4)
        if item is True:
            return take(4)
        if item is False:
            return take(5)
        if isinstance(item, int):
            return take(len(str(item)))
        if isinstance(item, list | tuple):
            if not take(2):
                return False
            for index, child in enumerate(item):
                if index and not take(1):
                    return False
                if not visit(child):
                    return False
            return True
        if isinstance(item, Mapping):
            if not take(2):
                return False
            for index, key in enumerate(sorted(item)):
                if not isinstance(key, str):
                    return False
                if index and not take(1):
                    return False
                if not string_size(key) or not take(1) or not visit(item[key]):
                    return False
            return True
        return False

    return visit(value)


def _reject_duplicate_json_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ContentKeyTreeError("tree page contains a duplicate JSON object key")
        result[key] = value
    return result

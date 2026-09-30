"""Persistent, bounded ordered observation prefixes for Listing history replay.

Each append copies only a root-to-leaf path. Published nodes are immutable and content addressed;
unchanged subtrees are shared by every later Raw snapshot prefix. Prefix root references are
spilled through RunSetBuilder rather than retained in a Python collection.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Generator, Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Final

from core.contracts.storage import StageRequest, StorageAdapter
from core.domain.base import canonical_json
from infrastructure.revision.exchange_info_store import ExchangeInfoRowVerifier
from infrastructure.streaming.runs import RunLimits, RunRef, RunSetBuilder, iter_run

__all__ = [
    "LISTING_PREFIX_INDEX_FORMAT",
    "ListingPrefixIndex",
    "ListingPrefixIndexError",
    "ListingPrefixIndexStats",
    "build_listing_prefix_index",
]

LISTING_PREFIX_INDEX_FORMAT: Final = "hlens.listing.observation-prefix-index@1.0.0"
_KEY_PREFIX: Final = "quality/listing-prefix-index/v1/"
_CURRENT_ROOT: Final = object()


class ListingPrefixIndexError(ValueError):
    """A prefix index input, node or traversal violates its bounded format."""


@dataclass(frozen=True, slots=True)
class _NodeRef:
    key: str
    sha256: str
    size: int
    first_key: tuple[str, datetime, str]


@dataclass(frozen=True, slots=True)
class ListingPrefixIndexStats:
    raw_record_count: int
    observation_count: int
    root_ref_count: int
    node_write_count: int
    node_write_bytes: int
    max_tree_depth: int


class ListingPrefixIndex:
    """A content-addressed immutable B+ tree with explicit leaf/node limits."""

    def __init__(
        self,
        storage: StorageAdapter,
        *,
        leaf_max_records: int,
        fanout: int,
        max_node_bytes: int,
        max_record_bytes: int,
    ) -> None:
        for name, value, minimum in (
            ("leaf_max_records", leaf_max_records, 1),
            ("fanout", fanout, 2),
            ("max_node_bytes", max_node_bytes, 1),
            ("max_record_bytes", max_record_bytes, 1),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
                raise ListingPrefixIndexError(f"{name} must be an integer >= {minimum}")
        if max_node_bytes <= 512:
            raise ListingPrefixIndexError("max_node_bytes must exceed the 512-byte header reserve")
        if leaf_max_records * max_record_bytes + 512 > max_node_bytes:
            raise ListingPrefixIndexError(
                "max_node_bytes must fit the declared leaf record and byte limits"
            )
        self._storage = storage
        self._leaf_max_records = leaf_max_records
        self._fanout = fanout
        self._max_node_bytes = max_node_bytes
        self._max_record_bytes = max_record_bytes
        self._root: _NodeRef | None = None
        self._node_write_count = 0
        self._node_write_bytes = 0
        self._observation_count = 0
        self._raw_record_count = 0
        self._root_ref_count = 0
        self._max_tree_depth = 0

    @property
    def root(self) -> _NodeRef | None:
        return self._root

    @property
    def stats(self) -> ListingPrefixIndexStats:
        return ListingPrefixIndexStats(
            raw_record_count=self._raw_record_count,
            observation_count=self._observation_count,
            root_ref_count=self._root_ref_count,
            node_write_count=self._node_write_count,
            node_write_bytes=self._node_write_bytes,
            max_tree_depth=self._max_tree_depth,
        )

    def insert(self, observation: Mapping[str, Any]) -> None:
        normalized = dict(observation)
        symbol = normalized.get("venue_symbol")
        revision_id = normalized.get("snapshot_revision_id")
        retrieved_at = normalized.get("retrieved_at")
        if (
            not isinstance(symbol, str)
            or not symbol
            or not isinstance(revision_id, str)
            or not revision_id
        ):
            raise ListingPrefixIndexError("observation key text fields must be non-empty")
        if not isinstance(retrieved_at, datetime):
            raise ListingPrefixIndexError("retrieved_at must be a datetime")
        for field in ("requested_at", "retrieved_at", "raw_knowledge_time"):
            value = normalized.get(field)
            if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
                raise ListingPrefixIndexError(f"{field} must be timezone-aware datetime")
            offset = value.utcoffset()
            if offset is None or offset.total_seconds() != 0:
                raise ListingPrefixIndexError(f"{field} must be UTC")
            normalized[field] = value.astimezone(UTC).isoformat()
        try:
            ExchangeInfoRowVerifier._check_bounded_row(normalized, self._max_record_bytes)
        except Exception as exc:
            raise ListingPrefixIndexError("observation exceeds its record bound") from exc
        key = (symbol, retrieved_at.astimezone(UTC), revision_id)
        if self._root is None:
            self._root = self._write_leaf([self._entry(key, normalized)])
        else:
            left, right = self._insert(self._root, self._entry(key, normalized), depth=1)
            if right is None:
                self._root = left
            else:
                self._root = self._write_internal([left, right])
        self._observation_count += 1
        self._max_tree_depth = max(self._max_tree_depth, self._depth(self._root))

    def iter_rows(
        self, root: _NodeRef | None | object = _CURRENT_ROOT
    ) -> Generator[Mapping[str, Any]]:
        """Read one exact prefix in key order, validating every node and leaf entry."""
        selected = self._root if root is _CURRENT_ROOT else root
        if selected is None:
            return
        if not isinstance(selected, _NodeRef):
            raise ListingPrefixIndexError("prefix root ref has an invalid runtime type")
        stack: list[_NodeRef] = [selected]
        previous: tuple[str, datetime, str] | None = None
        while stack:
            ref = stack.pop()
            document = self._read_node(ref)
            if document["kind"] == "internal":
                children = document["children"]
                for child in reversed(children):
                    stack.append(self._parse_ref(child))
                continue
            for item in document["entries"]:
                key = self._key_from_json(item["key"])
                if previous is not None and key <= previous:
                    raise ListingPrefixIndexError("prefix tree leaves are not strictly ordered")
                previous = key
                row = dict(item["row"])
                for field in ("requested_at", "retrieved_at", "raw_knowledge_time"):
                    row[field] = datetime.fromisoformat(row[field])
                yield row

    def iter_rows_for_root_document(
        self, root: Mapping[str, Any] | None
    ) -> Generator[Mapping[str, Any]]:
        """Read a prefix named by the serialized root value in a prefix-reference run."""
        selected = None if root is None else self._parse_ref(root)
        yield from self.iter_rows(selected)

    def lookup(
        self,
        venue_symbol: str,
        retrieved_at: datetime,
        snapshot_revision_id: str,
    ) -> Mapping[str, Any] | None:
        """Look up one exact observation key through the validated B+ tree path."""
        if not isinstance(venue_symbol, str) or not venue_symbol:
            raise ListingPrefixIndexError("lookup venue_symbol must be non-empty text")
        if not isinstance(snapshot_revision_id, str) or not snapshot_revision_id:
            raise ListingPrefixIndexError("lookup revision ID must be non-empty text")
        if not isinstance(retrieved_at, datetime) or retrieved_at.tzinfo is None:
            raise ListingPrefixIndexError("lookup retrieved_at must be timezone-aware")
        offset = retrieved_at.utcoffset()
        if offset is None or offset.total_seconds() != 0:
            raise ListingPrefixIndexError("lookup retrieved_at must be UTC")
        key = (venue_symbol, retrieved_at.astimezone(UTC), snapshot_revision_id)
        ref = self._root
        while ref is not None:
            document = self._read_node(ref)
            if document["kind"] == "internal":
                children = [self._parse_ref(child) for child in document["children"]]
                child_index = 0
                for index in range(1, len(children)):
                    if children[index].first_key > key:
                        break
                    child_index = index
                ref = children[child_index]
                continue
            entries = document["entries"]
            low, high = 0, len(entries)
            while low < high:
                middle = (low + high) // 2
                entry_key = self._key_from_json(entries[middle]["key"])
                if entry_key < key:
                    low = middle + 1
                else:
                    high = middle
            if low < len(entries) and self._key_from_json(entries[low]["key"]) == key:
                return dict(entries[low]["row"])
            return None
        return None

    def _insert(
        self, ref: _NodeRef, entry: Mapping[str, Any], *, depth: int
    ) -> tuple[_NodeRef, _NodeRef | None]:
        document = self._read_node(ref)
        if document["kind"] == "leaf":
            entries = list(document["entries"])
            key = self._key_from_json(entry["key"])
            lo, hi = 0, len(entries)
            while lo < hi:
                mid = (lo + hi) // 2
                if self._key_from_json(entries[mid]["key"]) < key:
                    lo = mid + 1
                else:
                    hi = mid
            if lo < len(entries) and self._key_from_json(entries[lo]["key"]) == key:
                raise ListingPrefixIndexError("duplicate observation key")
            entries.insert(lo, dict(entry))
            left, right = self._split_leaf_if_needed(entries)
            return left, right

        children = [self._parse_ref(child) for child in document["children"]]
        key = self._key_from_json(entry["key"])
        child_index = 0
        for index in range(1, len(children)):
            if children[index].first_key > key:
                break
            child_index = index
        updated, split = self._insert(children[child_index], entry, depth=depth + 1)
        children[child_index] = updated
        if split is not None:
            children.insert(child_index + 1, split)
        if len(children) <= self._fanout and self._fits_internal(children):
            return self._write_internal(children), None
        left_children, right_children = self._split_internal(children)
        return self._write_internal(left_children), self._write_internal(right_children)

    def _split_internal(self, children: list[_NodeRef]) -> tuple[list[_NodeRef], list[_NodeRef]]:
        best: tuple[int, int, int] | None = None
        prefix_sizes = [0]
        for child in children:
            ref_document = self._ref_json(child)
            prefix_sizes.append(prefix_sizes[-1] + self._encoded_item_size(ref_document))
        empty_size = self._encoded_size(self._internal_doc([]))
        for split_at in range(1, len(children)):
            left_count, right_count = split_at, len(children) - split_at
            if (
                left_count <= self._fanout
                and right_count <= self._fanout
                and self._collection_range_size(empty_size, prefix_sizes, 0, split_at)
                <= self._max_node_bytes
                and self._collection_range_size(empty_size, prefix_sizes, split_at, len(children))
                <= self._max_node_bytes
            ):
                left_size = self._collection_range_size(empty_size, prefix_sizes, 0, split_at)
                right_size = self._collection_range_size(
                    empty_size, prefix_sizes, split_at, len(children)
                )
                score = (max(left_size, right_size), abs(left_count - right_count), split_at)
                if best is None or score < best:
                    best = score
        if best is None:
            raise ListingPrefixIndexError("no byte- and fanout-valid internal split exists")
        return children[: best[2]], children[best[2] :]

    def _split_leaf_if_needed(
        self, entries: list[Mapping[str, Any]]
    ) -> tuple[_NodeRef, _NodeRef | None]:
        if len(entries) <= self._leaf_max_records and self._fits_leaf(entries):
            return self._write_leaf(entries), None
        best: tuple[int, int, int] | None = None
        prefix_sizes = [0]
        for entry in entries:
            prefix_sizes.append(prefix_sizes[-1] + self._encoded_item_size(entry))
        empty_size = self._encoded_size(self._leaf_doc([]))
        for split_at in range(1, len(entries)):
            left_count, right_count = split_at, len(entries) - split_at
            if (
                left_count <= self._leaf_max_records
                and right_count <= self._leaf_max_records
                and self._collection_range_size(empty_size, prefix_sizes, 0, split_at)
                <= self._max_node_bytes
                and self._collection_range_size(empty_size, prefix_sizes, split_at, len(entries))
                <= self._max_node_bytes
            ):
                left_size = self._collection_range_size(empty_size, prefix_sizes, 0, split_at)
                right_size = self._collection_range_size(
                    empty_size, prefix_sizes, split_at, len(entries)
                )
                score = (max(left_size, right_size), abs(left_count - right_count), split_at)
                if best is None or score < best:
                    best = score
        if best is None:
            raise ListingPrefixIndexError("no byte- and record-valid leaf split exists")
        left, right = entries[: best[2]], entries[best[2] :]
        return self._write_leaf(left), self._write_leaf(right)

    def _fits_leaf(self, entries: list[Mapping[str, Any]]) -> bool:
        return self._encoded_size(self._leaf_doc(entries)) <= self._max_node_bytes

    @staticmethod
    def _leaf_doc(entries: list[Mapping[str, Any]]) -> dict[str, Any]:
        return {"format": LISTING_PREFIX_INDEX_FORMAT, "kind": "leaf", "entries": entries}

    def _fits_internal(self, children: list[_NodeRef]) -> bool:
        return self._encoded_size(self._internal_doc(children)) <= self._max_node_bytes

    def _internal_doc(self, children: list[_NodeRef]) -> dict[str, Any]:
        return {
            "format": LISTING_PREFIX_INDEX_FORMAT,
            "kind": "internal",
            "children": [self._ref_json(child) for child in children],
        }

    def _write_leaf(self, entries: list[Mapping[str, Any]]) -> _NodeRef:
        if len(entries) > self._leaf_max_records:
            raise ListingPrefixIndexError("leaf record limit exceeded")
        doc = {"format": LISTING_PREFIX_INDEX_FORMAT, "kind": "leaf", "entries": entries}
        return self._publish(doc, self._key_from_json(entries[0]["key"]))

    def _write_internal(self, children: list[_NodeRef]) -> _NodeRef:
        if not 1 <= len(children) <= self._fanout:
            raise ListingPrefixIndexError("internal node fanout limit exceeded")
        doc = {
            "format": LISTING_PREFIX_INDEX_FORMAT,
            "kind": "internal",
            "children": [self._ref_json(child) for child in children],
        }
        return self._publish(doc, children[0].first_key)

    def _publish(
        self, document: Mapping[str, Any], first_key: tuple[str, datetime, str]
    ) -> _NodeRef:
        try:
            ExchangeInfoRowVerifier._check_bounded_row(document, self._max_node_bytes)
        except Exception as exc:
            raise ListingPrefixIndexError("index node exceeds max_node_bytes") from exc
        body = canonical_json(document).encode("utf-8")
        if len(body) > self._max_node_bytes:
            raise ListingPrefixIndexError("index node exceeds max_node_bytes")
        digest = hashlib.sha256(body).hexdigest()
        key = f"{_KEY_PREFIX}{digest}.json"
        staged = self._storage.stage(
            StageRequest(key=key, expected_sha256=digest, expected_size=len(body)), [body]
        )
        published = self._storage.publish(staged)
        self._node_write_count += 1
        self._node_write_bytes += len(body)
        return _NodeRef(published.ref.key, published.ref.sha256, published.ref.size, first_key)

    def _read_node(self, ref: _NodeRef) -> Mapping[str, Any]:
        if (
            not ref.key.startswith(_KEY_PREFIX)
            or ref.key != f"{_KEY_PREFIX}{ref.sha256}.json"
            or len(ref.sha256) != 64
            or any(character not in "0123456789abcdef" for character in ref.sha256)
            or ref.size <= 0
            or ref.size > self._max_node_bytes
        ):
            raise ListingPrefixIndexError("listing prefix node ref is malformed or out of bounds")
        found = self._storage.lookup(ref.key)
        if found is None or (found.key, found.sha256, found.size) != (
            ref.key,
            ref.sha256,
            ref.size,
        ):
            raise ListingPrefixIndexError("listing prefix node is missing or has a mismatched ref")
        with self._storage.open_read(found) as handle:
            body = handle.read(self._max_node_bytes + 1)
        if len(body) > self._max_node_bytes or hashlib.sha256(body).hexdigest() != ref.sha256:
            raise ListingPrefixIndexError("listing prefix node bytes exceed bounds or are corrupt")
        try:
            document = json.loads(body)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ListingPrefixIndexError("listing prefix node is malformed JSON") from exc
        if not isinstance(document, dict) or document.get("format") != LISTING_PREFIX_INDEX_FORMAT:
            raise ListingPrefixIndexError("listing prefix node has an invalid format")
        if canonical_json(document).encode("utf-8") != body:
            raise ListingPrefixIndexError("listing prefix node is not canonical JSON")
        kind = document.get("kind")
        if kind == "leaf":
            entries = document.get("entries")
            if not isinstance(entries, list) or not 1 <= len(entries) <= self._leaf_max_records:
                raise ListingPrefixIndexError("listing prefix leaf has an invalid record count")
            prior = None
            for entry in entries:
                if not isinstance(entry, dict) or set(entry) != {"key", "row"}:
                    raise ListingPrefixIndexError("listing prefix leaf entry is malformed")
                current = self._key_from_json(entry["key"])
                if prior is not None and current <= prior:
                    raise ListingPrefixIndexError("listing prefix leaf is not strictly ordered")
                prior = current
            if self._key_from_json(entries[0]["key"]) != ref.first_key:
                raise ListingPrefixIndexError("listing prefix leaf first key mismatches its ref")
        elif kind == "internal":
            children = document.get("children")
            if not isinstance(children, list) or not 1 <= len(children) <= self._fanout:
                raise ListingPrefixIndexError("listing prefix internal node has invalid fanout")
            refs = [self._parse_ref(item) for item in children]
            if refs[0].first_key != ref.first_key or any(
                refs[index - 1].first_key >= refs[index].first_key for index in range(1, len(refs))
            ):
                raise ListingPrefixIndexError("listing prefix child ranges are not ordered")
        else:
            raise ListingPrefixIndexError("listing prefix node has an unknown kind")
        return document

    @staticmethod
    def _key_from_json(value: object) -> tuple[str, datetime, str]:
        if (
            not isinstance(value, list)
            or len(value) != 3
            or not isinstance(value[0], str)
            or not isinstance(value[1], str)
            or not isinstance(value[2], str)
        ):
            raise ListingPrefixIndexError("listing prefix entry key is malformed")
        try:
            instant = datetime.fromisoformat(value[1])
        except ValueError as exc:
            raise ListingPrefixIndexError("listing prefix key time is malformed") from exc
        offset = instant.utcoffset()
        if instant.tzinfo is None or offset is None or offset.total_seconds() != 0:
            raise ListingPrefixIndexError("listing prefix key time must be UTC")
        return (value[0], instant, value[2])

    @staticmethod
    def _entry(key: tuple[str, datetime, str], row: Mapping[str, Any]) -> dict[str, Any]:
        return {
            "key": [key[0], key[1].isoformat(), key[2]],
            "row": dict(row),
        }

    @staticmethod
    def _ref_json(ref: _NodeRef) -> dict[str, Any]:
        return {
            "key": ref.key,
            "sha256": ref.sha256,
            "size": ref.size,
            "first_key": [ref.first_key[0], ref.first_key[1].isoformat(), ref.first_key[2]],
        }

    @classmethod
    def _parse_ref(cls, value: object) -> _NodeRef:
        if not isinstance(value, dict):
            raise ListingPrefixIndexError("listing prefix child ref is malformed")
        key = value.get("key")
        digest = value.get("sha256")
        size = value.get("size")
        first = cls._key_from_json(value.get("first_key"))
        if (
            not isinstance(key, str)
            or not key.startswith(_KEY_PREFIX)
            or not key.endswith(".json")
            or not isinstance(digest, str)
            or len(digest) != 64
            or any(character not in "0123456789abcdef" for character in digest)
            or key != f"{_KEY_PREFIX}{digest}.json"
        ):
            raise ListingPrefixIndexError("listing prefix child ref identity is malformed")
        if isinstance(size, bool) or not isinstance(size, int) or size <= 0:
            raise ListingPrefixIndexError("listing prefix child ref size is malformed")
        return _NodeRef(key, digest, size, first)

    def _depth(self, root: _NodeRef | None) -> int:
        if root is None:
            return 0
        depth = 0
        current = root
        while True:
            depth += 1
            node = self._read_node(current)
            if node["kind"] == "leaf":
                return depth
            current = self._parse_ref(node["children"][0])

    def _encoded_size(self, document: Mapping[str, Any]) -> int:
        try:
            ExchangeInfoRowVerifier._check_bounded_row(document, self._max_node_bytes)
        except Exception:
            return self._max_node_bytes + 1
        return len(canonical_json(document).encode("utf-8"))

    def _encoded_item_size(self, item: Mapping[str, Any]) -> int:
        try:
            ExchangeInfoRowVerifier._check_bounded_row(item, self._max_node_bytes)
        except Exception as exc:
            raise ListingPrefixIndexError("split item exceeds max_node_bytes") from exc
        return len(canonical_json(item).encode("utf-8"))

    @staticmethod
    def _collection_range_size(
        empty_document_bytes: int, prefix_sizes: list[int], start: int, end: int
    ) -> int:
        count = end - start
        return empty_document_bytes + prefix_sizes[end] - prefix_sizes[start] + max(0, count - 1)


def build_listing_prefix_index(
    raw_rows: RunRef,
    *,
    storage: StorageAdapter,
    raw_sort_capacity: int,
    raw_sort_merge_fanout: int,
    raw_sort_limits: RunLimits,
    raw_max_record_bytes: int,
    raw_max_run_object_bytes: int,
    root_refs_capacity: int,
    root_refs_merge_fanout: int,
    root_refs_limits: RunLimits,
    leaf_max_records: int,
    fanout: int,
    max_node_bytes: int,
    max_record_bytes: int,
) -> tuple[ListingPrefixIndex, RunRef]:
    """Build one persistent observation tree per Raw snapshot ordinal in a single Raw-run pass.

    Prefix roots are stored in a bounded RunSet keyed by ordinal; only the current root is kept in
    memory. The returned index can replay any raw prefix using the root row's ``snapshot_ordinal``.
    """
    index = ListingPrefixIndex(
        storage,
        leaf_max_records=leaf_max_records,
        fanout=fanout,
        max_node_bytes=max_node_bytes,
        max_record_bytes=max_record_bytes,
    )
    for name, value, minimum in (
        ("raw_sort_capacity", raw_sort_capacity, 1),
        ("raw_sort_merge_fanout", raw_sort_merge_fanout, 2),
        ("raw_max_record_bytes", raw_max_record_bytes, 1),
        ("raw_max_run_object_bytes", raw_max_run_object_bytes, 1),
    ):
        if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
            raise ListingPrefixIndexError(f"{name} must be an integer >= {minimum}")
    if not isinstance(raw_sort_limits, RunLimits):
        raise ListingPrefixIndexError("raw_sort_limits must be RunLimits")

    def raw_key(row: Mapping[str, Any]) -> int:
        ordinal = row.get("snapshot_ordinal")
        if isinstance(ordinal, bool) or not isinstance(ordinal, int) or ordinal < 0:
            raise ListingPrefixIndexError("Raw proof has invalid snapshot ordinal")
        return ordinal

    ordered = RunSetBuilder(
        storage,
        key=raw_key,
        capacity=raw_sort_capacity,
        merge_fanout=raw_sort_merge_fanout,
        limits=raw_sort_limits,
    )
    roots = RunSetBuilder(
        storage,
        key=lambda row: row["snapshot_ordinal"],
        capacity=root_refs_capacity,
        merge_fanout=root_refs_merge_fanout,
        limits=root_refs_limits,
    )
    snapshot_ids = RunSetBuilder(
        storage,
        key=lambda row: row["snapshot_id"],
        capacity=raw_sort_capacity,
        merge_fanout=raw_sort_merge_fanout,
        limits=raw_sort_limits,
    )
    with (
        ordered,
        snapshot_ids,
        iter_run(storage, raw_rows, max_object_bytes=raw_max_run_object_bytes) as raw_reader,
    ):
        for item in raw_reader:
            try:
                ExchangeInfoRowVerifier._check_bounded_row(item, raw_max_record_bytes)
            except Exception as exc:
                raise ListingPrefixIndexError(
                    "Raw proof row exceeds its explicit byte limit"
                ) from exc
            ordered.add(item)
        ordered_ref = ordered.finish()
        if ordered_ref is None:
            raise ListingPrefixIndexError("Raw proof run is empty")
        with iter_run(
            storage, ordered_ref, max_object_bytes=raw_max_run_object_bytes
        ) as ordered_reader:
            return _build_index_in_ordinal_order(index, ordered_reader, roots, snapshot_ids)


def _build_index_in_ordinal_order(
    index: ListingPrefixIndex,
    ordered_reader: Iterable[Mapping[str, Any]],
    roots: RunSetBuilder,
    snapshot_ids: RunSetBuilder,
) -> tuple[ListingPrefixIndex, RunRef]:
    with roots, snapshot_ids:
        expected_ordinal = 0
        raw_knowledge_floor: datetime | None = None
        for item in ordered_reader:
            ordinal = item["snapshot_ordinal"]
            if ordinal != expected_ordinal:
                raise ListingPrefixIndexError(
                    "Raw proof snapshot ordinals are missing or reordered"
                )
            snapshot_id = item.get("snapshot_id")
            if not isinstance(snapshot_id, str) or not snapshot_id:
                raise ListingPrefixIndexError("Raw proof row has an invalid snapshot id")
            snapshot_ids.add({"snapshot_id": snapshot_id, "snapshot_ordinal": ordinal})
            row = item["row"]
            knowledge_time = row.get("knowledge_time")
            if not isinstance(knowledge_time, datetime):
                raise ListingPrefixIndexError("Raw proof row has invalid knowledge_time")
            raw_knowledge_floor = (
                knowledge_time
                if raw_knowledge_floor is None or knowledge_time > raw_knowledge_floor
                else raw_knowledge_floor
            )
            requested = row["requested_symbols"]
            present = {entry["symbol"]: entry for entry in row["symbols"]}
            for symbol in requested:
                entry = present.get(symbol)
                observation = {
                    "snapshot_id": snapshot_id,
                    "snapshot_ordinal": ordinal,
                    "venue_symbol": symbol,
                    "snapshot_revision_id": row["revision_id"],
                    "requested_at": row["requested_at"],
                    "retrieved_at": row["retrieved_at"],
                    "raw_knowledge_time": row["knowledge_time"],
                    "status": None if entry is None else entry["status"],
                    "base_asset": None if entry is None else entry["base_asset"],
                    "quote_asset": None if entry is None else entry["quote_asset"],
                }
                index.insert(observation)
            root = index.root
            roots.add(
                {
                    "snapshot_ordinal": ordinal,
                    "snapshot_id": snapshot_id,
                    "root": None if root is None else index._ref_json(root),
                    "observation_count": index.stats.observation_count,
                    "raw_knowledge_floor": raw_knowledge_floor,
                }
            )
            expected_ordinal += 1
        index._raw_record_count = expected_ordinal
        index._root_ref_count = expected_ordinal
        result = roots.finish()
        if result is None:
            raise ListingPrefixIndexError("a non-empty Raw proof run has no prefix roots")
        snapshot_id_run = snapshot_ids.finish()
        if snapshot_id_run is None:
            raise ListingPrefixIndexError("Raw proof run has no snapshot identifiers")
        with iter_run(index._storage, snapshot_id_run) as reader:
            previous_id: str | None = None
            for item in reader:
                current_id = item["snapshot_id"]
                if current_id == previous_id:
                    raise ListingPrefixIndexError("Raw proof repeats a snapshot id")
                previous_id = current_id
        return index, result

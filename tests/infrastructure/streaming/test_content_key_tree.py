"""Content-key tree paging, seek, copy-on-write and corruption checks."""

from __future__ import annotations

import hashlib
import io
import json
from pathlib import Path
from typing import Any, BinaryIO

import pytest

from core.contracts.storage import ObjectRef, StageRequest, StorageAdapter
from core.domain.base import canonical_json
from infrastructure.streaming.content_key_tree import (
    ContentKeyTree,
    ContentKeyTreeError,
    ContentKeyTreeRoot,
    KeyTreeParams,
    _fits_json_bytes,
)
from tests.infrastructure.revision import rest_store_support as storage_support


def _params() -> KeyTreeParams:
    return KeyTreeParams(page_max_bytes=1024, leaf_max_records=2, fanout=2)


class _TrackingStorage:
    def __init__(self, storage: StorageAdapter) -> None:
        self._storage = storage
        self.opened = 0
        self.closed = 0
        self.corrupt_key: str | None = None

    def __getattr__(self, name: str) -> Any:
        return getattr(self._storage, name)

    def lookup(self, key: str) -> ObjectRef | None:
        return self._storage.lookup(key)

    def open_read(self, ref: ObjectRef) -> Any:
        self.opened += 1
        handle = self._storage.open_read(ref)
        if ref.key == self.corrupt_key:
            body = handle.read()
            handle.close()
            return io.BytesIO(body[:-1] + bytes([body[-1] ^ 1]))
        return _CountingHandle(handle, self)


class _CountingHandle:
    def __init__(self, handle: Any, storage: _TrackingStorage) -> None:
        self._handle: BinaryIO = handle
        self._storage = storage
        self._closed = False

    def __enter__(self) -> _CountingHandle:
        return self

    def __exit__(self, *_args: Any) -> None:
        self.close()

    def read(self, size: int = -1) -> bytes:
        return self._handle.read(size)

    def close(self) -> None:
        if not self._closed:
            self._closed = True
            self._storage.closed += 1
            self._handle.close()


def test_multilevel_tree_seek_iteration_and_copy_on_write(tmp_path: Path) -> None:
    with storage_support.sqlite_harness(tmp_path) as harness:
        storage = _TrackingStorage(harness.storage)
        entries = [((f"key-{index:04d}",), f"value-{index}") for index in range(128)]
        tree = ContentKeyTree.build(storage, entries, params=_params())
        descriptor = tree.root_ref
        assert descriptor is not None and descriptor.level >= 2

        reopened = ContentKeyTree.open(
            storage, _params(), ContentKeyTreeRoot.from_dict(descriptor.to_dict())
        )
        assert reopened.get(("key-0064",)) == "value-64"
        assert reopened.get(("missing",)) is None
        assert reopened.successor(("key-0064",), inclusive=False) == (
            ("key-0065",),
            "value-65",
        )
        assert reopened.successor(("key-0064x",)) == (("key-0065",), "value-65")
        rows = reopened.iter_from(("key-0120",))
        try:
            assert list(rows) == entries[120:]
        finally:
            rows.close()

        updated = reopened.put(("key-0064",), "changed")
        inserted = updated.put(("key-0064a",), "new")
        assert reopened.get(("key-0064",)) == "value-64"
        assert updated.get(("key-0064",)) == "changed"
        assert inserted.get(("key-0064a",)) == "new"
        assert inserted.root_ref is not None


def test_strict_successor_keeps_empty_tuple_component(tmp_path: Path) -> None:
    with storage_support.sqlite_harness(tmp_path) as harness:
        tree = ContentKeyTree.build(
            harness.storage,
            [(("a",), "short"), (("a", ""), "empty"), (("a", "b"), "child")],
            params=_params(),
        )
        assert tree.successor(("a",), inclusive=False) == (("a", ""), "empty")


def test_seek_reads_only_a_root_to_leaf_path_and_closes_before_yield(
    tmp_path: Path,
) -> None:
    with storage_support.sqlite_harness(tmp_path) as harness:
        storage = _TrackingStorage(harness.storage)
        entries = [((f"key-{index:05d}",), str(index)) for index in range(1024)]
        tree = ContentKeyTree.build(storage, entries, params=_params())
        descriptor = tree.root_ref
        assert descriptor is not None
        before = storage.opened
        rows = tree.iter_from(("key-00900",))
        assert next(rows) == (("key-00900",), "900")
        opened = storage.opened - before
        assert opened <= descriptor.level + 1
        assert storage.opened == storage.closed
        rows.close()
        assert storage.opened == storage.closed


def test_corrupt_root_page_fails_closed_on_open(tmp_path: Path) -> None:
    with storage_support.sqlite_harness(tmp_path) as harness:
        storage = _TrackingStorage(harness.storage)
        tree = ContentKeyTree.build(
            storage,
            [(("a",), "one"), (("b",), "two"), (("c",), "three")],
            params=_params(),
        )
        descriptor = tree.root_ref
        assert descriptor is not None
        storage.corrupt_key = descriptor.key
        with pytest.raises(ContentKeyTreeError, match="hash or size verification"):
            ContentKeyTree.open(storage, _params(), descriptor)


@pytest.mark.parametrize(
    "body",
    [
        b'{"format":"hlens.content-key-tree@1.0.0","node":"leaf","count":1,'
        b'"entries":[{"key":["a"],"value":"v"}],"extra":true}',
        b'{"format":"hlens.content-key-tree@1.0.0","node":"leaf","count":1}',
        b'{"format":"hlens.content-key-tree@1.0.0","node":"leaf","count":1,'
        b'"entries":[{"key":["a"],"value":"v","extra":true}]}',
        b'{"format":"hlens.content-key-tree@1.0.0","node":"leaf","count":1,'
        b'"count":1,"entries":[{"key":["a"],"value":"v"}]}',
    ],
    ids=["unknown-page-field", "missing-page-field", "unknown-entry-field", "duplicate-json-key"],
)
def test_valid_digest_does_not_accept_malformed_page_schema(tmp_path: Path, body: bytes) -> None:
    with storage_support.sqlite_harness(tmp_path) as harness:
        digest = hashlib.sha256(body).hexdigest()
        key = f"research/content-key-tree/v1/{digest}.json"
        staged = harness.storage.stage(
            StageRequest(key=key, expected_sha256=digest, expected_size=len(body)), [body]
        )
        ref = harness.storage.publish(staged).ref
        descriptor = ContentKeyTreeRoot(
            key=ref.key,
            sha256=ref.sha256,
            size=ref.size,
            level=0,
            min_key=("a",),
            max_key=("a",),
            count=1,
        )
        with pytest.raises(ContentKeyTreeError):
            ContentKeyTree.open(harness.storage, _params(), descriptor)


@pytest.mark.parametrize("mutate", ["unknown", "missing"], ids=["unknown", "missing"])
def test_index_page_requires_exact_versioned_fields(tmp_path: Path, mutate: str) -> None:
    with storage_support.sqlite_harness(tmp_path) as harness:
        tree = ContentKeyTree.build(
            harness.storage,
            [((f"key-{index}",), str(index)) for index in range(8)],
            params=_params(),
        )
        original = tree.root_ref
        assert original is not None and original.level > 0
        ref = harness.storage.lookup(original.key)
        assert ref is not None
        with harness.storage.open_read(ref) as handle:
            page = json.loads(handle.read())
        if mutate == "unknown":
            page["future_field"] = "must reject"
        else:
            del page["children"]
        body = canonical_json(page).encode("utf-8")
        digest = hashlib.sha256(body).hexdigest()
        key = f"research/content-key-tree/v1/{digest}.json"
        staged = harness.storage.stage(
            StageRequest(key=key, expected_sha256=digest, expected_size=len(body)), [body]
        )
        malformed_ref = harness.storage.publish(staged).ref
        descriptor = ContentKeyTreeRoot(
            key=malformed_ref.key,
            sha256=malformed_ref.sha256,
            size=malformed_ref.size,
            level=original.level,
            min_key=original.min_key,
            max_key=original.max_key,
            count=original.count,
        )
        with pytest.raises(ContentKeyTreeError, match="index page fields"):
            ContentKeyTree.open(harness.storage, _params(), descriptor)


@pytest.mark.parametrize("mutate", ["unknown", "missing"], ids=["unknown", "missing"])
def test_root_descriptor_requires_exact_fields(tmp_path: Path, mutate: str) -> None:
    with storage_support.sqlite_harness(tmp_path) as harness:
        tree = ContentKeyTree.build(harness.storage, [(("a",), "one")], params=_params())
        descriptor = tree.root_ref
        assert descriptor is not None
        value = descriptor.to_dict()
        if mutate == "unknown":
            value["future_field"] = "must reject"
        else:
            del value["count"]
        with pytest.raises(ContentKeyTreeError, match="descriptor"):
            ContentKeyTree.open(harness.storage, _params(), value)


def test_json_byte_preflight_matches_canonical_unicode_and_controls() -> None:
    page = {
        "control": '\u0000\b\f\n\r\t"\\',
        "format": "hlens.content-key-tree@1.0.0",
        "unicode": "é漢🙂",
    }
    encoded_size = len(canonical_json(page).encode("utf-8"))
    assert _fits_json_bytes(page, encoded_size)
    assert not _fits_json_bytes(page, encoded_size - 1)
    assert not _fits_json_bytes({"invalid": "\ud800"}, 100)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("page_max_bytes", True),
        ("page_max_bytes", 1024.0),
        ("leaf_max_records", False),
        ("leaf_max_records", 2.0),
        ("fanout", True),
        ("fanout", 2.0),
    ],
)
def test_key_tree_capacity_parameters_are_strict_integers(field: str, value: Any) -> None:
    values: dict[str, Any] = {"page_max_bytes": 1024, "leaf_max_records": 2, "fanout": 2}
    values[field] = value
    with pytest.raises(ContentKeyTreeError):
        KeyTreeParams(**values)


def test_oversized_leaf_value_fails_before_json_materialization(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with storage_support.sqlite_harness(tmp_path) as harness:

        def unexpected_json(_page: Any) -> str:
            raise AssertionError("oversized input reached full JSON serialization")

        monkeypatch.setattr(
            "infrastructure.streaming.content_key_tree.canonical_json", unexpected_json
        )
        with pytest.raises(ContentKeyTreeError, match="exceeds page_max_bytes"):
            ContentKeyTree.build(
                harness.storage,
                [(("large",), "x" * 1_000_000)],
                params=_params(),
            )

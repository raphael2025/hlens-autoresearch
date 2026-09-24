"""Implementation tests for ``LocalFileStorageAdapter`` (Phase 1 C1).

Covers path escape / symlink containment, streaming writes, checksum fail-closed cleanup,
idempotent replay, conflicts, tamper detection, syscall fault injection, and concurrent
publish of the same key. Crash coverage is via fsync/link failure injection and
visibility boundaries — not a real power loss.
"""

from __future__ import annotations

import hashlib
import os
import threading
from collections.abc import Iterator
from pathlib import Path

import pytest

from core.contracts.storage import (
    IntegrityViolation,
    ObjectConflict,
    ObjectKeyViolation,
    ObjectNotFound,
    ObjectRef,
    PublishOutcome,
    StageRequest,
)
from infrastructure.settings import Settings
from infrastructure.storage import LocalFileStorageAdapter
from infrastructure.storage.local import _PAYLOAD_NAME, _write_all

VALID_CATALOG = "postgresql://hlens:fake-password@127.0.0.1:5432/hlens_catalog"


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _chunks(data: bytes, size: int = 8) -> Iterator[bytes]:
    yield b""
    for start in range(0, len(data), size):
        yield data[start : start + size]


def _adapter(tmp_path: Path, *, chunk_size: int = 64) -> LocalFileStorageAdapter:
    warehouse = tmp_path / "warehouse"
    staging = warehouse / "staging"
    warehouse.mkdir()
    staging.mkdir()
    return LocalFileStorageAdapter(
        warehouse.as_uri(),
        staging.as_uri(),
        chunk_size=chunk_size,
    )


def _put(storage: LocalFileStorageAdapter, key: str, data: bytes) -> ObjectRef:
    staged = storage.stage(
        StageRequest(key=key, expected_sha256=_sha(data), expected_size=len(data)),
        _chunks(data),
    )
    return storage.publish(staged).ref


def test_from_settings_uses_file_uris(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    warehouse = (tmp_path / "wh").resolve()
    staging = (warehouse / "staging").resolve()
    warehouse.mkdir()
    staging.mkdir()
    monkeypatch.setenv("HLENS_CATALOG_URI", VALID_CATALOG)
    monkeypatch.setenv("HLENS_WAREHOUSE_URI", warehouse.as_uri())
    monkeypatch.setenv("HLENS_STAGING_URI", staging.as_uri())
    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    storage = LocalFileStorageAdapter.from_settings(settings)
    ref = _put(storage, "settings/ok.bin", b"via-settings")
    assert ref.uri.startswith("file:///")
    assert storage.lookup("settings/ok.bin") == ref


def test_nested_legal_keys_round_trip(tmp_path: Path) -> None:
    storage = _adapter(tmp_path)
    key = "raw/archives/BTCUSDT/2024-01-01/part=0/archive.zip"
    data = b"nested-payload" * 50
    ref = _put(storage, key, data)
    assert storage.lookup(key) == ref
    with storage.open_read(ref) as handle:
        assert handle.read() == data
    assert not (tmp_path / "warehouse" / "staging").joinpath(ref.key).exists()


@pytest.mark.parametrize(
    "key",
    [
        "../escape.bin",
        "a/../../escape.bin",
        "staging",
        "staging/escape.bin",
        "staging/nested/x.bin",
    ],
)
def test_path_escape_and_staging_keys_rejected(tmp_path: Path, key: str) -> None:
    storage = _adapter(tmp_path)
    with pytest.raises(ObjectKeyViolation):
        storage.lookup(key)
    with pytest.raises(ObjectKeyViolation):
        storage.stage(
            StageRequest.model_construct(key=key, expected_sha256=_sha(b"x"), expected_size=None),
            _chunks(b"x"),
        )


def test_symlink_escape_is_rejected(tmp_path: Path) -> None:
    storage = _adapter(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    secret = outside / "secret.bin"
    secret.write_bytes(b"secret")
    link = tmp_path / "warehouse" / "trap"
    link.symlink_to(outside, target_is_directory=True)
    with pytest.raises(ObjectKeyViolation):
        storage.stage(
            StageRequest(
                key="trap/secret.bin",
                expected_sha256=_sha(b"secret"),
                expected_size=6,
            ),
            _chunks(b"secret"),
        )
    with pytest.raises(ObjectKeyViolation):
        storage.lookup("trap/secret.bin")
    assert list(outside.iterdir()) == [secret]
    warehouse_files = [p for p in (tmp_path / "warehouse").rglob("*") if p.is_file()]
    assert not any(p.name == "secret.bin" for p in warehouse_files)


def test_checksum_mismatch_cleans_temp_and_stays_invisible(tmp_path: Path) -> None:
    storage = _adapter(tmp_path)
    key = "raw/bad-checksum.bin"
    data = b"actual-bytes"
    staging_root = tmp_path / "warehouse" / "staging"
    before = set(staging_root.iterdir())
    with pytest.raises(IntegrityViolation):
        storage.stage(
            StageRequest(key=key, expected_sha256=_sha(b"other"), expected_size=None),
            _chunks(data),
        )
    assert set(staging_root.iterdir()) == before
    assert storage.lookup(key) is None
    assert not (tmp_path / "warehouse" / key).exists()


def test_size_mismatch_cleans_temp(tmp_path: Path) -> None:
    storage = _adapter(tmp_path)
    key = "raw/bad-size.bin"
    data = b"twelve-bytes"
    with pytest.raises(IntegrityViolation):
        storage.stage(
            StageRequest(key=key, expected_sha256=_sha(data), expected_size=len(data) + 1),
            _chunks(data),
        )
    assert storage.lookup(key) is None
    assert list((tmp_path / "warehouse" / "staging").iterdir()) == []


def test_same_content_replay_is_idempotent(tmp_path: Path) -> None:
    storage = _adapter(tmp_path)
    key = "raw/idempotent.bin"
    data = b"same-bytes"
    first = _put(storage, key, data)
    staged = storage.stage(
        StageRequest(key=key, expected_sha256=_sha(data), expected_size=len(data)),
        _chunks(data),
    )
    second = storage.publish(staged)
    assert second.outcome is PublishOutcome.ALREADY_PRESENT
    assert second.ref == first
    restarted = LocalFileStorageAdapter(
        (tmp_path / "warehouse").as_uri(),
        (tmp_path / "warehouse" / "staging").as_uri(),
    )
    third = restarted.publish(staged)
    assert third.outcome is PublishOutcome.ALREADY_PRESENT
    assert third.ref == first


def test_different_content_conflict_preserves_original(tmp_path: Path) -> None:
    storage = _adapter(tmp_path)
    key = "raw/conflict.bin"
    original = b"original"
    ref = _put(storage, key, original)
    staged = storage.stage(
        StageRequest(key=key, expected_sha256=_sha(b"other"), expected_size=5),
        _chunks(b"other"),
    )
    with pytest.raises(ObjectConflict):
        storage.publish(staged)
    assert storage.lookup(key) == ref
    with storage.open_read(ref) as handle:
        assert handle.read() == original


def test_read_missing_and_tampered_object(tmp_path: Path) -> None:
    storage = _adapter(tmp_path)
    key = "raw/read.bin"
    data = b"intact"
    ref = _put(storage, key, data)
    missing = ref.model_copy(update={"key": "raw/missing.bin"})
    with pytest.raises(ObjectNotFound):
        storage.open_read(missing)
    path = tmp_path / "warehouse" / key
    path.write_bytes(b"tampered")
    with pytest.raises(IntegrityViolation):
        storage.open_read(ref)
    assert storage.lookup(key) is not None
    assert storage.lookup(key).sha256 == _sha(b"tampered")  # type: ignore[union-attr]


def test_streaming_write_never_joins_full_input(tmp_path: Path) -> None:
    storage = _adapter(tmp_path, chunk_size=16)
    data = os.urandom(100_000)
    passes = {"n": 0}
    max_held = {"n": 0}

    def limited_chunks() -> Iterator[bytes]:
        passes["n"] += 1
        assert passes["n"] == 1
        held = 0
        for start in range(0, len(data), 16):
            piece = data[start : start + 16]
            held += len(piece)
            max_held["n"] = max(max_held["n"], held)
            # Emulate a bounded reader: only one chunk is live at a time.
            yield piece
            held -= len(piece)

    staged = storage.stage(
        StageRequest(key="raw/stream.bin", expected_sha256=_sha(data), expected_size=len(data)),
        limited_chunks(),
    )
    assert staged.size == len(data)
    assert passes["n"] == 1
    assert max_held["n"] <= 16
    # Re-read published object to prove bytes survived the chunked path.
    ref = storage.publish(staged).ref
    with storage.open_read(ref) as handle:
        assert handle.read() == data


def test_fsync_failure_during_stage_leaves_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    storage = _adapter(tmp_path)
    real_fsync = os.fsync
    calls = {"n": 0}

    def flaky_fsync(fd: int) -> None:
        calls["n"] += 1
        if calls["n"] == 1:
            raise OSError("injected fsync failure")
        real_fsync(fd)

    monkeypatch.setattr(os, "fsync", flaky_fsync)
    with pytest.raises(OSError, match="injected fsync failure"):
        storage.stage(
            StageRequest(key="raw/fsync.bin", expected_sha256=_sha(b"x"), expected_size=1),
            _chunks(b"x"),
        )
    assert storage.lookup("raw/fsync.bin") is None
    assert list((tmp_path / "warehouse" / "staging").iterdir()) == []
    assert not (tmp_path / "warehouse" / "raw" / "fsync.bin").exists()


def test_link_failure_leaves_no_partial_object(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    storage = _adapter(tmp_path)
    key = "raw/link-fail.bin"
    data = b"payload"
    staged = storage.stage(
        StageRequest(key=key, expected_sha256=_sha(data), expected_size=len(data)),
        _chunks(data),
    )

    def boom(
        src: str | bytes | os.PathLike[str],
        dst: str | bytes | os.PathLike[str],
        *,
        src_dir_fd: int | None = None,
        dst_dir_fd: int | None = None,
        follow_symlinks: bool = True,
    ) -> None:
        raise OSError("injected link failure")

    monkeypatch.setattr(os, "link", boom)
    with pytest.raises(OSError, match="injected link failure"):
        storage.publish(staged)
    assert storage.lookup(key) is None
    dest = tmp_path / "warehouse" / key
    assert not dest.exists()
    # staging credential remains for retry after the injected fault
    assert (tmp_path / "warehouse" / "staging" / staged.staging_id / _PAYLOAD_NAME).is_file()


def test_concurrent_publish_same_key_converges(tmp_path: Path) -> None:
    warehouse = tmp_path / "warehouse"
    staging = warehouse / "staging"
    warehouse.mkdir()
    staging.mkdir()
    key = "raw/race.bin"
    data = b"race-payload"
    barriers = threading.Barrier(2)
    results: list[PublishOutcome | type[BaseException]] = []
    errors: list[BaseException] = []
    lock = threading.Lock()

    def worker() -> None:
        storage = LocalFileStorageAdapter(warehouse.as_uri(), staging.as_uri())
        staged = storage.stage(
            StageRequest(key=key, expected_sha256=_sha(data), expected_size=len(data)),
            _chunks(data),
        )
        barriers.wait(timeout=5)
        try:
            outcome = storage.publish(staged).outcome
            with lock:
                results.append(outcome)
        except BaseException as exc:  # noqa: BLE001 — collect for assertion
            with lock:
                errors.append(exc)
                results.append(type(exc))

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)
        assert not thread.is_alive()
    assert not errors
    assert sorted(results, key=lambda item: getattr(item, "value", str(item))) in (
        [PublishOutcome.ALREADY_PRESENT, PublishOutcome.CREATED],
        [PublishOutcome.CREATED, PublishOutcome.ALREADY_PRESENT],
    )
    storage = LocalFileStorageAdapter(warehouse.as_uri(), staging.as_uri())
    ref = storage.lookup(key)
    assert ref is not None
    with storage.open_read(ref) as handle:
        assert handle.read() == data
    # Final directory must not contain half-written names for this key.
    published = list((tmp_path / "warehouse" / "raw").iterdir())
    assert [p.name for p in published] == ["race.bin"]


def test_concurrent_conflicting_content_one_wins(tmp_path: Path) -> None:
    warehouse = tmp_path / "warehouse"
    staging = warehouse / "staging"
    warehouse.mkdir()
    staging.mkdir()
    key = "raw/conflict-race.bin"
    barrier = threading.Barrier(2)
    outcomes: list[str] = []
    lock = threading.Lock()

    def worker(payload: bytes) -> None:
        storage = LocalFileStorageAdapter(warehouse.as_uri(), staging.as_uri())
        staged = storage.stage(
            StageRequest(key=key, expected_sha256=_sha(payload), expected_size=len(payload)),
            _chunks(payload),
        )
        barrier.wait(timeout=5)
        try:
            result = storage.publish(staged)
            with lock:
                outcomes.append(f"ok:{result.outcome.value}:{result.ref.sha256}")
        except ObjectConflict:
            with lock:
                outcomes.append("conflict")

    threads = [
        threading.Thread(target=worker, args=(b"aaa",)),
        threading.Thread(target=worker, args=(b"bbb",)),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)
    assert len(outcomes) == 2
    assert sum(1 for item in outcomes if item.startswith("ok:created:")) == 1
    assert "conflict" in outcomes
    # With different content, the loser must be conflict (not silent overwrite).
    storage = LocalFileStorageAdapter(warehouse.as_uri(), staging.as_uri())
    ref = storage.lookup(key)
    assert ref is not None
    with storage.open_read(ref) as handle:
        body = handle.read()
    assert body in {b"aaa", b"bbb"}
    assert not any(p.name.endswith(".partial") for p in (tmp_path / "warehouse" / "raw").iterdir())


def test_failed_stage_leaves_no_half_file_in_final_tree(tmp_path: Path) -> None:
    storage = _adapter(tmp_path)
    key = "raw/deep/nested/object.bin"

    def boom() -> Iterator[bytes]:
        yield b"partial"
        raise RuntimeError("stream exploded")

    with pytest.raises(RuntimeError, match="stream exploded"):
        storage.stage(
            StageRequest(key=key, expected_sha256=_sha(b"partial"), expected_size=None),
            boom(),
        )
    warehouse = tmp_path / "warehouse"
    assert not (warehouse / "raw").exists() or list((warehouse / "raw").rglob("*")) == []
    assert list((warehouse / "staging").iterdir()) == []


# ---------------------------------------------------------------------------------------
# C1-R1：短写、路径 TOCTOU、相同根
# ---------------------------------------------------------------------------------------


def test_short_writes_are_completed_for_payload_and_meta(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    storage = _adapter(tmp_path)
    real_write = os.write
    calls = {"n": 0}

    def short_then_real(fd: int, data: bytes | bytearray | memoryview) -> int:
        calls["n"] += 1
        view = memoryview(data)
        if calls["n"] == 1 and len(view) > 3:
            return real_write(fd, view[:3])
        if calls["n"] == 2 and len(view) > 2:
            # metadata JSON is also subject to short writes
            return real_write(fd, view[:2])
        return real_write(fd, data)

    monkeypatch.setattr(os, "write", short_then_real)
    data = b"abcdefghij"
    key = "raw/short-write.bin"
    ref = _put(storage, key, data)
    assert ref.size == len(data)
    assert ref.sha256 == _sha(data)
    with storage.open_read(ref) as handle:
        assert handle.read() == data
    assert calls["n"] >= 2


def test_write_zero_fail_closed_and_cleans_staging(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    storage = _adapter(tmp_path)
    monkeypatch.setattr(os, "write", lambda fd, data: 0)
    with pytest.raises(OSError, match="os.write returned 0"):
        storage.stage(
            StageRequest(key="raw/zero-write.bin", expected_sha256=_sha(b"x"), expected_size=1),
            _chunks(b"x"),
        )
    assert storage.lookup("raw/zero-write.bin") is None
    assert list((tmp_path / "warehouse" / "staging").iterdir()) == []
    assert not (tmp_path / "warehouse" / "raw" / "zero-write.bin").exists()


def test_write_all_retries_interrupted_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_write = os.write
    state = {"interrupted": False}

    def flaky(fd: int, data: bytes | bytearray | memoryview) -> int:
        if not state["interrupted"]:
            state["interrupted"] = True
            raise InterruptedError
        return real_write(fd, data)

    monkeypatch.setattr(os, "write", flaky)
    target = tmp_path / "blob"
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    try:
        _write_all(fd, b"hello-interrupted")
    finally:
        os.close(fd)
    assert target.read_bytes() == b"hello-interrupted"
    assert state["interrupted"] is True


def test_publish_parent_replaced_with_external_symlink_does_not_escape(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    storage = _adapter(tmp_path)
    key = "raw/x.bin"
    data = b"anchored-payload"
    staged = storage.stage(
        StageRequest(key=key, expected_sha256=_sha(data), expected_size=len(data)),
        _chunks(data),
    )
    warehouse = tmp_path / "warehouse"
    external = tmp_path / "external"
    external.mkdir()
    real_link = os.link

    def racing_link(
        src: str | bytes | os.PathLike[str],
        dst: str | bytes | os.PathLike[str],
        *,
        src_dir_fd: int | None = None,
        dst_dir_fd: int | None = None,
        follow_symlinks: bool = True,
    ) -> None:
        raw = warehouse / "raw"
        saved = warehouse / "saved-raw"
        if raw.exists() and not raw.is_symlink():
            raw.rename(saved)
            raw.symlink_to(external, target_is_directory=True)
        real_link(
            src,
            dst,
            src_dir_fd=src_dir_fd,
            dst_dir_fd=dst_dir_fd,
            follow_symlinks=follow_symlinks,
        )

    monkeypatch.setattr(os, "link", racing_link)
    try:
        result = storage.publish(staged)
    except (ObjectKeyViolation, OSError, IntegrityViolation, ObjectConflict):
        # Fail-closed is acceptable; external must stay empty either way.
        assert list(external.iterdir()) == []
        assert not (external / "x.bin").exists()
        return
    assert list(external.iterdir()) == []
    assert not (external / "x.bin").exists()
    # Published into the original warehouse parent inode (now reachable as saved-raw).
    assert result.ref.sha256 == _sha(data)
    assert result.ref.size == len(data)
    saved_obj = warehouse / "saved-raw" / "x.bin"
    assert saved_obj.is_file()
    assert saved_obj.read_bytes() == data
    # Path walk after the rename must not follow the external symlink.
    with pytest.raises((ObjectKeyViolation, IntegrityViolation, ObjectNotFound)):
        storage.open_read(result.ref)


def test_open_read_same_fd_survives_path_replacement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    storage = _adapter(tmp_path)
    key = "raw/toc-tou.bin"
    original = b"verified-bytes"
    ref = _put(storage, key, original)
    target = tmp_path / "warehouse" / key
    real_open = os.open

    def racing_open(
        path: str | bytes | os.PathLike[str],
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        reading = (flags & os.O_ACCMODE) == os.O_RDONLY and not (flags & os.O_DIRECTORY)
        name = os.fsdecode(path)
        is_final = name == "toc-tou.bin" or name.endswith("/toc-tou.bin")
        if reading and is_final and dir_fd is None:
            # Old path-string reopen: replace before open so the handle sees evil.
            if target.exists() or target.is_symlink():
                target.unlink()
            target.write_bytes(b"evil")
            return real_open(path, flags, mode, dir_fd=dir_fd)
        fd = real_open(path, flags, mode, dir_fd=dir_fd)
        if reading and is_final and dir_fd is not None:
            # FD-anchored open: replace the path after the inode is already open.
            if target.exists() or target.is_symlink():
                target.unlink()
            target.write_bytes(b"evil")
        return fd

    monkeypatch.setattr(os, "open", racing_open)
    with storage.open_read(ref) as handle:
        body = handle.read()
    assert body == original
    assert body != b"evil"


def test_symlink_final_object_and_internal_symlink_parent_rejected(tmp_path: Path) -> None:
    storage = _adapter(tmp_path)
    key = "raw/sym-final.bin"
    other_key = "raw/other.bin"
    _put(storage, other_key, b"other")
    target = tmp_path / "warehouse" / key
    target.parent.mkdir(parents=True, exist_ok=True)
    target.symlink_to(tmp_path / "warehouse" / other_key)
    with pytest.raises((IntegrityViolation, ObjectKeyViolation)):
        storage.lookup(key)
    evil_ref = ObjectRef(
        key=key,
        uri=target.resolve().as_uri(),
        sha256=_sha(b"other"),
        size=5,
    )
    with pytest.raises((IntegrityViolation, ObjectKeyViolation, ObjectNotFound)):
        storage.open_read(evil_ref)

    outside = tmp_path / "outside-dir"
    outside.mkdir()
    (outside / "secret.bin").write_bytes(b"secret")
    link_parent = tmp_path / "warehouse" / "trapdir"
    link_parent.symlink_to(outside, target_is_directory=True)
    with pytest.raises(ObjectKeyViolation):
        storage.lookup("trapdir/secret.bin")
    with pytest.raises(ObjectKeyViolation):
        storage.stage(
            StageRequest(
                key="trapdir/secret.bin",
                expected_sha256=_sha(b"secret"),
                expected_size=6,
            ),
            _chunks(b"secret"),
        )


def test_identical_warehouse_and_staging_roots_rejected(tmp_path: Path) -> None:
    root = tmp_path / "same"
    root.mkdir()
    uri = root.as_uri()
    with pytest.raises(ValueError, match="distinct"):
        LocalFileStorageAdapter(uri, uri)

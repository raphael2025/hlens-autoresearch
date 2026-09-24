"""Local ``file://`` ``StorageAdapter`` (Phase 1 C1 / C1-R1 / C1-R2；ADR-0021 / B3 Protocol).

Warehouse 与 staging 必须是同一文件系统上的本地绝对 ``file://`` 根，且不得为同一路径。
构造时记录两个根目录的 inode；每次操作临时打开根 FD，用 ``fstat`` 核对仍绑定构造时的
inode，再以 ``dir_fd`` + ``O_NOFOLLOW`` 逐段访问，拒绝路径组件或最终对象上的 symlink。

写入使用可靠 write-all（处理短写、``InterruptedError``、返回 0）；哈希与 size 只对应确实
完整写入的内容。发布用同文件系统 ``os.link(..., src_dir_fd=..., dst_dir_fd=...,
follow_symlinks=False)``，不覆盖；link 成功后必须从**实际最终对象的同一个已打开 FD**
重新校验普通文件 / SHA-256 / size，并从配置的 warehouse 根安全重走 ``key``，确认与
本次 final FD 为同一 inode 且内容身份相同，才返回可用 ``ObjectRef``。任一步失败则用已
持有的父目录 FD 删除**本次**新建的 final（不碰既有对象）。``open_read`` / ``lookup``
对同一个已打开 FD 计算 SHA-256 / size。

根 FD 不跨操作持有；``close()`` / context manager 幂等，关闭后操作明确失败。
"""

from __future__ import annotations

import errno
import hashlib
import json
import os
import secrets
import stat
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import BinaryIO, Final, Self

from core.contracts.storage import (
    IntegrityViolation,
    ObjectConflict,
    ObjectKeyViolation,
    ObjectNotFound,
    ObjectRef,
    PublishOutcome,
    PublishResult,
    StagedObject,
    StageRequest,
    StagingViolation,
    StorageError,
    validate_object_key,
)
from infrastructure.settings import (
    Settings,
    filesystem_device_id,
    local_file_uri_to_path,
)

__all__ = ["DEFAULT_CHUNK_SIZE", "LocalFileStorageAdapter"]

DEFAULT_CHUNK_SIZE: Final[int] = 1024 * 1024
_META_NAME: Final[str] = "meta.json"
_PAYLOAD_NAME: Final[str] = "payload"
_STAGING_ID_BYTES: Final[int] = 16
_DIR_FLAGS: Final[int] = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
_ROOT_FLAGS: Final[int] = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
_FILE_READ_FLAGS: Final[int] = os.O_RDONLY | os.O_NOFOLLOW
_FILE_CREATE_FLAGS: Final[int] = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW


def _is_within(path: Path, root: Path) -> bool:
    """Return True iff ``path`` is ``root`` or a descendant (both must be resolved)."""
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _same_inode(st_a: os.stat_result, st_b: os.stat_result) -> bool:
    return st_a.st_dev == st_b.st_dev and st_a.st_ino == st_b.st_ino


def _write_all(fd: int, data: bytes | bytearray | memoryview) -> None:
    """Write every byte; handle short writes, ``InterruptedError``, and a zero return."""
    if not data:
        return
    view = memoryview(data)
    offset = 0
    total = len(view)
    while offset < total:
        try:
            written = os.write(fd, view[offset:])
        except InterruptedError:
            continue
        if written <= 0:
            msg = "os.write returned 0 before all bytes were written"
            raise OSError(errno.EIO, msg)
        offset += written


def _sha256_fd(fd: int, *, chunk_size: int) -> tuple[str, int]:
    hasher = hashlib.sha256()
    size = 0
    while True:
        chunk = os.read(fd, chunk_size)
        if not chunk:
            break
        hasher.update(chunk)
        size += len(chunk)
    return hasher.hexdigest(), size


def _close_quiet(fd: int | None) -> None:
    if fd is None:
        return
    try:
        os.close(fd)
    except OSError:
        return


class LocalFileStorageAdapter:
    """B3 ``StorageAdapter`` backed by a local ``file://`` warehouse + staging pair."""

    def __init__(
        self,
        warehouse_uri: str,
        staging_uri: str,
        *,
        chunk_size: int = DEFAULT_CHUNK_SIZE,
    ) -> None:
        if chunk_size <= 0:
            msg = "chunk_size must be positive"
            raise ValueError(msg)
        warehouse = local_file_uri_to_path(warehouse_uri, field_name="warehouse_uri")
        staging = local_file_uri_to_path(staging_uri, field_name="staging_uri")
        if filesystem_device_id(warehouse) != filesystem_device_id(staging):
            msg = "staging_uri and warehouse_uri must be on the same filesystem"
            raise ValueError(msg)
        warehouse.mkdir(parents=True, exist_ok=True)
        staging.mkdir(parents=True, exist_ok=True)
        self._warehouse = warehouse.resolve(strict=True)
        self._staging = staging.resolve(strict=True)
        if self._warehouse == self._staging:
            msg = "warehouse_uri and staging_uri must be distinct paths"
            raise ValueError(msg)
        if filesystem_device_id(self._warehouse) != filesystem_device_id(self._staging):
            msg = "staging_uri and warehouse_uri must be on the same filesystem"
            raise ValueError(msg)
        if _is_within(self._warehouse, self._staging):
            msg = "warehouse_uri must not be inside staging_uri"
            raise ValueError(msg)
        self._chunk_size = chunk_size
        self._warehouse_uri = self._warehouse.as_uri()
        self._staging_uri = self._staging.as_uri()
        self._closed = False
        # Capture root inodes without retaining FDs across the instance lifetime.
        warehouse_fd: int | None = None
        staging_fd: int | None = None
        try:
            warehouse_fd = os.open(self._warehouse, _ROOT_FLAGS)
            self._warehouse_stat = os.fstat(warehouse_fd)
            staging_fd = os.open(self._staging, _ROOT_FLAGS)
            self._staging_stat = os.fstat(staging_fd)
        except BaseException:
            _close_quiet(warehouse_fd)
            _close_quiet(staging_fd)
            raise
        else:
            _close_quiet(warehouse_fd)
            _close_quiet(staging_fd)

    @classmethod
    def from_settings(
        cls,
        settings: Settings,
        *,
        chunk_size: int = DEFAULT_CHUNK_SIZE,
    ) -> Self:
        return cls(
            settings.warehouse_uri,
            settings.staging_uri,
            chunk_size=chunk_size,
        )

    def close(self) -> None:
        """Idempotent explicit lifecycle end; subsequent operations fail closed."""
        self._closed = True

    def __enter__(self) -> Self:
        self._ensure_open()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    @property
    def warehouse_uri(self) -> str:
        return self._warehouse_uri

    @property
    def staging_uri(self) -> str:
        return self._staging_uri

    @property
    def closed(self) -> bool:
        return self._closed

    @property
    def forbidden_object_keys(self) -> tuple[str, ...]:
        """Syntax-legal keys that would resolve into the private staging area."""
        if not _is_within(self._staging, self._warehouse):
            return ()
        rel = self._staging.relative_to(self._warehouse).as_posix()
        return (rel, f"{rel}/escape.bin")

    def stage(self, request: StageRequest, content: Iterable[bytes]) -> StagedObject:
        key = self._validated_key(request.key)
        with self._open_roots() as (warehouse_fd, staging_fd):
            self._assert_key_not_staging(key, warehouse_fd)
            staging_id = secrets.token_hex(_STAGING_ID_BYTES)
            entry_fd: int | None = None
            try:
                os.mkdir(staging_id, 0o755, dir_fd=staging_fd)
                entry_fd = os.open(staging_id, _DIR_FLAGS, dir_fd=staging_fd)
                digest, size = self._stream_to_payload(entry_fd, content)
                if request.expected_size is not None and size != request.expected_size:
                    raise IntegrityViolation(
                        f"size mismatch for {key!r}: expected {request.expected_size}, got {size}"
                    )
                if digest != request.expected_sha256:
                    raise IntegrityViolation(
                        f"sha256 mismatch for {key!r}: "
                        f"expected {request.expected_sha256}, got {digest}"
                    )
                meta = {"key": key, "sha256": digest, "size": size}
                self._write_meta(entry_fd, meta)
                os.fsync(entry_fd)
                os.fsync(staging_fd)
            except BaseException:
                self._cleanup_staging_entry(staging_fd, staging_id, entry_fd)
                raise
            finally:
                _close_quiet(entry_fd)
            return StagedObject(key=key, sha256=digest, size=size, staging_id=staging_id)

    def publish(self, staged: StagedObject) -> PublishResult:
        key = self._validated_key(staged.key)
        with self._open_roots() as (warehouse_fd, staging_fd):
            self._assert_key_not_staging(key, warehouse_fd)
            entry_fd, payload_present, meta = self._load_issued(staging_fd, staged)
            try:
                if (meta["key"], meta["sha256"], meta["size"]) != (
                    key,
                    staged.sha256,
                    staged.size,
                ):
                    raise StagingViolation("staged object metadata does not match credential")

                if not payload_present:
                    existing = self._published_identity(warehouse_fd, key)
                    if existing is None:
                        raise StagingViolation("staged payload missing and target object absent")
                    existing_digest, existing_size = existing
                    if existing_digest != staged.sha256 or existing_size != staged.size:
                        raise ObjectConflict(key)
                    return PublishResult(
                        ref=self._usable_ref(warehouse_fd, key, existing_digest, existing_size),
                        outcome=PublishOutcome.ALREADY_PRESENT,
                    )

                payload_digest, payload_size = self._hash_named_regular(entry_fd, _PAYLOAD_NAME)
                if payload_digest != staged.sha256 or payload_size != staged.size:
                    raise StagingViolation("staged payload no longer matches issued identity")

                existing = self._published_identity(warehouse_fd, key)
                if existing is not None:
                    existing_digest, existing_size = existing
                    if existing_digest != staged.sha256 or existing_size != staged.size:
                        raise ObjectConflict(key)
                    self._unlink_in_dir(entry_fd, _PAYLOAD_NAME)
                    return PublishResult(
                        ref=self._usable_ref(warehouse_fd, key, existing_digest, existing_size),
                        outcome=PublishOutcome.ALREADY_PRESENT,
                    )

                parent_fd, final_name, owned_parent = self._open_publish_parent(warehouse_fd, key)
                created_final = False
                try:
                    try:
                        os.link(
                            _PAYLOAD_NAME,
                            final_name,
                            src_dir_fd=entry_fd,
                            dst_dir_fd=parent_fd,
                            follow_symlinks=False,
                        )
                    except FileExistsError:
                        existing = self._published_identity(warehouse_fd, key)
                        if existing is None:
                            raise ObjectConflict(key) from None
                        existing_digest, existing_size = existing
                        if existing_digest != staged.sha256 or existing_size != staged.size:
                            raise ObjectConflict(key) from None
                        self._unlink_in_dir(entry_fd, _PAYLOAD_NAME)
                        return PublishResult(
                            ref=self._usable_ref(warehouse_fd, key, existing_digest, existing_size),
                            outcome=PublishOutcome.ALREADY_PRESENT,
                        )

                    created_final = True
                    published_fd: int | None = None
                    try:
                        published_fd = os.open(final_name, _FILE_READ_FLAGS, dir_fd=parent_fd)
                        published_st = os.fstat(published_fd)
                        if not stat.S_ISREG(published_st.st_mode):
                            raise IntegrityViolation(f"{key} is not a regular file after publish")
                        os.lseek(published_fd, 0, os.SEEK_SET)
                        final_digest, final_size = _sha256_fd(
                            published_fd, chunk_size=self._chunk_size
                        )
                        if final_digest != staged.sha256 or final_size != staged.size:
                            raise IntegrityViolation(
                                f"published object identity does not match staging credential "
                                f"for {key!r}"
                            )
                        os.fsync(published_fd)
                        os.fsync(parent_fd)
                        # Re-bind configured warehouse root and confirm the key is reachable
                        # under it as the same inode with the same content identity.
                        self._assert_root_bound(warehouse_fd, self._warehouse_stat, "warehouse")
                        walk_fd = self._open_final_object_fd(warehouse_fd, key)
                        try:
                            walk_st = os.fstat(walk_fd)
                            if not _same_inode(walk_st, published_st):
                                raise IntegrityViolation(
                                    f"published object for {key!r} is not reachable under "
                                    "the configured warehouse root"
                                )
                            if not stat.S_ISREG(walk_st.st_mode):
                                raise IntegrityViolation(f"{key} is not a regular file")
                            os.lseek(walk_fd, 0, os.SEEK_SET)
                            walk_digest, walk_size = _sha256_fd(
                                walk_fd, chunk_size=self._chunk_size
                            )
                            if walk_digest != staged.sha256 or walk_size != staged.size:
                                raise IntegrityViolation(
                                    f"warehouse walk identity mismatch for {key!r}"
                                )
                        finally:
                            _close_quiet(walk_fd)
                    except BaseException:
                        if created_final:
                            self._unlink_created_final(parent_fd, final_name)
                        raise
                    finally:
                        _close_quiet(published_fd)
                finally:
                    if owned_parent:
                        _close_quiet(parent_fd)

                self._unlink_in_dir(entry_fd, _PAYLOAD_NAME)
                return PublishResult(
                    ref=self._ref(key, staged.sha256, staged.size),
                    outcome=PublishOutcome.CREATED,
                )
            finally:
                _close_quiet(entry_fd)

    def lookup(self, key: str) -> ObjectRef | None:
        valid = self._validated_key(key)
        with self._open_roots() as (warehouse_fd, _staging_fd):
            self._assert_key_not_staging(valid, warehouse_fd)
            identity = self._published_identity(warehouse_fd, valid)
            if identity is None:
                return None
            digest, size = identity
            return self._ref(valid, digest, size)

    def open_read(self, ref: ObjectRef) -> BinaryIO:
        key = self._validated_key(ref.key)
        owned_fd: int | None = None
        try:
            with self._open_roots() as (warehouse_fd, _staging_fd):
                self._assert_key_not_staging(key, warehouse_fd)
                try:
                    fd = self._open_final_object_fd(warehouse_fd, key)
                except ObjectKeyViolation:
                    raise
                except FileNotFoundError as exc:
                    raise ObjectNotFound(key) from exc
                except OSError as exc:
                    if exc.errno in {errno.ENOENT, errno.ENOTDIR}:
                        raise ObjectNotFound(key) from exc
                    if exc.errno in {errno.ELOOP, errno.EPERM}:
                        raise IntegrityViolation(
                            f"{key} path contains a symlink or unsafe entry"
                        ) from exc
                    raise
                owned_fd = fd
                try:
                    mode = os.fstat(fd).st_mode
                    if not stat.S_ISREG(mode):
                        raise IntegrityViolation(f"{key} is not a regular file")
                    digest, size = _sha256_fd(fd, chunk_size=self._chunk_size)
                    if digest != ref.sha256 or size != ref.size:
                        raise IntegrityViolation(f"{key} does not match the reference")
                    os.lseek(fd, 0, os.SEEK_SET)
                except BaseException:
                    _close_quiet(owned_fd)
                    owned_fd = None
                    raise
                # Keep the object FD; root FDs close when the with-block exits.
                transfer = owned_fd
                owned_fd = None
            try:
                return open(transfer, "rb", closefd=True)
            except BaseException:
                _close_quiet(transfer)
                raise
        except BaseException:
            _close_quiet(owned_fd)
            raise

    # -----------------------------------------------------------------------------------
    # internals
    # -----------------------------------------------------------------------------------

    def _ensure_open(self) -> None:
        if self._closed:
            raise StorageError("LocalFileStorageAdapter is closed")

    def _open_bound_root(self, path: Path, expected: os.stat_result, label: str) -> int:
        try:
            fd = os.open(path, _ROOT_FLAGS)
        except OSError as exc:
            if exc.errno in {errno.ELOOP, errno.EPERM}:
                raise IntegrityViolation(
                    f"{label} root is a symlink or unsafe entry: {path}"
                ) from exc
            raise
        try:
            self._assert_root_bound(fd, expected, label)
        except BaseException:
            _close_quiet(fd)
            raise
        return fd

    def _assert_root_bound(self, fd: int, expected: os.stat_result, label: str) -> None:
        st = os.fstat(fd)
        if not stat.S_ISDIR(st.st_mode):
            raise IntegrityViolation(f"{label} root is not a directory")
        if not _same_inode(st, expected):
            raise IntegrityViolation(
                f"{label} root inode no longer matches the adapter construction binding"
            )

    @contextmanager
    def _open_roots(self) -> Iterator[tuple[int, int]]:
        self._ensure_open()
        warehouse_fd = self._open_bound_root(self._warehouse, self._warehouse_stat, "warehouse")
        staging_fd: int | None = None
        try:
            staging_fd = self._open_bound_root(self._staging, self._staging_stat, "staging")
            yield warehouse_fd, staging_fd
        finally:
            _close_quiet(staging_fd)
            _close_quiet(warehouse_fd)

    def _validated_key(self, value: object) -> str:
        return validate_object_key(value)

    def _assert_key_not_staging(self, key: str, warehouse_fd: int) -> None:
        """Reject keys that map into staging or whose warehouse path crosses a symlink."""
        if _is_within(self._staging, self._warehouse):
            rel = self._staging.relative_to(self._warehouse).as_posix()
            if key == rel or key.startswith(f"{rel}/"):
                raise ObjectKeyViolation(f"{key!r} maps into the private staging area")
        parts = key.split("/")
        dir_fd = warehouse_fd
        owned: list[int] = []
        try:
            for part in parts[:-1]:
                try:
                    next_fd = os.open(part, _DIR_FLAGS, dir_fd=dir_fd)
                except FileNotFoundError:
                    return
                except OSError as exc:
                    if exc.errno == errno.ENOENT:
                        return
                    if exc.errno in {errno.ELOOP, errno.EPERM}:
                        raise ObjectKeyViolation(
                            f"object key path contains a symlink: {key!r}"
                        ) from exc
                    if exc.errno == errno.ENOTDIR:
                        # O_DIRECTORY|O_NOFOLLOW on a symlink yields ENOTDIR on Linux.
                        try:
                            st = os.stat(part, dir_fd=dir_fd, follow_symlinks=False)
                        except OSError as st_exc:
                            if st_exc.errno == errno.ENOENT:
                                return
                            raise ObjectKeyViolation(
                                f"object key path is not safely walkable: {key!r}"
                            ) from st_exc
                        if stat.S_ISLNK(st.st_mode):
                            raise ObjectKeyViolation(
                                f"object key path contains a symlink: {key!r}"
                            ) from exc
                        # Regular file / non-dir prefix: cannot be a staging mapping.
                        return
                    raise
                owned.append(next_fd)
                st = os.fstat(next_fd)
                if _same_inode(st, self._staging_stat):
                    raise ObjectKeyViolation(f"{key!r} maps into the private staging area")
                dir_fd = next_fd
        finally:
            for fd in reversed(owned):
                _close_quiet(fd)

    def _object_uri(self, key: str) -> str:
        return self._warehouse.joinpath(*key.split("/")).as_uri()

    def _ref(self, key: str, digest: str, size: int) -> ObjectRef:
        return ObjectRef(key=key, uri=self._object_uri(key), sha256=digest, size=size)

    def _usable_ref(self, warehouse_fd: int, key: str, digest: str, size: int) -> ObjectRef:
        """Build a ref only after confirming the object is reachable under the warehouse root."""
        fd = self._open_final_object_fd(warehouse_fd, key)
        try:
            mode = os.fstat(fd).st_mode
            if not stat.S_ISREG(mode):
                raise IntegrityViolation(f"{key} is not a regular file")
            got_digest, got_size = _sha256_fd(fd, chunk_size=self._chunk_size)
            if got_digest != digest or got_size != size:
                raise IntegrityViolation(f"{key} identity changed during publish")
        finally:
            _close_quiet(fd)
        return self._ref(key, digest, size)

    def _cleanup_staging_entry(
        self, staging_fd: int, staging_id: str, entry_fd: int | None
    ) -> None:
        if entry_fd is not None:
            self._unlink_in_dir(entry_fd, _PAYLOAD_NAME)
            self._unlink_in_dir(entry_fd, _META_NAME)
        try:
            os.rmdir(staging_id, dir_fd=staging_fd)
        except OSError:
            return

    def _unlink_in_dir(self, dir_fd: int, name: str) -> None:
        try:
            os.unlink(name, dir_fd=dir_fd)
        except FileNotFoundError:
            return
        except OSError:
            return

    def _unlink_created_final(self, parent_fd: int, final_name: str) -> None:
        """Remove a final we just created via this parent FD; fsync the parent."""
        self._unlink_in_dir(parent_fd, final_name)
        try:
            os.fsync(parent_fd)
        except OSError:
            return

    def _stream_to_payload(self, entry_fd: int, content: Iterable[bytes]) -> tuple[str, int]:
        hasher = hashlib.sha256()
        size = 0
        fd = os.open(_PAYLOAD_NAME, _FILE_CREATE_FLAGS, 0o644, dir_fd=entry_fd)
        try:
            for chunk in content:
                if not isinstance(chunk, (bytes, bytearray, memoryview)):
                    msg = f"content chunk must be bytes-like, got {type(chunk).__name__}"
                    raise TypeError(msg)
                data = bytes(chunk)
                if data:
                    _write_all(fd, data)
                hasher.update(data)
                size += len(data)
            os.fsync(fd)
        finally:
            os.close(fd)
        return hasher.hexdigest(), size

    def _write_meta(self, entry_fd: int, meta: dict[str, object]) -> None:
        payload = json.dumps(meta, separators=(",", ":"), sort_keys=True).encode("ascii")
        fd = os.open(_META_NAME, _FILE_CREATE_FLAGS, 0o644, dir_fd=entry_fd)
        try:
            _write_all(fd, payload)
            os.fsync(fd)
        finally:
            os.close(fd)

    def _load_issued(
        self, staging_fd: int, staged: StagedObject
    ) -> tuple[int, bool, dict[str, object]]:
        staging_id = staged.staging_id
        if "/" in staging_id or "\\" in staging_id or staging_id in {".", ".."}:
            raise StagingViolation(f"illegal staging_id: {staging_id!r}")
        try:
            entry_fd = os.open(staging_id, _DIR_FLAGS, dir_fd=staging_fd)
        except FileNotFoundError as exc:
            raise StagingViolation(f"unknown or altered staging_id: {staging_id!r}") from exc
        except OSError as exc:
            if exc.errno in {errno.ELOOP, errno.EPERM, errno.ENOTDIR}:
                raise StagingViolation(
                    f"staging entry is not a safe directory: {staging_id!r}"
                ) from exc
            raise
        try:
            meta = self._read_meta(entry_fd)
            payload_present = self._regular_file_exists(entry_fd, _PAYLOAD_NAME)
        except BaseException:
            _close_quiet(entry_fd)
            raise
        return entry_fd, payload_present, meta

    def _read_meta(self, entry_fd: int) -> dict[str, object]:
        try:
            fd = os.open(_META_NAME, _FILE_READ_FLAGS, dir_fd=entry_fd)
        except FileNotFoundError as exc:
            raise StagingViolation("unknown or altered staging_id: missing metadata") from exc
        except OSError as exc:
            if exc.errno in {errno.ELOOP, errno.EPERM}:
                raise StagingViolation("staged metadata path is a symlink") from exc
            raise StagingViolation("staged metadata is unreadable") from exc
        try:
            mode = os.fstat(fd).st_mode
            if not stat.S_ISREG(mode):
                raise StagingViolation("staged metadata is not a regular file")
            raw = b"".join(iter(lambda: os.read(fd, self._chunk_size), b""))
            meta = json.loads(raw.decode("ascii"))
        except StagingViolation:
            raise
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise StagingViolation("staged metadata is unreadable") from exc
        finally:
            os.close(fd)
        if not isinstance(meta, dict):
            raise StagingViolation("staged metadata has invalid shape")
        for field in ("key", "sha256", "size"):
            if field not in meta:
                raise StagingViolation(f"staged metadata missing {field}")
        if not isinstance(meta["key"], str) or not isinstance(meta["sha256"], str):
            raise StagingViolation("staged metadata has invalid types")
        if not isinstance(meta["size"], int) or isinstance(meta["size"], bool):
            raise StagingViolation("staged metadata size must be an int")
        return meta

    def _regular_file_exists(self, dir_fd: int, name: str) -> bool:
        try:
            st = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
        except FileNotFoundError:
            return False
        except OSError:
            return False
        if stat.S_ISLNK(st.st_mode):
            raise StagingViolation(f"{name} is a symlink")
        return stat.S_ISREG(st.st_mode)

    def _hash_named_regular(self, dir_fd: int, name: str) -> tuple[str, int]:
        try:
            fd = os.open(name, _FILE_READ_FLAGS, dir_fd=dir_fd)
        except OSError as exc:
            if exc.errno in {errno.ELOOP, errno.EPERM}:
                raise StagingViolation(f"{name} is a symlink") from exc
            raise StagingViolation(f"{name} is unreadable") from exc
        try:
            mode = os.fstat(fd).st_mode
            if not stat.S_ISREG(mode):
                raise StagingViolation(f"{name} is not a regular file")
            return _sha256_fd(fd, chunk_size=self._chunk_size)
        finally:
            os.close(fd)

    def _open_publish_parent(self, warehouse_fd: int, key: str) -> tuple[int, str, bool]:
        """Open (or create) the final parent directory under the warehouse FD.

        Returns ``(parent_fd, final_name, owned)``. Caller closes ``parent_fd`` iff ``owned``.
        """
        parts = key.split("/")
        final_name = parts[-1]
        if len(parts) == 1:
            return warehouse_fd, final_name, False

        dir_fd = warehouse_fd
        owned: list[int] = []
        try:
            for part in parts[:-1]:
                next_fd = self._mkdir_open_nofollow(dir_fd, part)
                owned.append(next_fd)
                st = os.fstat(next_fd)
                if _same_inode(st, self._staging_stat):
                    raise ObjectKeyViolation(f"{key!r} maps into the private staging area")
                dir_fd = next_fd
                os.fsync(dir_fd)
            # Transfer ownership of the deepest FD to the caller; close intermediates.
            parent_fd = owned.pop()
            for fd in reversed(owned):
                _close_quiet(fd)
            owned.clear()
            return parent_fd, final_name, True
        except BaseException:
            for fd in reversed(owned):
                _close_quiet(fd)
            raise

    def _mkdir_open_nofollow(self, parent_fd: int, name: str) -> int:
        try:
            os.mkdir(name, 0o755, dir_fd=parent_fd)
        except FileExistsError:
            pass
        try:
            return os.open(name, _DIR_FLAGS, dir_fd=parent_fd)
        except OSError as exc:
            if exc.errno in {errno.ELOOP, errno.EPERM, errno.ENOTDIR}:
                raise ObjectKeyViolation(
                    f"cannot create object path; prefix is a symlink or non-directory: {name!r}"
                ) from exc
            raise

    def _open_final_object_fd(self, warehouse_fd: int, key: str) -> int:
        parts = key.split("/")
        dir_fd = warehouse_fd
        owned: list[int] = []
        try:
            for part in parts[:-1]:
                try:
                    next_fd = os.open(part, _DIR_FLAGS, dir_fd=dir_fd)
                except OSError as exc:
                    if exc.errno in {errno.ELOOP, errno.EPERM}:
                        raise ObjectKeyViolation(
                            f"object key path contains a symlink: {key!r}"
                        ) from exc
                    if exc.errno == errno.ENOTDIR:
                        try:
                            st = os.stat(part, dir_fd=dir_fd, follow_symlinks=False)
                        except OSError as st_exc:
                            raise FileNotFoundError(key) from st_exc
                        if stat.S_ISLNK(st.st_mode):
                            raise ObjectKeyViolation(
                                f"object key path contains a symlink: {key!r}"
                            ) from exc
                        raise FileNotFoundError(key) from exc
                    raise
                owned.append(next_fd)
                st = os.fstat(next_fd)
                if _same_inode(st, self._staging_stat):
                    raise ObjectKeyViolation(f"{key!r} maps into the private staging area")
                dir_fd = next_fd
            try:
                fd = os.open(parts[-1], _FILE_READ_FLAGS, dir_fd=dir_fd)
            except OSError as exc:
                if exc.errno in {errno.ELOOP, errno.EPERM}:
                    raise IntegrityViolation(f"{key} is a symlink") from exc
                raise
            return fd
        finally:
            for owned_fd in reversed(owned):
                _close_quiet(owned_fd)

    def _published_identity(self, warehouse_fd: int, key: str) -> tuple[str, int] | None:
        try:
            fd = self._open_final_object_fd(warehouse_fd, key)
        except ObjectKeyViolation:
            raise
        except IntegrityViolation:
            raise
        except FileNotFoundError:
            return None
        except OSError as exc:
            if exc.errno in {errno.ENOENT, errno.ENOTDIR}:
                return None
            if exc.errno in {errno.ELOOP, errno.EPERM}:
                raise IntegrityViolation(f"{key} path contains a symlink or unsafe entry") from exc
            raise
        try:
            mode = os.fstat(fd).st_mode
            if not stat.S_ISREG(mode):
                raise IntegrityViolation(f"{key} is not a regular file")
            return _sha256_fd(fd, chunk_size=self._chunk_size)
        finally:
            os.close(fd)

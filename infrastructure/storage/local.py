"""Local ``file://`` ``StorageAdapter`` (Phase 1 C1；ADR-0021 / B3 Protocol).

Warehouse 与 staging 必须是同一文件系统上的本地绝对 ``file://`` 根。调用方只传逻辑
object key；实现在每个入口复核 key，并把最终路径重新 join / resolve 后做根包含检查。
写入走有界流式复制（边写边算 SHA-256），校验失败清理本次临时文件；发布用同文件系统
``os.link``（不覆盖）实现原子可见性，并在需要时 ``fsync`` 文件与父目录。
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import stat
from collections.abc import Iterable
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


def _is_within(path: Path, root: Path) -> bool:
    """Return True iff ``path`` is ``root`` or a descendant (both must be resolved)."""
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _fsync_dir(directory: Path) -> None:
    fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _fsync_file(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _sha256_file(path: Path, *, chunk_size: int) -> tuple[str, int]:
    hasher = hashlib.sha256()
    size = 0
    with open(path, "rb") as handle:
        while True:
            chunk = handle.read(chunk_size)
            if not chunk:
                break
            hasher.update(chunk)
            size += len(chunk)
    return hasher.hexdigest(), size


def _remove_path(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        return


def _remove_tree_if_empty(path: Path) -> None:
    """Best-effort remove a staging entry directory after payload/meta cleanup."""
    try:
        path.rmdir()
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
        if filesystem_device_id(self._warehouse) != filesystem_device_id(self._staging):
            msg = "staging_uri and warehouse_uri must be on the same filesystem"
            raise ValueError(msg)
        if _is_within(self._warehouse, self._staging) and self._warehouse != self._staging:
            msg = "warehouse_uri must not be inside staging_uri"
            raise ValueError(msg)
        self._chunk_size = chunk_size
        self._warehouse_uri = self._warehouse.as_uri()
        self._staging_uri = self._staging.as_uri()

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

    @property
    def warehouse_uri(self) -> str:
        return self._warehouse_uri

    @property
    def staging_uri(self) -> str:
        return self._staging_uri

    @property
    def forbidden_object_keys(self) -> tuple[str, ...]:
        """Syntax-legal keys that would resolve into the private staging area."""
        if not _is_within(self._staging, self._warehouse):
            return ()
        rel = self._staging.relative_to(self._warehouse).as_posix()
        if rel == ".":
            return ()
        return (rel, f"{rel}/escape.bin")

    def stage(self, request: StageRequest, content: Iterable[bytes]) -> StagedObject:
        key = self._validated_key(request.key)
        self._object_path(key)  # path safety before any write
        staging_id = secrets.token_hex(_STAGING_ID_BYTES)
        entry_dir = self._staging_entry_dir(staging_id)
        payload_path = entry_dir / _PAYLOAD_NAME
        meta_path = entry_dir / _META_NAME
        entry_dir.mkdir(mode=0o755)
        try:
            digest, size = self._stream_to_payload(payload_path, content)
            if request.expected_size is not None and size != request.expected_size:
                raise IntegrityViolation(
                    f"size mismatch for {key!r}: expected {request.expected_size}, got {size}"
                )
            if digest != request.expected_sha256:
                raise IntegrityViolation(
                    f"sha256 mismatch for {key!r}: expected {request.expected_sha256}, got {digest}"
                )
            meta = {"key": key, "sha256": digest, "size": size}
            self._write_meta(meta_path, meta)
            _fsync_dir(entry_dir)
            _fsync_dir(self._staging)
        except BaseException:
            _remove_path(payload_path)
            _remove_path(meta_path)
            _remove_tree_if_empty(entry_dir)
            raise
        return StagedObject(key=key, sha256=digest, size=size, staging_id=staging_id)

    def publish(self, staged: StagedObject) -> PublishResult:
        key = self._validated_key(staged.key)
        dest = self._object_path(key)
        entry_dir, payload_path, meta = self._load_issued(staged)
        if (meta["key"], meta["sha256"], meta["size"]) != (key, staged.sha256, staged.size):
            raise StagingViolation("staged object metadata does not match credential")
        if not payload_path.is_file():
            # Credential was already consumed; idempotent only if published object matches.
            existing = self._published_identity(dest)
            if existing is None:
                raise StagingViolation("staged payload missing and target object absent")
            existing_digest, existing_size = existing
            if existing_digest != staged.sha256 or existing_size != staged.size:
                raise ObjectConflict(key)
            return PublishResult(
                ref=self._ref(key, dest, existing_digest, existing_size),
                outcome=PublishOutcome.ALREADY_PRESENT,
            )

        payload_digest, payload_size = _sha256_file(payload_path, chunk_size=self._chunk_size)
        if payload_digest != staged.sha256 or payload_size != staged.size:
            raise StagingViolation("staged payload no longer matches issued identity")

        existing = self._published_identity(dest)
        if existing is not None:
            existing_digest, existing_size = existing
            if existing_digest != staged.sha256 or existing_size != staged.size:
                raise ObjectConflict(key)
            _remove_path(payload_path)
            return PublishResult(
                ref=self._ref(key, dest, existing_digest, existing_size),
                outcome=PublishOutcome.ALREADY_PRESENT,
            )

        self._ensure_parent_dirs(dest)
        try:
            os.link(payload_path, dest)
        except FileExistsError:
            existing = self._published_identity(dest)
            if existing is None:
                raise ObjectConflict(key) from None
            existing_digest, existing_size = existing
            if existing_digest != staged.sha256 or existing_size != staged.size:
                raise ObjectConflict(key) from None
            _remove_path(payload_path)
            return PublishResult(
                ref=self._ref(key, dest, existing_digest, existing_size),
                outcome=PublishOutcome.ALREADY_PRESENT,
            )

        try:
            _fsync_file(dest)
            _fsync_dir(dest.parent)
        finally:
            # Object is already visible via the hard link; drop this op's staging payload.
            _remove_path(payload_path)

        return PublishResult(
            ref=self._ref(key, dest, staged.sha256, staged.size),
            outcome=PublishOutcome.CREATED,
        )

    def lookup(self, key: str) -> ObjectRef | None:
        valid = self._validated_key(key)
        path = self._object_path(valid)
        identity = self._published_identity(path)
        if identity is None:
            return None
        digest, size = identity
        return self._ref(valid, path, digest, size)

    def open_read(self, ref: ObjectRef) -> BinaryIO:
        key = self._validated_key(ref.key)
        path = self._object_path(key)
        identity = self._published_identity(path)
        if identity is None:
            raise ObjectNotFound(key)
        digest, size = identity
        if digest != ref.sha256 or size != ref.size:
            raise IntegrityViolation(f"{key} does not match the reference")
        flags = os.O_RDONLY
        nofollow = getattr(os, "O_NOFOLLOW", 0)
        if nofollow:
            flags |= nofollow
        try:
            fd = os.open(path, flags)
        except OSError as exc:
            raise ObjectNotFound(key) from exc
        mode = os.fstat(fd).st_mode
        if not stat.S_ISREG(mode):
            os.close(fd)
            raise IntegrityViolation(f"{key} is not a regular file")
        return open(fd, "rb", closefd=True)

    # -----------------------------------------------------------------------------------
    # internals
    # -----------------------------------------------------------------------------------

    def _validated_key(self, value: object) -> str:
        return validate_object_key(value)

    def _staging_entry_dir(self, staging_id: str) -> Path:
        entry = (self._staging / staging_id).resolve(strict=False)
        if not _is_within(entry, self._staging) or entry == self._staging:
            raise StagingViolation(f"staging_id escapes staging root: {staging_id!r}")
        return entry

    def _object_path(self, key: str) -> Path:
        """Join + resolve ``key`` under the warehouse; reject escapes and staging overlap."""
        warehouse = self._warehouse
        current = warehouse
        for part in key.split("/"):
            candidate = current / part
            if candidate.exists() or candidate.is_symlink():
                try:
                    resolved = candidate.resolve(strict=True)
                except OSError as exc:
                    raise ObjectKeyViolation(f"object key path is not resolvable: {key!r}") from exc
                if not _is_within(resolved, warehouse):
                    raise ObjectKeyViolation(f"object key escapes warehouse root: {key!r}")
                if _is_within(resolved, self._staging):
                    raise ObjectKeyViolation(f"{key!r} maps into the private staging area")
                current = resolved
            else:
                current = candidate
        final = current.resolve(strict=False)
        if not _is_within(final, warehouse):
            raise ObjectKeyViolation(f"object key escapes warehouse root: {key!r}")
        if _is_within(final, self._staging):
            raise ObjectKeyViolation(f"{key!r} maps into the private staging area")
        return final

    def _ensure_parent_dirs(self, dest: Path) -> None:
        warehouse = self._warehouse
        parent = dest.parent
        if parent == warehouse:
            return
        rel = parent.relative_to(warehouse)
        current = warehouse
        for part in rel.parts:
            current = current / part
            if current.exists() or current.is_symlink():
                if not current.is_dir():
                    raise ObjectKeyViolation(
                        f"cannot create object path; prefix is not a directory: {current}"
                    )
                resolved = current.resolve(strict=True)
                if not _is_within(resolved, warehouse):
                    raise ObjectKeyViolation("parent directory escapes warehouse root")
                if _is_within(resolved, self._staging):
                    raise ObjectKeyViolation("parent directory maps into staging")
                current = resolved
            else:
                current.mkdir(mode=0o755, exist_ok=True)
                if current.is_symlink() or not current.is_dir():
                    raise ObjectKeyViolation(
                        f"cannot create object path; prefix is not a safe directory: {current}"
                    )
                resolved = current.resolve(strict=True)
                if not _is_within(resolved, warehouse):
                    raise ObjectKeyViolation("newly created parent escapes warehouse root")
                if _is_within(resolved, self._staging):
                    raise ObjectKeyViolation("parent directory maps into staging")
                _fsync_dir(resolved.parent)
                current = resolved

    def _stream_to_payload(self, payload_path: Path, content: Iterable[bytes]) -> tuple[str, int]:
        hasher = hashlib.sha256()
        size = 0
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        fd = os.open(payload_path, flags, 0o644)
        try:
            for chunk in content:
                if not isinstance(chunk, (bytes, bytearray, memoryview)):
                    msg = f"content chunk must be bytes-like, got {type(chunk).__name__}"
                    raise TypeError(msg)
                data = bytes(chunk)
                hasher.update(data)
                size += len(data)
                if data:
                    os.write(fd, data)
            os.fsync(fd)
        finally:
            os.close(fd)
        return hasher.hexdigest(), size

    def _write_meta(self, meta_path: Path, meta: dict[str, object]) -> None:
        payload = json.dumps(meta, separators=(",", ":"), sort_keys=True).encode("ascii")
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        fd = os.open(meta_path, flags, 0o644)
        try:
            os.write(fd, payload)
            os.fsync(fd)
        finally:
            os.close(fd)

    def _load_issued(self, staged: StagedObject) -> tuple[Path, Path, dict[str, object]]:
        staging_id = staged.staging_id
        if "/" in staging_id or "\\" in staging_id or staging_id in {".", ".."}:
            raise StagingViolation(f"illegal staging_id: {staging_id!r}")
        entry_dir = self._staging_entry_dir(staging_id)
        meta_path = entry_dir / _META_NAME
        payload_path = entry_dir / _PAYLOAD_NAME
        if not meta_path.is_file():
            raise StagingViolation(f"unknown or altered staging_id: {staging_id!r}")
        try:
            raw = meta_path.read_bytes()
            meta = json.loads(raw.decode("ascii"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise StagingViolation("staged metadata is unreadable") from exc
        if not isinstance(meta, dict):
            raise StagingViolation("staged metadata has invalid shape")
        for field in ("key", "sha256", "size"):
            if field not in meta:
                raise StagingViolation(f"staged metadata missing {field}")
        if not isinstance(meta["key"], str) or not isinstance(meta["sha256"], str):
            raise StagingViolation("staged metadata has invalid types")
        if not isinstance(meta["size"], int) or isinstance(meta["size"], bool):
            raise StagingViolation("staged metadata size must be an int")
        return entry_dir, payload_path, meta

    def _published_identity(self, path: Path) -> tuple[str, int] | None:
        if path.is_symlink():
            try:
                resolved = path.resolve(strict=True)
            except OSError:
                return None
            if not _is_within(resolved, self._warehouse) or _is_within(resolved, self._staging):
                return None
            path = resolved
        if not path.is_file():
            return None
        if not _is_within(path.resolve(strict=True), self._warehouse):
            return None
        return _sha256_file(path, chunk_size=self._chunk_size)

    def _ref(self, key: str, path: Path, digest: str, size: int) -> ObjectRef:
        return ObjectRef(key=key, uri=path.resolve(strict=True).as_uri(), sha256=digest, size=size)

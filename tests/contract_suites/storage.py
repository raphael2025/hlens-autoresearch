"""`StorageAdapter` 的 provider-agnostic contract suite（core/contracts/storage.py，ADR-0021）。

实现方提供 `StorageSubject`：

- `open`：每次调用返回一个**新** adapter 实例，与此前实例共享同一持久状态（例如同一 warehouse
  根目录），用来模拟进程重启；
- `forbidden_keys`：语法合法、但会映射进该实现私有区域（例如 staging 目录）而必须被拒绝的 key。

每个检查只通过 Protocol 方法观察行为；suite 不 import 任何实现。
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from functools import partial

import pytest

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
    StorageAdapter,
    StorageError,
)
from tests.contract_suites._support import (
    ContractSuiteFailure,
    call_ok,
    expect_error,
    require,
    revalidated,
)

__all__ = [
    "INVALID_OBJECT_KEYS",
    "STORAGE_CHECKS",
    "StorageAdapterContract",
    "StorageCheck",
    "StorageSubject",
]


@dataclass(frozen=True)
class StorageSubject:
    """被测 `StorageAdapter` 的接入点（见模块文档）。"""

    open: Callable[[], StorageAdapter]
    forbidden_keys: tuple[str, ...] = ()


type StorageCheck = Callable[[StorageSubject], None]

#: 必须在每个入口被拒绝的 key：路径逃逸、绝对路径、空段、`.` / `..`、隐藏段、反斜杠、URI scheme、
#: 盘符、NUL / 空白 / 控制字符、编码绕过、非 ASCII 同形字符、超长。
INVALID_OBJECT_KEYS: tuple[str, ...] = (
    "",
    "/",
    "/abs/object.bin",
    "a/",
    "a//b",
    "../escape.bin",
    "a/../../escape.bin",
    "a/./b",
    ".",
    "..",
    ".hidden",
    "a/.hidden",
    "-flag",
    "a\\b",
    "..\\escape.bin",
    "C:/escape.bin",
    "file:///etc/passwd",
    "s3://bucket/object.bin",
    "a/b\x00c",
    "a b",
    " a",
    "a\n",
    "a\tb",
    "%2e%2e/escape.bin",
    "a%2Fb",
    "~/escape.bin",
    "\uff41/b",
    "caf\u00e9",
    "a" * 1025,
    "a/" + "b" * 256,
)

_PREFIX = "contract-suite"


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _chunks(data: bytes, size: int = 7) -> Iterator[bytes]:
    """单遍生成器：多次遍历的实现会读到空流并校验失败。夹一个空块，实现不得因此出错。"""
    yield b""
    for start in range(0, len(data), size):
        yield data[start : start + size]


class _SourceInterrupted(Exception):
    """模拟下载中断的内容流异常；契约要求它原样传播。"""


def _interrupted(data: bytes) -> Iterator[bytes]:
    yield data[: len(data) // 2]
    raise _SourceInterrupted("content stream interrupted")


def _request(key: str, data: bytes, *, with_size: bool = True) -> StageRequest:
    return StageRequest(
        key=key, expected_sha256=_sha(data), expected_size=len(data) if with_size else None
    )


def _stage(storage: StorageAdapter, key: str, data: bytes) -> StagedObject:
    staged = call_ok(f"stage({key!r})", lambda: storage.stage(_request(key, data), _chunks(data)))
    staged = revalidated(StagedObject, staged, f"stage({key!r}) 的结果")
    require(staged.key == key, f"StagedObject.key 应为 {key!r}，实际为 {staged.key!r}")
    require(staged.sha256 == _sha(data), "StagedObject.sha256 与实际内容不符")
    require(staged.size == len(data), "StagedObject.size 与实际内容不符")
    return staged


def _publish(storage: StorageAdapter, staged: StagedObject, outcome: PublishOutcome) -> ObjectRef:
    result = call_ok(f"publish({staged.key!r})", lambda: storage.publish(staged))
    result = revalidated(PublishResult, result, f"publish({staged.key!r}) 的结果")
    require(result.outcome is outcome, f"publish 结果应为 {outcome.value}，实际为 {result.outcome}")
    ref = result.ref
    require(ref.key == staged.key, "发布引用的 key 与 staged 不符")
    require(ref.sha256 == staged.sha256 and ref.size == staged.size, "发布引用的内容身份不符")
    return ref


def _put(storage: StorageAdapter, key: str, data: bytes, outcome: PublishOutcome) -> ObjectRef:
    return _publish(storage, _stage(storage, key, data), outcome)


def _lookup(storage: StorageAdapter, key: str) -> ObjectRef | None:
    found = call_ok(f"lookup({key!r})", partial(storage.lookup, key))
    if found is None:
        return None
    return revalidated(ObjectRef, found, f"lookup({key!r}) 的结果")


def _read(storage: StorageAdapter, ref: ObjectRef) -> bytes:
    def read() -> bytes:
        with storage.open_read(ref) as handle:
            return handle.read()

    return call_ok(f"open_read({ref.key!r})", read)


def _require_absent(storage: StorageAdapter, key: str, why: str) -> None:
    require(_lookup(storage, key) is None, f"{why}：{key!r} 不得可见")


def _require_published(storage: StorageAdapter, ref: ObjectRef, data: bytes) -> None:
    require(_lookup(storage, ref.key) == ref, f"lookup({ref.key!r}) 应返回已发布引用")
    require(_read(storage, ref) == data, f"{ref.key!r} 的内容与发布时不同")


# ======================================================================================
# 检查
# ======================================================================================


def check_stage_publish_read_round_trip(subject: StorageSubject) -> None:
    """staging 不可见；发布后 lookup / read 与发布结果一致；重建实例后仍一致。"""
    storage = subject.open()
    key = f"{_PREFIX}/round-trip/2024-01-01/part=0/object.bin"
    data = bytes(range(256)) * 3
    _require_absent(storage, key, "发布前")
    staged = _stage(storage, key, data)
    _require_absent(storage, key, "stage 之后、publish 之前")
    _require_absent(subject.open(), key, "stage 之后（另一实例）")
    ref = _publish(storage, staged, PublishOutcome.CREATED)
    _require_published(storage, ref, data)
    _require_published(subject.open(), ref, data)


def check_zero_byte_object(subject: StorageSubject) -> None:
    storage = subject.open()
    key = f"{_PREFIX}/empty.bin"
    staged = call_ok(
        "stage(空内容)",
        lambda: storage.stage(_request(key, b"", with_size=False), _chunks(b"")),
    )
    staged = revalidated(StagedObject, staged, "stage(空内容) 的结果")
    ref = _publish(storage, staged, PublishOutcome.CREATED)
    require(ref.size == 0 and ref.sha256 == _sha(b""), "零字节对象的身份不符")
    _require_published(storage, ref, b"")


def check_legal_nested_keys_are_independent(subject: StorageSubject) -> None:
    """合法嵌套 key 互不影响；前缀碰撞要么都成功，要么 fail closed 且不改变已有对象。"""
    storage = subject.open()
    items = {
        f"{_PREFIX}/nested/a.bin": b"alpha",
        f"{_PREFIX}/nested/b.bin": b"bravo",
        f"{_PREFIX}/nested/deep/x/y/z=1/c.bin": b"charlie",
        f"{_PREFIX}/NESTED/a.bin": b"upper-case key is distinct",
    }
    refs = {key: _put(storage, key, data, PublishOutcome.CREATED) for key, data in items.items()}
    for key, data in items.items():
        _require_published(storage, refs[key], data)

    parent_key = f"{_PREFIX}/prefix/p"
    parent = _put(storage, parent_key, b"parent", PublishOutcome.CREATED)
    child_key = f"{parent_key}/child.bin"
    try:
        child = _put(storage, child_key, b"child", PublishOutcome.CREATED)
    except ContractSuiteFailure as failure:
        if not isinstance(failure.__cause__, StorageError):
            raise
        _require_absent(storage, child_key, "前缀碰撞失败之后")
    else:
        _require_published(storage, child, b"child")
    _require_published(storage, parent, b"parent")


def check_invalid_keys_rejected_at_every_entry(subject: StorageSubject) -> None:
    """非法 key 在 lookup / stage / publish / open_read 四个入口都以 `ObjectKeyViolation` 失败。

    `model_construct` 在这里模拟未经 DTO 校验的 Python 调用方：实现不得信任 DTO 已校验。
    """
    storage = subject.open()
    data = b"must never be written"
    sha = _sha(data)
    for key in (*INVALID_OBJECT_KEYS, *subject.forbidden_keys):
        expect_error(ObjectKeyViolation, f"lookup({key!r})", partial(storage.lookup, key))
        request = StageRequest.model_construct(key=key, expected_sha256=sha, expected_size=None)
        expect_error(
            ObjectKeyViolation,
            f"stage({key!r})",
            partial(storage.stage, request, _chunks(data)),
        )
        staged = StagedObject.model_construct(key=key, sha256=sha, size=len(data), staging_id="x")
        expect_error(ObjectKeyViolation, f"publish({key!r})", partial(storage.publish, staged))
        ref = ObjectRef.model_construct(key=key, uri="memory://x/y", sha256=sha, size=len(data))
        expect_error(ObjectKeyViolation, f"open_read({key!r})", partial(storage.open_read, ref))


def check_checksum_mismatch_is_not_published(subject: StorageSubject) -> None:
    storage = subject.open()
    key = f"{_PREFIX}/checksum.bin"
    data = b"payload whose checksum is wrong"
    wrong = StageRequest(key=key, expected_sha256=_sha(b"other"), expected_size=None)
    expect_error(
        IntegrityViolation, "SHA-256 不符的 stage", lambda: storage.stage(wrong, _chunks(data))
    )
    _require_absent(storage, key, "SHA-256 校验失败之后")
    _require_absent(subject.open(), key, "SHA-256 校验失败之后（另一实例）")
    ref = _put(storage, key, data, PublishOutcome.CREATED)
    _require_published(storage, ref, data)


def check_size_mismatch_is_not_published(subject: StorageSubject) -> None:
    storage = subject.open()
    key = f"{_PREFIX}/size.bin"
    data = b"payload whose size is wrong"
    wrong = StageRequest(key=key, expected_sha256=_sha(data), expected_size=len(data) + 1)
    expect_error(
        IntegrityViolation, "长度不符的 stage", lambda: storage.stage(wrong, _chunks(data))
    )
    _require_absent(storage, key, "长度校验失败之后")
    ref = _put(storage, key, data, PublishOutcome.CREATED)
    _require_published(storage, ref, data)


def check_interrupted_stream_leaves_nothing_visible(subject: StorageSubject) -> None:
    """内容流中途失败：异常原样传播，没有任何可见的半成品，之后可以正常重试。"""
    storage = subject.open()
    key = f"{_PREFIX}/interrupted.bin"
    data = b"0123456789" * 20
    expect_error(
        _SourceInterrupted,
        "内容流中断的 stage",
        lambda: storage.stage(_request(key, data), _interrupted(data)),
    )
    _require_absent(storage, key, "内容流中断之后")
    _require_absent(subject.open(), key, "内容流中断之后（另一实例）")
    ref = _put(storage, key, data, PublishOutcome.CREATED)
    _require_published(storage, ref, data)


def check_republishing_same_content_is_idempotent(subject: StorageSubject) -> None:
    """同 key 同内容：新 staging、同一 `StagedObject` 的重试、重建实例后重放，都返回同一引用。"""
    storage = subject.open()
    key = f"{_PREFIX}/idempotent.bin"
    data = b"idempotent payload"
    first_staged = _stage(storage, key, data)
    ref = _publish(storage, first_staged, PublishOutcome.CREATED)
    again = _put(storage, key, data, PublishOutcome.ALREADY_PRESENT)
    require(again == ref, "同内容重放必须返回同一引用")
    retried = _publish(storage, first_staged, PublishOutcome.ALREADY_PRESENT)
    require(retried == ref, "重试同一 StagedObject 必须返回同一引用")
    restarted = _put(subject.open(), key, data, PublishOutcome.ALREADY_PRESENT)
    require(restarted == ref, "重建实例后的重放必须返回同一引用")
    _require_published(storage, ref, data)


def check_conflicting_content_fails_closed(subject: StorageSubject) -> None:
    """同 key 不同内容：`ObjectConflict`（stage 时或 publish 时），已有对象保持不变。"""
    storage = subject.open()
    key = f"{_PREFIX}/conflict.bin"
    original = b"original content"
    ref = _put(storage, key, original, PublishOutcome.CREATED)
    other = b"different content"
    try:
        staged = storage.stage(_request(key, other), _chunks(other))
    except ObjectConflict:
        pass
    except Exception as exc:
        raise ContractSuiteFailure(
            f"冲突内容的 stage 只能成功或抛 ObjectConflict，实际 {type(exc).__name__}: {exc}"
        ) from exc
    else:
        expect_error(ObjectConflict, "发布冲突内容", lambda: storage.publish(staged))
    _require_published(storage, ref, original)
    _require_published(subject.open(), ref, original)


def check_forged_staged_object_is_rejected(subject: StorageSubject) -> None:
    """篡改或伪造的 `StagedObject` 一律 `StagingViolation`，且不产生可见对象。"""
    storage = subject.open()
    key = f"{_PREFIX}/forged.bin"
    other_key = f"{_PREFIX}/forged-elsewhere.bin"
    data = b"genuine staged content"
    staged = _stage(storage, key, data)
    forgeries = {
        "篡改 sha256": staged.model_copy(update={"sha256": _sha(b"forged")}),
        "篡改 size": staged.model_copy(update={"size": staged.size + 1}),
        "篡改 key": staged.model_copy(update={"key": other_key}),
        "未知 staging_id": staged.model_copy(update={"staging_id": "never-issued-staging-id"}),
    }
    for label, forged in forgeries.items():
        expect_error(StagingViolation, f"publish({label})", partial(storage.publish, forged))
        _require_absent(storage, key, f"{label} 之后")
        _require_absent(storage, other_key, f"{label} 之后")
    ref = _publish(storage, staged, PublishOutcome.CREATED)
    _require_published(storage, ref, data)


def check_read_requires_matching_ref_and_is_read_only(subject: StorageSubject) -> None:
    storage = subject.open()
    key = f"{_PREFIX}/read.bin"
    data = b"read-only content"
    ref = _put(storage, key, data, PublishOutcome.CREATED)
    missing = ref.model_copy(update={"key": f"{_PREFIX}/never-published.bin"})
    expect_error(ObjectNotFound, "open_read(未发布)", lambda: storage.open_read(missing))
    wrong_sha = ref.model_copy(update={"sha256": _sha(b"other")})
    expect_error(IntegrityViolation, "open_read(错误 sha256)", lambda: storage.open_read(wrong_sha))
    wrong_size = ref.model_copy(update={"size": ref.size + 1})
    expect_error(IntegrityViolation, "open_read(错误 size)", lambda: storage.open_read(wrong_size))

    def try_write() -> None:
        with storage.open_read(ref) as handle:
            require(not handle.writable(), "open_read 返回的 handle 不得可写")
            try:
                handle.write(b"tamper")
            except Exception:
                return
            raise ContractSuiteFailure("向只读 handle 写入必须失败")

    call_ok("只读 handle 检查", try_write)
    _require_published(storage, ref, data)


def check_restart_keeps_published_and_hides_staged(subject: StorageSubject) -> None:
    storage = subject.open()
    published_key = f"{_PREFIX}/restart/published.bin"
    staged_key = f"{_PREFIX}/restart/staged-only.bin"
    ref = _put(storage, published_key, b"survives restart", PublishOutcome.CREATED)
    _stage(storage, staged_key, b"never published")
    restarted = subject.open()
    _require_published(restarted, ref, b"survives restart")
    _require_absent(restarted, staged_key, "重建实例后，未发布的 staging")


STORAGE_CHECKS: tuple[StorageCheck, ...] = (
    check_stage_publish_read_round_trip,
    check_zero_byte_object,
    check_legal_nested_keys_are_independent,
    check_invalid_keys_rejected_at_every_entry,
    check_checksum_mismatch_is_not_published,
    check_size_mismatch_is_not_published,
    check_interrupted_stream_leaves_nothing_visible,
    check_republishing_same_content_is_idempotent,
    check_conflicting_content_fails_closed,
    check_forged_staged_object_is_rejected,
    check_read_requires_matching_ref_and_is_read_only,
    check_restart_keeps_published_and_hides_staged,
)


class StorageAdapterContract:
    """pytest 复用入口：子类以 `Test*` 命名并提供 `storage_subject` fixture。"""

    @pytest.fixture
    def storage_subject(self) -> StorageSubject:
        raise NotImplementedError("子类必须提供 storage_subject fixture")

    @pytest.mark.parametrize("check", STORAGE_CHECKS, ids=lambda check: check.__name__)
    def test_storage_contract(self, storage_subject: StorageSubject, check: StorageCheck) -> None:
        check(storage_subject)

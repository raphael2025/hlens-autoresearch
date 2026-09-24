"""`CollectorAdapter` 的 provider-agnostic contract suite（core/contracts/collector.py；ADR-0022）。

实现方提供 `CollectorSubject`：

- `storage`：collector 发布对象所用的 `StorageAdapter`（suite 只用它 `lookup` / `open_read` 核对）；
- `open`：每次调用返回一个**新** collector 实例，使用同一 storage 与同一离线来源夹具（模拟重启）；
- `request`：该 collector 能**离线**完成、且至少产出一个对象的请求。suite 不访问网络：D0 的实现
  应以本地夹具或 mock transport 提供来源。
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from dataclasses import dataclass
from functools import partial

import pytest

from core.contracts.collector import (
    CollectionRequest,
    CollectionResult,
    CollectorAdapter,
    CollectorDescriptor,
    SourceBinding,
    UnsupportedRequest,
)
from core.contracts.storage import ObjectRef, StorageAdapter
from tests.contract_suites._support import call_ok, expect_error, require, revalidated

__all__ = [
    "COLLECTOR_CHECKS",
    "CollectorAdapterContract",
    "CollectorCheck",
    "CollectorSubject",
]


@dataclass(frozen=True)
class CollectorSubject:
    """被测 `CollectorAdapter` 的接入点（见模块文档）。"""

    storage: StorageAdapter
    open: Callable[[], CollectorAdapter]
    request: CollectionRequest


type CollectorCheck = Callable[[CollectorSubject], None]

#: 结果中一个对象的可重放身份：`retrieved_at` 与来源元数据不在其中（重放时可以不同）。
type _ObjectIdentity = tuple[str, str, str, int, str, str, str, str, str | None]


def _descriptor(collector: CollectorAdapter) -> CollectorDescriptor:
    raw = call_ok("descriptor", lambda: collector.descriptor)
    return revalidated(CollectorDescriptor, raw, "descriptor")


def _collect(collector: CollectorAdapter, request: CollectionRequest) -> CollectionResult:
    raw = call_ok(f"collect({request.request_id!r})", lambda: collector.collect(request))
    result = revalidated(CollectionResult, raw, f"collect({request.request_id!r}) 的结果")
    require(result.request == request, "CollectionResult.request 必须原样回显请求")
    return result


def _identities(result: CollectionResult) -> tuple[_ObjectIdentity, ...]:
    return tuple(
        (
            item.ref.key,
            item.ref.uri,
            item.ref.sha256,
            item.ref.size,
            item.symbol,
            item.coverage_start.isoformat(),
            item.coverage_end.isoformat(),
            item.source_uri,
            item.source_sha256,
        )
        for item in result.objects
    )


def _read(storage: StorageAdapter, ref: ObjectRef) -> bytes:
    def read() -> bytes:
        with storage.open_read(ref) as handle:
            return handle.read()

    return call_ok(f"open_read({ref.key!r})", read)


def _origin(uri: str) -> str:
    scheme, rest = uri.split("://", 1)
    authority = rest.split("/", 1)[0].split("?", 1)[0]
    return f"{scheme}://{authority}"


# ======================================================================================
# 检查
# ======================================================================================


def check_result_echoes_request_and_collector_identity(subject: CollectorSubject) -> None:
    collector = subject.open()
    descriptor = _descriptor(collector)
    require(
        subject.request.source in descriptor.sources,
        "subject.request 的 source 必须由该 collector 声明服务",
    )
    result = _collect(collector, subject.request)
    require(
        result.collector_id == descriptor.collector_id, "结果的 collector_id 必须等于 descriptor"
    )
    require(
        result.collector_version == descriptor.version,
        "结果的 collector_version 必须等于 descriptor",
    )
    require(_descriptor(collector) == descriptor, "descriptor 在实例生命周期内不得改变")


def check_objects_are_published_and_intact(subject: CollectorSubject) -> None:
    """结果中每个对象都已经在 storage 中发布，引用完全一致，内容哈希与长度与引用一致。"""
    result = _collect(subject.open(), subject.request)
    require(bool(result.objects), "subject.request 必须至少产出一个对象")
    for item in result.objects:
        ref = item.ref
        found = call_ok(f"lookup({ref.key!r})", partial(subject.storage.lookup, ref.key))
        require(found == ref, f"{ref.key!r} 未发布，或已发布对象与结果中的引用不一致")
        data = _read(subject.storage, ref)
        require(hashlib.sha256(data).hexdigest() == ref.sha256, f"{ref.key!r} 的内容与 sha256 不符")
        require(len(data) == ref.size, f"{ref.key!r} 的长度与 size 不符")


def check_network_sources_are_declared(subject: CollectorSubject) -> None:
    """网络来源必须来自 descriptor 声明的 HTTPS origin（一致性核对，不是安全控制）。"""
    collector = subject.open()
    declared = set(_descriptor(collector).network_origins)
    result = _collect(collector, subject.request)
    for item in result.objects:
        scheme = item.source_uri.split("://", 1)[0]
        if scheme in {"http", "https", "ws", "wss"}:
            origin = _origin(item.source_uri)
            require(origin in declared, f"来源 {origin!r} 不在声明的 network_origins 内")


def check_replay_is_idempotent(subject: CollectorSubject) -> None:
    """同一请求重放（同一实例、重建实例）得到同一组对象身份与缺口，且对象仍已发布。"""
    first = _collect(subject.open(), subject.request)
    collector = subject.open()
    for label, result in (
        ("同一实例重放", _collect(collector, subject.request)),
        ("同一实例第二次重放", _collect(collector, subject.request)),
    ):
        require(_identities(result) == _identities(first), f"{label}的对象身份必须与首次相同")
        require(result.gaps == first.gaps, f"{label}的缺口必须与首次相同")
    for item in first.objects:
        found = call_ok(f"lookup({item.ref.key!r})", partial(subject.storage.lookup, item.ref.key))
        require(found == item.ref, f"重放后 {item.ref.key!r} 的已发布引用必须不变")


def check_undeclared_source_is_rejected(subject: CollectorSubject) -> None:
    collector = subject.open()
    declared = _descriptor(collector).sources
    source = subject.request.source
    major = 999_999
    while SourceBinding(source_id=source.source_id, version=f"{major}.0.0") in declared:
        major += 1
    undeclared = subject.request.model_copy(
        update={"source": SourceBinding(source_id=source.source_id, version=f"{major}.0.0")}
    )
    expect_error(
        UnsupportedRequest, "collect(未声明的 source)", partial(collector.collect, undeclared)
    )


COLLECTOR_CHECKS: tuple[CollectorCheck, ...] = (
    check_result_echoes_request_and_collector_identity,
    check_objects_are_published_and_intact,
    check_network_sources_are_declared,
    check_replay_is_idempotent,
    check_undeclared_source_is_rejected,
)


class CollectorAdapterContract:
    """pytest 复用入口：子类以 `Test*` 命名并提供 `collector_subject` fixture。"""

    @pytest.fixture
    def collector_subject(self) -> CollectorSubject:
        raise NotImplementedError("子类必须提供 collector_subject fixture")

    @pytest.mark.parametrize("check", COLLECTOR_CHECKS, ids=lambda check: check.__name__)
    def test_collector_contract(
        self, collector_subject: CollectorSubject, check: CollectorCheck
    ) -> None:
        check(collector_subject)

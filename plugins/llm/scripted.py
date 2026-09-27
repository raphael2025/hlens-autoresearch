"""An offline, deterministic LLMProvider that replays scripted outputs (ADR-0040).

For tests and dry runs of the hypothesis generator: no network, no keys. Every call is recorded
as an ``LlmCall`` whose prompt / input / output are content-addressed (``memory://sha256/<hash>``).
A real network provider is a separate plugin (declared ``network=True``) and needs credentials
that are out of scope for the framework build.

Optional ``store`` (Phase 7): any object with ``put(data, media_type) -> ContentBlobRef`` (the
structural ``BlobSink`` Protocol below; ``infrastructure.content.LocalContentStore`` satisfies
it — plugins never import infrastructure). With a store, each blob is the UTF-8
``canonical_json`` of the payload, so its SHA-256 is exactly the ``content_hash`` recorded without
a store; the ref the store returns (resolvable URI, ``byte_size``) goes into the ``LlmCall``, and
a ref whose hash or size does not match is refused. Without a store the behaviour, refs and
hashes are byte-identical to before.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from typing import Any, Protocol

from core.contracts.llm import LlmProviderDescriptor, LlmRequest, LlmResponse
from core.domain.base import ContentBlobRef, canonical_json, content_hash
from core.domain.research import LlmCall

__all__ = ["BlobSink", "ScriptedLLMProvider"]

_JSON = "application/json"


class BlobSink(Protocol):
    """Where call content is persisted; returns a resolvable ref to the stored bytes."""

    def put(self, data: bytes, media_type: str | None = None) -> ContentBlobRef: ...


def _blob(payload: Any) -> ContentBlobRef:
    digest = content_hash(payload)
    return ContentBlobRef(
        uri=f"memory://sha256/{digest}", sha256=digest, media_type="application/json"
    )


def _stored(payload: Any, store: BlobSink) -> ContentBlobRef:
    data = canonical_json(payload).encode("utf-8")
    ref = store.put(data, _JSON)
    if not isinstance(ref, ContentBlobRef) or ref.sha256 != content_hash(payload):
        raise RuntimeError("the content store returned a ref that does not match the content")
    if ref.byte_size is not None and ref.byte_size != len(data):
        raise RuntimeError("the content store returned a ref with the wrong byte size")
    return ref


class ScriptedLLMProvider:
    def __init__(
        self,
        outputs: Sequence[dict[str, Any]],
        *,
        clock: Callable[[], datetime] | None = None,
        store: BlobSink | None = None,
    ) -> None:
        self._outputs = list(outputs)
        self._store = store
        self._clock = clock or (lambda: datetime.now(UTC))
        self._calls: list[LlmCall] = []
        self._descriptor = LlmProviderDescriptor(
            name="hlens_llm_scripted",
            version="1.0.0",
            model="scripted",
            deterministic=True,
            network=False,
        )

    @property
    def descriptor(self) -> LlmProviderDescriptor:
        return self._descriptor

    @property
    def calls(self) -> tuple[LlmCall, ...]:
        return tuple(self._calls)

    def complete(self, request: LlmRequest) -> LlmResponse:
        if not self._outputs:
            raise RuntimeError("the scripted provider has no output left")
        output = self._outputs.pop(0)
        store = self._store

        def blob(payload: Any) -> ContentBlobRef:
            return _blob(payload) if store is None else _stored(payload, store)

        call = LlmCall(
            provider=self._descriptor.plugin_key,
            model=self._descriptor.model,
            prompt=blob(request.prompt),
            input=blob(request.model_dump(mode="json")["input"]),
            output=blob(output),
            called_at=self._clock(),
        )
        self._calls.append(call)
        return LlmResponse.model_validate({"output": output, "call": call.model_dump(mode="json")})

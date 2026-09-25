"""An offline, deterministic LLMProvider that replays scripted outputs (ADR-0040).

For tests and dry runs of the hypothesis generator: no network, no keys. Every call is recorded
as an ``LlmCall`` whose prompt / input / output are content-addressed (``memory://sha256/<hash>``).
A real network provider is a separate plugin (declared ``network=True``) and needs credentials
that are out of scope for the framework build.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from typing import Any

from core.contracts.llm import LlmProviderDescriptor, LlmRequest, LlmResponse
from core.domain.base import ContentBlobRef, content_hash
from core.domain.research import LlmCall

__all__ = ["ScriptedLLMProvider"]


def _blob(payload: Any) -> ContentBlobRef:
    digest = content_hash(payload)
    return ContentBlobRef(
        uri=f"memory://sha256/{digest}", sha256=digest, media_type="application/json"
    )


class ScriptedLLMProvider:
    def __init__(
        self,
        outputs: Sequence[dict[str, Any]],
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._outputs = list(outputs)
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
        call = LlmCall(
            provider=self._descriptor.plugin_key,
            model=self._descriptor.model,
            prompt=_blob(request.prompt),
            input=_blob(request.model_dump(mode="json")["input"]),
            output=_blob(output),
            called_at=self._clock(),
        )
        self._calls.append(call)
        return LlmResponse.model_validate({"output": output, "call": call.model_dump(mode="json")})

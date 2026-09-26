"""Retrievable LLM call content for the research loop (Phase 7, ADR-0040; 2026-09-26).

``LlmCall`` records only content-blob refs (``core.domain.research``); nothing guaranteed the
prompt / input / output behind them could be fetched again or matched their hashes (a known gap).
``ContentVerifiedLLM(inner, resolver)`` is an opt-in ``LLMProvider`` wrapper: after the inner
provider answers, the three refs of the call must resolve through ``resolver`` (e.g.
``infrastructure.content.LocalContentStore``; ``verify_llm_call`` re-hashes every blob and checks
its size) **and** the retrieved payloads must equal what was asked and answered — the prompt, the
request's input and the response's output, as JSON. Anything else raises ``ValueError``, which
``research.hypotheses.generator.from_llm`` and the hypothesis stage already treat as a rejected
draft (recorded, never registered). The provider must store each payload as its canonical JSON
(the convention of ``plugins.llm.ScriptedLLMProvider(store=...)``).

``compose_loop`` hands the resolver to the hypothesis stage, which verifies a reviewed draft's
call again when the draft is taken: content that vanished or changed between review and use stops
the round (fail closed). Without the wrapper nothing changes (record hashes unchanged).
"""

from __future__ import annotations

from typing import Any

from core.contracts.llm import LLMProvider, LlmProviderDescriptor, LlmRequest, LlmResponse
from core.domain.research import LlmCall
from infrastructure.content import ContentResolver, ContentStoreError, verify_llm_call

__all__ = ["ContentVerifiedLLM", "verify_call_content"]


def verify_call_content(
    call: LlmCall,
    resolver: ContentResolver,
    *,
    request: LlmRequest | None = None,
    response: LlmResponse | None = None,
) -> None:
    """Refs resolve and hash-match; with ``request`` / ``response`` the payloads must equal them."""
    try:
        content = verify_llm_call(call, resolver)
    except ContentStoreError as exc:
        raise ValueError(f"LLM call content is not retrievable: {exc}") from exc
    expected: dict[str, Any] = {}
    if request is not None:
        dumped = request.model_dump(mode="json")
        expected |= {"prompt": dumped["prompt"], "input": dumped["input"]}
    if response is not None:
        expected["output"] = response.model_dump(mode="json")["output"]
    for name, value in expected.items():
        if getattr(content, name) != value:
            raise ValueError(f"the stored LLM {name} is not what was exchanged")


class ContentVerifiedLLM:
    """``LLMProvider`` whose every call content is verified against ``resolver`` (module docs)."""

    def __init__(self, inner: LLMProvider, resolver: ContentResolver) -> None:
        self._inner = inner
        self._resolver = resolver

    @property
    def resolver(self) -> ContentResolver:
        return self._resolver

    @property
    def descriptor(self) -> LlmProviderDescriptor:
        return self._inner.descriptor

    def complete(self, request: LlmRequest) -> LlmResponse:
        response = self._inner.complete(request)
        verify_call_content(response.call, self._resolver, request=request, response=response)
        return response

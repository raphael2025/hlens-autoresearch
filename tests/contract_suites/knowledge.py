"""Provider-agnostic contract suite for ``KnowledgeProvider`` (ADR-0034, Phase 0.5).

A compliant provider: declares itself; answers every query with a ``KnowledgeResult`` whose items
all carry source and licence; honours every filter (terms AND, name prefix, minimum evidence,
statuses, limit); and, when it declares itself deterministic, answers equal queries with equal
``result_hash``.
"""

from __future__ import annotations

from collections.abc import Callable

from core.contracts.knowledge import (
    EVIDENCE_ORDER,
    KnowledgeProvider,
    KnowledgeProviderDescriptor,
    KnowledgeQuery,
    KnowledgeResult,
)
from core.domain.research import KnowledgeStatus

KnowledgeCheck = Callable[[KnowledgeProvider], None]


class KnowledgeSuiteFailure(AssertionError):
    """The provider violated the KnowledgeProvider contract."""


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise KnowledgeSuiteFailure(message)


def check_descriptor(provider: KnowledgeProvider) -> None:
    _require(type(provider.descriptor) is KnowledgeProviderDescriptor, "descriptor type is wrong")


def check_everything_has_provenance(provider: KnowledgeProvider) -> None:
    result = provider.search(KnowledgeQuery(limit=1000))
    _require(type(result) is KnowledgeResult, "search must return a KnowledgeResult")
    _require(bool(result.items), "the suite needs a provider with at least one item")
    for item in result.items:
        _require(bool(item.source.strip() and item.license.strip()), f"{item.name}: no provenance")


def check_filters(provider: KnowledgeProvider) -> None:
    everything = provider.search(KnowledgeQuery(limit=1000)).items
    first = everything[0]
    prefix = first.name.split("_")[0] + "_"
    by_prefix = provider.search(KnowledgeQuery(name_prefix=prefix, limit=1000)).items
    _require(all(i.name.startswith(prefix) for i in by_prefix), "name_prefix not honoured")
    _require(first in by_prefix, "name_prefix dropped a matching item")
    word = first.claim.split()[0]
    by_term = provider.search(KnowledgeQuery(terms=(word.upper(),), limit=1000)).items
    _require(first in by_term, "terms must match case-insensitively")
    floor = EVIDENCE_ORDER[-1]
    strong = provider.search(KnowledgeQuery(evidence_at_least=floor, limit=1000)).items
    _require(all(i.evidence_level is floor for i in strong), "evidence_at_least not honoured")
    status = provider.search(
        KnowledgeQuery(statuses=(KnowledgeStatus.SUPPORTED,), limit=1000)
    ).items
    _require(all(i.status is KnowledgeStatus.SUPPORTED for i in status), "statuses not honoured")
    _require(len(provider.search(KnowledgeQuery(limit=1)).items) <= 1, "limit not honoured")


def check_determinism(provider: KnowledgeProvider) -> None:
    if not provider.descriptor.deterministic:
        return
    query = KnowledgeQuery(terms=("return",), limit=50)
    _require(
        provider.search(query).result_hash == provider.search(query).result_hash,
        "a deterministic provider must answer equal queries equally",
    )


KNOWLEDGE_CHECKS: tuple[KnowledgeCheck, ...] = (
    check_descriptor,
    check_everything_has_provenance,
    check_filters,
    check_determinism,
)

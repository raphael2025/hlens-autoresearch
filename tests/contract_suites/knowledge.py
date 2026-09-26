"""Provider-agnostic contract suite for ``KnowledgeProvider`` (ADR-0034, Phase 0.5).

A compliant provider: declares itself; answers every query with a ``KnowledgeResult`` whose items
all carry source and licence; honours every filter (terms AND, name prefix, minimum evidence,
statuses, limit; since contract 2.2.0 ``tags_all`` AND and ``assets_any`` OR by exact token
equality, ADR-0055); and, when it declares itself deterministic, answers equal queries with equal
``result_hash``.
"""

from __future__ import annotations

import re
from collections.abc import Callable

from core.contracts.knowledge import (
    EVIDENCE_ORDER,
    KnowledgeProvider,
    KnowledgeProviderDescriptor,
    KnowledgeQuery,
    KnowledgeResult,
)
from core.domain.research import KNOWLEDGE_TOKEN_PATTERN, KnowledgeStatus

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


def _absent_tokens(carried: set[str]) -> list[str]:
    """Valid tokens no item carries: every proper prefix / suffix / infix of a carried token that
    is itself canonical, plus a fresh one — a substring or prefix matcher would hit them."""
    token = re.compile(KNOWLEDGE_TOKEN_PATTERN)
    candidates = {"zz_absent_token"}
    for value in carried:
        for start in range(len(value)):
            for end in range(start + 1, len(value) + 1):
                candidates.add(value[start:end])
    return sorted(c for c in candidates if token.fullmatch(c) and c not in carried)


def check_tag_and_asset_filters(provider: KnowledgeProvider) -> None:
    everything = provider.search(KnowledgeQuery(limit=1000)).items
    for item in everything:
        if item.tags:
            hits = provider.search(KnowledgeQuery(tags_all=item.tags, limit=1000)).items
            _require(item in hits, f"tags_all dropped {item.name}, which carries every tag")
            _require(
                all(set(item.tags) <= set(hit.tags) for hit in hits),
                "tags_all must require every requested tag",
            )
    tags = {tag for item in everything for tag in item.tags}
    assets = {asset for item in everything for asset in item.assets}
    for asset in sorted(assets):
        hits = provider.search(KnowledgeQuery(assets_any=(asset,), limit=1000)).items
        expected = tuple(item for item in everything if asset in item.assets)
        _require(hits == expected, f"assets_any=({asset!r},) must match exactly its carriers")
    if len(assets) >= 2:
        pair = tuple(sorted(assets)[:2])
        hits = provider.search(KnowledgeQuery(assets_any=pair, limit=1000)).items
        expected = tuple(item for item in everything if set(pair) & set(item.assets))
        _require(hits == expected, "assets_any must match any requested asset (OR)")
    for absent in _absent_tokens(tags):
        found = provider.search(KnowledgeQuery(tags_all=(absent,), limit=1000)).items
        _require(not found, f"tags_all=({absent!r},) matched no-carrier items (not exact)")
    for absent in _absent_tokens(assets):
        found = provider.search(KnowledgeQuery(assets_any=(absent,), limit=1000)).items
        _require(not found, f"assets_any=({absent!r},) matched no-carrier items (not exact)")


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
    check_tag_and_asset_filters,
    check_determinism,
)

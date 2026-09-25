"""Feature runner: structural truncation of every provider call (Phase 1 F4; ADR-0030 §1).

``run_feature(provider, spec, request)`` answers ``request`` with ``provider`` without ever
showing the provider an observation it may not use. For every evaluation time ``t`` the provider
receives a sub-request whose only observations are the visible set
``request.visible_at(t, spec.available_lag)`` — ``available_time + available_lag <= t`` (one
observation per key: the latest available), with ``knowledge_time <= knowledge_cutoff`` already
guaranteed by the request contract. Leakage therefore does not depend on the provider (ADR-0030
option A); the contract suite additionally checks providers called directly.

The provider is called once per evaluation time (F4-R1), so a run costs evaluation times x visible
prefix. Measured (G3-P) almost all of that is the contract's own per-call work on the sub-request:
the ``content_hash()`` that the provider's ``FeatureResult.build`` and ``check_answers`` each
compute over the whole prefix, plus the sub-request's validation; building the visible sets
incrementally here saved about 1 % and was not kept. Removing the rest needs a core change
(e.g. memoizing ``content_hash`` of frozen contracts), not a weaker check here.

Every sub-result must be exactly a valid ``FeatureResult`` that answers its sub-request
(``FeatureResult.check_answers``); the descriptor must declare the spec's hash and must not change
during the run. The returned result answers the full ``request`` and is built by the runner, so its
``request_hash`` / ``result_hash`` are those of the full request: equal to what a compliant provider
returns for the full request directly.

``run_feature`` does not check ``request.manifest_content_hash``: a result is a Research Dataset
result only when its request was built by ``infrastructure.feature.dataset.
feature_request_from_dataset`` (manifest loaded and verified, observations proven against it; G2
RT-6). A request from ``pit_feature_request`` is an ad-hoc / test run whose hash is a label.

Pure: no catalog, no clock, no randomness.
"""

from __future__ import annotations

from pydantic import ValidationError

from core.contracts.feature import (
    FeatureProvider,
    FeatureRequest,
    FeatureResult,
    FeatureValue,
    ProviderDescriptor,
    UnsupportedFeature,
)
from core.domain.specs import FeatureSpec

__all__ = ["FeatureRunnerError", "run_feature"]


class FeatureRunnerError(Exception):
    """The run cannot produce an honest result (spec / request mismatch, a non-compliant answer)."""


def _descriptor(provider: FeatureProvider) -> ProviderDescriptor:
    raw = provider.descriptor
    if type(raw) is not ProviderDescriptor:
        raise FeatureRunnerError("the provider descriptor is not a ProviderDescriptor")
    try:
        return ProviderDescriptor.model_validate_json(raw.model_dump_json())
    except (ValidationError, ValueError) as exc:
        raise FeatureRunnerError(f"the provider descriptor is invalid: {exc}") from None


def _answer(
    provider: FeatureProvider,
    descriptor: ProviderDescriptor,
    spec: FeatureSpec,
    sub: FeatureRequest,
) -> tuple[FeatureValue, ...]:
    raw = provider.compute(sub)
    if type(raw) is not FeatureResult:
        raise FeatureRunnerError("the provider did not return a FeatureResult")
    try:
        result = FeatureResult.model_validate_json(raw.model_dump_json())
        result.check_answers(sub, descriptor, spec.available_lag)
    except (ValidationError, ValueError) as exc:
        raise FeatureRunnerError(f"the provider's answer is not compliant: {exc}") from None
    return result.values


def run_feature(
    provider: FeatureProvider, spec: FeatureSpec, request: FeatureRequest
) -> FeatureResult:
    """Answer ``request`` for ``spec`` with ``provider``: one truncated sub-request per time."""
    if not isinstance(spec, FeatureSpec) or not isinstance(request, FeatureRequest):
        raise FeatureRunnerError("run_feature needs a FeatureSpec and a FeatureRequest")
    if not spec.deterministic:
        raise FeatureRunnerError(f"{spec.ref} is not declared deterministic")
    spec_hash = spec.content_hash()
    if request.feature != spec.ref or request.spec_hash != spec_hash:
        raise FeatureRunnerError(f"the request is not for {spec.ref} with this spec hash")
    descriptor = _descriptor(provider)
    if not descriptor.supports(spec.ref, spec_hash):
        raise UnsupportedFeature(f"{descriptor.plugin_key} does not declare {spec.ref}")

    lag = spec.available_lag
    values: list[FeatureValue] = []
    # One evaluation time per call (F4-R1): a provider can never let one time's value depend on
    # which other times it was asked about; each call sees only that time's visible prefix.
    for at in request.evaluation_times:
        sub = FeatureRequest(
            feature=request.feature,
            spec_hash=request.spec_hash,
            manifest_content_hash=request.manifest_content_hash,
            knowledge_cutoff=request.knowledge_cutoff,
            evaluation_times=(at,),
            observations=request.visible_at(at, lag),
        )
        values.extend(_answer(provider, descriptor, spec, sub))

    if _descriptor(provider) != descriptor:
        raise FeatureRunnerError("the provider descriptor changed during the run")
    return FeatureResult.build(request, descriptor, values)

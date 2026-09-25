"""State runner: structural truncation of every provider call (Phase 2; ADR-0035 §1).

``run_state(provider, spec, request)`` answers ``request`` with ``provider`` without ever showing
the provider a feature value it may not use. For every evaluation time ``t`` the provider receives a
sub-request whose only inputs are ``request.visible_at(t, spec.training_window)``: feature values
evaluated at or before ``t`` and, for a trained model, inside the fixed trailing window
``(t - training_window, t]``. A provider therefore cannot look ahead, and cannot fit on the full
sample: the widest history it can ever see is the declared window.

Inputs are feature values, never outcomes: ``StateInput.feature`` is a ``kind=feature`` reference
by contract, and ``state_inputs`` builds inputs only from ``(FeatureRequest, FeatureResult)`` pairs
whose result answers its request (``request_hash``), binding each value to its ``result_hash``.

Every sub-result must be exactly a valid ``StateResult`` that answers its sub-request
(``StateResult.check_answers``); the descriptor must declare the spec's hash and must not change
during the run. A trained spec (``training_window`` set) must fix its ``seed``.

The provider is called once per evaluation time, so a run costs evaluation times x visible prefix
(bounded by the training window when there is one). Pure: no catalog, no clock, no randomness.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from datetime import datetime

from pydantic import ValidationError

from core.contracts.feature import FeatureRequest, FeatureResult
from core.contracts.state import (
    StateInput,
    StateProvider,
    StateProviderDescriptor,
    StateRequest,
    StateResult,
    StateValue,
    UnsupportedState,
)
from core.domain.specs import StateSpec

__all__ = ["StateRunnerError", "run_state", "state_inputs", "state_request"]


class StateRunnerError(Exception):
    """The run cannot produce an honest result (spec / request mismatch, a non-compliant answer)."""


def state_inputs(pairs: Iterable[tuple[FeatureRequest, FeatureResult]]) -> tuple[StateInput, ...]:
    """Feature values as state inputs: one ``StateInput`` per value of each answered request.

    Each result must answer its request (same ``request_hash``, one value per evaluation time); the
    feature reference is the request's, the lineage is the result's ``result_hash``.
    """
    out: list[StateInput] = []
    for request, result in pairs:
        if not isinstance(request, FeatureRequest) or not isinstance(result, FeatureResult):
            raise StateRunnerError("state inputs need (FeatureRequest, FeatureResult) pairs")
        if result.request_hash != request.content_hash():
            raise StateRunnerError(f"a {request.feature} result does not answer its request")
        if tuple(item.evaluation_time for item in result.values) != request.evaluation_times:
            raise StateRunnerError(f"a {request.feature} result is not one value per time")
        out.extend(
            StateInput(
                feature=request.feature,
                evaluation_time=item.evaluation_time,
                value=item.value,
                source_result_hash=result.result_hash,
            )
            for item in result.values
        )
    return tuple(out)


def state_request(
    spec: StateSpec, evaluation_times: Sequence[datetime], inputs: Iterable[StateInput]
) -> StateRequest:
    """A request for ``spec`` (its ref and content hash) over the given inputs."""
    return StateRequest(
        state=spec.ref,
        spec_hash=spec.content_hash(),
        evaluation_times=tuple(evaluation_times),
        inputs=tuple(inputs),
    )


def _descriptor(provider: StateProvider) -> StateProviderDescriptor:
    raw = provider.descriptor
    if type(raw) is not StateProviderDescriptor:
        raise StateRunnerError("the provider descriptor is not a StateProviderDescriptor")
    try:
        return StateProviderDescriptor.model_validate_json(raw.model_dump_json())
    except (ValidationError, ValueError) as exc:
        raise StateRunnerError(f"the provider descriptor is invalid: {exc}") from None


def _answer(
    provider: StateProvider,
    descriptor: StateProviderDescriptor,
    spec: StateSpec,
    sub: StateRequest,
) -> tuple[StateValue, ...]:
    raw = provider.compute(sub)
    if type(raw) is not StateResult:
        raise StateRunnerError("the provider did not return a StateResult")
    try:
        result = StateResult.model_validate_json(raw.model_dump_json())
        result.check_answers(sub, descriptor, spec)
    except (ValidationError, ValueError) as exc:
        raise StateRunnerError(f"the provider's answer is not compliant: {exc}") from None
    return result.values


def _check_spec(spec: StateSpec, request: StateRequest) -> None:
    if spec.training_window is not None and spec.seed is None:
        raise StateRunnerError(
            f"{spec.ref} is trained (training_window set) but does not fix its seed"
        )
    if request.state != spec.ref or request.spec_hash != spec.content_hash():
        raise StateRunnerError(f"the request is not for {spec.ref} with this spec hash")
    declared = set(spec.features)
    stray = sorted({str(item.feature) for item in request.inputs if item.feature not in declared})
    if stray:
        raise StateRunnerError(f"inputs outside {spec.ref}'s declared features: {stray}")


def run_state(provider: StateProvider, spec: StateSpec, request: StateRequest) -> StateResult:
    """Answer ``request`` for ``spec`` with ``provider``: one truncated sub-request per time."""
    if not isinstance(spec, StateSpec) or not isinstance(request, StateRequest):
        raise StateRunnerError("run_state needs a StateSpec and a StateRequest")
    _check_spec(spec, request)
    descriptor = _descriptor(provider)
    if not descriptor.supports(spec.ref, spec.content_hash()):
        raise UnsupportedState(f"{descriptor.plugin_key} does not declare {spec.ref}")

    values: list[StateValue] = []
    # One evaluation time per call: a state can never depend on which other times were asked,
    # and each call sees only that time's visible (and windowed) inputs.
    for at in request.evaluation_times:
        sub = StateRequest(
            state=request.state,
            spec_hash=request.spec_hash,
            evaluation_times=(at,),
            inputs=request.visible_at(at, spec.training_window),
        )
        values.extend(_answer(provider, descriptor, spec, sub))

    if _descriptor(provider) != descriptor:
        raise StateRunnerError("the provider descriptor changed during the run")
    return StateResult.build(request, descriptor, values)

"""Event runner: structural truncation and point-in-time consistency (Phase 3; ADR-0036 §3).

``run_events(provider, spec, request)`` answers ``request`` (the event table as of
``request.as_of``) with ``provider`` without ever showing the provider an input it may not use.
For every checkpoint ``t`` the provider receives the sub-request ``request.truncated(t, lag)``:
``as_of = t`` and only the inputs with ``available_time + observable_lag <= t`` and the upstream
events with ``event_time + observable_lag <= t``. Leakage therefore does not depend on the
provider, exactly as for features (ADR-0030 option A).

Every sub-result must be exactly a valid ``EventResult`` that answers its sub-request
(``EventResult.check_answers``: every event cites only inputs visible at its ``event_time`` and its
``event_time`` is the observable time of those inputs). Consecutive checkpoints must agree: the
table as of ``t`` must be exactly the table as of a later checkpoint restricted to
``event_time <= t``. An event that appears only later with an earlier ``event_time`` is a
back-dated ("future confirmed") event; one that disappears is a retracted event. Both fail closed
(``FutureConfirmationError``).

The default checkpoints are every distinct time at which the visible set changes (each input's
``available_time + lag`` and each upstream event's ``event_time + lag`` up to ``as_of``) plus
``as_of`` itself, so every event is checked at its own ``event_time`` with only the data visible
then. A caller may pass a coarser grid (cheaper, weaker: back-dating between two checkpoints is
then invisible to the runner).

The returned result answers the full ``request`` and is built by the runner; its hashes equal what
a compliant provider returns for the full request directly.

Before any checkpoint the runner verifies what it is given against what the spec declares
(``infrastructure.event.upstream``; ADR-0036 implementation note of 2026-09-26): an interaction
(a spec whose ``lineage`` names upstream events, or a request carrying upstream events) needs its
``upstream_specs`` — exactly the declared ones, each bound by its spec hash in the interaction's
trigger, the interaction's Feature / State inputs equal to their union, every upstream event of
one of them with its hash, and (with ``upstream_results``) every upstream event taken from those
results. With ``feature_runs`` / ``state_runs`` every input point must be exactly the point the
adapter recomputes from its run (per-point ``source_lineage_hash``). Any mismatch raises
``UpstreamVerificationError`` (fail closed).

Pure: no catalog, no clock, no randomness.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from itertools import pairwise

from pydantic import ValidationError

from core.contracts.event import (
    Event,
    EventProvider,
    EventProviderDescriptor,
    EventRequest,
    EventResult,
    UnsupportedEvent,
)
from core.contracts.feature import FeatureRequest, FeatureResult
from core.contracts.state import StateRequest, StateResult
from core.domain.specs import EventSpec
from infrastructure.event.errors import EventRunnerError, FutureConfirmationError
from infrastructure.event.upstream import (
    UpstreamVerificationError,
    verify_input_lineage,
    verify_interaction,
)

__all__ = [
    "EventRunnerError",
    "FutureConfirmationError",
    "UpstreamVerificationError",
    "default_checkpoints",
    "run_events",
]


def _descriptor(provider: EventProvider) -> EventProviderDescriptor:
    raw = provider.descriptor
    if type(raw) is not EventProviderDescriptor:
        raise EventRunnerError("the provider descriptor is not an EventProviderDescriptor")
    try:
        return EventProviderDescriptor.model_validate_json(raw.model_dump_json())
    except (ValidationError, ValueError) as exc:
        raise EventRunnerError(f"the provider descriptor is invalid: {exc}") from None


def default_checkpoints(request: EventRequest, spec: EventSpec) -> tuple[datetime, ...]:
    """Every time the visible set changes up to ``as_of``, plus ``as_of`` (ascending, unique)."""
    lag = spec.observable_lag
    times = {item.available_time + lag for item in request.inputs}
    times |= {item.event_time + lag for item in request.upstream_events}
    times = {at for at in times if at <= request.as_of}
    times.add(request.as_of)
    return tuple(sorted(times))


def _grid(
    request: EventRequest, spec: EventSpec, checkpoints: Sequence[datetime] | None
) -> tuple[datetime, ...]:
    if checkpoints is None:
        return default_checkpoints(request, spec)
    grid = list(checkpoints)
    if any(at.tzinfo is None for at in grid):
        raise EventRunnerError("checkpoints must be timezone-aware UTC times")
    if any(later <= earlier for earlier, later in pairwise(grid)):
        raise EventRunnerError("checkpoints must be strictly ascending")
    if grid and grid[-1] > request.as_of:
        raise EventRunnerError("checkpoints must not be later than as_of")
    if not grid or grid[-1] != request.as_of:
        grid.append(request.as_of)
    return tuple(grid)


def _answer(
    provider: EventProvider,
    descriptor: EventProviderDescriptor,
    spec: EventSpec,
    sub: EventRequest,
) -> tuple[Event, ...]:
    raw = provider.detect(sub)
    if type(raw) is not EventResult:
        raise EventRunnerError("the provider did not return an EventResult")
    try:
        result = EventResult.model_validate_json(raw.model_dump_json())
        result.check_answers(sub, descriptor, spec.observable_lag)
    except (ValidationError, ValueError) as exc:
        raise EventRunnerError(f"the provider's answer is not compliant: {exc}") from None
    return result.events


def _require_consistent(
    earlier_at: datetime, earlier: tuple[Event, ...], later_at: datetime, later: tuple[Event, ...]
) -> None:
    restricted = tuple(item for item in later if item.event_time <= earlier_at)
    if restricted == earlier:
        return
    before = {item.event_id for item in earlier}
    after = {item.event_id for item in restricted}
    backdated = sorted(after - before)
    retracted = sorted(before - after)
    raise FutureConfirmationError(
        f"the table as of {later_at.isoformat()} disagrees with the table as of "
        f"{earlier_at.isoformat()}: back-dated {[item[:12] for item in backdated]}, "
        f"retracted {[item[:12] for item in retracted]}"
    )


def run_events(
    provider: EventProvider,
    spec: EventSpec,
    request: EventRequest,
    *,
    checkpoints: Sequence[datetime] | None = None,
    upstream_specs: Sequence[EventSpec] | None = None,
    upstream_results: Sequence[EventResult] | None = None,
    feature_runs: Sequence[tuple[FeatureRequest, FeatureResult]] = (),
    state_runs: Sequence[tuple[StateRequest, StateResult]] = (),
) -> EventResult:
    """Answer ``request`` for ``spec``: one truncated sub-request per checkpoint.

    ``upstream_specs`` is required for an interaction; ``upstream_results``, ``feature_runs``
    and ``state_runs`` are optional, stricter provenance (see the module docstring).
    """
    if not isinstance(spec, EventSpec) or not isinstance(request, EventRequest):
        raise EventRunnerError("run_events needs an EventSpec and an EventRequest")
    spec_hash = spec.content_hash()
    if request.event != spec.ref or request.spec_hash != spec_hash:
        raise EventRunnerError(f"the request is not for {spec.ref} with this spec hash")
    verify_interaction(spec, request, upstream_specs, upstream_results)
    verify_input_lineage(request, feature_runs, state_runs)
    descriptor = _descriptor(provider)
    if not descriptor.supports(spec.ref, spec_hash):
        raise UnsupportedEvent(f"{descriptor.plugin_key} does not declare {spec.ref}")

    lag = spec.observable_lag
    previous: tuple[datetime, tuple[Event, ...]] | None = None
    for at in _grid(request, spec, checkpoints):
        events = _answer(provider, descriptor, spec, request.truncated(at, lag))
        if previous is not None:
            _require_consistent(previous[0], previous[1], at, events)
        previous = (at, events)

    if _descriptor(provider) != descriptor:
        raise EventRunnerError("the provider descriptor changed during the run")
    if previous is None:  # pragma: no cover - the grid always ends with as_of
        raise EventRunnerError("no checkpoint was evaluated")
    result = EventResult.build(request, descriptor, previous[1])
    result.check_answers(request, descriptor, lag)
    return result

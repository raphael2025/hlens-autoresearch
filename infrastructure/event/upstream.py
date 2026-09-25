"""Runner-side upstream verification (Phase 3; ADR-0036 §5 and its 2026-09-26 implementation note).

Two checks close the ADR-0036 honesty boundary for what a run is actually given; both fail closed
with ``UpstreamVerificationError`` (an ``EventRunnerError``), never by dropping anything.

``verify_interaction(spec, request, upstream_specs, upstream_results=None)`` — an **interaction**
spec (one whose ``lineage`` names ``kind=event`` refs, or whose request carries upstream events):

- the supplied upstream specs are exactly the declared upstream event refs (``lineage``), one spec
  per ref — none missing, none extra;
- the interaction binds each one's spec hash: ADR-0036 §5 has the interaction's ``trigger`` bind
  "the upstream refs, the upstream spec hashes and the window", so the supplied spec's
  ``content_hash()`` must occur in the trigger text (a 256-bit content hash; a spec of another
  content under the same ref has another hash and is refused). Format-agnostic: no trigger
  convention of any provider is parsed;
- every upstream event of the request is of a declared upstream spec and carries that spec's hash;
- the interaction's declared Feature / State inputs **equal** the union of the upstream specs'
  (not "a superset"): ADR-0036 §5 and 02-domain §2.8 define the interaction's inputs as the union
  of its upstream specs' features / states ("交互规格声明上游规格的 Feature / State 并集为（传递）
  输入"; honesty boundary: "是否与上游规格一致"), and an interaction consumes only upstream events,
  so a Feature / State beyond the union would be a declared input no computation can use — a
  false lineage claim;
- with ``upstream_results``: every upstream event of the request is one of those results' events
  (identical), and every supplied result holds only events of declared upstream specs.

A non-interaction spec must receive neither upstream events nor upstream specs.

``verify_input_lineage(request, feature_runs=(), state_runs=())`` — given the upstream feature /
state runs the inputs were built from, every input point of the request must be exactly the point
the adapter (``inputs_from_feature_run`` / ``state_series_from_state_run``) recomputes from its
run: same value, times and per-point ``source_lineage_hash``. A point whose source has no supplied
run, a point the run does not produce, a forged lineage hash or value, a result that does not
answer its request, or two runs of one source are refused. Without runs nothing is checked (the
local ``StateSeriesPoint`` shape carries caller-computed lineage; that remains the caller's /
Registry's responsibility).

Pure: no catalog, no clock, no plugin import.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from datetime import datetime

from core.contracts.event import Event, EventInputPoint, EventRequest, EventResult
from core.contracts.feature import FeatureRequest, FeatureResult
from core.contracts.state import StateRequest, StateResult
from core.domain.base import Kind, Ref
from core.domain.specs import EventSpec
from infrastructure.event.errors import EventRunnerError
from infrastructure.event.inputs import (
    inputs_from_feature_run,
    inputs_from_state_series,
    state_series_from_state_run,
)

__all__ = [
    "UpstreamVerificationError",
    "declared_upstream",
    "is_interaction",
    "verify_input_lineage",
    "verify_interaction",
]


class UpstreamVerificationError(EventRunnerError):
    """What the run is given does not match what the spec declares (fail closed)."""


def declared_upstream(spec: EventSpec) -> tuple[Ref, ...]:
    """The upstream event refs an event spec declares (its ``kind=event`` lineage refs)."""
    return tuple(ref for ref in spec.lineage if ref.kind is Kind.EVENT)


def is_interaction(spec: EventSpec, request: EventRequest) -> bool:
    """Whether the run involves upstream events (declared by the spec or carried by the request)."""
    return bool(declared_upstream(spec)) or bool(request.upstream_events)


def _keys(refs: Iterable[Ref]) -> frozenset[str]:
    return frozenset(str(ref) for ref in refs)


def _upstream_specs(
    spec: EventSpec, upstream_specs: Sequence[EventSpec]
) -> dict[str, tuple[EventSpec, str]]:
    declared = declared_upstream(spec)
    if len(_keys(declared)) != len(declared):
        raise UpstreamVerificationError(f"{spec.ref} declares an upstream event ref twice")
    supplied: dict[str, tuple[EventSpec, str]] = {}
    for item in upstream_specs:
        if not isinstance(item, EventSpec):
            raise UpstreamVerificationError("upstream specs must be EventSpec instances")
        key = str(item.ref)
        if key in supplied:
            raise UpstreamVerificationError(f"upstream spec {key} is supplied twice")
        supplied[key] = (item, item.content_hash())
    missing = sorted(_keys(declared) - supplied.keys())
    extra = sorted(supplied.keys() - _keys(declared))
    if missing:
        raise UpstreamVerificationError(f"{spec.ref}: no upstream spec supplied for {missing}")
    if extra:
        raise UpstreamVerificationError(f"{spec.ref} does not declare the upstream specs {extra}")
    for key, (_, spec_hash) in supplied.items():
        if spec_hash not in spec.trigger:
            raise UpstreamVerificationError(
                f"{spec.ref} does not bind the supplied upstream spec {key} (spec hash "
                f"{spec_hash[:12]} is not in its trigger)"
            )
    return supplied


def _require_input_union(spec: EventSpec, upstream: Iterable[EventSpec]) -> None:
    specs = tuple(upstream)
    for label, declared, union in (
        ("features", _keys(spec.features), _keys(r for item in specs for r in item.features)),
        ("states", _keys(spec.states), _keys(r for item in specs for r in item.states)),
    ):
        if declared != union:
            raise UpstreamVerificationError(
                f"{spec.ref} declares {label} {sorted(declared)} but its upstream specs declare "
                f"{sorted(union)} (an interaction's inputs are exactly their union)"
            )


def _require_declared_events(
    spec: EventSpec, events: Iterable[Event], supplied: dict[str, tuple[EventSpec, str]]
) -> None:
    for item in events:
        entry = supplied.get(str(item.event))
        if entry is None:
            raise UpstreamVerificationError(
                f"{spec.ref} does not declare the upstream event {item.event}"
            )
        if item.spec_hash != entry[1]:
            raise UpstreamVerificationError(
                f"upstream event {item.event_id[:12]} of {item.event} has spec hash "
                f"{item.spec_hash[:12]}, not the supplied spec's {entry[1][:12]}"
            )


def verify_interaction(
    spec: EventSpec,
    request: EventRequest,
    upstream_specs: Sequence[EventSpec] | None,
    upstream_results: Sequence[EventResult] | None = None,
) -> None:
    """Check an event run against its supplied upstream specs / results (module docstring)."""
    if not is_interaction(spec, request):
        if upstream_specs or upstream_results:
            raise UpstreamVerificationError(
                f"{spec.ref} declares no upstream events but upstream specs / results were supplied"
            )
        return
    if not declared_upstream(spec):
        raise UpstreamVerificationError(
            f"{spec.ref} declares no upstream events but the request carries some"
        )
    if upstream_specs is None:
        raise UpstreamVerificationError(
            f"{spec.ref} is an interaction: its upstream specs must be supplied to the run"
        )
    supplied = _upstream_specs(spec, upstream_specs)
    _require_input_union(spec, (item for item, _ in supplied.values()))
    _require_declared_events(spec, request.upstream_events, supplied)
    if upstream_results is None:
        return
    known: dict[str, Event] = {}
    for result in upstream_results:
        if not isinstance(result, EventResult):
            raise UpstreamVerificationError("upstream results must be EventResult instances")
        _require_declared_events(spec, result.events, supplied)
        known.update((item.event_id, item) for item in result.events)
    for item in request.upstream_events:
        if known.get(item.event_id) != item:
            raise UpstreamVerificationError(
                f"upstream event {item.event_id[:12]} of {item.event} is in none of the supplied "
                "upstream results"
            )


def _expected_points(
    feature_runs: Sequence[tuple[FeatureRequest, FeatureResult]],
    state_runs: Sequence[tuple[StateRequest, StateResult]],
) -> dict[str, dict[datetime, EventInputPoint]]:
    runs: list[tuple[Ref, tuple[EventInputPoint, ...]]] = []
    try:
        for feature_request, feature_result in feature_runs:
            runs.append(
                (feature_request.feature, inputs_from_feature_run(feature_request, feature_result))
            )
        for state_request, state_result in state_runs:
            series = state_series_from_state_run(state_request, state_result)
            runs.append(
                (state_request.state, inputs_from_state_series(state_request.state, series))
            )
    except ValueError as exc:
        raise UpstreamVerificationError(f"an upstream run is not usable: {exc}") from None
    by_source: dict[str, dict[datetime, EventInputPoint]] = {}
    for source, points in runs:
        key = str(source)
        if key in by_source:
            raise UpstreamVerificationError(f"two upstream runs are supplied for {key}")
        by_source[key] = {item.evaluation_time: item for item in points}
    return by_source


def verify_input_lineage(
    request: EventRequest,
    feature_runs: Sequence[tuple[FeatureRequest, FeatureResult]] = (),
    state_runs: Sequence[tuple[StateRequest, StateResult]] = (),
) -> None:
    """Every input point must be recomputed exactly from a supplied run (module docstring)."""
    if not feature_runs and not state_runs:
        return
    expected = _expected_points(feature_runs, state_runs)
    for item in request.inputs:
        label = f"input {item.source} @ {item.evaluation_time.isoformat()}"
        series = expected.get(str(item.source))
        if series is None:
            raise UpstreamVerificationError(f"{label}: no upstream run was supplied for its source")
        recomputed = series.get(item.evaluation_time)
        if recomputed is None:
            raise UpstreamVerificationError(f"{label}: the supplied upstream run has no such point")
        if item.source_lineage_hash != recomputed.source_lineage_hash:
            raise UpstreamVerificationError(
                f"{label}: source_lineage_hash {item.source_lineage_hash[:12]} is not the "
                f"recomputed per-point lineage {recomputed.source_lineage_hash[:12]}"
            )
        if item != recomputed:
            raise UpstreamVerificationError(
                f"{label}: value / times differ from the supplied upstream run"
            )

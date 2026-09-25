"""Event inputs from upstream runs (Phase 3; ADR-0036 §1).

- ``inputs_from_feature_run``: one ``EventInputPoint`` per ``FeatureValue`` of a feature run. A
  feature value at evaluation time ``t`` uses only observations visible at ``t`` (ADR-0030), so it
  is observable at ``t``: ``available_time = evaluation_time``. The feature ref comes from the
  request, and the result must answer that request (``request_hash``).
- ``feature_value_lineage``: a point's ``source_lineage_hash`` — the feature, its spec hash, the
  dataset manifest, the provider identity and **that time's** ``FeatureValue``. It is deliberately
  not the run's ``result_hash``: that hash covers every later value too, so a past point (and every
  event citing it) would change identity when future data changes.
- ``inputs_from_state_series``: the **minimal local state-series shape** of Phase 3. Phase 2's
  ``StateProvider`` is built in parallel; until it is wired, a state series is given as
  ``StateSeriesPoint``s (``evaluation_time``, ``available_time``, ``label``, per-point
  ``lineage_hash``). WIRING POINT (Phase 2): add an adapter from the Phase 2 state result DTO (state
  ref from its request, ``available_time`` from its values, a per-point causal lineage like
  ``feature_value_lineage``) to ``StateSeriesPoint`` / ``EventInputPoint``; nothing else in the
  event engine changes.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime

from core.contracts.event import EventInputPoint
from core.contracts.feature import FeatureRequest, FeatureResult, FeatureValue
from core.domain.base import Kind, Ref, content_hash

__all__ = [
    "StateSeriesPoint",
    "feature_value_lineage",
    "inputs_from_feature_run",
    "inputs_from_state_series",
]


def feature_value_lineage(
    request: FeatureRequest, result: FeatureResult, value: FeatureValue
) -> str:
    """The causal lineage of one feature value (see the module docstring)."""
    return content_hash(
        {
            "feature": str(request.feature),
            "spec_hash": request.spec_hash,
            "manifest_content_hash": request.manifest_content_hash,
            "knowledge_cutoff": request.knowledge_cutoff.isoformat(),
            "provider": result.provider,
            "provider_hash": result.provider_hash,
            "value": value.model_dump(mode="json"),
        }
    )


def inputs_from_feature_run(
    request: FeatureRequest, result: FeatureResult
) -> tuple[EventInputPoint, ...]:
    """The feature series of one run as event inputs (``None`` values are kept: not computable)."""
    if result.request_hash != request.content_hash():
        raise ValueError("the feature result does not answer this request")
    return tuple(
        EventInputPoint(
            source=request.feature,
            source_lineage_hash=feature_value_lineage(request, result, item),
            evaluation_time=item.evaluation_time,
            available_time=item.evaluation_time,
            value=item.value,
        )
        for item in result.values
    )


@dataclass(frozen=True, slots=True)
class StateSeriesPoint:
    """One point of a state series (the local Phase 3 shape; see the module docstring).

    ``label`` is a state name of the ``StateSpec.state_space`` or ``None`` (not computable);
    ``lineage_hash`` identifies where the point came from and must depend only on information
    known at ``available_time``.
    """

    evaluation_time: datetime
    available_time: datetime
    label: str | None
    lineage_hash: str


def inputs_from_state_series(
    state: Ref, points: Iterable[StateSeriesPoint]
) -> tuple[EventInputPoint, ...]:
    """A state series as event inputs."""
    if state.kind is not Kind.STATE:
        raise ValueError(f"{state} is not a state reference")
    return tuple(
        EventInputPoint(
            source=state,
            source_lineage_hash=item.lineage_hash,
            evaluation_time=item.evaluation_time,
            available_time=item.available_time,
            value=item.label,
        )
        for item in points
    )

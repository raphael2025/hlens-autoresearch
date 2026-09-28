"""Point-in-time wiring: an answered ``FeatureResult`` -> ``VolScaledTripleBarrierOutcome``'s
volatility constructor argument (ADR-0088 decision 5 follow-up; ``docs/research/outcome-library.md``
gap O-3).

``VolScaledTripleBarrierOutcome`` (``plugins/outcomes/vol_scaled_triple_barrier.py``) takes the
resolved per-event volatility values as a constructor argument rather than reading them from the
frozen ``OutcomeRequest``: ADR-0088 only extended ``OutcomeLabelSpec`` with a ``Ref[FEATURE]``
naming *which* feature to use, not a data channel for its values (that wiring was explicitly left
to a later plugins/research batch — this module is it). Given the ``OutcomeRequest`` (whose
``label_spec.volatility_feature`` names the feature) and the matching, already-answered
``FeatureRequest`` / ``FeatureResult`` pair for that feature, ``select_volatility_for_entry_times``
selects, for every event, the point-in-time value of that feature visible at the event's *entry
time* — not its ``event_time``; the entry bar is what the label actually anchors to
(``plugins.outcomes._window.label_window``'s own definition, reused here so the two modules can
never disagree about which bar is the entry) — and returns the ``Mapping[event_key, Decimal |
None]`` the Provider expects.

**Point-in-time selection**: a ``FeatureValue`` is available starting at its own
``evaluation_time`` — the same convention ``infrastructure/strategy/signals.py``'s
``signals_from_features`` already uses (``available_time = evaluation_time`` in the
``SignalObservation`` it builds). So the value used for an event is the value at the latest
``evaluation_time <= entry_time`` among ``feature_result.values`` (already strictly ascending by
contract, ``core/contracts/feature.py``): the same "PIT replacement, not append" rule the contract
states for observations, applied here to a result's values.

**Entry time, not event time**: an event whose entry would be delayed by a whole bar or more (or
that has no bar at/after ``event_time`` at all) has no entry bar (``label_window`` returns
``None``); its label is ``unknown`` regardless of volatility, so this module maps it straight to
``None`` without a feature lookup.

**Missing is ``None``, never filled**: no feature value with ``evaluation_time <= entry_time``
exists, or the selected value is itself ``None`` (the feature was not computable at that point) ->
``None`` — exactly ``VolScaledTripleBarrierOutcome``'s "not visible" convention (ADR-0088 collapses
"not yet visible" and "absent" into the same case; see that provider's module docstring). This
module never raises for a missing value: the Provider treats it as an ordinary unresolved label.

**Fail closed on a wiring mismatch**: ``feature_request.feature`` must be the same *target*
(``kind``, ``name``, ``version`` — ``Ref.target_identity()``, ADR-0018 §D-26.5) as
``label_spec.volatility_feature``, and ``feature_result.request_hash`` must match
``feature_request.content_hash()`` (the result must actually answer the given request); either
mismatch raises ``VolatilityWiringError`` rather than silently pairing the wrong feature with the
label spec. A feature value that is present but not a ``Decimal`` (an ``int`` / ``bool`` slipped
into a channel meant for a real-valued volatility feature) is malformed input, not "missing", so it
also raises rather than being coerced or silently dropped.

Outcome labels are never fed back as inputs (Constitution C-L2): nothing in this module reads an
``OutcomeLabel`` or ``OutcomeResult``, only the request/result pair of the *feature* the label spec
names.
"""

from __future__ import annotations

from bisect import bisect_right
from collections.abc import Mapping
from decimal import Decimal

from core.contracts.feature import FeatureRequest, FeatureResult
from core.contracts.outcome import OutcomeMethod, OutcomeRequest
from plugins.outcomes._window import label_window

__all__ = ["VolatilityWiringError", "select_volatility_for_entry_times"]


class VolatilityWiringError(ValueError):
    """The feature request/result do not honestly answer the label spec's ``volatility_feature``,
    or a selected value is not a real-valued (``Decimal``) volatility; fail closed."""


def select_volatility_for_entry_times(
    outcome_request: OutcomeRequest,
    feature_request: FeatureRequest,
    feature_result: FeatureResult,
) -> Mapping[str, Decimal | None]:
    """``event_key -> Decimal | None``, ready to pass as ``VolScaledTripleBarrierOutcome``'s
    ``volatility=`` constructor argument for exactly ``outcome_request``.

    See the module docstring for the point-in-time selection rule and the fail-closed checks. Does
    not read or require ``outcome_request.label_spec.upper_barrier`` / ``lower_barrier`` (both must
    already be absent for this method, enforced by the contract) or anything about outcome values —
    only ``label_spec.volatility_feature``, ``label_spec.horizon`` and ``outcome_request.bars`` /
    ``events`` (to find each event's entry time) plus ``feature_result.values`` (to select from).
    """
    label_spec = outcome_request.label_spec
    if label_spec.method is not OutcomeMethod.VOL_SCALED_TRIPLE_BARRIER:
        raise VolatilityWiringError(
            f"label_spec.method is {label_spec.method.value}, not "
            f"{OutcomeMethod.VOL_SCALED_TRIPLE_BARRIER.value}"
        )
    target = label_spec.volatility_feature
    if target is None:  # pragma: no cover — the contract already requires it for this method
        raise VolatilityWiringError("label_spec has no volatility_feature")
    if feature_request.feature.target_identity() != target.target_identity():
        raise VolatilityWiringError(
            f"feature_request.feature {feature_request.feature} does not match "
            f"label_spec.volatility_feature {target}"
        )
    if feature_result.request_hash != feature_request.content_hash():
        raise VolatilityWiringError(
            "feature_result does not answer the given feature_request (request_hash mismatch)"
        )

    horizon = label_spec.horizon
    values = feature_result.values  # strictly ascending by evaluation_time (contract-guaranteed)
    evaluation_times = [item.evaluation_time for item in values]

    out: dict[str, Decimal | None] = {}
    for event in outcome_request.events:
        window = label_window(outcome_request.bars, event.event_time, horizon)
        if window is None:
            out[event.event_key] = None
            continue
        index = bisect_right(evaluation_times, window.entry_time) - 1
        if index < 0:
            out[event.event_key] = None
            continue
        selected = values[index].value
        if selected is None:
            out[event.event_key] = None
            continue
        if not isinstance(selected, Decimal):
            raise VolatilityWiringError(
                f"{event.event_key!r}: feature value at "
                f"{values[index].evaluation_time.isoformat()} is "
                f"{type(selected).__name__}, not Decimal"
            )
        out[event.event_key] = selected
    return out

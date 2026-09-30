"""EventProvider plugins (Phase 3; ADR-0036).

- ``features``: ``FeatureThresholdCrossProvider`` (a feature crosses a level),
  ``FeatureRelativeThresholdCrossProvider`` (a feature crosses a feature-relative level),
  ``VolatilityBreakoutProvider`` (a volatility feature enters ``value > multiplier * mean``);
- ``states``: ``StateSwitchProvider`` (the state label changes);
- ``interactions``: ``EventSequenceProvider`` (A then B within a window) and
  ``EventCoOccurrenceProvider`` (A and B within a window) — outputs cite their upstream event ids;
- ``windows``: ``EventWindowEndProvider`` (an event at the end of each upstream event's window),
  ``EventAbsenceProvider`` (an anchor with no other event in the window before it) and
  ``EventCountProvider`` (at least n upstream events in the window) — the DSL's ``not`` / ``count``;
- ``dsl``: the interaction DSL (ADR-0061) — a JSON expression of ``ref`` leaves and the operators
  ``seq`` / ``and`` / ``not`` / ``count``, compiled into ordinary interaction specs of the
  providers above (every hop still runs through the runner's upstream verification);
- ``p7_temporal``: ``P7TemporalSequenceProvider`` — executes the P7 ``temporal`` lowering
  (``p7.temporal.sequence_within_bars@1.0.0``; ADR-0100 item 1) with explicit bar durations.

Every parameter lives in the ``EventSpec.trigger`` (canonical JSON) and is bound by the spec hash.
Deterministic, exact ``Decimal``; only ``core`` is imported.
"""

from plugins.events.features import (
    FeatureRelativeThresholdCrossProvider,
    FeatureThresholdCrossProvider,
    VolatilityBreakoutProvider,
)
from plugins.events.interactions import EventCoOccurrenceProvider, EventSequenceProvider
from plugins.events.p7_temporal import P7TemporalSequenceProvider
from plugins.events.states import StateSwitchProvider
from plugins.events.windows import EventAbsenceProvider, EventCountProvider, EventWindowEndProvider

__all__ = [
    "EventAbsenceProvider",
    "EventCoOccurrenceProvider",
    "EventCountProvider",
    "EventSequenceProvider",
    "EventWindowEndProvider",
    "FeatureRelativeThresholdCrossProvider",
    "FeatureThresholdCrossProvider",
    "P7TemporalSequenceProvider",
    "StateSwitchProvider",
    "VolatilityBreakoutProvider",
]

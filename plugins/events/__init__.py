"""EventProvider plugins (Phase 3; ADR-0036).

- ``features``: ``FeatureThresholdCrossProvider`` (a feature crosses a level),
  ``VolatilityBreakoutProvider`` (a volatility feature enters ``value > multiplier * mean``);
- ``states``: ``StateSwitchProvider`` (the state label changes);
- ``interactions``: ``EventSequenceProvider`` (A then B within a window) and
  ``EventCoOccurrenceProvider`` (A and B within a window) — outputs cite their upstream event ids.

Every parameter lives in the ``EventSpec.trigger`` (canonical JSON) and is bound by the spec hash.
Deterministic, exact ``Decimal``; only ``core`` is imported.
"""

from plugins.events.features import FeatureThresholdCrossProvider, VolatilityBreakoutProvider
from plugins.events.interactions import EventCoOccurrenceProvider, EventSequenceProvider
from plugins.events.states import StateSwitchProvider

__all__ = [
    "EventCoOccurrenceProvider",
    "EventSequenceProvider",
    "FeatureThresholdCrossProvider",
    "StateSwitchProvider",
    "VolatilityBreakoutProvider",
]

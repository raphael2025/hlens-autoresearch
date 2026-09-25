"""Upstream results → strategy signals (Phase 5 wiring; ADR-0038).

- ``signals``: ``signals_from_features`` / ``signals_from_states`` / ``signals_from_events`` turn
  answered F4 / Phase 2 / Phase 3 results into ``SignalObservation`` rows for a
  ``StrategyRequest`` / ``RiskRequest``. A value evaluated at ``t`` (or an event observable at
  ``t``) is available at ``t``; the knowledge time is the caller's explicit knowledge cutoff of
  the upstream run — never guessed. Outcomes can never become signals (Constitution C-L2).
"""

from infrastructure.strategy.signals import (
    SignalAdapterError,
    signals_from_events,
    signals_from_features,
    signals_from_states,
)

__all__ = [
    "SignalAdapterError",
    "signals_from_events",
    "signals_from_features",
    "signals_from_states",
]

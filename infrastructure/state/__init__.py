"""State execution over feature values (Phase 2; ADR-0035).

- ``runner``: ``run_state`` — hands a ``StateProvider`` only the visible inputs of each evaluation
  time (feature values evaluated at or before ``t``; inside ``(t - training_window, t]`` for a
  trained spec) and checks every answer; ``state_inputs`` turns answered ``FeatureRequest`` /
  ``FeatureResult`` pairs into ``StateInput``s; ``state_request`` binds them to a spec;
- ``table``: ``state_table`` — the Arrow materialization of one ``StateResult``.

Providers are injected through the ``core.contracts.state.StateProvider`` Protocol; nothing here
imports a plugin.
"""

from infrastructure.state.runner import StateRunnerError, run_state, state_inputs, state_request
from infrastructure.state.table import STATE_TABLE_SCHEMA, state_table

__all__ = [
    "STATE_TABLE_SCHEMA",
    "StateRunnerError",
    "run_state",
    "state_inputs",
    "state_request",
    "state_table",
]

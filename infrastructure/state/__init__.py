"""State execution over feature values (Phase 2; ADR-0035, ADR-0089, ADR-0102).

- ``runner``: ``run_state`` — hands a ``StateProvider`` only the visible inputs of each evaluation
  time (feature values evaluated at or before ``t``; inside ``(t - training_window, t]`` for a
  trained spec) and checks every answer; ``state_inputs`` turns answered ``FeatureRequest`` /
  ``FeatureResult`` pairs into ``StateInput``s; ``state_request`` binds them to a spec;
- ``table``: ``state_table`` — the Arrow materialization of one ``StateResult``;
- ``store``: ``StateResultStore`` — local content-addressed artifact store of whole state runs
  (``<root>/<result_hash>.json``, never overwritten, verified on read); not an Iceberg table;
- ``table_definition`` / ``iceberg`` (ADR-0089): the physical append-only Iceberg table
  ``state.states`` (logical columns + run block, ``day(evaluation_time)``) and ``StateTable`` — one
  batch per ``result_hash``, identical rewrite = no-op, other content under a run refused,
  snapshot-pinned reads that rebuild and re-verify the ``StateResult``. ``ensure_state_tables`` is
  the only creation path; ``create_state_tables`` is its explicit command (plan-only unless
  ``--apply``; not wired into provisioning). Not exported here: importing them loads pyiceberg;
- ``run_cli`` (ADR-0102): ``python -m infrastructure.state.run_cli`` — read-only ``show`` (by
  ``result_hash``, verified) and ``list`` over a ``StateResultStore``. Running a state
  (``compute``) is ``research.states.run_cli``: it resolves providers from ``plugins``, which this
  package never imports.

Providers are injected through the ``core.contracts.state.StateProvider`` Protocol; nothing here
imports a plugin or research code.
"""

from infrastructure.state.runner import StateRunnerError, run_state, state_inputs, state_request
from infrastructure.state.store import StateResultStore, StateStoreCorrupted
from infrastructure.state.table import STATE_TABLE_SCHEMA, state_table

__all__ = [
    "STATE_TABLE_SCHEMA",
    "StateResultStore",
    "StateRunnerError",
    "StateStoreCorrupted",
    "run_state",
    "state_inputs",
    "state_request",
    "state_table",
]

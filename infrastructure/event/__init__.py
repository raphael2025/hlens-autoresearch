"""Event execution over upstream feature / state series (Phase 3; ADR-0036).

- ``runner``: ``run_events`` — hands an ``EventProvider`` only the visible set of each checkpoint
  (``available_time + observable_lag <= t``), checks every answer and requires the tables as of
  consecutive checkpoints to agree (no back-dated / retracted events: no future confirmation);
- ``inputs``: ``inputs_from_feature_run`` / ``inputs_from_state_series`` build
  ``EventInputPoint``s with lineage (the state side is the local Phase 3 shape until Phase 2's
  ``StateProvider`` is wired);
- ``table``: ``event_table`` flattens a result into Event-table rows.

Providers are injected through the ``core.contracts.event.EventProvider`` Protocol; nothing here
imports a plugin.
"""

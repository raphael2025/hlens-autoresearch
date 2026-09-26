"""Event execution over upstream feature / state series (Phase 3; ADR-0036).

- ``runner``: ``run_events`` — hands an ``EventProvider`` only the visible set of each checkpoint
  (``available_time + observable_lag <= t``), checks every answer and requires the tables as of
  consecutive checkpoints to agree (no back-dated / retracted events: no future confirmation);
- ``inputs``: ``inputs_from_feature_run`` / ``inputs_from_state_series`` build
  ``EventInputPoint``s with lineage (the state side is the local Phase 3 shape until Phase 2's
  ``StateProvider`` is wired);
- ``upstream``: ``verify_interaction`` / ``verify_input_lineage`` — what a run is given must match
  what the spec declares (an interaction's upstream specs, their hashes and Feature / State union;
  each input point's per-point lineage against its feature / state run); called by ``run_events``;
  each returns an ``UpstreamVerification`` naming the checks performed / not performed, and
  ``verify_upstream(..., require_full=True)`` (``run_events(..., require_full=True)``) refuses a
  run whose evidence for any applicable check is missing;
- ``table``: ``event_table`` flattens a result into Event-table rows;
- ``store``: ``EventResultStore`` — local content-addressed artifact store of whole event runs
  (``<root>/<result_hash>.json``, never overwritten, verified on read); not an Iceberg table;
- ``table_definition`` / ``iceberg`` (ADR-0056; CODE_COMPLETE / DEBUG_PENDING): the physical
  append-only Iceberg table ``event.events`` (logical columns + run block, ``month(event_time)``)
  and ``EventTable`` — one batch per ``result_hash``, identical rewrite = no-op, other content
  under a run refused, snapshot-pinned reads that rebuild and re-verify the ``EventResult``.
  ``ensure_event_tables`` is the only creation path (explicit; not wired into provisioning).

Providers are injected through the ``core.contracts.event.EventProvider`` Protocol; nothing here
imports a plugin.
"""

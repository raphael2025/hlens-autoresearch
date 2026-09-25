"""Feature execution over PIT-selected inputs (Phase 1 F4; ADR-0030).

- ``runner``: ``run_feature`` — hands a ``FeatureProvider`` only the visible set of each evaluation
  time (``available_time + available_lag <= t``; ``knowledge_time <= knowledge_cutoff``) and checks
  every answer;
- ``observations``: ``bar_observations`` / ``derived_bar_observations`` build
  ``FeatureObservation``s with lineage from F1 selections and E4 derived bars;
  ``pit_feature_request`` binds them to a ``FeatureRequest`` whose evaluation times the PIT view
  can answer (ad-hoc: the manifest hash is taken as given);
- ``dataset``: ``feature_request_from_dataset`` — the only request that is a Research Dataset run:
  the manifest is loaded and verified through ``ManifestStore`` and the observations are proven
  against its PIT spec, dataset snapshot rows, lineage and a re-selection (G2 RT-6).

Providers are injected through the ``core.contracts.feature.FeatureProvider`` Protocol; nothing here
imports a plugin.
"""

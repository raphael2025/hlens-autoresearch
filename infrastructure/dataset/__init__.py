"""Research Datasets of the Phase 1 first slice (F3; ADR-0023 §6, ADR-0024 §6).

- ``builder``: ``DatasetBuilder`` — (universe spec, PIT spec, data type, UTC window) →
  deterministic selection at the bound snapshots, gated by the F2 universe, bound to verified
  quality reports, materialized as one batch of a research table, recorded as a manifest;
- ``selection``: the **proposed** shape of that research table (no table is frozen for it);
- ``manifests``: ``ManifestStore`` — content-hash idempotent ``research.dataset_manifests`` rows,
  each re-derived by its ``ManifestVerifier`` (``DatasetBuilder.verify_manifest``) on persist and
  load. ``ManifestStore.load_any`` dispatches between the v2 table and the ADR-0077 v3
  ``research.dataset_evidence_manifests`` table (a hash in both is an integrity error);
- ADR-0077 bounded v3 path (contract 2.3.0): ``builder.DatasetEvidenceBuilder`` writes
  content-addressed ``evidence`` trees and fixed-size chunks (``chunks.IcebergChunkWriter`` into
  ``research.dataset_selection_chunks``); ``sources`` adapts the bounded universe cursor and PIT
  generator (sorted runs, ADR §2 stream orders); ``verify_v3.StreamingEvidenceVerifier``
  re-derives and merge-compares every stream. All size parameters are explicit (DQ-9 open).
"""

"""Research Datasets of the Phase 1 first slice (F3; ADR-0023 §6, ADR-0024 §6).

- ``builder``: ``DatasetBuilder`` — (universe spec, PIT spec, data type, UTC window) →
  deterministic selection at the bound snapshots, gated by the F2 universe, bound to verified
  quality reports, materialized as one batch of a research table, recorded as a manifest;
- ``selection``: the **proposed** shape of that research table (no table is frozen for it);
- ``manifests``: ``ManifestStore`` — content-hash idempotent ``research.dataset_manifests`` rows.
"""

"""Point-in-time reads of the Phase 1 first slice (ADR-0023 §5, ADR-0028).

- ``view``: ``PinnedCatalogView`` — every read at a manifest's bound snapshots, read-only;
- ``selector``: ``PitSelector`` — proven Canonical revisions + mapped Raw edges → maximal-head
  selections, lineage and evidence gaps under ``hlens.pit.maximal-head@1.0.0``.
"""

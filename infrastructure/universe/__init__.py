"""Historical tradable universe of the Phase 1 first slice (F2; ADR-0024, ADR-0029).

- ``builder``: ``UniverseBuilder`` — members and exclusions of a registered
  ``UniverseSelectionSpec`` at a PIT spec's bound snapshots, read through E2's
  ``ListingDeriver.listing_at``; every unconstructible listing fails the build closed.
"""

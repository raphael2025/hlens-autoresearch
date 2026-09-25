"""Canonical layer of the Phase 1 first slice (ADR-0023, ADR-0028).

- ``rules``: the four versioned Canonical identifiers — revision identity, the Binance spot
  normalizer, derived availability and the precedence map — as pure functions;
- ``normalizer``: ``CanonicalNormalizer`` — one Canonical revision per verified Raw element
  revision, per normalization unit (one Raw source revision), idempotent and crash-recoverable.
"""

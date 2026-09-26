"""Research Dataset price bars → Phase 4 outcomes and Phase 5 backtests (ADR-0037 / ADR-0038 notes).

- ``dataset``: ``outcome_request_from_dataset`` / ``backtest_bars_from_dataset`` — the only path
  from a persisted, verified ``ResearchDatasetManifest`` (loaded through the builder's own
  ``ManifestStore``) to ``OutcomePriceBar`` / ``PriceBar``. The Canonical 1m bars are re-selected
  under the manifest's point spec, must be exactly the dataset's rows, keep their own
  ``available_time`` (ADR-0032 effective only when the spec binds it) and ``Decimal`` OHLC, and any
  bar available after ``price_cutoff`` is refused.
- ``pair``: ``pair_manifests`` — the verified pairing of one chain's feature (interval) and price
  (point) manifests (backlog E1): both loaded through the verifying ``ManifestStore``, proven to
  share snapshots, policies (the ADR-0032 choice), universe, window, instruments and lineage, with
  the price view at the end of the feature interval; returns an immutable ``ManifestPair``.
  ``pair_hash_of`` recomputes a pair hash (the research validator's ``G0.manifest_binding``).
- ``verified``: ``VerifiedManifestCache`` — an opt-in, process-local cache of successful verified
  manifest loads, keyed by the manifest hash, the builder (verifier + rule), its catalog and the
  heads of every table the verifier reads unpinned; every entry point above takes it as
  ``manifest_cache`` (default ``None``: every load re-verifies, unchanged).
"""

from infrastructure.bars.dataset import (
    DatasetBarsError,
    DatasetPriceBars,
    backtest_bars_from_dataset,
    outcome_request_from_dataset,
)
from infrastructure.bars.pair import (
    PAIR_RULE,
    PAIR_RULE_HASH,
    ManifestPair,
    ManifestPairError,
    pair_hash_of,
    pair_manifests,
)
from infrastructure.bars.verified import (
    HEAD_READ_TABLES,
    ManifestCacheStats,
    VerifiedManifestCache,
    load_verified_manifest,
    verification_head_tables,
)

__all__ = [
    "HEAD_READ_TABLES",
    "PAIR_RULE",
    "PAIR_RULE_HASH",
    "DatasetBarsError",
    "DatasetPriceBars",
    "ManifestCacheStats",
    "ManifestPair",
    "ManifestPairError",
    "VerifiedManifestCache",
    "backtest_bars_from_dataset",
    "load_verified_manifest",
    "outcome_request_from_dataset",
    "pair_hash_of",
    "pair_manifests",
    "verification_head_tables",
]

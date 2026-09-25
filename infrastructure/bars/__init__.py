"""Research Dataset price bars → Phase 4 outcomes and Phase 5 backtests (ADR-0037 / ADR-0038 notes).

- ``dataset``: ``outcome_request_from_dataset`` / ``backtest_bars_from_dataset`` — the only path
  from a persisted, verified ``ResearchDatasetManifest`` (loaded through the builder's own
  ``ManifestStore``) to ``OutcomePriceBar`` / ``PriceBar``. The Canonical 1m bars are re-selected
  under the manifest's point spec, must be exactly the dataset's rows, keep their own
  ``available_time`` (ADR-0032 effective only when the spec binds it) and ``Decimal`` OHLC, and any
  bar available after ``price_cutoff`` is refused.
"""

from infrastructure.bars.dataset import (
    DatasetBarsError,
    DatasetPriceBars,
    backtest_bars_from_dataset,
    outcome_request_from_dataset,
)

__all__ = [
    "DatasetBarsError",
    "DatasetPriceBars",
    "backtest_bars_from_dataset",
    "outcome_request_from_dataset",
]

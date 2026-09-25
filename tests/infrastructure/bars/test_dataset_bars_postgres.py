"""PostgreSQL evidence for ``test_dataset_bars.py`` (backlog C: 数据集接线 PostgreSQL 变体).

Every scenario below is the exact same test function defined in ``test_dataset_bars.py`` --
imported, not re-typed -- so the assertions can never drift between backends; only the ``w``
fixture changes, from the SQLite ``World`` to a PostgreSQL one built on the dedicated test catalog
(``HLENS_TEST_CATALOG_URI``). Skips (not passes) when that catalog is not configured. Same tiny
five-bar, one-symbol fixture as the SQLite module.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from tests.infrastructure.bars.test_dataset_bars import (
    test_a_dataset_feeds_the_bar_backtester,
    test_a_manifest_of_another_data_type_has_no_bar_rows,
    test_a_manifest_that_does_not_load_yields_no_bars,
    test_a_price_cutoff_beyond_the_datasets_view_is_refused,
    test_a_symbol_without_dataset_rows_is_refused,
    test_an_interval_dataset_is_refused,
    test_bars_available_after_the_price_cutoff_are_refused,
    test_dataset_bars_keep_their_own_times_and_decimal_prices,
    test_dataset_runs_are_bit_identical_across_reruns,
    test_outcome_labels_from_a_dataset_are_known_only_after_their_exit,
    test_there_is_no_unverified_entry,
    test_triple_barrier_labels_run_on_the_same_dataset_bars,
)
from tests.infrastructure.catalog.catalog_support import postgres_test_catalog_uri
from tests.infrastructure.dataset import dataset_support as ds
from tests.infrastructure.dataset.dataset_support import World

pytestmark = pytest.mark.postgres

__all__ = [
    "test_a_dataset_feeds_the_bar_backtester",
    "test_a_manifest_of_another_data_type_has_no_bar_rows",
    "test_a_manifest_that_does_not_load_yields_no_bars",
    "test_a_price_cutoff_beyond_the_datasets_view_is_refused",
    "test_a_symbol_without_dataset_rows_is_refused",
    "test_an_interval_dataset_is_refused",
    "test_bars_available_after_the_price_cutoff_are_refused",
    "test_dataset_bars_keep_their_own_times_and_decimal_prices",
    "test_dataset_runs_are_bit_identical_across_reruns",
    "test_outcome_labels_from_a_dataset_are_known_only_after_their_exit",
    "test_there_is_no_unverified_entry",
    "test_triple_barrier_labels_run_on_the_same_dataset_bars",
]


@pytest.fixture
def w(tmp_path: Path) -> Iterator[World]:
    with ds.postgres_world(tmp_path, postgres_test_catalog_uri()) as opened:
        yield opened

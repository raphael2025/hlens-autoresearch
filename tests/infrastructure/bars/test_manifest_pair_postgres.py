"""PostgreSQL evidence for ``test_manifest_pair.py`` (backlog C: 数据集接线 PostgreSQL 变体).

Every scenario below is the exact same test function defined in ``test_manifest_pair.py`` --
imported, not re-typed -- so the assertions can never drift between backends; only the ``w``
fixture changes, from the SQLite ``World`` to a PostgreSQL one built on the dedicated test catalog
(``HLENS_TEST_CATALOG_URI``). Skips (not passes) when that catalog is not configured. Same tiny
five-bar fixture as the SQLite module.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from tests.infrastructure.bars.test_manifest_pair import (
    test_a_different_adr_0032_choice_is_refused,
    test_a_different_instrument_set_is_refused,
    test_a_different_upstream_snapshot_is_refused,
    test_a_feature_interval_ending_before_the_price_view_is_refused,
    test_a_hand_made_pair_hash_is_refused,
    test_a_manifest_that_does_not_load_is_refused,
    test_a_matching_pair_is_bound_by_hash,
    test_a_price_view_before_the_end_of_the_feature_interval_is_refused,
    test_both_sides_without_the_adr_0032_assumption_also_pair,
    test_different_knowledge_cutoffs_are_refused,
    test_swapped_roles_are_refused,
    test_there_is_no_unverified_entry,
)
from tests.infrastructure.catalog.catalog_support import postgres_test_catalog_uri
from tests.infrastructure.dataset import dataset_support as ds
from tests.infrastructure.dataset.dataset_support import World

pytestmark = pytest.mark.postgres

__all__ = [
    "test_a_different_adr_0032_choice_is_refused",
    "test_a_different_instrument_set_is_refused",
    "test_a_different_upstream_snapshot_is_refused",
    "test_a_feature_interval_ending_before_the_price_view_is_refused",
    "test_a_hand_made_pair_hash_is_refused",
    "test_a_manifest_that_does_not_load_is_refused",
    "test_a_matching_pair_is_bound_by_hash",
    "test_a_price_view_before_the_end_of_the_feature_interval_is_refused",
    "test_both_sides_without_the_adr_0032_assumption_also_pair",
    "test_different_knowledge_cutoffs_are_refused",
    "test_swapped_roles_are_refused",
    "test_there_is_no_unverified_entry",
]


@pytest.fixture
def w(tmp_path: Path) -> Iterator[World]:
    with ds.postgres_world(tmp_path, postgres_test_catalog_uri()) as opened:
        yield opened

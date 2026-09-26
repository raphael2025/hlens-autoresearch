"""PostgreSQL evidence for ``test_manifest_cache.py`` (the verified-manifest cache).

Every scenario is the exact same test function defined in ``test_manifest_cache.py`` -- imported,
not re-typed -- so the assertions cannot drift between backends; only the ``w`` / ``other``
fixtures change, to PostgreSQL worlds on the dedicated test catalog (``HLENS_TEST_CATALOG_URI``,
each with its own catalog name). Skips (not passes) when that catalog is not configured.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from tests.infrastructure.bars.test_manifest_cache import (
    test_a_cache_shared_across_two_catalogs_misses,
    test_a_forged_manifest_is_still_refused_with_the_cache_on,
    test_a_hit_returns_the_same_manifest_without_a_second_verification,
    test_a_manifest_that_does_not_load_is_never_stored,
    test_a_new_snapshot_of_a_table_read_at_its_head_misses,
    test_a_non_builder_verifier_is_refused_with_the_cache_on,
    test_a_pair_reuses_both_proofs,
    test_a_verification_that_fails_is_not_stored,
    test_another_builder_or_a_reopened_catalog_misses,
    test_another_manifest_with_other_pinned_snapshots_is_its_own_proof,
    test_the_replay_path_is_re_proven_when_its_bound_if_present_table_appears,
    test_the_replay_path_reads_no_other_table_at_its_head,
    test_the_verifier_reads_no_other_table_at_its_head,
    test_without_a_cache_every_load_verifies,
    verified,
)
from tests.infrastructure.catalog.catalog_support import postgres_test_catalog_uri
from tests.infrastructure.dataset import dataset_support as ds
from tests.infrastructure.dataset.dataset_support import World

pytestmark = pytest.mark.postgres

__all__ = [
    "test_a_cache_shared_across_two_catalogs_misses",
    "test_a_forged_manifest_is_still_refused_with_the_cache_on",
    "test_a_hit_returns_the_same_manifest_without_a_second_verification",
    "test_a_manifest_that_does_not_load_is_never_stored",
    "test_a_new_snapshot_of_a_table_read_at_its_head_misses",
    "test_a_non_builder_verifier_is_refused_with_the_cache_on",
    "test_a_pair_reuses_both_proofs",
    "test_a_verification_that_fails_is_not_stored",
    "test_another_builder_or_a_reopened_catalog_misses",
    "test_another_manifest_with_other_pinned_snapshots_is_its_own_proof",
    "test_the_replay_path_is_re_proven_when_its_bound_if_present_table_appears",
    "test_the_replay_path_reads_no_other_table_at_its_head",
    "test_the_verifier_reads_no_other_table_at_its_head",
    "test_without_a_cache_every_load_verifies",
    "verified",
]


@pytest.fixture
def w(tmp_path: Path) -> Iterator[World]:
    with ds.postgres_world(tmp_path / "w", postgres_test_catalog_uri()) as opened:
        yield opened


@pytest.fixture
def other(tmp_path: Path) -> Iterator[World]:
    """A second, independent catalog."""
    with ds.postgres_world(tmp_path / "other", postgres_test_catalog_uri()) as opened:
        yield opened

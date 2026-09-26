"""ADR-0052 §4 blocker evidence: a contract minor bump vs. Canonical idempotent replay.

ADR-0052 (Accepted) bumps ``CONTRACT_SCHEMA_VERSION`` 2.0.0 -> 2.1.0 and requires the
implementation batch to first prove that already-committed Canonical data still replays. It does
not: ``infrastructure/canonical/rules.py`` (``canonical_row``) stamps the **live**
``CONTRACT_SCHEMA_VERSION`` into every Canonical row's ``contract_schema_version`` column, and
``infrastructure/canonical/normalizer.py`` (``_exact``) compares **every** column of a committed
unit with its re-normalized rows — before that, ``infrastructure/revision/row_integrity.py``
(``check_batch_snapshot``) re-fingerprints each committed batch from the re-built rows.
Re-normalizing a unit committed at 2.0.0 after the bump therefore raises
``CatalogIntegrityError`` (``batch ... was committed with other content``; the per-column check
would report ``['contract_schema_version']``) instead of replaying.

The bump is simulated by patching the version the row builder reads (``rules``'s module global);
nothing is committed at a new version for real. ``test_replay_after_a_minor_bump_is_idempotent``
is the property ADR-0052 needs (strict xfail until an old-version replay path exists: committed
rows keep, and are compared against, their recorded version); the second test pins today's
failure mode so the evidence cannot silently change. Fixing it requires editing Phase 1
infrastructure (under Codex review), so ADR-0052 is not implemented (ADR-0052 "Implementation
blocker (2026-09-26)").
"""

from __future__ import annotations

import pytest

from core.domain.base import CONTRACT_SCHEMA_VERSION
from infrastructure.canonical import rules
from infrastructure.catalog.iceberg_adapter import CatalogIntegrityError
from tests.infrastructure.canonical import canonical_support as c
from tests.infrastructure.revision import rest_store_support as ss
from tests.infrastructure.revision.rest_store_support import RestHarness, StepClock, utc

K_ARCHIVE = utc(2023, 12, 1)
K_NORM = utc(2023, 12, 6)
#: The version ADR-0052 §4 would publish (a minor bump inside major 2).
BUMPED = "2.1.0"


def _committed_unit(h: RestHarness) -> str:
    archive = c.ingest_archive(
        h, "agg_trades", ss.archive_agg_lines(ss.agg_items(3)), knowledge=K_ARCHIVE
    )
    c.normalizer(h, clock=StepClock(start=K_NORM)).normalize_unit(c.ARCHIVE_AGGS.table, archive)
    rows = h.rows(c.TRADES)
    assert rows and {row["contract_schema_version"] for row in rows} == {CONTRACT_SCHEMA_VERSION}
    return archive


@pytest.mark.xfail(
    strict=True,
    raises=CatalogIntegrityError,
    reason=(
        "ADR-0052 blocker: canonical_row stamps the live CONTRACT_SCHEMA_VERSION; the batch "
        "fingerprint check and _exact compare every column, so a 2.0.0 -> 2.1.0 bump breaks "
        "idempotent replay of committed Canonical rows; needs an old-version replay path in "
        "Phase 1 infrastructure (under Codex review)"
    ),
)
def test_replay_after_a_minor_bump_is_idempotent(
    h: RestHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    archive = _committed_unit(h)
    before = (h.head(c.TRADES.table), h.rows(c.TRADES))
    monkeypatch.setattr(rules, "CONTRACT_SCHEMA_VERSION", BUMPED)
    again = c.normalizer(h, clock=StepClock(start=K_NORM)).normalize_unit(
        c.ARCHIVE_AGGS.table, archive
    )
    assert again.replayed
    assert (h.head(c.TRADES.table), h.rows(c.TRADES)) == before  # read as recorded, not rewritten


def test_today_a_minor_bump_makes_committed_rows_disagree(
    h: RestHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    archive = _committed_unit(h)
    before = (h.head(c.TRADES.table), h.rows(c.TRADES))
    monkeypatch.setattr(rules, "CONTRACT_SCHEMA_VERSION", BUMPED)
    # The batch fingerprint check (row_integrity.check_batch_snapshot) fires first; the
    # column-by-column _exact comparison would name ['contract_schema_version'] next.
    with pytest.raises(CatalogIntegrityError, match="was committed with other content"):
        c.normalizer(h, clock=StepClock(start=K_NORM)).normalize_unit(c.ARCHIVE_AGGS.table, archive)
    assert (h.head(c.TRADES.table), h.rows(c.TRADES)) == before  # nothing was written


def test_replay_at_the_recorded_version_is_idempotent(h: RestHarness) -> None:
    """Control: without a bump the same replay is idempotent (the harness is not the cause)."""
    archive = _committed_unit(h)
    before = (h.head(c.TRADES.table), h.rows(c.TRADES))
    again = c.normalizer(h, clock=StepClock(start=K_NORM)).normalize_unit(
        c.ARCHIVE_AGGS.table, archive
    )
    assert again.replayed
    assert (h.head(c.TRADES.table), h.rows(c.TRADES)) == before

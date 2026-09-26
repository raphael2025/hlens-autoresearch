"""Well-formed research-loop round records for report / console tests (ADR-0049 / ADR-0050).

A worker never writes a round without its stages, so these builders produce what a real round of
``STAGE_ORDER`` looks like: every stage ``COMPLETED`` with a declared and reported usage, the
round and cumulative totals adding up, and rounds after the first chained by ``previous_hash``.
Every record built here is a valid ``core.contracts.loop_audit.LoopRoundRecord``.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from apps.worker.loop import (
    STAGE_ORDER,
    LoopRecord,
    RoundStatus,
    StageRecord,
    StageStatus,
    StageUsage,
)
from core.domain.base import content_hash

T0 = datetime(2026, 1, 1, tzinfo=UTC)
#: The one stage that spends something in these rounds (TEST ONLY numbers).
SPENDING_STAGE = "experiment"
NOTHING = StageUsage()
SPEND = StageUsage(trials=1, llm_cost_units=Decimal("0.5"), compute_seconds=Decimal("2"))


def completed_round(
    round_index: int = 0,
    seed: int = 1,
    *,
    previous_hash: str | None = None,
    total_before: StageUsage = NOTHING,
    loop_id: str = "loop-x",
) -> LoopRecord:
    """One ``COMPLETED`` round; ``previous_hash`` must be set for every round after the first."""
    stages = tuple(
        StageRecord(
            name,
            StageStatus.COMPLETED,
            estimate=SPEND if name == SPENDING_STAGE else NOTHING,
            usage=SPEND if name == SPENDING_STAGE else NOTHING,
            summary={"stage": name},
        )
        for name in STAGE_ORDER
    )
    return LoopRecord(
        loop_id=loop_id,
        round_index=round_index,
        seed=seed,
        as_of=T0 + timedelta(hours=round_index),
        budget_hash=content_hash({"budget": "test"}),
        status=RoundStatus.COMPLETED,
        stages=stages,
        transitions=(),
        round_usage=SPEND,
        total_usage=total_before + SPEND,
        previous_hash=previous_hash,
    )


def chained_rounds(count: int) -> list[LoopRecord]:
    """``count`` consecutive rounds of one loop, each chained to the previous one."""
    records: list[LoopRecord] = []
    for index in range(count):
        previous = records[-1] if records else None
        records.append(
            completed_round(
                index,
                seed=index + 1,
                previous_hash=None if previous is None else previous.record_hash,
                total_before=NOTHING if previous is None else previous.total_usage,
            )
        )
    return records

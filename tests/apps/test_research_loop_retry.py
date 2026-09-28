"""ADR-0083: the worker lifts the ADR-0070 fail-stop only through an explicit retry receipt.

Fake stages only (``apps/`` never imports ``research/``); every number is TEST ONLY.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any, cast

import pytest

from apps.worker import (
    STAGE_ORDER,
    LoopBudget,
    LoopHalted,
    ResearchLoop,
    RoundContext,
    StageFailed,
    StageResult,
    StageStatus,
    StageUsage,
)
from infrastructure.event_bus import InMemoryEventBus

EPOCH = datetime(2026, 1, 1, tzinfo=UTC)
TEST_ONLY_BUDGET = LoopBudget(
    max_trials_per_round=5,
    max_trials_total=100,
    max_llm_cost_units=Decimal(100),
    max_compute_seconds=Decimal(1000),
)


@dataclass(frozen=True)
class Receipt:
    """A well-formed receipt shape (``FailedRoundRetryReceipt``); hashes are placeholders."""

    failed_record_hash: str
    retry_id: str = "1" * 64
    commit_seq: int = 2
    commit_hash: str = "2" * 64
    checkpoint_seq: int = 3
    checkpoint_hash: str = "3" * 64


class Stage:
    def __init__(self, name: str, *, fail_round: int | None = None) -> None:
        self.name = name
        self._fail_round = fail_round
        self.runs = 0

    def estimate(self, ctx: RoundContext) -> StageUsage:
        return StageUsage()

    def run(self, ctx: RoundContext) -> StageResult:
        self.runs += 1
        if ctx.round_index == self._fail_round:
            raise StageFailed("TEST ONLY: the batch died", usage=StageUsage())
        return StageResult({"stage": self.name})


def _failed_loop() -> tuple[ResearchLoop, Stage]:
    experiment = Stage("experiment", fail_round=0)
    stages = [experiment if name == "experiment" else Stage(name) for name in STAGE_ORDER]
    loop = ResearchLoop(
        loop_id="retry_loop",
        stages=stages,
        budget=TEST_ONLY_BUDGET,
        bus=InMemoryEventBus(),
        seed=7,
        epoch=EPOCH,
        cadence=timedelta(hours=1),
    )
    [record] = loop.run_unattended(1)
    assert {s.name: s for s in record.stages}["experiment"].status is StageStatus.FAILED
    assert loop.recovery_required is not None
    return loop, experiment


def test_only_a_receipt_naming_the_failed_audit_head_lifts_the_stop() -> None:
    loop, experiment = _failed_loop()
    head = loop.audit.records[-1].record_hash
    with pytest.raises(LoopHalted, match="human review"):
        loop.submit_round(1)
    with pytest.raises(LoopHalted, match="another failed round"):
        loop.authorize_failed_round_retry(Receipt(failed_record_hash="f" * 64))
    with pytest.raises(LoopHalted, match="malformed"):
        loop.authorize_failed_round_retry(replace(Receipt(head), commit_seq=0))
    with pytest.raises(LoopHalted, match="malformed"):
        loop.authorize_failed_round_retry(replace(Receipt(head), checkpoint_hash="not-a-hash"))
    with pytest.raises(LoopHalted, match="receipt"):
        loop.authorize_failed_round_retry(cast(Any, object()))
    assert loop.recovery_required is not None and experiment.runs == 1

    loop.authorize_failed_round_retry(Receipt(head))
    assert loop.recovery_required is None
    assert experiment.runs == 1  # authorizing runs, schedules and submits nothing
    with pytest.raises(LoopHalted, match="no failed experiment round"):
        loop.authorize_failed_round_retry(Receipt(head))  # consumed once
    [record] = loop.run_unattended(1)
    assert record.round_index == 1 and experiment.runs == 2


def test_a_loop_without_a_failed_experiment_round_accepts_no_receipt() -> None:
    stages = [Stage(name) for name in STAGE_ORDER]
    loop = ResearchLoop(
        loop_id="retry_loop",
        stages=stages,
        budget=TEST_ONLY_BUDGET,
        bus=InMemoryEventBus(),
        seed=7,
        epoch=EPOCH,
        cadence=timedelta(hours=1),
    )
    [record] = loop.run_unattended(1)
    with pytest.raises(LoopHalted, match="no failed experiment round"):
        loop.authorize_failed_round_retry(Receipt(record.record_hash))

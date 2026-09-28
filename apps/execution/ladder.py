"""The execution ladder: SIMULATED -> PAPER -> SMALL_LIVE -> SCALED_LIVE (Phase 13; ADR-0046).

* SIMULATED -> PAPER is allowed, one rung at a time, and recorded as a granted gate with evidence.
* Any step to a live rung needs an ``AuthorizationRecord`` (ADR-0006 §2, ADR-0011 D-17.3) and, in
  this build, is **always refused**, with or without one: live trading is not authorized by Raphael
  (H10, ADR-0022 §7). The refusal is recorded before the exception is raised. ADR-0084 reserves a
  live venue *interface* (``apps.execution.live_venue``) without changing this: the ladder still
  refuses every live rung on its own, independent of whether a live adapter is ever registered.
* Nothing is ever auto-granted: a grant needs a named human ``decided_by`` and evidence.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime

from apps.execution.errors import LadderGateRefused, LiveExecutionRefused
from apps.execution.records import (
    LIVE_STAGES,
    STAGE_ORDER,
    ExecutionStage,
    LadderGateRecord,
)
from core.domain.base import Ref
from core.lifecycle.strategy import AuthorizationRecord, RiskGateRecord

__all__ = ["ExecutionLadder", "LIVE_REFUSAL_MESSAGE"]

LIVE_REFUSAL_MESSAGE = (
    "live execution is not available in this build (ADR-0046, ADR-0084): the live venue port is "
    "reserved but unconfigured, and no credentials are read; a live rung needs Raphael's explicit "
    "authorization and a new ADR"
)


class ExecutionLadder:
    def __init__(
        self,
        *,
        deployment_id: str,
        subject: Ref,
        clock: Callable[[], datetime],
        on_record: Callable[[LadderGateRecord], None] | None = None,
    ) -> None:
        self._deployment_id = deployment_id
        self._subject = subject
        self._clock = clock
        self._on_record = on_record
        self._stage = ExecutionStage.SIMULATED
        self._history: list[LadderGateRecord] = []

    @property
    def stage(self) -> ExecutionStage:
        return self._stage

    @property
    def history(self) -> tuple[LadderGateRecord, ...]:
        return tuple(self._history)

    def promote_to_paper(self, *, evidence: tuple[str, ...], decided_by: str) -> LadderGateRecord:
        if self._stage is not ExecutionStage.SIMULATED:
            raise LadderGateRefused(f"PAPER is reached from SIMULATED, not from {self._stage}")
        record = self._record(
            ExecutionStage.PAPER,
            granted=True,
            reason="simulated run reviewed; paper stage granted",
            evidence=evidence,
            decided_by=decided_by,
        )
        self._stage = ExecutionStage.PAPER
        return record

    def request_live(
        self,
        to_stage: ExecutionStage,
        *,
        requested_by: str,
        authorization: AuthorizationRecord | None = None,
        risk_gate: RiskGateRecord | None = None,
    ) -> LadderGateRecord:
        """Always records a refusal and raises ``LiveExecutionRefused`` in this build."""
        if to_stage not in LIVE_STAGES:
            raise LadderGateRefused(f"{to_stage} is not a live rung; use promote_to_paper")
        if authorization is None:
            reason = f"{self._stage} -> {to_stage} requires an AuthorizationRecord; none supplied"
        elif authorization.subject.target_identity() != self._subject.target_identity():
            reason = "the AuthorizationRecord belongs to another subject"
        else:
            reason = "AuthorizationRecord supplied, but " + LIVE_REFUSAL_MESSAGE
        if STAGE_ORDER.index(to_stage) != STAGE_ORDER.index(self._stage) + 1:
            reason += f"; also, {to_stage} is not the next rung after {self._stage}"
        record = self._record(
            to_stage,
            granted=False,
            reason=reason,
            evidence=(),
            decided_by=requested_by,
            authorization=authorization,
            risk_gate=risk_gate,
        )
        raise LiveExecutionRefused(f"{reason} (gate record {record.record_id})")

    def _record(
        self,
        to_stage: ExecutionStage,
        *,
        granted: bool,
        reason: str,
        evidence: tuple[str, ...],
        decided_by: str,
        authorization: AuthorizationRecord | None = None,
        risk_gate: RiskGateRecord | None = None,
    ) -> LadderGateRecord:
        record = LadderGateRecord(
            sequence=len(self._history),
            deployment_id=self._deployment_id,
            subject=self._subject,
            from_stage=self._stage,
            to_stage=to_stage,
            granted=granted,
            reason=reason,
            evidence=evidence,
            decided_by=decided_by,
            decided_at=self._clock(),
            authorization=authorization,
            risk_gate=risk_gate,
        )
        self._history.append(record)
        if self._on_record is not None:
            self._on_record(record)
        return record

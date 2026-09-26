"""Immutable, content-addressed execution records (ADR-0046).

Every record is a frozen ``Contract`` (extra fields forbidden, NaN / infinity refused, ADR-0008 /
ADR-0013); its ``record_id`` is the SHA-256 of its canonical JSON, so an audit entry cannot be
edited without changing its identity. Money and quantities are ``Decimal`` so that a replay of the
same inputs is bit-identical. These are application-plane records, not frozen domain contracts:
they are not in ``core/contracts/registry`` and changing them needs no Domain ADR (H1).

Nothing here describes a real venue order, account or credential; ``mode`` is always SIMULATED.
"""

from __future__ import annotations

from decimal import Decimal
from enum import StrEnum

from pydantic import Field, model_validator

from core.domain.base import ContentHash, Contract, Ref, UtcDatetime
from core.domain.execution import ExecutionMode
from core.domain.specs import Instrument
from core.lifecycle.strategy import AuthorizationRecord, RiskGateRecord

__all__ = [
    "LIVE_STAGES",
    "STAGE_ORDER",
    "Alert",
    "AlertKind",
    "ExecutionStage",
    "FillRecord",
    "KillSwitchTrip",
    "LadderGateRecord",
    "MarkPrice",
    "MarkRecord",
    "OrderRecord",
    "RejectionRecord",
    "RejectionSource",
    "Side",
    "TargetPosition",
    "TargetPositions",
    "instrument_key",
]


def instrument_key(instrument: Instrument) -> str:
    """Stable string key of an instrument: ``venue:instrument_type:symbol``."""
    return f"{instrument.venue}:{instrument.instrument_type.value}:{instrument.symbol}"


class _Record(Contract):
    @property
    def record_id(self) -> str:
        return self.content_hash()


class Side(StrEnum):
    BUY = "BUY"
    SELL = "SELL"

    @property
    def sign(self) -> int:
        return 1 if self is Side.BUY else -1


class ExecutionStage(StrEnum):
    """Rungs of the Phase 13 ladder (roadmap: paper -> small live -> scale-up).

    Distinct from ``ExecutionMode``: SIMULATED and PAPER both run with
    ``ExecutionMode.SIMULATED``; SMALL_LIVE and SCALED_LIVE would need LIVE and are refused.
    """

    SIMULATED = "SIMULATED"
    PAPER = "PAPER"
    SMALL_LIVE = "SMALL_LIVE"
    SCALED_LIVE = "SCALED_LIVE"


STAGE_ORDER: tuple[ExecutionStage, ...] = tuple(ExecutionStage)
LIVE_STAGES: frozenset[ExecutionStage] = frozenset(
    {ExecutionStage.SMALL_LIVE, ExecutionStage.SCALED_LIVE}
)


class TargetPosition(Contract):
    """Desired signed quantity of one instrument (positive = long, negative = short)."""

    instrument: Instrument
    quantity: Decimal


class TargetPositions(_Record):
    """The narrow Phase 13 input: target positions per instrument from one deployment.

    Phase 5 (Strategy / Risk / Backtest providers, ADR-0038) is wired to this shape through
    ``apps.execution.service.TargetPositionSource``; nothing else enters the execution plane.
    """

    deployment_id: str = Field(min_length=1)
    as_of: UtcDatetime
    targets: tuple[TargetPosition, ...]

    @model_validator(mode="after")
    def _unique_instruments(self) -> TargetPositions:
        keys = [instrument_key(t.instrument) for t in self.targets]
        if len(keys) != len(set(keys)):
            raise ValueError("each instrument may appear at most once in TargetPositions")
        return self


class OrderRecord(_Record):
    """An order intent, recorded before any check. Always SIMULATED."""

    sequence: int = Field(ge=0)
    deployment_id: str = Field(min_length=1)
    artifact_id: ContentHash
    stage: ExecutionStage
    mode: ExecutionMode
    instrument: Instrument
    side: Side
    quantity: Decimal = Field(gt=0)
    reference_price: Decimal = Field(gt=0)
    submitted_at: UtcDatetime

    @model_validator(mode="after")
    def _simulated_only(self) -> OrderRecord:
        if self.mode is not ExecutionMode.SIMULATED or self.stage in LIVE_STAGES:
            raise ValueError("this build records SIMULATED orders only (ADR-0046 red line)")
        return self

    @property
    def signed_quantity(self) -> Decimal:
        return self.quantity * self.side.sign


class FillRecord(_Record):
    """A deterministic full fill from the simulated venue."""

    order_id: ContentHash
    venue_id: str = Field(min_length=1)
    deployment_id: str = Field(min_length=1)
    instrument: Instrument
    side: Side
    quantity: Decimal = Field(gt=0)
    reference_price: Decimal = Field(gt=0)
    price: Decimal = Field(gt=0)
    fee: Decimal = Field(ge=0)
    cost_model: str = Field(min_length=1)
    filled_at: UtcDatetime

    @property
    def signed_quantity(self) -> Decimal:
        return self.quantity * self.side.sign


class MarkPrice(Contract):
    """One marked price inside a ``MarkRecord`` (``key`` is an ``instrument_key``)."""

    key: str = Field(min_length=1)
    price: Decimal = Field(gt=0)


class MarkRecord(_Record):
    """The prices the service marked its risk book and monitor with (P13 risk / alert replay).

    Written only by a service built with ``record_marks=True``, immediately before the marks are
    applied, so every drawdown alert they cause follows it in the audit. ``prices`` holds every
    supplied price, sorted by key. ``sequence`` keeps two identical marks distinct.
    """

    sequence: int = Field(ge=0)
    deployment_id: str = Field(min_length=1)
    prices: tuple[MarkPrice, ...] = Field(min_length=1)
    marked_at: UtcDatetime

    @model_validator(mode="after")
    def _sorted_unique(self) -> MarkRecord:
        keys = [p.key for p in self.prices]
        if keys != sorted(set(keys)):
            raise ValueError("MarkRecord prices must be sorted by key and unique")
        return self

    def price_map(self) -> dict[str, Decimal]:
        return {p.key: p.price for p in self.prices}


class RejectionSource(StrEnum):
    KILL_SWITCH = "KILL_SWITCH"
    SECOND_LINE_RISK = "SECOND_LINE_RISK"


class RejectionRecord(_Record):
    """An order that did not reach the venue, and why."""

    order_id: ContentHash
    source: RejectionSource
    limit_name: str = Field(min_length=1)
    reason: str = Field(min_length=1)
    observed: Decimal | None = None
    limit: Decimal | None = None
    rejected_at: UtcDatetime


class KillSwitchTrip(_Record):
    sequence: int = Field(ge=0)
    reason: str = Field(min_length=1)
    tripped_by: str = Field(min_length=1)
    tripped_at: UtcDatetime


class AlertKind(StrEnum):
    DRAWDOWN_BREACH = "DRAWDOWN_BREACH"
    RISK_REJECTION = "RISK_REJECTION"
    KILL_SWITCH_TRIPPED = "KILL_SWITCH_TRIPPED"


class Alert(_Record):
    """A monitoring / risk-breach event handed to the alert hooks."""

    sequence: int = Field(ge=0)
    kind: AlertKind
    message: str = Field(min_length=1)
    observed: Decimal | None = None
    threshold: Decimal | None = None
    related_record_id: ContentHash | None = None
    raised_at: UtcDatetime


class LadderGateRecord(_Record):
    """One decision on the execution ladder: granted or refused, always recorded.

    A grant needs at least one evidence reference. A request for a live rung carries the
    ``AuthorizationRecord`` / ``RiskGateRecord`` it was made with (if any) and is always refused in
    this build (ADR-0046).
    """

    sequence: int = Field(ge=0)
    deployment_id: str = Field(min_length=1)
    subject: Ref
    from_stage: ExecutionStage
    to_stage: ExecutionStage
    granted: bool
    reason: str = Field(min_length=1)
    evidence: tuple[str, ...] = ()
    decided_by: str = Field(min_length=1)
    decided_at: UtcDatetime
    authorization: AuthorizationRecord | None = None
    risk_gate: RiskGateRecord | None = None

    @model_validator(mode="after")
    def _grant_rules(self) -> LadderGateRecord:
        if self.granted and not self.evidence:
            raise ValueError("a granted ladder gate needs at least one evidence reference")
        if self.granted and self.to_stage in LIVE_STAGES:
            raise ValueError("a live ladder rung cannot be granted in this build (ADR-0046)")
        return self

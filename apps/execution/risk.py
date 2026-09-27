"""Second-line risk: an independent pre-trade check (09-security.md §5; ADR-0046).

It is a second line of defence *outside* any strategy's RiskProvider: it keeps its own position book
from the fills it is shown, and checks every order against gross exposure, leverage and loss limits
before the order may reach the venue. Every rejection is recorded.

All limit numbers are injected (``RiskLimits`` or a ``RiskPolicy`` the operator supplies). There are
no default numbers here, and leverage is always bounded (roadmap Phase 13: no unbounded leverage).
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from decimal import Decimal, InvalidOperation

from pydantic import Field

from apps.execution.book import PositionBook
from apps.execution.records import (
    FillRecord,
    OrderRecord,
    RejectionRecord,
    RejectionSource,
    instrument_key,
)
from core.domain.base import Contract, Ref
from core.domain.specs import RiskPolicy

__all__ = ["RISK_POLICY_LIMIT_KEYS", "RiskLimits", "SecondLineRisk"]

#: Keys a ``RiskPolicy.params`` must carry to configure second-line risk. All are required.
RISK_POLICY_LIMIT_KEYS: tuple[str, ...] = ("max_gross_exposure", "max_leverage", "max_loss")


class RiskLimits(Contract):
    """Second-line limits, in the quote currency of the account (leverage is a ratio).

    * ``capital``: the risk budget the account starts from (supplied by the operator).
    * ``max_gross_exposure``: the largest allowed sum of |position x mark|.
    * ``max_leverage``: the largest allowed gross exposure / equity; finite by construction.
    * ``max_loss``: once capital - equity reaches this, no exposure-increasing order passes.
    """

    capital: Decimal = Field(gt=0)
    max_gross_exposure: Decimal = Field(gt=0)
    max_leverage: Decimal = Field(gt=0)
    max_loss: Decimal = Field(gt=0)
    policy: Ref | None = None

    @classmethod
    def from_risk_policy(cls, policy: RiskPolicy, *, capital: Decimal) -> RiskLimits:
        """Read the limits from ``policy.params``; a missing or non-numeric key is refused."""
        missing = [key for key in RISK_POLICY_LIMIT_KEYS if key not in policy.params]
        if missing:
            raise ValueError(f"RiskPolicy {policy.ref} lacks second-line limits: {missing}")
        values: dict[str, object] = {"capital": capital, "policy": policy.ref}
        for key in RISK_POLICY_LIMIT_KEYS:
            raw = policy.params[key]
            if isinstance(raw, bool):
                raise ValueError(f"RiskPolicy param {key} must be numeric, got a bool")
            try:
                values[key] = Decimal(str(raw))
            except InvalidOperation as exc:
                raise ValueError(f"RiskPolicy param {key} is not a number: {raw!r}") from exc
        return cls.model_validate(values)


class SecondLineRisk:
    def __init__(self, limits: RiskLimits) -> None:
        self._limits = limits
        self._book = PositionBook(limits.capital)
        self._rejections: list[RejectionRecord] = []

    @property
    def limits(self) -> RiskLimits:
        return self._limits

    @property
    def rejections(self) -> tuple[RejectionRecord, ...]:
        return tuple(self._rejections)

    def mark(self, prices: Mapping[str, Decimal]) -> None:
        self._book.mark(prices)

    def on_fill(self, fill: FillRecord) -> None:
        self._book.apply(fill)

    def check(self, order: OrderRecord, at: datetime) -> RejectionRecord | None:
        """Return ``None`` if the order may proceed, else the (recorded) rejection.

        Orders that do not increase gross exposure always pass, so the book can be de-risked even
        after a limit has been breached.
        """
        key = instrument_key(order.instrument)
        if self._book.mark_of(key) is None:
            self._book.mark({key: order.reference_price})
        limits = self._limits
        current = self._book.gross_exposure()
        projected = self._book.gross_exposure(
            {key: self._book.quantity(key) + order.signed_quantity}
        )
        if projected <= current:
            return None
        equity = self._book.equity()
        loss = limits.capital - equity
        if loss >= limits.max_loss:
            return self._reject(order, at, "max_loss", "loss limit reached", loss, limits.max_loss)
        if projected > limits.max_gross_exposure:
            return self._reject(
                order,
                at,
                "max_gross_exposure",
                "gross exposure limit exceeded",
                projected,
                limits.max_gross_exposure,
            )
        if equity <= 0:
            return self._reject(
                order, at, "max_leverage", "equity is not positive", None, limits.max_leverage
            )
        leverage = projected / equity
        if leverage > limits.max_leverage:
            return self._reject(
                order, at, "max_leverage", "leverage limit exceeded", leverage, limits.max_leverage
            )
        return None

    def _reject(
        self,
        order: OrderRecord,
        at: datetime,
        limit_name: str,
        reason: str,
        observed: Decimal | None,
        limit: Decimal,
    ) -> RejectionRecord:
        rejection = RejectionRecord(
            order_id=order.record_id,
            source=RejectionSource.SECOND_LINE_RISK,
            limit_name=limit_name,
            reason=reason,
            observed=observed,
            limit=limit,
            rejected_at=at,
        )
        self._rejections.append(rejection)
        return rejection

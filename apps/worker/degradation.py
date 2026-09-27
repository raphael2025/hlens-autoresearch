"""Degradation monitor: recent metrics vs the validation baseline (Phase 11; ADR-0049).

The monitor compares a strategy's recent (simulated / paper / backtest) metrics with the metrics of
the validation that admitted it and publishes a degradation **event** on the bus when a metric fell
by more than its allowed decline. It never changes lifecycle state: ``ACTIVE -> DEGRADED`` is a
Control Plane transition that cites the event as evidence (ADR-0006).

Thresholds come only from ``ValidationProfile.lifecycle.degradation_thresholds_exact`` when that
exact sibling is present, otherwise from the legacy ``degradation_thresholds`` field (or an
explicit mapping of the same shape); nothing here has a default number. Key convention (like the
gate comparators in ``research/validation/gates.py``):

- ``"<metric>"`` or ``"<metric>[>=]"``: higher is better; degraded when ``baseline - recent``
  exceeds the threshold;
- ``"<metric>[<=]"``: lower is better (e.g. drawdown); degraded when ``recent - baseline`` exceeds
  the threshold.

A metric with a threshold but no recent value is reported as ``missing`` (evidence insufficient),
never as healthy and never as degraded. When **every** ruled metric is missing the check as a whole
is ``insufficient_evidence`` (``status``), never "not degraded": there is no evidence either way.
``observe`` publishes an actual breach only on ``DEGRADATION_TOPIC`` and an all-missing check only
on ``INSUFFICIENT_EVIDENCE_TOPIC`` (D-DEG-IE, ADR-0049): an alert that the monitor cannot decide,
never a degradation, never healthy. Partial missing metrics publish nothing new (a breach still
goes out on ``DEGRADATION_TOPIC`` with the missing list). Neither event changes lifecycle state.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal
from typing import Final, Literal

from core.contracts.event_bus import BusMessage, EventBusAdapter
from core.contracts.validation_profile import ValidationProfile
from core.domain.base import Ref

__all__ = [
    "DEGRADATION_TOPIC",
    "DegradationCheck",
    "DegradationMonitor",
    "DegradationRule",
    "DegradationStatus",
    "INSUFFICIENT_EVIDENCE_TOPIC",
]

DEGRADATION_TOPIC: Final = "research_loop.degradation"
#: every ruled metric lacked a recent value (D-DEG-IE): an alert, not a degradation
INSUFFICIENT_EVIDENCE_TOPIC: Final = "research_loop.degradation.insufficient_evidence"

type DegradationStatus = Literal["degraded", "insufficient_evidence", "not_degraded"]
_KEY: Final = re.compile(r"^(?P<metric>[A-Za-z0-9_.]+)(?:\[(?P<op><=|>=)\])?$")


@dataclass(frozen=True, slots=True)
class DegradationRule:
    metric: str
    lower_is_better: bool
    max_decline: Decimal
    source: str


@dataclass(frozen=True, slots=True)
class DegradationCheck:
    subject: Ref
    breaches: tuple[dict[str, str], ...]
    missing: tuple[str, ...]
    #: every ruled metric lacked a recent value: no evidence either way (never "not degraded")
    insufficient_evidence: bool = False

    def __post_init__(self) -> None:
        if self.insufficient_evidence and (self.breaches or not self.missing):
            raise ValueError("insufficient evidence means every metric is missing, none breached")

    @property
    def degraded(self) -> bool:
        return bool(self.breaches)

    @property
    def status(self) -> DegradationStatus:
        if self.breaches:
            return "degraded"
        if self.insufficient_evidence:
            return "insufficient_evidence"
        return "not_degraded"


def _to_decimal(value: Decimal | float | int, name: str) -> Decimal:
    number = value if isinstance(value, Decimal) else Decimal(repr(value))
    if not number.is_finite():
        raise ValueError(f"{name} must be finite")
    return number


class DegradationMonitor:
    def __init__(
        self,
        thresholds: Mapping[str, float | Decimal],
        *,
        source: str,
        bus: EventBusAdapter | None = None,
    ) -> None:
        if not thresholds:
            raise ValueError("a degradation monitor needs at least one threshold (no defaults)")
        rules: list[DegradationRule] = []
        for key in sorted(thresholds):
            match = _KEY.fullmatch(key)
            if match is None:
                raise ValueError(f"not a degradation threshold key: {key!r}")
            limit = _to_decimal(thresholds[key], key)
            if limit < 0:
                raise ValueError(f"the allowed decline of {key!r} must be >= 0")
            rules.append(
                DegradationRule(
                    metric=match.group("metric"),
                    lower_is_better=match.group("op") == "<=",
                    max_decline=limit,
                    source=f"{source}[{key}]",
                )
            )
        self._rules = tuple(rules)
        self._bus = bus

    @classmethod
    def from_profile(
        cls, profile: ValidationProfile, *, bus: EventBusAdapter | None = None
    ) -> DegradationMonitor:
        exact = profile.lifecycle.degradation_thresholds_exact
        if exact is not None:
            return cls(
                exact,
                source=f"{profile.ref}#lifecycle.degradation_thresholds_exact",
                bus=bus,
            )
        return cls(
            dict(profile.lifecycle.degradation_thresholds),
            source=f"{profile.ref}#lifecycle.degradation_thresholds",
            bus=bus,
        )

    @property
    def rules(self) -> tuple[DegradationRule, ...]:
        return self._rules

    def check(
        self,
        subject: Ref,
        baseline: Mapping[str, Decimal | float | int],
        recent: Mapping[str, Decimal | float | int],
    ) -> DegradationCheck:
        breaches: list[dict[str, str]] = []
        missing: list[str] = []
        for rule in self._rules:
            if rule.metric not in baseline:
                raise ValueError(f"the validation baseline of {subject} lacks {rule.metric!r}")
            if rule.metric not in recent:
                missing.append(rule.metric)
                continue
            base = _to_decimal(baseline[rule.metric], rule.metric)
            now = _to_decimal(recent[rule.metric], rule.metric)
            decline = now - base if rule.lower_is_better else base - now
            if decline > rule.max_decline:
                breaches.append(
                    {
                        "metric": rule.metric,
                        "baseline": str(base),
                        "recent": str(now),
                        "decline": str(decline),
                        "max_decline": str(rule.max_decline),
                        "threshold_source": rule.source,
                    }
                )
        return DegradationCheck(
            subject,
            tuple(breaches),
            tuple(missing),
            insufficient_evidence=len(missing) == len(self._rules),
        )

    def observe(
        self,
        subject: Ref,
        baseline: Mapping[str, Decimal | float | int],
        recent: Mapping[str, Decimal | float | int],
        *,
        window: str,
    ) -> DegradationCheck:
        """``check`` and publish: a breach on ``DEGRADATION_TOPIC``; every ruled metric missing on
        ``INSUFFICIENT_EVIDENCE_TOPIC`` (``window`` names the data). Either needs a bus (fail
        closed); partial missing metrics without a breach publish nothing. Never a lifecycle move.
        """
        result = self.check(subject, baseline, recent)
        if not (result.degraded or result.status == "insufficient_evidence"):
            return result
        if self._bus is None:
            raise ValueError(f"observe needs a bus to publish the {result.status} event")
        key = f"{subject}:{window}"
        if result.degraded:
            message = BusMessage.build(
                DEGRADATION_TOPIC,
                key,
                {
                    "subject": str(subject),
                    "window": window,
                    "breaches": list(result.breaches),
                    "missing": list(result.missing),
                },
            )
        else:
            message = BusMessage.build(
                INSUFFICIENT_EVIDENCE_TOPIC,
                key,
                {
                    "subject": str(subject),
                    "window": window,
                    "status": result.status,
                    "missing": sorted(result.missing),
                    "required": sorted(rule.metric for rule in self._rules),
                },
            )
        self._bus.publish(message)
        return result

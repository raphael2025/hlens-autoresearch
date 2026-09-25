"""Degradation monitor: recent metrics vs the validation baseline (Phase 11; ADR-0049).

The monitor compares a strategy's recent (simulated / paper / backtest) metrics with the metrics of
the validation that admitted it and publishes a degradation **event** on the bus when a metric fell
by more than its allowed decline. It never changes lifecycle state: ``ACTIVE -> DEGRADED`` is a
Control Plane transition that cites the event as evidence (ADR-0006).

Thresholds come only from ``ValidationProfile.lifecycle.degradation_thresholds`` (or an explicit
mapping of the same shape); nothing here has a default number. Key convention (like the gate
comparators in ``research/validation/gates.py``):

- ``"<metric>"`` or ``"<metric>[>=]"``: higher is better; degraded when ``baseline - recent``
  exceeds the threshold;
- ``"<metric>[<=]"``: lower is better (e.g. drawdown); degraded when ``recent - baseline`` exceeds
  the threshold.

A metric with a threshold but no recent value is reported as ``missing`` (evidence insufficient),
never as healthy and never as degraded.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal
from typing import Final

from core.contracts.event_bus import BusMessage, EventBusAdapter
from core.contracts.validation_profile import ValidationProfile
from core.domain.base import Ref

__all__ = ["DEGRADATION_TOPIC", "DegradationCheck", "DegradationMonitor", "DegradationRule"]

DEGRADATION_TOPIC: Final = "research_loop.degradation"
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

    @property
    def degraded(self) -> bool:
        return bool(self.breaches)


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
        return DegradationCheck(subject, tuple(breaches), tuple(missing))

    def observe(
        self,
        subject: Ref,
        baseline: Mapping[str, Decimal | float | int],
        recent: Mapping[str, Decimal | float | int],
        *,
        window: str,
    ) -> DegradationCheck:
        """``check`` and publish a degradation event when degraded (``window`` names the data)."""
        result = self.check(subject, baseline, recent)
        if result.degraded:
            if self._bus is None:
                raise ValueError("observe needs a bus to publish the degradation event")
            self._bus.publish(
                BusMessage.build(
                    DEGRADATION_TOPIC,
                    f"{subject}:{window}",
                    {
                        "subject": str(subject),
                        "window": window,
                        "breaches": list(result.breaches),
                        "missing": list(result.missing),
                    },
                )
            )
        return result

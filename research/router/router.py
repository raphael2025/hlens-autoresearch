"""State-driven routing among validated strategies (roadmap Phase 10; ADR-0043).

- Only strategies whose lifecycle state is ACTIVE or PRODUCTION_CANDIDATE may be routed (roadmap:
  "Router 使用未验证策略" is forbidden); the router itself is a strategy object that must pass full
  validation before promotion, and in this build it only ever drives PAPER runs (no live).
- At time t the weights come from the routing table entry of the state **known at t** (``None``
  state -> the declared fallback, usually flat); weights per state are non-negative and sum to at
  most 1 (the rest is cash).
- Switching cost: every change of weights is turnover ``sum |w_new - w_old|``, charged at the
  declared cost rate, so a router cannot hide its churn (failure mode "路由切换成本吞噬收益").
- Explicit stop (code completion, 2026-09-26): a router with no candidate is refused with
  ``RouterStopped`` (a ``RouterError``) and a typed ``reason`` instead of silently routing flat:
  ``no_validated_candidate`` when the lifecycle map declares no ACTIVE / PRODUCTION_CANDIDATE
  strategy, ``all_routes_flat`` when every state entry and the fallback give zero weight to every
  strategy. A table whose *some* states (or the fallback) are flat is legitimate per-state
  routing and is kept, as long as at least one entry routes positive weight to a validated
  strategy.
- Evidence mode (P10-ELIG, code completion 2026-09-26; ``evidence.py``): with
  ``StrategyRouter(spec, lifecycle, evidence=EligibilityEvidence(...))`` every strategy the spec
  can route must also be backed by its actual ``ValidationReport`` (claimed hash = content hash,
  subject = the routed ref, verdict PASS, a passed G5 sealed-OOS gate). Any refusal raises
  ``RouterEligibilityRefused`` (a ``RouterStopped``, reason ``eligibility_not_evidenced``, with the
  specific per-strategy ``refusal``); the verified checks are ``StrategyRouter.eligibility``.
  Without ``evidence`` (trust mode) nothing changes.

``paper.py`` (W1 wiring) turns the routing weights into combined P5 target positions and runs them
through a ``BacktestProvider``, charging the switching cost on the simulated book.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Final, Literal

from core.domain.base import SHA256_PATTERN, Ref, content_hash
from core.domain.research import ValidationReport
from core.lifecycle.strategy import LifecycleState
from research.router.evidence import (
    EligibilityCheck,
    EligibilityEvidence,
    EligibilityRefusal,
    ReportResolver,
    check_report,
)

__all__ = [
    "RouterEligibilityRefused",
    "RouterError",
    "RouterSpec",
    "RouterStopReason",
    "RouterStopped",
    "RoutingDecision",
    "StrategyRouter",
    "normalize_report_hashes",
]

ROUTABLE = frozenset({LifecycleState.ACTIVE, LifecycleState.PRODUCTION_CANDIDATE})
_SHA256: Final = re.compile(SHA256_PATTERN)


class RouterError(ValueError):
    """The router would route to an unvalidated strategy or an ill-formed table."""


type RouterStopReason = Literal[
    "no_validated_candidate", "all_routes_flat", "eligibility_not_evidenced"
]


class RouterStopped(RouterError):
    """The router has nothing to route: an explicit stop, never a silent flat router."""

    def __init__(
        self,
        reason: RouterStopReason,
        router: str,
        spec_hash: str,
        detail: str,
        *,
        eligibility: tuple[EligibilityCheck, ...] | None = None,
    ) -> None:
        super().__init__(f"router {router} stopped ({reason}): {detail}")
        self.reason: RouterStopReason = reason
        #: ``name@version`` of the stopped router spec.
        self.router = router
        #: ``RouterSpec.spec_hash()`` of the stopped spec.
        self.spec_hash = spec_hash
        self.detail = detail
        #: Evidence mode only (``None`` in trust mode): every eligibility check that was run —
        #: empty when the router stopped before verifying evidence.
        self.eligibility = eligibility


class RouterEligibilityRefused(RouterStopped):
    """Evidence mode: a routed strategy's claimed eligibility is not backed by its report."""

    def __init__(self, router: str, spec_hash: str, checks: tuple[EligibilityCheck, ...]) -> None:
        refused = [check for check in checks if check.refusal is not None]
        if not refused:
            raise ValueError("RouterEligibilityRefused needs at least one refused check")
        detail = "; ".join(f"{c.strategy}: {c.refusal} ({c.detail})" for c in refused)
        super().__init__("eligibility_not_evidenced", router, spec_hash, detail, eligibility=checks)
        first = refused[0]
        #: the first refused strategy (ref string order) and its specific reason
        self.strategy = first.strategy
        self.refusal: EligibilityRefusal | None = first.refusal
        #: every refused strategy -> its reason
        self.refusals: dict[str, EligibilityRefusal | None] = {
            c.strategy: c.refusal for c in refused
        }


@dataclass(frozen=True, slots=True)
class RouterSpec:
    name: str
    version: str
    #: state label -> {strategy ref string -> weight}
    table: Mapping[str, Mapping[str, Decimal]]
    #: weights when the state is unknown (None) or not in the table
    fallback: Mapping[str, Decimal]
    #: cost charged per unit of turnover (a spec parameter, not a validation threshold)
    switching_cost_rate: Decimal

    def spec_hash(self) -> str:
        """Content hash of the spec (weights and rate as their ``Decimal`` text)."""

        def weights(values: Mapping[str, Decimal]) -> dict[str, str]:
            return {key: str(value) for key, value in values.items()}

        return content_hash(
            {
                "name": self.name,
                "version": self.version,
                "table": {label: weights(values) for label, values in self.table.items()},
                "fallback": weights(self.fallback),
                "switching_cost_rate": str(self.switching_cost_rate),
            }
        )

    def strategies(self) -> frozenset[str]:
        """Every strategy ref string the table or the fallback can route to."""
        return frozenset(key for values in (*self.table.values(), self.fallback) for key in values)


@dataclass(frozen=True, slots=True)
class RoutingDecision:
    at: datetime
    state: str | None
    weights: Mapping[str, Decimal]
    turnover: Decimal
    switching_cost: Decimal


def _check_weights(weights: Mapping[str, Decimal], where: str) -> None:
    for key, weight in weights.items():
        if not isinstance(weight, Decimal) or not weight.is_finite() or weight < 0:
            raise RouterError(f"{where}: weight of {key} must be a finite, non-negative Decimal")
    if sum(weights.values(), Decimal(0)) > 1:
        raise RouterError(f"{where}: weights must sum to at most 1 (no leverage)")


def normalize_report_hashes(reports: Mapping[Ref, str] | Mapping[str, str]) -> dict[str, str]:
    """Normalize ``strategy ref -> validation report hash``; refuse ill-formed entries."""
    if not isinstance(reports, Mapping):
        raise RouterError("validation_reports must map strategy refs to report hashes")
    out: dict[str, str] = {}
    for key, value in reports.items():
        name = str(key) if isinstance(key, Ref) else key
        if not isinstance(name, str) or not name:
            raise RouterError(f"validation_reports: {key!r} is not a strategy ref")
        if name in out:
            raise RouterError(f"validation_reports: {name} is given twice")
        if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
            raise RouterError(f"validation_reports: {name} needs a sha256 report hash")
        out[name] = value
    return dict(sorted(out.items()))


def _evidence_reports(
    evidence: EligibilityEvidence, routed: frozenset[str]
) -> tuple[dict[str, str], Mapping[str, ValidationReport] | ReportResolver]:
    """Normalize evidence-mode inputs; ill-formed input is a plain ``RouterError``."""
    if not isinstance(evidence, EligibilityEvidence):
        raise RouterError("evidence must be an EligibilityEvidence")
    hashes = normalize_report_hashes(evidence.report_hashes)
    extra = sorted(set(hashes) - routed)
    if extra:
        raise RouterError(f"validation reports for strategies the router never routes: {extra}")
    if not isinstance(evidence.reports, Mapping):
        if not callable(evidence.reports):
            raise RouterError("evidence.reports must be a mapping or a resolver")
        return hashes, evidence.reports
    reports: dict[str, ValidationReport] = {}
    for key, report in evidence.reports.items():
        name = str(key) if isinstance(key, Ref) else key
        if not isinstance(name, str) or not name:
            raise RouterError(f"evidence.reports: {key!r} is not a strategy ref")
        if name in reports:
            raise RouterError(f"evidence.reports: {name} is given twice")
        reports[name] = report
    stray = sorted(set(reports) - routed)
    if stray:
        raise RouterError(f"reports for strategies the router never routes: {stray}")
    return hashes, reports


class StrategyRouter:
    def __init__(
        self,
        spec: RouterSpec,
        lifecycle: Mapping[Ref, LifecycleState],
        *,
        evidence: EligibilityEvidence | None = None,
    ) -> None:
        normalized = None if evidence is None else _evidence_reports(evidence, spec.strategies())
        stopped_before_evidence = None if evidence is None else ()
        routable = {str(ref) for ref, state in lifecycle.items() if state in ROUTABLE}
        entries = (*spec.table.items(), ("<fallback>", spec.fallback))
        for label, weights in entries:
            _check_weights(weights, f"state {label}")
        if not spec.switching_cost_rate.is_finite() or spec.switching_cost_rate < 0:
            raise RouterError("switching_cost_rate must be a finite, non-negative Decimal")
        name = f"{spec.name}@{spec.version}"
        if not routable:
            raise RouterStopped(
                "no_validated_candidate",
                name,
                spec.spec_hash(),
                "the lifecycle map declares no ACTIVE / PRODUCTION_CANDIDATE strategy",
                eligibility=stopped_before_evidence,
            )
        for label, weights in entries:
            unvalidated = sorted(set(weights) - routable)
            if unvalidated:
                raise RouterError(f"state {label} routes to unvalidated strategies {unvalidated}")
        if not any(weight > 0 for _, weights in entries for weight in weights.values()):
            raise RouterStopped(
                "all_routes_flat",
                name,
                spec.spec_hash(),
                "every state entry and the fallback give zero weight to every strategy",
                eligibility=stopped_before_evidence,
            )
        self._spec = spec
        self._report_hashes: dict[str, str] | None = None
        self._eligibility: tuple[EligibilityCheck, ...] | None = None
        if normalized is not None:
            hashes, reports = normalized
            claimed = {str(ref): LifecycleState(state).value for ref, state in lifecycle.items()}
            checks = tuple(
                check_report(key, claimed[key], hashes.get(key), reports)
                for key in sorted(spec.strategies())
            )
            if not all(check.verified for check in checks):
                raise RouterEligibilityRefused(name, spec.spec_hash(), checks)
            self._report_hashes = hashes
            self._eligibility = checks

    @property
    def spec(self) -> RouterSpec:
        return self._spec

    @property
    def eligibility(self) -> tuple[EligibilityCheck, ...] | None:
        """Evidence mode: the verified check of every routable strategy (``None``: trust mode)."""
        return self._eligibility

    @property
    def report_hashes(self) -> Mapping[str, str] | None:
        """Evidence mode: the verified strategy ref -> report hash map (``None``: trust mode)."""
        return None if self._report_hashes is None else dict(self._report_hashes)

    def route(self, states: Sequence[tuple[datetime, str | None]]) -> tuple[RoutingDecision, ...]:
        decisions: list[RoutingDecision] = []
        previous: Mapping[str, Decimal] = {}
        last_at: datetime | None = None
        for at, state in states:
            if last_at is not None and at <= last_at:
                raise RouterError("states must be strictly increasing in time")
            last_at = at
            weights = (
                self._spec.table.get(state, self._spec.fallback)
                if state is not None
                else self._spec.fallback
            )
            keys = set(weights) | set(previous)
            turnover = sum(
                (abs(weights.get(k, Decimal(0)) - previous.get(k, Decimal(0))) for k in keys),
                Decimal(0),
            )
            decisions.append(
                RoutingDecision(
                    at=at,
                    state=state,
                    weights=dict(weights),
                    turnover=turnover,
                    switching_cost=turnover * self._spec.switching_cost_rate,
                )
            )
            previous = weights
        return tuple(decisions)

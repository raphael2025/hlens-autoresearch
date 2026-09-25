"""Provider-agnostic contract suite for ``RiskProvider`` (ADR-0038, Phase 5).

A compliant provider: declares itself; answers every supported request with a ``RiskResult`` that
survives re-validation and ``check_answers`` (one position per target, requested weights echoed,
input times inside the visible set, every adjustment named by a rule of the policy); is
deterministic; and refuses an unsupported policy (``UnsupportedRiskPolicy``).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

import pytest

from core.contracts.strategy import (
    RiskProvider,
    RiskProviderDescriptor,
    RiskRequest,
    RiskResult,
    UnsupportedRiskPolicy,
)
from tests.contract_suites._support import call_ok, expect_error, require, revalidated

__all__ = ["RISK_CHECKS", "RiskCheck", "RiskProviderContract", "RiskSubject"]


@dataclass(frozen=True)
class RiskSubject:
    """``requests`` are supported; ``unsupported`` must be refused; ``rules`` are the policy's."""

    open: Callable[[], RiskProvider]
    requests: tuple[RiskRequest, ...]
    unsupported: RiskRequest
    rules: frozenset[str]


type RiskCheck = Callable[[RiskSubject], None]


def _answer(provider: RiskProvider, request: RiskRequest) -> RiskResult:
    result = call_ok("constrain", lambda: provider.constrain(request))
    return revalidated(RiskResult, result, "constrain result")


def check_descriptor(subject: RiskSubject) -> None:
    provider = subject.open()
    revalidated(RiskProviderDescriptor, provider.descriptor, "descriptor")


def _check_answers(result: RiskResult, request: RiskRequest, provider: RiskProvider) -> None:
    call_ok("check_answers", lambda: result.check_answers(request, provider.descriptor))


def check_answers(subject: RiskSubject) -> None:
    require(bool(subject.requests), "the suite needs at least one request")
    provider = subject.open()
    for request in subject.requests:
        result = _answer(provider, request)
        _check_answers(result, request, provider)
        for item in result.positions:
            unknown = set(item.binding_rules) - subject.rules
            require(not unknown, f"{item.instrument}: rules {sorted(unknown)} are not the policy's")


def check_determinism(subject: RiskSubject) -> None:
    for request in subject.requests:
        first = _answer(subject.open(), request)
        second = _answer(subject.open(), request)
        require(first.result_hash == second.result_hash, "equal requests must give equal results")


def check_refuses_unsupported(subject: RiskSubject) -> None:
    provider = subject.open()
    expect_error(
        UnsupportedRiskPolicy,
        "an unsupported policy",
        lambda: provider.constrain(subject.unsupported),
    )


RISK_CHECKS: tuple[RiskCheck, ...] = (
    check_descriptor,
    check_answers,
    check_determinism,
    check_refuses_unsupported,
)


class RiskProviderContract:
    """pytest entry point: subclass as ``Test*`` and provide ``risk_subject``."""

    @pytest.fixture
    def risk_subject(self) -> RiskSubject:
        raise NotImplementedError("subclasses provide risk_subject")

    @pytest.mark.parametrize("check", RISK_CHECKS, ids=lambda check: check.__name__)
    def test_risk_contract(self, risk_subject: RiskSubject, check: RiskCheck) -> None:
        check(risk_subject)

"""Provider-agnostic contract suite for ``StrategyProvider`` (ADR-0038, Phase 5).

A compliant provider: declares itself; answers a supported request with a ``StrategyResult`` that
survives re-validation and ``check_answers`` (one position per decision time × instrument, input
times inside the visible set); is deterministic across fresh instances; is causal — changing every
signal that becomes available after ``t`` leaves every position at or before ``t`` unchanged; and
refuses an unsupported request (``UnsupportedStrategy``).

Reuse::

    class TestMyStrategy(StrategyProviderContract):
        @pytest.fixture
        def strategy_subject(self) -> StrategySubject: ...
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

import pytest

from core.contracts.strategy import (
    SignalObservation,
    StrategyProvider,
    StrategyProviderDescriptor,
    StrategyRequest,
    StrategyResult,
    UnsupportedStrategy,
)
from tests.contract_suites._support import call_ok, expect_error, require, revalidated

__all__ = ["STRATEGY_CHECKS", "StrategyCheck", "StrategyProviderContract", "StrategySubject"]


@dataclass(frozen=True)
class StrategySubject:
    """``request`` is supported; ``unsupported`` must be refused; ``perturb`` changes a value."""

    open: Callable[[], StrategyProvider]
    request: StrategyRequest
    unsupported: StrategyRequest
    perturb: Callable[[SignalObservation], SignalObservation]


type StrategyCheck = Callable[[StrategySubject], None]


def _answer(provider: StrategyProvider, request: StrategyRequest) -> StrategyResult:
    result = call_ok("target_positions", lambda: provider.target_positions(request))
    return revalidated(StrategyResult, result, "target_positions result")


def check_descriptor(subject: StrategySubject) -> None:
    provider = subject.open()
    revalidated(StrategyProviderDescriptor, provider.descriptor, "descriptor")
    require(provider.descriptor == provider.descriptor, "descriptor must be stable")


def check_answers(subject: StrategySubject) -> None:
    provider = subject.open()
    result = _answer(provider, subject.request)
    call_ok("check_answers", lambda: result.check_answers(subject.request, provider.descriptor))


def check_determinism(subject: StrategySubject) -> None:
    first = _answer(subject.open(), subject.request)
    second = _answer(subject.open(), subject.request)
    require(first.result_hash == second.result_hash, "equal requests must give equal results")


def check_causality(subject: StrategySubject) -> None:
    """No look-ahead: signals available after ``t`` never move a position at or before ``t``."""
    times = subject.request.decision_times
    require(len(times) >= 2, "the suite needs at least two decision times")
    cut = times[len(times) // 2 - 1]
    later = [item for item in subject.request.signals if item.available_time > cut]
    require(bool(later), "the suite needs signals available after the cut")
    perturbed = subject.request.model_copy(
        update={
            "signals": tuple(
                subject.perturb(item) if item.available_time > cut else item
                for item in subject.request.signals
            )
        }
    )
    base = _answer(subject.open(), subject.request)
    moved = _answer(subject.open(), perturbed)
    early = [item for item in base.positions if item.decision_time <= cut]
    require(
        early == [item for item in moved.positions if item.decision_time <= cut],
        f"positions at or before {cut.isoformat()} changed when only later signals changed",
    )


def check_refuses_unsupported(subject: StrategySubject) -> None:
    provider = subject.open()
    expect_error(
        UnsupportedStrategy,
        "an unsupported request",
        lambda: provider.target_positions(subject.unsupported),
    )


STRATEGY_CHECKS: tuple[StrategyCheck, ...] = (
    check_descriptor,
    check_answers,
    check_determinism,
    check_causality,
    check_refuses_unsupported,
)


class StrategyProviderContract:
    """pytest entry point: subclass as ``Test*`` and provide ``strategy_subject``."""

    @pytest.fixture
    def strategy_subject(self) -> StrategySubject:
        raise NotImplementedError("subclasses provide strategy_subject")

    @pytest.mark.parametrize("check", STRATEGY_CHECKS, ids=lambda check: check.__name__)
    def test_strategy_contract(
        self, strategy_subject: StrategySubject, check: StrategyCheck
    ) -> None:
        check(strategy_subject)

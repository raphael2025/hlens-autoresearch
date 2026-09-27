"""The StateProvider contract suite has teeth (ADR-0035 §4; smoke level).

Each variant below breaks exactly one rule on top of a compliant provider; the named check must
reject it with ``ContractSuiteFailure`` while the compliant base passes (tests/plugins/states).
"""

from __future__ import annotations

from collections.abc import Callable
from itertools import count

import pytest

from core.contracts.state import MethodParam, StateRequest, StateResult
from plugins.states import VolatilityRegimeProvider
from tests.contract_suites import state as suite
from tests.contract_suites._support import ContractSuiteFailure
from tests.contract_suites.state import StateSubject
from tests.fake_states import (
    TEST_CUTS,
    TEST_MIN_HISTORY,
    TEST_SEED,
    TEST_WINDOW,
    VOL_FEATURE,
    at,
    level_inputs,
    negate,
)

SPEC = VolatilityRegimeProvider.spec(
    VOL_FEATURE,
    cuts=TEST_CUTS,
    min_history=TEST_MIN_HISTORY,
    training_window=TEST_WINDOW,
    seed=TEST_SEED,
)


class IgnoresWindow(VolatilityRegimeProvider):
    """Fits on every past input it is given (an expanding, not a fixed, window)."""

    def compute(self, request: StateRequest) -> StateResult:
        params: dict[str, MethodParam] = {
            "cuts": ",".join(TEST_CUTS),
            "min_history": TEST_MIN_HISTORY,
        }
        values = [
            self._value(SPEC, params, t, [i for i in request.inputs if i.evaluation_time <= t])
            for t in request.evaluation_times
        ]
        return StateResult.build(request, self.descriptor, values)


class PeeksAhead(VolatilityRegimeProvider):
    """Labels ``t`` with the state of the last input of the whole request (reports honest times)."""

    def compute(self, request: StateRequest) -> StateResult:
        base = super().compute(request)
        if not request.inputs:
            return base
        end = request.inputs[-1].evaluation_time
        ahead = request.model_copy(update={"evaluation_times": (end,)})
        [future] = super().compute(ahead).values
        values = [
            v.model_copy(update={"state": future.state})
            if v.state is not None and future.state is not None
            else v
            for v in base.values
        ]
        return StateResult.build(request, self.descriptor, values)


class Flips(VolatilityRegimeProvider):
    """Nondeterministic: every other call relabels computable states."""

    _calls = count()

    def compute(self, request: StateRequest) -> StateResult:
        base = super().compute(request)
        if next(self._calls) % 2 == 0:
            return base
        values = [
            v.model_copy(update={"state": SPEC.state_space[0]}) if v.state is not None else v
            for v in base.values
        ]
        return StateResult.build(request, self.descriptor, values)


class OutsideSpace(VolatilityRegimeProvider):
    def compute(self, request: StateRequest) -> StateResult:
        base = super().compute(request)
        values = [
            v.model_copy(update={"state": "bogus"}) if v.state is not None else v
            for v in base.values
        ]
        return StateResult.build(request, self.descriptor, values)


def _subject(make: Callable[[], VolatilityRegimeProvider]) -> StateSubject:
    return StateSubject(
        open=make,
        spec=SPEC,
        inputs=level_inputs(VOL_FEATURE),
        evaluation_times=tuple(at(i) for i in range(26)),
        perturb=negate,
    )


@pytest.mark.parametrize(
    ("variant", "check"),
    [
        (IgnoresWindow, suite.check_training_window_is_respected),
        (PeeksAhead, suite.check_causal_perturbation),
        (Flips, suite.check_determinism),
        (OutsideSpace, suite.check_labels_belong_to_the_state_space),
    ],
    ids=lambda item: getattr(item, "__name__", str(item)),
)
def test_a_single_fault_is_caught(
    variant: type[VolatilityRegimeProvider], check: suite.StateCheck
) -> None:
    compliant = _subject(lambda: VolatilityRegimeProvider((SPEC,)))
    check(compliant)
    with pytest.raises(ContractSuiteFailure):
        check(_subject(lambda: variant((SPEC,))))

"""The EventProvider contract suite has teeth (Phase 3; ADR-0036 §6).

The compliant plugins pass the whole suite (``tests/plugins/events``). Here each deliberately
faulty provider of ``tests/fake_events.py`` is killed by the named check, while it still passes a
basic check — the suite kills it by the targeted behaviour, not because it is broken overall.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from plugins.events import FeatureThresholdCrossProvider
from tests.contract_suites import event as suite
from tests.contract_suites._support import ContractSuiteFailure
from tests.contract_suites.event import EventCheck, EventSubject
from tests.fake_events import (
    AS_OF_TIMES,
    LAG,
    X_INPUTS,
    BackdatedProvider,
    ConfirmedTopProvider,
    X,
    perturb_number,
)

SPEC = FeatureThresholdCrossProvider.spec(X, Decimal("4.5"), "both", observable_lag=LAG)


def _subject(cls: type[FeatureThresholdCrossProvider]) -> EventSubject:
    return EventSubject(
        open=lambda: cls((SPEC,)),
        spec=SPEC,
        as_of_times=AS_OF_TIMES,
        inputs=X_INPUTS,
        perturb=perturb_number,
    )


@pytest.mark.parametrize(
    ("cls", "check"),
    [
        (ConfirmedTopProvider, suite.check_point_in_time_consistency),
        (ConfirmedTopProvider, suite.check_only_the_visible_set_matters),
        (BackdatedProvider, suite.check_fixture_has_events),
        (BackdatedProvider, suite.check_point_in_time_consistency),
    ],
    ids=lambda value: getattr(value, "__name__", str(value)),
)
def test_faulty_provider_is_killed(
    cls: type[FeatureThresholdCrossProvider], check: EventCheck
) -> None:
    subject = _subject(cls)
    if cls is ConfirmedTopProvider:  # every single answer is well-formed
        suite.check_descriptor_declares_the_spec(subject)
    with pytest.raises(ContractSuiteFailure):
        check(subject)


def test_the_compliant_twin_passes_every_check() -> None:
    subject = _subject(FeatureThresholdCrossProvider)
    for check in suite.EVENT_CHECKS:
        check(subject)

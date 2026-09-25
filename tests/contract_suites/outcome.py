"""Provider-agnostic contract suite for ``OutcomeProvider`` (core/contracts/outcome.py; ADR-0037).

An implementation provides an ``OutcomeSubject``:

- ``open``: returns a **new** provider instance on every call (simulated restart);
- ``label_spec``: a label spec the provider declares; ``other_spec``: one it does not declare;
- ``bars`` / ``events`` / ``manifest_content_hash``: a request fixture in which at least one
  event has a computable label and at least one event has no complete window (for example, the
  last event close to the end of the bars).

The checks look only at observable behaviour: the descriptor, ``compute`` and how labels change
under truncated or perturbed prices. Every failure is a ``ContractSuiteFailure``.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal
from functools import partial
from typing import Any

import pytest
from pydantic import ValidationError

from core.contracts.feature import FeatureObservation
from core.contracts.outcome import (
    OutcomeEvent,
    OutcomeLabel,
    OutcomeLabelSpec,
    OutcomePriceBar,
    OutcomeProvider,
    OutcomeProviderDescriptor,
    OutcomeRequest,
    OutcomeResult,
    UnsupportedOutcome,
    is_outcome_payload,
)
from tests.contract_suites._support import (
    ContractSuiteFailure,
    call_ok,
    expect_error,
    require,
    revalidated,
)

__all__ = ["OUTCOME_CHECKS", "OutcomeCheck", "OutcomeProviderContract", "OutcomeSubject"]


@dataclass(frozen=True)
class OutcomeSubject:
    open: Callable[[], OutcomeProvider]
    label_spec: OutcomeLabelSpec
    other_spec: OutcomeLabelSpec
    bars: tuple[OutcomePriceBar, ...]
    events: tuple[OutcomeEvent, ...]
    manifest_content_hash: str


type OutcomeCheck = Callable[[OutcomeSubject], None]


def _request(
    subject: OutcomeSubject,
    *,
    bars: tuple[OutcomePriceBar, ...] | None = None,
    events: tuple[OutcomeEvent, ...] | None = None,
    label_spec: OutcomeLabelSpec | None = None,
) -> OutcomeRequest:
    use_bars = subject.bars if bars is None else bars
    return OutcomeRequest(
        label_spec=subject.label_spec if label_spec is None else label_spec,
        manifest_content_hash=subject.manifest_content_hash,
        price_cutoff=max(bar.available_time for bar in subject.bars),
        events=subject.events if events is None else events,
        bars=use_bars,
    )


def _compute(provider: OutcomeProvider, request: OutcomeRequest, what: str) -> OutcomeResult:
    result = call_ok(what, partial(provider.compute, request))
    return revalidated(OutcomeResult, result, what)


def _label(subject: OutcomeSubject, event: OutcomeEvent, bars: tuple[Any, ...]) -> OutcomeLabel:
    result = _compute(subject.open(), _request(subject, bars=bars, events=(event,)), "compute")
    return result.labels[0]


def _scaled(bar: OutcomePriceBar, factor: Decimal) -> OutcomePriceBar:
    return OutcomePriceBar(
        interval_start=bar.interval_start,
        interval_end=bar.interval_end,
        available_time=bar.available_time,
        open=bar.open * factor,
        high=bar.high * factor,
        low=bar.low * factor,
        close=bar.close * factor,
    )


def _computable(subject: OutcomeSubject) -> list[OutcomeLabel]:
    result = _compute(subject.open(), _request(subject), "compute")
    labels = [label for label in result.labels if label.value is not None]
    require(bool(labels), "the subject must have at least one computable label")
    return labels


# ======================================================================================
# Checks
# ======================================================================================


def check_descriptor_declares_the_spec(subject: OutcomeSubject) -> None:
    descriptor = subject.open().descriptor
    require(type(descriptor) is OutcomeProviderDescriptor, "descriptor has the wrong type")
    require(descriptor.deterministic is True, "an OutcomeProvider must be deterministic")
    require(descriptor.supports(subject.label_spec), "the descriptor must declare label_spec")
    require(descriptor == subject.open().descriptor, "the descriptor must be stable")


def check_answers_match_the_request(subject: OutcomeSubject) -> None:
    provider = subject.open()
    request = _request(subject)
    result = _compute(provider, request, "compute")
    try:
        result.check_answers(request, provider.descriptor)
    except ValueError as exc:
        raise ContractSuiteFailure(f"answers do not match the request: {exc}") from exc
    require(
        any(label.value is None for label in result.labels),
        "the subject must include an event without a complete window",
    )


def check_determinism(subject: OutcomeSubject) -> None:
    request = _request(subject)
    first = _compute(subject.open(), request, "compute (first instance)")
    second = _compute(subject.open(), request, "compute (second instance)")
    require(first.result_hash == second.result_hash, "equal requests must give equal results")


def check_labels_ignore_prices_after_they_are_known(subject: OutcomeSubject) -> None:
    """Truncating or perturbing bars that become available after the label must not change it."""
    for label in _computable(subject):
        assert label.available_time is not None
        event = next(e for e in subject.events if e.event_key == label.event_key)
        known = tuple(bar for bar in subject.bars if bar.available_time <= label.available_time)
        perturbed = tuple(
            bar if bar.available_time <= label.available_time else _scaled(bar, Decimal(2))
            for bar in subject.bars
        )
        require(_label(subject, event, known) == label, "a label changed after truncation")
        require(_label(subject, event, perturbed) == label, "a label saw later prices")


def check_labels_ignore_prices_before_the_event(subject: OutcomeSubject) -> None:
    """Bars that end at or before the event time are not part of the label window."""
    for label in _computable(subject):
        event = next(e for e in subject.events if e.event_key == label.event_key)
        perturbed = tuple(
            _scaled(bar, Decimal(3)) if bar.interval_end <= event.event_time else bar
            for bar in subject.bars
        )
        require(_label(subject, event, perturbed) == label, "a label used pre-event prices")


def check_missing_data_is_explicit_none(subject: OutcomeSubject) -> None:
    """With the window's last bar removed, a label is ``None`` or known before the cut.

    The cut result must still satisfy ``check_answers`` (no exit short of the horizon without a
    barrier touch), so a provider that fills a partial window is caught.
    """
    for label in _computable(subject):
        assert label.exit_time is not None and label.available_time is not None
        event = next(e for e in subject.events if e.event_key == label.event_key)
        cut = tuple(bar for bar in subject.bars if bar.interval_end < label.exit_time)
        if not cut:
            continue
        provider = subject.open()
        request = _request(subject, bars=cut, events=(event,))
        result = _compute(provider, request, "compute(cut)")
        try:
            result.check_answers(request, provider.descriptor)
        except ValueError as exc:
            raise ContractSuiteFailure(f"a cut window was filled: {exc}") from exc
        again = result.labels[0]
        require(
            again.value is None
            or (again.available_time is not None and again.available_time < label.available_time),
            "a label was filled beyond the available data",
        )


def check_event_order_is_irrelevant(subject: OutcomeSubject) -> None:
    forward = _compute(subject.open(), _request(subject), "compute")
    backward = _compute(
        subject.open(), _request(subject, events=tuple(reversed(subject.events))), "compute"
    )
    require(forward.result_hash == backward.result_hash, "event order changed the result")


def check_outcomes_are_label_only(subject: OutcomeSubject) -> None:
    result = _compute(subject.open(), _request(subject), "compute")
    require(is_outcome_payload(result), "a result must be recognisable as an Outcome payload")
    for payload in (result.labels[0], result.labels[0].model_dump(mode="json")):
        require(is_outcome_payload(payload), "a label must be recognisable as an Outcome payload")
        expect_error(
            ValidationError,
            "FeatureObservation.model_validate(outcome label)",
            partial(FeatureObservation.model_validate, payload),
        )


def check_unsupported_spec_is_refused(subject: OutcomeSubject) -> None:
    provider = subject.open()
    require(not provider.descriptor.supports(subject.other_spec), "other_spec must be undeclared")
    request = _request(subject, label_spec=subject.other_spec)
    expect_error(UnsupportedOutcome, "compute(undeclared spec)", partial(provider.compute, request))


OUTCOME_CHECKS: tuple[OutcomeCheck, ...] = (
    check_descriptor_declares_the_spec,
    check_answers_match_the_request,
    check_determinism,
    check_labels_ignore_prices_after_they_are_known,
    check_labels_ignore_prices_before_the_event,
    check_missing_data_is_explicit_none,
    check_event_order_is_irrelevant,
    check_outcomes_are_label_only,
    check_unsupported_spec_is_refused,
)


class OutcomeProviderContract:
    """pytest entry point: subclasses named ``Test*`` provide an ``outcome_subject`` fixture."""

    @pytest.fixture
    def outcome_subject(self) -> OutcomeSubject:
        raise NotImplementedError("subclasses must provide the outcome_subject fixture")

    @pytest.mark.parametrize("check", OUTCOME_CHECKS, ids=lambda check: check.__name__)
    def test_outcome_contract(self, outcome_subject: OutcomeSubject, check: OutcomeCheck) -> None:
        check(outcome_subject)

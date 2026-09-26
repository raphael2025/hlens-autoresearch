"""`EventRequest.subject` (ADR-0057; backlog P3-MULTISYM): one request per subject, bound results.

Golden pins (``PRE_ADR_0057``) were computed with the code **before** the field existed (commit
``cc30ece``): a request without ``subject`` must keep its request hash, event ids and result hash
bit for bit. With a subject, the subject is part of every identity and is checked end to end.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from pydantic import ValidationError

from core.contracts.event import EventRequest, EventResult
from infrastructure.event.runner import UpstreamVerificationError, run_events
from infrastructure.event.table import event_table
from plugins.events import EventSequenceProvider, FeatureThresholdCrossProvider, StateSwitchProvider
from tests.contract_version_support import at_pre_bump, at_version, built_at, built_at_pre_bump
from tests.fake_events import LAG, MINUTE, REGIME, REGIME_INPUTS, X_INPUTS, X, request

CROSS = FeatureThresholdCrossProvider.spec(X, Decimal("4.5"), "both", observable_lag=LAG)
CROSS_UP = FeatureThresholdCrossProvider.spec(X, Decimal("4.5"), "up", observable_lag=LAG)
SWITCH = StateSwitchProvider.spec(REGIME, observable_lag=LAG)
SEQUENCE = EventSequenceProvider.spec(CROSS_UP, SWITCH, 3 * MINUTE, observable_lag=LAG)

#: Hashes of the unchanged fixtures, computed before ADR-0057 (see module docs).
PRE_ADR_0057 = {
    "cross_request": "12c63b77745f1dd11e669e0813ba2d5c254792787bf7ee75a3a68f87595641ea",
    "cross_result_hash": "92534a0661b515e1b3cb723d2106c8d5877e411417f1db5b8ea7ba1f7e4eefdf",
    "cross_result_content": "b86a14eef50cdf34eb012a6a94a8a4d4265f3b05e51fbb2c7b51008baac2533b",
    "cross_first_event": "375136f05c94cc2f18270fe1ec7a8c26ee36d7e1dbe469f1444c6a66f0b18c69",
    "switch_result_hash": "62f903fa49d9c87eaa2485aaf4402326cd8e4a2a69d900fc4ecb231f9fbdaa4d",
}
#: The same objects built now carry the 2.1.0 envelope (ADR-0052 M2: new objects are 2.1.0
#: and the envelope is part of every content hash); pinned next to the 2.0.0 evidence above.
AT_2_1_0 = {
    "cross_request": "d61cd4dde180a3a20e21893a3ac61c1d7d25a9964537c252d7a2f5db44345397",
    "cross_result_hash": "c29f89a2f2584da67565052ba8e1882381af08d25c9e7b9f1856508536bf1ff9",
    "cross_result_content": "e009d61ad9d5f74e653de04fd17aeb49ccbb8509e6233791b05f2480352d9f17",
    "cross_first_event": "e153d1a22664a51ee08e45c8114637d71729a77f18eeaf807a9262f83f8d61f3",
    "switch_result_hash": "70b8a9f3a083fff1decaa194902d54a61e3e030eb0e1c91b470eef047a603cd7",
}


def _cross(subject: str | None = None) -> EventResult:
    req = (
        request(CROSS, inputs=X_INPUTS)
        if subject is None
        else request(CROSS, inputs=X_INPUTS, subject=subject)
    )
    return run_events(FeatureThresholdCrossProvider((CROSS,)), CROSS, req)


# ======================================================================================
# absent subject: bit-identical to before
# ======================================================================================


def test_without_a_subject_every_hash_is_unchanged() -> None:
    # The pins were taken at contract 2.0.0; ADR-0057 is re-declared at 2.1.0 (ADR-0052 §4), so
    # the no-subject objects are rebuilt exactly as the 2.0.0 code built them (every envelope
    # 2.0.0) and must still hash to the pins bit for bit.
    with built_at_pre_bump():
        cross, switch_spec = at_pre_bump(CROSS), at_pre_bump(SWITCH)
        req = at_pre_bump(request(cross, inputs=X_INPUTS))
        assert req.subject is None and "subject" not in req.model_dump(mode="json")
        assert req.content_hash() == PRE_ADR_0057["cross_request"]
        result = run_events(FeatureThresholdCrossProvider((cross,)), cross, req)
        switch = run_events(
            StateSwitchProvider((switch_spec,)),
            switch_spec,
            at_pre_bump(request(switch_spec, inputs=REGIME_INPUTS)),
        )
    assert result.schema_version == "2.0.0"
    assert result.subject is None and "subject" not in result.model_dump(mode="json")
    assert result.result_hash == PRE_ADR_0057["cross_result_hash"]
    assert result.content_hash() == PRE_ADR_0057["cross_result_content"]
    assert result.events[0].event_id == PRE_ADR_0057["cross_first_event"]
    assert all("subject" not in item.model_dump(mode="json") for item in result.events)
    assert switch.result_hash == PRE_ADR_0057["switch_result_hash"]
    # The 2.1.0 pins (ADR-0055: checked on what the 2.1.0 code builds; 2.2.0 is now current).
    with built_at("2.1.0"):
        cross, switch_spec = at_version(CROSS, "2.1.0"), at_version(SWITCH, "2.1.0")
        cross_req = at_version(request(cross, inputs=X_INPUTS), "2.1.0")
        now = run_events(FeatureThresholdCrossProvider((cross,)), cross, cross_req)
        now_switch = run_events(
            StateSwitchProvider((switch_spec,)),
            switch_spec,
            at_version(request(switch_spec, inputs=REGIME_INPUTS), "2.1.0"),
        )
    assert now.schema_version == "2.1.0"
    assert {
        "cross_request": cross_req.content_hash(),
        "cross_result_hash": now.result_hash,
        "cross_result_content": now.content_hash(),
        "cross_first_event": now.events[0].event_id,
        "switch_result_hash": now_switch.result_hash,
    } == AT_2_1_0


# ======================================================================================
# a subject is bound end to end
# ======================================================================================


def test_a_subject_binds_the_request_the_events_and_the_result() -> None:
    plain, bound = _cross(), _cross("BTCUSDT")
    assert bound.subject == "BTCUSDT"
    assert all(item.subject == "BTCUSDT" for item in bound.events)
    assert bound.request_hash != plain.request_hash
    assert bound.result_hash != plain.result_hash
    assert {item.event_id for item in bound.events}.isdisjoint(
        {item.event_id for item in plain.events}
    )
    assert sorted(item.event_time for item in bound.events) == sorted(
        item.event_time for item in plain.events
    )
    # JSON round trip keeps the binding (and re-verifies every id / hash)
    again = EventResult.model_validate_json(bound.model_dump_json())
    assert again == bound


def test_two_subjects_make_one_table_with_a_subject_key() -> None:
    rows = event_table(_cross("AAA")) + event_table(_cross("BBB"))
    assert {row.subject for row in rows} == {"AAA", "BBB"}
    assert len({row.event_id for row in rows}) == len(rows)  # no id collides across subjects


def test_the_runner_keeps_the_subject_on_every_checkpoint() -> None:
    req = request(CROSS, inputs=X_INPUTS, subject="S")
    assert req.truncated(req.as_of - MINUTE, LAG).subject == "S"
    direct = FeatureThresholdCrossProvider((CROSS,)).detect(req)
    assert run_events(FeatureThresholdCrossProvider((CROSS,)), CROSS, req) == direct


def test_an_interaction_runs_per_subject() -> None:
    up = run_events(
        FeatureThresholdCrossProvider((CROSS_UP,)),
        CROSS_UP,
        request(CROSS_UP, inputs=X_INPUTS, subject="S"),
    )
    switch = run_events(
        StateSwitchProvider((SWITCH,)), SWITCH, request(SWITCH, inputs=REGIME_INPUTS, subject="S")
    )
    req = request(
        SEQUENCE,
        upstream_events=up.events + switch.events,
        subject="S",
    )
    result = run_events(
        EventSequenceProvider((SEQUENCE,)),
        SEQUENCE,
        req,
        upstream_specs=(CROSS_UP, SWITCH),
        upstream_results=(up, switch),
    )
    assert result.events and result.subject == "S"
    assert all(item.subject == "S" for item in result.events)


# ======================================================================================
# refusals
# ======================================================================================


@pytest.mark.parametrize("bad", ["", "   "])
def test_a_blank_subject_is_refused(bad: str) -> None:
    with pytest.raises(ValidationError):
        request(CROSS, inputs=X_INPUTS, subject=bad)


def test_upstream_events_of_another_subject_are_refused() -> None:
    events = _cross("A").events
    with pytest.raises(ValidationError, match="ADR-0057"):
        request(SEQUENCE, upstream_events=events, subject="B")
    with pytest.raises(ValidationError, match="ADR-0057"):
        request(SEQUENCE, upstream_events=events)  # bound upstream, unbound request
    with pytest.raises(ValidationError, match="ADR-0057"):
        request(SEQUENCE, upstream_events=_cross().events, subject="B")  # the reverse


def test_an_upstream_result_of_another_subject_is_refused() -> None:
    up = run_events(
        FeatureThresholdCrossProvider((CROSS_UP,)),
        CROSS_UP,
        request(CROSS_UP, inputs=X_INPUTS, subject="S"),
    )
    other = run_events(
        FeatureThresholdCrossProvider((CROSS_UP,)),
        CROSS_UP,
        request(CROSS_UP, inputs=X_INPUTS, subject="T"),
    )
    switch = run_events(
        StateSwitchProvider((SWITCH,)), SWITCH, request(SWITCH, inputs=REGIME_INPUTS, subject="S")
    )
    req = request(
        SEQUENCE,
        upstream_events=up.events + switch.events,
        subject="S",
    )
    with pytest.raises(UpstreamVerificationError, match="subject"):
        run_events(
            EventSequenceProvider((SEQUENCE,)),
            SEQUENCE,
            req,
            upstream_specs=(CROSS_UP, SWITCH),
            upstream_results=(up, switch, other),
        )


def test_event_rebinding_rules() -> None:
    unbound = _cross().events[0]
    assert unbound.bound_to(None) is unbound
    bound = unbound.bound_to("S")
    assert bound.subject == "S" and bound.event_id != unbound.event_id
    assert bound.bound_to("S") is bound
    with pytest.raises(ValueError, match="'S'"):
        bound.bound_to("T")
    with pytest.raises(ValueError, match="'S'"):
        bound.bound_to(None)


def test_a_result_must_answer_its_request_subject() -> None:
    bound, plain_request = _cross("S"), request(CROSS, inputs=X_INPUTS)
    descriptor = FeatureThresholdCrossProvider((CROSS,)).descriptor
    with pytest.raises(ValueError):
        bound.check_answers(plain_request, descriptor, LAG)  # request hash and subject differ
    # a result claiming another subject for the same events: refused at construction
    payload = bound.model_dump()
    with pytest.raises(ValidationError, match="ADR-0057"):
        EventResult.model_validate({**payload, "subject": "T"})
    with pytest.raises(ValidationError):
        EventResult.model_validate(
            {key: value for key, value in payload.items() if key != "subject"}
        )
    # building a subject-S result from events bound to another subject is refused
    with pytest.raises(ValueError, match="'T'"):
        EventResult.build(
            EventRequest.model_validate(
                {**request(CROSS, inputs=X_INPUTS).model_dump(), "subject": "S"}
            ),
            descriptor,
            _cross("T").events,
        )

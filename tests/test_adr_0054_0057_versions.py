"""ADR-0054 (partial-fill carry-over) and ADR-0057 (``subject``) re-declared at contract 2.1.0.

Codex K3 / K5: a new contract field must not be published as 2.0.0 (ADR-0052 §4). The new
fields, the new model ``FillRemainder`` and the new execution-model literal are therefore
2.1.0 content (``_FIELDS_SINCE`` / ``_MODEL_SINCE`` / ``_VALUES_SINCE``):

* a 2.0.0 envelope carrying any of them is refused (an old reader would not know them);
* objects carrying them are valid from 2.1.0 on: built at 2.1.0 (a replay scope) they are 2.1.0,
  built by the current code they carry the current envelope (2.5.0 since ADR-0094), and both
  round-trip byte-identically;
* 2.0.0 objects without them read as recorded (envelope kept, hash unchanged — the golden pins
  are the pre-ADR values), still answer their 2.0.0 requests, and can sit inside 2.1.0 objects.

Every numeric value here is a TEST ONLY fixture.
"""

from __future__ import annotations

import json
from decimal import Decimal
from typing import Any

import pytest
from pydantic import ValidationError

from core.contracts.event import Event, EventRequest, EventResult
from core.contracts.strategy import (
    BacktestProviderDescriptor,
    BacktestRequest,
    BacktestResult,
    FillRemainder,
    PriceBar,
)
from core.domain.base import CONTRACT_SCHEMA_VERSION, Contract, contract_schema_version_scope
from core.domain.specs import EventSpec
from infrastructure.event.runner import run_events
from plugins.backtest import BarBacktester, ExecutionModel
from plugins.events import FeatureThresholdCrossProvider
from tests.contract_version_support import (
    PRE_BUMP_VERSION,
    at_pre_bump,
    at_version,
    built_at_pre_bump,
)
from tests.fake_events import LAG, X_INPUTS, X, request
from tests.plugins.backtest.test_carry_over import (
    _GOLDEN_REQUESTS,
    _flat,
    _request,
    _target,
)
from tests.plugins.backtest.test_execution_model import _GOLDEN as _GOLDEN_V1_RESULTS
from tests.plugins.backtest.test_execution_model import _golden_requests
from tests.test_event_subject import PRE_ADR_0057

CROSS = FeatureThresholdCrossProvider.spec(X, Decimal("4.5"), "both", observable_lag=LAG)
REFUSED = r"自 2\.1\.0 引入，不能出现在 2\.0\.0 信封中"


def _cross(version: str) -> EventSpec:
    """``CROSS`` as the ``version`` code built it (every envelope at ``version``)."""
    return at_version(CROSS, version)


def _events(subject: str | None, spec: EventSpec = CROSS) -> tuple[EventRequest, EventResult]:
    fields: dict[str, Any] = {} if subject is None else {"subject": subject}
    # the request (and its input refs) at the spec's version, as that version's code built it
    req = at_version(request(spec, inputs=X_INPUTS, **fields), spec.schema_version)
    return req, run_events(FeatureThresholdCrossProvider((spec,)), spec, req)


def _carry() -> tuple[BarBacktester, BacktestRequest, BacktestResult]:
    backtester = BarBacktester(
        execution=ExecutionModel(max_participation_rate=Decimal("0.5"), carry_over=True)
    )
    req = _request(_flat(8, "40"), (_target(0, "1"),))
    return backtester, req, backtester.run(req)


def _at_2_0_0(obj: Contract) -> dict[str, Any]:
    """``obj``'s payload with only its top-level envelope written as 2.0.0."""
    return {**obj.model_dump(), "schema_version": PRE_BUMP_VERSION}


def _round_trips(obj: Contract) -> None:
    kind = type(obj)
    for again in (
        kind.model_validate(obj.model_dump()),
        kind.model_validate_json(obj.model_dump_json()),
    ):
        assert again == obj
        assert again.schema_version == obj.schema_version
        assert again.content_hash() == obj.content_hash()
        assert again.model_dump_json() == obj.model_dump_json()


# ======================================================================================
# objects carrying the new content are valid from 2.1.0 on (2.1.0 and the current 2.5.0)
# ======================================================================================

#: 2.1.0 introduced the content; ADR-0055 raised the current minor to 2.2.0, ADR-0077 to 2.3.0,
#: ADR-0088 to 2.4.0.
SINCE_2_1_0 = ("2.1.0", CONTRACT_SCHEMA_VERSION)


def test_the_content_is_2_1_0_and_the_current_version_is_2_5_0() -> None:
    assert CONTRACT_SCHEMA_VERSION == "2.5.0"
    assert dict(EventRequest._FIELDS_SINCE) == {"subject": "2.1.0"}
    assert dict(BacktestResult._FIELDS_SINCE)["remainders"] == "2.1.0"
    assert FillRemainder._MODEL_SINCE == "2.1.0"


@pytest.mark.parametrize("version", SINCE_2_1_0)
def test_objects_with_a_subject_carry_their_envelope_and_round_trip(version: str) -> None:
    with contract_schema_version_scope(version):
        spec = _cross(version)
        req, result = _events("BTCUSDT", spec)
        descriptor = FeatureThresholdCrossProvider((spec,)).descriptor
    assert spec.schema_version == req.schema_version == result.schema_version == version
    assert result.subject == "BTCUSDT" and result.events
    for obj in (req, result, *result.events):
        assert obj.schema_version == version
        _round_trips(obj)
    again = EventResult.model_validate_json(result.model_dump_json())
    again.check_answers(req, descriptor, LAG)


@pytest.mark.parametrize("version", SINCE_2_1_0)
def test_objects_with_carry_over_content_carry_their_envelope_and_round_trip(
    version: str,
) -> None:
    with contract_schema_version_scope(version):
        backtester, req, result = _carry()
    assert result.remainders and req.bars[0].volume is not None
    descriptor = backtester.descriptor
    assert descriptor.execution_model == "next_bar_open_participation"
    for obj in (descriptor, req, req.bars[0], result, *result.remainders):
        assert obj.schema_version == version
        _round_trips(obj)
    BacktestResult.model_validate_json(result.model_dump_json()).check_answers(req, descriptor)


def test_the_current_code_builds_the_current_envelope() -> None:
    req, result = _events("BTCUSDT")
    backtester, bt_req, bt_result = _carry()
    for obj in (req, result, backtester.descriptor, bt_req, bt_result):
        assert obj.schema_version == CONTRACT_SCHEMA_VERSION == "2.5.0"


# ======================================================================================
# a 2.0.0 envelope carrying 2.1.0 content is refused
# ======================================================================================


def test_a_2_0_0_envelope_with_a_subject_is_refused() -> None:
    req, result = _events("BTCUSDT")
    for obj in (req, result, result.events[0]):
        with pytest.raises(ValidationError, match=r"subject " + REFUSED):
            type(obj).model_validate(_at_2_0_0(obj))
    with built_at_pre_bump(), pytest.raises(ValidationError, match=r"subject " + REFUSED):
        _events("BTCUSDT")  # the 2.0.0 code path cannot build a subject at all


def test_a_2_0_0_envelope_with_carry_over_content_is_refused() -> None:
    backtester, req, result = _carry()
    with pytest.raises(ValidationError, match=r"volume " + REFUSED):
        PriceBar.model_validate(_at_2_0_0(req.bars[0]))
    with pytest.raises(ValidationError, match=r"remainders " + REFUSED):
        BacktestResult.model_validate(_at_2_0_0(result))
    with pytest.raises(ValidationError, match=r"FillRemainder 自 2\.1\.0 引入"):
        FillRemainder.model_validate(_at_2_0_0(result.remainders[0]))
    with pytest.raises(ValidationError, match=r"execution_model='next_bar_open_participation' "):
        BacktestProviderDescriptor.model_validate(_at_2_0_0(backtester.descriptor))
    with built_at_pre_bump(), pytest.raises(ValidationError, match=REFUSED):
        _carry()


def test_the_absent_forms_are_valid_at_2_0_0() -> None:
    """Absent = omitted from the payload: ``None`` volume / subject, empty ``remainders``."""
    with built_at_pre_bump():
        descriptor = BarBacktester().descriptor
        req = at_pre_bump(_golden_requests()["alternating"])
        result = BarBacktester().run(req)
    assert descriptor.schema_version == result.schema_version == "2.0.0"
    assert descriptor.execution_model == "next_bar_open"
    assert result.remainders == () and all(bar.volume is None for bar in req.bars)
    for obj in (descriptor, req, result):
        assert type(obj).model_validate_json(obj.model_dump_json()) == obj


# ======================================================================================
# cross-version reads: persisted 2.0.0 payloads read as recorded
# ======================================================================================


def _persisted_2_0_0() -> dict[str, tuple[type[Contract], str, str]]:
    """What the 2.0.0 code wrote: JSON payloads and the hashes the pre-ADR code recorded."""
    with built_at_pre_bump():
        spec = at_pre_bump(CROSS)
        req = at_pre_bump(request(spec, inputs=X_INPUTS))
        result = run_events(FeatureThresholdCrossProvider((spec,)), spec, req)
        bt_req = at_pre_bump(_golden_requests()["alternating"])
        bt_result = BarBacktester().run(bt_req)
    return {
        "event_request": (EventRequest, req.model_dump_json(), PRE_ADR_0057["cross_request"]),
        "event_result": (
            EventResult,
            result.model_dump_json(),
            PRE_ADR_0057["cross_result_content"],
        ),
        "event": (Event, result.events[0].model_dump_json(), ""),
        "backtest_request": (
            BacktestRequest,
            bt_req.model_dump_json(),
            _GOLDEN_REQUESTS["alternating"],
        ),
        "backtest_result": (BacktestResult, bt_result.model_dump_json(), ""),
    }


@pytest.mark.parametrize("name", sorted(_persisted_2_0_0()))
def test_a_persisted_2_0_0_payload_reads_as_recorded(name: str) -> None:
    model, text, pinned = _persisted_2_0_0()[name]
    assert json.loads(text)["schema_version"] == "2.0.0"
    obj = model.model_validate_json(text)  # outside any scope: the current code
    assert obj.schema_version == "2.0.0"  # never rewritten
    assert obj.model_dump_json() == text
    if pinned:
        assert obj.content_hash() == pinned


def test_2_0_0_results_still_answer_their_2_0_0_requests() -> None:
    with built_at_pre_bump():
        spec = at_pre_bump(CROSS)
        provider = FeatureThresholdCrossProvider((spec,))
        req = at_pre_bump(request(spec, inputs=X_INPUTS))
        text = run_events(provider, spec, req).model_dump_json()
        backtester = BarBacktester()
        bt_req = at_pre_bump(_golden_requests()["two_instruments"])
        bt_text = backtester.run(bt_req).model_dump_json()
    result = EventResult.model_validate_json(text)
    result.check_answers(req, provider.descriptor, LAG)
    assert result.result_hash == PRE_ADR_0057["cross_result_hash"]
    assert result.events[0].event_id == PRE_ADR_0057["cross_first_event"]
    bt_result = BacktestResult.model_validate_json(bt_text)
    bt_result.check_answers(bt_req, backtester.descriptor)
    assert bt_result.result_hash == _GOLDEN_V1_RESULTS["two_instruments"]


def test_2_0_0_events_nest_in_a_current_request_and_bind_as_new_objects() -> None:
    _, persisted = _persisted_2_0_0()["event"][:2]
    old = Event.model_validate_json(persisted)
    assert old.schema_version == "2.0.0" and old.subject is None
    carrier = EventRequest(
        event=CROSS.ref,
        spec_hash=CROSS.content_hash(),
        as_of=old.event_time,
        upstream_events=(old,),
    )  # a current object may carry recorded 2.0.0 objects unchanged
    assert carrier.schema_version == CONTRACT_SCHEMA_VERSION
    assert carrier.upstream_events[0].schema_version == "2.0.0"
    bound = old.bound_to("BTCUSDT")  # new content -> a new current object; the old one is intact
    assert bound.schema_version == CONTRACT_SCHEMA_VERSION and bound.subject == "BTCUSDT"
    assert bound.event_id != old.event_id
    assert Event.model_validate_json(persisted) == old


# ======================================================================================
# subject identity (Codex 648fe6c; ADR-0057 implementation note)
# ======================================================================================


def test_the_subject_is_a_case_sensitive_opaque_identifier() -> None:
    upper, lower = _events("BTCUSDT"), _events("btcusdt")
    assert upper[0].subject == "BTCUSDT" and lower[0].subject == "btcusdt"
    assert upper[0].content_hash() != lower[0].content_hash()
    assert upper[1].result_hash != lower[1].result_hash
    assert {item.event_id for item in upper[1].events}.isdisjoint(
        {item.event_id for item in lower[1].events}
    )
    padded = request(CROSS, inputs=X_INPUTS, subject="  BTCUSDT ")
    assert padded.subject == "BTCUSDT"  # only the shared outer-whitespace rule applies
    assert padded.content_hash() == upper[0].content_hash()
    assert _events("binance:spot:BTCUSDT")[0].subject == "binance:spot:BTCUSDT"  # no parsing

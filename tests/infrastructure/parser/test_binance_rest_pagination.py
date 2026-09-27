"""Envelope, coverage and chain evidence for the D3C decoder (ADR-0027 §6 / §7; matrix 11 ~ 13).

Page ordering and cross-page continuity, the answered interval of every page shape, the four stop
reasons and their exact continuation queries, unclosed 1m klines, the accepted quality facts, and
the determinism / immutability / atomicity properties the store and collector rely on.
"""

from __future__ import annotations

import subprocess
import sys
from dataclasses import FrozenInstanceError
from datetime import timedelta
from typing import Any

import pytest

from infrastructure.parser import (
    RestAggTradeElement,
    RestDecodeRequestError,
    RestKline1mElement,
    RestPageDecoded,
    RestPageSummary,
    RestQualityFactType,
    RestQuantityUnit,
    RestRejectionCode,
    RestStopReason,
    decode_rest_page,
)
from infrastructure.parser import binance_rest as decoder_mod
from infrastructure.revision.rest_identity import PAGE_LIMIT, RestPageQuery
from tests.infrastructure.parser.rest_support import (
    MINUTE_MS,
    RETRIEVED_AT_MS,
    SYMBOL,
    T0,
    TARGET_END,
    agg_item,
    agg_items,
    agg_request,
    at_ms,
    body,
    continuation,
    decoded,
    kline_item,
    kline_items,
    kline_request,
    rejected,
)

CODE = RestRejectionCode
STOP = RestStopReason
INT64_MAX = (1 << 63) - 1


def _agg(items: Any, **kwargs: Any) -> object:
    return decode_rest_page(agg_request(**kwargs), body(items))


def _kline(items: Any, **kwargs: Any) -> object:
    return decode_rest_page(kline_request(**kwargs), body(items))


def _full_agg_page() -> RestPageDecoded:
    page = decoded(_agg(agg_items(PAGE_LIMIT)))
    assert page.summary.continues
    return page


def _full_kline_page() -> RestPageDecoded:
    page = decoded(_kline(kline_items(PAGE_LIMIT)))
    assert page.summary.continues
    return page


# --------------------------------------------------------------------------- page shapes


def test_an_empty_first_page_answers_nothing_and_stops() -> None:
    for page in (decoded(_agg([])), decoded(_kline([]))):
        summary = page.summary
        assert page.elements == ()
        assert summary.stop_reason is STOP.EMPTY_PAGE
        assert summary.next_query is None
        assert summary.answered.is_empty
        assert (summary.answered.start_ms, summary.answered.end_ms) == (T0, T0)
        assert summary.served_count == 0


def test_a_short_page_answers_up_to_its_last_element_inclusive() -> None:
    page = decoded(_agg(agg_items(3)))
    assert page.summary.stop_reason is STOP.SHORT_PAGE
    assert page.summary.next_query is None
    assert (page.summary.answered.start_ms, page.summary.answered.end_ms) == (T0, T0 + 3)

    klines = decoded(_kline(kline_items(3)))
    assert klines.summary.stop_reason is STOP.SHORT_PAGE
    assert klines.summary.answered.end_ms == T0 + 3 * MINUTE_MS


def test_a_full_page_leaves_its_last_tick_open_and_names_the_next_query() -> None:
    page = _full_agg_page()
    summary = page.summary
    assert summary.served_count == summary.element_count == PAGE_LIMIT
    assert summary.stop_reason is None
    # Exclusive upper bound: the last millisecond may continue on the next page.
    assert (summary.answered.start_ms, summary.answered.end_ms) == (T0, T0 + PAGE_LIMIT - 1)
    assert summary.next_query == RestPageQuery.agg_trades_from_id(SYMBOL, 500 + PAGE_LIMIT)

    klines = _full_kline_page().summary
    assert klines.answered.end_ms == T0 + PAGE_LIMIT * MINUTE_MS
    assert klines.next_query == RestPageQuery.klines_from_start(SYMBOL, T0 + PAGE_LIMIT * MINUTE_MS)


def test_an_empty_continuation_page_completes_the_boundary_tick() -> None:
    first = _full_agg_page().summary
    page = decoded(decode_rest_page(continuation(first), body([])))
    assert page.summary.stop_reason is STOP.EMPTY_PAGE
    assert (page.summary.answered.start_ms, page.summary.answered.end_ms) == (
        first.answered.end_ms,
        first.answered.end_ms + 1,
    )


def test_a_continuation_page_starts_where_the_previous_one_stopped() -> None:
    first = _full_agg_page().summary
    items = agg_items(3, first_id=500 + PAGE_LIMIT, first_ms=first.answered.end_ms)
    page = decoded(decode_rest_page(continuation(first), body(items)))
    assert page.summary.page_index == 1
    assert page.summary.answered.start_ms == first.answered.end_ms
    assert page.summary.stop_reason is STOP.SHORT_PAGE


def test_a_klines_chain_advances_by_exactly_one_minute() -> None:
    first = _full_kline_page().summary
    following = continuation(first)
    assert following.query.start_time_ms == T0 + PAGE_LIMIT * MINUTE_MS
    page = decoded(
        decode_rest_page(following, body(kline_items(2, first_ms=T0 + PAGE_LIMIT * MINUTE_MS)))
    )
    assert page.summary.answered.start_ms == T0 + PAGE_LIMIT * MINUTE_MS
    assert page.summary.stop_reason is STOP.SHORT_PAGE


# --------------------------------------------------------------------------- target window


def test_reaching_the_target_end_stops_the_chain() -> None:
    narrow_end = T0 + 2
    items = agg_items(3)  # T0, T0 + 1, T0 + 2
    page = decoded(_agg(items, target_end_ms=narrow_end))
    assert page.summary.stop_reason is STOP.REACHED_TARGET_END
    assert page.summary.next_query is None
    assert page.element_count == 3

    klines = decoded(_kline(kline_items(3), target_end_ms=T0 + 2 * MINUTE_MS))
    assert klines.summary.stop_reason is STOP.REACHED_TARGET_END


def test_reaching_the_target_end_outranks_every_other_stop_reason() -> None:
    # A full page that also reaches the target end stops for reaching it, not for its length.
    page = decoded(_agg(agg_items(PAGE_LIMIT), target_end_ms=T0 + 1))
    assert page.summary.stop_reason is STOP.REACHED_TARGET_END
    # A klines page whose unclosed tail is already past the target end stops the same way.
    retrieved = T0 + 3 * MINUTE_MS + 30_000
    unclosed = [*kline_items(3), kline_item(T0 + 3 * MINUTE_MS)]
    summary = decoded(
        _kline(
            unclosed,
            retrieved_at=at_ms(retrieved),
            target_end_ms=T0 + 2 * MINUTE_MS,
        )
    ).summary
    assert summary.stop_reason is STOP.REACHED_TARGET_END


def test_valid_elements_beyond_the_target_end_are_still_decoded() -> None:
    narrow_end = T0 + 1
    items = agg_items(5)
    page = decoded(_agg(items, target_end_ms=narrow_end))
    assert page.element_count == 5  # nothing is dropped, nothing is rejected
    assert [element.element_index for element in page.elements] == [0, 1, 2, 3, 4]
    overshoot = page.elements[-1]
    assert isinstance(overshoot, RestAggTradeElement)
    assert overshoot.timestamp_raw >= narrow_end

    klines = decoded(_kline(kline_items(5), target_end_ms=T0 + MINUTE_MS))
    assert klines.element_count == 5
    last = klines.elements[-1]
    assert isinstance(last, RestKline1mElement)
    assert last.open_time_raw >= T0 + MINUTE_MS


# --------------------------------------------------------------------------- page ordering


def test_aggtrade_ids_must_strictly_increase() -> None:
    rejected(_agg([agg_item(1, T0), agg_item(1, T0 + 1)]), CODE.DUPLICATE_ELEMENT)
    rejected(
        _agg([agg_item(5, T0), agg_item(4, T0 + 1, first=60, last=61)]),
        CODE.OUT_OF_ORDER,
    )


def test_aggtrade_timestamps_must_not_decrease() -> None:
    rejected(_agg([agg_item(1, T0 + 5), agg_item(2, T0 + 4)]), CODE.TIMESTAMP_DECREASING)
    assert decoded(_agg([agg_item(1, T0 + 5), agg_item(2, T0 + 5)])).element_count == 2


def test_aggtrade_trade_ranges_must_not_overlap() -> None:
    rejected(_agg([agg_item(1, T0), agg_item(2, T0, first=12)]), CODE.TRADE_ID_OVERLAP)
    rejected(_agg([agg_item(1, T0), agg_item(2, T0, first=11)]), CODE.TRADE_ID_OVERLAP)
    assert decoded(_agg([agg_item(1, T0), agg_item(2, T0, first=13, last=14)])).element_count == 2


def test_kline_opens_must_strictly_increase() -> None:
    rejected(_kline([kline_item(T0), kline_item(T0)]), CODE.DUPLICATE_ELEMENT)
    rejected(_kline([kline_item(T0 + MINUTE_MS), kline_item(T0)]), CODE.OUT_OF_ORDER)


def test_the_query_lower_bound_is_enforced_on_every_element() -> None:
    rejected(_agg([agg_item(1, T0 - 1)]), CODE.QUERY_LOWER_BOUND)
    rejected(_kline([kline_item(T0 - MINUTE_MS)]), CODE.QUERY_LOWER_BOUND)

    first = _full_agg_page().summary
    assert first.next_query is not None and first.next_query.from_id is not None
    below = agg_items(1, first_id=first.next_query.from_id - 1, first_ms=first.answered.end_ms)
    rejected(decode_rest_page(continuation(first), body(below)), CODE.QUERY_LOWER_BOUND)


# --------------------------------------------------------------------------- cross-page


def test_a_page_may_not_step_back_before_the_previous_page() -> None:
    first = _full_agg_page().summary
    assert first.last_event_time_ms is not None
    earlier = agg_items(1, first_id=500 + PAGE_LIMIT, first_ms=first.last_event_time_ms - 1)
    outcome = rejected(
        decode_rest_page(continuation(first), body(earlier)),
        CODE.PREVIOUS_PAGE_DISCONTINUITY,
    )
    assert outcome.element_index == 0


def test_a_page_may_not_overlap_the_previous_page_trade_range() -> None:
    first = _full_agg_page().summary
    assert first.last_trade_id is not None
    overlapping = [
        agg_item(
            500 + PAGE_LIMIT,
            first.answered.end_ms,
            first=first.last_trade_id,
            last=first.last_trade_id + 5,
        )
    ]
    rejected(
        decode_rest_page(continuation(first), body(overlapping)),
        CODE.PREVIOUS_PAGE_DISCONTINUITY,
    )


def test_a_continuation_must_use_the_previous_summary_exact_next_query() -> None:
    first = _full_agg_page().summary
    assert first.next_query is not None
    wrong = RestPageQuery.agg_trades_from_id(SYMBOL, first.next_query.from_id + 1)  # type: ignore[operator]
    with pytest.raises(RestDecodeRequestError, match="exact next query"):
        agg_request(query=wrong, page_index=1, previous=first)
    with pytest.raises(RestDecodeRequestError, match="page_index"):
        agg_request(query=first.next_query, page_index=2, previous=first)
    with pytest.raises(RestDecodeRequestError, match="target window"):
        agg_request(
            query=first.next_query, page_index=1, previous=first, target_end_ms=TARGET_END - 1
        )
    with pytest.raises(RestDecodeRequestError, match="backwards"):
        agg_request(
            query=first.next_query,
            page_index=1,
            previous=first,
            retrieved_at=first.retrieved_at - timedelta(milliseconds=1),
        )


def test_a_stopped_page_cannot_be_continued() -> None:
    stopped = decoded(_agg(agg_items(3))).summary
    assert stopped.next_query is None
    with pytest.raises(RestDecodeRequestError, match="stopped"):
        agg_request(
            query=RestPageQuery.agg_trades_from_id(SYMBOL, 503), page_index=1, previous=stopped
        )


def test_a_summary_from_another_decoder_version_is_refused() -> None:
    first = _full_agg_page().summary
    foreign = RestPageSummary(
        decoder=first.decoder.model_copy(update={"version": "2.0.0"}),
        data_type=first.data_type,
        symbol=first.symbol,
        query=first.query,
        page_index=first.page_index,
        retrieved_at=first.retrieved_at,
        target_start_ms=first.target_start_ms,
        target_end_ms=first.target_end_ms,
        element_count=first.element_count,
        served_count=first.served_count,
        answered=first.answered,
        stop_reason=None,
        next_query=first.next_query,
        last_agg_trade_id=first.last_agg_trade_id,
        last_event_time_ms=first.last_event_time_ms,
        last_trade_id=first.last_trade_id,
    )
    with pytest.raises(RestDecodeRequestError, match="decoder version"):
        agg_request(query=first.next_query, page_index=1, previous=foreign)


# --------------------------------------------------------------------------- unclosed klines


def _unclosed_case() -> tuple[int, list[list[Any]]]:
    """Three closed minutes and one still-open final minute at ``retrieved_at``."""
    retrieved = T0 + 3 * MINUTE_MS + 30_000
    return retrieved, [*kline_items(3), kline_item(T0 + 3 * MINUTE_MS)]


def test_one_final_unclosed_kline_is_skipped_and_stops_the_chain() -> None:
    retrieved, items = _unclosed_case()
    page = decoded(_kline(items, retrieved_at=at_ms(retrieved)))
    summary = page.summary
    assert summary.served_count == 4
    assert page.element_count == 3
    assert [element.element_index for element in page.elements] == [0, 1, 2]
    assert summary.stop_reason is STOP.UNCLOSED_TAIL
    assert summary.next_query is None
    # Answered coverage stops at the unclosed kline's open, never inside it.
    assert summary.answered.end_ms == T0 + 3 * MINUTE_MS
    (fact,) = summary.quality_facts
    assert fact.fact_type is RestQualityFactType.REST_UNCLOSED_KLINE_SKIPPED
    assert (fact.range_start, fact.range_end) == (T0 + 3 * MINUTE_MS, T0 + 4 * MINUTE_MS)
    assert fact.unit is RestQuantityUnit.EPOCH_MILLISECOND


def test_an_unclosed_kline_before_the_last_item_rejects_the_page() -> None:
    retrieved = T0 + 3 * MINUTE_MS + 30_000
    items = [kline_item(T0), kline_item(T0 + 3 * MINUTE_MS), kline_item(T0 + 4 * MINUTE_MS)]
    outcome = rejected(_kline(items, retrieved_at=at_ms(retrieved)), CODE.UNCLOSED_KLINE_NOT_LAST)
    assert outcome.element_index == 1


def test_more_than_one_unclosed_kline_cannot_survive_either_bound() -> None:
    # Two minute-aligned opens can never both satisfy `open <= retrieved_at` and
    # `open + 60s > retrieved_at`, so the second one is caught by one bound or the other.
    retrieved = T0 + 3 * MINUTE_MS + 30_000
    both_unclosed = [kline_item(T0 + 3 * MINUTE_MS), kline_item(T0 + 4 * MINUTE_MS)]
    rejected(_kline(both_unclosed, retrieved_at=at_ms(retrieved)), CODE.UNCLOSED_KLINE_NOT_LAST)
    only_future = [kline_item(T0 + 4 * MINUTE_MS)]
    rejected(_kline(only_future, retrieved_at=at_ms(retrieved)), CODE.FUTURE_TIMESTAMP)


def test_a_page_of_only_an_unclosed_kline_answers_no_time() -> None:
    retrieved = T0 + 30_000
    page = decoded(_kline([kline_item(T0)], retrieved_at=at_ms(retrieved)))
    assert page.elements == ()
    assert page.summary.answered.is_empty
    assert (page.summary.answered.start_ms, page.summary.answered.end_ms) == (T0, T0)
    assert page.summary.stop_reason is STOP.UNCLOSED_TAIL


def test_a_kline_closing_exactly_at_retrieved_at_is_closed() -> None:
    retrieved = T0 + MINUTE_MS  # the exclusive interval end
    page = decoded(_kline([kline_item(T0)], retrieved_at=at_ms(retrieved)))
    assert page.element_count == 1
    assert page.summary.stop_reason is STOP.SHORT_PAGE
    assert page.summary.quality_facts == ()


# --------------------------------------------------------------------------- quality facts


def test_an_aggtrade_id_gap_is_a_quality_fact_not_a_rejection() -> None:
    page = decoded(_agg([agg_item(1, T0), agg_item(4, T0 + 1)]))
    assert page.element_count == 2
    (fact,) = page.summary.quality_facts
    assert fact.fact_type is RestQualityFactType.REST_AGG_TRADE_ID_GAP
    assert fact.unit is RestQuantityUnit.AGG_TRADE_ID
    assert (fact.range_start, fact.range_end) == (2, 4)
    assert (fact.occurrences, fact.missing) == (1, 2)


def test_a_from_id_page_that_starts_above_its_cursor_records_the_gap() -> None:
    first = _full_agg_page().summary
    assert first.next_query is not None and first.next_query.from_id is not None
    ahead = agg_items(2, first_id=first.next_query.from_id + 3, first_ms=first.answered.end_ms)
    page = decoded(decode_rest_page(continuation(first), body(ahead)))
    (fact,) = page.summary.quality_facts
    assert fact.fact_type is RestQualityFactType.REST_AGG_TRADE_ID_GAP
    assert (fact.range_start, fact.range_end) == (
        first.next_query.from_id,
        first.next_query.from_id + 3,
    )
    assert fact.missing == 3


def test_a_missing_kline_minute_is_a_quality_fact_not_a_coverage_gap() -> None:
    page = decoded(_kline([kline_item(T0), kline_item(T0 + 2 * MINUTE_MS)]))
    assert page.element_count == 2
    (fact,) = page.summary.quality_facts
    assert fact.fact_type is RestQualityFactType.REST_WINDOW_GAP
    assert (fact.range_start, fact.range_end) == (T0 + MINUTE_MS, T0 + 2 * MINUTE_MS)
    assert (fact.occurrences, fact.missing) == (1, 1)
    # The answered interval still spans the whole window: missing minutes are not a gap in it.
    assert (page.summary.answered.start_ms, page.summary.answered.end_ms) == (
        T0,
        T0 + 3 * MINUTE_MS,
    )


def test_a_leading_missing_minute_counts_from_the_page_start_time() -> None:
    page = decoded(_kline([kline_item(T0 + 3 * MINUTE_MS)]))
    (fact,) = page.summary.quality_facts
    assert (fact.range_start, fact.range_end) == (T0, T0 + 3 * MINUTE_MS)
    assert fact.missing == 3


def test_quality_facts_are_deterministic_and_bounded() -> None:
    items = [agg_item(index * 3 + 1, T0 + index) for index in range(100)]
    page = decoded(_agg(items))
    (fact,) = page.summary.quality_facts  # one aggregated fact, never one per gap
    assert fact.occurrences == 99
    assert fact.missing == 99 * 2
    assert len(fact.detail) <= 240


# --------------------------------------------------------------------------- continuation guards


def test_an_int64_overflow_in_the_next_query_rejects_the_page() -> None:
    base = INT64_MAX - PAGE_LIMIT + 1
    items = [
        {
            "a": base + index,
            "p": "1.00000000",
            "q": "1.00000000",
            "f": index + 1,
            "l": index + 1,
            "T": T0 + index,
            "m": True,
            "M": False,
        }
        for index in range(PAGE_LIMIT)
    ]
    assert items[-1]["a"] == INT64_MAX
    rejected(_agg(items), CODE.NEXT_QUERY_UNREPRESENTABLE)


def test_the_non_progress_guard_cannot_be_bypassed() -> None:
    """Unreachable through ``decode_rest_page`` (cursors always advance); tested directly."""
    query = RestPageQuery.agg_trades_from_id(SYMBOL, 77)
    with pytest.raises(decoder_mod._Reject) as caught:
        decoder_mod._next_query("agg_trades", SYMBOL, 77, query)
    assert caught.value.code is CODE.NON_PROGRESSING_PAGE


# --------------------------------------------------------------------------- determinism


def test_decoding_is_deterministic_and_input_independent() -> None:
    items = agg_items(5)
    payload = body(items)
    request = agg_request()
    first = decoded(decode_rest_page(request, payload))
    items[0]["p"] = "1.00000000"  # mutating the caller's structure changes nothing
    second = decoded(decode_rest_page(request, payload))
    assert first == second
    assert first.summary == second.summary
    assert first.elements == second.elements


def test_decoded_values_are_frozen() -> None:
    page = decoded(_agg(agg_items(2)))
    assert isinstance(page.elements, tuple)
    assert isinstance(page.summary.quality_facts, tuple)
    with pytest.raises(FrozenInstanceError):
        page.elements[0].element_index = 9  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        page.summary.element_count = 0  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        page.summary.answered.start_ms = 0  # type: ignore[misc]


def test_a_previous_summary_is_never_mutated_by_the_next_page() -> None:
    first = _full_agg_page().summary
    snapshot = (first.next_query, first.answered, first.element_count, first.quality_facts)
    decoded(decode_rest_page(continuation(first), body([])))
    assert (first.next_query, first.answered, first.element_count, first.quality_facts) == snapshot


_OPTIMIZED_PROBE = """
import json
from datetime import UTC, datetime, timedelta

from infrastructure.parser import (
    RestPageDecodeRequest, RestPageRejection, decode_rest_page,
)
from infrastructure.revision.rest_identity import RestPageQuery

assert __debug__ is False, "probe must run under python -O"

T0 = {t0}
RETRIEVED = datetime(1970, 1, 1, tzinfo=UTC) + timedelta(milliseconds={retrieved})
query = RestPageQuery.agg_trades_from_start("BTCUSDT", T0)
request = RestPageDecodeRequest(
    query=query,
    retrieved_at=RETRIEVED,
    target_start_ms=T0,
    target_end_ms=T0 + 3_600_000,
    max_body_bytes=8_388_608,
)
items = [
    {{"a": i + 1, "p": "1.00000000", "q": "1.00000000", "f": i * 10, "l": i * 10 + 2,
      "T": T0 + i, "m": True, "M": True}}
    for i in range(50)
]
items[-1]["p"] = "0"
outcome = decode_rest_page(request, json.dumps(items).encode())
if not isinstance(outcome, RestPageRejection):
    raise SystemExit("assertions off changed the verdict")
if outcome.elements != () or outcome.code.value != "non_positive_price":
    raise SystemExit("a rejected page leaked elements under -O")

items[-1]["p"] = "1.00000000"
page = decode_rest_page(request, json.dumps(items).encode())
if page.element_count != 50 or page.summary.stop_reason.value != "short_page":
    raise SystemExit("summary differs under -O")
try:
    page.elements[0].price = 1
except Exception:
    pass
else:
    raise SystemExit("decoded elements are mutable under -O")
try:
    RestPageDecodeRequest(
        query=query, retrieved_at=RETRIEVED, target_start_ms=T0, target_end_ms=T0,
        max_body_bytes=8_388_608,
    )
except ValueError:
    pass
else:
    raise SystemExit("request validation is assert-based")
print("ok")
"""


def test_the_decoder_behaves_identically_under_python_dash_o() -> None:
    """No rule may rest on ``assert``: ``python -O`` must reject and freeze exactly the same."""
    script = _OPTIMIZED_PROBE.format(t0=T0, retrieved=RETRIEVED_AT_MS)
    result = subprocess.run(
        [sys.executable, "-O", "-c", script],
        capture_output=True,
        text=True,
        cwd=str(__import__("pathlib").Path(__file__).resolve().parents[3]),
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.strip() == "ok"

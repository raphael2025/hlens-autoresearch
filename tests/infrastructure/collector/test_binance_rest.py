"""D3D REST collector: pagination, honest coverage, HTTP boundaries and the endpoint allowlist.

Every test drives ``httpx.MockTransport`` with injected wall / monotonic clocks and an injected
sleeper: nothing here waits, downloads or reads the real clock.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from core.contracts.collector import (
    CollectionFailed,
    CollectionRequest,
    CollectionResult,
    SourceBinding,
    UnsupportedRequest,
)
from infrastructure.collector.binance_rest import (
    MAX_RESPONSE_BYTES,
    REST_COLLECTOR_ID,
    REST_COLLECTOR_VERSION,
    REST_SOURCE,
    BinanceSpotRestCollector,
)
from infrastructure.revision.rest_identity import RestPageQuery, page_source_uri
from tests.infrastructure.collector.rest_support import (
    FULL_PAGE,
    MINUTE_MS,
    ORIGIN,
    OTHER_SYMBOL,
    RETRIEVED_AT_MS,
    SYMBOL,
    T0,
    Answer,
    FakeTime,
    FrozenClock,
    RestVenue,
    StepClock,
    agg_page,
    agg_request,
    at_ms,
    kline_page,
    kline_request,
    make_collector,
    make_storage,
    queue_agg_chain,
    queue_kline_chain,
)
from tests.infrastructure.parser.rest_support import body

WINDOW_END = T0 + 5 * MINUTE_MS


@pytest.fixture
def venue() -> RestVenue:
    return RestVenue()


def _collect(
    tmp_path: Path, venue: RestVenue, request: CollectionRequest, **kwargs: Any
) -> CollectionResult:
    storage = make_storage(tmp_path)
    return make_collector(storage, venue, **kwargs).collect(request)


# ======================================================================================
# #10 pagination, stop reasons, budget
# ======================================================================================


def test_agg_chain_advances_by_from_id_and_stops_on_a_short_page(
    tmp_path: Path, venue: RestVenue
) -> None:
    queries = queue_agg_chain(
        venue,
        SYMBOL,
        T0,
        [
            agg_page(FULL_PAGE, first_id=500, first_ms=T0),
            agg_page(3, first_id=500 + FULL_PAGE, first_ms=T0 + FULL_PAGE),
        ],
    )
    assert queries[1] == RestPageQuery.agg_trades_from_id(SYMBOL, 500 + FULL_PAGE)

    result = _collect(tmp_path, venue, agg_request())

    assert {item.source_uri for item in result.objects} == {
        page_source_uri(query, ORIGIN) for query in queries
    }
    assert venue.pending() == 0
    # Two abutting answers: [T0, T0+999) then [T0+999, T0+1003).
    spans = sorted((o.coverage_start, o.coverage_end) for o in result.objects)
    assert spans == [
        (at_ms(T0), at_ms(T0 + FULL_PAGE - 1)),
        (at_ms(T0 + FULL_PAGE - 1), at_ms(T0 + FULL_PAGE + 3)),
    ]
    assert len(result.gaps) == 1
    gap = result.gaps[0]
    assert (gap.coverage_start, gap.coverage_end) == (at_ms(T0 + FULL_PAGE + 3), at_ms(WINDOW_END))
    assert gap.reason.value == "source_absent"
    assert "records the source's answer only, not an absence of market activity" in gap.detail
    assert "returned 3 of 1000" in gap.detail
    assert result.collector_id == REST_COLLECTOR_ID
    assert result.collector_version == REST_COLLECTOR_VERSION


def test_kline_chain_advances_by_start_time_and_leaves_one_tail_gap(
    tmp_path: Path, venue: RestVenue
) -> None:
    queries = queue_kline_chain(
        venue,
        SYMBOL,
        T0,
        [
            kline_page(FULL_PAGE, first_ms=T0),
            kline_page(2, first_ms=T0 + FULL_PAGE * MINUTE_MS),
        ],
    )
    assert queries[1] == RestPageQuery.klines_from_start(SYMBOL, T0 + FULL_PAGE * MINUTE_MS)

    result = _collect(tmp_path, venue, kline_request(end_ms=T0 + (FULL_PAGE + 10) * MINUTE_MS))

    assert venue.pending() == 0
    assert len(result.objects) == 2
    assert min(item.coverage_start for item in result.objects) == at_ms(T0)
    end = max(item.coverage_end for item in result.objects)
    assert end == at_ms(T0 + (FULL_PAGE + 2) * MINUTE_MS)
    assert len(result.gaps) == 1
    assert result.gaps[0].coverage_start == end


def test_reached_target_end_stops_the_chain_and_leaves_no_gap(
    tmp_path: Path, venue: RestVenue
) -> None:
    queue_agg_chain(venue, SYMBOL, T0, [agg_page(FULL_PAGE, first_id=500, first_ms=T0)])

    result = _collect(tmp_path, venue, agg_request(end_ms=T0 + 500))

    assert venue.pending() == 0
    assert result.gaps == ()
    assert len(result.objects) == 1
    assert result.objects[0].coverage_start == at_ms(T0)
    assert result.objects[0].coverage_end == at_ms(T0 + 500)


def test_an_empty_first_page_is_a_full_window_gap_and_no_object(
    tmp_path: Path, venue: RestVenue
) -> None:
    queue_agg_chain(venue, SYMBOL, T0, [[]])

    result = _collect(tmp_path, venue, agg_request())

    assert result.objects == ()
    assert len(result.gaps) == 1
    assert (result.gaps[0].coverage_start, result.gaps[0].coverage_end) == (
        at_ms(T0),
        at_ms(WINDOW_END),
    )
    assert "returned 0 of 1000" in result.gaps[0].detail


def test_an_unclosed_final_kline_stops_the_chain_and_never_extends_coverage(
    tmp_path: Path, venue: RestVenue
) -> None:
    # retrieved_at sits inside the third minute, so only two klines are closed.
    retrieved_ms = T0 + 2 * MINUTE_MS + 15_000
    venue.serve_items(RestPageQuery.klines_from_start(SYMBOL, T0), kline_page(3, first_ms=T0))

    result = _collect(
        tmp_path,
        venue,
        kline_request(),
        clock=StepClock(start_ms=retrieved_ms),
    )

    assert len(result.objects) == 1
    assert result.objects[0].coverage_end == at_ms(T0 + 2 * MINUTE_MS)
    assert len(result.gaps) == 1
    assert result.gaps[0].coverage_start == at_ms(T0 + 2 * MINUTE_MS)


def test_overshooting_elements_are_kept_but_coverage_is_clipped(
    tmp_path: Path, venue: RestVenue
) -> None:
    """A valid element after ``t1`` never rejects a page; it only stops being coverage."""
    queue_agg_chain(venue, SYMBOL, T0, [agg_page(5, first_id=500, first_ms=T0, ms_step=1_000)])

    result = _collect(tmp_path, venue, agg_request(end_ms=T0 + 2_000))

    assert len(result.objects) == 1
    assert result.objects[0].coverage_end == at_ms(T0 + 2_000)
    assert result.gaps == ()


def test_a_page_answering_an_empty_interval_is_committed_but_is_not_an_object(
    tmp_path: Path, venue: RestVenue
) -> None:
    """A full page of same-millisecond trades answers no time at all (ADR-0027 §7)."""
    queue_agg_chain(
        venue,
        SYMBOL,
        T0,
        [
            agg_page(FULL_PAGE, first_id=500, first_ms=T0, ms_step=0),
            agg_page(3, first_id=500 + FULL_PAGE, first_ms=T0),
        ],
    )
    result = _collect(tmp_path, venue, agg_request())

    assert venue.pending() == 0
    assert len(venue.requests) == 2
    assert len(result.objects) == 1
    assert (result.objects[0].coverage_start, result.objects[0].coverage_end) == (
        at_ms(T0),
        at_ms(T0 + 3),
    )
    assert len(result.gaps) == 1
    assert result.gaps[0].coverage_start == at_ms(T0 + 3)


def test_two_symbols_run_independent_chains(tmp_path: Path, venue: RestVenue) -> None:
    for symbol, first_id in ((SYMBOL, 500), (OTHER_SYMBOL, 9_000)):
        queue_agg_chain(venue, symbol, T0, [agg_page(4, first_id=first_id, first_ms=T0)])

    result = _collect(tmp_path, venue, agg_request(symbols=(OTHER_SYMBOL, SYMBOL)))

    assert sorted(item.symbol for item in result.objects) == [SYMBOL, OTHER_SYMBOL]
    assert sorted(gap.symbol for gap in result.gaps) == [SYMBOL, OTHER_SYMBOL]
    assert venue.pending() == 0


def test_page_budget_exhaustion_fails_without_a_gap_and_stays_resumable(
    tmp_path: Path, venue: RestVenue
) -> None:
    queue_agg_chain(
        venue,
        SYMBOL,
        T0,
        [
            agg_page(FULL_PAGE, first_id=500, first_ms=T0),
            agg_page(3, first_id=500 + FULL_PAGE, first_ms=T0 + FULL_PAGE),
        ],
    )
    storage = make_storage(tmp_path)
    request = agg_request()

    with pytest.raises(CollectionFailed, match="page budget"):
        make_collector(storage, venue, max_pages=1).collect(request)

    # The committed first page is not re-fetched; the budget only counts new pages.
    result = make_collector(storage, venue, max_pages=1).collect(request)
    assert len(result.objects) == 2
    assert venue.pending() == 0


# ======================================================================================
# #16 HTTP failures are never gaps
# ======================================================================================


@pytest.mark.parametrize(
    ("answer", "match"),
    [
        pytest.param(Answer(status=400), "HTTP 400", id="400"),
        pytest.param(Answer(status=403), "HTTP 403", id="403"),
        pytest.param(Answer(status=404), "HTTP 404", id="404"),
        pytest.param(Answer(status=418), "418", id="418"),
        pytest.param(
            Answer(status=301, headers={"location": "https://evil.test/x"}), "redirect", id="301"
        ),
        pytest.param(
            Answer(status=302, headers={"location": "/api/v3/account"}), "redirect", id="302"
        ),
        pytest.param(
            Answer(status=307, headers={"location": "https://evil.test/x"}), "redirect", id="307"
        ),
        pytest.param(Answer(status=429), "Retry-After", id="429-missing-retry-after"),
        pytest.param(
            Answer(status=429, headers={"retry-after": "-1"}), "Retry-After", id="429-negative"
        ),
        pytest.param(
            Answer(status=429, headers={"retry-after": "Wed, 21 Oct 2026 07:28:00 GMT"}),
            "Retry-After",
            id="429-http-date",
        ),
        pytest.param(
            Answer(status=429, headers={"retry-after": "61"}), "Retry-After", id="429-over-limit"
        ),
        pytest.param(
            Answer(status=429, headers={"retry-after": " 1"}), "Retry-After", id="429-padded"
        ),
    ],
)
def test_http_failures_raise_and_never_produce_a_gap(
    tmp_path: Path, venue: RestVenue, answer: Answer, match: str
) -> None:
    venue.serve(RestPageQuery.agg_trades_from_start(SYMBOL, T0), answer)

    with pytest.raises(CollectionFailed, match=match):
        _collect(tmp_path, venue, agg_request(), max_retries=0)


def test_429_with_a_legal_retry_after_waits_through_the_injected_sleeper(
    tmp_path: Path, venue: RestVenue
) -> None:
    query = RestPageQuery.agg_trades_from_start(SYMBOL, T0)
    venue.serve(query, Answer(status=429, headers={"retry-after": "2"}))
    venue.serve_items(query, agg_page(2, first_id=500, first_ms=T0))
    timing = FakeTime()

    result = _collect(tmp_path, venue, agg_request(), fake_time=timing)

    assert len(result.objects) == 1
    # The Retry-After wait is honoured exactly once and already covers the minimum spacing.
    assert timing.slept == [2.0]
    assert len(venue.requests) == 2


def test_5xx_is_retried_within_the_attempt_budget_then_fails(
    tmp_path: Path, venue: RestVenue
) -> None:
    query = RestPageQuery.agg_trades_from_start(SYMBOL, T0)
    for _ in range(3):
        venue.serve(query, Answer(status=503))

    with pytest.raises(CollectionFailed, match="exhausted retries for HTTP 503"):
        _collect(tmp_path, venue, agg_request(), max_retries=2)
    assert len(venue.requests) == 3


def test_5xx_then_success_is_one_committed_page(tmp_path: Path, venue: RestVenue) -> None:
    query = RestPageQuery.agg_trades_from_start(SYMBOL, T0)
    venue.serve(query, Answer(status=500))
    venue.serve_items(query, agg_page(2, first_id=500, first_ms=T0))

    result = _collect(tmp_path, venue, agg_request(), max_retries=1)
    assert len(result.objects) == 1


def test_transport_errors_are_retried_then_fail_closed(tmp_path: Path, venue: RestVenue) -> None:
    query = RestPageQuery.agg_trades_from_start(SYMBOL, T0)
    for _ in range(2):
        venue.serve(query, Answer(transport_error=True))

    with pytest.raises(CollectionFailed, match="HTTP transport failure: ConnectError"):
        _collect(tmp_path, venue, agg_request(), max_retries=1)
    assert len(venue.requests) == 2


def test_a_mid_stream_failure_is_retried_and_commits_nothing_on_exhaustion(
    tmp_path: Path, venue: RestVenue
) -> None:
    query = RestPageQuery.agg_trades_from_start(SYMBOL, T0)
    payload = body(agg_page(4, first_id=500, first_ms=T0))
    for _ in range(2):
        venue.serve(query, Answer(payload=payload, fail_after=10))
    storage = make_storage(tmp_path)

    with pytest.raises(CollectionFailed, match="HTTP transport failure"):
        make_collector(storage, venue, max_retries=1).collect(agg_request())
    assert storage.lookup("raw/binance/spot/rest/collections") is None


def test_the_minimum_request_interval_is_enforced_across_every_request(
    tmp_path: Path, venue: RestVenue
) -> None:
    queue_agg_chain(
        venue,
        SYMBOL,
        T0,
        [
            agg_page(FULL_PAGE, first_id=500, first_ms=T0),
            agg_page(1, first_id=500 + FULL_PAGE, first_ms=T0 + FULL_PAGE),
        ],
    )
    timing = FakeTime()

    _collect(tmp_path, venue, agg_request(), fake_time=timing, min_interval_ms=500)

    assert timing.slept == [0.5]  # one gap between the two requests


# ======================================================================================
# #16 body bounds and declared length
# ======================================================================================


def test_a_body_exactly_at_the_limit_is_accepted(tmp_path: Path, venue: RestVenue) -> None:
    payload = body(agg_page(2, first_id=500, first_ms=T0))
    venue.serve(RestPageQuery.agg_trades_from_start(SYMBOL, T0), Answer(payload=payload))

    result = _collect(tmp_path, venue, agg_request(), max_response_bytes=max(65_536, len(payload)))
    assert len(result.objects) == 1


def test_one_byte_over_the_limit_fails_and_publishes_nothing(
    tmp_path: Path, venue: RestVenue
) -> None:
    payload = body(agg_page(FULL_PAGE, first_id=500, first_ms=T0))
    limit = 65_536
    assert len(payload) > limit
    venue.serve(
        RestPageQuery.agg_trades_from_start(SYMBOL, T0),
        Answer(payload=payload, omit_content_length=True, chunk_size=1024),
    )
    storage = make_storage(tmp_path)

    with pytest.raises(CollectionFailed, match="exceeds the 65536 byte limit"):
        make_collector(storage, venue, max_response_bytes=limit, max_retries=0).collect(
            agg_request()
        )


def test_a_declared_length_above_the_limit_fails_before_the_body_is_read(
    tmp_path: Path, venue: RestVenue
) -> None:
    venue.serve(
        RestPageQuery.agg_trades_from_start(SYMBOL, T0),
        Answer(payload=b"[]", content_length=str(MAX_RESPONSE_BYTES)),
    )
    with pytest.raises(CollectionFailed, match="declared body"):
        _collect(tmp_path, venue, agg_request(), max_response_bytes=65_536, max_retries=0)


@pytest.mark.parametrize("declared", ["1", "999999", "0"])
def test_a_content_length_disagreeing_with_the_entity_fails_closed(
    tmp_path: Path, venue: RestVenue, declared: str
) -> None:
    payload = body(agg_page(2, first_id=500, first_ms=T0))
    venue.serve(
        RestPageQuery.agg_trades_from_start(SYMBOL, T0),
        Answer(payload=payload, content_length=declared),
    )
    with pytest.raises(CollectionFailed, match="disagrees with"):
        _collect(tmp_path, venue, agg_request(), max_retries=0)


@pytest.mark.parametrize("declared", ["0x10", "+12", "12,12", "twelve", ""])
def test_a_malformed_content_length_fails_closed(
    tmp_path: Path, venue: RestVenue, declared: str
) -> None:
    venue.serve(
        RestPageQuery.agg_trades_from_start(SYMBOL, T0),
        Answer(payload=b"[]", content_length=declared),
    )
    with pytest.raises(CollectionFailed, match="Content-Length"):
        _collect(tmp_path, venue, agg_request(), max_retries=0)


def test_a_compressed_body_that_explodes_past_the_limit_fails_closed(
    tmp_path: Path, venue: RestVenue
) -> None:
    import gzip

    plain = b"[" + b'{"a":1},' * 20_000 + b"]"
    compressed = gzip.compress(plain)
    assert len(compressed) < 65_536 < len(plain)
    venue.serve(
        RestPageQuery.agg_trades_from_start(SYMBOL, T0),
        Answer(payload=compressed, headers={"content-encoding": "gzip"}),
    )
    with pytest.raises(CollectionFailed, match="exceeds the 65536 byte limit"):
        _collect(tmp_path, venue, agg_request(), max_response_bytes=65_536, max_retries=0)


# ======================================================================================
# #10 / #20 local instants and metadata
# ======================================================================================


def test_a_stalled_clock_fails_closed(tmp_path: Path, venue: RestVenue) -> None:
    venue.serve_items(
        RestPageQuery.agg_trades_from_start(SYMBOL, T0), agg_page(2, first_id=500, first_ms=T0)
    )
    with pytest.raises(CollectionFailed, match="clock did not advance"):
        _collect(tmp_path, venue, agg_request(), clock=FrozenClock(), max_retries=0)


@pytest.mark.parametrize(
    "bad",
    [
        pytest.param(lambda: datetime(2026, 9, 25, 12, 0), id="naive"),
        pytest.param(lambda: datetime(2026, 9, 25, 12, 0, tzinfo=_plus_one_hour()), id="non-utc"),
    ],
)
def test_a_non_utc_clock_fails_closed(tmp_path: Path, venue: RestVenue, bad: Any) -> None:
    venue.serve_items(
        RestPageQuery.agg_trades_from_start(SYMBOL, T0), agg_page(2, first_id=500, first_ms=T0)
    )
    with pytest.raises(CollectionFailed, match="requested_at must"):
        _collect(tmp_path, venue, agg_request(), clock=bad, max_retries=0)


def _plus_one_hour() -> Any:
    from datetime import timedelta, timezone

    return timezone(timedelta(hours=1))


def test_only_allow_listed_response_headers_are_recorded(tmp_path: Path, venue: RestVenue) -> None:
    venue.serve(
        RestPageQuery.agg_trades_from_start(SYMBOL, T0),
        Answer(
            payload=body(agg_page(2, first_id=500, first_ms=T0)),
            headers={
                "etag": '"abc"',
                "date": "Wed, 25 Sep 2026 12:00:00 GMT",
                "x-mbx-used-weight-1m": "4",
                "x-request-trace": "should-not-be-kept",
                "server": "nginx",
            },
        ),
    )
    result = _collect(tmp_path, venue, agg_request())
    metadata = dict(result.objects[0].source_metadata)
    assert metadata["etag"] == '"abc"'
    assert metadata["x-mbx-used-weight-1m"] == "4"
    assert "x-request-trace" not in metadata


@pytest.mark.parametrize(
    "header", ["authorization", "cookie", "set-cookie", "x-api-key", "x-mbx-apikey", "x-signature"]
)
def test_a_credential_shaped_response_header_refuses_the_page(
    tmp_path: Path, venue: RestVenue, header: str
) -> None:
    venue.serve(
        RestPageQuery.agg_trades_from_start(SYMBOL, T0),
        Answer(
            payload=body(agg_page(2, first_id=500, first_ms=T0)),
            headers={header: "leaked-value"},
        ),
    )
    storage = make_storage(tmp_path)
    with pytest.raises(CollectionFailed) as info:
        make_collector(storage, venue, max_retries=0).collect(agg_request())
    assert "leaked-value" not in str(info.value)


def test_the_credential_denylist_matches_the_collector_contract() -> None:
    from core.contracts.collector import _SECRET_NAME_PARTS
    from infrastructure.collector.binance_rest import _CREDENTIAL_NAME_PARTS

    assert set(_SECRET_NAME_PARTS) <= set(_CREDENTIAL_NAME_PARTS)


# ======================================================================================
# #17 the endpoint allowlist: refused before any socket
# ======================================================================================


@pytest.mark.parametrize(
    "base",
    [
        "https://market.test/api",
        "https://market.test/api/v3",
        "https://market.test/?x=1",
        "https://market.test#frag",
        "https://user:pass@market.test",
        "https://MARKET.test",
        "https://market.test:0",
        "https://market.test:099",
        "https://market.test:70000",
        "http://market.test",
        "ftp://market.test",
        "https://market%2etest",
        "market.test",
        "",
        " https://market.test",
        "https://market.test/../evil",
    ],
)
def test_an_unacceptable_market_data_base_is_refused_at_construction(
    tmp_path: Path, venue: RestVenue, base: str
) -> None:
    storage = make_storage(tmp_path)
    with pytest.raises(ValueError):
        make_collector(storage, venue, base_url=base)
    assert venue.requests == []


def test_a_base_with_a_bare_root_path_is_the_origin(tmp_path: Path, venue: RestVenue) -> None:
    storage = make_storage(tmp_path)
    collector = make_collector(storage, venue, base_url=f"{ORIGIN}/")
    assert collector.descriptor.network_origins == (ORIGIN,)


def test_an_unsupported_source_or_shape_is_refused_with_zero_requests(
    tmp_path: Path, venue: RestVenue
) -> None:
    storage = make_storage(tmp_path)
    collector = make_collector(storage, venue)
    base = agg_request()
    cases = [
        base.model_copy(
            update={"source": SourceBinding(source_id="binance.public.spot.rest", version="2.0.0")}
        ),
        base.model_copy(update={"source": REST_SOURCE, "data_type": "order_book"}),
        base.model_copy(update={"symbols": ("DOGEUSDT",)}),
        kline_request().model_copy(
            update={"coverage_start": at_ms(T0 + 1), "coverage_end": at_ms(T0 + MINUTE_MS)}
        ),
    ]
    for request in cases:
        with pytest.raises(UnsupportedRequest):
            collector.collect(request)
    assert venue.requests == []


def test_a_sub_millisecond_window_is_refused_with_zero_requests(
    tmp_path: Path, venue: RestVenue
) -> None:
    storage = make_storage(tmp_path)
    collector = make_collector(storage, venue)
    request = CollectionRequest(
        request_id="d3d-sub-ms",
        source=REST_SOURCE,
        data_type="agg_trades",
        symbols=(SYMBOL,),
        coverage_start=datetime(2023, 11, 14, 22, 14, 0, 500, tzinfo=UTC),
        coverage_end=at_ms(WINDOW_END),
    )
    with pytest.raises(UnsupportedRequest, match="whole millisecond"):
        collector.collect(request)
    assert venue.requests == []


def test_the_collector_only_ever_asks_for_the_two_frozen_endpoints(
    tmp_path: Path, venue: RestVenue
) -> None:
    queue_agg_chain(venue, SYMBOL, T0, [agg_page(2, first_id=500, first_ms=T0)])
    queue_kline_chain(venue, SYMBOL, T0, [kline_page(2, first_ms=T0)])
    storage = make_storage(tmp_path)
    make_collector(storage, venue).collect(agg_request())
    make_collector(storage, venue).collect(kline_request())

    for url in venue.requests:
        assert url.startswith(f"{ORIGIN}/api/v3/aggTrades?") or url.startswith(
            f"{ORIGIN}/api/v3/klines?"
        )
        assert "apiKey" not in url and "signature" not in url and "timestamp" not in url


def test_a_forged_query_can_never_reach_the_transport(tmp_path: Path, venue: RestVenue) -> None:
    """The only way in is a ``RestPageQuery``; its construction is the parameter allowlist."""
    from infrastructure.revision.rest_identity import RestIdentityViolation

    storage = make_storage(tmp_path)
    collector = make_collector(storage, venue)
    makers: tuple[Callable[[], RestPageQuery], ...] = (
        lambda: RestPageQuery("agg_trades", SYMBOL),  # neither fromId nor startTime
        lambda: RestPageQuery("agg_trades", SYMBOL, from_id=1, start_time_ms=T0),
        lambda: RestPageQuery("klines_1m", SYMBOL, start_time_ms=T0, from_id=1),
        lambda: RestPageQuery("klines_1m", SYMBOL, start_time_ms=T0 + 1),
        lambda: RestPageQuery("agg_trades", SYMBOL, start_time_ms=T0, limit=500),
        lambda: RestPageQuery("depth", SYMBOL, start_time_ms=T0),
        lambda: RestPageQuery("agg_trades", "../../account", start_time_ms=T0),
    )
    for maker in makers:
        with pytest.raises(RestIdentityViolation):
            collector._page_url(maker())
    assert venue.requests == []


def test_a_page_url_pointing_off_origin_is_refused_before_any_request(
    tmp_path: Path, venue: RestVenue
) -> None:
    storage = make_storage(tmp_path)
    collector = make_collector(storage, venue)

    class _Elsewhere:
        data_type = "agg_trades"
        symbol = SYMBOL
        path = "/api/v3/aggTrades"
        limit = 1000

        @staticmethod
        def query_string() -> str:
            return "limit=1000&startTime=1&symbol=BTCUSDT"

    with pytest.raises(CollectionFailed):
        collector._page_url(_Elsewhere())  # type: ignore[arg-type]
    assert venue.requests == []


def test_the_descriptor_declares_only_the_configured_origin_and_source(
    tmp_path: Path, venue: RestVenue
) -> None:
    collector = make_collector(make_storage(tmp_path), venue)
    descriptor = collector.descriptor
    assert descriptor.collector_id == REST_COLLECTOR_ID
    assert descriptor.version == REST_COLLECTOR_VERSION
    assert descriptor.sources == (REST_SOURCE,)
    assert descriptor.network_origins == (ORIGIN,)


def test_the_client_never_follows_redirects(tmp_path: Path, venue: RestVenue) -> None:
    collector = make_collector(make_storage(tmp_path), venue)
    assert collector._client.follow_redirects is False


def test_out_of_range_operational_settings_are_refused(tmp_path: Path, venue: RestVenue) -> None:
    storage = make_storage(tmp_path)
    for kwargs in (
        {"max_pages": 0},
        {"max_pages": 5_001},
        {"min_interval_ms": 49},
        {"min_interval_ms": 60_001},
        {"max_retry_after": 0},
        {"max_retry_after": 3_601},
        {"max_response_bytes": 65_535},
        {"max_response_bytes": 67_108_865},
    ):
        with pytest.raises(ValueError):
            make_collector(storage, venue, **kwargs)  # type: ignore[arg-type]


def test_close_is_idempotent_and_leaves_injected_clients_alone(
    tmp_path: Path, venue: RestVenue
) -> None:
    import httpx

    storage = make_storage(tmp_path)
    with BinanceSpotRestCollector(
        storage,
        market_data_base_url=ORIGIN,
        http_connect_timeout_seconds=1.0,
        http_read_timeout_seconds=1.0,
        http_max_retries=0,
        http_user_agent="hlens-d3d-test/0.0.0",
        max_pages_per_collect=1,
        min_request_interval_ms=50,
        max_retry_after_seconds=1,
        max_response_bytes=65_536,
        http_transport=venue.transport(),
    ) as collector:
        assert collector.descriptor.version == REST_COLLECTOR_VERSION
    injected = httpx.Client(transport=venue.transport())
    other = BinanceSpotRestCollector(
        storage,
        market_data_base_url=ORIGIN,
        http_connect_timeout_seconds=1.0,
        http_read_timeout_seconds=1.0,
        http_max_retries=0,
        http_user_agent="hlens-d3d-test/0.0.0",
        max_pages_per_collect=1,
        min_request_interval_ms=50,
        max_retry_after_seconds=1,
        max_response_bytes=65_536,
        http_client=injected,
    )
    other.close()
    assert not injected.is_closed
    injected.close()


def test_retrieved_at_is_taken_after_the_last_byte(tmp_path: Path, venue: RestVenue) -> None:
    venue.serve(
        RestPageQuery.agg_trades_from_start(SYMBOL, T0),
        Answer(payload=body(agg_page(2, first_id=500, first_ms=T0)), chunk_size=4),
    )
    clock = StepClock(start_ms=RETRIEVED_AT_MS, step_ms=7)
    result = _collect(tmp_path, venue, agg_request(), clock=clock)
    # requested_at was the first reading, retrieved_at the second.
    assert result.objects[0].retrieved_at == at_ms(RETRIEVED_AT_MS + 7)

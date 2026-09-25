"""D3E REST revision store (ADR-0027 §2 / §3 / §8 / §9 / §11; acceptance #2 ~ #5, #8, #11, #13,
#15 crash point 3, #20, #21).

Every test drives the real D3D collector against the mock venue, so the store consumes genuine,
committed checkpoints and bodies. Expected values are derived independently of the store: from
the bytes the venue served, from the committed D3D checkpoint documents, from the injected
clocks and from the frozen identity / policy constants.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from core.contracts.catalog import CommitConflict, CommitOutcome, CommitRequest
from core.contracts.collector import CollectionFailed, CollectionResult
from core.domain.base import canonical_json
from infrastructure.catalog.iceberg_adapter import CatalogIntegrityError
from infrastructure.catalog.phase1_tables import (
    BINANCE_SPOT_PRECEDENCE_EVIDENCE,
    BINANCE_SPOT_REST_AGG_TRADES,
    BINANCE_SPOT_REST_KLINES_1M,
    BINANCE_SPOT_REST_RESPONSES,
)
from infrastructure.parser.binance_rest import DECODER_HASH
from infrastructure.revision import rest_identity
from infrastructure.revision.rest_availability import REST_AVAILABILITY_HASH
from infrastructure.revision.rest_identity import RestPageQuery
from infrastructure.revision.rest_store import (
    FINDING_AGG_TRADE_COMPETING,
    FINDING_DECODER_REJECTED,
    FINDING_RESPONSE_COMPETING,
    RestCheckpointIntegrityError,
    RestCollectionStored,
    RestRevisionStoreConflict,
    RestRevisionStoreError,
)
from tests.infrastructure.collector import rest_support as cs
from tests.infrastructure.revision import rest_store_support as ss
from tests.infrastructure.revision.rest_store_support import (
    MINUTE_MS,
    ORIGIN,
    SYMBOL,
    T0,
    Crash,
    ProxyCatalog,
    RestHarness,
    StepClock,
    at_ms,
    utc,
)

RESPONSES = BINANCE_SPOT_REST_RESPONSES
AGGS = BINANCE_SPOT_REST_AGG_TRADES
KLINES = BINANCE_SPOT_REST_KLINES_1M
BASE = 1 << 62
STRIDE = 1 << 32
K1 = utc(2023, 12, 1)


@pytest.fixture
def h(tmp_path: Path) -> Iterator[RestHarness]:
    with ss.sqlite_harness(tmp_path) as opened:
        yield opened


def _collected(h: RestHarness, request: Any, **kwargs: Any) -> CollectionResult:
    result = h.collect(request, **kwargs)
    assert isinstance(result, CollectionResult), result
    return result


def _page_doc(h: RestHarness, request_id: str, index: int = 0) -> dict[str, Any]:
    document: dict[str, Any] = json.loads(h.read_object(ss.page_checkpoint_key(request_id, index)))
    return document


def _by(rows: list[dict[str, Any]], column: str) -> dict[Any, dict[str, Any]]:
    return {row[column]: row for row in rows}


def _page_identity(query: RestPageQuery) -> str:
    """Independent re-statement of the ADR-0027 §5 canonical page identity document."""
    document = {
        "method": "GET",
        "origin": ORIGIN,
        "path": query.path,
        "query": [[name, value] for name, value in query.pairs()],
        "declared_time_unit": "millisecond",
        "rule": "hlens.binance.spot.rest-revision-identity@1.0.0",
    }
    return hashlib.sha256(canonical_json(document).encode("utf-8")).hexdigest()


# =========================================================================================
# #20 / #21 / #3-matrix: every frozen field of a response and its elements
# =========================================================================================


def test_agg_page_response_and_elements_carry_every_frozen_field(h: RestHarness) -> None:
    items = ss.agg_items(3)
    body = ss.body(items)
    cs.queue_agg_chain(h.venue, SYMBOL, T0, [items])
    request = ss.agg_request("req-a")
    _collected(h, request)
    clock = StepClock(start=K1)

    out = h.store(clock=clock).ingest_collection(request)

    doc = _page_doc(h, "req-a")
    requested = datetime.fromisoformat(doc["requested_at"])
    retrieved = datetime.fromisoformat(doc["retrieved_at"])
    query = RestPageQuery.agg_trades_from_start(SYMBOL, T0)
    identity = _page_identity(query)
    body_sha = hashlib.sha256(body).hexdigest()
    key = f"binance:spot:rest:{identity}"
    response_id = rest_identity.revision_id(key, "binance.public.spot.rest@1.0.0", body_sha)
    [row] = h.rows(RESPONSES)
    assert clock.calls == 1  # knowledge_time is read once, after verification
    assert row == {
        "observation_key": key,
        "revision_id": response_id,
        "source_id": "binance.public.spot.rest@1.0.0",
        "payload_hash": body_sha,
        "arrival_seq": BASE,
        "supersedes": [],
        "source_revision_id": None,
        "source_revision_time": None,
        "event_time": requested,
        "event_end_time": retrieved,
        "source_time": None,
        "available_time": retrieved,
        "ingest_time": retrieved,
        "knowledge_time": K1,
        "declared_latency_us": 0,
        "availability_policy_id": "binance.spot.rest-publication",
        "availability_policy_version": "1.0.0",
        "availability_policy_hash": REST_AVAILABILITY_HASH,
        "availability_evidence": [],
        "availability_evidence_gap": row["availability_evidence_gap"],
        "precedence_evidence": [],
        "contract_schema_version": "2.0.0",
        "source_binding_id": "binance.public.spot.rest",
        "source_binding_version": "1.0.0",
        "collector_id": "binance.spot.public-rest",
        "collector_version": "1.0.0",
        "collection_request_id": "req-a",
        "page_index": 0,
        "data_type": "agg_trades",
        "symbol": SYMBOL,
        "request_origin": ORIGIN,
        "request_path": "/api/v3/aggTrades",
        "request_query": f"limit=1000&startTime={T0}&symbol={SYMBOL}",
        "declared_time_unit": "millisecond",
        "page_limit": 1000,
        "page_identity_sha256": identity,
        "source_uri": f"{ORIGIN}/api/v3/aggTrades?limit=1000&startTime={T0}&symbol={SYMBOL}",
        "requested_at": requested,
        "retrieved_at": retrieved,
        "http_status": 200,
        "source_metadata": [
            {"name": name, "value": doc["http"]["metadata"][name]}
            for name in sorted(doc["http"]["metadata"])
        ],
        "object_key": f"raw/binance/spot/rest/responses/{body_sha}/agg_trades/{SYMBOL}/"
        f"{identity}.json",
        "object_uri": doc["body"]["uri"],
        "object_sha256": body_sha,
        "object_size_bytes": len(body),
        "decoder_id": "binance.spot.rest.decoder",
        "decoder_version": "1.0.0",
        "decoder_hash": DECODER_HASH,
        "decode_outcome": "accepted",
        "decode_rejection_code": None,
        "element_count": 3,
        "answered_start": at_ms(T0),
        "answered_end": at_ms(T0 + 2 + 1),  # short page: last T + 1 tick
    }
    assert row["availability_evidence_gap"].startswith(
        "binance.spot.rest-publication@1.0.0:rest_response_publication_time_not_stated"
    )
    assert h.read_object(row["object_key"]) == body

    elements = sorted(h.rows(AGGS), key=lambda item: item["element_index"])
    assert [item["element_index"] for item in elements] == [0, 1, 2]
    for index, (element, item) in enumerate(zip(elements, items, strict=True)):
        assert element["observation_key"] == f"binance:spot:agg_trade:{SYMBOL}:{item['a']}"
        assert element["source_id"] == "binance.public.spot.rest@1.0.0"
        assert element["arrival_seq"] == BASE + index + 1
        assert element["response_revision_id"] == response_id
        assert element["event_time"] == at_ms(item["T"])
        assert element["timestamp_raw"] == item["T"]
        assert element["price"] == Decimal(item["p"]) and element["quantity"] == Decimal(item["q"])
        assert (element["agg_trade_id"], element["first_trade_id"], element["last_trade_id"]) == (
            item["a"],
            item["f"],
            item["l"],
        )
        assert (element["is_buyer_maker"], element["is_best_match"]) == (item["m"], item["M"])
        assert element["ingest_time"] == element["available_time"] == retrieved
        assert element["knowledge_time"] == K1
        assert element["supersedes"] == [] and element["precedence_evidence"] == []
        assert element["availability_evidence_gap"].startswith(
            "binance.spot.rest-publication@1.0.0:agg_trade_publication_bound_not_stated"
        )
        assert (element["decoder_id"], element["decoder_version"], element["decoder_hash"]) == (
            "binance.spot.rest.decoder",
            "1.0.0",
            DECODER_HASH,
        )
    # The payload hash is the D2 document over the *stored* decimal(38, 18) values.
    first = items[0]
    document = {
        "kind": "binance.spot.agg_trade/1",
        "venue": "binance",
        "market": "spot",
        "symbol": SYMBOL,
        "time_unit": "millisecond",
        "agg_trade_id": first["a"],
        "price": "92792.050000000000000000",
        "quantity": "0.001500000000000000",
        "first_trade_id": first["f"],
        "last_trade_id": first["l"],
        "timestamp_raw": first["T"],
        "is_buyer_maker": first["m"],
        "is_best_match": first["M"],
    }
    expected_payload = hashlib.sha256(canonical_json(document).encode("utf-8")).hexdigest()
    assert elements[0]["payload_hash"] == expected_payload
    assert elements[0]["revision_id"] == rest_identity.revision_id(
        elements[0]["observation_key"], "binance.public.spot.rest@1.0.0", expected_payload
    )
    [page] = out.pages
    assert page.response_revision_id == response_id and page.first_delivery
    assert page.element_revision_ids == tuple(item["revision_id"] for item in elements)
    assert page.owned_element_revision_ids == page.element_revision_ids
    assert out.findings == ()
    assert h.rows(BINANCE_SPOT_PRECEDENCE_EVIDENCE) == []


def test_kline_page_elements_use_exact_millisecond_intervals(h: RestHarness) -> None:
    items = ss.kline_items(3)
    cs.queue_kline_chain(h.venue, SYMBOL, T0, [items])
    request = ss.kline_request("req-k")
    _collected(h, request)
    h.store(clock=StepClock(start=K1)).ingest_collection(request)

    [response] = h.rows(RESPONSES)
    assert response["answered_start"] == at_ms(T0)
    assert response["answered_end"] == at_ms(T0 + 3 * MINUTE_MS)
    rows = sorted(h.rows(KLINES), key=lambda item: item["element_index"])
    assert len(rows) == 3
    for index, row in enumerate(rows):
        start = T0 + index * MINUTE_MS
        assert row["observation_key"] == (
            f"binance:spot:kline:{SYMBOL}:1m:{start * 1000}"  # epoch microseconds
        )
        assert row["interval_start"] == at_ms(start)
        assert row["interval_end"] == at_ms(start + MINUTE_MS)
        assert (row["open_time_raw"], row["close_time_raw"]) == (start, start + MINUTE_MS - 1)
        assert row["arrival_seq"] == BASE + index + 1
        assert row["ignore_raw"] == "0"
        assert row["knowledge_time"] == K1 and row["ingest_time"] == response["ingest_time"]
        assert row["availability_evidence_gap"].startswith(
            "binance.spot.rest-publication@1.0.0:kline_1m_publication_bound_not_stated"
        )


# =========================================================================================
# #2 / #3: response idempotency and competing payloads
# =========================================================================================


def test_same_page_same_bytes_from_another_request_is_idempotent(h: RestHarness) -> None:
    items = ss.agg_items(3)
    for _ in range(2):
        cs.queue_agg_chain(h.venue, SYMBOL, T0, [items])
    first_request, second_request = ss.agg_request("req-a"), ss.agg_request("req-b", minutes=9)
    _collected(h, first_request)
    _collected(h, second_request, start_ms=cs.RETRIEVED_AT_MS + 60_000)
    clock = StepClock(start=K1)
    store = h.store(clock=clock)
    first = store.ingest_collection(first_request)
    heads = (h.head(RESPONSES.table), h.head(AGGS.table))

    second = store.ingest_collection(second_request)

    assert second.replayed and second.new_snapshot_ids == ()
    assert (h.head(RESPONSES.table), h.head(AGGS.table)) == heads
    assert clock.calls == 1  # no second knowledge_time for the same revision
    [row] = h.rows(RESPONSES)
    assert row["collection_request_id"] == "req-a"  # first delivery stays the provenance
    [page_a], [page_b] = first.pages, second.pages
    assert page_b.response_revision_id == page_a.response_revision_id
    assert (page_b.arrival_seq_base, page_b.knowledge_time) == (BASE, K1)
    assert page_a.first_delivery and not page_b.first_delivery
    assert len(h.rows(AGGS)) == 3 and second.findings == ()


def test_same_page_other_bytes_is_a_second_response_revision_and_a_finding(
    h: RestHarness,
) -> None:
    items = ss.agg_items(3)
    changed = [dict(item) for item in items]
    changed[1]["p"] = "92792.06000000"
    cs.queue_agg_chain(h.venue, SYMBOL, T0, [items])
    cs.queue_agg_chain(h.venue, SYMBOL, T0, [changed])
    _collected(h, ss.agg_request("req-a"))
    _collected(h, ss.agg_request("req-b"), start_ms=cs.RETRIEVED_AT_MS + 60_000)
    store = h.store(clock=StepClock(start=K1))
    first = store.ingest_collection(ss.agg_request("req-a"))
    second = store.ingest_collection(ss.agg_request("req-b"))

    responses = h.rows(RESPONSES)
    assert len(responses) == 2
    assert {row["observation_key"] for row in responses} == {first.pages[0].observation_key}
    assert sorted(row["arrival_seq"] for row in responses) == [BASE, BASE + STRIDE]
    assert all(row["supersedes"] == [] for row in responses)
    finding = [item for item in second.findings if item.code == FINDING_RESPONSE_COMPETING]
    assert len(finding) == 1
    assert finding[0].related_revision_ids == (first.pages[0].response_revision_id,)

    # Elements: the two identical ones keep their first lineage; the changed one competes.
    elements = h.rows(AGGS)
    assert len(elements) == 4
    by_id = _by(elements, "revision_id")
    page_b = second.pages[0]
    assert page_b.owned_element_revision_ids == (page_b.element_revision_ids[1],)
    changed_row = by_id[page_b.element_revision_ids[1]]
    assert changed_row["response_revision_id"] == page_b.response_revision_id
    assert changed_row["arrival_seq"] == BASE + STRIDE + 1 + 1
    for kept in (page_b.element_revision_ids[0], page_b.element_revision_ids[2]):
        assert by_id[kept]["response_revision_id"] == first.pages[0].response_revision_id
    competing = [item for item in second.findings if item.code == FINDING_AGG_TRADE_COMPETING]
    assert [item.related_revision_ids for item in competing] == [
        (first.pages[0].element_revision_ids[1],)
    ]
    assert all(row["supersedes"] == [] and row["precedence_evidence"] == [] for row in elements)
    assert h.rows(BINANCE_SPOT_PRECEDENCE_EVIDENCE) == []


# =========================================================================================
# #4 / #5: elements delivered by overlapping pages
# =========================================================================================


def test_overlapping_pages_are_idempotent_per_element_and_keep_first_lineage(
    h: RestHarness,
) -> None:
    a_items = ss.agg_items(3, first_id=100, first_ms=T0)
    b_items = ss.agg_items(3, first_id=101, first_ms=T0 + 1)
    cs.queue_agg_chain(h.venue, SYMBOL, T0, [a_items])
    cs.queue_agg_chain(h.venue, SYMBOL, T0 + 1, [b_items])
    _collected(h, ss.agg_request("req-a"))
    _collected(h, ss.agg_request("req-b", start_ms=T0 + 1))
    store = h.store(clock=StepClock(start=K1))
    first = store.ingest_collection(ss.agg_request("req-a"))
    second = store.ingest_collection(ss.agg_request("req-b", start_ms=T0 + 1))

    rows = h.rows(AGGS)
    assert len(rows) == 4
    page_a, page_b = first.pages[0], second.pages[0]
    assert page_b.element_revision_ids[:2] == page_a.element_revision_ids[1:]
    assert page_b.owned_element_revision_ids == (page_b.element_revision_ids[2],)
    by_id = _by(rows, "revision_id")
    for shared in page_a.element_revision_ids[1:]:
        assert by_id[shared]["response_revision_id"] == page_a.response_revision_id
        assert by_id[shared]["knowledge_time"] == page_a.knowledge_time
    new = by_id[page_b.element_revision_ids[2]]
    assert new["arrival_seq"] == page_b.arrival_seq_base + 2 + 1
    assert new["knowledge_time"] == page_b.knowledge_time > page_a.knowledge_time
    assert second.findings == ()


def test_two_pages_disagreeing_on_an_element_append_a_competing_revision(
    h: RestHarness,
) -> None:
    a_items = ss.agg_items(3, first_id=100, first_ms=T0)
    b_items = ss.agg_items(3, first_id=101, first_ms=T0 + 1)
    b_items[0]["q"] = "0.00250000"  # element 101 with other content
    cs.queue_agg_chain(h.venue, SYMBOL, T0, [a_items])
    cs.queue_agg_chain(h.venue, SYMBOL, T0 + 1, [b_items])
    _collected(h, ss.agg_request("req-a"))
    _collected(h, ss.agg_request("req-b", start_ms=T0 + 1))
    store = h.store(clock=StepClock(start=K1))
    first = store.ingest_collection(ss.agg_request("req-a"))
    second = store.ingest_collection(ss.agg_request("req-b", start_ms=T0 + 1))

    key = f"binance:spot:agg_trade:{SYMBOL}:101"
    revisions = [row for row in h.rows(AGGS) if row["observation_key"] == key]
    assert len(revisions) == 2
    assert all(row["supersedes"] == [] for row in revisions)
    [finding] = second.findings
    assert finding.code == FINDING_AGG_TRADE_COMPETING and finding.observation_key == key
    assert finding.related_revision_ids == (first.pages[0].element_revision_ids[1],)
    assert h.rows(BINANCE_SPOT_PRECEDENCE_EVIDENCE) == []


# =========================================================================================
# #11 / #12 / #13: empty, overshoot, unclosed, quality facts, rejected pages
# =========================================================================================


def test_empty_first_page_is_a_response_revision_without_elements(h: RestHarness) -> None:
    cs.queue_agg_chain(h.venue, SYMBOL, T0, [[]])
    request = ss.agg_request("req-empty")
    result = _collected(h, request)
    assert result.objects == ()  # not a CollectedObject …

    out = h.store(clock=StepClock(start=K1)).ingest_collection(request)

    [row] = h.rows(RESPONSES)  # … but a response revision
    assert (row["decode_outcome"], row["element_count"]) == ("accepted", 0)
    assert (row["answered_start"], row["answered_end"]) == (None, None)
    assert row["payload_hash"] == hashlib.sha256(b"[]").hexdigest()
    assert out.pages[0].element_commits == () and h.rows(AGGS) == []


def test_elements_beyond_the_target_window_are_element_revisions(h: RestHarness) -> None:
    items = [ss.agg_item(100, T0), ss.agg_item(101, T0 + 2 * MINUTE_MS)]
    cs.queue_agg_chain(h.venue, SYMBOL, T0, [items])
    request = ss.agg_request("req-over", minutes=1)
    _collected(h, request)

    h.store(clock=StepClock(start=K1)).ingest_collection(request)

    assert sorted(row["agg_trade_id"] for row in h.rows(AGGS)) == [100, 101]
    [row] = h.rows(RESPONSES)
    assert row["element_count"] == 2


def test_unclosed_kline_is_kept_in_the_bytes_but_never_an_element(h: RestHarness) -> None:
    items = ss.kline_items(4)
    retrieved_ms = T0 + 3 * MINUTE_MS + 30_000  # during the fourth minute
    cs.queue_kline_chain(h.venue, SYMBOL, T0, [items], retrieved_at_ms=retrieved_ms)
    request = ss.kline_request("req-unclosed")
    _collected(h, request, start_ms=retrieved_ms)

    out = h.store(clock=StepClock(start=K1)).ingest_collection(request)

    [row] = h.rows(RESPONSES)
    assert row["element_count"] == 3
    assert row["answered_end"] == at_ms(T0 + 3 * MINUTE_MS)
    assert h.read_object(row["object_key"]) == ss.body(items)  # the bytes keep all four
    assert len(h.rows(KLINES)) == 3
    assert [item.code for item in out.findings] == ["rest_unclosed_kline_skipped"]


def test_decoder_quality_facts_become_findings(h: RestHarness) -> None:
    klines = [ss.kline_item(T0), ss.kline_item(T0 + 2 * MINUTE_MS)]
    cs.queue_kline_chain(h.venue, SYMBOL, T0, [klines])
    trades = [ss.agg_item(100, T0), ss.agg_item(103, T0 + 1)]
    cs.queue_agg_chain(h.venue, SYMBOL, T0, [trades])
    _collected(h, ss.kline_request("req-gap-k"))
    _collected(h, ss.agg_request("req-gap-a"))
    store = h.store(clock=StepClock(start=K1))

    kline_out = store.ingest_collection(ss.kline_request("req-gap-k"))
    agg_out = store.ingest_collection(ss.agg_request("req-gap-a"))

    assert [item.code for item in kline_out.findings] == ["rest_window_gap"]
    assert [item.code for item in agg_out.findings] == ["rest_agg_trade_id_gap"]
    assert all(item.page_index == 0 for item in (*kline_out.findings, *agg_out.findings))
    assert len(h.rows(KLINES)) == 2 and len(h.rows(AGGS)) == 2


def test_rejected_page_is_a_response_revision_with_zero_elements(h: RestHarness) -> None:
    full = ss.agg_items(1000, first_id=100, first_ms=T0)
    bad = [ss.agg_item(1100, T0 + 1000, price="0.00000000")]
    cs.queue_agg_chain(h.venue, SYMBOL, T0, [full, bad])
    request = ss.agg_request("req-bad")
    assert isinstance(h.collect(request), CollectionFailed)
    clock = StepClock(start=K1)

    out = h.store(clock=clock).ingest_collection(request)

    assert out.collection_outcome == "failed" and out.result is None
    assert [page.decode_outcome for page in out.pages] == ["accepted", "rejected"]
    rows = sorted(h.rows(RESPONSES), key=lambda row: row["page_index"])
    rejected = rows[1]
    assert rejected["decode_outcome"] == "rejected"
    assert rejected["decode_rejection_code"] == "non_positive_price"
    assert rejected["element_count"] is None
    assert rejected["answered_start"] is None and rejected["answered_end"] is None
    assert rejected["knowledge_time"] == K1 + timedelta(seconds=1)  # exists despite rejection
    assert rejected["arrival_seq"] == BASE + STRIDE
    assert len(h.rows(AGGS)) == 1000
    assert all(row["response_revision_id"] == rows[0]["revision_id"] for row in h.rows(AGGS))
    [finding] = out.findings
    assert finding.code == FINDING_DECODER_REJECTED
    assert finding.revision_id == rejected["revision_id"] and finding.page_index == 1
    assert finding.detail.startswith("non_positive_price:")


# =========================================================================================
# c3 trust boundary
# =========================================================================================


def test_an_uncommitted_attempt_is_refused_without_any_network(h: RestHarness) -> None:
    store = h.store(clock=StepClock(start=K1))
    with pytest.raises(RestRevisionStoreError, match="no committed collection checkpoint"):
        store.ingest_collection(ss.agg_request("never-collected"))
    # A chain that stopped mid-way (budget exhausted) has pages but no collection checkpoint.
    full = ss.agg_items(1000, first_id=100, first_ms=T0)
    cs.queue_agg_chain(h.venue, SYMBOL, T0, [full])
    collector = cs.make_collector(h.storage, h.venue, max_pages=1)
    with pytest.raises(CollectionFailed, match="budget"):
        collector.collect(ss.agg_request("half-done"))
    collector.close()
    with pytest.raises(RestRevisionStoreError, match="no committed collection checkpoint"):
        store.ingest_collection(ss.agg_request("half-done"))
    assert h.rows(RESPONSES) == [] and h.venue.pending() == 0


def test_a_reused_request_id_with_other_content_is_refused(h: RestHarness) -> None:
    cs.queue_agg_chain(h.venue, SYMBOL, T0, [ss.agg_items(2)])
    _collected(h, ss.agg_request("req-a"))
    with pytest.raises(RestRevisionStoreConflict, match="different request content"):
        h.store(clock=StepClock(start=K1)).ingest_collection(ss.agg_request("req-a", minutes=7))
    assert h.rows(RESPONSES) == []


@pytest.mark.parametrize(
    ("label", "mutate"),
    [
        ("decoder summary", lambda d: d["accepted"].__setitem__("element_count", 2)),
        ("decoder binding", lambda d: d["decoder"].__setitem__("version", "1.0.1")),
        ("body sha", lambda d: d["body"].__setitem__("sha256", "0" * 64)),
        ("body key", lambda d: d["body"].__setitem__("key", d["body"]["key"] + "x")),
        ("http status", lambda d: d["http"].__setitem__("status", 206)),
        ("header", lambda d: d["http"]["metadata"].__setitem__("set-cookie", "a=b")),
        ("requested_at", lambda d: d.__setitem__("requested_at", d["retrieved_at"])),
        ("source uri", lambda d: d.__setitem__("source_uri", d["source_uri"] + "&x=1")),
        ("page identity", lambda d: d.__setitem__("page_identity_sha256", "f" * 64)),
    ],
)
def test_a_drifted_page_checkpoint_fails_closed_before_any_write(
    h: RestHarness, label: str, mutate: Any
) -> None:
    cs.queue_agg_chain(h.venue, SYMBOL, T0, [ss.agg_items(3)])
    _collected(h, ss.agg_request("req-a"))
    h.tamper_json(ss.page_checkpoint_key("req-a", 0), mutate)
    clock = StepClock(start=K1)
    with pytest.raises(RestCheckpointIntegrityError):
        h.store(clock=clock).ingest_collection(ss.agg_request("req-a"))
    assert h.rows(RESPONSES) == [] and clock.calls == 0


def test_a_tampered_body_or_collection_checkpoint_fails_closed(h: RestHarness) -> None:
    items = ss.agg_items(3)
    cs.queue_agg_chain(h.venue, SYMBOL, T0, [items])
    _collected(h, ss.agg_request("req-a"))
    doc = _page_doc(h, "req-a")
    body_path = h.object_path(doc["body"]["key"])
    original = body_path.read_bytes()
    body_path.write_bytes(original.replace(b"92792.05", b"92792.07"))
    with pytest.raises(RestCheckpointIntegrityError):
        h.store(clock=StepClock(start=K1)).ingest_collection(ss.agg_request("req-a"))
    body_path.write_bytes(original)
    h.tamper_json(
        f"{ss.checkpoint_root('req-a')}/collection.json",
        lambda d: d["result"]["gaps"][0].__setitem__("detail", "the market was idle"),
    )
    with pytest.raises(RestCheckpointIntegrityError):
        h.store(clock=StepClock(start=K1)).ingest_collection(ss.agg_request("req-a"))
    assert h.rows(RESPONSES) == []


# =========================================================================================
# #15 crash point 3: s1 / s2 recovery equals one uninterrupted run
# =========================================================================================


def _two_page_agg(h: RestHarness, request_id: str) -> Any:
    full = ss.agg_items(1000, first_id=100, first_ms=T0)
    tail = ss.agg_items(4, first_id=1100, first_ms=T0 + 1000)
    cs.queue_agg_chain(h.venue, SYMBOL, T0, [full, tail])
    request = ss.agg_request(request_id)
    _collected(h, request)
    return request


def _portable(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Rows without the warehouse URI (each harness has its own temporary warehouse)."""
    return sorted(
        ({key: value for key, value in row.items() if key != "object_uri"} for row in rows),
        key=lambda row: row["arrival_seq"],
    )


def _logical(h: RestHarness) -> dict[str, Any]:
    """Everything a recovery must reproduce: rows, and each table's batch sequence."""
    return {
        "responses": _portable(h.rows(RESPONSES)),
        "elements": _portable(h.rows(AGGS)),
        "response_batches": [item.batch_id for item in h.history(RESPONSES.table)],
        "element_batches": [item.batch_id for item in h.history(AGGS.table)],
    }


@pytest.mark.parametrize(
    ("label", "crash_table", "crash_after"),
    [
        ("before any commit (c3 -> s1)", RESPONSES.table, 0),
        ("after the first response (s1 -> s2)", RESPONSES.table, 1),
        ("inside the element microbatches", AGGS.table, 3),
        ("after page 0, before page 1's response", AGGS.table, 4),
        ("after page 1's response", RESPONSES.table, 2),
    ],
)
def test_recovery_after_a_crash_equals_one_uninterrupted_run(
    tmp_path: Path, label: str, crash_table: str, crash_after: int
) -> None:
    with ss.sqlite_harness(tmp_path / "once") as once:
        request = _two_page_agg(once, "req-crash")
        once.store(clock=StepClock(start=K1), element_microbatch_rows=250).ingest_collection(
            request
        )
        expected = _logical(once)

    with ss.sqlite_harness(tmp_path / "crash") as h:
        request = _two_page_agg(h, "req-crash")
        clock = StepClock(start=K1)
        proxy = ProxyCatalog(h.adapter)
        if crash_after == 0:

            def die(commit: CommitRequest) -> None:
                raise Crash("before the first commit")

            proxy.before = die
        else:
            proxy.after = ss.crash_after_commits(crash_after, table=crash_table)
        with pytest.raises(Crash):
            h.store(clock=clock, adapter=proxy, element_microbatch_rows=250).ingest_collection(
                request
            )
        if crash_after == 0:
            # Nothing was committed, so the lost reading never became a fact: a new one is lawful.
            clock = StepClock(start=K1)
        h.reopen()  # a restart: fresh adapter, same catalog
        recovered = h.store(clock=clock, element_microbatch_rows=250).ingest_collection(request)
        # Exactly one knowledge_time reading per committed response revision, ever.
        assert clock.calls == 2
        assert _logical(h) == expected, label
        again = h.store(clock=clock, element_microbatch_rows=250).ingest_collection(request)
        assert again.replayed and again.new_snapshot_ids == () and clock.calls == 2
        assert [page.arrival_seq_base for page in recovered.pages] == [BASE, BASE + STRIDE]


def test_changing_the_microbatch_plan_mid_recovery_fails_closed(h: RestHarness) -> None:
    request = _two_page_agg(h, "req-plan")
    proxy = ProxyCatalog(h.adapter, after=ss.crash_after_commits(2, table=AGGS.table))
    with pytest.raises(Crash):
        h.store(clock=StepClock(start=K1), adapter=proxy, element_microbatch_rows=250)\
            .ingest_collection(request)  # fmt: skip
    with pytest.raises(CatalogIntegrityError, match="partially committed|other content"):
        h.store(clock=StepClock(start=K1), element_microbatch_rows=300).ingest_collection(request)


# =========================================================================================
# #7: concurrency
# =========================================================================================


def test_a_concurrent_response_commit_never_shares_a_block(h: RestHarness) -> None:
    cs.queue_agg_chain(h.venue, SYMBOL, T0, [ss.agg_items(2)])
    cs.queue_kline_chain(h.venue, SYMBOL, T0, [ss.kline_items(2)])
    _collected(h, ss.agg_request("req-a"))
    _collected(h, ss.kline_request("req-k"))
    other = h.store(clock=StepClock(start=K1 + timedelta(hours=1)))
    fired: list[str] = []

    def interleave(commit: CommitRequest) -> None:
        if commit.table == RESPONSES.table and not fired:
            fired.append(commit.batch_id)
            other.ingest_collection(ss.kline_request("req-k"))  # wins the same block base

    proxy = ProxyCatalog(h.adapter, before=interleave)
    out = h.store(clock=StepClock(start=K1), adapter=proxy).ingest_collection(
        ss.agg_request("req-a")
    )

    seqs = sorted(row["arrival_seq"] for row in h.rows(RESPONSES))
    assert seqs == [BASE, BASE + STRIDE]
    assert out.pages[0].arrival_seq_base == BASE + STRIDE
    assert fired and fired[0].endswith(f".response.{BASE}")  # the lost base is never reused
    element_seqs = [row["arrival_seq"] for table in (AGGS, KLINES) for row in h.rows(table)]
    assert len(element_seqs) == len(set(element_seqs)) == 4


def test_the_same_revision_raced_by_two_stores_is_committed_once(h: RestHarness) -> None:
    cs.queue_agg_chain(h.venue, SYMBOL, T0, [ss.agg_items(3)])
    request = ss.agg_request("req-a")
    _collected(h, request)
    rival = h.store(clock=StepClock(start=K1 + timedelta(minutes=5)))
    fired: list[bool] = []

    def interleave(commit: CommitRequest) -> None:
        if not fired:
            fired.append(True)
            rival.ingest_collection(request)

    proxy = ProxyCatalog(h.adapter, before=interleave)
    out = h.store(clock=StepClock(start=K1), adapter=proxy).ingest_collection(request)

    [row] = h.rows(RESPONSES)
    assert row["knowledge_time"] == K1 + timedelta(minutes=5)  # the winner's reading is kept
    assert out.pages[0].knowledge_time == row["knowledge_time"]
    assert out.replayed
    elements = h.rows(AGGS)
    assert len(elements) == 3 and len({item["revision_id"] for item in elements}) == 3
    assert {item["knowledge_time"] for item in elements} == {row["knowledge_time"]}


def test_commit_races_are_bounded(h: RestHarness) -> None:
    cs.queue_agg_chain(h.venue, SYMBOL, T0, [ss.agg_items(2)])
    _collected(h, ss.agg_request("req-a"))

    def always_lose(commit: CommitRequest) -> None:
        raise CommitConflict("head moved")

    proxy = ProxyCatalog(h.adapter, before=always_lose)
    with pytest.raises(RestRevisionStoreConflict, match="after 8 attempts"):
        h.store(clock=StepClock(start=K1), adapter=proxy).ingest_collection(ss.agg_request("req-a"))
    assert h.rows(RESPONSES) == []


# =========================================================================================
# #8: committed catalog content that does not reproduce
# =========================================================================================


def _stored_once(h: RestHarness, **kwargs: Any) -> RestCollectionStored:
    cs.queue_agg_chain(h.venue, SYMBOL, T0, [ss.agg_items(3)])
    _collected(h, ss.agg_request("req-a"))
    return h.store(clock=StepClock(start=K1), **kwargs).ingest_collection(ss.agg_request("req-a"))


@pytest.mark.parametrize(
    ("column", "value"),
    [
        ("knowledge_time", K1 + timedelta(days=1)),
        ("object_size_bytes", 1),
        ("decoder_hash", "0" * 64),
    ],
)
def test_a_duplicated_response_revision_fails_closed(
    h: RestHarness, column: str, value: Any
) -> None:
    _stored_once(h)
    [row] = h.rows(RESPONSES)
    forged = dict(row)
    forged[column] = value
    h.forge_rows(RESPONSES, [forged], "forged-response")
    with pytest.raises(CatalogIntegrityError, match="committed twice"):
        h.store(clock=StepClock(start=K1)).ingest_collection(ss.agg_request("req-a"))


@pytest.mark.parametrize(
    ("column", "value"),
    [
        ("knowledge_time", K1 + timedelta(days=1)),
        ("page_index", 3),
        ("object_uri", "file:///elsewhere.json"),
        ("availability_evidence_gap", "made up"),
        ("event_end_time", utc(2023, 11, 25)),
        ("source_metadata", []),
    ],
)
def test_a_response_row_that_drifts_in_place_fails_closed(
    h: RestHarness, column: str, value: Any
) -> None:
    """The committed row is replaced (catalog corruption) by a twin differing in one field."""
    cs.queue_agg_chain(h.venue, SYMBOL, T0, [ss.agg_items(3)])
    _collected(h, ss.agg_request("req-a"))
    proxy = ProxyCatalog(h.adapter, after=ss.crash_after_commits(1))
    with pytest.raises(Crash):
        h.store(clock=StepClock(start=K1), adapter=proxy).ingest_collection(ss.agg_request("req-a"))
    [row] = h.rows(RESPONSES)
    forged = dict(row)
    forged[column] = value
    h.overwrite_rows(RESPONSES, [forged], batch_id="corruption")
    assert h.rows(RESPONSES) == [forged]
    with pytest.raises(CatalogIntegrityError):
        h.store(clock=StepClock(start=K1)).ingest_collection(ss.agg_request("req-a"))
    assert h.rows(AGGS) == []


@pytest.mark.parametrize(
    ("column", "value", "match"),
    [
        ("payload_hash", "0" * 64, "re-derive"),
        ("source_id", "binance.public.spot.archive@1.0.0", "re-derive"),
        ("price", Decimal("1.5"), "re-derive"),
        ("event_time", utc(2023, 11, 14, 22, 15), "times"),
        ("arrival_seq", 7, "not lawful"),
    ],
)
def test_a_forged_element_row_fails_closed(
    h: RestHarness, column: str, value: Any, match: str
) -> None:
    _stored_once(h)
    row = sorted(h.rows(AGGS), key=lambda item: item["element_index"])[0]
    forged = dict(row)
    forged[column] = value
    forged["arrival_seq"] = value if column == "arrival_seq" else BASE + 999
    h.forge_rows(AGGS, [forged], "forged-element")
    with pytest.raises(CatalogIntegrityError, match=match):
        h.store(clock=StepClock(start=K1)).ingest_collection(ss.agg_request("req-a"))


@pytest.mark.parametrize("kind", ["revision", "arrival"])
def test_a_duplicated_element_revision_or_arrival_number_fails_closed(
    h: RestHarness, kind: str
) -> None:
    _stored_once(h)
    rows = sorted(h.rows(AGGS), key=lambda item: item["element_index"])
    if kind == "revision":
        forged = dict(rows[0])
        forged["arrival_seq"] = BASE + 500
        match = "committed twice"
    else:
        # A lawful competing revision of element 1 that reuses element 0's arrival number.
        forged = dict(rows[1])
        forged["quantity"] = Decimal("0.500000000000000000")  # as decimal(38, 18) stores it
        forged["payload_hash"] = rest_identity.agg_trade_payload_hash(SYMBOL, forged)
        forged["revision_id"] = rest_identity.revision_id(
            forged["observation_key"], forged["source_id"], forged["payload_hash"]
        )
        forged["arrival_seq"] = rows[0]["arrival_seq"]
        match = "not unique"
    h.forge_rows(AGGS, [forged], "forged-duplicate")
    with pytest.raises(CatalogIntegrityError, match=match):
        h.store(clock=StepClock(start=K1)).ingest_collection(ss.agg_request("req-a"))


@pytest.mark.parametrize(
    ("drift", "match"),
    [
        # Not what the lineage response released: caught before any re-decode comparison.
        (
            {"knowledge_time": K1 + timedelta(hours=1)},
            r"does not inherit its lineage response .*\['knowledge_time'\]",
        ),
        # Lawful under its lineage (index 2 of 3, arrival base + 3) but not where the re-decoded
        # page puts this element: only the owned re-decode comparison can see it.
        (
            {"element_index": 2, "arrival_seq": BASE + 3},
            r"disagrees with its re-decoded element: \['arrival_seq', 'element_index'\]",
        ),
    ],
)
def test_an_owned_element_that_drifts_from_its_re_decode_fails_closed(
    h: RestHarness, drift: dict[str, Any], match: str
) -> None:
    cs.queue_agg_chain(h.venue, SYMBOL, T0, [ss.agg_items(3)])
    _collected(h, ss.agg_request("req-a"))
    proxy = ProxyCatalog(h.adapter, after=ss.crash_after_commits(1, table=RESPONSES.table))
    with pytest.raises(Crash):
        h.store(clock=StepClock(start=K1), adapter=proxy).ingest_collection(ss.agg_request("req-a"))
    # A writer commits element 0 under our lineage but not as the re-decoded page has it — as
    # our lineage's first element batch, so the D3E-R2 batch check (fingerprint, row count,
    # one microbatch plan) holds and only the comparisons named above can see the drift.
    out_rows = _expected_element_rows(tmp_path=h.tmp_path)
    forged = dict(out_rows[0])
    forged.update(drift)
    [response] = h.rows(RESPONSES)
    h.forge_snapshot(AGGS, [forged], batch_id=f"{response['revision_id']}.elements.00000000")
    heads = (h.head(RESPONSES.table), h.head(AGGS.table))
    with pytest.raises(CatalogIntegrityError, match=match):
        h.store(clock=StepClock(start=K1)).ingest_collection(ss.agg_request("req-a"))
    assert (h.head(RESPONSES.table), h.head(AGGS.table)) == heads
    assert h.rows(AGGS) == [forged]


def _expected_element_rows(*, tmp_path: Path) -> list[dict[str, Any]]:
    """The element rows an uninterrupted run of the same page produces (independent catalog)."""
    with ss.sqlite_harness(tmp_path / "reference") as ref:
        cs.queue_agg_chain(ref.venue, SYMBOL, T0, [ss.agg_items(3)])
        _collected(ref, ss.agg_request("req-a"))
        ref.store(clock=StepClock(start=K1)).ingest_collection(ss.agg_request("req-a"))
        return sorted(ref.rows(AGGS), key=lambda item: item["element_index"])


def test_an_element_batch_id_taken_by_other_content_fails_closed(h: RestHarness) -> None:
    cs.queue_agg_chain(h.venue, SYMBOL, T0, [ss.agg_items(3)])
    _collected(h, ss.agg_request("req-a"))
    proxy = ProxyCatalog(h.adapter, after=ss.crash_after_commits(1, table=RESPONSES.table))
    with pytest.raises(Crash):
        h.store(clock=StepClock(start=K1), adapter=proxy).ingest_collection(ss.agg_request("req-a"))
    [response] = h.rows(RESPONSES)
    # Some other element row (another key) was committed under our first batch id.
    unrelated = dict(_expected_element_rows(tmp_path=h.tmp_path)[0])
    unrelated["agg_trade_id"] = 999
    unrelated["observation_key"] = f"binance:spot:agg_trade:{SYMBOL}:999"
    h.forge_rows(AGGS, [unrelated], f"{response['revision_id']}.elements.00000000")
    with pytest.raises(CatalogIntegrityError, match="other content"):
        h.store(clock=StepClock(start=K1)).ingest_collection(ss.agg_request("req-a"))


def test_a_response_batch_committed_twice_fails_closed(h: RestHarness) -> None:
    _stored_once(h)
    [row] = h.rows(RESPONSES)
    other = dict(row)
    other["observation_key"] = "binance:spot:rest:" + "e" * 64
    other["revision_id"] = rest_identity.revision_id(
        other["observation_key"], other["source_id"], other["payload_hash"]
    )
    other["arrival_seq"] = BASE + 5 * STRIDE
    h.forge_snapshot(RESPONSES, [other], batch_id=f"{row['revision_id']}.response.{BASE}")
    with pytest.raises(CatalogIntegrityError, match="2 snapshots"):
        h.store(clock=StepClock(start=K1)).ingest_collection(ss.agg_request("req-a"))


# =========================================================================================
# #9 / #21: REST arrival blocks
# =========================================================================================


def _anchor_row(h: RestHarness, arrival_seq: int, tag: str) -> dict[str, Any]:
    """A lawful foreign response row (another page identity) holding ``arrival_seq``."""
    query = RestPageQuery.klines_from_start("ETHUSDT", T0 - 60 * MINUTE_MS)
    identity = rest_identity.page_identity_sha256(query, ORIGIN)
    body = hashlib.sha256(tag.encode()).hexdigest()
    with ss.sqlite_harness(h.tmp_path / f"anchor-{tag}") as other:
        cs.queue_kline_chain(other.venue, "ETHUSDT", T0 - 60 * MINUTE_MS, [[]])
        request = cs.kline_request(
            request_id=f"anchor-{tag}",
            symbols=("ETHUSDT",),
            start_ms=T0 - 60 * MINUTE_MS,
            end_ms=T0 - 55 * MINUTE_MS,
        )
        other.collect(request)
        other.store(clock=StepClock(start=K1)).ingest_collection(request)
        [row] = other.rows(RESPONSES)
    assert row["page_identity_sha256"] == identity
    row["payload_hash"] = row["object_sha256"] = body
    row["revision_id"] = rest_identity.revision_id(row["observation_key"], row["source_id"], body)
    row["arrival_seq"] = arrival_seq
    return row


def test_the_last_legal_block_is_used_and_the_next_allocation_fails_closed(
    h: RestHarness,
) -> None:
    last_base = (1 << 63) - STRIDE
    h.forge_rows(RESPONSES, [_anchor_row(h, last_base - STRIDE, "a")], "anchor")
    cs.queue_agg_chain(h.venue, SYMBOL, T0, [ss.agg_items(3)])
    cs.queue_kline_chain(h.venue, SYMBOL, T0, [ss.kline_items(1)])
    _collected(h, ss.agg_request("req-a"))
    _collected(h, ss.kline_request("req-k"))
    store = h.store(clock=StepClock(start=K1))

    out = store.ingest_collection(ss.agg_request("req-a"))

    assert out.pages[0].arrival_seq_base == last_base
    seqs = sorted(row["arrival_seq"] for row in h.rows(AGGS))
    assert seqs == [last_base + 1, last_base + 2, last_base + 3]
    assert all(seq <= (1 << 63) - 1 for seq in seqs)
    with pytest.raises(RestRevisionStoreError, match="exhausted"):
        store.ingest_collection(ss.kline_request("req-k"))
    assert h.rows(KLINES) == []


@pytest.mark.parametrize("anchor", [BASE + 5, BASE - STRIDE, 17])
def test_a_corrupt_arrival_anchor_fails_closed(h: RestHarness, anchor: int) -> None:
    h.forge_rows(RESPONSES, [_anchor_row(h, anchor, "c")], "anchor")
    cs.queue_agg_chain(h.venue, SYMBOL, T0, [ss.agg_items(2)])
    _collected(h, ss.agg_request("req-a"))
    with pytest.raises(CatalogIntegrityError):
        h.store(clock=StepClock(start=K1)).ingest_collection(ss.agg_request("req-a"))
    assert h.rows(AGGS) == []


def test_element_index_outside_one_block_is_unrepresentable() -> None:
    with pytest.raises(rest_identity.RestArrivalSeqOverflow):
        rest_identity.element_arrival_seq(BASE, 1000)
    assert rest_identity.element_arrival_seq(BASE, 999) == BASE + 1000


# =========================================================================================
# #20: the knowledge clock
# =========================================================================================


def test_a_clock_behind_ingest_time_or_naive_is_refused(h: RestHarness) -> None:
    cs.queue_agg_chain(h.venue, SYMBOL, T0, [ss.agg_items(2)])
    _collected(h, ss.agg_request("req-a"))
    with pytest.raises(RestRevisionStoreConflict, match="precede ingest_time"):
        h.store(clock=StepClock(start=utc(2023, 11, 1))).ingest_collection(ss.agg_request("req-a"))
    naive = StepClock(start=K1)
    with pytest.raises(RestRevisionStoreError, match="timezone-aware"):
        h.store(clock=lambda: naive().replace(tzinfo=None)).ingest_collection(
            ss.agg_request("req-a")
        )
    assert h.rows(RESPONSES) == []


def test_a_page_first_rejected_elsewhere_cannot_lend_its_knowledge_time(
    h: RestHarness,
) -> None:
    # Chain A: page 1 (fromId=1100) follows a page whose last T is small → accepted.
    # Chain B: the same page 1 bytes follow a page whose last T is larger → rejected
    # (continuity). B is stored first, so the page's response revision is "rejected".
    page0_a = ss.agg_items(1000, first_id=100, first_ms=T0)
    page0_b = ss.agg_items(1000, first_id=100, first_ms=T0 + 5)
    page1 = ss.agg_items(2, first_id=1100, first_ms=T0 + 1000)
    cs.queue_agg_chain(h.venue, SYMBOL, T0 + 5, [page0_b, page1])
    cs.queue_agg_chain(h.venue, SYMBOL, T0, [page0_a, page1])
    request_b = ss.agg_request("req-b", start_ms=T0 + 5)
    assert isinstance(h.collect(request_b), CollectionFailed)
    _collected(h, ss.agg_request("req-a"), start_ms=cs.RETRIEVED_AT_MS + 60_000)
    store = h.store(clock=StepClock(start=K1))
    out_b = store.ingest_collection(request_b)
    assert out_b.pages[1].decode_outcome == "rejected"

    with pytest.raises(RestRevisionStoreConflict, match="would backfill"):
        store.ingest_collection(ss.agg_request("req-a"))
    assert not [row for row in h.rows(AGGS) if row["agg_trade_id"] >= 1100]


def test_findings_and_rows_are_deterministic_across_independent_runs(tmp_path: Path) -> None:
    outcomes = []
    for name in ("one", "two"):
        with ss.sqlite_harness(tmp_path / name) as h:
            cs.queue_agg_chain(h.venue, SYMBOL, T0, [[ss.agg_item(100, T0), ss.agg_item(104, T0)]])
            _collected(h, ss.agg_request("req-a"))
            out = h.store(clock=StepClock(start=K1)).ingest_collection(ss.agg_request("req-a"))
            outcomes.append((out.findings, _portable(h.rows(RESPONSES)), _portable(h.rows(AGGS))))
    assert outcomes[0] == outcomes[1]
    assert outcomes[0][0][0].code == "rest_agg_trade_id_gap"


def test_committed_outcomes_report_commit_outcomes(h: RestHarness) -> None:
    out = _stored_once(h)
    assert out.pages[0].response_commit.outcome is CommitOutcome.COMMITTED
    assert [commit.outcome for commit in out.pages[0].element_commits] == [CommitOutcome.COMMITTED]
    assert len(out.new_snapshot_ids) == 2 and not out.replayed


# =========================================================================================
# #8 (D3E-R1): every committed row the store adopts, compares or reports is proven lawful
# =========================================================================================

K2 = K1 + timedelta(days=1)
FORGED_ID = "rev1-" + "9" * 64
PRECEDENCE_ITEM = {
    "superseded_revision_id": "rev1-" + "a" * 64,
    "policy_id": "binance.spot.delivery-channel",
    "policy_version": "1.0.0",
    "policy_hash": "b" * 64,
    "evidence": ["made up"],
    "knowledge_time": K1,
}


def _tables_state(h: RestHarness) -> dict[str, Any]:
    """Heads and row counts of every table the store or a reconciler could write."""
    tables = (RESPONSES, AGGS, KLINES, BINANCE_SPOT_PRECEDENCE_EVIDENCE)
    return {
        definition.table: (h.head(definition.table), len(h.rows(definition)))
        for definition in tables
    }


def _drifted(row: dict[str, Any], drift: dict[str, Any]) -> dict[str, Any]:
    forged = dict(row)
    for column, value in drift.items():
        forged[column] = value(row) if callable(value) else value
    return forged


@dataclass
class _CaptureCatalog(ProxyCatalog):
    """Records the batch the store is about to commit, then dies before committing it."""

    captured: list[dict[str, Any]] = field(default_factory=list)

    def commit_batch(self, request: CommitRequest, batch: Any) -> Any:
        self.captured.extend(batch.to_pylist())
        raise Crash(f"captured {request.batch_id}")


def _captured_response(h: RestHarness, request: Any, *, clock_start: datetime) -> dict[str, Any]:
    """The exact response row the store would commit for ``request`` (nothing is committed)."""
    capture = _CaptureCatalog(h.adapter)
    with pytest.raises(Crash):
        h.store(clock=StepClock(start=clock_start), adapter=capture).ingest_collection(request)
    [row] = capture.captured
    return row


def _response_batch(row: dict[str, Any]) -> str:
    return f"{row['revision_id']}.response.{row['arrival_seq']}"


# ------------------------------------------------------------------ Codex counterexample A


def test_codex_a_a_foreign_element_with_an_orphan_lineage_is_refused(h: RestHarness) -> None:
    """Same payload / revision id, lineage ``rev1-999…`` and a knowledge_time one hour late."""
    cs.queue_agg_chain(h.venue, SYMBOL, T0, [ss.agg_items(1)])
    _collected(h, ss.agg_request("req-a"))
    [reference] = _expected_element_rows(tmp_path=h.tmp_path)[:1]
    forged = _drifted(
        reference,
        {
            "response_revision_id": FORGED_ID,
            "knowledge_time": lambda r: r["knowledge_time"] + timedelta(hours=1),
        },
    )
    h.forge_rows(AGGS, [forged], "hostile-foreign-element")
    element_head = h.head(AGGS.table)

    with pytest.raises(
        CatalogIntegrityError, match=f"lineage response revision {FORGED_ID} is committed 0 time"
    ):
        h.store(clock=StepClock(start=K2)).ingest_collection(ss.agg_request("req-a"))

    # s1 (this page's own, lawful response revision) is committed; no element is, and the
    # forged row is neither adopted nor repaired.
    assert h.head(AGGS.table) == element_head and h.rows(AGGS) == [forged]
    [response] = h.rows(RESPONSES)
    assert response["knowledge_time"] == K2
    assert h.rows(BINANCE_SPOT_PRECEDENCE_EVIDENCE) == []


# ------------------------------------------------------------------ foreign element lineage


def _overlap(h: RestHarness) -> tuple[RestCollectionStored, Any]:
    """Page A holds 100..102; page B (not yet stored) holds 101..103 — 101 and 102 overlap."""
    cs.queue_agg_chain(h.venue, SYMBOL, T0, [ss.agg_items(3, first_id=100, first_ms=T0)])
    cs.queue_agg_chain(h.venue, SYMBOL, T0 + 1, [ss.agg_items(3, first_id=101, first_ms=T0 + 1)])
    _collected(h, ss.agg_request("req-a"))
    request_b = ss.agg_request("req-b", start_ms=T0 + 1)
    _collected(h, request_b)
    first = h.store(clock=StepClock(start=K1)).ingest_collection(ss.agg_request("req-a"))
    return first, request_b


def _replace_element(h: RestHarness, agg_trade_id: int, forged: dict[str, Any]) -> None:
    rows = [row for row in h.rows(AGGS) if row["agg_trade_id"] != agg_trade_id]
    h.overwrite_rows(AGGS, [*rows, forged], batch_id="corruption")


@pytest.mark.parametrize(
    ("drift", "match"),
    [
        ({"response_revision_id": FORGED_ID}, "committed 0 time"),
        ({"arrival_seq": BASE + 7}, r"\['arrival_seq'\]"),
        ({"element_index": 5, "arrival_seq": BASE + 6}, "outside the 3 element"),
        ({"element_index": -1, "arrival_seq": BASE}, "outside the 3 element"),
        (
            {"knowledge_time": lambda r: r["knowledge_time"] + timedelta(hours=1)},
            r"\['knowledge_time'\]",
        ),
        (
            {
                "ingest_time": lambda r: r["ingest_time"] - timedelta(seconds=1),
                "available_time": lambda r: r["available_time"] - timedelta(seconds=1),
            },
            r"\['available_time', 'ingest_time'\]",
        ),
        ({"availability_policy_hash": "0" * 64}, r"\['availability_policy_hash'\]"),
        ({"availability_policy_version": "9.9.9"}, r"\['availability_policy_version'\]"),
        ({"availability_evidence_gap": "made up"}, r"\['availability_evidence_gap'\]"),
        ({"availability_evidence": ["made up"]}, r"\['availability_evidence'\]"),
        ({"declared_latency_us": 5}, r"\['declared_latency_us'\]"),
        ({"decoder_hash": "0" * 64}, r"\['decoder_hash'\]"),
        ({"decoder_version": "9.9.9"}, r"\['decoder_version'\]"),
        ({"contract_schema_version": "9.9.9"}, r"\['contract_schema_version'\]"),
        ({"supersedes": ["rev1-" + "a" * 64]}, r"\['supersedes'\]"),
        ({"precedence_evidence": [PRECEDENCE_ITEM]}, r"\['precedence_evidence'\]"),
        ({"source_revision_id": "venue-7"}, r"\['source_revision_id'\]"),
    ],
)
def test_a_foreign_element_whose_lineage_does_not_hold_is_refused(
    h: RestHarness, drift: dict[str, Any], match: str
) -> None:
    first, request_b = _overlap(h)
    shared = next(row for row in h.rows(AGGS) if row["agg_trade_id"] == 101)
    forged = _drifted(shared, drift)
    _replace_element(h, 101, forged)
    before = _tables_state(h)

    with pytest.raises(CatalogIntegrityError, match=match):
        h.store(clock=StepClock(start=K2)).ingest_collection(request_b)

    after = _tables_state(h)
    assert after[AGGS.table] == before[AGGS.table]  # no element of page B was written
    assert after[BINANCE_SPOT_PRECEDENCE_EVIDENCE.table] == (None, 0)
    assert after[RESPONSES.table][1] == before[RESPONSES.table][1] + 1  # only B's own s1 row
    assert forged in h.rows(AGGS)


@pytest.mark.parametrize(
    ("drift", "match"),
    [
        ({"availability_policy_hash": "0" * 64}, r"\['availability_policy_hash'\]"),
        (
            {"knowledge_time": lambda r: r["ingest_time"] - timedelta(hours=1)},
            "knowledge_time must not precede ingest_time",
        ),
        ({"decoder_hash": "0" * 64}, r"\['decoder_hash'\]"),
    ],
)
def test_a_foreign_element_whose_lineage_response_is_not_lawful_is_refused(
    h: RestHarness, drift: dict[str, Any], match: str
) -> None:
    """The lineage response itself (another page identity than B's) drifted in place."""
    first, request_b = _overlap(h)
    [response_a] = h.rows(RESPONSES)
    h.overwrite_rows(RESPONSES, [_drifted(response_a, drift)], batch_id="corruption")
    before = _tables_state(h)

    with pytest.raises(CatalogIntegrityError, match=match):
        h.store(clock=StepClock(start=K2)).ingest_collection(request_b)

    after = _tables_state(h)
    assert after[AGGS.table] == before[AGGS.table]
    assert after[RESPONSES.table][1] == before[RESPONSES.table][1] + 1
    assert after[BINANCE_SPOT_PRECEDENCE_EVIDENCE.table] == (None, 0)  # evidence stays silent


@pytest.mark.parametrize("lineage", ["rejected-page", "kline-page"])
def test_a_foreign_element_naming_a_response_that_released_no_such_element_is_refused(
    h: RestHarness, lineage: str
) -> None:
    """The lineage response is lawful, but it did not accept an aggTrades page of BTCUSDT."""
    first, request_b = _overlap(h)
    if lineage == "rejected-page":
        bad = [ss.agg_item(900, T0 + 50, price="0.00000000")]
        cs.queue_agg_chain(h.venue, SYMBOL, T0 + 50, [bad])
        request = ss.agg_request("req-bad", start_ms=T0 + 50)
        assert isinstance(h.collect(request), CollectionFailed)
    else:
        cs.queue_kline_chain(h.venue, SYMBOL, T0, [ss.kline_items(1)])
        request = ss.kline_request("req-k")
        _collected(h, request)
    h.store(clock=StepClock(start=K1)).ingest_collection(request)
    [response] = [
        row for row in h.rows(RESPONSES) if row["collection_request_id"] == request.request_id
    ]
    shared = next(row for row in h.rows(AGGS) if row["agg_trade_id"] == 101)
    forged = _drifted(
        shared,
        {
            "response_revision_id": response["revision_id"],
            "arrival_seq": response["arrival_seq"] + 1 + 1,
            "ingest_time": response["ingest_time"],
            "available_time": response["ingest_time"],
            "knowledge_time": response["knowledge_time"],
        },
    )
    _replace_element(h, 101, forged)
    before = _tables_state(h)

    with pytest.raises(CatalogIntegrityError, match="did not accept a agg_trades page of BTCUSDT"):
        h.store(clock=StepClock(start=K2)).ingest_collection(request_b)
    after = _tables_state(h)
    assert after[AGGS.table] == before[AGGS.table]
    assert after[BINANCE_SPOT_PRECEDENCE_EVIDENCE.table] == (None, 0)


def test_a_lawful_foreign_lineage_is_adopted_and_replays_idempotently(h: RestHarness) -> None:
    first, request_b = _overlap(h)
    store = h.store(clock=StepClock(start=K2))
    second = store.ingest_collection(request_b)
    state = _tables_state(h)

    again = store.ingest_collection(request_b)

    assert again.replayed and _tables_state(h) == state
    by_id = _by(h.rows(AGGS), "revision_id")
    for shared in first.pages[0].element_revision_ids[1:]:
        assert by_id[shared]["response_revision_id"] == first.pages[0].response_revision_id
        assert by_id[shared]["knowledge_time"] == K1
    assert second.findings == again.findings == ()


def test_a_competing_element_revision_must_be_lawful_to_become_a_finding(h: RestHarness) -> None:
    """Page B disagrees on element 101; A's committed 101 drifted, so no finding is reported."""
    cs.queue_agg_chain(h.venue, SYMBOL, T0, [ss.agg_items(3, first_id=100, first_ms=T0)])
    b_items = ss.agg_items(3, first_id=101, first_ms=T0 + 1)
    b_items[0]["q"] = "0.00250000"
    cs.queue_agg_chain(h.venue, SYMBOL, T0 + 1, [b_items])
    _collected(h, ss.agg_request("req-a"))
    request_b = ss.agg_request("req-b", start_ms=T0 + 1)
    _collected(h, request_b)
    h.store(clock=StepClock(start=K1)).ingest_collection(ss.agg_request("req-a"))
    shared = next(row for row in h.rows(AGGS) if row["agg_trade_id"] == 101)
    _replace_element(h, 101, _drifted(shared, {"knowledge_time": K1 + timedelta(hours=1)}))
    element_state = _tables_state(h)[AGGS.table]

    with pytest.raises(CatalogIntegrityError, match=r"\['knowledge_time'\]"):
        h.store(clock=StepClock(start=K2)).ingest_collection(request_b)
    after = _tables_state(h)
    assert after[AGGS.table] == element_state
    assert after[BINANCE_SPOT_PRECEDENCE_EVIDENCE.table] == (None, 0)


# ------------------------------------------------------------------ competing response revisions


def _competing_pages(h: RestHarness) -> tuple[Any, Any]:
    """req-a and req-b ask the same page identity and are answered with different bytes."""
    items = ss.agg_items(3)
    changed = [dict(item) for item in items]
    changed[1]["p"] = "92792.06000000"
    cs.queue_agg_chain(h.venue, SYMBOL, T0, [items])
    cs.queue_agg_chain(h.venue, SYMBOL, T0, [changed])
    request_a, request_b = ss.agg_request("req-a"), ss.agg_request("req-b")
    _collected(h, request_a)
    _collected(h, request_b, start_ms=cs.RETRIEVED_AT_MS + 60_000)
    return request_a, request_b


def _other_page_response(h: RestHarness) -> dict[str, Any]:
    """A lawful response row of another page identity (klines), not committed."""
    cs.queue_kline_chain(h.venue, SYMBOL, T0, [ss.kline_items(1)])
    request = ss.kline_request("req-k")
    _collected(h, request)
    return _captured_response(h, request, clock_start=K1)


def test_codex_b_a_competing_response_with_time_and_policy_drift_is_refused(
    h: RestHarness,
) -> None:
    request_a, request_b = _competing_pages(h)
    competitor = _captured_response(h, request_a, clock_start=K1)
    forged = _drifted(
        competitor,
        {
            "knowledge_time": lambda r: r["ingest_time"] - timedelta(hours=1),
            "availability_policy_hash": "0" * 64,
        },
    )
    h.forge_rows(RESPONSES, [forged], _response_batch(forged))
    before = _tables_state(h)

    with pytest.raises(CatalogIntegrityError, match="knowledge_time must not precede"):
        h.store(clock=StepClock(start=K2)).ingest_collection(request_b)

    assert _tables_state(h) == before  # nothing at all: s1 is refused before any commit
    assert h.rows(RESPONSES) == [forged]


@pytest.mark.parametrize(
    ("drift", "match"),
    [
        (
            {
                "requested_at": lambda r: r["retrieved_at"] + timedelta(seconds=1),
                "event_time": lambda r: r["retrieved_at"] + timedelta(seconds=1),
            },
            "requested_at must precede ingest_time",
        ),
        ({"event_end_time": lambda r: r["retrieved_at"] - timedelta(seconds=1)}, "event_end_time"),
        ({"ingest_time": lambda r: r["ingest_time"] - timedelta(seconds=1)}, r"\['ingest_time'\]"),
        ({"available_time": lambda r: r["knowledge_time"]}, r"\['available_time'\]"),
        ({"availability_policy_version": "9.9.9"}, r"\['availability_policy_version'\]"),
        ({"availability_evidence_gap": "made up"}, r"\['availability_evidence_gap'\]"),
        ({"decoder_hash": "0" * 64}, r"\['decoder_hash'\]"),
        ({"decoder_id": "some.other.decoder"}, r"\['decoder_id'\]"),
        ({"collector_version": "9.9.9"}, "another collector"),
        ({"source_binding_version": "2.0.0"}, r"\['source_binding_version'\]"),
        ({"source_uri": "https://elsewhere.example/api/v3/aggTrades"}, r"\['source_uri'\]"),
        ({"http_status": 206}, r"\['http_status'\]"),
        ({"object_size_bytes": 1}, r"\['object_size_bytes'\]"),
        ({"object_uri": "file:///elsewhere.json"}, r"\['object_uri'\]"),
        ({"page_limit": 500}, r"\['page_limit'\]"),
        # Another query derives another page identity, hence another body key: nothing is there.
        ({"request_query": "limit=1000&startTime=1&symbol=BTCUSDT"}, "is not published"),
        ({"page_identity_sha256": "c" * 64}, r"\['page_identity_sha256'\]"),
        ({"symbol": "ETHUSDT"}, r"\['symbol'\]"),
        ({"contract_schema_version": "9.9.9"}, r"\['contract_schema_version'\]"),
        ({"supersedes": ["rev1-" + "a" * 64]}, r"\['supersedes'\]"),
        ({"precedence_evidence": [PRECEDENCE_ITEM]}, r"\['precedence_evidence'\]"),
        (
            {"source_metadata": [{"name": "set-cookie", "value": "x"}]},
            "header allowlist",
        ),
        ({"decode_rejection_code": "made_up"}, "a count and no rejection code"),
        ({"element_count": 1001}, "more elements than one page"),
        ({"page_index": -1}, "page_index"),
        ({"collection_request_id": ""}, "collection request id"),
        ({"arrival_seq": BASE + 5}, "not a block base"),
    ],
)
def test_a_competing_response_revision_must_be_lawful(
    h: RestHarness, drift: dict[str, Any], match: str
) -> None:
    request_a, request_b = _competing_pages(h)
    forged = _drifted(_captured_response(h, request_a, clock_start=K1), drift)
    h.forge_rows(RESPONSES, [forged], _response_batch(forged))
    before = _tables_state(h)

    with pytest.raises(CatalogIntegrityError, match=match):
        h.store(clock=StepClock(start=K2)).ingest_collection(request_b)

    assert _tables_state(h) == before
    assert h.rows(RESPONSES) == [forged]


def test_a_competing_response_sharing_an_arrival_block_is_refused(h: RestHarness) -> None:
    request_a, request_b = _competing_pages(h)
    competitor = _captured_response(h, request_a, clock_start=K1)
    other = _other_page_response(h)
    assert competitor["arrival_seq"] == other["arrival_seq"] == BASE
    h.forge_rows(RESPONSES, [competitor], _response_batch(competitor))
    h.forge_rows(RESPONSES, [other], _response_batch(other))
    before = _tables_state(h)

    with pytest.raises(CatalogIntegrityError, match=f"block {BASE} is not held by"):
        h.store(clock=StepClock(start=K2)).ingest_collection(request_b)
    assert _tables_state(h) == before


@pytest.mark.parametrize("corruption", ["foreign-batch-id", "fingerprint", "row-count"])
def test_a_competing_response_batch_must_be_its_own_exact_snapshot(
    h: RestHarness, corruption: str
) -> None:
    request_a, request_b = _competing_pages(h)
    competitor = _captured_response(h, request_a, clock_start=K1)
    if corruption == "foreign-batch-id":
        h.forge_rows(RESPONSES, [competitor], "hostile-batch")
        match = "0 snapshots"
    elif corruption == "fingerprint":
        h.forge_snapshot(
            RESPONSES, [competitor], batch_id=_response_batch(competitor), fingerprint="0" * 64
        )
        match = "committed with other content"
    else:
        other = _drifted(_other_page_response(h), {"arrival_seq": BASE + 9 * STRIDE})
        h.forge_snapshot(RESPONSES, [competitor, other], batch_id=_response_batch(competitor))
        match = "committed with other content"
    before = _tables_state(h)

    with pytest.raises(CatalogIntegrityError, match=match):
        h.store(clock=StepClock(start=K2)).ingest_collection(request_b)
    assert _tables_state(h) == before


def test_a_lawful_competing_response_still_yields_the_finding(h: RestHarness) -> None:
    request_a, request_b = _competing_pages(h)
    competitor = _captured_response(h, request_a, clock_start=K1)
    h.forge_rows(RESPONSES, [competitor], _response_batch(competitor))

    out = h.store(clock=StepClock(start=K2)).ingest_collection(request_b)

    [finding] = [item for item in out.findings if item.code == FINDING_RESPONSE_COMPETING]
    assert finding.related_revision_ids == (competitor["revision_id"],)
    responses = h.rows(RESPONSES)
    assert len(responses) == 2 and all(row["supersedes"] == [] for row in responses)
    assert competitor in responses
    assert out.pages[0].arrival_seq_base == BASE + STRIDE
    assert h.rows(BINANCE_SPOT_PRECEDENCE_EVIDENCE) == []


# ------------------------------------------------------------------ cross-table pinned reads


@dataclass
class _ScanHook(ProxyCatalog):
    """Runs ``hook`` right after each lineage lookup (response scan by ``revision_id``) that
    follows an element-table scan — i.e. between the element read and the closing head check."""

    hook: Any = None
    lineage_scans: int = 0
    armed: bool = False

    def scan_columns(self, table: str, **kwargs: Any) -> Any:
        result = self.inner.scan_columns(table, **kwargs)
        text = repr(kwargs.get("row_filter"))
        if table == AGGS.table and "observation_key" in text:
            self.armed = True
        elif (
            table == RESPONSES.table
            and self.armed
            and "revision_id" in text
            and kwargs.get("limit") is None
        ):
            self.armed = False
            self.lineage_scans += 1
            if self.hook is not None:
                self.hook(self.lineage_scans)
        return result


def _unrelated_response_factory(h: RestHarness) -> Any:
    """Commits a fresh, unrelated response row (another page identity) per call."""
    template = _anchor_row(h, BASE + 100 * STRIDE, "mover")

    def commit(index: int) -> None:
        row = dict(template)
        digest = hashlib.sha256(f"mover-{index}".encode()).hexdigest()
        row["payload_hash"] = row["object_sha256"] = digest
        row["revision_id"] = rest_identity.revision_id(
            row["observation_key"], row["source_id"], digest
        )
        row["arrival_seq"] = BASE + (100 + index) * STRIDE
        h.forge_rows(RESPONSES, [row], f"mover-{index}")

    return commit


def test_a_head_moved_between_element_and_lineage_reads_is_read_again(h: RestHarness) -> None:
    first, request_b = _overlap(h)
    mover = _unrelated_response_factory(h)
    proxy = _ScanHook(h.adapter, hook=lambda count: mover(count) if count == 1 else None)

    out = h.store(clock=StepClock(start=K2), adapter=proxy).ingest_collection(request_b)

    assert proxy.lineage_scans >= 3  # moved once → read again, then the closing read-back
    by_id = _by(h.rows(AGGS), "revision_id")
    for shared in first.pages[0].element_revision_ids[1:]:
        assert by_id[shared]["response_revision_id"] == first.pages[0].response_revision_id
    assert out.pages[0].owned_element_revision_ids == (out.pages[0].element_revision_ids[2],)


def test_a_mixed_view_is_never_judged_the_newer_snapshot_is(h: RestHarness) -> None:
    """A twin of the lineage response lands after the lineage read: the first judgement (one
    row, lawful) mixes two snapshots and must be discarded; the re-read sees the twin."""
    first, request_b = _overlap(h)
    [response_a] = h.rows(RESPONSES)
    twin = _drifted(response_a, {"arrival_seq": BASE + 50 * STRIDE})

    def land_twin(count: int) -> None:
        if count == 1:
            h.forge_rows(RESPONSES, [twin], "hostile-twin")

    proxy = _ScanHook(h.adapter, hook=land_twin)
    element_state = _tables_state(h)[AGGS.table]

    with pytest.raises(CatalogIntegrityError, match="committed 2 time"):
        h.store(clock=StepClock(start=K2), adapter=proxy).ingest_collection(request_b)
    assert proxy.lineage_scans == 2
    after = _tables_state(h)
    assert after[AGGS.table] == element_state
    assert after[BINANCE_SPOT_PRECEDENCE_EVIDENCE.table] == (None, 0)


def test_heads_that_keep_moving_end_in_a_bounded_conflict(h: RestHarness) -> None:
    first, request_b = _overlap(h)
    mover = _unrelated_response_factory(h)
    proxy = _ScanHook(h.adapter, hook=mover)
    element_state = _tables_state(h)[AGGS.table]

    with pytest.raises(RestRevisionStoreConflict, match="kept moving.*after 8 attempts"):
        h.store(clock=StepClock(start=K2), adapter=proxy).ingest_collection(request_b)
    assert proxy.lineage_scans == 8
    after = _tables_state(h)
    assert after[AGGS.table] == element_state
    assert after[BINANCE_SPOT_PRECEDENCE_EVIDENCE.table] == (None, 0)

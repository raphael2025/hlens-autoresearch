"""``binance.spot.archive-revision@1.0.0``: replay, provable order, competing heads.

Also checks the negative property the ADR cares most about: the precedence module must not read
arrival order, wall clocks, transport metadata or hashes to break a tie.
"""

from __future__ import annotations

import ast
import hashlib
import inspect
import json
from datetime import UTC, datetime, timedelta

import pytest

from core.contracts.revision import (
    AvailabilityDecision,
    ObservationTimes,
    PolicyRole,
    PrecedenceEvidence,
    RevisionGraph,
    RevisionRecord,
)
from core.domain.base import canonical_json
from infrastructure.revision import precedence as precedence_module
from infrastructure.revision import store as store_module
from infrastructure.revision.availability import AVAILABILITY_BINDING
from infrastructure.revision.precedence import (
    PRECEDENCE_BINDING,
    PRECEDENCE_HASH,
    PRECEDENCE_POLICY_ID,
    PRECEDENCE_POLICY_VERSION,
    PRECEDENCE_SPEC,
    PrecedenceOutcome,
    PrecedenceViolation,
    RevisionFacts,
    decide_precedence,
    maximal_heads,
    supersedes_for,
)

#: Golden hash of ``PRECEDENCE_SPEC``; a rule change must bump the policy version.
GOLDEN_PRECEDENCE_HASH = "5465c2e794af51ef9087bfd29ec49887372ee42b9991a71106661aa7a64cebc9"

KEY = "binance:spot:archive:data/spot/daily/aggTrades/BTCUSDT/BTCUSDT-aggTrades-2025-01-01.zip"
SOURCE = "binance.public.spot.archive@1.0.0"
T0 = datetime(2025, 3, 1, tzinfo=UTC)


def facts(
    revision: str,
    payload: str,
    *,
    source_revision_id: str | None = None,
    source_revision_time: datetime | None = None,
    source_identity: str = SOURCE,
) -> RevisionFacts:
    return RevisionFacts(
        observation_key=KEY,
        revision_id=revision,
        source_identity=source_identity,
        payload_hash=hashlib.sha256(payload.encode()).hexdigest(),
        source_revision_id=source_revision_id,
        source_revision_time=source_revision_time,
    )


def record(revision: str, *, supersedes: tuple[str, ...] = (), seq: int = 0) -> RevisionRecord:
    times = ObservationTimes(
        event_time=T0 - timedelta(days=60),
        event_end_time=T0 - timedelta(days=59),
        available_time=T0,
        ingest_time=T0,
        knowledge_time=T0,
        declared_latency=timedelta(0),
    )
    return RevisionRecord(
        observation_key=KEY,
        revision_id=revision,
        source_id=SOURCE,
        payload_hash=hashlib.sha256(revision.encode()).hexdigest(),
        arrival_seq=seq,
        supersedes=supersedes,
        availability=AvailabilityDecision(
            times=times, policy=AVAILABILITY_BINDING, evidence_gap="no publication evidence"
        ),
    )


def edge(newer: str, older: str) -> PrecedenceEvidence:
    return PrecedenceEvidence(
        observation_key=KEY,
        revision_id=newer,
        superseded_revision_id=older,
        policy=PRECEDENCE_BINDING,
        evidence=("source revision 7 is later than source revision 6",),
        knowledge_time=T0,
    )


def test_policy_hash_and_binding_are_derived_from_the_spec() -> None:
    assert PRECEDENCE_HASH == GOLDEN_PRECEDENCE_HASH
    assert (
        hashlib.sha256(canonical_json(PRECEDENCE_SPEC).encode("utf-8")).hexdigest()
        == PRECEDENCE_HASH
    )
    assert PRECEDENCE_BINDING.role is PolicyRole.PRECEDENCE
    assert PRECEDENCE_BINDING.policy_id == PRECEDENCE_POLICY_ID
    assert PRECEDENCE_BINDING.version == PRECEDENCE_POLICY_VERSION


def test_rule_changes_change_the_hash() -> None:
    mutated = json.loads(canonical_json(PRECEDENCE_SPEC))
    mutated["rules"]["ordering"] = "anything goes"
    assert hashlib.sha256(canonical_json(mutated).encode("utf-8")).hexdigest() != PRECEDENCE_HASH


def test_same_source_and_payload_is_a_replay() -> None:
    one = facts("rev1-a", "payload")
    two = facts("rev1-a", "payload")
    assert decide_precedence(one, two) is PrecedenceOutcome.REPLAY


def test_a_different_payload_is_never_ordered_without_source_evidence() -> None:
    old = facts("rev1-a", "old")
    new = facts("rev1-b", "new")
    assert decide_precedence(new, old) is PrecedenceOutcome.UNORDERED
    assert decide_precedence(old, new) is PrecedenceOutcome.UNORDERED


def test_binance_archive_replacements_produce_competing_heads() -> None:
    old = facts("rev1-a", "old")
    new = facts("rev1-b", "new")
    superseded, evidence, unordered = supersedes_for(new, [old], knowledge_time=T0)
    assert superseded == () and evidence == ()
    assert unordered == ("rev1-a",)
    assert maximal_heads([record("rev1-a"), record("rev1-b")]) == ("rev1-a", "rev1-b")


def test_a_declared_source_revision_time_orders_strictly() -> None:
    old = facts("rev1-a", "old", source_revision_id="6", source_revision_time=T0)
    new = facts(
        "rev1-b", "new", source_revision_id="7", source_revision_time=T0 + timedelta(hours=1)
    )
    assert decide_precedence(new, old) is PrecedenceOutcome.SUPERSEDES
    assert decide_precedence(old, new) is PrecedenceOutcome.SUPERSEDED_BY
    superseded, evidence, unordered = supersedes_for(new, [old], knowledge_time=T0)
    assert superseded == ("rev1-a",) and unordered == ()
    assert len(evidence) == 1
    assert evidence[0].revision_id == "rev1-b"
    assert evidence[0].superseded_revision_id == "rev1-a"
    assert evidence[0].policy == PRECEDENCE_BINDING


def test_the_ordering_conclusion_does_not_depend_on_arrival_order() -> None:
    old = facts("rev1-a", "old", source_revision_id="6", source_revision_time=T0)
    new = facts(
        "rev1-b", "new", source_revision_id="7", source_revision_time=T0 + timedelta(hours=1)
    )
    # The semantically newer revision arriving first changes nothing.
    first = supersedes_for(new, [old], knowledge_time=T0)[0]
    second = supersedes_for(old, [new], knowledge_time=T0)[0]
    assert first == ("rev1-a",)
    assert second == ()  # the older revision never supersedes the newer one
    heads = maximal_heads(
        [record("rev1-a"), record("rev1-b", supersedes=("rev1-a",))], [edge("rev1-b", "rev1-a")]
    )
    assert heads == ("rev1-b",)


@pytest.mark.parametrize(
    ("mine", "theirs"),
    [
        ((None, None), ("7", T0)),
        (("7", T0), (None, None)),
        (("7", T0), ("6", T0)),  # equal times
        (("7", T0), ("7", T0 - timedelta(hours=1))),  # one source revision, two payloads
    ],
)
def test_incomplete_or_contradictory_evidence_stays_unordered(
    mine: tuple[str | None, datetime | None], theirs: tuple[str | None, datetime | None]
) -> None:
    new = facts("rev1-b", "new", source_revision_id=mine[0], source_revision_time=mine[1])
    old = facts("rev1-a", "old", source_revision_id=theirs[0], source_revision_time=theirs[1])
    assert decide_precedence(new, old) is PrecedenceOutcome.UNORDERED


def test_half_declared_source_revisions_are_refused() -> None:
    with pytest.raises(PrecedenceViolation):
        facts("rev1-a", "x", source_revision_id="7")
    with pytest.raises(PrecedenceViolation):
        facts("rev1-a", "x", source_revision_time=T0)


def test_naive_source_revision_times_are_refused() -> None:
    with pytest.raises(PrecedenceViolation):
        facts("rev1-a", "x", source_revision_id="7", source_revision_time=datetime(2025, 3, 1))  # noqa: DTZ001


def test_cross_key_comparison_is_refused() -> None:
    other = RevisionFacts(
        observation_key=KEY + ".other",
        revision_id="rev1-c",
        source_identity=SOURCE,
        payload_hash="c" * 64,
    )
    with pytest.raises(PrecedenceViolation, match="observation_key"):
        decide_precedence(facts("rev1-a", "x"), other)


def test_two_payloads_sharing_a_revision_id_are_refused() -> None:
    with pytest.raises(PrecedenceViolation, match="revision_id"):
        decide_precedence(facts("rev1-a", "one"), facts("rev1-a", "two"))


def test_a_replay_must_not_yield_an_appended_revision() -> None:
    with pytest.raises(PrecedenceViolation, match="replay"):
        supersedes_for(facts("rev1-a", "x"), [facts("rev1-a", "x")], knowledge_time=T0)


def test_a_non_precedence_binding_is_refused() -> None:
    with pytest.raises(PrecedenceViolation):
        supersedes_for(
            facts("rev1-b", "new"),
            [facts("rev1-a", "old")],
            knowledge_time=T0,
            binding=AVAILABILITY_BINDING,
        )


def test_transitive_supersession_leaves_one_head() -> None:
    records = [
        record("rev1-a"),
        record("rev1-b", supersedes=("rev1-a",), seq=1),
        record("rev1-c", supersedes=("rev1-b",), seq=2),
    ]
    evidence = [edge("rev1-b", "rev1-a"), edge("rev1-c", "rev1-b")]
    assert maximal_heads(records, evidence) == ("rev1-c",)
    # The result is a property of the edges, not of the order the records are given in.
    assert maximal_heads(list(reversed(records)), list(reversed(evidence))) == ("rev1-c",)


def test_heads_ignore_arrival_sequence_numbers() -> None:
    low = record("rev1-a", seq=0)
    high = record("rev1-b", seq=10**9)
    assert maximal_heads([low, high]) == ("rev1-a", "rev1-b")


def test_graph_rejections_are_contract_level() -> None:
    cyclic_records = (
        record("rev1-a", supersedes=("rev1-b",)),
        record("rev1-b", supersedes=("rev1-a",), seq=1),
    )
    with pytest.raises(ValueError, match="成环"):
        RevisionGraph(
            revisions=cyclic_records,
            precedence_evidence=(edge("rev1-a", "rev1-b"), edge("rev1-b", "rev1-a")),
        )
    with pytest.raises(ValueError):  # self reference
        record("rev1-a", supersedes=("rev1-a",))
    with pytest.raises(ValueError, match="缺少"):  # an edge without evidence
        RevisionGraph(revisions=(record("rev1-b", supersedes=("rev1-a",)),))


def test_cross_key_evidence_is_refused_by_maximal_heads() -> None:
    foreign = PrecedenceEvidence(
        observation_key=KEY + ".other",
        revision_id="rev1-x",
        superseded_revision_id="rev1-a",
        policy=PRECEDENCE_BINDING,
        evidence=("e",),
        knowledge_time=T0,
    )
    with pytest.raises(PrecedenceViolation, match="another observation_key"):
        maximal_heads([record("rev1-a")], [foreign])


def test_precedence_never_reads_arrival_or_clock_inputs() -> None:
    """No *executable* name in the module touches a forbidden tie-breaker.

    Only identifiers are inspected: the module documents and lists the forbidden inputs as data
    (``PRECEDENCE_SPEC["never_evidence"]``), which is exactly how they should appear.
    """
    tree = ast.parse(inspect.getsource(precedence_module))
    identifiers: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            identifiers.add(node.id)
        elif isinstance(node, ast.Attribute):
            identifiers.add(node.attr)
        elif isinstance(node, ast.arg):
            identifiers.add(node.arg)
        elif isinstance(node, ast.keyword) and node.arg is not None:
            identifiers.add(node.arg)
    forbidden = {
        "arrival_seq",
        "retrieved_at",
        "ingest_time",
        "available_time",
        "now",
        "utcnow",
        "monotonic",
        "etag",
        "last_modified",
    }
    assert not identifiers & forbidden, f"precedence must not read {identifiers & forbidden}"
    assert "sorted" in identifiers  # canonical output order is still a sort over ids


def test_precedence_inputs_cannot_carry_arrival_or_time_fields() -> None:
    """``RevisionFacts`` is the *only* input of a precedence decision, by construction."""
    fields = set(RevisionFacts.__dataclass_fields__)
    assert fields == {
        "observation_key",
        "revision_id",
        "source_identity",
        "payload_hash",
        "source_revision_id",
        "source_revision_time",
    }


def test_only_the_allocation_path_of_the_store_touches_arrival_seq() -> None:
    """Everywhere else, ``arrival_seq`` is carried, never consulted (ADR-0023 §4)."""
    source = inspect.getsource(store_module)
    tree = ast.parse(source)
    mentioning = {
        node.name
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef)
        and "arrival_seq" in (ast.get_source_segment(source, node) or "")
    }
    assert mentioning == {
        "_persist",  # reads the recovered block base back from the archive row
        "_max_archive_arrival_seq",  # allocation anchor
        "_append_archive",  # allocates the block
        "_verify_stored_archive",  # recovers the block base
        "_record_from_row",  # rebuilds the contract record
        "_revision_columns",  # writes the column
        "_row_records",  # assigns base + line number
    }, mentioning


def test_the_spec_lists_the_forbidden_inputs() -> None:
    never = set(PRECEDENCE_SPEC["never_evidence"])
    assert {"arrival_seq", "retrieved_at", "ingest_time", "http etag", "local wall clock"} <= never

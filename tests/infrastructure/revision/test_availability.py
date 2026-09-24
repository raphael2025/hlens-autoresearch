"""``binance.spot.publication@1.0.0``: golden hash, conservative fallback, fail-closed inputs.

The 1.0.0 outcome is deliberate: no official Binance statement bounds the publication time of a
concrete revision, so every subject falls back to ``available_time = ingest_time`` and persists an
evidence gap (ADR-0023 §2; ``docs/architecture/evidence/binance-spot-publication.md``). The tests
below pin that outcome, pin the fail-closed guards, and exercise the evidence-bearing mechanism
through an explicitly test-only policy version so a future evidence-backed rule has a harness.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta

import pytest

from core.contracts.revision import PolicyBinding, PolicyRole
from core.domain.base import canonical_json
from infrastructure.revision.availability import (
    AVAILABILITY_BINDING,
    AVAILABILITY_HASH,
    AVAILABILITY_POLICY_ID,
    AVAILABILITY_POLICY_VERSION,
    AVAILABILITY_SPEC,
    AvailabilityRule,
    AvailabilityRuleKind,
    AvailabilitySubject,
    AvailabilityViolation,
    decide_availability,
    rule_for,
)

#: Golden hash of ``AVAILABILITY_SPEC``; a rule change must bump the policy version.
GOLDEN_AVAILABILITY_HASH = "014a6fe3e7d9a2ca7eea775b5f811377b26ae884cd3d41ba73fc088c9ab22ca9"

EVENT = datetime(2025, 1, 1, 12, 0, tzinfo=UTC)
INGEST = datetime(2025, 3, 1, 9, 0, tzinfo=UTC)
KNOWLEDGE = INGEST + timedelta(seconds=5)

#: Test-only: what an evidence-bearing rule would look like. Not registered, never used in
#: production, and deliberately *not* named after the frozen policy id.
TEST_BINDING = PolicyBinding(
    role=PolicyRole.AVAILABILITY,
    policy_id="test.publication-with-bound",
    version="0.0.1",
    policy_hash="f" * 64,
)


def bounded(subject: AvailabilitySubject, bound: timedelta) -> AvailabilityRule:
    return AvailabilityRule(
        subject=subject,
        kind=AvailabilityRuleKind.OBSERVABLE_PLUS_BOUND,
        evidence=("test-only rule: an official document bounds publication by this delay",),
        bound=bound,
    )


def test_policy_hash_and_binding_are_derived_from_the_spec() -> None:
    assert AVAILABILITY_HASH == GOLDEN_AVAILABILITY_HASH
    assert (
        hashlib.sha256(canonical_json(AVAILABILITY_SPEC).encode("utf-8")).hexdigest()
        == AVAILABILITY_HASH
    )
    assert AVAILABILITY_BINDING.policy_id == AVAILABILITY_POLICY_ID
    assert AVAILABILITY_BINDING.version == AVAILABILITY_POLICY_VERSION
    assert AVAILABILITY_BINDING.role is PolicyRole.AVAILABILITY
    assert AVAILABILITY_BINDING.policy_hash == AVAILABILITY_HASH


def test_changing_any_rule_changes_the_hash() -> None:
    mutated = json.loads(canonical_json(AVAILABILITY_SPEC))
    mutated["rules"][0]["kind"] = AvailabilityRuleKind.OBSERVABLE_PLUS_BOUND.value
    assert hashlib.sha256(canonical_json(mutated).encode("utf-8")).hexdigest() != AVAILABILITY_HASH


@pytest.mark.parametrize("subject", list(AvailabilitySubject))
def test_every_subject_falls_back_to_ingest_time_with_a_gap(
    subject: AvailabilitySubject,
) -> None:
    decision = decide_availability(
        subject,
        event_time=EVENT,
        event_end_time=EVENT + timedelta(minutes=1),
        ingest_time=INGEST,
        knowledge_time=KNOWLEDGE,
    )
    assert decision.times.available_time == INGEST
    assert decision.evidence == ()
    assert decision.evidence_gap == rule_for(subject).gap
    assert decision.evidence_gap is not None and AVAILABILITY_POLICY_ID in decision.evidence_gap
    assert decision.times.source_time is None
    assert decision.policy == AVAILABILITY_BINDING


def test_the_policy_never_derives_a_source_time_from_transport_metadata() -> None:
    assert AVAILABILITY_SPEC["source_time"]["set_from"] == []
    rejected = AVAILABILITY_SPEC["source_time"]["rejected_inputs"]
    assert {"http last-modified", "http etag"} <= set(rejected)


def test_ingest_before_the_observable_time_fails_closed() -> None:
    with pytest.raises(AvailabilityViolation, match="observable"):
        decide_availability(
            AvailabilitySubject.KLINE_1M,
            event_time=EVENT,
            event_end_time=EVENT + timedelta(minutes=1),
            ingest_time=EVENT,  # the bar has not closed yet
            knowledge_time=KNOWLEDGE,
        )


def test_knowledge_before_ingest_fails_closed() -> None:
    with pytest.raises(AvailabilityViolation, match="knowledge_time"):
        decide_availability(
            AvailabilitySubject.AGG_TRADE,
            event_time=EVENT,
            ingest_time=INGEST,
            knowledge_time=INGEST - timedelta(seconds=1),
        )


def test_negative_declared_latency_fails_closed() -> None:
    with pytest.raises(AvailabilityViolation):
        decide_availability(
            AvailabilitySubject.AGG_TRADE,
            event_time=EVENT,
            ingest_time=INGEST,
            knowledge_time=KNOWLEDGE,
            declared_latency=timedelta(microseconds=-1),
        )


def test_a_rule_may_not_serve_another_subject() -> None:
    with pytest.raises(AvailabilityViolation, match="does not serve"):
        decide_availability(
            AvailabilitySubject.AGG_TRADE,
            event_time=EVENT,
            ingest_time=INGEST,
            knowledge_time=KNOWLEDGE,
            rule=rule_for(AvailabilitySubject.KLINE_1M),
        )


def test_a_non_availability_binding_is_refused() -> None:
    precedence = PolicyBinding(
        role=PolicyRole.PRECEDENCE, policy_id="x.y", version="1.0.0", policy_hash="a" * 64
    )
    with pytest.raises(AvailabilityViolation):
        decide_availability(
            AvailabilitySubject.AGG_TRADE,
            event_time=EVENT,
            ingest_time=INGEST,
            knowledge_time=KNOWLEDGE,
            binding=precedence,
        )


def test_a_malformed_rule_cannot_be_constructed() -> None:
    with pytest.raises(AvailabilityViolation):
        AvailabilityRule(
            subject=AvailabilitySubject.AGG_TRADE, kind=AvailabilityRuleKind.INGEST_FALLBACK
        )
    with pytest.raises(AvailabilityViolation):  # a fallback rule carrying evidence
        AvailabilityRule(
            subject=AvailabilitySubject.AGG_TRADE,
            kind=AvailabilityRuleKind.INGEST_FALLBACK,
            gap="g",
            evidence=("e",),
        )
    with pytest.raises(AvailabilityViolation):  # an evidence rule without a bound
        AvailabilityRule(
            subject=AvailabilitySubject.AGG_TRADE,
            kind=AvailabilityRuleKind.OBSERVABLE_PLUS_BOUND,
            evidence=("e",),
        )
    with pytest.raises(AvailabilityViolation):  # a negative bound would look into the future
        AvailabilityRule(
            subject=AvailabilitySubject.AGG_TRADE,
            kind=AvailabilityRuleKind.OBSERVABLE_PLUS_BOUND,
            evidence=("e",),
            bound=timedelta(seconds=-1),
        )


# --------------------------------------------------------------- evidence-bearing mechanism


def test_an_evidence_rule_can_place_availability_before_ingest() -> None:
    decision = decide_availability(
        AvailabilitySubject.AGG_TRADE,
        event_time=EVENT,
        ingest_time=INGEST,
        knowledge_time=KNOWLEDGE,
        rule=bounded(AvailabilitySubject.AGG_TRADE, timedelta(seconds=2)),
        binding=TEST_BINDING,
    )
    assert decision.times.available_time == EVENT + timedelta(seconds=2)
    assert decision.times.available_time < decision.times.ingest_time
    assert decision.evidence and decision.evidence_gap is None


def test_an_interval_rule_measures_from_the_interval_end() -> None:
    end = EVENT + timedelta(minutes=1)
    decision = decide_availability(
        AvailabilitySubject.KLINE_1M,
        event_time=EVENT,
        event_end_time=end,
        ingest_time=INGEST,
        knowledge_time=KNOWLEDGE,
        rule=bounded(AvailabilitySubject.KLINE_1M, timedelta(seconds=2)),
        binding=TEST_BINDING,
    )
    assert decision.times.available_time == end + timedelta(seconds=2)
    assert decision.times.available_time > decision.times.observable_time


@pytest.mark.parametrize("bound", [timedelta(0), timedelta(microseconds=1), timedelta(hours=26)])
def test_evidence_rules_never_predate_the_observation(bound: timedelta) -> None:
    end = EVENT + timedelta(minutes=1)
    decision = decide_availability(
        AvailabilitySubject.KLINE_1M,
        event_time=EVENT,
        event_end_time=end,
        ingest_time=INGEST,
        knowledge_time=KNOWLEDGE,
        declared_latency=timedelta(milliseconds=250),
        rule=bounded(AvailabilitySubject.KLINE_1M, bound),
        binding=TEST_BINDING,
    )
    assert decision.times.available_time >= end
    assert decision.times.available_time == end + bound + timedelta(milliseconds=250)


def test_an_evidence_rule_never_predates_a_declared_source_time() -> None:
    source_time = EVENT + timedelta(hours=3)
    decision = decide_availability(
        AvailabilitySubject.AGG_TRADE,
        event_time=EVENT,
        source_time=source_time,
        ingest_time=INGEST,
        knowledge_time=KNOWLEDGE,
        rule=bounded(AvailabilitySubject.AGG_TRADE, timedelta(seconds=1)),
        binding=TEST_BINDING,
    )
    assert decision.times.available_time == source_time + timedelta(seconds=1)


def test_a_fallback_with_a_later_source_time_fails_closed() -> None:
    with pytest.raises(AvailabilityViolation, match="source_time"):
        decide_availability(
            AvailabilitySubject.AGG_TRADE,
            event_time=EVENT,
            source_time=INGEST + timedelta(days=1),
            ingest_time=INGEST,
            knowledge_time=KNOWLEDGE,
        )

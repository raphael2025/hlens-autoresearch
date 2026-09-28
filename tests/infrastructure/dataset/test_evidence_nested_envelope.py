"""v3 evidence records with a nested contract of another version (ADR-0088 PM decision; ADR-0051
second phase).

``UniverseMember.assumption`` nests the ADR-0051 ``ASSUMPTION_BINDING``, a registered identity
pinned at its 2.0.0 publication envelope, inside a member written at the current contract
version. The record projection keeps exactly such a differing nested ``schema_version`` (and only
it), so the member rebuilds bit-identically; every record without a nested version difference is
encoded exactly as before. Limits are arbitrary small values (DQ-9 OPEN).
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from core.contracts.revision import PolicyBinding
from core.contracts.universe import EvidenceStream, UniverseMember
from core.domain.base import CONTRACT_SCHEMA_VERSION, Contract, canonical_json
from infrastructure.contract_version import PHASE1_PUBLICATION_VERSION
from infrastructure.dataset.evidence import (
    EvidenceIntegrityError,
    EvidenceTreeLimits,
    EvidenceTreeWriter,
    evidence_record_bytes,
    evidence_record_from_bytes,
    iter_evidence_stream,
)
from infrastructure.storage import LocalFileStorageAdapter
from infrastructure.universe import listing_assumption as backfill
from tests.infrastructure.dataset import dataset_support as ds

MEMBERS = EvidenceStream.MEMBERS
VERSION = CONTRACT_SCHEMA_VERSION
TREE = EvidenceTreeLimits(leaf_max_records=1, leaf_max_bytes=4096, fanout=2)


def member(assumption: PolicyBinding | None = backfill.ASSUMPTION_BINDING) -> UniverseMember:
    return UniverseMember(
        episode=ds.v3_episode("BTCUSDT"),
        listing_revision_id="listing-btc",
        assumption=assumption,
    )


def at_current_envelope(binding: PolicyBinding) -> PolicyBinding:
    """The same policy identity (id, version, hash) under the current contract envelope."""
    return PolicyBinding(**binding.model_dump(exclude={"schema_version"}))


def strip_all(value: Any) -> Any:
    """The projection before the fix: every ``schema_version`` removed at any depth."""
    if isinstance(value, dict):
        return {key: strip_all(item) for key, item in value.items() if key != "schema_version"}
    if isinstance(value, list):
        return [strip_all(item) for item in value]
    return value


def old_bytes(record: Contract) -> bytes:
    return (canonical_json(strip_all(record.model_dump(mode="json"))) + "\n").encode("utf-8")


def reline(line: bytes, edit: Any) -> bytes:
    payload = json.loads(line)
    edit(payload)
    return (canonical_json(payload) + "\n").encode("utf-8")


def test_the_nested_2_0_0_binding_is_kept_and_rebuilds_bit_identically() -> None:
    original = member()
    assert original.schema_version == VERSION != PHASE1_PUBLICATION_VERSION
    assert original.assumption is not None
    assert original.assumption.schema_version == PHASE1_PUBLICATION_VERSION
    line = evidence_record_bytes(original)
    # Exactly one envelope in the bytes: the nested one that differs from the record's own.
    assert line.count(b'"schema_version"') == 1
    assert json.loads(line)["assumption"]["schema_version"] == PHASE1_PUBLICATION_VERSION
    assert "schema_version" not in json.loads(line)
    again = evidence_record_from_bytes(MEMBERS, line, schema_version=VERSION)
    assert again == original
    assert again.model_dump(mode="python") == original.model_dump(mode="python")
    assert again.content_hash() == original.content_hash()
    assert evidence_record_bytes(again) == line


def test_a_stream_of_assumed_members_round_trips(evidence_store: LocalFileStorageAdapter) -> None:
    records = [member(), ds.v3_member("ETHUSDT", "listing-eth")]
    writer = EvidenceTreeWriter(evidence_store, MEMBERS, limits=TREE, schema_version=VERSION)
    for record in records:
        writer.append(record)  # the writer's lossless proof passes (it used to refuse this)
    ref = writer.finish()
    with iter_evidence_stream(evidence_store, ref, limits=TREE, schema_version=VERSION) as items:
        assert list(items) == records


def test_records_without_a_nested_version_difference_keep_their_bytes() -> None:
    """Every record the old projection could prove lossless is encoded exactly as before."""
    same_envelope = member(at_current_envelope(backfill.ASSUMPTION_BINDING))
    assert same_envelope.assumption is not None
    assert same_envelope.assumption.schema_version == VERSION
    for record in (
        ds.v3_member("BTCUSDT", "listing-btc"),
        ds.v3_exclusion("ETHUSDT", "listing-eth"),
        ds.trade_lineage("r1"),
        same_envelope,
    ):
        line = evidence_record_bytes(record)
        assert line == old_bytes(record)
        assert b"schema_version" not in line


def test_the_old_projection_of_the_assumed_member_is_another_record() -> None:
    """Dropping the nested 2.0.0 envelope (the old projection) rebuilds the binding at the
    manifest version: a different object, which is why the version is kept."""
    original = member()
    stripped = old_bytes(original)
    assert stripped != evidence_record_bytes(original)
    rebuilt = evidence_record_from_bytes(MEMBERS, stripped, schema_version=VERSION)
    assert isinstance(rebuilt, UniverseMember) and rebuilt.assumption is not None
    assert rebuilt.assumption.schema_version == VERSION
    assert rebuilt != original


@pytest.mark.parametrize(
    "edit",
    [
        # a nested envelope equal to the record's own: never in the canonical bytes
        lambda payload: payload["assumption"].__setitem__("schema_version", VERSION),
        # an unpublished nested version
        lambda payload: payload["assumption"].__setitem__("schema_version", "2.99.0"),
        # the record's own envelope stored at the top level
        lambda payload: payload.__setitem__("schema_version", VERSION),
    ],
)
def test_only_the_canonical_projection_of_a_nested_record_parses(edit: Any) -> None:
    line = evidence_record_bytes(member())
    with pytest.raises(EvidenceIntegrityError):
        evidence_record_from_bytes(MEMBERS, reline(line, edit), schema_version=VERSION)


def test_the_writer_refuses_an_unpublished_nested_version(
    evidence_store: LocalFileStorageAdapter,
) -> None:
    unpublished = PolicyBinding(
        **backfill.ASSUMPTION_BINDING.model_dump(exclude={"schema_version"}),
        schema_version="2.99.0",
    )
    with pytest.raises(EvidenceIntegrityError, match="published"):
        evidence_record_bytes(member(unpublished))
    writer = EvidenceTreeWriter(evidence_store, MEMBERS, limits=TREE, schema_version=VERSION)
    with pytest.raises(EvidenceIntegrityError):
        writer.append(member(unpublished))

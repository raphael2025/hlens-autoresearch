"""ADR-0077 (DQ-1 = A): the bounded v3 Research Dataset evidence manifest at contract 2.3.0.

Contract layer only: field validation of the six additive models, deterministic content hashes,
the 2.3.0 version boundary (`_MODEL_SINCE`), the v2 `ResearchDatasetManifest` kept bit for bit
(fields, Schema, content hash of a 2.2.0 payload) and v2 / v3 read side by side. Evidence object
storage, stream readers, chunk commits and the streaming verifier are infrastructure (not here).
Every hash, id and count is a TEST ONLY fixture.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import date
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from core.contracts import universe
from core.contracts.catalog import BATCH_ID_PATTERN
from core.contracts.registry import CONTRACT_MODELS, export_json_schemas
from core.contracts.revision import PointInTimeSpec
from core.contracts.storage import validate_object_key
from core.contracts.universe import (
    ADR_0077_VERSION,
    DATASET_CHUNK_INDEX_MAX,
    DATASET_EVIDENCE_FORMAT,
    DATASET_EVIDENCE_KEY_PREFIX,
    LISTINGS_TABLE,
    QUALITY_REPORTS_TABLE,
    AvailabilityEvidenceGap,
    DatasetChunkProof,
    DatasetQualityReportRef,
    DatasetQualitySubject,
    DatasetRuleBinding,
    EvidenceObjectRef,
    EvidenceStream,
    EvidenceStreamRef,
    ResearchDatasetEvidenceManifest,
    ResearchDatasetManifest,
    SelectedRevisionLineage,
    UniverseExclusion,
    UniverseMember,
    dataset_chunk_batch_id,
    dataset_evidence_key,
)
from core.domain.base import (
    CONTRACT_SCHEMA_VERSION,
    PUBLISHED_CONTRACT_SCHEMA_VERSIONS,
    Contract,
    canonical_json,
    content_hash,
    contract_schema_version_scope,
)
from core.domain.specs import Zone
from tests.contract_version_support import as_published_at, at_version
from tests.test_universe_contracts import (
    HASH,
    dataset,
    first_slice_spec,
    manifest,
    pit,
)

REPO = Path(__file__).resolve().parents[1]
CURRENT_SCHEMA_DIR = REPO / "schemas"

NEW_MODELS: tuple[type[Contract], ...] = (
    DatasetRuleBinding,
    EvidenceObjectRef,
    EvidenceStreamRef,
    DatasetQualityReportRef,
    DatasetChunkProof,
    ResearchDatasetEvidenceManifest,
)

#: SHA-256 of the committed Schema bytes before ADR-0077 (contract 2.2.0): the v2 manifest and the
#: v2 models reused as stream records. The 2.3.0 bump may change only their envelope default.
V2_SCHEMA_SHA256_AT_2_2_0 = {
    "ResearchDatasetManifest": "025753992b512fc50ad43d76983aa37f9107da8bf76da16b172839d6f151b4ef",
    "UniverseMember": "92a54584c8567cd468837c00672970d20c01c9b8b8493a286823bbd5bafd1d0b",
    "UniverseExclusion": "ab1f380a49cccafa455d73690c1ab47d43e9193598b9e9bf4bf34860500efd2b",
    "SelectedRevisionLineage": "383bc39f806172b6bd0c1942c7a3084cf7e347b30bae013c1954be25463f371d",
    "AvailabilityEvidenceGap": "fe21e259cdd62555f56a83bf9800accb07f2a3a93defcf95980a9d4af9382718",
}

# Fixed, canonical persisted evidence for the six-stream v3 manifest at its original envelope.
LEGACY_V3_MANIFEST_SHA256 = {
    "2.3.0": "40899f7552ed35bbb5f505b2ba30c7007c5400190ed65664f947954e291c1da5",
    "2.4.0": "a2b89b7e5444399748d91e3d51021ec82970bfdc2edd29f39a2216fcf3a9930c",
}

#: A persisted-shape v2 manifest written at 2.2.0 (canonical JSON, TEST ONLY values) and its
#: content hash (SHA-256 of exactly these bytes), pinned before the 2.3.0 bump.
V2_MANIFEST_AT_2_2_0 = (
    '{"dataset":{"schema_version":"2.2.0","snapshot_id":"9001",'
    '"table":"research.dataset_selections","time_range_end":"2024-07-01T00:00:00Z",'
    '"time_range_start":"2024-06-01T00:00:00Z","zone":"research_dataset"},'
    '"evidence_gaps":[],"exclusions":[],"lineage":[],"members":[],'
    '"point_in_time":{"availability_bindings":[{"policy_hash":"' + HASH + '",'
    '"policy_id":"binance.spot.publication","role":"availability","schema_version":"2.2.0",'
    '"version":"1.0.0"}],"knowledge_cutoff":"2026-09-01T00:05:00Z","name":"phase1.first_slice",'
    '"parser_bindings":[{"policy_hash":"' + HASH + '","policy_id":"binance.spot.archive.parser",'
    '"role":"parser","schema_version":"2.2.0","version":"1.0.0"}],'
    '"point_in_time_binding":{"policy_hash":"' + HASH + '","policy_id":"hlens.pit.maximal-head",'
    '"role":"point_in_time","schema_version":"2.2.0","version":"1.0.0"},'
    '"precedence_bindings":[{"policy_hash":"' + HASH + '",'
    '"policy_id":"binance.spot.archive-revision","role":"precedence","schema_version":"2.2.0",'
    '"version":"1.0.0"}],"schema_version":"2.2.0","simulation_end":null,"simulation_start":null,'
    '"simulation_time":"2024-06-01T00:00:00Z","snapshot_bindings":'
    '{"canonical.instrument_listings":"7001","quality.data_quality_reports":"7002"},'
    '"version":"1.0.0"},"quality_report_ids":["qr-1"],"schema_version":"2.2.0",'
    '"universe_spec":{"name":"binance.spot.btc-eth","schema_version":"2.2.0",'
    '"spec_hash":"' + HASH + '","version":"1.0.0"}}'
)
V2_MANIFEST_AT_2_2_0_HASH = "89455ec16a53c92d5f71365aec036617fa24f649c669c6e3e7f164fea861c880"

RULE_ID = "hlens.dataset.pit-selection"
SELECTION = f"{RULE_ID}@2.0.0." + "a" * 64
FINGERPRINT = "f" * 64


# ======================================================================================
# 构造辅助（TEST ONLY）
# ======================================================================================


def sha(n: int) -> str:
    return format(n, "064x")


def obj(n: int = 1, size: int = 128) -> EvidenceObjectRef:
    return EvidenceObjectRef(key=dataset_evidence_key(sha(n)), sha256=sha(n), size=size)


def stream_ref(
    stream: EvidenceStream, record_count: int = 3, leaf_count: int | None = None, **extra: Any
) -> EvidenceStreamRef:
    if leaf_count is None:
        leaf_count = 1 if record_count else 0
    payload: dict[str, Any] = {
        "stream": stream,
        "format": DATASET_EVIDENCE_FORMAT,
        "record_count": record_count,
        "leaf_count": leaf_count,
        "depth": 1,
        "root": obj(list(EvidenceStream).index(stream) + 1),
    }
    payload.update(extra)
    return EvidenceStreamRef(**payload)


def evidence(chunk_count: int = 3, **counts: int) -> tuple[EvidenceStreamRef, ...]:
    records = {
        EvidenceStream.MEMBERS: 2,
        EvidenceStream.EXCLUSIONS: 0,
        EvidenceStream.LINEAGE: 5,
        EvidenceStream.EVIDENCE_GAPS: 1,
        EvidenceStream.QUALITY_REPORTS: 3,
        EvidenceStream.CHUNK_PROOFS: chunk_count,
    }
    for name, value in counts.items():
        records[EvidenceStream(name)] = value
    return tuple(stream_ref(stream, count) for stream, count in records.items())


def rule(**overrides: Any) -> DatasetRuleBinding:
    payload: dict[str, Any] = {"rule_id": RULE_ID, "version": "2.0.0", "rule_hash": HASH}
    payload.update(overrides)
    return DatasetRuleBinding(**payload)


def report(subject: str = "symbol_day", **overrides: Any) -> DatasetQualityReportRef:
    payload: dict[str, Any] = {"report_id": "qr-1", "subject": subject}
    if subject == "symbol_day":
        payload.update(symbol="BTCUSDT", day=date(2024, 6, 1))
    payload.update(overrides)
    return DatasetQualityReportRef(**payload)


def proof(chunk_index: int = 0, **overrides: Any) -> DatasetChunkProof:
    payload: dict[str, Any] = {
        "chunk_index": chunk_index,
        "batch_id": dataset_chunk_batch_id(SELECTION, chunk_index),
        "snapshot_id": "9101",
        "first_row_ordinal": chunk_index * 1000,
        "row_count": 1000,
        "batch_fingerprint": FINGERPRINT,
    }
    payload.update(overrides)
    return DatasetChunkProof(**payload)


def v3(*, schema_version: str = "2.4.0", **overrides: Any) -> ResearchDatasetEvidenceManifest:
    """Legacy six-stream manifest fixture, explicitly rebuilt at its recorded 2.3/2.4 era."""
    with contract_schema_version_scope(schema_version):
        return _v3_payload(**overrides)


def _v3_payload(**overrides: Any) -> ResearchDatasetEvidenceManifest:
    payload: dict[str, Any] = {
        "dataset": dataset(table="research.dataset_selection_chunks", snapshot_id="9103"),
        "point_in_time": pit(),
        "universe_spec": first_slice_spec().binding(),
        "rule": rule(),
        "data_type": "klines_1m",
        "selection_id": SELECTION,
        "row_count": 2500,
        "chunk_rows": 1000,
        "chunk_count": 3,
    }
    payload.update(overrides)
    if "evidence" not in payload:
        payload["evidence"] = evidence(payload["chunk_count"])
    return ResearchDatasetEvidenceManifest(**payload)


def valid_instances() -> dict[type[Contract], Contract]:
    current_evidence = (*evidence(3), stream_ref(EvidenceStream.PIT_CONFLICTS, 0))
    current_pit = pit().model_copy(
        update={
            "snapshot_bindings": {
                **pit().snapshot_bindings,
                "quality.data_quality_report_manifests": "9104",
            }
        }
    )
    return {
        DatasetRuleBinding: rule(),
        EvidenceObjectRef: obj(),
        EvidenceStreamRef: stream_ref(EvidenceStream.LINEAGE),
        DatasetQualityReportRef: report(),
        DatasetChunkProof: proof(),
        ResearchDatasetEvidenceManifest: v3(
            schema_version="2.5.0", evidence=current_evidence, point_in_time=current_pit
        ),
    }


def wire(instance: Contract) -> dict[str, Any]:
    payload: dict[str, Any] = json.loads(instance.model_dump_json())
    return payload


# ======================================================================================
# 版本与登记（ADR-0077 DQ-1 = A；ADR-0052 §4）
# ======================================================================================


def test_2_3_shapes_remain_replayable_and_every_minor_stays_published() -> None:
    assert ADR_0077_VERSION == "2.3.0"
    assert CONTRACT_SCHEMA_VERSION == "2.5.0"  # ADR-0094 raised the current minor
    assert PUBLISHED_CONTRACT_SCHEMA_VERSIONS == (
        "2.0.0",
        "2.1.0",
        "2.2.0",
        "2.3.0",
        "2.4.0",
        "2.5.0",
    )
    assert PUBLISHED_CONTRACT_SCHEMA_VERSIONS[-1] == CONTRACT_SCHEMA_VERSION


def test_every_new_model_is_declared_since_2_3_0() -> None:
    for model in NEW_MODELS:
        assert model._MODEL_SINCE == "2.3.0", model.__name__
        assert not model._FIELDS_SINCE, model.__name__
        if model is EvidenceStreamRef:
            assert dict(model._VALUES_SINCE) == {"stream": {EvidenceStream.PIT_CONFLICTS: "2.5.0"}}
        else:
            assert not model._VALUES_SINCE, model.__name__
    # the v2 manifest and its record models are not new content
    for model in (
        ResearchDatasetManifest,
        UniverseMember,
        UniverseExclusion,
        SelectedRevisionLineage,
        AvailabilityEvidenceGap,
    ):
        assert model._MODEL_SINCE is None, model.__name__


def test_the_new_models_are_appended_to_the_registry_and_exported() -> None:
    names = tuple(model.__name__ for model in CONTRACT_MODELS)
    assert len(names) == 148  # ADR-0094 appends two models after ADR-0088
    assert names[:135][-1] == "FillRemainder"  # everything before ADR-0077 keeps its place
    assert CONTRACT_MODELS[135:141] == NEW_MODELS
    for model in NEW_MODELS:
        assert model.__name__ in universe.__all__


def test_the_committed_schemas_of_the_new_models_match_the_contracts(tmp_path: Path) -> None:
    written = export_json_schemas(tmp_path)
    for model in NEW_MODELS:
        committed = json.loads(
            (CURRENT_SCHEMA_DIR / f"{model.__name__}.schema.json").read_text(encoding="utf-8")
        )
        exported = json.loads(written[model.__name__].read_text(encoding="utf-8"))
        assert committed == exported, model.__name__
        assert committed["properties"]["schema_version"]["default"] == "2.5.0"  # ADR-0094
        assert committed["additionalProperties"] is False


@pytest.mark.parametrize("model", NEW_MODELS, ids=lambda m: m.__name__)
def test_new_objects_carry_the_2_3_0_envelope(model: type[Contract]) -> None:
    instance = valid_instances()[model]
    assert instance.schema_version == "2.5.0"  # current envelope, separate from introduction
    assert model.model_validate(wire(instance)) == instance


@pytest.mark.parametrize("old", ["2.0.0", "2.1.0", "2.2.0"])
@pytest.mark.parametrize("model", NEW_MODELS, ids=lambda m: m.__name__)
def test_an_older_envelope_cannot_carry_a_2_3_0_model(model: type[Contract], old: str) -> None:
    payload = {**wire(valid_instances()[model]), "schema_version": old}
    with pytest.raises(ValidationError, match="2.3.0"):
        model.model_validate(payload)
    with pytest.raises(ValidationError, match="2.3.0"):
        model.model_validate_json(json.dumps(payload))


def test_new_content_cannot_be_built_inside_an_older_replay_scope() -> None:
    for old in ("2.0.0", "2.1.0", "2.2.0"):
        with contract_schema_version_scope(old), pytest.raises(ValidationError, match="2.3.0"):
            rule()
        with contract_schema_version_scope(old), pytest.raises(ValidationError, match="2.3.0"):
            proof()
    assert v3(schema_version="2.3.0").schema_version == "2.3.0"


# ======================================================================================
# EvidenceObjectRef 与对象键（§3.4，DQ-6 / DQ-8）
# ======================================================================================


def test_the_evidence_key_is_derived_only_from_the_content_hash() -> None:
    key = dataset_evidence_key(sha(7))
    assert key == f"{DATASET_EVIDENCE_KEY_PREFIX}{sha(7)}.jsonl"
    assert validate_object_key(key) == key  # a legal StorageAdapter key
    assert obj(7).key == key


@pytest.mark.parametrize("bad", ["A" * 64, "1" * 63, "1" * 65, "g" * 64, "", 7])
def test_the_evidence_key_needs_a_lowercase_sha256(bad: Any) -> None:
    with pytest.raises(ValueError, match="sha256"):
        dataset_evidence_key(bad)


def test_an_object_ref_whose_key_is_not_its_content_key_is_refused() -> None:
    with pytest.raises(ValidationError, match="DQ-6"):
        EvidenceObjectRef(key=dataset_evidence_key(sha(2)), sha256=sha(1), size=1)
    for key in (f"research/dataset-evidence/v2/{sha(1)}.jsonl", f"x/{sha(1)}.jsonl", sha(1)):
        with pytest.raises(ValidationError):
            EvidenceObjectRef(key=key, sha256=sha(1), size=1)


def test_an_object_ref_has_no_uri_and_at_least_one_byte() -> None:
    with pytest.raises(ValidationError):
        EvidenceObjectRef.model_validate({**wire(obj()), "uri": "file:///warehouse/x.jsonl"})
    with pytest.raises(ValidationError):
        obj(size=0)
    assert obj(size=1).size == 1
    assert "uri" not in EvidenceObjectRef.model_fields


# ======================================================================================
# EvidenceStreamRef（§1.3、§3）
# ======================================================================================


def test_an_empty_stream_has_no_leaf_and_a_depth_one_root() -> None:
    empty = stream_ref(EvidenceStream.EXCLUSIONS, 0)
    assert (empty.record_count, empty.leaf_count, empty.depth) == (0, 0, 1)


@pytest.mark.parametrize(
    ("records", "leaves", "depth", "message"),
    [
        (0, 1, 1, "空流"),
        (3, 0, 1, "空流"),
        (2, 3, 2, "叶对象数不得超过记录数"),
        (3, 1, 2, "depth 必须为 1"),
        (0, 0, 2, "depth 必须为 1"),
    ],
)
def test_inconsistent_stream_counts_are_refused(
    records: int, leaves: int, depth: int, message: str
) -> None:
    with pytest.raises(ValidationError, match=message):
        stream_ref(EvidenceStream.LINEAGE, records, leaves, depth=depth)


def test_a_multi_leaf_stream_may_be_deeper() -> None:
    ref = stream_ref(EvidenceStream.LINEAGE, 5000, 40, depth=2)
    assert (ref.leaf_count, ref.depth) == (40, 2)


@pytest.mark.parametrize(
    "field", [{"depth": 0}, {"record_count": -1}, {"format": "hlens.dataset.evidence-jsonl@2.0.0"}]
)
def test_stream_shape_is_checked(field: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        stream_ref(EvidenceStream.LINEAGE, **field)


def test_legacy_stream_names_remain_the_six_of_adr_0077() -> None:
    legacy_names = [
        stream.value for stream in EvidenceStream if stream is not EvidenceStream.PIT_CONFLICTS
    ]
    assert legacy_names == [
        "members",
        "exclusions",
        "lineage",
        "evidence_gaps",
        "quality_reports",
        "chunk_proofs",
    ]
    assert EvidenceStream.PIT_CONFLICTS.value == "pit_conflicts"


# ======================================================================================
# DatasetQualityReportRef / DatasetChunkProof（流内记录）
# ======================================================================================


def test_a_quality_report_names_the_listing_or_one_symbol_day() -> None:
    assert report("listing").sort_key() == (0, "", "")
    assert report().sort_key() == (1, "BTCUSDT", "2024-06-01")
    assert report().subject is DatasetQualitySubject.SYMBOL_DAY
    with pytest.raises(ValidationError, match="listing"):
        report("listing", symbol="BTCUSDT")
    with pytest.raises(ValidationError, match="listing"):
        report("listing", day=date(2024, 6, 1))
    with pytest.raises(ValidationError, match="symbol_day"):
        report(symbol=None)
    with pytest.raises(ValidationError, match="symbol_day"):
        report(day=None)
    with pytest.raises(ValidationError):
        report(report_id="")


def test_quality_reports_order_listing_first_then_symbol_and_day() -> None:
    items = [
        report(symbol="ETHUSDT", day=date(2024, 6, 1)),
        report(day=date(2024, 6, 2)),
        report("listing"),
        report(day=date(2024, 6, 1)),
    ]
    ordered = sorted(items, key=lambda item: item.sort_key())
    assert [(item.subject.value, item.symbol, item.day) for item in ordered] == [
        ("listing", None, None),
        ("symbol_day", "BTCUSDT", date(2024, 6, 1)),
        ("symbol_day", "BTCUSDT", date(2024, 6, 2)),
        ("symbol_day", "ETHUSDT", date(2024, 6, 1)),
    ]


def test_the_chunk_batch_id_names_the_selection_and_the_chunk() -> None:
    assert dataset_chunk_batch_id(SELECTION, 7) == f"{SELECTION}.chunk-0000000007"
    assert proof(7).selection_id == SELECTION
    assert re.fullmatch(BATCH_ID_PATTERN, proof(7).batch_id)
    longest = "s" * 239
    batch = dataset_chunk_batch_id(longest, DATASET_CHUNK_INDEX_MAX)
    assert len(batch) == 256 and re.fullmatch(BATCH_ID_PATTERN, batch)


@pytest.mark.parametrize(
    ("selection_id", "chunk_index"),
    [("s" * 240, 0), ("bad/selection", 0), ("", 0), (SELECTION, -1), (SELECTION, True)],
)
def test_an_unusable_chunk_batch_id_is_refused(selection_id: str, chunk_index: Any) -> None:
    with pytest.raises(ValueError):
        dataset_chunk_batch_id(selection_id, chunk_index)
    with pytest.raises(ValueError):
        dataset_chunk_batch_id(SELECTION, DATASET_CHUNK_INDEX_MAX + 1)


@pytest.mark.parametrize(
    "batch_id",
    [
        f"{SELECTION}.chunk-0000000001",  # another chunk
        f"{SELECTION}.chunk-000000000",  # nine digits
        f"{SELECTION}.chunk-00000000000",  # eleven digits
        f"{SELECTION}-chunk-0000000000",
        ".chunk-0000000000",  # no selection
        SELECTION,
    ],
)
def test_a_proof_whose_batch_id_does_not_name_its_chunk_is_refused(batch_id: str) -> None:
    with pytest.raises(ValidationError):
        proof(0, batch_id=batch_id)


@pytest.mark.parametrize(
    "field",
    [
        {"row_count": 0},
        {"first_row_ordinal": -1},
        {"batch_fingerprint": "F" * 64},
        {"snapshot_id": "bad snapshot"},
    ],
)
def test_proof_shape_is_checked(field: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        proof(0, **field)


# ======================================================================================
# ResearchDatasetEvidenceManifest（§1）
# ======================================================================================


def test_a_valid_v3_manifest_is_fixed_size_and_canonically_ordered() -> None:
    built = v3()
    assert [item.stream.value for item in built.evidence] == sorted(
        stream.value for stream in EvidenceStream if stream is not EvidenceStream.PIT_CONFLICTS
    )
    shuffled = v3(evidence=tuple(at_version(item, "2.4.0") for item in reversed(evidence(3))))
    assert shuffled == built
    assert shuffled.content_hash() == built.content_hash()
    assert built.evidence_for(EvidenceStream.CHUNK_PROOFS).record_count == 3
    assert [built.chunk_batch_id(i) for i in range(3)] == [
        dataset_chunk_batch_id(SELECTION, i) for i in range(3)
    ]
    for bad in (-1, 3, True):
        with pytest.raises(ValueError):
            built.chunk_batch_id(bad)


@pytest.mark.parametrize(
    ("rows", "chunk_rows", "chunks"),
    [(1, 1000, 1), (1000, 1000, 1), (1001, 1000, 2), (3000, 7, 429)],
)
def test_chunk_count_is_the_ceiling_of_rows_over_chunk_rows(
    rows: int, chunk_rows: int, chunks: int
) -> None:
    assert v3(row_count=rows, chunk_rows=chunk_rows, chunk_count=chunks).chunk_count == chunks
    with pytest.raises(ValidationError, match="ceil"):
        v3(
            row_count=rows,
            chunk_rows=chunk_rows,
            chunk_count=chunks + 1,
            evidence=evidence(chunks + 1),
        )


def test_an_empty_selection_is_refused() -> None:
    with pytest.raises(ValidationError):
        v3(row_count=0, chunk_count=0, evidence=evidence(0))


def test_the_chunk_proof_stream_must_count_every_chunk() -> None:
    with pytest.raises(ValidationError, match="chunk_proofs"):
        v3(evidence=evidence(3, chunk_proofs=2))


@pytest.mark.parametrize("stream", ["lineage", "quality_reports"])
def test_lineage_and_quality_report_streams_are_never_empty(stream: str) -> None:
    with pytest.raises(ValidationError, match=stream):
        v3(evidence=evidence(3, **{stream: 0}))


def test_members_exclusions_and_gaps_may_be_empty() -> None:
    built = v3(evidence=evidence(3, members=0, exclusions=0, evidence_gaps=0))
    assert built.evidence_for(EvidenceStream.MEMBERS).record_count == 0


def test_every_stream_appears_exactly_once() -> None:
    complete = evidence(3)
    duplicated = (*complete[:5], stream_ref(EvidenceStream.MEMBERS, 2))
    with pytest.raises(ValidationError, match="重复"):
        v3(evidence=duplicated)
    with pytest.raises(ValidationError):
        v3(evidence=complete[:5])
    with pytest.raises(ValidationError):
        v3(evidence=(*complete, stream_ref(EvidenceStream.MEMBERS, 2)))


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"dataset": dataset(zone=Zone.CANONICAL)}, "research_dataset"),
        ({"dataset": dataset(table="dataset_without_namespace")}, "namespace.table"),
        ({"dataset": dataset(table=LISTINGS_TABLE)}, "上游"),
        (
            {"point_in_time": pit(snapshot_bindings={QUALITY_REPORTS_TABLE: "7002"})},
            LISTINGS_TABLE,
        ),
        (
            {"point_in_time": pit(snapshot_bindings={LISTINGS_TABLE: "7001"})},
            QUALITY_REPORTS_TABLE,
        ),
    ],
)
def test_the_v2_dataset_and_upstream_rules_hold_for_v3(
    overrides: dict[str, Any], message: str
) -> None:
    with pytest.raises(ValidationError, match=message):
        v3(**overrides)


@pytest.mark.parametrize(
    "field",
    [
        {"data_type": "Klines"},
        {"data_type": ""},
        {"selection_id": "s" * 240},
        {"selection_id": "bad/selection"},
        {"chunk_rows": 0},
        {"rule": {"rule_id": "Bad", "version": "2.0.0", "rule_hash": HASH}},
        {"rule": {"rule_id": RULE_ID, "version": "2", "rule_hash": HASH}},
    ],
)
def test_manifest_field_shapes_are_checked(field: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        v3(**field)


def test_every_field_is_required() -> None:
    payload = wire(v3())
    for name in ResearchDatasetEvidenceManifest.model_fields:
        if name == "schema_version":
            continue
        with pytest.raises(ValidationError):
            ResearchDatasetEvidenceManifest.model_validate(
                {key: value for key, value in payload.items() if key != name}
            )


# ======================================================================================
# 内容哈希：确定、承诺每个根、不含 uri
# ======================================================================================


def test_the_v3_hash_is_deterministic_across_rebuilds_and_json_round_trips() -> None:
    built = v3()
    payload = wire(built)
    reordered = dict(reversed(list(payload.items())))
    again = (
        v3(),
        ResearchDatasetEvidenceManifest.model_validate(payload),
        ResearchDatasetEvidenceManifest.model_validate(reordered),
        ResearchDatasetEvidenceManifest.model_validate_json(built.model_dump_json()),
    )
    assert {item.content_hash() for item in again} == {built.content_hash()}
    assert built.content_hash() == content_hash(built.model_dump(mode="json"))
    assert canonical_json(again[1].model_dump(mode="json")) == canonical_json(payload)


@pytest.mark.parametrize(("old", "expected_hash"), LEGACY_V3_MANIFEST_SHA256.items())
def test_legacy_v3_manifest_goldens_replay_at_their_recorded_version(
    old: str, expected_hash: str
) -> None:
    path = REPO / "tests" / "golden" / f"v{old.replace('.', '_')}" / "dataset_v3_manifest.json"
    original = path.read_bytes().rstrip(b"\n")
    assert hashlib.sha256(original).hexdigest() == expected_hash

    replayed = v3(schema_version=old)
    assert replayed.schema_version == old
    assert canonical_json(replayed.model_dump(mode="json")).encode("utf-8") == original
    assert replayed.content_hash() == expected_hash
    parsed = ResearchDatasetEvidenceManifest.model_validate_json(original)
    assert parsed.schema_version == old and parsed.content_hash() == expected_hash


def test_the_v3_hash_commits_every_root_count_and_binding() -> None:
    base = v3().content_hash()
    variants = [
        v3(selection_id=SELECTION[:-1] + "b"),
        v3(data_type="agg_trades"),
        v3(rule=rule(rule_hash="2" * 64)),
        v3(rule=rule(version="2.0.1")),
        v3(dataset=dataset(table="research.dataset_selection_chunks", snapshot_id="9104")),
        v3(row_count=2600),
        v3(evidence=evidence(3, members=4)),
        v3(
            evidence=tuple(
                stream_ref(item.stream, item.record_count, root=obj(99))
                if item.stream is EvidenceStream.LINEAGE
                else item
                for item in evidence(3)
            )
        ),
        v3(
            evidence=tuple(
                stream_ref(item.stream, item.record_count, root=obj(3, size=129))
                if item.stream is EvidenceStream.LINEAGE
                else item
                for item in evidence(3)
            )
        ),
    ]
    hashes = {item.content_hash() for item in variants}
    assert base not in hashes and len(hashes) == len(variants)


def test_the_new_record_hashes_are_deterministic() -> None:
    for instance in (report(), report("listing"), proof(2), rule(), obj(3)):
        model = type(instance)
        rebuilt = model.model_validate_json(instance.model_dump_json())
        assert rebuilt == instance and rebuilt.content_hash() == instance.content_hash()
        assert instance.content_hash() == content_hash(instance.model_dump(mode="json"))


# ======================================================================================
# v2 `ResearchDatasetManifest` 逐位不变；v2 / v3 并存读取
# ======================================================================================


def test_the_v2_manifest_fields_are_unchanged() -> None:
    assert list(ResearchDatasetManifest.model_fields) == [
        "schema_version",
        "dataset",
        "point_in_time",
        "universe_spec",
        "members",
        "exclusions",
        "lineage",
        "quality_report_ids",
        "evidence_gaps",
    ]


@pytest.mark.parametrize("name", sorted(V2_SCHEMA_SHA256_AT_2_2_0))
def test_the_v2_schemas_change_only_their_envelope_default(name: str, tmp_path: Path) -> None:
    committed = (CURRENT_SCHEMA_DIR / f"{name}.schema.json").read_bytes()
    regenerated = export_json_schemas(tmp_path)[name].read_bytes()
    assert committed == regenerated
    for schema in (committed, regenerated):
        digest = hashlib.sha256(as_published_at(schema, "2.2.0")).hexdigest()
        assert digest == V2_SCHEMA_SHA256_AT_2_2_0[name]


def test_a_persisted_2_2_0_v2_manifest_reads_bit_for_bit_with_its_pinned_hash() -> None:
    assert hashlib.sha256(V2_MANIFEST_AT_2_2_0.encode("utf-8")).hexdigest() == (
        V2_MANIFEST_AT_2_2_0_HASH
    )
    payload = json.loads(V2_MANIFEST_AT_2_2_0)
    for read in (
        ResearchDatasetManifest.model_validate(payload),
        ResearchDatasetManifest.model_validate_json(V2_MANIFEST_AT_2_2_0),
    ):
        assert read.schema_version == "2.2.0"  # the recorded envelope is never rewritten
        assert canonical_json(read.model_dump(mode="json")) == V2_MANIFEST_AT_2_2_0
        assert read.content_hash() == V2_MANIFEST_AT_2_2_0_HASH


def test_a_v2_manifest_built_now_is_a_2_3_0_object_and_its_twins_keep_their_envelopes() -> None:
    current = manifest()
    assert current.schema_version == "2.5.0"  # the current envelope (ADR-0094)
    for old in ("2.0.0", "2.1.0", "2.2.0"):
        twin = at_version(current, old)
        assert twin.schema_version == old
        assert twin.content_hash() != current.content_hash()  # the envelope is hashed
        assert ResearchDatasetManifest.model_validate(wire(twin)).content_hash() == (
            twin.content_hash()
        )


def test_v2_and_v3_manifests_are_read_side_by_side_and_never_confused() -> None:
    old = ResearchDatasetManifest.model_validate_json(V2_MANIFEST_AT_2_2_0)
    new = v3()
    assert (old.schema_version, new.schema_version) == ("2.2.0", "2.4.0")
    with pytest.raises(ValidationError):
        ResearchDatasetEvidenceManifest.model_validate(json.loads(V2_MANIFEST_AT_2_2_0))
    with pytest.raises(ValidationError):
        ResearchDatasetManifest.model_validate(wire(new))
    # each is rebuilt inside its own recorded-version scope (ADR-0052 V7)
    with contract_schema_version_scope("2.2.0"):
        rebuilt_old = ResearchDatasetManifest.model_validate(
            json.loads(V2_MANIFEST_AT_2_2_0.replace('"schema_version":"2.2.0",', ""))
        )
    assert rebuilt_old.content_hash() == V2_MANIFEST_AT_2_2_0_HASH
    with contract_schema_version_scope(new.schema_version):
        rebuilt_new = ResearchDatasetEvidenceManifest.model_validate(wire(new))
    assert rebuilt_new.content_hash() == new.content_hash()


def test_the_v3_manifest_reuses_the_v2_pit_spec_unchanged() -> None:
    spec = PointInTimeSpec.model_validate(json.loads(V2_MANIFEST_AT_2_2_0)["point_in_time"])
    built = v3(point_in_time=spec)
    assert built.point_in_time.schema_version == "2.2.0"  # a nested recorded object is kept
    assert built.point_in_time.content_hash() == spec.content_hash()

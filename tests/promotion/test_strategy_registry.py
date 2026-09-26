"""File-backed Strategy Registry (``infrastructure.registry``, ADR-0005): append-only, hash-chained;
duplicate / tampered / truncated / unknown / dangling records refused; nothing edited or removed."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from core.domain.artifact import StrategyArtifact
from core.domain.base import Kind, Ref
from infrastructure.event_bus.journal import AppendOnlyJournal
from infrastructure.registry import (
    DuplicateRecord,
    RegistryCorrupted,
    RegistryLocked,
    RegistryRefused,
    StrategyRegistry,
    UnknownArtifact,
)
from research.promotion import build_artifact, promote
from tests.factories import HASH_A, deployment_record, equivalence_check
from tests.promotion.fixtures import toy_evidence

OTHER_STRATEGY = Ref(kind=Kind.STRATEGY, name="someone_else", version="1.0.0")


def _journal(root: Path) -> Path:
    return root / "registry.jsonl"


def _promoted(root: Path) -> StrategyArtifact:
    with StrategyRegistry(root) as registry:
        return promote(toy_evidence(), registry)


def _rewrite_line(path: Path, index: int, mutate: object) -> None:
    lines = path.read_text(encoding="utf-8").splitlines()
    record = json.loads(lines[index])
    assert callable(mutate)
    mutate(record)
    lines[index] = json.dumps(record, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def test_records_replay_after_reopening(tmp_path: Path) -> None:
    root = tmp_path / "registry"
    artifact = _promoted(root)
    check = equivalence_check(artifact_id=artifact.artifact_id)
    record = deployment_record(equivalence=check)
    with StrategyRegistry(root) as registry:
        registry.record_equivalence(check)
        registry.record_deployment(record)
        head = registry.head_hash
    with StrategyRegistry(root) as reopened:
        assert len(reopened) == 3
        assert reopened.head_hash == head
        assert reopened.artifacts() == (artifact,)
        assert reopened.equivalence_checks(artifact.artifact_id) == (check,)
        assert reopened.deployments(artifact.artifact_id) == (record,)
        assert reopened.deployments(HASH_A) == ()


def test_the_registry_has_no_edit_or_remove_operation() -> None:
    public = {name for name in dir(StrategyRegistry) if not name.startswith("_")}
    for verb in ("delete", "remove", "update", "edit", "replace", "overwrite", "pop", "clear"):
        assert not any(verb in name for name in public), public


def test_duplicates_are_refused_and_nothing_is_written(tmp_path: Path) -> None:
    root = tmp_path / "registry"
    artifact = _promoted(root)
    check = equivalence_check(artifact_id=artifact.artifact_id)
    record = deployment_record(equivalence=check)
    with StrategyRegistry(root) as registry:
        with pytest.raises(DuplicateRecord):
            registry.register_artifact(artifact)
        registry.record_equivalence(check)
        with pytest.raises(DuplicateRecord):
            registry.record_equivalence(check)
        registry.record_deployment(record)
        with pytest.raises(DuplicateRecord):
            registry.record_deployment(record)
        assert len(registry) == 3
    assert len(_journal(root).read_text(encoding="utf-8").splitlines()) == 3


def test_dangling_references_are_refused(tmp_path: Path) -> None:
    root = tmp_path / "registry"
    artifact = _promoted(root)
    with StrategyRegistry(root) as registry:
        with pytest.raises(UnknownArtifact):
            registry.record_equivalence(equivalence_check(artifact_id=HASH_A))
        with pytest.raises(UnknownArtifact):
            registry.record_deployment(deployment_record(equivalence=equivalence_check()))
        unrecorded = equivalence_check(artifact_id=artifact.artifact_id)
        with pytest.raises(RegistryRefused, match="recorded first"):
            registry.record_deployment(deployment_record(equivalence=unrecorded))
        with pytest.raises(UnknownArtifact):
            registry.artifact(HASH_A)
        assert len(registry) == 1


def test_an_artifact_without_its_golden_blobs_is_refused(tmp_path: Path) -> None:
    package = build_artifact(toy_evidence())
    with StrategyRegistry(tmp_path / "registry") as registry:
        with pytest.raises(RegistryRefused, match="golden signals blob"):
            registry.register_artifact(package.artifact)
        registry.put_blob(package.signals_payload)
        with pytest.raises(RegistryRefused, match="golden positions blob"):
            registry.register_artifact(package.artifact)
        registry.put_blob(package.positions_payload)
        registry.register_artifact(package.artifact)
        assert len(registry) == 1


def test_an_artifact_whose_golden_data_answers_another_strategy_is_refused(
    tmp_path: Path,
) -> None:
    package = build_artifact(toy_evidence())
    payload = package.artifact.model_dump(mode="json")
    payload["strategy_spec"] = OTHER_STRATEGY.model_dump(mode="json")
    payload["dependencies"] = {**payload["dependencies"], str(OTHER_STRATEGY): HASH_A}
    other = StrategyArtifact.model_validate(payload)
    with StrategyRegistry(tmp_path / "registry") as registry:
        registry.put_blob(package.signals_payload)
        registry.put_blob(package.positions_payload)
        with pytest.raises(RegistryRefused, match="another strategy"):
            registry.register_artifact(other)
        assert len(registry) == 0


def test_a_tampered_record_is_refused_on_open(tmp_path: Path) -> None:
    root = tmp_path / "registry"
    _promoted(root)

    def tamper(record: dict[str, object]) -> None:
        payload = record["payload"]
        assert isinstance(payload, dict)
        payload["artifact"]["validation_reports"] = ["TEST-ONLY-forged"]

    _rewrite_line(_journal(root), 0, tamper)
    with pytest.raises(RegistryCorrupted, match="content hash"):
        StrategyRegistry(root)


def test_a_rechained_forgery_still_breaks_the_registry_rules(tmp_path: Path) -> None:
    """Even with a valid hash chain, a record whose identity is not its content is refused."""
    root = tmp_path / "registry"
    artifact = _promoted(root)
    forged = AppendOnlyJournal(_journal(root))
    payload = dict(forged.entry(0).payload)
    forged.append(
        "equivalence.recorded",
        {
            "check_hash": HASH_A,
            "check": json.loads(
                equivalence_check(artifact_id=artifact.artifact_id).model_dump_json()
            ),
        },
    )
    with pytest.raises(RegistryCorrupted, match="check_hash"):
        StrategyRegistry(root)
    assert payload["artifact_id"] == artifact.artifact_id


@pytest.mark.parametrize(
    ("type_", "payload", "match"),
    [
        ("artifact.deleted", {"artifact_id": HASH_A}, "unknown record type"),
        ("equivalence.recorded", {"check_hash": HASH_A}, "exactly the keys"),
    ],
)
def test_unknown_or_malformed_records_are_refused_on_open(
    tmp_path: Path, type_: str, payload: dict[str, object], match: str
) -> None:
    root = tmp_path / "registry"
    _promoted(root)
    AppendOnlyJournal(_journal(root)).append(type_, payload)
    with pytest.raises(RegistryCorrupted, match=match):
        StrategyRegistry(root)


def test_a_duplicate_record_in_the_file_is_refused_on_open(tmp_path: Path) -> None:
    root = tmp_path / "registry"
    _promoted(root)
    journal = AppendOnlyJournal(_journal(root))
    journal.append(journal.entry(0).type, journal.entry(0).payload)
    with pytest.raises(RegistryCorrupted, match="already registered"):
        StrategyRegistry(root)


def test_a_partial_trailing_line_is_refused(tmp_path: Path) -> None:
    root = tmp_path / "registry"
    _promoted(root)
    text = _journal(root).read_text(encoding="utf-8")
    _journal(root).write_text(text[:-10], encoding="utf-8")
    with pytest.raises(RegistryCorrupted, match="partial trailing line"):
        StrategyRegistry(root)


def test_whole_line_truncation_needs_the_anchor(tmp_path: Path) -> None:
    root, anchor = tmp_path / "registry", tmp_path / "anchor.jsonl"
    with StrategyRegistry(root, anchor=anchor) as registry:
        artifact = promote(toy_evidence(), registry)
        registry.record_equivalence(equivalence_check(artifact_id=artifact.artifact_id))
    lines = _journal(root).read_text(encoding="utf-8").splitlines(keepends=True)
    _journal(root).write_text("".join(lines[:1]), encoding="utf-8")
    # honest boundary: without the anchor a shorter valid chain opens
    with StrategyRegistry(root) as unanchored:
        assert len(unanchored) == 1
    with pytest.raises(RegistryCorrupted, match="records were removed"):
        StrategyRegistry(root, anchor=anchor)


def test_the_anchor_detects_a_rewritten_history_and_heals_one_crash_record(
    tmp_path: Path,
) -> None:
    root, anchor = tmp_path / "registry", tmp_path / "anchor.jsonl"
    with StrategyRegistry(root, anchor=anchor) as registry:
        artifact = promote(toy_evidence(), registry)
    # one record written without its anchor line: the legitimate crash window
    with StrategyRegistry(root) as registry:
        registry.record_equivalence(equivalence_check(artifact_id=artifact.artifact_id))
    with StrategyRegistry(root, anchor=anchor) as healed:
        assert len(healed) == 2
    assert len(anchor.read_text(encoding="utf-8").splitlines()) == 2
    # two records written without the anchor are not a crash window
    with StrategyRegistry(root) as registry:
        registry.record_equivalence(
            equivalence_check(artifact_id=artifact.artifact_id, positions_match=False)
        )
        registry.record_equivalence(
            equivalence_check(artifact_id=artifact.artifact_id, signals_match=False)
        )
    with pytest.raises(RegistryCorrupted, match="without the anchor"):
        StrategyRegistry(root, anchor=anchor)


def test_the_anchor_must_live_outside_the_registry(tmp_path: Path) -> None:
    root = tmp_path / "registry"
    with pytest.raises(ValueError, match="outside"):
        StrategyRegistry(root, anchor=root / "anchor.jsonl")


def test_a_single_writer_holds_the_registry(tmp_path: Path) -> None:
    root = tmp_path / "registry"
    first = StrategyRegistry(root)
    with pytest.raises(RegistryLocked):
        StrategyRegistry(root)
    first.close()
    with StrategyRegistry(root) as second:
        assert len(second) == 0
    with pytest.raises(Exception, match="closed"):
        first.put_blob({"x": 1})


def test_a_tampered_golden_blob_is_refused_on_read(tmp_path: Path) -> None:
    root = tmp_path / "registry"
    artifact = _promoted(root)
    digest = artifact.golden_outputs.signals_hash
    blob = root / "blobs" / f"{digest}.json"
    blob.write_bytes(blob.read_bytes().replace(b"BTCUSDT", b"BTCUSDX"))
    with StrategyRegistry(root) as registry:
        assert registry.artifact(artifact.artifact_id) == artifact  # blobs are not read on replay
        with pytest.raises(RegistryCorrupted, match="does not hash to its name"):
            registry.golden_inputs(artifact.artifact_id)

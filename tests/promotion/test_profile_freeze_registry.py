"""ADR-0062: the file-backed, append-only Validation Profile freeze registry (B56).

Every registry here is a temporary directory with a temporary sibling anchor file; every Profile
and calibration report is TEST ONLY (``tests.promotion.fixtures``) — nothing is calibrated and no
Profile value is chosen.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from core.contracts.validation_profile import ProfileStatus, ValidationProfile
from core.domain.base import canonical_json, content_hash
from infrastructure.event_bus.journal import AppendOnlyJournal
from infrastructure.registry import (
    DuplicateRecord,
    FreezeConflict,
    ProfileFreezeRegistry,
    RegistryCorrupted,
    RegistryError,
    RegistryLocked,
    RegistryRefused,
)
from infrastructure.registry.profile_freeze import PROFILE_FROZEN
from tests.promotion.fixtures import toy_calibration_report, toy_profile

APPROVED_AT = datetime(2026, 9, 27, 8, 0, tzinfo=UTC)
APPROVER = "TEST-ONLY approver"


@pytest.fixture
def paths(tmp_path: Path) -> tuple[Path, Path]:
    """A temporary registry directory and its temporary sibling anchor file."""
    return tmp_path / "freezes", tmp_path / "freezes.anchor.jsonl"


def _open(paths: tuple[Path, Path]) -> ProfileFreezeRegistry:
    root, anchor = paths
    return ProfileFreezeRegistry(root, anchor=anchor)


def _frozen(
    note: str = "TEST ONLY", name: str = "test_scope"
) -> tuple[ValidationProfile, bytes, str]:
    """A FROZEN TEST ONLY Profile citing a TEST ONLY calibration report, and the report."""
    report, report_hash = toy_calibration_report(note)
    return toy_profile(calibration_report=report_hash, name=name), report, report_hash


def _register(
    paths: tuple[Path, Path], note: str = "TEST ONLY", name: str = "test_scope"
) -> ValidationProfile:
    """Freeze a TEST ONLY Profile named ``name`` (distinct names give distinct refs)."""
    profile, report, _ = _frozen(note, name=name)
    with _open(paths) as registry:
        registry.register_freeze(profile, report, approved_by=APPROVER, approved_at=APPROVED_AT)
    return profile


def _journal(paths: tuple[Path, Path]) -> Path:
    return paths[0] / "freezes.jsonl"


def _payloads(paths: tuple[Path, Path]) -> list[dict[str, Any]]:
    return [dict(entry.payload) for entry in AppendOnlyJournal(_journal(paths)).entries]


def _forge(paths: tuple[Path, Path], records: list[tuple[str, Mapping[str, Any]]]) -> None:
    """Rewrite the registry as a **valid chain** of ``records`` with a matching anchor, so only the
    registry's own rules can refuse it (the journal and anchor checks pass)."""
    root, anchor = paths
    _journal(paths).unlink()
    anchor.unlink()
    journal = AppendOnlyJournal(_journal(paths))
    heads = AppendOnlyJournal(anchor)
    for type_, payload in records:
        journal.append(type_, payload)
        heads.append("profile_freeze.head", {"length": len(journal), "head": journal.head_hash})
    assert root.is_dir()


def _reidentified(payload: Mapping[str, Any]) -> dict[str, Any]:
    """``payload`` with ``freeze_id`` recomputed, so a mutation is not caught by the id alone."""
    body = {key: value for key, value in payload.items() if key != "freeze_id"}
    return {**body, "freeze_id": content_hash(body)}


# ======================================================================================
# register, replay, read
# ======================================================================================


def test_a_freeze_is_recorded_and_replayed(paths: tuple[Path, Path]) -> None:
    profile, report, report_hash = _frozen()
    with _open(paths) as registry:
        record = registry.register_freeze(
            profile, report, approved_by=APPROVER, approved_at=APPROVED_AT
        )
        assert len(registry) == 1
    assert record.profile_ref == str(profile.ref)
    assert record.profile_hash == profile.content_hash()
    assert record.report_hash == report_hash == profile.provenance.calibration_report
    assert record.report_sha256 == hashlib.sha256(report).hexdigest()
    assert (record.approved_by, record.approved_at) == (APPROVER, APPROVED_AT)
    # the calibration report's original bytes are stored write-once, unchanged
    assert (paths[0] / "blobs" / f"{record.report_sha256}.json").read_bytes() == report
    with _open(paths) as again:  # a fresh process: full replay and verification
        assert again.freezes == (record,)
        assert again.frozen_record(profile) == record
        assert again.freeze_of(profile.ref) == record
    [payload] = _payloads(paths)
    assert set(payload) == {
        "format_version",
        "freeze_id",
        "profile",
        "profile_ref",
        "profile_hash",
        "calibration",
        "approved_by",
        "approved_at",
    }
    assert payload["format_version"] == "1.0.0"
    assert payload["approved_at"] == "2026-09-27T08:00:00Z"
    assert payload["freeze_id"] == record.freeze_id


def test_an_approval_time_in_another_zone_is_recorded_in_utc(paths: tuple[Path, Path]) -> None:
    profile, report, _ = _frozen()
    local = APPROVED_AT.astimezone(timezone(timedelta(hours=3)))
    with _open(paths) as registry:
        record = registry.register_freeze(profile, report, approved_by=APPROVER, approved_at=local)
    assert record.approved_at == APPROVED_AT
    assert _payloads(paths)[0]["approved_at"] == "2026-09-27T08:00:00Z"


def test_frozen_record_binds_ref_hash_and_cited_report(paths: tuple[Path, Path]) -> None:
    profile = _register(paths)
    other_hash, _, _ = _frozen("TEST ONLY another report")  # same ref, other provenance -> hash
    with _open(paths) as registry:
        assert registry.frozen_record(profile) is not None
        assert registry.frozen_record(other_hash) is None
        assert registry.frozen_record(toy_profile(name="test_only_other_profile")) is None
        # status is not in the content hash (ADR-0008): the record answers for the content; the
        # caller (Promotion) still requires the object's own status to be FROZEN
        draft_twin = toy_profile(
            status=ProfileStatus.DRAFT, calibration_report=profile.provenance.calibration_report
        )
        assert draft_twin.content_hash() == profile.content_hash()


# ======================================================================================
# the anchor is mandatory
# ======================================================================================


def test_the_anchor_is_mandatory_and_outside_the_registry(tmp_path: Path) -> None:
    root = tmp_path / "freezes"
    with pytest.raises(TypeError):
        ProfileFreezeRegistry(root)  # type: ignore[call-arg]
    with pytest.raises(ValueError, match="anchor"):
        ProfileFreezeRegistry(root, anchor=None)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="outside"):
        ProfileFreezeRegistry(root, anchor=root / "anchor.jsonl")


def test_whole_line_truncation_is_detected_by_the_anchor(paths: tuple[Path, Path]) -> None:
    _register(paths, "first", name="test_only_first")
    _register(paths, "second", name="test_only_second")
    lines = _journal(paths).read_bytes().splitlines(keepends=True)
    assert len(lines) == 2
    _journal(paths).write_bytes(lines[0])  # a valid, shorter chain
    AppendOnlyJournal(_journal(paths))  # the journal alone cannot tell
    with pytest.raises(RegistryCorrupted, match="removed"):
        _open(paths)


def test_a_rewritten_history_is_detected_by_the_anchor(paths: tuple[Path, Path]) -> None:
    _register(paths, "first")
    root, anchor = paths
    saved_anchor = anchor.read_bytes()
    _journal(paths).unlink()
    for blob in (root / "blobs").iterdir():
        blob.unlink()
    anchor.unlink()
    _register(paths, "another first record")  # a different history of the same length
    anchor.write_bytes(saved_anchor)
    with pytest.raises(RegistryCorrupted, match="not the history its anchor saw"):
        _open(paths)


def test_records_written_without_the_anchor_are_refused(paths: tuple[Path, Path]) -> None:
    _register(paths, "first")
    root, anchor = paths
    saved_anchor = anchor.read_bytes()
    _register(paths, "second", name="test_only_second")
    _register(paths, "third", name="test_only_third")
    anchor.write_bytes(saved_anchor)  # the anchor saw 1 record, the registry now has 3
    with pytest.raises(RegistryCorrupted, match="without the anchor"):
        _open(paths)


def test_the_one_crash_window_is_re_anchored(paths: tuple[Path, Path]) -> None:
    _register(paths, "first")
    root, anchor = paths
    saved_anchor = anchor.read_bytes()
    _register(paths, "second", name="test_only_second")
    anchor.write_bytes(saved_anchor)  # crash: the record is fsync'd, its anchor line is not
    with _open(paths) as registry:
        assert len(registry) == 2
    heads = AppendOnlyJournal(anchor).entries
    assert [entry.payload["length"] for entry in heads] == [1, 2]
    with _open(paths) as registry:
        assert len(registry) == 2


def test_a_corrupt_anchor_is_refused(paths: tuple[Path, Path]) -> None:
    _register(paths)
    anchor = paths[1]
    anchor.write_bytes(anchor.read_bytes().replace(b'"length":1', b'"length":2'))
    with pytest.raises(RegistryCorrupted, match="anchor"):
        _open(paths)


# ======================================================================================
# crash tail / tampering (the journal)
# ======================================================================================


def test_a_partial_trailing_line_is_refused(paths: tuple[Path, Path]) -> None:
    _register(paths)
    with _journal(paths).open("ab") as handle:
        handle.write(b'{"seq": 2, "type": "profile.frozen"')  # a crash in the middle of a write
    with pytest.raises(RegistryCorrupted, match="partial trailing line"):
        _open(paths)


def test_a_tampered_record_breaks_the_chain(paths: tuple[Path, Path]) -> None:
    _register(paths)
    text = _journal(paths).read_text(encoding="utf-8")
    _journal(paths).write_text(text.replace(APPROVER, "TEST-ONLY someone else"), "utf-8")
    with pytest.raises(RegistryCorrupted, match="hash"):
        _open(paths)


# ======================================================================================
# duplicate / conflict
# ======================================================================================


def test_a_duplicate_freeze_is_refused_and_writes_nothing(paths: tuple[Path, Path]) -> None:
    profile, report, _ = _frozen()
    with _open(paths) as registry:
        registry.register_freeze(profile, report, approved_by=APPROVER, approved_at=APPROVED_AT)
        before = (_journal(paths).read_bytes(), paths[1].read_bytes())
        with pytest.raises(DuplicateRecord):
            registry.register_freeze(
                profile, report, approved_by="TEST-ONLY another approver", approved_at=APPROVED_AT
            )
        assert len(registry) == 1
    assert (_journal(paths).read_bytes(), paths[1].read_bytes()) == before


def test_the_same_ref_under_another_hash_is_a_conflict(paths: tuple[Path, Path]) -> None:
    _register(paths, "first")
    other, report, _ = _frozen("TEST ONLY a different report")
    with _open(paths) as registry:
        with pytest.raises(FreezeConflict, match="already frozen as"):
            registry.register_freeze(other, report, approved_by=APPROVER, approved_at=APPROVED_AT)
        assert len(registry) == 1


def test_a_replayed_duplicate_is_corruption(paths: tuple[Path, Path]) -> None:
    _register(paths, "first")
    [first] = _payloads(paths)
    _forge(paths, [(PROFILE_FROZEN, first), (PROFILE_FROZEN, first)])
    with pytest.raises(RegistryCorrupted, match=r"\) is already frozen"):
        _open(paths)


def test_a_replayed_conflict_is_corruption(tmp_path: Path) -> None:
    """Two records freezing one ref under two hashes (each valid alone) cannot be replayed."""
    first_paths = (tmp_path / "a", tmp_path / "a.anchor.jsonl")
    second_paths = (tmp_path / "b", tmp_path / "b.anchor.jsonl")
    _register(first_paths, "first")
    _register(second_paths, "a different report")
    [first], [second] = _payloads(first_paths), _payloads(second_paths)
    assert first["profile_ref"] == second["profile_ref"]
    assert first["profile_hash"] != second["profile_hash"]
    for blob in (second_paths[0] / "blobs").iterdir():  # both calibration reports are present
        (first_paths[0] / "blobs" / blob.name).write_bytes(blob.read_bytes())
    _forge(first_paths, [(PROFILE_FROZEN, first), (PROFILE_FROZEN, second)])
    with pytest.raises(RegistryCorrupted, match="is already frozen as"):
        _open(first_paths)


# ======================================================================================
# wrong Profile / wrong calibration evidence / approver / time (on register)
# ======================================================================================


def test_a_non_frozen_profile_is_refused(paths: tuple[Path, Path]) -> None:
    report, report_hash = toy_calibration_report()
    draft = toy_profile(status=ProfileStatus.DRAFT, calibration_report=report_hash)
    with _open(paths) as registry, pytest.raises(RegistryRefused, match="not frozen"):
        registry.register_freeze(draft, report, approved_by=APPROVER, approved_at=APPROVED_AT)


def test_a_forged_frozen_profile_without_calibration_is_refused(paths: tuple[Path, Path]) -> None:
    report, _ = toy_calibration_report()
    real = toy_profile(status=ProfileStatus.DRAFT, calibration_report=None)
    forged = ValidationProfile.model_construct(**{**dict(real), "status": ProfileStatus.FROZEN})
    with _open(paths) as registry, pytest.raises(RegistryRefused, match="validate"):
        registry.register_freeze(forged, report, approved_by=APPROVER, approved_at=APPROVED_AT)


def test_a_profile_citing_another_report_is_refused(paths: tuple[Path, Path]) -> None:
    profile, _, _ = _frozen("TEST ONLY the cited report")
    other_report, _ = toy_calibration_report("TEST ONLY a different report")
    with _open(paths) as registry, pytest.raises(RegistryRefused, match="cites calibration"):
        registry.register_freeze(
            profile, other_report, approved_by=APPROVER, approved_at=APPROVED_AT
        )
    assert not (paths[0] / "blobs").exists() or not any((paths[0] / "blobs").iterdir())


def _report_bytes(mutate: Callable[[dict[str, Any]], None], *, rehash: bool) -> bytes:
    report = json.loads(toy_calibration_report()[0])
    mutate(report)
    if rehash:
        body = {key: value for key, value in report.items() if key != "report_hash"}
        report["report_hash"] = content_hash(body)
    return canonical_json(report).encode("utf-8")


@pytest.mark.parametrize(
    ("report", "match"),
    [
        (_report_bytes(lambda r: r.update(kind="validation_report"), rehash=True), "kind"),
        (_report_bytes(lambda r: r.update(note="edited after hashing"), rehash=False), "content"),
        (_report_bytes(lambda r: r.pop("report_hash"), rehash=False), "report_hash"),
        (json.dumps(json.loads(toy_calibration_report()[0]), indent=2).encode(), "canonical"),
        (b"not json", "JSON"),
        (b"[]", "object"),
        ("a str, not bytes", "bytes"),
    ],
)
def test_wrong_calibration_evidence_is_refused(
    paths: tuple[Path, Path], report: bytes, match: str
) -> None:
    try:
        cited = json.loads(report)["report_hash"]
    except (ValueError, TypeError, KeyError):
        cited = toy_calibration_report()[1]
    profile = toy_profile(calibration_report=str(cited))
    with _open(paths) as registry, pytest.raises(RegistryRefused, match=match):
        registry.register_freeze(profile, report, approved_by=APPROVER, approved_at=APPROVED_AT)
        assert len(registry) == 0


@pytest.mark.parametrize("approver", ["", "   ", " TEST-ONLY padded ", None])
def test_an_unnamed_approver_is_refused(paths: tuple[Path, Path], approver: object) -> None:
    profile, report, _ = _frozen()
    with _open(paths) as registry, pytest.raises(RegistryRefused, match="approved_by"):
        registry.register_freeze(
            profile,
            report,
            approved_by=approver,  # type: ignore[arg-type]
            approved_at=APPROVED_AT,
        )


def test_a_naive_approval_time_is_refused(paths: tuple[Path, Path]) -> None:
    profile, report, _ = _frozen()
    with _open(paths) as registry, pytest.raises(RegistryRefused, match="timezone"):
        registry.register_freeze(
            profile, report, approved_by=APPROVER, approved_at=APPROVED_AT.replace(tzinfo=None)
        )


# ======================================================================================
# the same rules on replay (valid chain + anchor; only the registry's rules can refuse)
# ======================================================================================


def _mutated(path: tuple[str, ...], value: object) -> Callable[[dict[str, Any]], dict[str, Any]]:
    def mutate(payload: dict[str, Any]) -> dict[str, Any]:
        target = payload
        for step in path[:-1]:
            target = target[step]
        target[path[-1]] = value
        return _reidentified(payload)

    return mutate


REPLAY_CASES: list[tuple[str, Callable[[dict[str, Any]], dict[str, Any]], str]] = [
    ("format_version", _mutated(("format_version",), "2.0.0"), "format_version"),
    ("freeze_id", lambda p: {**p, "freeze_id": "0" * 64}, "freeze_id"),
    ("extra key", lambda p: _reidentified({**p, "note": "x"}), "exactly the keys"),
    (
        "missing key",
        lambda p: _reidentified({k: v for k, v in p.items() if k != "approved_by"}),
        "exactly the keys",
    ),
    ("profile_hash", _mutated(("profile_hash",), "1" * 64), "profile_ref / profile_hash"),
    (
        "profile_ref",
        _mutated(("profile_ref",), "profile:other@1.0.0"),
        "profile_ref / profile_hash",
    ),
    ("status", _mutated(("profile", "status"), "draft"), "not frozen"),
    ("provenance", _mutated(("calibration", "report_hash"), "2" * 64), "cites calibration"),
    ("calibration kind", _mutated(("calibration", "kind"), "validation_report"), "kind"),
    (
        "calibration uri",
        _mutated(("calibration", "uri"), "registry-blob:sha256:" + "3" * 64),
        "uri",
    ),
    ("approver", _mutated(("approved_by",), " "), "approved_by"),
    ("time zone", _mutated(("approved_at",), "2026-09-27T11:00:00+03:00"), "is not UTC"),
    ("naive time", _mutated(("approved_at",), "2026-09-27T08:00:00"), "is not UTC"),
    ("time form", _mutated(("approved_at",), "2026-09-27T08:00:00+00:00"), "canonical"),
]


@pytest.mark.parametrize(
    ("label", "mutate", "match"), REPLAY_CASES, ids=[c[0] for c in REPLAY_CASES]
)
def test_a_record_breaking_a_rule_makes_the_registry_unopenable(
    paths: tuple[Path, Path],
    label: str,
    mutate: Callable[[dict[str, Any]], dict[str, Any]],
    match: str,
) -> None:
    _register(paths)
    [payload] = _payloads(paths)
    _forge(paths, [(PROFILE_FROZEN, mutate(json.loads(json.dumps(payload))))])
    with pytest.raises(RegistryCorrupted, match=match):
        _open(paths)


def test_an_unknown_record_type_makes_the_registry_unopenable(paths: tuple[Path, Path]) -> None:
    _register(paths)
    [payload] = _payloads(paths)
    _forge(paths, [("profile.unfrozen", payload)])
    with pytest.raises(RegistryCorrupted, match="unknown record type"):
        _open(paths)


def test_a_missing_or_tampered_calibration_blob_makes_it_unopenable(
    paths: tuple[Path, Path],
) -> None:
    _register(paths)
    blob = next((paths[0] / "blobs").iterdir())
    original = blob.read_bytes()
    blob.write_bytes(original.replace(b"TEST ONLY", b"TEST 0NLY"))
    with pytest.raises(RegistryCorrupted, match="calibration report blob"):
        _open(paths)
    blob.unlink()
    with pytest.raises(RegistryCorrupted, match="calibration report blob"):
        _open(paths)
    blob.write_bytes(original)
    with _open(paths) as registry:
        assert len(registry) == 1


# ======================================================================================
# single writer
# ======================================================================================


def test_a_single_writer_holds_the_registry(paths: tuple[Path, Path]) -> None:
    first = _open(paths)
    with pytest.raises(RegistryLocked):
        _open(paths)
    first.close()
    with _open(paths) as second:
        assert len(second) == 0
    profile, report, _ = _frozen()
    with pytest.raises(RegistryError, match="closed"):
        first.register_freeze(profile, report, approved_by=APPROVER, approved_at=APPROVED_AT)
    with pytest.raises(RegistryError, match="closed"):
        first.frozen_record(profile)

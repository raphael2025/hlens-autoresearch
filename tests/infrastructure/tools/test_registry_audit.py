"""ADR-0091: unified registry audit is read-only and labels evidence limits."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from core.domain.base import Kind, Ref
from core.domain.execution import ExecutionMode
from core.domain.research import FailureRecord, RetirementRecord
from core.errors import ReasonCode
from infrastructure.event_bus.journal import AppendOnlyJournal
from infrastructure.registry.profile_freeze import (
    ProfileFreezeRegistry,
    RegistryCorrupted as FreezeRegistryCorrupted,
    verify_integrity_snapshot as audit_profile_freeze,
)
from infrastructure.registry.registry import (
    RegistryCorrupted,
    verify_integrity_snapshot as audit_strategy,
)
from infrastructure.registry.retirement import (
    RetirementRegistry,
    verify_integrity_snapshot as audit_retirement,
)
from infrastructure.tools.registry_audit import main
from research.strategies.failure_registry import (
    FailureRegistryCorrupted,
    verify_integrity_snapshot as audit_failure,
)
from tests.promotion.fixtures import (
    TOY_FREEZE_APPROVER,
    TOY_FREEZE_TIME,
    toy_calibration_report,
    toy_profile,
)


def _empty_registry_files(tmp_path: Path) -> dict[str, Path]:
    strategy = tmp_path / "strategy"
    strategy.mkdir()
    (strategy / "registry.jsonl").write_bytes(b"")
    strategy_anchor = tmp_path / "strategy.anchor.jsonl"
    strategy_anchor.write_bytes(b"")

    freeze = tmp_path / "freeze"
    freeze.mkdir()
    (freeze / "freezes.jsonl").write_bytes(b"")
    freeze_anchor = tmp_path / "freeze.anchor.jsonl"
    freeze_anchor.write_bytes(b"")

    retirement = tmp_path / "retirement"
    retirement.mkdir()
    (retirement / "retirements.jsonl").write_bytes(b"")

    failure = tmp_path / "failures.jsonl"
    failure.write_bytes(b"")
    return {
        "strategy": strategy,
        "strategy_anchor": strategy_anchor,
        "freeze": freeze,
        "freeze_anchor": freeze_anchor,
        "retirement": retirement,
        "failure": failure,
    }


def test_empty_snapshots_are_independent_and_do_not_create_locks(tmp_path: Path) -> None:
    paths = _empty_registry_files(tmp_path)
    results = (
        audit_strategy(paths["strategy"], anchor=paths["strategy_anchor"]),
        audit_profile_freeze(paths["freeze"], anchor=paths["freeze_anchor"]),
        audit_retirement(paths["retirement"]),
        audit_failure(paths["failure"]),
    )
    assert [result["status"] for result in results] == ["OK"] * 4
    assert [result["evidence"] for result in results] == [
        "HASH_CHAIN",
        "HASH_CHAIN",
        "HASH_CHAIN",
        "STRUCTURAL_ONLY",
    ]
    assert results[0]["anchor_status"] == "VERIFIED"
    assert results[2]["anchor_status"] == "UNANCHORED"
    assert results[3]["limitation"]
    unanchored_strategy = audit_strategy(paths["strategy"])
    assert unanchored_strategy["anchor_status"] == "UNANCHORED"
    assert unanchored_strategy["limitations"]
    assert not (paths["strategy"] / ".lock").exists()
    assert not (paths["freeze"] / ".lock").exists()
    assert not (paths["retirement"] / ".lock").exists()


def test_missing_registry_paths_are_not_created(tmp_path: Path) -> None:
    root = tmp_path / "missing"
    with pytest.raises(RegistryCorrupted):
        audit_strategy(root)
    assert not root.exists()
    with pytest.raises(FreezeRegistryCorrupted):
        audit_profile_freeze(root, anchor=tmp_path / "missing.anchor")
    assert not root.exists()


def test_strategy_hash_tampering_is_detected_without_rewriting(tmp_path: Path) -> None:
    root = tmp_path / "strategy"
    root.mkdir()
    journal = root / "registry.jsonl"
    AppendOnlyJournal(journal).append("unused", {})
    raw = json.loads(journal.read_text(encoding="utf-8"))
    raw["hash"] = "0" * 64
    journal.write_text(json.dumps(raw, separators=(",", ":")) + "\n", encoding="utf-8")
    before = journal.read_bytes()
    with pytest.raises(RegistryCorrupted, match="content hash"):
        audit_strategy(root)
    assert journal.read_bytes() == before
    assert not (root / ".lock").exists()


def test_strategy_valid_chain_with_bad_registry_record_is_rejected(tmp_path: Path) -> None:
    root = tmp_path / "strategy"
    root.mkdir()
    journal_path = root / "registry.jsonl"
    AppendOnlyJournal(journal_path).append("unknown.record", {})
    before = journal_path.read_bytes()
    with pytest.raises(RegistryCorrupted, match="unknown record type"):
        audit_strategy(root)
    assert journal_path.read_bytes() == before


def test_profile_freeze_anchor_ahead_is_rejected_without_repair(tmp_path: Path) -> None:
    paths = _empty_registry_files(tmp_path)
    AppendOnlyJournal(paths["freeze_anchor"]).append(
        "profile_freeze.head", {"length": 1, "head": "f" * 64}
    )
    before = paths["freeze_anchor"].read_bytes()
    with pytest.raises(FreezeRegistryCorrupted, match="does not match"):
        audit_profile_freeze(paths["freeze"], anchor=paths["freeze_anchor"])
    assert paths["freeze_anchor"].read_bytes() == before
    assert (paths["freeze"] / "freezes.jsonl").read_bytes() == b""


def test_profile_freeze_business_replay_is_read_only(tmp_path: Path) -> None:
    root, anchor = tmp_path / "freeze", tmp_path / "freeze.anchor.jsonl"
    report, report_hash = toy_calibration_report("audit fixture")
    profile = toy_profile(calibration_report=report_hash)
    with ProfileFreezeRegistry(root, anchor=anchor) as registry:
        registry.register_freeze(
            profile,
            report,
            approved_by=TOY_FREEZE_APPROVER,
            approved_at=TOY_FREEZE_TIME,
        )
    journal_before = (root / "freezes.jsonl").read_bytes()
    anchor_before = anchor.read_bytes()
    lock_before = (root / ".lock").read_bytes()
    blobs_before = {item.name: item.read_bytes() for item in (root / "blobs").iterdir()}
    result = audit_profile_freeze(root, anchor=anchor)
    assert result["journal_records"] == 1
    assert (root / "freezes.jsonl").read_bytes() == journal_before
    assert anchor.read_bytes() == anchor_before
    assert (root / ".lock").read_bytes() == lock_before
    assert {item.name: item.read_bytes() for item in (root / "blobs").iterdir()} == blobs_before


def test_profile_freeze_journal_ahead_of_anchor_is_not_auto_repaired(tmp_path: Path) -> None:
    root, anchor = tmp_path / "freeze", tmp_path / "freeze.anchor.jsonl"
    report, report_hash = toy_calibration_report("crash-window fixture")
    profile = toy_profile(calibration_report=report_hash)
    with ProfileFreezeRegistry(root, anchor=anchor) as registry:
        registry.register_freeze(
            profile,
            report,
            approved_by=TOY_FREEZE_APPROVER,
            approved_at=TOY_FREEZE_TIME,
        )
    anchor.write_bytes(b"")
    journal_before, anchor_before = (root / "freezes.jsonl").read_bytes(), anchor.read_bytes()
    with pytest.raises(FreezeRegistryCorrupted, match="does not match"):
        audit_profile_freeze(root, anchor=anchor)
    assert (root / "freezes.jsonl").read_bytes() == journal_before
    assert anchor.read_bytes() == anchor_before


def test_retirement_unanchored_result_discloses_tail_deletion_limit(tmp_path: Path) -> None:
    paths = _empty_registry_files(tmp_path)
    result = audit_retirement(paths["retirement"])
    assert result["anchor_status"] == "UNANCHORED"
    assert "trailing" in str(result["limitations"])


def test_retirement_anchor_ahead_is_rejected_without_repair(tmp_path: Path) -> None:
    paths = _empty_registry_files(tmp_path)
    anchor = tmp_path / "retirement.anchor.jsonl"
    AppendOnlyJournal(anchor).append(
        "retirement_registry.head", {"length": 1, "head": "e" * 64}
    )
    before = anchor.read_bytes()
    with pytest.raises(RegistryCorrupted, match="does not match"):
        audit_retirement(paths["retirement"], anchor=anchor)
    assert anchor.read_bytes() == before


def test_retirement_business_replay_is_read_only(tmp_path: Path) -> None:
    root = tmp_path / "retirement"
    root.mkdir()
    (root / "retirements.jsonl").write_bytes(b"")
    subject = Ref(kind=Kind.STRATEGY, name="audit_retired", version="1.0.0")
    record = RetirementRecord(
        subject_ref=subject,
        retirement_reason="TEST ONLY audit fixture",
        evidence=("report:test-only",),
        active_from="2026-01-01T00:00:00Z",
        active_to="2026-06-01T00:00:00Z",
        execution_mode=ExecutionMode.SIMULATED,
        lessons="TEST ONLY",
        recorded_at="2026-09-28T09:00:00Z",
    )
    with RetirementRegistry(root) as registry:
        registry.register_retirement(record)
    journal = root / "retirements.jsonl"
    before = journal.read_bytes()
    lock_before = (root / ".lock").read_bytes()
    result = audit_retirement(root)
    assert result["journal_records"] == 1
    assert journal.read_bytes() == before
    assert (root / ".lock").read_bytes() == lock_before


def test_retirement_journal_ahead_of_anchor_is_not_auto_repaired(tmp_path: Path) -> None:
    root = tmp_path / "retirement"
    root.mkdir()
    (root / "retirements.jsonl").write_bytes(b"")
    subject = Ref(kind=Kind.STRATEGY, name="audit_crash_window", version="1.0.0")
    record = RetirementRecord(
        subject_ref=subject,
        retirement_reason="TEST ONLY crash-window fixture",
        evidence=("report:test-only",),
        active_from="2026-01-01T00:00:00Z",
        active_to="2026-06-01T00:00:00Z",
        execution_mode=ExecutionMode.SIMULATED,
        lessons="TEST ONLY",
        recorded_at="2026-09-28T09:00:00Z",
    )
    with RetirementRegistry(root) as registry:
        registry.register_retirement(record)
    anchor = tmp_path / "retirement.anchor.jsonl"
    anchor.write_bytes(b"")
    journal_before = (root / "retirements.jsonl").read_bytes()
    with pytest.raises(RegistryCorrupted, match="does not match"):
        audit_retirement(root, anchor=anchor)
    assert (root / "retirements.jsonl").read_bytes() == journal_before
    assert anchor.read_bytes() == b""


def test_failure_audit_validates_structure_and_detects_partial_tail(tmp_path: Path) -> None:
    path = tmp_path / "failures.jsonl"
    record = FailureRecord(
        subject_ref=Ref(kind=Kind.STRATEGY, name="test", version="1.0.0"),
        terminal_state="REJECTED",
        reason_code=ReasonCode.COST_KILLED,
        gate_id=None,
        hypothesis_family_id="test_family",
    )
    path.write_text(record.model_dump_json() + "\n", encoding="utf-8")
    result = audit_failure(path)
    assert result["evidence"] == "STRUCTURAL_ONLY"
    assert result["records"] == 1
    assert len(str(result["snapshot_sha256"])) == 64
    path.write_bytes(path.read_bytes().rstrip(b"\n"))
    with pytest.raises(FailureRegistryCorrupted, match="partial trailing line"):
        audit_failure(path)


def test_failure_audit_rejects_schema_invalid_json(tmp_path: Path) -> None:
    path = tmp_path / "failures.jsonl"
    path.write_text('{"not":"a FailureRecord"}\n', encoding="utf-8")
    with pytest.raises(FailureRegistryCorrupted, match="not a FailureRecord"):
        audit_failure(path)


def test_cli_returns_independent_structured_results(capsys: pytest.CaptureFixture[str], tmp_path: Path) -> None:
    paths = _empty_registry_files(tmp_path)
    status = main(
        [
            "--strategy-root",
            str(paths["strategy"]),
            "--strategy-anchor",
            str(paths["strategy_anchor"]),
            "--profile-freeze-root",
            str(paths["freeze"]),
            "--profile-freeze-anchor",
            str(paths["freeze_anchor"]),
            "--retirement-root",
            str(paths["retirement"]),
            "--failure-registry",
            str(paths["failure"]),
        ]
    )
    report = json.loads(capsys.readouterr().out)
    assert status == 0
    assert report["status"] == "OK"
    assert set(report["registries"]) == {
        "strategy",
        "profile_freeze",
        "retirement",
        "failure",
    }
    assert all(item["status"] == "OK" for item in report["registries"].values())


def test_cli_keeps_other_registry_results_when_one_fails(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    paths = _empty_registry_files(tmp_path)
    (paths["strategy"] / "registry.jsonl").write_bytes(b"{partial")
    status = main(
        [
            "--strategy-root",
            str(paths["strategy"]),
            "--profile-freeze-root",
            str(paths["freeze"]),
            "--profile-freeze-anchor",
            str(paths["freeze_anchor"]),
            "--retirement-root",
            str(paths["retirement"]),
            "--failure-registry",
            str(paths["failure"]),
        ]
    )
    report = json.loads(capsys.readouterr().out)
    assert status == 1
    assert report["status"] == "FAILED"
    assert report["registries"]["strategy"]["status"] == "FAILED"
    assert report["registries"]["profile_freeze"]["status"] == "OK"
    assert report["registries"]["failure"]["status"] == "OK"

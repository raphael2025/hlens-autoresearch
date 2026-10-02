"""Phase 14: ``MigrationTarget`` and ``MigrationReport`` (ADR-0106 decision 4) — declaration
checks, exclusion-aware comparison, canonical form, content hash, write-once persistence and
tamper detection. Pure framework tests: no engine is involved."""

from __future__ import annotations

import json
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from infrastructure.migration import (
    ConformanceReport,
    GoldenError,
    GoldenRecord,
    MigrationReport,
    MigrationTarget,
    RollbackVerdict,
    compare_golden,
    load_migration_report,
    record_golden,
    rollback_evidence,
    save_migration_report,
)

ZERO = Decimal(0)
GOLDEN_OUTPUTS = {"equity": Decimal("100.5"), "fees": Decimal("1.25"), "hash": Decimal(123456789)}


def _target(**changes: Any) -> MigrationTarget:
    fields: dict[str, Any] = {
        "migration_id": "m-1",
        "source": "old@1.0.0",
        "target": "new@1.0.0",
        "tolerance": ZERO,
        "excluded_outputs": ("hash",),
        "scope": ("one engine",),
        "out_of_scope": ("everything else",),
        "limitations": ("same team",),
    }
    fields.update(changes)
    return MigrationTarget(**fields)


@pytest.fixture
def golden() -> GoldenRecord:
    return record_golden("exp", lambda: dict(GOLDEN_OUTPUTS))


def _ok_conformance(candidate: str = "new@1.0.0") -> ConformanceReport:
    return ConformanceReport(candidate=candidate, checks=("a", "b"), failures=())


def _report(
    golden: GoldenRecord,
    *,
    target: MigrationTarget | None = None,
    migrated_outputs: dict[str, Decimal] | None = None,
    conformance: tuple[ConformanceReport, ...] | None = None,
    rolled_back_outputs: dict[str, Decimal] | None = None,
) -> MigrationReport:
    target = target or _target()
    migrated = migrated_outputs or {**GOLDEN_OUTPUTS, "hash": Decimal(987)}
    full = compare_golden(golden, lambda: migrated, ZERO)
    back = compare_golden(golden, lambda: rolled_back_outputs or dict(GOLDEN_OUTPUTS), ZERO)
    return MigrationReport(
        target=target,
        golden_name=golden.name,
        golden_record_hash=golden.record_hash,
        conformance=conformance or (_ok_conformance(target.target),),
        golden_diff=target.compare(golden, lambda: migrated),
        rollback=rollback_evidence(target.migration_id, golden, full, back),
    )


# --- MigrationTarget ---------------------------------------------------------------------------


def test_a_target_is_normalized_and_hashed_canonically() -> None:
    a = _target(excluded_outputs=("b", "a"), migration_id="  m-1 ")
    b = _target(excluded_outputs=("a", "b"))
    assert a.excluded_outputs == ("a", "b") and a.migration_id == "m-1"
    assert a.target_hash == b.target_hash and len(a.target_hash) == 64
    assert a.target_hash != _target(tolerance=Decimal("1e-18")).target_hash
    assert a.target_hash != _target(scope=("another",)).target_hash
    assert a.target_hash != _target(limitations=()).target_hash


@pytest.mark.parametrize(
    "bad",
    [
        {"migration_id": " "},
        {"source": ""},
        {"target": "old@1.0.0"},  # same identity as the source
        {"tolerance": Decimal(-1)},
        {"tolerance": Decimal("NaN")},
        {"tolerance": 0.0},
        {"excluded_outputs": ("a", "a")},
        {"excluded_outputs": ("",)},
        {"excluded_outputs": "hash"},
        {"scope": ()},  # a migration must declare its scope
        {"scope": ("ok", " ")},
        {"out_of_scope": (1,)},
    ],
)
def test_an_ill_formed_target_is_refused(bad: dict[str, Any]) -> None:
    with pytest.raises(GoldenError):
        _target(**bad)


def test_projection_removes_only_the_excluded_outputs() -> None:
    assert _target().project(GOLDEN_OUTPUTS) == {
        "equity": Decimal("100.5"),
        "fees": Decimal("1.25"),
    }
    assert _target(excluded_outputs=()).project(GOLDEN_OUTPUTS) == GOLDEN_OUTPUTS


def test_comparison_applies_the_exclusion_to_both_sides(golden: GoldenRecord) -> None:
    same = _target().compare(golden, lambda: {**GOLDEN_OUTPUTS, "hash": Decimal(1)})
    assert same.passed and same.bit_identical and same.differences == {}
    assert same.tolerance == ZERO
    # the same rerun is a difference when nothing is excluded
    strict = _target(excluded_outputs=()).compare(
        golden, lambda: {**GOLDEN_OUTPUTS, "hash": Decimal(1)}
    )
    assert not strict.passed and set(strict.differences) == {"hash"}


def test_comparison_still_reports_everything_else(golden: GoldenRecord) -> None:
    rerun = {**GOLDEN_OUTPUTS, "equity": Decimal("100.5000000001"), "extra": Decimal(1)}
    del rerun["fees"]
    diff = _target().compare(golden, lambda: rerun)
    assert diff.differences == {
        "equity": (Decimal("100.5"), Decimal("100.5000000001")),
        "fees": (Decimal("1.25"), None),
        "extra": (None, Decimal(1)),
    }
    tolerant = _target(tolerance=Decimal("1e-9")).compare(golden, lambda: {**GOLDEN_OUTPUTS})
    assert tolerant.passed and tolerant.tolerance == Decimal("1e-9")


def test_an_exclusion_that_names_nothing_is_refused(golden: GoldenRecord) -> None:
    with pytest.raises(GoldenError, match="not in the golden record"):
        _target(excluded_outputs=("hash", "typo")).compare(golden, lambda: dict(GOLDEN_OUTPUTS))
    with pytest.raises(GoldenError, match="GoldenRecord"):
        _target().compare("golden", lambda: {})  # type: ignore[arg-type]


# --- MigrationReport ---------------------------------------------------------------------------


def test_a_clean_migration_passes(golden: GoldenRecord) -> None:
    report = _report(golden)
    assert report.passed and report.verdict == "passed" and report.failures == ()
    assert report.rollback.verdict is RollbackVerdict.RESTORED
    text = report.render()
    assert "verdict: PASSED" in text and "excluded outputs: hash" in text
    assert "limitation: same team" in text and report.render() == text


def test_every_kind_of_failure_is_named_and_never_dropped(golden: GoldenRecord) -> None:
    broken = ConformanceReport("new@1.0.0", ("a", "b"), (("b", "AssertionError: no"),))
    drifted = {**GOLDEN_OUTPUTS, "equity": Decimal("100.6")}
    report = _report(
        golden,
        migrated_outputs=drifted,
        conformance=(broken, _ok_conformance("old@1.0.0")),
        rolled_back_outputs={**GOLDEN_OUTPUTS, "fees": Decimal(2)},
    )
    assert not report.passed and report.verdict == "failed"
    assert report.failures == (
        "conformance:new@1.0.0:b",
        "golden:equity",
        "rollback:not_restored",
    )
    assert report.rollback.residual_differences == ("fees",)


def test_a_report_must_be_coherent(golden: GoldenRecord) -> None:
    good = _report(golden)
    with pytest.raises(GoldenError, match="ConformanceReport"):
        replace(good, conformance=())
    with pytest.raises(GoldenError, match="not run through conformance"):
        replace(good, conformance=(_ok_conformance("other@1.0.0"),))
    with pytest.raises(GoldenError, match="another migration"):
        replace(good, target=_target(migration_id="m-2"))
    with pytest.raises(GoldenError, match="tolerance"):
        replace(good, target=_target(tolerance=Decimal("1e-9")))
    with pytest.raises(GoldenError, match="another golden record"):
        replace(good, golden_record_hash="a" * 64)
    with pytest.raises(GoldenError, match="name the golden record"):
        replace(good, golden_name="other")
    drifted = _report(golden, migrated_outputs={**GOLDEN_OUTPUTS, "equity": Decimal(1)})
    with pytest.raises(GoldenError, match="declares excluded"):
        replace(drifted, target=_target(excluded_outputs=("equity", "hash")))
    forged = replace(good.golden_diff, differences={"hash": (Decimal(1), Decimal(2))})
    with pytest.raises(GoldenError, match="declares excluded"):
        replace(good, golden_diff=forged)


def test_the_report_hash_is_content_addressed_and_sensitive(golden: GoldenRecord) -> None:
    a, b = _report(golden), _report(golden)
    assert a.report_hash == b.report_hash and len(a.report_hash) == 64
    assert a.report_hash != _report(golden, target=_target(limitations=("other",))).report_hash
    drifted = _report(golden, migrated_outputs={**GOLDEN_OUTPUTS, "equity": Decimal(1)})
    assert drifted.report_hash != a.report_hash


# --- persistence -------------------------------------------------------------------------------


def test_a_report_is_written_once_and_read_back_equal(tmp_path: Path, golden: GoldenRecord) -> None:
    report = _report(golden)
    path = save_migration_report(report, tmp_path / "reports")
    assert path.name == f"{report.report_hash}.json"
    data = path.read_bytes()
    assert save_migration_report(report, tmp_path / "reports") == path  # no-op re-save
    assert path.read_bytes() == data
    assert sorted(p.name for p in path.parent.iterdir()) == [path.name], "no temporary files left"
    loaded = load_migration_report(path.parent, report.report_hash)
    assert loaded == report and loaded.report_hash == report.report_hash
    assert loaded.render() == report.render() and loaded.passed


def test_a_failed_report_round_trips_with_its_differences(
    tmp_path: Path, golden: GoldenRecord
) -> None:
    failing = _report(
        golden,
        migrated_outputs={**GOLDEN_OUTPUTS, "equity": Decimal("100.6")},
        conformance=(ConformanceReport("new@1.0.0", ("a",), (("a", "ValueError: x"),)),),
    )
    save_migration_report(failing, tmp_path)
    loaded = load_migration_report(tmp_path, failing.report_hash)
    assert loaded == failing and not loaded.passed
    assert loaded.golden_diff.differences["equity"] == (Decimal("100.5"), Decimal("100.6"))
    assert loaded.failures == failing.failures


def test_a_different_file_at_the_same_address_is_never_overwritten(
    tmp_path: Path, golden: GoldenRecord
) -> None:
    report = _report(golden)
    squatter = tmp_path / f"{report.report_hash}.json"
    squatter.write_bytes(b"{}")
    with pytest.raises(GoldenError, match="different file"):
        save_migration_report(report, tmp_path)
    assert squatter.read_bytes() == b"{}"


def _saved(tmp_path: Path, golden: GoldenRecord) -> tuple[Path, dict[str, Any], str]:
    report = _report(golden)
    path = save_migration_report(report, tmp_path)
    return path, json.loads(path.read_text(encoding="utf-8")), report.report_hash


def _rewrite(path: Path, raw: dict[str, Any], tmp_path: Path) -> str:
    """Write ``raw`` canonically under its own (new) hash, as a forger who re-addresses would."""
    from core.domain.base import canonical_json, content_hash

    new_hash = content_hash(raw)
    (tmp_path / f"{new_hash}.json").write_bytes(canonical_json(raw).encode("utf-8"))
    return new_hash


def test_editing_the_file_in_place_is_detected(tmp_path: Path, golden: GoldenRecord) -> None:
    path, _, report_hash = _saved(tmp_path, golden)
    path.write_bytes(path.read_bytes().replace(b"m-1", b"m-9"))
    with pytest.raises(GoldenError, match="does not hash to its name"):
        load_migration_report(tmp_path, report_hash)


@pytest.mark.parametrize(
    ("edit", "message"),
    [
        (lambda raw: raw["target"].update({"tolerance": "0.5"}), "target_hash"),
        (lambda raw: raw["target"].update({"excluded_outputs": []}), "target_hash"),
        (
            lambda raw: raw["rollback"].update({"verdict": "not_restored"}),
            "evidence_hash",
        ),
        (lambda raw: raw["rollback"].update({"verdict": "bogus"}), "unknown rollback verdict"),
        (lambda raw: raw.update({"verdict": "failed"}), "stored verdict"),
        (lambda raw: raw.update({"failures": ["golden:x"]}), "stored verdict"),
        (lambda raw: raw["conformance"][0].update({"passed": False}), "conformance verdict"),
        (lambda raw: raw["golden_diff"].update({"tolerance": "1"}), "tolerance"),
        (lambda raw: raw.update({"extra": 1}), "not a migration report"),
        (lambda raw: raw.update({"format": "other"}), "not a migration report"),
        (lambda raw: raw.update({"schema_version": "9.0.0"}), "not a migration report"),
        (lambda raw: raw["golden_diff"].update({"differences": {"x": ["1"]}}), "malformed"),
        (lambda raw: raw["golden_diff"].update({"differences": {"x": ["a", "b"]}}), "decimal"),
        (lambda raw: raw["conformance"][0].update({"failures": [["a"]]}), "malformed"),
        (lambda raw: raw.pop("rollback"), "not a migration report"),
    ],
)
def test_a_forged_report_that_re_addresses_itself_is_still_refused(
    tmp_path: Path, golden: GoldenRecord, edit: Any, message: str
) -> None:
    path, raw, _ = _saved(tmp_path, golden)
    edit(raw)
    forged = _rewrite(path, raw, tmp_path)
    with pytest.raises(GoldenError, match=message):
        load_migration_report(tmp_path, forged)


def test_bad_addresses_and_missing_files_are_refused(tmp_path: Path) -> None:
    with pytest.raises(GoldenError, match="not a migration report hash"):
        load_migration_report(tmp_path, "nope")
    with pytest.raises(GoldenError, match="unreadable"):
        load_migration_report(tmp_path, "a" * 64)
    (tmp_path / f"{'b' * 64}.json").write_bytes(b"not json")
    with pytest.raises(GoldenError, match="does not hash"):
        load_migration_report(tmp_path, "b" * 64)
    with pytest.raises(GoldenError, match="MigrationReport"):
        save_migration_report("report", tmp_path)  # type: ignore[arg-type]

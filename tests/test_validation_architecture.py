"""三层验证架构测试（ADR-0007）。

Constitution = 原则；Validation Profile = 版本化阈值；Experiment Metadata = 每次实验的记录。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from pydantic import ValidationError

from core.contracts.profile_selection import ExperimentMetadata, OosUnsealing, ProfileSelectionKey
from core.contracts.validation_profile import ProfileStatus, ValidationProfile
from core.errors import ProfileViolation
from tests import factories

REPO = Path(__file__).resolve().parents[1]


def _metadata(**overrides: object) -> ExperimentMetadata:
    payload: dict[str, object] = {
        "experiment_hash": "exp-hash",
        "constitution_version": "0.2.0-draft",
        "validation_profile_version": "vp:test_scope@1.0.0",
        "validation_profile_hash": "profile-hash",
        "profile_selection_rule_version": "1.0.0",
        "profile_selection_key": factories.selection_key(),
        "hypothesis_family_id": "family-1",
        "trial_index": 1,
        "family_trial_count": 1,
        "declared_research_class": "swing",
    }
    payload.update(overrides)
    return ExperimentMetadata(**payload)  # type: ignore[arg-type]


def test_profile_requires_all_five_threshold_groups() -> None:
    """缺任何一类门槛的 Profile 无效（Constitution C-A7）。"""
    full = factories.validation_profile()
    for missing in ["data_split", "sample_size", "significance", "benchmark",
                    "parameter_stability", "cost_stress"]:
        payload = full.model_dump()
        payload.pop(missing)
        with pytest.raises(ValidationError):
            ValidationProfile(**payload)


def test_frozen_profile_is_immutable_and_needs_calibration() -> None:
    """一经使用即不可变；冻结必须引用校准报告（C-A5、C-A8）。"""
    frozen = factories.frozen_profile()
    assert frozen.status is ProfileStatus.FROZEN
    with pytest.raises(ValidationError):
        frozen.significance = frozen.significance  # type: ignore[misc]
    with pytest.raises(ValidationError):
        factories.validation_profile(status="frozen")  # 无 provenance.calibration_report


def test_profile_selection_is_deterministic_and_has_no_fallback() -> None:
    """研究者不能自选 Profile；无匹配时报错而不是回退（C-A4）。"""
    rule = factories.selection_rule()
    assert rule.select(factories.selection_key()).profile_version == "1.0.0"
    with pytest.raises(ProfileViolation):
        rule.select(factories.selection_key(research_class="intraday"))
    with pytest.raises(ProfileViolation):
        rule.select(
            ProfileSelectionKey(
                venue="other", symbol="TESTPAIR", timeframe="1h", research_class="swing"
            )
        )


def test_metadata_binds_rule_versions() -> None:
    meta = _metadata()
    assert meta.constitution_version and meta.validation_profile_version
    assert meta.profile_selection_rule_version


def test_metadata_counts_failed_trials() -> None:
    with pytest.raises(ValidationError):
        _metadata(trial_index=5, family_trial_count=2)


def test_metadata_rejects_class_switching() -> None:
    """不得把实验重新归类到更宽松的 Profile（C-A4）。"""
    with pytest.raises(ValidationError):
        _metadata(declared_research_class="intraday")


def test_unsealing_is_recorded_with_approval() -> None:
    meta = _metadata(
        oos_unsealing=OosUnsealing(
            unsealed_at=datetime(2026, 1, 1, tzinfo=UTC),
            approved_by="raphael",
            family_unseal_count=1,
        )
    )
    assert meta.oos_unsealing is not None
    assert meta.oos_unsealing.approved_by == "raphael"


def test_profile_contract_module_contains_no_threshold_defaults() -> None:
    """契约只定义字段，数值留给 Phase 4 校准（ADR-0007 两步冻结）。"""
    source = (REPO / "core" / "contracts" / "validation_profile.py").read_text(encoding="utf-8")
    for line in source.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or ":" not in stripped:
            continue
        if "Field(" in stripped and "=" in stripped.split("Field(")[0]:
            assert "default=" not in stripped, f"契约字段不得预设数值：{stripped}"


def test_report_and_tuple_agree_on_rule_versions() -> None:
    repro = factories.repro_tuple()
    report = factories.validation_report(
        constitution_version=repro.constitution_version,
        validation_profile_version=repro.validation_profile_version,
        validation_profile_hash=repro.validation_profile_hash,
        experiment_hash=repro.experiment_hash,
    )
    assert report.validation_profile_version == repro.validation_profile_version
    assert report.experiment_hash == repro.experiment_hash


def test_profile_rejects_boundary_before_window_start() -> None:
    from datetime import date

    split = factories.validation_profile().data_split
    with pytest.raises(ValidationError):
        split.model_validate(split.model_dump() | {"sealed_oos_boundary": date(2019, 1, 1)})


def test_walk_forward_fractions_are_bounded() -> None:
    profile = factories.validation_profile()
    wf = profile.data_split.walk_forward
    with pytest.raises(ValidationError):
        wf.model_validate(wf.model_dump() | {"min_positive_window_fraction": 1.5})
    assert wf.step > timedelta(0)

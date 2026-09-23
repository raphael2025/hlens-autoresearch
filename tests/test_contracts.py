"""契约测试：版本化、不可变、Schema 导出、point-in-time 与 Outcome 规则。

对应 roadmap Phase 0 验收标准"所有核心实体有契约与 Schema 导出"。
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from pydantic import ValidationError

from core.contracts.registry import CONTRACT_MODELS, export_json_schemas
from core.domain.base import CONTRACT_SCHEMA_VERSION, Contract, Kind, Ref, content_hash
from core.domain.research import Verdict
from core.domain.specs import FeatureSpec, OutcomeSpec, StrategySpec
from tests import factories


@pytest.mark.parametrize("model", CONTRACT_MODELS, ids=lambda m: m.__name__)
def test_every_contract_is_frozen_and_strict(model: type[Contract]) -> None:
    config = model.model_config
    assert config.get("frozen") is True, f"{model.__name__} 必须不可变"
    assert config.get("extra") == "forbid", f"{model.__name__} 必须禁止未声明字段"


@pytest.mark.parametrize("model", CONTRACT_MODELS, ids=lambda m: m.__name__)
def test_every_contract_declares_schema_version(model: type[Contract]) -> None:
    assert "schema_version" in model.model_fields, f"{model.__name__} 必须带 schema_version"
    assert model.model_fields["schema_version"].default == CONTRACT_SCHEMA_VERSION


def test_schema_export_covers_all_contracts(tmp_path: Path) -> None:
    written = export_json_schemas(tmp_path)
    assert len(written) == len(CONTRACT_MODELS)
    for model in CONTRACT_MODELS:
        path = written[model.__name__]
        schema = json.loads(path.read_text(encoding="utf-8"))
        assert schema["title"] == model.__name__
        assert "schema_version" in schema["properties"]


def test_ref_round_trip_and_rejects_bad_format() -> None:
    ref = Ref(kind=Kind.FEATURE, name="realized_vol_1h", version="1.2.0")
    assert str(ref) == "feature:realized_vol_1h@1.2.0"
    assert Ref.parse(str(ref)) == ref
    for bad in ["feature:Realized@1.0.0", "feature:x@1", "nope", "feature:x@1.0"]:
        with pytest.raises(ValueError):
            Ref.parse(bad)


def test_content_hash_is_order_independent_but_value_sensitive() -> None:
    assert content_hash({"a": 1, "b": 2}) == content_hash({"b": 2, "a": 1})
    assert content_hash({"a": 1}) != content_hash({"a": 2})


def test_published_versions_are_immutable() -> None:
    spec = OutcomeSpec(
        name="fwd_return",
        version="1.0.0",
        horizon=timedelta(hours=4),
        label_definition="forward return",
    )
    with pytest.raises(ValidationError):
        spec.version = "1.0.1"


def test_feature_rejects_negative_available_lag() -> None:
    """负的 available_lag 即未来函数（Constitution C-L1）。"""
    with pytest.raises(ValidationError):
        FeatureSpec(
            name="bad_feature",
            version="1.0.0",
            definition="x",
            inputs=(factories.dataset_ref(),),
            available_lag=timedelta(seconds=-1),
        )


def test_outcome_cannot_be_strategy_input() -> None:
    """Outcome 永不作为输入（Constitution C-L2）。"""
    with pytest.raises(ValidationError):
        StrategySpec(
            name="bad_strategy",
            version="1.0.0",
            signals=(Ref(kind=Kind.OUTCOME, name="fwd_return", version="1.0.0"),),
        )


def test_naive_datetime_rejected() -> None:
    """所有时间必须带时区（03-data.md §4）。"""
    from core.domain.specs import DatasetRef, Zone

    with pytest.raises(ValidationError):
        DatasetRef(
            zone=Zone.CANONICAL,
            table="t",
            snapshot_id="s",
            time_range_start=datetime(2024, 1, 1),  # naive
            time_range_end=datetime(2024, 1, 2, tzinfo=UTC),
        )


def test_reproducibility_tuple_requires_profile_version() -> None:
    """复现元组必须含 Constitution 版本与 Profile 版本（ADR-0007）。"""
    tuple_ = factories.repro_tuple()
    assert tuple_.constitution_version and tuple_.validation_profile_version
    with pytest.raises(ValidationError):
        factories.repro_tuple(validation_profile_version="")


def test_experiment_hash_is_stable_and_excludes_nothing_semantic() -> None:
    first = factories.repro_tuple()
    same = factories.repro_tuple()
    other = factories.repro_tuple(code_commit="fedcba9876543210")
    assert first.experiment_hash == same.experiment_hash
    assert first.experiment_hash != other.experiment_hash


def test_gate_threshold_requires_profile_source() -> None:
    """阈值必须声明它来自 Profile 的哪个字段（ADR-0007）。"""
    from core.domain.research import GateResult

    with pytest.raises(ValidationError):
        GateResult(gate_id="G1", metric="m", value=1.0, threshold=1.0, verdict=Verdict.PASS)


def test_report_cannot_pass_with_failing_gate() -> None:
    failing = factories.gate_result(Verdict.FAIL)
    with pytest.raises(ValidationError):
        factories.validation_report(verdict=Verdict.PASS, gates=(failing,))


def test_created_at_is_not_part_of_content_hash() -> None:
    spec = OutcomeSpec(
        name="fwd_return",
        version="1.0.0",
        horizon=timedelta(hours=4),
        label_definition="forward return",
    )
    later = spec.model_copy(update={"created_at": datetime(2030, 1, 1, tzinfo=UTC)})
    assert spec.content_hash() == later.content_hash()


def test_committed_schemas_match_contracts(tmp_path: Path) -> None:
    """仓库中的 **current** schemas/ 必须与当前契约一致（契约变更必须可见于 diff）。

    `schemas/` 顶层是当前 major；`schemas/<major>/` 是历史只读快照，
    **不参与**本一致性检查，也不会被当前导出覆盖（10-migration.md §3.1）。
    """
    repo_schemas = Path(__file__).resolve().parents[1] / "schemas"
    assert repo_schemas.exists(), "缺少导出的 JSON Schema：运行 python -m core.contracts.registry"
    fresh = export_json_schemas(tmp_path)
    committed = {p.name for p in repo_schemas.glob("*.schema.json")}
    assert committed == {p.name for p in fresh.values()}, "schemas/ 与契约注册表不一致"
    for name, path in fresh.items():
        expected = json.loads(path.read_text(encoding="utf-8"))
        actual = json.loads((repo_schemas / f"{name}.schema.json").read_text(encoding="utf-8"))
        assert actual == expected, f"{name} 的 Schema 已过期，请重新导出"

    legacy = repo_schemas / "v1"
    assert legacy.is_dir(), "v1 只读快照缺失（10-migration.md §3 要求保留至少一个 major）"
    assert legacy not in {p.parent for p in fresh.values()}, "当前导出不得写入历史快照目录"
    assert any(legacy.glob("*.schema.json")), "v1 快照目录为空"


def test_documented_core_entities_all_have_contracts() -> None:
    """02-domain.md §2 列出的核心实体都必须有契约（Phase 0 验收标准）。"""
    from core.contracts.registry import CONTRACT_MODELS

    names = {model.__name__ for model in CONTRACT_MODELS}
    documented_to_contract = {
        "Instrument": "Instrument",
        "Dataset": "DatasetRef",
        "Representation": "RepresentationSpec",
        "FeatureSpec": "FeatureSpec",
        "StateSpec": "StateSpec",
        "EventSpec": "EventSpec",
        "OutcomeSpec": "OutcomeSpec",
        "KnowledgeItem": "KnowledgeItem",
        "Hypothesis": "Hypothesis",
        "StrategySpec": "StrategySpec",
        "RiskPolicy": "RiskPolicy",
        "ExperimentSpec": "ExperimentSpec",
        "ExperimentRun": "ExperimentRun",
        "ValidationReport": "ValidationReport",
        # LifecycleRecord 由 LifecycleHistory + LifecycleTransition 表达
        "LifecycleRecord": "LifecycleHistory",
        "FailureRecord": "FailureRecord",
    }
    missing = {doc: impl for doc, impl in documented_to_contract.items() if impl not in names}
    assert not missing, f"以下文档实体缺少契约：{missing}"

"""契约值对象的语义身份（ADR-0018，D-26）。

三类语义身份都排除各自的 Contract 信封 `schema_version`：

* Profile 选择键 `(venue, symbol, timeframe, research_class)`——选择规则判重与 `select()` 同源；
* `Ref` 目标 `(kind, name, version)`——生命周期与 LIVE 证据的跨对象主体比较；
* `GitCodeRevision` 代码 `(commit_oid, tree_oid)`——部署记录与等价检查的生产代码修订比较。

全局结构相等（Pydantic `__eq__`）与内容哈希**保持不变**（D-26.7）：仅信封版本不同的对象，
`==` 与 `content_hash()` 仍然不同。本文件的行为测试只通过公开的构造 / 选择 / 校验路径观察，
不依赖新 API，因此能在旧实现上暴露旧行为（red）。
"""

from __future__ import annotations

import ast
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from pydantic import ValidationError

from core.contracts.profile_selection import (
    ProfileSelectionKey,
    ProfileSelectionRule,
    SelectionEntry,
)
from core.contracts.validation_profile import ProfileScope
from core.domain import base
from core.domain.base import GitCodeRevision, Kind, Ref
from core.errors import LifecycleViolation, ProfileViolation
from core.lifecycle.strategy import (
    AuthorizationRecord,
    ExecutionMode,
    ExecutionModeChange,
    LifecycleHistory,
    LifecycleState,
    LifecycleTransition,
    RiskGateRecord,
)
from tests import factories

REPO = Path(__file__).resolve().parents[1]

#: 与当前 major 相同、仅信封不同的版本（ADR-0010 §D-14：同 major 更高 minor 与 build 可识别）。
OTHER_ENVELOPES = ("2.0.1", "2.1.0", "2.0.0+build.7")

T0 = datetime(2026, 1, 1, tzinfo=UTC)


def _key(schema_version: str = "2.0.0", **overrides: str) -> ProfileSelectionKey:
    payload = {
        "venue": "testvenue",
        "symbol": "TESTPAIR",
        "timeframe": "1h",
        "research_class": "swing",
    }
    payload.update(overrides)
    return ProfileSelectionKey(schema_version=schema_version, **payload)


def _rule(*entries: tuple[ProfileSelectionKey, str]) -> ProfileSelectionRule:
    return ProfileSelectionRule(
        name="test_rule",
        version="1.0.0",
        entries=tuple(
            SelectionEntry(key=key, profile=factories.profile_ref(name=profile))
            for key, profile in entries
        ),
    )


def _select_or_none(rule: ProfileSelectionRule, key: ProfileSelectionKey) -> SelectionEntry | None:
    try:
        return rule.select(key)
    except ProfileViolation:
        return None


def _subject(schema_version: str = "2.0.0", **overrides: str) -> Ref:
    payload: dict[str, object] = {"kind": Kind.STRATEGY, "name": "s_example", "version": "1.0.0"}
    payload.update(overrides)
    return Ref(schema_version=schema_version, **payload)  # type: ignore[arg-type]


# ======================================================================================
# 矩阵 1 ~ 5：Profile 选择键身份（D-26.1、D-26.2、D-26.4）
# ======================================================================================


@pytest.mark.parametrize("envelope", OTHER_ENVELOPES)
def test_duplicate_business_key_mapping_to_different_profiles_is_rejected(envelope: str) -> None:
    """矩阵 1：同一业务键仅信封版本不同 = 重复，不得借信封选出另一个 Profile。"""
    with pytest.raises(ValidationError, match="重复"):
        _rule((_key("2.0.0"), "strict"), (_key(envelope), "lenient"))


@pytest.mark.parametrize("envelope", OTHER_ENVELOPES)
def test_duplicate_business_key_mapping_to_the_same_profile_is_rejected(envelope: str) -> None:
    """矩阵 2：重复检测只看选择键身份，与映射目标无关。"""
    with pytest.raises(ValidationError, match="重复"):
        _rule((_key("2.0.0"), "strict"), (_key(envelope), "strict"))


@pytest.mark.parametrize("envelope", OTHER_ENVELOPES)
def test_query_key_with_another_envelope_selects_the_same_entry(envelope: str) -> None:
    """矩阵 3：查询端的同 major 信封版本不影响匹配。"""
    rule = _rule((_key("2.0.0"), "strict"), (_key(research_class="intraday"), "other"))
    entry = _select_or_none(rule, _key(envelope))
    assert entry is not None, f"信封版本 {envelope} 的同一业务键没有匹配到任何 entry（旧行为）"
    assert entry.profile == factories.profile_ref(name="strict")


def test_entry_key_with_another_envelope_is_still_selected_by_the_default_query() -> None:
    """矩阵 3 的对称情形：规则里的 key 信封不同，查询 key 用默认信封。"""
    rule = _rule((_key("2.1.0"), "strict"))
    entry = _select_or_none(rule, _key())
    assert entry is not None, "规则 entry 的信封版本不同就匹配不到（旧行为）"
    assert entry.profile.name == "strict"


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("venue", "othervenue"),
        ("symbol", "OTHERPAIR"),
        ("timeframe", "4h"),
        ("research_class", "intraday"),
        ("venue", "TESTVENUE"),
        ("symbol", "testpair"),
        ("timeframe", "1H"),
    ),
)
def test_any_business_field_difference_does_not_match(field: str, value: str) -> None:
    """矩阵 4：任一业务字段不同（含仅大小写不同）都不匹配，且不回退。"""
    rule = _rule((_key(), "strict"))
    with pytest.raises(ProfileViolation):
        rule.select(_key(**{field: value}))


def test_case_variants_of_venue_are_distinct_keys() -> None:
    """矩阵 5：不做大小写折叠（D-26.4），`binance` / `Binance` 是两条不同的键。"""
    rule = _rule((_key(venue="binance"), "a"), (_key(venue="Binance"), "b"))
    assert rule.select(_key(venue="binance")).profile.name == "a"
    assert rule.select(_key(venue="Binance")).profile.name == "b"


# ======================================================================================
# 矩阵 6、7、14：research_class 与 ProfileScope 共用同一个标识符约束（D-26.3）
# ======================================================================================

BAD_RESEARCH_CLASSES = ("Swing", "swing-1", "1swing", "swing class", "_swing", "swíng")
GOOD_RESEARCH_CLASSES = ("swing", "swing_1", "intraday")


@pytest.mark.parametrize("value", BAD_RESEARCH_CLASSES)
def test_invalid_research_class_is_rejected_in_python(value: str) -> None:
    with pytest.raises(ValidationError):
        _key(research_class=value)


@pytest.mark.parametrize("value", BAD_RESEARCH_CLASSES)
def test_invalid_research_class_is_rejected_in_json(value: str) -> None:
    payload = json.loads(_key().model_dump_json())
    payload["research_class"] = value
    with pytest.raises(ValidationError):
        ProfileSelectionKey.model_validate_json(json.dumps(payload))


def test_empty_research_class_is_still_rejected() -> None:
    with pytest.raises(ValidationError):
        _key(research_class="")


@pytest.mark.parametrize("value", GOOD_RESEARCH_CLASSES)
def test_identifier_research_class_is_accepted(value: str) -> None:
    assert _key(research_class=value).research_class == value


def _field_pattern(model: type[ProfileSelectionKey] | type[ProfileScope]) -> str | None:
    for item in model.model_fields["research_class"].metadata:
        pattern = getattr(item, "pattern", None)
        if pattern is not None:
            return str(pattern)
    return None


def test_research_class_constraint_is_one_shared_named_constant() -> None:
    """矩阵 7：两处引用同一个命名常量，而不是各写一份正则。"""
    shared = getattr(base, "RESEARCH_CLASS_PATTERN", None)
    assert shared == r"^[a-z][a-z0-9_]*$", "core.domain.base 缺少共享常量 RESEARCH_CLASS_PATTERN"
    assert _field_pattern(ProfileSelectionKey) == shared
    assert _field_pattern(ProfileScope) == shared
    for relative in ("core/domain/selection.py", "core/contracts/validation_profile.py"):
        source = (REPO / relative).read_text(encoding="utf-8")
        assert "RESEARCH_CLASS_PATTERN" in source, f"{relative} 没有引用共享常量"
        assert "a-z0-9_]*$" not in source, f"{relative} 复制了正则字面量"


def test_exported_schemas_carry_the_shared_research_class_pattern() -> None:
    """矩阵 14：导出 Schema 与共享常量一致。"""
    shared = getattr(base, "RESEARCH_CLASS_PATTERN", None)
    for model in (ProfileSelectionKey, ProfileScope):
        schema = model.model_json_schema(mode="serialization")
        assert schema["properties"]["research_class"].get("pattern") == shared, model.__name__
    committed = json.loads(
        (REPO / "schemas" / "ProfileSelectionKey.schema.json").read_text(encoding="utf-8")
    )
    assert committed["properties"]["research_class"].get("pattern") == shared


# ======================================================================================
# 矩阵 8 ~ 10：Ref 目标身份用于生命周期与 LIVE 证据的主体比较（D-26.5）
# ======================================================================================


def _transition(
    subject: Ref, from_state: LifecycleState, to_state: LifecycleState
) -> LifecycleTransition:
    return LifecycleTransition(
        subject=subject,
        from_state=from_state,
        to_state=to_state,
        reason="test",
        triggered_by="test",
        occurred_at=T0,
    )


@pytest.mark.parametrize("envelope", OTHER_ENVELOPES)
def test_history_construction_accepts_the_same_target_in_another_envelope(envelope: str) -> None:
    transition = _transition(_subject(envelope), LifecycleState.IDEA, LifecycleState.CANDIDATE)
    try:
        history = LifecycleHistory(subject=_subject(), transitions=(transition,))
    except (ValidationError, LifecycleViolation) as exc:
        pytest.fail(f"同一目标、信封 {envelope} 的转移被误拒（旧行为）：{exc}")
    assert history.current_state is LifecycleState.CANDIDATE


@pytest.mark.parametrize("envelope", OTHER_ENVELOPES)
def test_history_append_accepts_the_same_target_in_another_envelope(envelope: str) -> None:
    history = LifecycleHistory(subject=_subject())
    transition = _transition(_subject(envelope), LifecycleState.IDEA, LifecycleState.CANDIDATE)
    try:
        appended = history.append(transition)
    except (ValidationError, LifecycleViolation) as exc:
        pytest.fail(f"append 误拒同一目标、信封 {envelope} 的转移（旧行为）：{exc}")
    assert appended.current_state is LifecycleState.CANDIDATE


TARGET_DIFFERENCES: tuple[dict[str, object], ...] = (
    {"kind": Kind.FEATURE},
    {"name": "s_other"},
    {"version": "1.0.1"},
)


@pytest.mark.parametrize("difference", TARGET_DIFFERENCES)
def test_history_still_rejects_a_different_target(difference: dict[str, object]) -> None:
    """矩阵 10：kind / name / version 任一不同，仍是别的对象。"""
    other = _subject(**difference)  # type: ignore[arg-type]
    transition = _transition(other, LifecycleState.IDEA, LifecycleState.CANDIDATE)
    with pytest.raises((ValidationError, LifecycleViolation)):
        LifecycleHistory(subject=_subject(), transitions=(transition,))
    with pytest.raises(LifecycleViolation):
        LifecycleHistory(subject=_subject()).append(transition)


def _live_change(*, gate_subject: Ref, authorization_subject: Ref) -> ExecutionModeChange:
    return ExecutionModeChange(
        subject=_subject(),
        state=LifecycleState.ACTIVE,
        from_mode=ExecutionMode.SIMULATED,
        to_mode=ExecutionMode.LIVE,
        risk_gate=RiskGateRecord(subject=gate_subject, gate_id="rg", passed=True, checked_at=T0),
        authorization=AuthorizationRecord(
            subject=authorization_subject,
            authorized_by="raphael",
            risk_budget="test-budget",
            authorized_at=T0,
            valid_until=T0 + timedelta(days=30),
        ),
        occurred_at=T0 + timedelta(days=1),
    )


@pytest.mark.parametrize("envelope", OTHER_ENVELOPES)
@pytest.mark.parametrize("which", ("risk_gate", "authorization"))
def test_live_evidence_accepts_the_same_target_in_another_envelope(
    envelope: str, which: str
) -> None:
    subjects = {"gate_subject": _subject(), "authorization_subject": _subject()}
    subjects["gate_subject" if which == "risk_gate" else "authorization_subject"] = _subject(
        envelope
    )
    try:
        change = _live_change(**subjects)
    except ValidationError as exc:
        pytest.fail(f"LIVE {which} 的同一目标、信封 {envelope} 被误拒（旧行为）：{exc}")
    assert change.to_mode is ExecutionMode.LIVE


@pytest.mark.parametrize("difference", TARGET_DIFFERENCES)
@pytest.mark.parametrize("which", ("risk_gate", "authorization"))
def test_live_evidence_still_rejects_a_different_target(
    difference: dict[str, object], which: str
) -> None:
    subjects = {"gate_subject": _subject(), "authorization_subject": _subject()}
    subjects["gate_subject" if which == "risk_gate" else "authorization_subject"] = _subject(
        **difference  # type: ignore[arg-type]
    )
    with pytest.raises(ValidationError, match="subject"):
        _live_change(**subjects)


# ======================================================================================
# 矩阵 11、12：GitCodeRevision 代码身份用于部署与等价检查（D-26.5）
# ======================================================================================


@pytest.mark.parametrize("envelope", OTHER_ENVELOPES)
def test_deployment_accepts_the_same_commit_and_tree_in_another_envelope(envelope: str) -> None:
    equivalence_revision = GitCodeRevision(
        schema_version=envelope,
        commit_oid=factories.GIT_COMMIT_OID,
        tree_oid=factories.GIT_TREE_OID,
    )
    try:
        record = factories.deployment_record(
            production_code_hash=factories.git_code_revision(),
            equivalence=factories.equivalence_check(production_code_hash=equivalence_revision),
        )
    except ValidationError as exc:
        pytest.fail(f"同一 commit + tree、信封 {envelope} 的修订被误拒（旧行为）：{exc}")
    assert record.equivalence.passed


@pytest.mark.parametrize("differing", ("commit_oid", "tree_oid"))
@pytest.mark.parametrize("envelope", ("2.0.0", *OTHER_ENVELOPES))
def test_deployment_still_rejects_a_different_commit_or_tree(differing: str, envelope: str) -> None:
    payload = {"commit_oid": factories.GIT_COMMIT_OID, "tree_oid": factories.GIT_TREE_OID}
    payload[differing] = factories.OTHER_GIT_COMMIT_OID
    other = GitCodeRevision(schema_version=envelope, **payload)
    with pytest.raises(ValidationError, match="生产代码修订"):
        factories.deployment_record(
            production_code_hash=factories.git_code_revision(),
            equivalence=factories.equivalence_check(production_code_hash=other),
        )


# ======================================================================================
# 矩阵 13：不做全局改写——仅信封不同的对象，结构相等与内容哈希仍然不同（D-26.7）
# ======================================================================================


def _envelope_pairs() -> tuple[tuple[object, object], ...]:
    return (
        (_subject(), _subject("2.0.1")),
        (_key(), _key("2.0.1")),
        (
            factories.git_code_revision(),
            GitCodeRevision(
                schema_version="2.0.1",
                commit_oid=factories.GIT_COMMIT_OID,
                tree_oid=factories.GIT_TREE_OID,
            ),
        ),
    )


@pytest.mark.parametrize("pair", _envelope_pairs(), ids=("Ref", "ProfileSelectionKey", "Git"))
def test_structural_equality_and_content_hash_still_include_the_envelope(
    pair: tuple[object, object],
) -> None:
    left, right = pair
    assert isinstance(left, base.Contract) and isinstance(right, base.Contract)
    assert left != right
    assert left.content_hash() != right.content_hash()


# ======================================================================================
# 显式语义身份 API：精确的元组、排除信封版本
# ======================================================================================


def test_selection_key_identity_is_exactly_the_four_business_fields() -> None:
    identity = getattr(ProfileSelectionKey, "selection_identity", None)
    assert callable(identity), "ProfileSelectionKey 缺少显式选择键身份 API selection_identity()"
    key = _key("2.0.1")
    assert key.selection_identity() == ("testvenue", "TESTPAIR", "1h", "swing")
    assert key.selection_identity() == _key().selection_identity()


def test_ref_target_identity_is_exactly_kind_name_version() -> None:
    identity = getattr(Ref, "target_identity", None)
    assert callable(identity), "Ref 缺少显式目标身份 API target_identity()"
    ref = _subject("2.0.1")
    assert ref.target_identity() == (Kind.STRATEGY, "s_example", "1.0.0")
    assert ref.target_identity() == _subject().target_identity()
    assert ref.target_identity() != _subject(version="1.0.1").target_identity()


def test_git_code_revision_identity_is_exactly_commit_and_tree() -> None:
    identity = getattr(GitCodeRevision, "code_identity", None)
    assert callable(identity), "GitCodeRevision 缺少显式代码身份 API code_identity()"
    revision = factories.git_code_revision()
    assert revision.code_identity() == (factories.GIT_COMMIT_OID, factories.GIT_TREE_OID)


# ======================================================================================
# 矩阵 15：D-26.6 比较点清单——七个语义比较点不再使用全结构相等或序列化文本
# ======================================================================================


def _compare_sources(relative: str) -> list[str]:
    tree = ast.parse((REPO / relative).read_text(encoding="utf-8"))
    return [ast.unparse(node) for node in ast.walk(tree) if isinstance(node, ast.Compare)]


def test_semantic_comparison_points_use_the_identity_apis() -> None:
    lifecycle = _compare_sources("core/lifecycle/strategy.py")
    subject_compares = [c for c in lifecycle if "subject" in c]
    assert len(subject_compares) == 4, subject_compares
    assert all("target_identity()" in c for c in subject_compares), subject_compares

    artifact = _compare_sources("core/domain/artifact.py")
    code_compares = [c for c in artifact if "production_code_hash" in c]
    assert len(code_compares) == 1, code_compares
    assert all("code_identity()" in c for c in code_compares), code_compares

    selection = (REPO / "core/contracts/profile_selection.py").read_text(encoding="utf-8")
    assert "key.model_dump_json()" not in selection
    assert "entry.key == key" not in selection
    assert selection.count("selection_identity()") >= 2

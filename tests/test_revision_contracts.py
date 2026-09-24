"""双时间与 revision DAG 契约（ADR-0023，Phase 1 批次 B1，roadmap Phase 1 验收 #4）。

覆盖 ADR-0023 验收矩阵中**契约层可表达**的各项：两轴时间的局部不变量（#19、#20）、
availability 证据与证据缺口（#1、#5、#19）、revision 与 supersedes 的局部 / 聚合约束
（#8、#9、#11、#12、#13 的图形部分）、不以 arrival / hash / 墙钟决定优先级（#17，静态检查）、
PIT 输入形状与三种结果形状（#10）。

**不在本批范围**：PIT 选择算法、存储层幂等与 `arrival_seq` 跨重启不复用、证据真实性、
policy 登记、universe 与 manifest（B2）。本文件断言公开行为：线载荷的接受 / 拒绝、
Schema 与运行时同源、内容身份稳定。
"""

from __future__ import annotations

import ast
import itertools
import json
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from core.compat.v1 import V1_MODEL_NAMES
from core.contracts import revision
from core.contracts.registry import CONTRACT_MODELS, export_json_schemas
from core.contracts.revision import (
    AvailabilityDecision,
    ObservationTimes,
    PointInTimeSelection,
    PointInTimeSpec,
    PointInTimeStatus,
    PolicyBinding,
    PolicyRole,
    PrecedenceEvidence,
    RevisionGraph,
    RevisionRecord,
)
from core.domain.base import CONTRACT_SCHEMA_VERSION, Contract, Kind, content_hash

REPO = Path(__file__).resolve().parents[1]
CURRENT_SCHEMA_DIR = REPO / "schemas"
LEGACY_SCHEMA_DIR = CURRENT_SCHEMA_DIR / "v1"
REVISION_MODULE = REPO / "core" / "contracts" / "revision.py"

B1_MODELS: tuple[type[Contract], ...] = (
    PolicyBinding,
    ObservationTimes,
    AvailabilityDecision,
    RevisionRecord,
    PrecedenceEvidence,
    RevisionGraph,
    PointInTimeSpec,
    PointInTimeSelection,
)

#: 历史事件发生在 2024 年，本机在 2026 年才得知——历史 backfill 的典型形状。
EVENT = datetime(2024, 1, 1, 12, 0, tzinfo=UTC)
INGEST = datetime(2026, 9, 1, tzinfo=UTC)
KNOWN = INGEST + timedelta(minutes=5)
HASH_POLICY = "1" * 64
KEY = "binance:spot:BTCUSDT:agg_trade:1"
OTHER_KEY = "binance:spot:BTCUSDT:agg_trade:2"


# ======================================================================================
# 构造辅助（全部显式时间，不依赖 now）
# ======================================================================================


def binding(role: PolicyRole, policy_id: str = "binance.spot.publication") -> PolicyBinding:
    return PolicyBinding(role=role, policy_id=policy_id, version="1.0.0", policy_hash=HASH_POLICY)


def times(**overrides: Any) -> ObservationTimes:
    payload: dict[str, Any] = {
        "event_time": EVENT,
        "available_time": EVENT,
        "ingest_time": INGEST,
        "knowledge_time": KNOWN,
        "declared_latency": timedelta(0),
    }
    payload.update(overrides)
    return ObservationTimes(**payload)


def decision(*, knowledge_time: datetime = KNOWN, **overrides: Any) -> AvailabilityDecision:
    payload: dict[str, Any] = {
        "times": times(knowledge_time=knowledge_time),
        "policy": binding(PolicyRole.AVAILABILITY),
        "evidence": ("binance spot aggTrades are published in real time at trade time",),
    }
    payload.update(overrides)
    return AvailabilityDecision(**payload)


def record(
    revision_id: str,
    arrival_seq: int,
    *,
    supersedes: tuple[str, ...] = (),
    key: str = KEY,
    knowledge_time: datetime = KNOWN,
    **overrides: Any,
) -> RevisionRecord:
    payload: dict[str, Any] = {
        "observation_key": key,
        "revision_id": revision_id,
        "source_id": "binance.public.spot.archive@1.0.0",
        "payload_hash": content_hash({"payload": revision_id}),
        "arrival_seq": arrival_seq,
        "supersedes": supersedes,
        "availability": decision(knowledge_time=knowledge_time),
    }
    payload.update(overrides)
    return RevisionRecord(**payload)


def edge(
    newer: str, older: str, *, key: str = KEY, knowledge_time: datetime = KNOWN
) -> PrecedenceEvidence:
    return PrecedenceEvidence(
        observation_key=key,
        revision_id=newer,
        superseded_revision_id=older,
        policy=binding(PolicyRole.PRECEDENCE, "binance.spot.archive-revision"),
        evidence=("archive checksum history",),
        knowledge_time=knowledge_time,
    )


def distinct_payload(index: int) -> str:
    return f"{index:064x}"


def pit_spec(**overrides: Any) -> PointInTimeSpec:
    payload: dict[str, Any] = {
        "name": "phase1.first_slice",
        "version": "1.0.0",
        "simulation_time": EVENT,
        "knowledge_cutoff": KNOWN,
        "snapshot_bindings": {"canonical.trades": "4242", "canonical.bars_1m": "4343"},
        "point_in_time_binding": binding(PolicyRole.POINT_IN_TIME, "hlens.pit.maximal-head"),
        "availability_bindings": (binding(PolicyRole.AVAILABILITY),),
        "precedence_bindings": (binding(PolicyRole.PRECEDENCE, "binance.spot.archive-revision"),),
        "parser_bindings": (binding(PolicyRole.PARSER, "binance.spot.archive.parser"),),
    }
    payload.update(overrides)
    return PointInTimeSpec(**payload)


def selection(**overrides: Any) -> PointInTimeSelection:
    payload: dict[str, Any] = {
        "observation_key": KEY,
        "simulation_time": EVENT,
        "knowledge_cutoff": KNOWN,
        "status": PointInTimeStatus.SELECTED,
        "selected_revision_id": "r1",
        "maximal_heads": ("r1",),
    }
    payload.update(overrides)
    return PointInTimeSelection(**payload)


def unique_records(*specs: tuple[str, int, tuple[str, ...]]) -> tuple[RevisionRecord, ...]:
    """每条记录给不同的 payload hash，避免触发重复 payload 规则。"""
    return tuple(
        record(rid, seq, supersedes=sup, payload_hash=distinct_payload(i))
        for i, (rid, seq, sup) in enumerate(specs)
    )


def valid_instances() -> dict[type[Contract], Contract]:
    return {
        PolicyBinding: binding(PolicyRole.AVAILABILITY),
        ObservationTimes: times(),
        AvailabilityDecision: decision(),
        RevisionRecord: record("r1", 0),
        PrecedenceEvidence: edge("r2", "r1"),
        RevisionGraph: RevisionGraph(
            revisions=unique_records(("r1", 0, ()), ("r2", 1, ("r1",))),
            precedence_evidence=(edge("r2", "r1"),),
        ),
        PointInTimeSpec: pit_spec(),
        PointInTimeSelection: selection(),
    }


def wire(instance: Contract) -> dict[str, Any]:
    payload: dict[str, Any] = json.loads(instance.model_dump_json())
    return payload


# ======================================================================================
# ObservationTimes：两轴时间的局部不变量（ADR-0023 §1、矩阵 #19、#20）
# ======================================================================================


def test_historical_backfill_times_are_legal() -> None:
    """矩阵 #1：`available_time < ingest_time <= knowledge_time` 本身是合法形状。"""
    value = times()
    assert value.available_time < value.ingest_time <= value.knowledge_time
    assert value.source_time is None


@pytest.mark.parametrize(
    "field",
    [
        "event_time",
        "event_end_time",
        "source_time",
        "available_time",
        "ingest_time",
        "knowledge_time",
    ],
)
def test_naive_datetimes_are_rejected(field: str) -> None:
    with pytest.raises(ValidationError, match="naive"):
        times(**{field: datetime(2024, 1, 1, 12, 0)})


def test_offset_datetimes_are_normalised_to_utc_across_the_day_boundary() -> None:
    """矩阵 #20：UTC 跨日确定——+08:00 的次日 00:30 就是 UTC 当日 16:30。"""
    local = datetime(2024, 1, 2, 0, 30, tzinfo=timezone(timedelta(hours=8)))
    value = times(event_time=local, available_time=local)
    assert value.event_time == datetime(2024, 1, 1, 16, 30, tzinfo=UTC)
    assert value.event_time.tzinfo is UTC


def test_knowledge_time_cannot_precede_ingest_time() -> None:
    with pytest.raises(ValidationError, match="knowledge_time"):
        times(knowledge_time=INGEST - timedelta(microseconds=1))
    assert times(knowledge_time=INGEST).knowledge_time == INGEST


def test_declared_latency_cannot_be_negative() -> None:
    with pytest.raises(ValidationError, match="declared_latency"):
        times(declared_latency=timedelta(microseconds=-1))
    assert times(declared_latency=timedelta(0)).declared_latency == timedelta(0)


def test_available_time_cannot_precede_the_event() -> None:
    with pytest.raises(ValidationError, match="available_time"):
        times(available_time=EVENT - timedelta(microseconds=1))


def test_interval_data_is_not_available_before_its_end() -> None:
    """ADR-0023 §2：区间型数据取区间结束——1m bar 不得在该分钟结束前可用。"""
    end = EVENT + timedelta(minutes=1)
    with pytest.raises(ValidationError, match="available_time"):
        times(event_end_time=end, available_time=EVENT)
    with pytest.raises(ValidationError, match="available_time"):
        times(event_end_time=end, available_time=end - timedelta(microseconds=1))
    assert times(event_end_time=end, available_time=end).observable_time == end


@pytest.mark.parametrize("delta", [timedelta(0), timedelta(minutes=-1)])
def test_interval_end_must_be_after_its_start(delta: timedelta) -> None:
    with pytest.raises(ValidationError, match="event_end_time"):
        times(event_end_time=EVENT + delta)


def test_available_time_cannot_precede_the_declared_source_time() -> None:
    source = EVENT + timedelta(seconds=3)
    with pytest.raises(ValidationError, match="source_time"):
        times(source_time=source, available_time=source - timedelta(microseconds=1))
    assert times(source_time=source, available_time=source).source_time == source


# ======================================================================================
# AvailabilityDecision：证据与证据缺口（ADR-0023 §2、矩阵 #1、#5、#19）
# ======================================================================================


def test_early_availability_without_evidence_is_rejected() -> None:
    """`available_time < ingest_time` 却没有来源证据（只有缺口或什么都没有）：拒绝。"""
    assert times().available_time < times().ingest_time
    with pytest.raises(ValidationError):
        decision(evidence=(), evidence_gap="publication time unknown")
    with pytest.raises(ValidationError):
        decision(evidence=(), evidence_gap=None)


def test_evidence_gap_must_fall_back_to_ingest_time() -> None:
    """矩阵 #5 / #19：缺口时不得保留由 event_time 回填的早期时间。"""
    with pytest.raises(ValidationError, match="ingest_time"):
        decision(evidence=(), evidence_gap="publication time unknown")
    later = times(available_time=KNOWN)
    with pytest.raises(ValidationError, match="ingest_time"):
        decision(times=later, evidence=(), evidence_gap="publication time unknown")


def test_evidence_gap_at_ingest_time_is_legal() -> None:
    """缺口不是数据集级失败：保守取 ingest_time 后该判定合法。"""
    fallback = times(available_time=INGEST)
    value = decision(times=fallback, evidence=(), evidence_gap="no revision publication time")
    assert value.times.available_time == value.times.ingest_time
    assert value.evidence == ()


def test_evidence_and_gap_are_mutually_exclusive_and_one_is_required() -> None:
    fallback = times(available_time=INGEST)
    with pytest.raises(ValidationError, match="恰好"):
        decision(times=fallback, evidence=("doc",), evidence_gap="gap")
    with pytest.raises(ValidationError, match="恰好"):
        decision(times=fallback, evidence=(), evidence_gap=None)


@pytest.mark.parametrize(
    "overrides", [{"evidence": ("  ",)}, {"evidence": (), "evidence_gap": "   "}]
)
def test_blank_evidence_or_gap_is_rejected(overrides: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        decision(times=times(available_time=INGEST), **overrides)


@pytest.mark.parametrize("role", [PolicyRole.PRECEDENCE, PolicyRole.PARSER])
def test_availability_decision_requires_an_availability_policy(role: PolicyRole) -> None:
    with pytest.raises(ValidationError, match="availability"):
        decision(policy=binding(role))


# ======================================================================================
# PolicyBinding
# ======================================================================================


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("policy_id", ""),
        ("policy_id", "Binance.spot"),
        ("policy_id", "binance..spot"),
        ("policy_id", "binance.spot@1.0.0"),
        ("version", "1.0"),
        ("version", "01.0.0"),
        ("policy_hash", "A" * 64),
        ("policy_hash", "a" * 63),
        ("role", "latest"),
    ],
)
def test_policy_binding_rejects_bad_fields(field: str, value: str) -> None:
    payload = wire(binding(PolicyRole.AVAILABILITY))
    payload[field] = value
    with pytest.raises(ValidationError):
        PolicyBinding.model_validate(payload)


def test_frozen_policy_identifiers_are_accepted() -> None:
    """03-data.md §7.3 冻结的标识符都能作为 policy_id。"""
    for policy_id in (
        "binance.spot.publication",
        "binance.spot.archive-revision",
        "binance.spot.archive.parser",
    ):
        assert binding(PolicyRole.PRECEDENCE, policy_id).policy_id == policy_id


# ======================================================================================
# RevisionRecord：局部不变量（ADR-0023 §4、矩阵 #11）
# ======================================================================================


@pytest.mark.parametrize("field", ["observation_key", "revision_id", "source_id"])
@pytest.mark.parametrize("value", ["", "   "])
def test_revision_identities_must_be_non_empty(field: str, value: str) -> None:
    payload = wire(record("r1", 0))
    with pytest.raises(ValidationError):
        RevisionRecord.model_validate({**payload, field: value})


@pytest.mark.parametrize("value", ["", "A" * 64, "a" * 65, "sha256:" + "a" * 57])
def test_payload_hash_must_be_sha256(value: str) -> None:
    with pytest.raises(ValidationError):
        record("r1", 0, payload_hash=value)


@pytest.mark.parametrize("value", [-1, True, "3", 1.0])
def test_arrival_seq_is_a_strict_non_negative_integer(value: object) -> None:
    with pytest.raises(ValidationError):
        record("r1", value)  # type: ignore[arg-type]


def test_duplicate_supersedes_are_rejected() -> None:
    with pytest.raises(ValidationError, match="重复"):
        record("r3", 0, supersedes=("r1", "r1"))


def test_self_supersedes_is_rejected() -> None:
    with pytest.raises(ValidationError, match="自身"):
        record("r1", 0, supersedes=("r1",))


def test_supersedes_is_a_set_with_one_canonical_form() -> None:
    """集合语义：输入顺序不同的同一集合是同一个 revision（内容哈希一致）。"""
    forward = record("r3", 0, supersedes=("r1", "r2"))
    backward = record("r3", 0, supersedes=("r2", "r1"))
    assert forward.supersedes == backward.supersedes == ("r1", "r2")
    assert forward.content_hash() == backward.content_hash()


def test_available_time_cannot_precede_the_source_revision_time() -> None:
    """ADR-0023 §2：来源给出修订时间时，该修订不得早于它回填。"""
    with pytest.raises(ValidationError, match="source_revision_time"):
        record("r1", 0, source_revision_id="v2", source_revision_time=EVENT + timedelta(days=1))
    value = record("r1", 0, source_revision_id="v2", source_revision_time=EVENT)
    assert value.source_revision_time == EVENT


# ======================================================================================
# PrecedenceEvidence
# ======================================================================================


def test_precedence_edge_cannot_point_to_itself() -> None:
    with pytest.raises(ValidationError, match="不得相同"):
        edge("r1", "r1")


def test_precedence_edge_requires_evidence_and_a_precedence_policy() -> None:
    payload = wire(edge("r2", "r1"))
    with pytest.raises(ValidationError):
        PrecedenceEvidence.model_validate({**payload, "evidence": []})
    with pytest.raises(ValidationError, match="precedence"):
        PrecedenceEvidence.model_validate(
            {**payload, "policy": wire(binding(PolicyRole.AVAILABILITY))}
        )


# ======================================================================================
# RevisionGraph：聚合不变量（ADR-0023 §4、矩阵 #8、#9、#11、#12）
# ======================================================================================


def test_duplicate_revision_ids_are_rejected() -> None:
    first = record("r1", 0, payload_hash=distinct_payload(1))
    again = record("r1", 1, payload_hash=distinct_payload(2))
    with pytest.raises(ValidationError, match="revision_id 重复"):
        RevisionGraph(revisions=(first, again))


def test_duplicate_arrival_seqs_are_rejected() -> None:
    first = record("r1", 7, payload_hash=distinct_payload(1))
    second = record("r2", 7, payload_hash=distinct_payload(2))
    with pytest.raises(ValidationError, match="arrival_seq 重复"):
        RevisionGraph(revisions=(first, second))


def test_arrival_seq_gaps_are_legal() -> None:
    """允许间隙；无间隙不是正确性条件（ADR-0023 §4）。"""
    graph = RevisionGraph(
        revisions=(
            record("r1", 3, payload_hash=distinct_payload(1)),
            record("r2", 99, payload_hash=distinct_payload(2)),
        )
    )
    assert len(graph.revisions) == 2


def test_replayed_payload_is_not_a_new_revision() -> None:
    """矩阵 #13（图形部分）：同键下 source_id + payload_hash 相同 = 重复。"""
    first = record("r1", 0, payload_hash=distinct_payload(1))
    replay = record("r1-replay", 1, payload_hash=distinct_payload(1))
    with pytest.raises(ValidationError, match="重复 payload"):
        RevisionGraph(revisions=(first, replay))
    other_key = record("r2", 1, key=OTHER_KEY, payload_hash=distinct_payload(1))
    assert len(RevisionGraph(revisions=(first, other_key)).revisions) == 2


def test_same_event_time_on_distinct_keys_are_all_kept() -> None:
    """矩阵 #14：相同 event_time 的多条事件是不同 key，互不覆盖。"""
    a = record("a1", 0, key=KEY, payload_hash=distinct_payload(1))
    b = record("b1", 1, key=OTHER_KEY, payload_hash=distinct_payload(2))
    graph = RevisionGraph(revisions=(a, b))
    assert {r.observation_key for r in graph.revisions} == {KEY, OTHER_KEY}


def test_known_cross_key_record_edge_is_rejected() -> None:
    old = record("x1", 0, key=OTHER_KEY, payload_hash=distinct_payload(1))
    new = record("r2", 1, supersedes=("x1",), payload_hash=distinct_payload(2))
    with pytest.raises(ValidationError, match="跨 observation_key"):
        RevisionGraph(revisions=(old, new), precedence_evidence=(edge("r2", "x1"),))


def test_known_cross_key_evidence_edge_is_rejected() -> None:
    old = record("x1", 0, key=OTHER_KEY, payload_hash=distinct_payload(1))
    new = record("r2", 1, payload_hash=distinct_payload(2))
    with pytest.raises(ValidationError, match="跨 observation_key"):
        RevisionGraph(revisions=(old, new), precedence_evidence=(edge("r2", "x1"),))


#: 统一 claim 冲突的稳定报错：ID 与排序后的全部认领键（与输入顺序无关）。
DANGLING_X_CONFLICT = (
    r"revision_id 'x' 跨 observation_key 归属冲突：同时被 "
    r"\['binance:spot:BTCUSDT:agg_trade:1', 'binance:spot:BTCUSDT:agg_trade:2'\] 认领"
)


@pytest.mark.parametrize("reverse", [False, True], ids=["forward", "reversed"])
def test_evidence_on_two_keys_cannot_share_a_dangling_endpoint(reverse: bool) -> None:
    """R2：a→x 在 KEY、x→b 在 OTHER_KEY，x 不在图中——x 的归属自相矛盾。"""
    evidence = (edge("a", "x", key=KEY), edge("x", "b", key=OTHER_KEY))
    with pytest.raises(ValidationError, match=DANGLING_X_CONFLICT):
        RevisionGraph(precedence_evidence=evidence[::-1] if reverse else evidence)


@pytest.mark.parametrize("reverse", [False, True], ids=["forward", "reversed"])
def test_dangling_supersedes_cannot_be_claimed_by_evidence_on_another_key(reverse: bool) -> None:
    new = record("r2", 0, supersedes=("x",))
    evidence = (edge("r2", "x", key=KEY), edge("y", "x", key=OTHER_KEY))
    with pytest.raises(ValidationError, match=DANGLING_X_CONFLICT):
        RevisionGraph(revisions=(new,), precedence_evidence=evidence[::-1] if reverse else evidence)


@pytest.mark.parametrize("reverse", [False, True], ids=["forward", "reversed"])
def test_records_on_two_keys_cannot_supersede_the_same_dangling_id(reverse: bool) -> None:
    a = record("a2", 0, supersedes=("x",), key=KEY, payload_hash=distinct_payload(1))
    b = record("b2", 1, supersedes=("x",), key=OTHER_KEY, payload_hash=distinct_payload(2))
    evidence = (edge("a2", "x", key=KEY), edge("b2", "x", key=OTHER_KEY))
    revisions = (b, a) if reverse else (a, b)
    with pytest.raises(ValidationError, match=DANGLING_X_CONFLICT):
        RevisionGraph(revisions=revisions, precedence_evidence=evidence)


def test_dangling_endpoint_claimed_only_by_one_key_is_legal() -> None:
    """同一 dangling x 被多个同键记录 / 证据认领：归属一致，合法。"""
    a = record("a2", 0, supersedes=("x",), payload_hash=distinct_payload(1))
    b = record("b2", 1, supersedes=("x",), payload_hash=distinct_payload(2))
    evidence = (edge("a2", "x"), edge("b2", "x"), edge("x", "w"))
    graph = RevisionGraph(revisions=(a, b), precedence_evidence=evidence)
    assert {r.revision_id for r in graph.revisions} == {"a2", "b2"}


def test_dangling_predecessor_is_legal() -> None:
    """矩阵 #12：predecessor 尚未 ingest 时，边与其证据可以先存在。"""
    new = record("r2", 0, supersedes=("r1",))
    graph = RevisionGraph(revisions=(new,), precedence_evidence=(edge("r2", "r1"),))
    assert graph.revisions[0].supersedes == ("r1",)


def test_dangling_predecessor_arriving_later_keeps_the_relation() -> None:
    """矩阵 #12：predecessor 后到，关系保持不变（记录不可变，边仍是 r2 → r1）。"""
    new = record("r2", 0, supersedes=("r1",), payload_hash=distinct_payload(2))
    before = RevisionGraph(revisions=(new,), precedence_evidence=(edge("r2", "r1"),))
    old = record("r1", 1, payload_hash=distinct_payload(1))
    after = RevisionGraph(revisions=(new, old), precedence_evidence=(edge("r2", "r1"),))
    assert before.revisions[0] == after.revisions[0]
    assert after.revisions[0].supersedes == ("r1",)


def test_record_edge_without_matching_evidence_is_rejected() -> None:
    new = record("r2", 0, supersedes=("r1",))
    with pytest.raises(ValidationError, match="precedence 证据"):
        RevisionGraph(revisions=(new,))
    with pytest.raises(ValidationError, match="precedence 证据"):
        RevisionGraph(revisions=(new,), precedence_evidence=(edge("r2", "r0"),))
    # 另一 key 上的同名边不算匹配：它指向已知的 r2，先被跨 key 规则拒绝。
    with pytest.raises(ValidationError, match="跨 observation_key"):
        RevisionGraph(revisions=(new,), precedence_evidence=(edge("r2", "r1", key=OTHER_KEY),))


def test_record_edge_evidence_must_be_known_no_later_than_the_record() -> None:
    """否则在两者之间的 cutoff 下，这条可用于选择的边没有证据（03-data.md §7.5）。"""
    new = record("r2", 0, supersedes=("r1",))
    late = edge("r2", "r1", knowledge_time=KNOWN + timedelta(microseconds=1))
    with pytest.raises(ValidationError, match="precedence 证据"):
        RevisionGraph(revisions=(new,), precedence_evidence=(late,))
    early = edge("r2", "r1", knowledge_time=INGEST)
    assert RevisionGraph(revisions=(new,), precedence_evidence=(early,)).precedence_evidence


def test_evidence_only_edge_resolves_competing_heads_later() -> None:
    """冲突只能靠追加有证据的 precedence 记录解决；它不需要改写已有记录。"""
    a = record("a", 0, payload_hash=distinct_payload(1))
    b = record("b", 1, payload_hash=distinct_payload(2))
    later = edge("b", "a", knowledge_time=KNOWN + timedelta(days=30))
    graph = RevisionGraph(revisions=(a, b), precedence_evidence=(later,))
    assert graph.revisions == (a, b)


def test_two_node_cycle_is_rejected() -> None:
    a = record("a", 0, supersedes=("b",), payload_hash=distinct_payload(1))
    b = record("b", 1, supersedes=("a",), payload_hash=distinct_payload(2))
    with pytest.raises(ValidationError, match="成环"):
        RevisionGraph(revisions=(a, b), precedence_evidence=(edge("a", "b"), edge("b", "a")))


def test_three_node_cycle_is_rejected() -> None:
    records = unique_records(("a", 0, ("c",)), ("b", 1, ("a",)), ("c", 2, ("b",)))
    evidence = (edge("a", "c"), edge("b", "a"), edge("c", "b"))
    with pytest.raises(ValidationError, match="成环"):
        RevisionGraph(revisions=records, precedence_evidence=evidence)


def test_cycle_formed_only_by_evidence_edges_is_rejected() -> None:
    evidence = (edge("a", "b"), edge("b", "a"))
    with pytest.raises(ValidationError, match="成环"):
        RevisionGraph(precedence_evidence=evidence)


def _chain_graph(order: tuple[int, ...], seqs: tuple[int, ...]) -> RevisionGraph:
    """A ← B ← C（C 取代 B，B 取代 A）按给定元组顺序与 arrival_seq 组装。"""
    specs = [("A", ()), ("B", ("A",)), ("C", ("B",))]
    records = tuple(
        record(specs[i][0], seqs[i], supersedes=specs[i][1], payload_hash=distinct_payload(i))
        for i in order
    )
    return RevisionGraph(revisions=records, precedence_evidence=(edge("B", "A"), edge("C", "B")))


def test_graph_validity_does_not_depend_on_arrival_order() -> None:
    """矩阵 #8、#9：新版本先 ingest、旧版本后到，与任意到达顺序得到同一张图。"""
    reference: set[tuple[str, str]] | None = None
    for order in itertools.permutations(range(3)):
        for seqs in itertools.permutations((0, 1, 2)):
            graph = _chain_graph(order, seqs)
            edges = {(r.revision_id, old) for r in graph.revisions for old in r.supersedes}
            reference = reference or edges
            assert edges == reference == {("B", "A"), ("C", "B")}


def test_newest_revision_ingested_first_is_a_valid_graph() -> None:
    """来源新版本 C 以最小 arrival_seq 先到、旧版本 A 最后到：仍是同一张合法图。"""
    graph = _chain_graph((2, 1, 0), (2, 1, 0))
    by_id = {r.revision_id: r for r in graph.revisions}
    assert by_id["C"].arrival_seq < by_id["A"].arrival_seq
    assert by_id["C"].supersedes == ("B",)


def test_cycle_rejection_does_not_depend_on_arrival_order() -> None:
    specs = [("a", ("c",)), ("b", ("a",)), ("c", ("b",))]
    evidence = (edge("a", "c"), edge("b", "a"), edge("c", "b"))
    for order in itertools.permutations(range(3)):
        records = tuple(
            record(specs[i][0], n, supersedes=specs[i][1], payload_hash=distinct_payload(i))
            for n, i in enumerate(order)
        )
        with pytest.raises(ValidationError, match="成环"):
            RevisionGraph(revisions=records, precedence_evidence=evidence)


# ======================================================================================
# 静态检查：不以 arrival / hash / 墙钟决定优先级（矩阵 #17）
# ======================================================================================

#: 不得作为排序键或优先级来源的字段。
_NON_PRIORITY_FIELDS = {
    "arrival_seq",
    "payload_hash",
    "ingest_time",
    "knowledge_time",
    "source_revision_time",
}


def _module_tree() -> ast.Module:
    return ast.parse(REVISION_MODULE.read_text(encoding="utf-8"), filename=str(REVISION_MODULE))


def test_no_ordering_call_uses_arrival_hash_or_time_fields() -> None:
    """`sorted` / `min` / `max` / `.sort` 的参数里不得出现这些字段。"""
    for node in ast.walk(_module_tree()):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", None)
        if name not in {"sorted", "min", "max", "sort"}:
            continue
        used = {sub.attr for sub in ast.walk(node) if isinstance(sub, ast.Attribute)} | {
            sub.value for sub in ast.walk(node) if isinstance(sub, ast.Constant)
        }
        assert not used & _NON_PRIORITY_FIELDS, f"第 {node.lineno} 行以 {used} 排序"


def test_arrival_seq_is_only_read_for_uniqueness() -> None:
    """`arrival_seq` 只在聚合唯一性校验里被读取，其余位置只有字段声明。"""
    readers: set[str] = set()
    for func in ast.walk(_module_tree()):
        if isinstance(func, ast.FunctionDef):
            for sub in ast.walk(func):
                if isinstance(sub, ast.Attribute) and sub.attr == "arrival_seq":
                    readers.add(func.name)
    assert readers == {"_graph_invariants"}


def test_revision_module_never_reads_the_wall_clock() -> None:
    tree = _module_tree()
    imported = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import | ast.ImportFrom)
        for alias in node.names
    }
    assert "time" not in imported
    attrs = {node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)}
    assert not attrs & {"now", "utcnow", "today", "time_ns", "monotonic"}


def test_no_self_reported_winner_or_validity_fields() -> None:
    """不得出现 `latest` / `winner` / `is_valid` 之类的自报字段。"""
    for model in B1_MODELS:
        for field in model.model_fields:
            assert not {"latest", "winner", "valid", "verified"} & set(field.split("_")), (
                f"{model.__name__}.{field}"
            )


# ======================================================================================
# PointInTimeSpec：输入形状（ADR-0023 §5、03-data.md §7.5）
# ======================================================================================


def test_point_and_interval_specs_are_legal() -> None:
    point = pit_spec()
    interval = pit_spec(
        simulation_time=None, simulation_start=EVENT, simulation_end=EVENT + timedelta(days=1)
    )
    assert point.simulation_time == EVENT
    assert interval.simulation_start is not None and interval.simulation_end is not None


@pytest.mark.parametrize(
    "overrides",
    [
        {"simulation_start": EVENT, "simulation_end": EVENT + timedelta(hours=1)},
        {"simulation_time": None},
        {"simulation_time": None, "simulation_start": EVENT},
        {"simulation_time": None, "simulation_end": EVENT},
        {"simulation_start": EVENT},
    ],
    ids=["point_and_interval", "neither", "start_only", "end_only", "point_and_start"],
)
def test_simulation_shape_is_exactly_one_of_point_or_interval(overrides: dict[str, Any]) -> None:
    with pytest.raises(ValidationError, match="simulation"):
        pit_spec(**overrides)


@pytest.mark.parametrize("delta", [timedelta(0), timedelta(seconds=-1)], ids=["empty", "reversed"])
def test_simulation_interval_must_be_non_empty_half_open(delta: timedelta) -> None:
    with pytest.raises(ValidationError, match="simulation_start < simulation_end"):
        pit_spec(simulation_time=None, simulation_start=EVENT, simulation_end=EVENT + delta)


def test_knowledge_cutoff_is_required_and_utc() -> None:
    payload = wire(pit_spec())
    del payload["knowledge_cutoff"]
    with pytest.raises(ValidationError):
        PointInTimeSpec.model_validate(payload)
    with pytest.raises(ValidationError, match="naive"):
        pit_spec(knowledge_cutoff=datetime(2026, 9, 1))


@pytest.mark.parametrize(
    "bindings",
    [
        {},
        {"trades": "1"},
        {"Canonical.trades": "1"},
        {"canonical.trades": ""},
        {"canonical.trades": "  "},
    ],
)
def test_snapshot_bindings_must_be_non_empty_and_well_formed(bindings: dict[str, str]) -> None:
    with pytest.raises(ValidationError):
        pit_spec(snapshot_bindings=bindings)


@pytest.mark.parametrize(
    "field", ["availability_bindings", "precedence_bindings", "parser_bindings"]
)
def test_policy_and_parser_bindings_cannot_be_empty(field: str) -> None:
    with pytest.raises(ValidationError):
        pit_spec(**{field: ()})


@pytest.mark.parametrize(
    ("field", "wrong"),
    [
        ("point_in_time_binding", binding(PolicyRole.AVAILABILITY)),
        ("availability_bindings", (binding(PolicyRole.PRECEDENCE),)),
        ("precedence_bindings", (binding(PolicyRole.AVAILABILITY),)),
        ("parser_bindings", (binding(PolicyRole.PRECEDENCE),)),
    ],
)
def test_each_binding_field_requires_its_role(field: str, wrong: object) -> None:
    with pytest.raises(ValidationError, match="role="):
        pit_spec(**{field: wrong})


def test_duplicate_policy_ids_in_one_field_are_rejected() -> None:
    first = binding(PolicyRole.AVAILABILITY)
    second = PolicyBinding(
        role=PolicyRole.AVAILABILITY,
        policy_id=first.policy_id,
        version="1.1.0",
        policy_hash="2" * 64,
    )
    with pytest.raises(ValidationError, match="policy_id"):
        pit_spec(availability_bindings=(first, second))


def test_binding_order_does_not_change_spec_identity() -> None:
    a = binding(PolicyRole.AVAILABILITY, "a.source")
    b = binding(PolicyRole.AVAILABILITY, "b.source")
    forward = pit_spec(availability_bindings=(a, b))
    backward = pit_spec(availability_bindings=(b, a))
    assert forward.availability_bindings == (a, b)
    assert forward.content_hash() == backward.content_hash()


@pytest.mark.parametrize(("field", "value"), [("name", "Phase1"), ("version", "1.0")])
def test_spec_identity_is_well_formed(field: str, value: str) -> None:
    with pytest.raises(ValidationError):
        pit_spec(**{field: value})


# ======================================================================================
# PointInTimeSelection：三种结果形状（ADR-0023 §5 第 4 步、矩阵 #10）
# ======================================================================================


def test_legal_selection_shapes() -> None:
    assert selection().selected_revision_id == "r1"
    absent = selection(status=PointInTimeStatus.ABSENT, selected_revision_id=None, maximal_heads=())
    assert absent.maximal_heads == ()
    conflict = selection(
        status=PointInTimeStatus.CONFLICT, selected_revision_id=None, maximal_heads=("r2", "r1")
    )
    assert conflict.maximal_heads == ("r1", "r2")


@pytest.mark.parametrize(
    ("status", "selected", "heads"),
    [
        (PointInTimeStatus.SELECTED, None, ()),
        (PointInTimeStatus.SELECTED, None, ("r1",)),
        (PointInTimeStatus.SELECTED, "r1", ()),
        (PointInTimeStatus.SELECTED, "r1", ("r2",)),
        (PointInTimeStatus.SELECTED, "r1", ("r1", "r2")),
        (PointInTimeStatus.ABSENT, "r1", ()),
        (PointInTimeStatus.ABSENT, None, ("r1",)),
        (PointInTimeStatus.ABSENT, "r1", ("r1",)),
        (PointInTimeStatus.CONFLICT, None, ()),
        (PointInTimeStatus.CONFLICT, None, ("r1",)),
        (PointInTimeStatus.CONFLICT, "r1", ("r1", "r2")),
        (PointInTimeStatus.CONFLICT, "r3", ("r1", "r2")),
    ],
)
def test_illegal_selection_shapes_are_rejected(
    status: PointInTimeStatus, selected: str | None, heads: tuple[str, ...]
) -> None:
    with pytest.raises(ValidationError):
        selection(status=status, selected_revision_id=selected, maximal_heads=heads)


def test_duplicate_maximal_heads_are_rejected() -> None:
    with pytest.raises(ValidationError, match="重复"):
        selection(
            status=PointInTimeStatus.CONFLICT,
            selected_revision_id=None,
            maximal_heads=("r1", "r1"),
        )


@pytest.mark.parametrize("status", ["latest", "winner", "valid", ""])
def test_unknown_selection_status_is_rejected(status: str) -> None:
    with pytest.raises(ValidationError):
        PointInTimeSelection.model_validate({**wire(selection()), "status": status})


# ======================================================================================
# 通用：入口、不可变、未知字段、内容哈希
# ======================================================================================


@pytest.mark.parametrize("model", B1_MODELS, ids=lambda m: m.__name__)
def test_python_and_json_entries_round_trip(model: type[Contract]) -> None:
    instance = valid_instances()[model]
    from_json = model.model_validate_json(instance.model_dump_json())
    from_python = model.model_validate(wire(instance))
    assert from_json == from_python == instance
    assert from_json.content_hash() == instance.content_hash()


@pytest.mark.parametrize("model", B1_MODELS, ids=lambda m: m.__name__)
def test_models_are_frozen_and_forbid_extra_fields(model: type[Contract]) -> None:
    instance = valid_instances()[model]
    field = next(iter(model.model_fields))
    with pytest.raises(ValidationError):
        setattr(instance, field, getattr(instance, field))
    payload = {**wire(instance), "is_valid": True}
    with pytest.raises(ValidationError):
        model.model_validate(payload)
    with pytest.raises(ValidationError):
        model.model_validate_json(json.dumps(payload))


@pytest.mark.parametrize("model", B1_MODELS, ids=lambda m: m.__name__)
def test_content_hash_is_stable_and_value_sensitive(model: type[Contract]) -> None:
    first = valid_instances()[model]
    second = valid_instances()[model]
    assert first.content_hash() == second.content_hash()
    bumped = first.model_copy(update={"schema_version": "2.1.0"})
    assert first.content_hash() != bumped.content_hash()


@pytest.mark.parametrize(
    ("model", "overrides"),
    [
        (ObservationTimes, {"knowledge_time": "2026-08-31T00:00:00Z"}),
        (ObservationTimes, {"declared_latency": "-PT1S"}),
        (ObservationTimes, {"event_time": "2024-01-01T12:00:00"}),
        (AvailabilityDecision, {"evidence": [], "evidence_gap": "unknown"}),
        (RevisionRecord, {"supersedes": ["r1"]}),
        (RevisionRecord, {"supersedes": ["r0", "r0"]}),
        (PrecedenceEvidence, {"superseded_revision_id": "r2"}),
        (PointInTimeSpec, {"simulation_start": "2024-01-01T00:00:00Z"}),
        (PointInTimeSpec, {"parser_bindings": []}),
        (PointInTimeSelection, {"maximal_heads": []}),
    ],
)
def test_json_entry_rejects_what_python_entry_rejects(
    model: type[Contract], overrides: dict[str, Any]
) -> None:
    """同一反例经 Python 与 JSON 两个入口都被拒绝。"""
    payload = {**wire(valid_instances()[model]), **overrides}
    with pytest.raises(ValidationError):
        model.model_validate(payload)
    with pytest.raises(ValidationError):
        model.model_validate_json(json.dumps(payload))


def test_copy_with_update_revalidates() -> None:
    with pytest.raises(ValidationError):
        selection().model_copy(update={"status": PointInTimeStatus.CONFLICT})
    with pytest.raises(ValidationError):
        record("r1", 0).model_copy(update={"supersedes": ("r1",)})


# ======================================================================================
# JSON Schema 与运行时同源
# ======================================================================================


def _schema(model: type[Contract]) -> dict[str, Any]:
    schema: dict[str, Any] = model.model_json_schema(mode="serialization")
    return schema


@pytest.mark.parametrize("model", B1_MODELS, ids=lambda m: m.__name__)
def test_schema_required_matches_runtime_required(model: type[Contract]) -> None:
    runtime = {name for name, field in model.model_fields.items() if field.is_required()}
    assert set(_schema(model).get("required", [])) == runtime
    assert _schema(model)["properties"]["schema_version"]["default"] == CONTRACT_SCHEMA_VERSION


def test_schema_expresses_the_field_level_constraints() -> None:
    policy = _schema(PolicyBinding)["properties"]
    assert policy["policy_id"]["pattern"] == revision.BINDING_ID_PATTERN
    assert policy["policy_hash"]["pattern"] == r"^[0-9a-f]{64}$"
    assert set(_schema(PolicyBinding)["$defs"]["PolicyRole"]["enum"]) == {
        role.value for role in PolicyRole
    }

    rec = _schema(RevisionRecord)["properties"]
    assert rec["arrival_seq"] == {"minimum": 0, "title": "Arrival Seq", "type": "integer"}
    assert rec["supersedes"]["uniqueItems"] is True
    assert rec["supersedes"]["items"]["minLength"] == 1
    for field in ("observation_key", "revision_id", "source_id"):
        assert rec[field]["minLength"] == 1

    assert _schema(PrecedenceEvidence)["properties"]["evidence"]["minItems"] == 1

    spec = _schema(PointInTimeSpec)["properties"]
    snapshots = spec["snapshot_bindings"]
    assert snapshots["minProperties"] == 1
    assert "minLength" not in snapshots
    assert snapshots["additionalProperties"] is False
    assert list(snapshots["patternProperties"]) == [revision.SNAPSHOT_TABLE_PATTERN]
    for field in ("availability_bindings", "precedence_bindings", "parser_bindings"):
        assert spec[field]["minItems"] == 1

    sel = _schema(PointInTimeSelection)
    assert sel["properties"]["maximal_heads"]["uniqueItems"] is True
    assert set(sel["$defs"]["PointInTimeStatus"]["enum"]) == {"selected", "absent", "conflict"}


# ======================================================================================
# 注册表与 Schema 边界：新增只加不改
# ======================================================================================


def test_every_b1_model_is_registered_and_exported(tmp_path: Path) -> None:
    written = export_json_schemas(tmp_path)
    for model in B1_MODELS:
        assert model in CONTRACT_MODELS
        committed = CURRENT_SCHEMA_DIR / f"{model.__name__}.schema.json"
        assert committed.read_bytes() == written[model.__name__].read_bytes()


def test_reexport_is_byte_identical_for_every_current_schema(tmp_path: Path) -> None:
    """重导出后全部 current Schema（含 B1 之前的 38 份）逐字节不变。"""
    written = export_json_schemas(tmp_path)
    committed = sorted(p.name for p in CURRENT_SCHEMA_DIR.glob("*.schema.json"))
    assert committed == sorted(p.name for p in written.values())
    for name, path in written.items():
        assert (CURRENT_SCHEMA_DIR / f"{name}.schema.json").read_bytes() == path.read_bytes(), name


def test_contract_version_and_kind_are_unchanged() -> None:
    """ADR-0023「版本策略」：不改 `CONTRACT_SCHEMA_VERSION`；ADR-0024 §5：`Kind` 不新增取值。"""
    assert CONTRACT_SCHEMA_VERSION == "2.0.0"
    assert {kind.value for kind in Kind} == {
        "dataset",
        "representation",
        "feature",
        "state",
        "event",
        "outcome",
        "strategy",
        "risk",
        "cost_model",
        "experiment",
        "hypothesis",
        "knowledge",
        "artifact",
        "profile",
        "profile_selection_rule",
    }


def test_b1_models_are_not_legacy_v1_models() -> None:
    names = {p.stem.removesuffix(".schema") for p in LEGACY_SCHEMA_DIR.glob("*.schema.json")}
    assert names == set(V1_MODEL_NAMES)
    assert len(names) == 35
    for model in B1_MODELS:
        assert model.__name__ not in V1_MODEL_NAMES
        assert not (LEGACY_SCHEMA_DIR / f"{model.__name__}.schema.json").exists()

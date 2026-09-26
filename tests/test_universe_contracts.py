"""历史可交易 universe 与 manifest 契约（ADR-0024、ADR-0023 §6；Phase 1 批次 B2，验收 #5）。

覆盖 ADR-0024 验收矩阵中**契约层可表达**的各项：symbol 复用形成不同 episode（#2）、暂停 / 恢复在同一
episode 保留多段区间（#3）、有 / 无稳定 ID 的改名（#4）、过滤指标只能按 PIT 基准求值（#8，声明层）、
manifest 缺绑定项被拒（#10；ADR-0023 #22）、不存在"今天 / 当前 / 最终"来源（#11）、universe spec 不
以 `Ref` / `Kind` 表达（#13）、revision 图校验与到达顺序无关且不跨 episode（#14 ~ #16 的图形部分）、
不以 `arrival_seq` 决定优先级（#17，静态检查）。

**明确延期（不在本文件以单元测试冒充完成）**：#1、#5、#6、#7、#9（按
`(simulation_time, knowledge_cutoff)` 执行 universe 查询与按位重建）、#14 ~ #16 的 maximal-head 选择
结果、#15 的质量事件写入，属批次 F 的 PIT / universe 执行器；#12（退市缺失收益显式处理）属 Phase 4 /
5 的 Outcome / Strategy 规格；Registry 存在性、哈希与真实内容一致、Runner 强制消费 manifest 属未来
Registry / Runner。
"""

from __future__ import annotations

import ast
import hashlib
import itertools
import json
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from core.compat.v1 import V1_MODEL_NAMES
from core.contracts import universe
from core.contracts.registry import CONTRACT_MODELS, export_json_schemas
from core.contracts.revision import (
    BINDING_ID_PATTERN,
    SNAPSHOT_TABLE_PATTERN,
    AvailabilityDecision,
    ObservationTimes,
    PointInTimeSpec,
    PolicyBinding,
    PolicyRole,
    PrecedenceEvidence,
    RevisionRecord,
)
from core.contracts.universe import (
    LISTINGS_TABLE,
    QUALITY_REPORTS_TABLE,
    AvailabilityEvidenceGap,
    DegradedEpisodeKey,
    EpisodeIdentityBasis,
    ExclusionReason,
    FilterComparator,
    ListingHistory,
    ListingRevision,
    ListingStatus,
    MetricBasis,
    ResearchDatasetManifest,
    SelectedRevisionLineage,
    StableEpisodeKey,
    TradableInterval,
    UniverseCandidateSource,
    UniverseExclusion,
    UniverseFilter,
    UniverseMember,
    UniverseSelectionSpec,
    UniverseSpecBinding,
)
from core.domain.base import (
    CONTRACT_SCHEMA_VERSION,
    SEMVER_PATTERN,
    SHA256_PATTERN,
    Contract,
    Kind,
    Ref,
    content_hash,
)
from core.domain.specs import DatasetRef, Instrument, InstrumentType, Zone
from tests.contract_version_support import as_published_at_2_0_0

REPO = Path(__file__).resolve().parents[1]
CURRENT_SCHEMA_DIR = REPO / "schemas"
LEGACY_SCHEMA_DIR = CURRENT_SCHEMA_DIR / "v1"
UNIVERSE_MODULE = REPO / "core" / "contracts" / "universe.py"

B2_MODELS: tuple[type[Contract], ...] = (
    TradableInterval,
    StableEpisodeKey,
    DegradedEpisodeKey,
    ListingRevision,
    ListingHistory,
    UniverseFilter,
    UniverseSelectionSpec,
    UniverseSpecBinding,
    UniverseMember,
    UniverseExclusion,
    SelectedRevisionLineage,
    AvailabilityEvidenceGap,
    ResearchDatasetManifest,
)

#: B2 之前 `CONTRACT_MODELS` 的 46 个模型（起点 2d2946f），顺序即导出顺序。
PRE_B2_MODEL_NAMES = (
    "Ref",
    "GitCodeRevision",
    "ContentBlobRef",
    "Instrument",
    "DatasetRef",
    "RepresentationSpec",
    "FeatureSpec",
    "StateSpec",
    "EventSpec",
    "OutcomeSpec",
    "StrategySpec",
    "RiskPolicy",
    "KnowledgeItem",
    "Hypothesis",
    "LlmCall",
    "ReproducibilityTuple",
    "ExperimentSpec",
    "ExperimentRun",
    "GateResult",
    "ValidationReport",
    "FailureRecord",
    "RetirementRecord",
    "ValidationProfile",
    "ProfileSelectionKey",
    "ProfileSelection",
    "SelectionEntry",
    "ProfileSelectionRule",
    "OosUnsealing",
    "ExperimentMetadata",
    "LifecycleTransition",
    "LifecycleHistory",
    "RiskGateRecord",
    "AuthorizationRecord",
    "ExecutionModeChange",
    "GoldenOutputs",
    "StrategyArtifact",
    "EquivalenceCheck",
    "DeploymentRecord",
    "PolicyBinding",
    "ObservationTimes",
    "AvailabilityDecision",
    "RevisionRecord",
    "PrecedenceEvidence",
    "RevisionGraph",
    "PointInTimeSpec",
    "PointInTimeSelection",
)

#: 起点 2d2946f 已提交的冻结 Schema 的 sha256（`git show 2d2946f:schemas/<M>.schema.json |
#: sha256sum`）。
FROZEN_SCHEMA_SHA256 = {
    "Instrument": "ebb85f8b2d8cc7124d82978f284f91f0832f860ad828e2ffdc4dc2e51fc69df6",
    "DatasetRef": "6f9d28fb365b8acfe5d15b93049bdc0f2e683b58794c07383e4776f9bed6d742",
    "ReproducibilityTuple": "9183c12b72bd7d1ac20166bd34c923904264ca30273fda799e74b6cf9506ae16",
}

T0 = datetime(2017, 8, 17, tzinfo=UTC)
T1 = datetime(2022, 5, 13, tzinfo=UTC)
T2 = datetime(2022, 5, 28, tzinfo=UTC)
T3 = datetime(2023, 1, 1, tzinfo=UTC)
EVENT = datetime(2024, 1, 1, 12, 0, tzinfo=UTC)
INGEST = datetime(2026, 9, 1, tzinfo=UTC)
KNOWN = INGEST + timedelta(minutes=5)
SIM = datetime(2024, 6, 1, tzinfo=UTC)
SIM_END = datetime(2024, 7, 1, tzinfo=UTC)
SIM_MID = datetime(2024, 6, 15, tzinfo=UTC)
HASH = "1" * 64
OTHER_HASH = "2" * 64


# ======================================================================================
# 构造辅助（全部显式时间，不依赖 now）
# ======================================================================================


def binding(role: PolicyRole, policy_id: str) -> PolicyBinding:
    return PolicyBinding(role=role, policy_id=policy_id, version="1.0.0", policy_hash=HASH)


def decision() -> AvailabilityDecision:
    return AvailabilityDecision(
        times=ObservationTimes(
            event_time=EVENT,
            available_time=EVENT,
            ingest_time=INGEST,
            knowledge_time=KNOWN,
            declared_latency=timedelta(0),
        ),
        policy=binding(PolicyRole.AVAILABILITY, "binance.spot.publication"),
        evidence=("binance announcement published at listing time",),
    )


def stable(product_id: str = "P-1001", venue: str = "binance") -> StableEpisodeKey:
    return StableEpisodeKey(
        basis=EpisodeIdentityBasis.STABLE_PRODUCT_ID,
        venue=venue,
        instrument_type=InstrumentType.SPOT,
        venue_product_id=product_id,
    )


def degraded(
    symbol: str = "BTCUSDT", start: datetime = T0, venue: str = "binance"
) -> DegradedEpisodeKey:
    return DegradedEpisodeKey(
        basis=EpisodeIdentityBasis.DEGRADED_SYMBOL_START,
        venue=venue,
        instrument_type=InstrumentType.SPOT,
        symbol=symbol,
        tradable_from=start,
    )


def iv(start: datetime, end: datetime | None) -> TradableInterval:
    return TradableInterval(tradable_from=start, tradable_until=end)


def instrument(symbol: str, venue: str = "binance", base: str | None = None) -> Instrument:
    return Instrument(
        venue=venue,
        symbol=symbol,
        instrument_type=InstrumentType.SPOT,
        base=base if base is not None else symbol.removesuffix("USDT"),
        quote="USDT",
    )


def _seq(revision_id: str) -> int:
    """确定性、互不相同的 arrival_seq；只为满足唯一性，不表示任何先后。"""
    return int(hashlib.sha256(revision_id.encode()).hexdigest()[:12], 16)


def listing(
    revision_id: str,
    episode: StableEpisodeKey | DegradedEpisodeKey,
    intervals: tuple[TradableInterval, ...],
    *,
    symbol: str | None = None,
    status: ListingStatus | None = None,
    supersedes: tuple[str, ...] = (),
    arrival_seq: int | None = None,
    **overrides: Any,
) -> ListingRevision:
    if symbol is None:
        symbol = episode.symbol if isinstance(episode, DegradedEpisodeKey) else "BTCUSDT"
    if status is None:
        open_ended = intervals and max(intervals, key=lambda i: i.tradable_from).tradable_until
        status = ListingStatus.LISTED if open_ended is None else ListingStatus.SUSPENDED
    payload: dict[str, Any] = {
        "revision": RevisionRecord(
            observation_key=episode.observation_key(),
            revision_id=revision_id,
            source_id="binance.public.spot.listing@1.0.0",
            payload_hash=content_hash({"payload": revision_id}),
            arrival_seq=_seq(revision_id) if arrival_seq is None else arrival_seq,
            supersedes=supersedes,
            availability=decision(),
        ),
        "episode": episode,
        "instrument": instrument(symbol, venue=episode.venue),
        "tradable_intervals": intervals,
        "status": status,
    }
    payload.update(overrides)
    return ListingRevision(**payload)


def edge(
    newer: str, older: str, episode: StableEpisodeKey | DegradedEpisodeKey
) -> PrecedenceEvidence:
    return PrecedenceEvidence(
        observation_key=episode.observation_key(),
        revision_id=newer,
        superseded_revision_id=older,
        policy=binding(PolicyRole.PRECEDENCE, "binance.spot.listing-revision"),
        evidence=("source revision time is later",),
        knowledge_time=KNOWN,
    )


def first_slice_spec(**overrides: Any) -> UniverseSelectionSpec:
    payload: dict[str, Any] = {
        "name": "binance.spot.btc-eth",
        "version": "1.0.0",
        "candidate_source": UniverseCandidateSource.POINT_IN_TIME_LISTINGS,
        "venue": "binance",
        "instrument_type": InstrumentType.SPOT,
        "symbols": ("BTCUSDT", "ETHUSDT"),
        "filters": (),
    }
    payload.update(overrides)
    return UniverseSelectionSpec(**payload)


def a_filter(filter_id: str = "trailing_liquidity", **overrides: Any) -> UniverseFilter:
    payload: dict[str, Any] = {
        "filter_id": filter_id,
        "feature_name": "trailing_quote_volume",
        "feature_version": "1.0.0",
        "feature_hash": HASH,
        "metric_basis": MetricBasis.POINT_IN_TIME,
        "comparator": FilterComparator.AT_LEAST,
        "threshold": 0.0,
    }
    payload.update(overrides)
    return UniverseFilter(**payload)


UPSTREAM = {
    LISTINGS_TABLE: "7001",
    QUALITY_REPORTS_TABLE: "7002",
    "canonical.trades": "7003",
    "raw.binance_spot_agg_trades": "7004",
    "raw.binance_spot_archives": "7005",
    # listing 的 Raw 表不在首切片冻结清单内；以下两个名字只是测试夹具。
    "raw.listing_rows": "7006",
    "raw.listing_payloads": "7007",
}


def pit(**overrides: Any) -> PointInTimeSpec:
    payload: dict[str, Any] = {
        "name": "phase1.first_slice",
        "version": "1.0.0",
        "simulation_time": SIM,
        "knowledge_cutoff": KNOWN,
        "snapshot_bindings": dict(UPSTREAM),
        "point_in_time_binding": binding(PolicyRole.POINT_IN_TIME, "hlens.pit.maximal-head"),
        "availability_bindings": (binding(PolicyRole.AVAILABILITY, "binance.spot.publication"),),
        "precedence_bindings": (binding(PolicyRole.PRECEDENCE, "binance.spot.archive-revision"),),
        "parser_bindings": (binding(PolicyRole.PARSER, "binance.spot.archive.parser"),),
    }
    payload.update(overrides)
    return PointInTimeSpec(**payload)


def interval_pit() -> PointInTimeSpec:
    return pit(simulation_time=None, simulation_start=SIM, simulation_end=SIM_END)


def dataset(**overrides: Any) -> DatasetRef:
    payload: dict[str, Any] = {
        "zone": Zone.RESEARCH_DATASET,
        "table": "research.first_slice_1m",
        "snapshot_id": "9001",
        "time_range_start": SIM,
        "time_range_end": SIM_END,
    }
    payload.update(overrides)
    return DatasetRef(**payload)


def member(
    episode: StableEpisodeKey | DegradedEpisodeKey, revision_id: str = "lr-1", **span: Any
) -> UniverseMember:
    return UniverseMember(episode=episode, listing_revision_id=revision_id, **span)


def exclusion(
    episode: StableEpisodeKey | DegradedEpisodeKey,
    reason: ExclusionReason = ExclusionReason.NOT_TRADABLE,
    revision_id: str | None = None,
    **extra: Any,
) -> UniverseExclusion:
    if revision_id is None:
        # 每个 episode 一个确定的 listing revision ID：同一 ID 不得被两个 episode 认领。
        revision_id = "lx-" + hashlib.sha256(episode.observation_key().encode()).hexdigest()[:8]
    return UniverseExclusion(
        episode=episode, listing_revision_id=revision_id, reason=reason, **extra
    )


def lineage(canonical_revision_id: str = "c-1", **overrides: Any) -> SelectedRevisionLineage:
    payload: dict[str, Any] = {
        "canonical_table": "canonical.trades",
        "canonical_revision_id": canonical_revision_id,
        "raw_table": "raw.binance_spot_agg_trades",
        "raw_revision_id": f"r-{canonical_revision_id}",
        "source_table": "raw.binance_spot_archives",
        "source_revision_id": "a-1",
    }
    payload.update(overrides)
    return SelectedRevisionLineage(**payload)


def listing_lineage(listing_revision_id: str) -> SelectedRevisionLineage:
    return SelectedRevisionLineage(
        canonical_table=LISTINGS_TABLE,
        canonical_revision_id=listing_revision_id,
        raw_table="raw.listing_rows",
        raw_revision_id=f"row-{listing_revision_id}",
        source_table="raw.listing_payloads",
        source_revision_id=f"payload-{listing_revision_id}",
    )


def complete_lineage(
    members: tuple[UniverseMember, ...], exclusions: tuple[UniverseExclusion, ...]
) -> tuple[SelectedRevisionLineage, ...]:
    """交易数据的 lineage + 每个被引用 listing revision 的 listing lineage。"""
    entries: tuple[UniverseMember | UniverseExclusion, ...] = (*members, *exclusions)
    referenced = sorted({entry.listing_revision_id for entry in entries})
    return (lineage("c-1"), lineage("c-2"), *(listing_lineage(rid) for rid in referenced))


def gap(revision_id: str = "c-1", **overrides: Any) -> AvailabilityEvidenceGap:
    payload: dict[str, Any] = {
        "table": "canonical.trades",
        "revision_id": revision_id,
        "quality_report_id": "qr-1",
        "gap": "no source publication time; available_time falls back to ingest_time",
    }
    payload.update(overrides)
    return AvailabilityEvidenceGap(**payload)


BTC = degraded("BTCUSDT", T0)
ETH = degraded("ETHUSDT", T0 + timedelta(days=1))


def manifest(**overrides: Any) -> ResearchDatasetManifest:
    payload: dict[str, Any] = {
        "dataset": dataset(),
        "point_in_time": pit(),
        "universe_spec": first_slice_spec().binding(),
        "members": (member(BTC), member(ETH, "lr-2")),
        "exclusions": (),
        "quality_report_ids": ("qr-1", "qr-2"),
        "evidence_gaps": (gap("c-1"),),
    }
    payload.update(overrides)
    if "lineage" not in payload:
        payload["lineage"] = complete_lineage(payload["members"], payload["exclusions"])
    return ResearchDatasetManifest(**payload)


def pause_resume_chain() -> tuple[tuple[ListingRevision, ...], tuple[PrecedenceEvidence, ...]]:
    """同一 episode：上市 → 暂停（关闭区间）→ 恢复（同 episode 新区间）。"""
    r1 = listing("pr-1", BTC, (iv(T0, None),))
    r2 = listing("pr-2", BTC, (iv(T0, T1),), supersedes=("pr-1",))
    r3 = listing("pr-3", BTC, (iv(T2, None), iv(T0, T1)), supersedes=("pr-2",))
    return (r1, r2, r3), (edge("pr-2", "pr-1", BTC), edge("pr-3", "pr-2", BTC))


def valid_instances() -> dict[type[Contract], Contract]:
    revisions, evidence = pause_resume_chain()
    return {
        TradableInterval: iv(T0, T1),
        StableEpisodeKey: stable(),
        DegradedEpisodeKey: degraded(),
        ListingRevision: revisions[2],
        ListingHistory: ListingHistory(revisions=revisions, precedence_evidence=evidence),
        UniverseFilter: a_filter(),
        UniverseSelectionSpec: first_slice_spec(filters=(a_filter(),)),
        UniverseSpecBinding: first_slice_spec().binding(),
        UniverseMember: member(BTC),
        UniverseExclusion: exclusion(BTC, ExclusionReason.FILTERED, filter_id="liquidity"),
        SelectedRevisionLineage: lineage(),
        AvailabilityEvidenceGap: gap(),
        ResearchDatasetManifest: manifest(),
    }


def wire(instance: Contract) -> dict[str, Any]:
    payload: dict[str, Any] = json.loads(instance.model_dump_json())
    return payload


def _schema(model: type[Contract]) -> dict[str, Any]:
    schema: dict[str, Any] = model.model_json_schema(mode="serialization")
    return schema


# ======================================================================================
# TradableInterval：UTC 半开区间（ADR-0024 §1）
# ======================================================================================


def test_open_and_closed_intervals_are_legal() -> None:
    assert iv(T0, None).tradable_until is None
    assert iv(T0, T1).tradable_until == T1


@pytest.mark.parametrize("end", [T0, T0 - timedelta(seconds=1)])
def test_empty_or_reversed_interval_is_rejected(end: datetime) -> None:
    with pytest.raises(ValidationError, match="tradable_until 必须晚于"):
        iv(T0, end)


@pytest.mark.parametrize("field", ["tradable_from", "tradable_until"])
def test_naive_interval_bounds_are_rejected(field: str) -> None:
    payload: dict[str, Any] = {"tradable_from": T0, "tradable_until": T1}
    payload[field] = payload[field].replace(tzinfo=None)
    with pytest.raises(ValidationError, match="naive"):
        TradableInterval(**payload)


def test_offset_bounds_are_normalised_to_utc() -> None:
    plus8 = timezone(timedelta(hours=8))
    interval = iv(datetime(2017, 8, 17, 7, 0, tzinfo=plus8), None)
    assert interval.tradable_from == datetime(2017, 8, 16, 23, 0, tzinfo=UTC)
    assert interval.tradable_from.tzinfo is UTC


def test_open_end_must_be_explicit_not_defaulted() -> None:
    """漏写 `tradable_until` 不得被读成"仍在交易"。"""
    with pytest.raises(ValidationError, match="tradable_until"):
        TradableInterval.model_validate({"tradable_from": "2017-08-17T00:00:00Z"})
    assert "tradable_until" in _schema(TradableInterval)["required"]


# ======================================================================================
# Episode 身份：稳定产品 ID 与显式退化键（ADR-0024 §2）
# ======================================================================================


def test_both_identity_paths_are_legal() -> None:
    assert listing("s-1", stable(), (iv(T0, None),)).episode.basis is (
        EpisodeIdentityBasis.STABLE_PRODUCT_ID
    )
    assert listing("d-1", degraded(), (iv(T0, None),)).episode.basis is (
        EpisodeIdentityBasis.DEGRADED_SYMBOL_START
    )


def _episode_payload(**fields: Any) -> dict[str, Any]:
    return {"venue": "binance", "instrument_type": "spot", **fields}


@pytest.mark.parametrize(
    "episode",
    [
        # 混用：稳定路径夹带退化键字段
        _episode_payload(basis="stable_product_id", venue_product_id="P", symbol="BTCUSDT"),
        # 伪造稳定路径：声明 stable 却只给 symbol + 起点
        _episode_payload(
            basis="stable_product_id", symbol="BTCUSDT", tradable_from="2017-08-17T00:00:00Z"
        ),
        # 退化路径夹带产品 ID
        _episode_payload(
            basis="degraded_symbol_start",
            symbol="BTCUSDT",
            tradable_from="2017-08-17T00:00:00Z",
            venue_product_id="P",
        ),
        # 缺字段
        _episode_payload(basis="stable_product_id"),
        _episode_payload(basis="degraded_symbol_start", symbol="BTCUSDT"),
        _episode_payload(basis="degraded_symbol_start", tradable_from="2017-08-17T00:00:00Z"),
        # 缺 basis 或未知 basis：不能默认为任一路径
        _episode_payload(venue_product_id="P"),
        _episode_payload(basis="symbol", venue_product_id="P"),
        # 空白身份
        _episode_payload(basis="stable_product_id", venue_product_id="   "),
    ],
)
def test_mixed_missing_or_forged_identity_is_rejected(episode: dict[str, Any]) -> None:
    payload = wire(listing("d-1", degraded(), (iv(T0, None),)))
    payload["episode"] = episode
    with pytest.raises(ValidationError):
        ListingRevision.model_validate(payload)
    with pytest.raises(ValidationError):
        ListingRevision.model_validate_json(json.dumps(payload))


def test_stable_product_id_equal_to_symbol_is_legal_and_survives_rename() -> None:
    """Codex B2 复核：产品 ID 的字符串值可以恰好等于 symbol；契约不从相等推断冒充。"""
    key = stable(product_id="BTCUSDT")
    first = listing("eq-1", key, (iv(T0, None),), symbol="BTCUSDT")
    renamed = listing("eq-2", key, (iv(T0, None),), symbol="XBTUSDT", supersedes=("eq-1",))
    history = ListingHistory(
        revisions=(first, renamed), precedence_evidence=(edge("eq-2", "eq-1", key),)
    )
    assert {item.instrument.symbol for item in history.revisions} == {"BTCUSDT", "XBTUSDT"}
    assert first.revision.observation_key == renamed.revision.observation_key


def test_observation_key_must_equal_the_episode_key() -> None:
    good = listing("d-1", BTC, (iv(T0, None),))
    payload = wire(good)
    payload["revision"]["observation_key"] = ETH.observation_key()
    with pytest.raises(ValidationError, match="observation_key 必须等于"):
        ListingRevision.model_validate(payload)


def test_observation_keys_are_injective_across_paths_and_delimiters() -> None:
    keys = {
        stable("BTCUSDT").observation_key(),
        degraded("BTCUSDT").observation_key(),
        degraded("b:c", venue="a").observation_key(),
        degraded("c", venue="a:b").observation_key(),
        degraded("BTCUSDT", T0 + timedelta(microseconds=1)).observation_key(),
    }
    assert len(keys) == 5


def test_observation_key_ignores_the_envelope_version() -> None:
    bumped = degraded().model_copy(update={"schema_version": "2.1.0"})
    assert bumped.observation_key() == degraded().observation_key()


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"instrument": instrument("ETHUSDT")}, "退化键的 symbol"),
        ({"instrument": instrument("BTCUSDT", venue="okx")}, "episode.venue"),
        ({"instrument": instrument("BTCUSDT", base="")}, "instrument.base"),
        ({"tradable_intervals": (iv(T0 + timedelta(days=1), None),)}, "第一段可交易区间的起点"),
    ],
)
def test_degraded_listing_must_agree_with_its_key(overrides: dict[str, Any], message: str) -> None:
    with pytest.raises(ValidationError, match=message):
        listing("d-1", BTC, (iv(T0, None),), **overrides)


def test_instrument_type_must_agree_with_the_key() -> None:
    payload = wire(listing("s-1", stable(), (iv(T0, None),)))
    payload["instrument"]["instrument_type"] = "perpetual"
    with pytest.raises(ValidationError, match="instrument_type"):
        ListingRevision.model_validate(payload)


# ======================================================================================
# 可交易区间序列、状态（ADR-0024 §1、§2；矩阵 #3）
# ======================================================================================


@pytest.mark.parametrize(
    ("intervals", "message"),
    [
        ((iv(T0, T2), iv(T1, None)), "不得重叠"),
        ((iv(T0, T1), iv(T1, None)), "必须合并"),
        ((iv(T0, None), iv(T2, T3)), "开放区间"),
        ((iv(T0, None), iv(T0, T1)), "开放区间|不得重叠"),
    ],
)
def test_interval_sequence_must_be_disjoint_and_open_only_last(
    intervals: tuple[TradableInterval, ...], message: str
) -> None:
    with pytest.raises(ValidationError, match=message):
        listing("d-1", BTC, intervals)


def test_listing_requires_at_least_one_interval() -> None:
    with pytest.raises(ValidationError):
        listing("d-1", BTC, (), status=ListingStatus.SUSPENDED)


def test_pause_resume_keeps_every_interval_in_canonical_order() -> None:
    forward = listing("pr-3", BTC, (iv(T0, T1), iv(T2, None)))
    backward = listing("pr-3", BTC, (iv(T2, None), iv(T0, T1)))
    assert forward.tradable_intervals == backward.tradable_intervals == (iv(T0, T1), iv(T2, None))
    assert forward.content_hash() == backward.content_hash()


@pytest.mark.parametrize(
    ("intervals", "status"),
    [
        ((iv(T0, T1),), ListingStatus.LISTED),
        ((iv(T0, None),), ListingStatus.SUSPENDED),
        ((iv(T0, None),), ListingStatus.DELISTED),
    ],
)
def test_status_must_match_the_interval_shape(
    intervals: tuple[TradableInterval, ...], status: ListingStatus
) -> None:
    with pytest.raises(ValidationError, match="status=listed"):
        listing("d-1", BTC, intervals, status=status)


def test_delisted_listing_keeps_its_history() -> None:
    delisted = listing(
        "dl-1",
        BTC,
        (iv(T0, T1), iv(T2, T3)),
        status=ListingStatus.DELISTED,
        source_status="BREAK",
        status_reason="delisted by venue announcement",
    )
    assert delisted.tradable_intervals == (iv(T0, T1), iv(T2, T3))


def test_source_status_and_reason_are_optional_but_never_blank() -> None:
    assert listing("d-1", BTC, (iv(T0, None),)).source_status is None
    for field in ("source_status", "status_reason"):
        blank: dict[str, Any] = {field: "  "}
        with pytest.raises(ValidationError):
            listing("d-1", BTC, (iv(T0, None),), **blank)


# ======================================================================================
# symbol 复用与改名（矩阵 #2、#4）
# ======================================================================================


def test_symbol_reuse_forms_two_distinct_episodes() -> None:
    old = degraded("LUNAUSDT", T0)
    new = degraded("LUNAUSDT", T2)
    history = ListingHistory(
        revisions=(
            listing("luna-old", old, (iv(T0, T1),), status=ListingStatus.DELISTED),
            listing("luna-new", new, (iv(T2, None),)),
        )
    )
    keys = {item.revision.observation_key for item in history.revisions}
    assert keys == {old.observation_key(), new.observation_key()}


def test_supersedes_across_reused_symbol_episodes_is_rejected() -> None:
    old = degraded("LUNAUSDT", T0)
    new = degraded("LUNAUSDT", T2)
    with pytest.raises(ValidationError, match="跨 observation_key"):
        ListingHistory(
            revisions=(
                listing("luna-old", old, (iv(T0, T1),), status=ListingStatus.DELISTED),
                listing("luna-new", new, (iv(T2, None),), supersedes=("luna-old",)),
            ),
            precedence_evidence=(edge("luna-new", "luna-old", new),),
        )


def test_same_revision_id_cannot_belong_to_two_episodes() -> None:
    with pytest.raises(ValidationError, match="revision_id 重复"):
        ListingHistory(
            revisions=(
                listing("dup", BTC, (iv(T0, None),)),
                listing("dup", ETH, (iv(ETH.tradable_from, None),), arrival_seq=1),
            )
        )


def test_stable_rename_stays_in_the_same_episode() -> None:
    key = stable("P-137")
    history = ListingHistory(
        revisions=(
            listing("m-1", key, (iv(T0, None),), symbol="MATICUSDT"),
            listing("m-2", key, (iv(T0, None),), symbol="POLUSDT", supersedes=("m-1",)),
        ),
        precedence_evidence=(edge("m-2", "m-1", key),),
    )
    assert len({item.revision.observation_key for item in history.revisions}) == 1


def test_degraded_rename_forms_a_new_episode_linked_to_the_old_one() -> None:
    old = degraded("MATICUSDT", T0)
    new = degraded("POLUSDT", T2)
    renamed = listing("pol-1", new, (iv(T2, None),), renamed_from=old)
    history = ListingHistory(
        revisions=(listing("matic-1", old, (iv(T0, T1),), status=ListingStatus.DELISTED), renamed)
    )
    assert renamed.renamed_from == old
    assert len({item.revision.observation_key for item in history.revisions}) == 2


@pytest.mark.parametrize(
    ("renamed_from", "message"),
    [
        (degraded("POLUSDT", T0), "复用而不是改名"),
        (degraded("MATICUSDT", T2), "必须早于"),
        (degraded("MATICUSDT", T3), "必须早于"),
        (degraded("MATICUSDT", T0, venue="okx"), "同 venue"),
    ],
)
def test_bad_rename_links_are_rejected(renamed_from: DegradedEpisodeKey, message: str) -> None:
    with pytest.raises(ValidationError, match=message):
        listing("pol-1", degraded("POLUSDT", T2), (iv(T2, None),), renamed_from=renamed_from)


def test_stable_episode_cannot_link_another_episode() -> None:
    with pytest.raises(ValidationError, match="不得关联其它 episode"):
        listing("s-1", stable(), (iv(T0, None),), renamed_from=degraded("OLDUSDT", T0))


def test_rename_link_must_be_a_degraded_key() -> None:
    payload = wire(listing("pol-1", degraded("POLUSDT", T2), (iv(T2, None),)))
    payload["renamed_from"] = wire(stable())
    with pytest.raises(ValidationError):
        ListingRevision.model_validate(payload)


# ======================================================================================
# ListingHistory：复用 B1 revision 图（矩阵 #14 ~ #17 的图形部分）
# ======================================================================================


def test_stable_and_degraded_episodes_may_share_a_venue_and_type() -> None:
    """Codex B2 复核：同一 venue/type 下产品 A 有稳定 ID、产品 B 没有，是合法历史。"""
    history = ListingHistory(
        revisions=(
            listing("a-1", stable("P-A"), (iv(T0, None),), symbol="AAAUSDT"),
            listing("b-1", degraded("BBBUSDT", T0), (iv(T0, None),)),
        )
    )
    assert {item.episode.basis for item in history.revisions} == set(EpisodeIdentityBasis)


def test_supersedes_between_stable_and_degraded_episodes_is_rejected() -> None:
    """允许两种身份共存，不等于允许跨 episode supersedes。"""
    key = stable("P-A")
    with pytest.raises(ValidationError, match="跨 observation_key"):
        ListingHistory(
            revisions=(
                listing("b-1", degraded("AAAUSDT", T0), (iv(T0, None),)),
                listing("a-1", key, (iv(T0, None),), symbol="AAAUSDT", supersedes=("b-1",)),
            ),
            precedence_evidence=(edge("a-1", "b-1", key),),
        )


def test_evidence_claiming_a_revision_of_another_episode_is_rejected() -> None:
    with pytest.raises(ValidationError, match="跨 observation_key"):
        ListingHistory(
            revisions=(listing("btc-1", BTC, (iv(T0, None),)),),
            precedence_evidence=(edge("eth-2", "btc-1", ETH),),
        )


def test_declared_supersedes_without_evidence_is_rejected() -> None:
    revisions, _ = pause_resume_chain()
    with pytest.raises(ValidationError, match="precedence 证据"):
        ListingHistory(revisions=revisions)


def test_supersedes_cycle_is_rejected() -> None:
    with pytest.raises(ValidationError, match="成环"):
        ListingHistory(
            revisions=(
                listing("c-1", BTC, (iv(T0, None),), supersedes=("c-2",)),
                listing("c-2", BTC, (iv(T0, T1),), supersedes=("c-1",)),
            ),
            precedence_evidence=(edge("c-1", "c-2", BTC), edge("c-2", "c-1", BTC)),
        )


def test_dangling_predecessor_on_the_same_episode_is_legal() -> None:
    history = ListingHistory(
        revisions=(listing("pr-3", BTC, (iv(T0, T1), iv(T2, None)), supersedes=("pr-2",)),),
        precedence_evidence=(edge("pr-3", "pr-2", BTC),),
    )
    assert history.revisions[0].revision.supersedes == ("pr-2",)


def _with_seq(item: ListingRevision, seq: int) -> ListingRevision:
    return item.model_copy(
        update={"revision": item.revision.model_copy(update={"arrival_seq": seq})}
    )


def test_history_validity_and_hash_do_not_depend_on_arrival_order() -> None:
    """A → B → C 以任意元组顺序、任意 arrival_seq 进入：都是合法图，规范顺序相同。"""
    revisions, evidence = pause_resume_chain()
    for order in itertools.permutations(range(3)):
        reseq = tuple(_with_seq(revisions[i], n) for n, i in enumerate(order))
        history = ListingHistory(revisions=reseq, precedence_evidence=evidence[::-1])
        assert [item.revision.revision_id for item in history.revisions] == ["pr-1", "pr-2", "pr-3"]
    base = ListingHistory(revisions=revisions, precedence_evidence=evidence).content_hash()
    for order in itertools.permutations(range(3)):
        shuffled = tuple(revisions[i] for i in order)
        again = ListingHistory(revisions=shuffled, precedence_evidence=evidence[::-1])
        assert again.content_hash() == base


def test_newest_listing_ingested_first_is_still_a_valid_history() -> None:
    """来源新修订以最小 arrival_seq 先到、旧修订最后到：契约不因到达顺序拒绝或改写。"""
    revisions, evidence = pause_resume_chain()
    seqs = {"pr-3": 0, "pr-2": 1, "pr-1": 2}
    reseq = tuple(_with_seq(r, seqs[r.revision.revision_id]) for r in revisions)
    history = ListingHistory(revisions=reseq, precedence_evidence=evidence)
    assert history.revisions[2].revision.supersedes == ("pr-2",)
    assert history.revisions[2].revision.arrival_seq == 0


def test_identical_evidence_twice_is_rejected() -> None:
    revisions, evidence = pause_resume_chain()
    with pytest.raises(ValidationError, match="重复证据"):
        ListingHistory(revisions=revisions, precedence_evidence=(*evidence, evidence[0]))


def test_replayed_listing_payload_is_not_a_new_revision() -> None:
    first = listing("x-1", BTC, (iv(T0, None),))
    replay = first.model_copy(
        update={
            "revision": first.revision.model_copy(update={"revision_id": "x-2", "arrival_seq": 7})
        }
    )
    with pytest.raises(ValidationError, match="重复 payload"):
        ListingHistory(revisions=(first, replay))


# ======================================================================================
# UniverseSelectionSpec 与其绑定（ADR-0024 §5；矩阵 #8、#11、#13）
# ======================================================================================


def test_first_slice_spec_and_its_binding() -> None:
    spec = first_slice_spec()
    bound = spec.binding()
    assert (bound.name, bound.version) == ("binance.spot.btc-eth", "1.0.0")
    assert bound.spec_hash == spec.content_hash()
    assert bound.binds(spec)
    assert spec.symbols == ("BTCUSDT", "ETHUSDT")
    assert spec.filters == ()


def test_spec_hash_is_input_order_independent() -> None:
    a = first_slice_spec(symbols=("ETHUSDT", "BTCUSDT"), filters=(a_filter("b"), a_filter("a")))
    b = first_slice_spec(symbols=("BTCUSDT", "ETHUSDT"), filters=(a_filter("a"), a_filter("b")))
    assert a.content_hash() == b.content_hash()
    assert [f.filter_id for f in a.filters] == ["a", "b"]


@pytest.mark.parametrize(
    "overrides",
    [
        {"version": "1.0.1"},
        {"name": "binance.spot.btc-eth-sol"},
        {"symbols": ("BTCUSDT",)},
        {"instrument_type": InstrumentType.PERPETUAL},
        {"venue": "okx"},
        {"filters": (a_filter(),)},
        {"filters": (a_filter(threshold=1.0),)},
        {"filters": (a_filter(comparator=FilterComparator.AT_MOST),)},
    ],
)
def test_semantic_spec_change_changes_hash_and_breaks_the_binding(
    overrides: dict[str, Any],
) -> None:
    base = first_slice_spec()
    changed = first_slice_spec(**overrides)
    assert changed.content_hash() != base.content_hash()
    assert not base.binding().binds(changed)


@pytest.mark.parametrize("version", ["1.0", "01.0.0", "v1.0.0", "1.0.0.0", "١.0.0", "1.0.0-"])
def test_spec_and_binding_versions_are_strict_semver(version: str) -> None:
    with pytest.raises(ValidationError):
        first_slice_spec(version=version)
    with pytest.raises(ValidationError):
        UniverseSpecBinding(name="binance.spot.btc-eth", version=version, spec_hash=HASH)


@pytest.mark.parametrize("name", ["", "Binance.spot", "binance..spot", "binance spot", "1binance"])
def test_spec_name_is_a_binding_identifier(name: str) -> None:
    with pytest.raises(ValidationError):
        first_slice_spec(name=name)


@pytest.mark.parametrize(
    "source",
    ["current_listings", "today_symbols", "final_volume", "survivors", "latest_listings", ""],
)
def test_current_or_final_candidate_sources_do_not_exist(source: str) -> None:
    payload = {**wire(first_slice_spec()), "candidate_source": source}
    with pytest.raises(ValidationError):
        UniverseSelectionSpec.model_validate(payload)


@pytest.mark.parametrize("basis", ["final", "current", "latest", "full_history", ""])
def test_filter_metrics_must_be_point_in_time(basis: str) -> None:
    payload = {**wire(a_filter()), "metric_basis": basis}
    with pytest.raises(ValidationError):
        UniverseFilter.model_validate(payload)


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
def test_filter_threshold_must_be_finite(bad: float) -> None:
    with pytest.raises(ValidationError):
        a_filter(threshold=bad)


def test_filters_must_be_declared_explicitly() -> None:
    payload = wire(first_slice_spec())
    del payload["filters"]
    with pytest.raises(ValidationError, match="filters"):
        UniverseSelectionSpec.model_validate(payload)


@pytest.mark.parametrize(
    "overrides",
    [
        {"symbols": ()},
        {"symbols": ("BTCUSDT", "BTCUSDT")},
        {"symbols": ("BTCUSDT", " ")},
        {"filters": (a_filter("x"), a_filter("x", threshold=2.0))},
        {"venue": " "},
    ],
)
def test_spec_scope_is_non_empty_and_duplicate_free(overrides: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        first_slice_spec(**overrides)


def test_universe_spec_has_no_ref_or_kind() -> None:
    """矩阵 #13：spec 与其绑定都不是 `Ref`，也不携带 `Kind`。"""
    for model in (UniverseSelectionSpec, UniverseSpecBinding, UniverseFilter):
        assert "kind" not in model.model_fields
        assert "lineage" not in model.model_fields
        text = json.dumps(_schema(model))
        assert '"Ref"' not in text and '"Kind"' not in text
        assert "#/$defs/Ref" not in text and "#/$defs/Kind" not in text
    bound = first_slice_spec().binding()
    with pytest.raises(ValidationError):
        Ref.model_validate(wire(bound))
    with pytest.raises(ValidationError):
        UniverseSpecBinding.model_validate(
            {"kind": "dataset", "name": "binance_spot", "version": "1.0.0", "spec_hash": HASH}
        )
    with pytest.raises(ValidationError):
        UniverseSpecBinding.model_validate(
            {
                "name": "binance.spot.btc-eth",
                "version": "1.0.0",
                "spec_hash": HASH,
                "kind": "dataset",
            }
        )


def test_binding_does_not_match_a_foreign_hash() -> None:
    spec = first_slice_spec()
    forged = UniverseSpecBinding(name=spec.name, version=spec.version, spec_hash=OTHER_HASH)
    assert not forged.binds(spec)


# ======================================================================================
# 成员与排除（ADR-0024 §4、§6）
# ======================================================================================


def test_filtered_exclusion_requires_a_filter_id_and_others_forbid_it() -> None:
    assert exclusion(BTC, ExclusionReason.FILTERED, filter_id="liquidity").filter_id == "liquidity"
    with pytest.raises(ValidationError, match="filter_id"):
        exclusion(BTC, ExclusionReason.FILTERED)
    with pytest.raises(ValidationError, match="filter_id"):
        exclusion(BTC, ExclusionReason.NOT_TRADABLE, filter_id="liquidity")


def test_conflict_is_not_an_exclusion_reason() -> None:
    """competing heads 使构建 fail closed，不能被记成"排除"后继续。"""
    assert {reason.value for reason in ExclusionReason} == {"not_tradable", "filtered"}
    payload = {**wire(exclusion(BTC)), "reason": "conflict"}
    with pytest.raises(ValidationError):
        UniverseExclusion.model_validate(payload)


@pytest.mark.parametrize(
    "span",
    [
        {"effective_from": SIM},
        {"effective_until": SIM_END},
        {"effective_from": SIM, "effective_until": SIM},
        {"effective_from": SIM_END, "effective_until": SIM},
    ],
)
def test_entry_spans_are_both_or_neither_and_non_empty(span: dict[str, Any]) -> None:
    with pytest.raises(ValidationError, match="effective"):
        member(BTC, **span)
    with pytest.raises(ValidationError, match="effective"):
        exclusion(BTC, **span)


@pytest.mark.parametrize("field", ["members", "exclusions", "lineage", "evidence_gaps"])
def test_required_lists_may_be_explicitly_empty(field: str) -> None:
    """lineage 为空只在没有任何 member / exclusion 引用 listing revision 时合法（无候选）。"""
    extra: dict[str, Any] = {"members": (), "exclusions": ()} if field == "lineage" else {}
    empty = manifest(**{field: ()}, **extra)
    assert getattr(empty, field) == ()
    payload = {**wire(manifest(**extra)), field: []}
    assert getattr(ResearchDatasetManifest.model_validate_json(json.dumps(payload)), field) == ()


@pytest.mark.parametrize(
    "field",
    [
        "dataset",
        "point_in_time",
        "universe_spec",
        "members",
        "exclusions",
        "lineage",
        "quality_report_ids",
        "evidence_gaps",
    ],
)
def test_missing_manifest_binding_is_rejected(field: str) -> None:
    """ADR-0024 矩阵 #10、ADR-0023 矩阵 #22：缺字段不能被默认值伪装成空清单。"""
    payload = wire(manifest())
    del payload[field]
    with pytest.raises(ValidationError, match=field):
        ResearchDatasetManifest.model_validate(payload)
    with pytest.raises(ValidationError, match=field):
        ResearchDatasetManifest.model_validate_json(json.dumps(payload))
    assert ResearchDatasetManifest.model_fields[field].is_required()
    assert "default" not in _schema(ResearchDatasetManifest)["properties"][field]


def test_point_mode_rejects_duplicate_members() -> None:
    with pytest.raises(ValidationError, match="重复"):
        manifest(members=(member(BTC, "lr-1"), member(BTC, "lr-3")))


def test_point_mode_rejects_member_that_is_also_excluded() -> None:
    with pytest.raises(ValidationError, match="既是成员又被排除"):
        manifest(exclusions=(exclusion(BTC),))


def test_point_mode_rejects_duplicate_exclusions() -> None:
    with pytest.raises(ValidationError, match="重复"):
        manifest(
            members=(),
            exclusions=(exclusion(BTC), exclusion(BTC, ExclusionReason.FILTERED, filter_id="f")),
        )


def test_point_mode_entries_must_not_carry_spans() -> None:
    with pytest.raises(ValidationError, match="不得带生效区间"):
        manifest(members=(member(BTC, effective_from=SIM, effective_until=SIM_END),))


def test_all_members_all_excluded_or_no_candidates_are_legal() -> None:
    assert manifest(exclusions=()).exclusions == ()
    assert manifest(members=(), exclusions=(exclusion(BTC), exclusion(ETH))).members == ()
    assert manifest(members=(), exclusions=()).members == ()


def test_interval_mode_member_then_excluded_after_a_pause_is_legal() -> None:
    result = manifest(
        point_in_time=interval_pit(),
        members=(member(BTC, effective_from=SIM, effective_until=SIM_MID),),
        exclusions=(exclusion(BTC, effective_from=SIM_MID, effective_until=SIM_END),),
    )
    assert result.members[0].effective_until == result.exclusions[0].effective_from


def test_interval_mode_overlapping_member_and_exclusion_is_rejected() -> None:
    with pytest.raises(ValidationError, match="既是成员又被排除"):
        manifest(
            point_in_time=interval_pit(),
            members=(
                member(BTC, effective_from=SIM, effective_until=SIM_MID + timedelta(hours=1)),
            ),
            exclusions=(exclusion(BTC, effective_from=SIM_MID, effective_until=SIM_END),),
        )


def test_interval_mode_overlapping_member_spans_are_rejected() -> None:
    with pytest.raises(ValidationError, match="重叠"):
        manifest(
            point_in_time=interval_pit(),
            members=(
                member(BTC, "lr-1", effective_from=SIM, effective_until=SIM_MID),
                member(BTC, "lr-3", effective_from=SIM, effective_until=SIM_END),
            ),
        )


def test_interval_mode_entries_need_spans_inside_the_window() -> None:
    with pytest.raises(ValidationError, match="必须给出生效区间"):
        manifest(point_in_time=interval_pit(), members=(member(BTC),))
    with pytest.raises(ValidationError, match="落在 simulation 区间内"):
        manifest(
            point_in_time=interval_pit(),
            members=(member(BTC, effective_from=SIM - timedelta(days=1), effective_until=SIM_MID),),
        )
    with pytest.raises(ValidationError, match="落在 simulation 区间内"):
        manifest(
            point_in_time=interval_pit(),
            members=(
                member(BTC, effective_from=SIM_MID, effective_until=SIM_END + timedelta(days=1)),
            ),
        )


# ======================================================================================
# ResearchDatasetManifest：自身、上游、PIT、lineage、质量与证据缺口
# ======================================================================================


@pytest.mark.parametrize("zone", [z for z in Zone if z is not Zone.RESEARCH_DATASET])
def test_self_dataset_must_be_a_research_dataset(zone: Zone) -> None:
    with pytest.raises(ValidationError, match="research_dataset"):
        manifest(dataset=dataset(zone=zone))


@pytest.mark.parametrize("table", ["first_slice", "Research.first", "research.first.x", ""])
def test_self_dataset_table_must_be_namespace_table(table: str) -> None:
    with pytest.raises(ValidationError):
        manifest(dataset=dataset(table=table))


def test_self_dataset_is_not_confused_with_upstream() -> None:
    upstream = {**UPSTREAM, "research.first_slice_1m": "8000"}
    with pytest.raises(ValidationError, match="上游"):
        manifest(point_in_time=pit(snapshot_bindings=upstream))


@pytest.mark.parametrize("required", [LISTINGS_TABLE, QUALITY_REPORTS_TABLE])
def test_listing_and_quality_snapshots_are_required(required: str) -> None:
    upstream = {k: v for k, v in UPSTREAM.items() if k != required}
    with pytest.raises(ValidationError, match=required):
        manifest(point_in_time=pit(snapshot_bindings=upstream))


def test_at_least_one_upstream_snapshot_is_required() -> None:
    payload = wire(manifest())
    payload["point_in_time"]["snapshot_bindings"] = {}
    with pytest.raises(ValidationError):
        ResearchDatasetManifest.model_validate(payload)


def test_pit_spec_is_the_single_source_of_times_and_policies() -> None:
    """manifest 不复制第二套 simulation / cutoff / snapshot / policy 字段。"""
    duplicated = {
        "simulation_time",
        "simulation_start",
        "simulation_end",
        "knowledge_cutoff",
        "snapshot_bindings",
        "point_in_time_binding",
        "availability_bindings",
        "precedence_bindings",
        "parser_bindings",
    }
    assert not duplicated & set(ResearchDatasetManifest.model_fields)
    base = wire(manifest())
    for field in ("knowledge_cutoff", "simulation_time", "snapshot_bindings"):
        payload = {**base, field: base["point_in_time"][field]}
        with pytest.raises(ValidationError, match=field):
            ResearchDatasetManifest.model_validate(payload)


@pytest.mark.parametrize(
    "field", ["availability_bindings", "precedence_bindings", "parser_bindings"]
)
def test_missing_policy_or_parser_binding_is_rejected(field: str) -> None:
    payload = wire(manifest())
    payload["point_in_time"][field] = []
    with pytest.raises(ValidationError):
        ResearchDatasetManifest.model_validate(payload)
    del payload["point_in_time"][field]
    with pytest.raises(ValidationError):
        ResearchDatasetManifest.model_validate(payload)


def test_pit_binding_role_cannot_be_impersonated() -> None:
    payload = wire(manifest())
    payload["point_in_time"]["parser_bindings"][0]["role"] = "availability"
    with pytest.raises(ValidationError, match="role"):
        ResearchDatasetManifest.model_validate(payload)


@pytest.mark.parametrize(
    "universe_spec",
    [
        {"kind": "dataset", "name": "binance_spot", "version": "1.0.0"},
        {
            "role": "point_in_time",
            "policy_id": "binance.spot.btc-eth",
            "version": "1.0.0",
            "policy_hash": HASH,
        },
        {"name": "binance.spot.btc-eth", "version": "1.0.0"},
    ],
)
def test_universe_binding_is_a_dedicated_structure(universe_spec: dict[str, Any]) -> None:
    payload = {**wire(manifest()), "universe_spec": universe_spec}
    with pytest.raises(ValidationError):
        ResearchDatasetManifest.model_validate(payload)


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"canonical_table": "canonical.bars_1m"}, "canonical.bars_1m"),
        ({"raw_table": "raw.binance_spot_klines_1m"}, "raw.binance_spot_klines_1m"),
        ({"source_table": "raw.other_payloads"}, "raw.other_payloads"),
    ],
)
def test_lineage_tables_need_upstream_snapshots(overrides: dict[str, Any], message: str) -> None:
    with pytest.raises(ValidationError, match=message):
        manifest(lineage=(lineage(**overrides),))


@pytest.mark.parametrize(
    "overrides",
    [
        {"canonical_table": "raw.binance_spot_agg_trades"},
        {"raw_table": "canonical.trades"},
        {"source_table": "quality.data_quality_reports"},
        {"canonical_revision_id": " "},
        {"canonical_table": "canonical"},
    ],
)
def test_lineage_hops_are_in_their_namespaces_and_non_empty(overrides: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        lineage(**overrides)


def test_duplicate_lineage_identity_is_rejected() -> None:
    with pytest.raises(ValidationError, match="lineage"):
        manifest(lineage=(lineage("c-1"), lineage("c-1", raw_revision_id="other")))


def test_no_candidates_with_all_lists_empty_is_legal() -> None:
    result = manifest(members=(), exclusions=(), lineage=(), evidence_gaps=())
    assert (result.members, result.exclusions, result.lineage) == ((), (), ())


# --------------------------------------------------------------------------------------
# listing revision 引用：episode 归属唯一、不得悬空（B2 R2）
# --------------------------------------------------------------------------------------


def test_point_mode_listing_revision_cannot_be_claimed_by_two_members() -> None:
    with pytest.raises(ValidationError, match="被多个 episode 认领"):
        manifest(members=(member(BTC, "lr-1"), member(ETH, "lr-1")))


def test_listing_revision_cannot_be_claimed_across_member_and_exclusion() -> None:
    with pytest.raises(ValidationError, match="被多个 episode 认领"):
        manifest(exclusions=(exclusion(degraded("SOLUSDT", T1), revision_id="lr-1"),))


def test_listing_revision_cannot_be_claimed_by_two_exclusions() -> None:
    sol, ada = degraded("SOLUSDT", T1), degraded("ADAUSDT", T1)
    with pytest.raises(ValidationError, match="被多个 episode 认领"):
        manifest(
            members=(),
            exclusions=(exclusion(sol, revision_id="lx-1"), exclusion(ada, revision_id="lx-1")),
        )


def test_interval_mode_listing_revision_cannot_be_claimed_by_two_episodes() -> None:
    with pytest.raises(ValidationError, match="被多个 episode 认领"):
        manifest(
            point_in_time=interval_pit(),
            members=(member(BTC, "lr-1", effective_from=SIM, effective_until=SIM_MID),),
            exclusions=(
                exclusion(ETH, revision_id="lr-1", effective_from=SIM_MID, effective_until=SIM_END),
            ),
        )


def test_same_listing_revision_may_span_disjoint_intervals_of_one_episode() -> None:
    """同一 head 在同一 episode 的多个不重叠生效区间中重复出现是合法的（例如成员 → 过滤排除）。"""
    result = manifest(
        point_in_time=interval_pit(),
        members=(member(BTC, "lr-1", effective_from=SIM, effective_until=SIM_MID),),
        exclusions=(
            exclusion(
                BTC,
                ExclusionReason.FILTERED,
                revision_id="lr-1",
                filter_id="liquidity",
                effective_from=SIM_MID,
                effective_until=SIM_END,
            ),
        ),
    )
    entries: tuple[UniverseMember | UniverseExclusion, ...] = (*result.members, *result.exclusions)
    assert {e.listing_revision_id for e in entries} == {"lr-1"}


def test_member_listing_revision_without_listing_lineage_is_rejected() -> None:
    with pytest.raises(ValidationError, match="'lr-2' 在 lineage 中没有"):
        manifest(lineage=(lineage("c-1"), listing_lineage("lr-1")))


def test_exclusion_listing_revision_without_listing_lineage_is_rejected() -> None:
    sol = degraded("SOLUSDT", T1)
    with pytest.raises(ValidationError, match="'lx-sol' 在 lineage 中没有"):
        manifest(
            exclusions=(exclusion(sol, revision_id="lx-sol"),),
            lineage=complete_lineage((member(BTC), member(ETH, "lr-2")), ()),
        )


def test_unrelated_canonical_lineage_cannot_stand_in_for_listing_lineage() -> None:
    """同一 revision ID 出现在 `canonical.trades` 的 lineage 中，不等于 listing lineage 存在。"""
    with pytest.raises(ValidationError, match="'lr-2' 在 lineage 中没有"):
        manifest(lineage=(lineage("c-1"), listing_lineage("lr-1"), lineage("lr-2")))


def test_complete_listing_lineage_for_members_and_exclusions_is_legal() -> None:
    sol = degraded("SOLUSDT", T1)
    result = manifest(exclusions=(exclusion(sol, revision_id="lx-sol"),))
    listed = {
        i.canonical_revision_id for i in result.lineage if i.canonical_table == LISTINGS_TABLE
    }
    assert listed == {"lr-1", "lr-2", "lx-sol"}


def test_listing_lineage_is_still_deduplicated_by_identity() -> None:
    duplicate = listing_lineage("lr-1").model_copy(update={"raw_revision_id": "row-other"})
    with pytest.raises(ValidationError, match="lineage"):
        manifest(lineage=(*complete_lineage((member(BTC), member(ETH, "lr-2")), ()), duplicate))


def test_listing_lineage_hops_need_upstream_snapshots() -> None:
    upstream = {k: v for k, v in UPSTREAM.items() if k != "raw.listing_payloads"}
    with pytest.raises(ValidationError, match="raw.listing_payloads"):
        manifest(point_in_time=pit(snapshot_bindings=upstream))


def test_lineage_third_hop_is_a_generic_raw_source() -> None:
    """第三跳是通用 Raw source（归档或 REST 响应载荷），不再冻结为 archive。"""
    fields = set(SelectedRevisionLineage.model_fields)
    assert {"source_table", "source_revision_id"} <= fields
    assert not {name for name in fields if "archive" in name}
    payload = {**wire(lineage()), "archive_table": "raw.binance_spot_archives"}
    with pytest.raises(ValidationError):
        SelectedRevisionLineage.model_validate(payload)


def test_quality_reports_are_required_non_empty_and_unique() -> None:
    with pytest.raises(ValidationError):
        manifest(quality_report_ids=())
    with pytest.raises(ValidationError, match="quality_report_ids"):
        manifest(quality_report_ids=("qr-1", "qr-1"))
    with pytest.raises(ValidationError):
        manifest(quality_report_ids=(" ",))


def test_evidence_gap_must_reference_a_listed_quality_report() -> None:
    with pytest.raises(ValidationError, match="qr-404"):
        manifest(evidence_gaps=(gap(quality_report_id="qr-404"),))


def test_evidence_gap_table_needs_an_upstream_snapshot() -> None:
    with pytest.raises(ValidationError, match="canonical.bars_1m"):
        manifest(evidence_gaps=(gap(table="canonical.bars_1m"),))


def test_evidence_gap_on_a_listing_revision_is_legal() -> None:
    result = manifest(evidence_gaps=(gap("lr-1", table=LISTINGS_TABLE),))
    assert result.evidence_gaps[0].table == LISTINGS_TABLE


def test_duplicate_evidence_gap_is_rejected() -> None:
    with pytest.raises(ValidationError, match="evidence_gaps"):
        manifest(evidence_gaps=(gap("c-1"), gap("c-1", gap="another description")))


def test_manifest_hash_is_order_independent() -> None:
    base = manifest(
        exclusions=(exclusion(degraded("SOLUSDT", T1)), exclusion(degraded("ADAUSDT", T1))),
        evidence_gaps=(gap("c-1"), gap("c-2", quality_report_id="qr-2")),
    )
    reordered = manifest(
        point_in_time=pit(snapshot_bindings=dict(reversed(list(UPSTREAM.items())))),
        members=(member(ETH, "lr-2"), member(BTC)),
        exclusions=(exclusion(degraded("ADAUSDT", T1)), exclusion(degraded("SOLUSDT", T1))),
        lineage=tuple(reversed(base.lineage)),
        quality_report_ids=("qr-2", "qr-1"),
        evidence_gaps=(gap("c-2", quality_report_id="qr-2"), gap("c-1")),
    )
    assert reordered == base
    assert reordered.content_hash() == base.content_hash()


@pytest.mark.parametrize(
    "overrides",
    [
        {"point_in_time": pit(knowledge_cutoff=KNOWN + timedelta(seconds=1))},
        {"point_in_time": pit(simulation_time=SIM + timedelta(minutes=1))},
        {"universe_spec": first_slice_spec(version="1.0.1").binding()},
        {"members": (member(BTC),)},
        {"members": (member(BTC, "lr-other"), member(ETH, "lr-2"))},
        {"lineage": (lineage("c-1"), listing_lineage("lr-1"), listing_lineage("lr-2"))},
        {"quality_report_ids": ("qr-1",)},
        {"evidence_gaps": ()},
        {"dataset": dataset(snapshot_id="9002")},
    ],
)
def test_semantic_manifest_change_changes_the_hash(overrides: dict[str, Any]) -> None:
    assert manifest(**overrides).content_hash() != manifest().content_hash()


# ======================================================================================
# 通用：不可变、严格、两入口一致、copy 重新校验、Schema 与运行时同源
# ======================================================================================


@pytest.mark.parametrize("model", B2_MODELS, ids=lambda m: m.__name__)
def test_round_trip_through_json_and_python(model: type[Contract]) -> None:
    instance = valid_instances()[model]
    from_json = model.model_validate_json(instance.model_dump_json())
    from_python = model.model_validate(wire(instance))
    assert from_json == from_python == instance
    assert from_json.content_hash() == instance.content_hash()


@pytest.mark.parametrize("model", B2_MODELS, ids=lambda m: m.__name__)
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


@pytest.mark.parametrize("model", B2_MODELS, ids=lambda m: m.__name__)
def test_content_hash_is_stable_and_value_sensitive(model: type[Contract]) -> None:
    first = valid_instances()[model]
    second = valid_instances()[model]
    assert first.content_hash() == second.content_hash()
    bumped = first.model_copy(update={"schema_version": "2.99.0"})  # another envelope
    assert first.content_hash() != bumped.content_hash()


def test_copy_with_update_revalidates() -> None:
    with pytest.raises(ValidationError, match="research_dataset"):
        manifest().model_copy(update={"dataset": dataset(zone=Zone.CANONICAL)})
    with pytest.raises(ValidationError, match="既是成员又被排除"):
        manifest().model_copy(update={"exclusions": (exclusion(BTC),)})
    with pytest.raises(ValidationError, match="status=listed"):
        listing("d-1", BTC, (iv(T0, None),)).model_copy(update={"status": ListingStatus.DELISTED})
    with pytest.raises(ValidationError):
        first_slice_spec().model_copy(update={"symbols": ("BTCUSDT", "BTCUSDT")})
    with pytest.raises(ValidationError, match="跨 observation_key"):
        ListingHistory(revisions=(listing("btc-1", BTC, (iv(T0, None),)),)).model_copy(
            update={"precedence_evidence": (edge("eth-2", "btc-1", ETH),)}
        )


@pytest.mark.parametrize("model", B2_MODELS, ids=lambda m: m.__name__)
def test_schema_required_matches_runtime_required(model: type[Contract]) -> None:
    runtime = {name for name, field in model.model_fields.items() if field.is_required()}
    assert set(_schema(model).get("required", [])) == runtime
    assert _schema(model)["properties"]["schema_version"]["default"] == CONTRACT_SCHEMA_VERSION
    assert _schema(model)["additionalProperties"] is False


def test_schema_expresses_the_field_level_constraints() -> None:
    spec = _schema(UniverseSelectionSpec)
    props = spec["properties"]
    assert props["name"]["pattern"] == BINDING_ID_PATTERN
    assert props["version"]["pattern"] == SEMVER_PATTERN
    assert props["symbols"]["minItems"] == 1
    assert props["symbols"]["uniqueItems"] is True
    assert "default" not in props["filters"]
    assert spec["$defs"]["UniverseCandidateSource"]["enum"] == ["point_in_time_listings"]
    assert spec["$defs"]["MetricBasis"]["enum"] == ["point_in_time"]

    bound = _schema(UniverseSpecBinding)["properties"]
    assert bound["name"]["pattern"] == BINDING_ID_PATTERN
    assert bound["version"]["pattern"] == SEMVER_PATTERN
    assert bound["spec_hash"]["pattern"] == SHA256_PATTERN

    assert _schema(StableEpisodeKey)["properties"]["basis"]["const"] == "stable_product_id"
    assert _schema(DegradedEpisodeKey)["properties"]["basis"]["const"] == "degraded_symbol_start"

    rev = _schema(ListingRevision)
    assert rev["properties"]["tradable_intervals"]["minItems"] == 1
    assert set(rev["properties"]["episode"]["discriminator"]["mapping"]) == set(
        EpisodeIdentityBasis
    )
    assert set(rev["$defs"]["ListingStatus"]["enum"]) == {s.value for s in ListingStatus}

    man = _schema(ResearchDatasetManifest)
    assert man["properties"]["quality_report_ids"]["minItems"] == 1
    assert man["properties"]["quality_report_ids"]["uniqueItems"] is True
    assert set(man["$defs"]["ExclusionReason"]["enum"]) == {"not_tradable", "filtered"}

    for model in (SelectedRevisionLineage, AvailabilityEvidenceGap):
        for name, prop in _schema(model)["properties"].items():
            if name.endswith("_table") or name == "table":
                assert prop["pattern"] == SNAPSHOT_TABLE_PATTERN


# ======================================================================================
# 静态检查：不以 arrival / hash / 墙钟决定优先级（ADR-0024 矩阵 #17）
# ======================================================================================

_NON_PRIORITY_FIELDS = {
    "arrival_seq",
    "payload_hash",
    "ingest_time",
    "knowledge_time",
    "available_time",
    "source_revision_time",
}


def _module_tree() -> ast.Module:
    return ast.parse(UNIVERSE_MODULE.read_text(encoding="utf-8"), filename=str(UNIVERSE_MODULE))


def test_universe_module_never_reads_arrival_seq_or_payload_hash() -> None:
    attrs = {node.attr for node in ast.walk(_module_tree()) if isinstance(node, ast.Attribute)}
    assert not attrs & {"arrival_seq", "payload_hash"}


def test_no_ordering_call_uses_arrival_hash_or_time_fields() -> None:
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


def test_universe_module_never_reads_the_wall_clock() -> None:
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
    for model in B2_MODELS:
        for field in model.model_fields:
            parts = set(field.split("_"))
            assert not {"latest", "winner", "valid", "verified", "resolved"} & parts, (
                f"{model.__name__}.{field}"
            )


# ======================================================================================
# 注册表与冻结契约：只加不改
# ======================================================================================


def test_every_b2_model_is_registered_and_exported(tmp_path: Path) -> None:
    written = export_json_schemas(tmp_path)
    for model in B2_MODELS:
        assert model in CONTRACT_MODELS
        committed = CURRENT_SCHEMA_DIR / f"{model.__name__}.schema.json"
        assert committed.read_bytes() == written[model.__name__].read_bytes()


def test_b2_only_appends_to_the_registry() -> None:
    """B2 的 13 个模型紧接在 B2 之前的 46 个之后；之后的批次（B3 起）只能继续追加在其后。"""
    names = tuple(model.__name__ for model in CONTRACT_MODELS)
    b2_end = len(PRE_B2_MODEL_NAMES) + len(B2_MODELS)
    assert names[: len(PRE_B2_MODEL_NAMES)] == PRE_B2_MODEL_NAMES
    assert names[len(PRE_B2_MODEL_NAMES) : b2_end] == tuple(model.__name__ for model in B2_MODELS)
    assert b2_end == 59
    assert len(CONTRACT_MODELS) == 135


@pytest.mark.parametrize("name", sorted(FROZEN_SCHEMA_SHA256))
def test_frozen_contract_schemas_are_byte_identical_to_pre_b2(name: str, tmp_path: Path) -> None:
    # ADR-0052 §4: the 2.1.0 bump may change only the envelope default of these schemas.
    committed = as_published_at_2_0_0((CURRENT_SCHEMA_DIR / f"{name}.schema.json").read_bytes())
    assert hashlib.sha256(committed).hexdigest() == FROZEN_SCHEMA_SHA256[name]
    regenerated = as_published_at_2_0_0(export_json_schemas(tmp_path)[name].read_bytes())
    assert hashlib.sha256(regenerated).hexdigest() == FROZEN_SCHEMA_SHA256[name]


def test_frozen_contract_fields_are_unchanged() -> None:
    assert list(Instrument.model_fields) == [
        "schema_version",
        "venue",
        "symbol",
        "instrument_type",
        "base",
        "quote",
    ]
    assert list(DatasetRef.model_fields) == [
        "schema_version",
        "zone",
        "table",
        "snapshot_id",
        "time_range_start",
        "time_range_end",
    ]


def test_contract_version_and_kind_are_unchanged() -> None:
    """ADR-0024 §5：不新增 `Kind`；版本策略：`CONTRACT_SCHEMA_VERSION` 保持 2.0.0。"""
    # ADR-0052 §4 raised the minor to 2.1.0 and ADR-0055 to 2.2.0; this batch changed no version.
    assert CONTRACT_SCHEMA_VERSION == "2.2.0"
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


def test_b2_models_are_not_legacy_v1_models() -> None:
    names = {p.stem.removesuffix(".schema") for p in LEGACY_SCHEMA_DIR.glob("*.schema.json")}
    assert names == set(V1_MODEL_NAMES)
    assert len(names) == 35
    for model in B2_MODELS:
        assert model.__name__ not in V1_MODEL_NAMES
        assert not (LEGACY_SCHEMA_DIR / f"{model.__name__}.schema.json").exists()


def test_module_exports_every_b2_model() -> None:
    for model in B2_MODELS:
        assert model.__name__ in universe.__all__

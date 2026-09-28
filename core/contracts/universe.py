"""历史可交易 universe 与 `ResearchDatasetManifest` 契约（ADR-0024、ADR-0023 §6；Phase 1 B2）。

对应 docs/architecture/03-data.md §3、§4.5、§7.1、§7.3、§7.5。本模块只定义**契约**与可在本对象内证明
的不变量；不含 universe 选择器、PIT 执行器、数据库、Iceberg、Collector 或 Provider。

- **listing 历史**与静态 `Instrument` 分离：`Instrument` 字段不变，只作为 listing revision 内的静态
  描述；可交易时间、状态与修订由 `ListingRevision` 表达，并复用 B1 的 `RevisionRecord`（两轴时间、
  availability 判定、append-only revision）与 `RevisionGraph`（supersedes DAG）。
- **episode 身份**：优先 venue 原生稳定产品 ID（`StableEpisodeKey`）；缺失时显式退化为
  `(venue, instrument_type, symbol, tradable_from)`（`DegradedEpisodeKey`）。episode 键即 revision
  的 `observation_key`，因此跨 episode 的 supersedes 由 `RevisionGraph` 的键归属规则拒绝。
- **`UniverseSelectionSpec`**：独立版本化契约，以 `name + SemVer + content_hash()` 形成
  `UniverseSpecBinding`；不新增 `Kind`、不使用 `Ref`、不借用 `Kind.DATASET`（ADR-0024 §5）。
- **`ResearchDatasetManifest`**：审计契约，绑定自身 `DatasetRef`、完整 `PointInTimeSpec`（上游
  snapshot、simulation、`knowledge_cutoff` 与全部 policy / parser 绑定的唯一来源）、universe spec 绑
  定、成员与排除清单、选中 revision 的 lineage、质量报告与 availability 证据缺口。

**追加顺序 ≠ 修订优先级**（ADR-0023 §4）：本模块不读取 `arrival_seq`，也不以墙钟或 payload hash 排序
或打破冲突。集合语义的序列按稳定身份规范排序只为内容哈希唯一，不表示先后。

**诚实边界**：契约层只校验结构与声明。Registry 中是否存在被绑定的版本、哈希是否等于真实内容、
snapshot 是否存在、成员清单是否真的由 spec + snapshot + 两个截止按 maximal-head 算法得出、逐行 PIT
是否正确、lineage 是否是完整闭包、Runner 是否强制消费 manifest，都属于未来的 Registry、存储、PIT /
universe 执行器与 Runner（批次 B3 ~ F 及以后）。

**有界 evidence manifest（ADR-0077，自契约 2.3.0，additive）**：
`ResearchDatasetEvidenceManifest`（"v3" 形态代号，不是契约 major）与 `ResearchDatasetManifest`
（v2 形态）并存；v2 模型的字段、校验、Schema 与内容哈希逐位不变。v3 只含固定大小字段：
成员、排除、lineage、证据缺口、质量报告与逐 chunk 证明不再内联，而是由六条内容寻址的有序
evidence stream 的根对象引用（`EvidenceStreamRef` / `EvidenceObjectRef`）经 manifest 内容哈希
承诺。流内记录复用 v2 记录模型，另加 `DatasetQualityReportRef` 与 `DatasetChunkProof`。
对象是否存在、字节是否匹配、流内容是否就是输入的派生，属 infrastructure 的 streaming verifier。
"""

from __future__ import annotations

import re
from datetime import UTC, date, datetime
from enum import StrEnum
from typing import Annotated, Final, Literal

from pydantic import Field, ValidationError, field_validator, model_validator

from core.contracts.catalog import BATCH_ID_PATTERN, SNAPSHOT_ID_PATTERN
from core.contracts.revision import (
    BINDING_ID_PATTERN,
    SNAPSHOT_TABLE_PATTERN,
    PointInTimeSpec,
    PolicyBinding,
    PolicyRole,
    PrecedenceEvidence,
    RevisionGraph,
    RevisionRecord,
)
from core.domain.base import (
    NAME_PATTERN,
    SEMVER_PATTERN,
    ContentHash,
    Contract,
    UtcDatetime,
    canonical_json,
    omit_none,
)
from core.domain.specs import ADR_0088_VERSION, DatasetRef, Instrument, InstrumentType, Zone

__all__ = [
    "ADR_0077_VERSION",
    "DATASET_CHUNK_INDEX_MAX",
    "DATASET_EVIDENCE_FORMAT",
    "DATASET_EVIDENCE_KEY_PATTERN",
    "DATASET_EVIDENCE_KEY_PREFIX",
    "DATASET_SELECTION_ID_PATTERN",
    "LISTINGS_TABLE",
    "LISTING_BACKFILL_ASSUMPTION_ID",
    "QUALITY_REPORTS_TABLE",
    "AvailabilityEvidenceGap",
    "DatasetChunkProof",
    "DatasetQualityReportRef",
    "DatasetQualitySubject",
    "DatasetRuleBinding",
    "DegradedEpisodeKey",
    "EpisodeIdentityBasis",
    "EvidenceObjectRef",
    "EvidenceStream",
    "EvidenceStreamRef",
    "ExclusionReason",
    "FilterComparator",
    "ListingEpisodeKey",
    "ListingHistory",
    "ListingRevision",
    "ListingStatus",
    "MetricBasis",
    "ResearchDatasetEvidenceManifest",
    "ResearchDatasetManifest",
    "SelectedRevisionLineage",
    "StableEpisodeKey",
    "TradableInterval",
    "UniverseCandidateSource",
    "UniverseExclusion",
    "UniverseFilter",
    "UniverseMember",
    "UniverseSelectionSpec",
    "UniverseSpecBinding",
    "dataset_chunk_batch_id",
    "dataset_evidence_key",
]

#: listing 历史表（03-data.md §7.1）；manifest 必须绑定它的 snapshot（ADR-0024 §6）。
LISTINGS_TABLE = "canonical.instrument_listings"
#: 质量报告表（03-data.md §5、§7.1）；manifest 必须绑定它的 snapshot 并引用所用报告。
QUALITY_REPORTS_TABLE = "quality.data_quality_reports"
#: ADR-0051（D-LIST）listing 回填假设的 availability 政策标识；`UniverseMember.assumption` 非空时
#: 必须绑定它（ADR-0088 决策 6，自契约 2.4.0）。
LISTING_BACKFILL_ASSUMPTION_ID: Final = "hlens.listing.observed-state-backfill-assumption"

NonEmptyStr = Annotated[str, Field(min_length=1)]
SnapshotTable = Annotated[str, Field(pattern=SNAPSHOT_TABLE_PATTERN)]
#: `dataset.table` 的格式复核：已发布 `DatasetRef.table` 不受约束，manifest 另行要求
#: `namespace.table`。
_SNAPSHOT_TABLE_RE = re.compile(SNAPSHOT_TABLE_PATTERN)

#: 排序占位：只用于把"未给生效区间"的条目排在一起；这类条目与带区间的条目不能共存（manifest 校验）。
_NO_SPAN = datetime.min.replace(tzinfo=UTC)


def _canonical_unique(values: tuple[str, ...], label: str) -> tuple[str, ...]:
    """集合语义的字符串序列：重复即拒绝；规范排序只为内容哈希唯一，不表示先后。"""
    if len(set(values)) != len(values):
        raise ValueError(f"{label} 不得包含重复项")
    return tuple(sorted(values))


def _namespace(table: str) -> str:
    return table.split(".", 1)[0]


# ======================================================================================
# Listing episode 与 revision（ADR-0024 §1 ~ §3）
# ======================================================================================


class TradableInterval(Contract):
    """一段历史可交易区间：UTC 半开区间 `[tradable_from, tradable_until)`（ADR-0024 §1）。

    `tradable_until` **必须显式给出**：`null` 表示仍可交易（开放区间），不提供默认值，避免"漏写结束
    时间"被读成"仍在交易"。区间非空：`tradable_from < tradable_until`。按 `simulation_time` 判断是否
    可交易：`tradable_from <= simulation_time < tradable_until`。
    """

    tradable_from: UtcDatetime
    tradable_until: UtcDatetime | None

    @model_validator(mode="after")
    def _non_empty(self) -> TradableInterval:
        if self.tradable_until is not None and self.tradable_until <= self.tradable_from:
            raise ValueError("tradable_until 必须晚于 tradable_from（半开区间不得为空或反向）")
        return self


class EpisodeIdentityBasis(StrEnum):
    """episode 身份的依据（ADR-0024 §2）：稳定产品 ID，或显式标注的退化键。"""

    STABLE_PRODUCT_ID = "stable_product_id"
    DEGRADED_SYMBOL_START = "degraded_symbol_start"


class StableEpisodeKey(Contract):
    """以 venue 原生稳定产品 ID 标识的 episode：`(venue, instrument_type, venue_product_id)`。

    symbol 改名仍属同一 episode（symbol 不在键内，由各 revision 的 `instrument.symbol` 记录变化）。
    `instrument_type` 在键内：venue 的产品 ID 命名空间可能按市场类型区分。

    `basis` 是来源声明：产品 ID 的字符串值可以恰好等于某时刻的 symbol，契约不从字符串相等推断冒充。

    **诚实边界**：来源是否真的提供了稳定 ID、该 ID 是否真的跨改名不变，属未来 Collector / Registry /
    质量证据，契约层无法证明。
    """

    basis: Literal[EpisodeIdentityBasis.STABLE_PRODUCT_ID]
    venue: NonEmptyStr
    instrument_type: InstrumentType
    venue_product_id: NonEmptyStr

    def observation_key(self) -> str:
        """该 episode 作为 revision `observation_key` 的规范串：规范 JSON，单射、与信封版本无关。"""
        return canonical_json(self.model_dump(mode="json", exclude={"schema_version"}))


class DegradedEpisodeKey(Contract):
    """来源不提供稳定产品 ID 时的退化键：`(venue, instrument_type, symbol, tradable_from)`。

    `basis` 显式标注这是退化身份，不能假装有稳定 ID。`tradable_from` 是该 episode 第一段可交易区间的
    起点，因此 symbol 复用（同名、不同起点）自然形成不同 episode；无稳定 ID 的改名同样形成新
    episode，由 `ListingRevision.renamed_from` 记录与旧 episode 的关联。起点被来源更正时也只能形成新
    episode 并写质量事件，不猜测合并（ADR-0024「失败与恢复语义」）。
    """

    basis: Literal[EpisodeIdentityBasis.DEGRADED_SYMBOL_START]
    venue: NonEmptyStr
    instrument_type: InstrumentType
    symbol: NonEmptyStr
    tradable_from: UtcDatetime

    def observation_key(self) -> str:
        """该 episode 作为 revision `observation_key` 的规范串：规范 JSON，单射、与信封版本无关。"""
        return canonical_json(self.model_dump(mode="json", exclude={"schema_version"}))


#: episode 键：按 `basis` 判别的两条路径；字段混用、缺字段由各自模型的 `extra="forbid"` 与 required
#: 拒绝。
ListingEpisodeKey = Annotated[StableEpisodeKey | DegradedEpisodeKey, Field(discriminator="basis")]


class ListingStatus(StrEnum):
    """listing 在该 revision 中的规范化状态类别（最小、稳定、可审计）。

    只冻结 universe 语义需要区分的三类，交易所原生状态原样记在 `ListingRevision.source_status`，因此
    来源新增状态不需要修改契约：

    - `listed`：最后一段可交易区间开放（`tradable_until = null`，起点可以是已公告的将来时刻）；
    - `suspended`：全部区间已关闭，来源表示为暂停（恢复时在同一 episode 追加新区间）；
    - `delisted`：全部区间已关闭，来源表示为下架。下架 episode 的历史区间与 revision 永远保留。
    """

    LISTED = "listed"
    SUSPENDED = "suspended"
    DELISTED = "delisted"


class ListingRevision(Contract):
    """一个 listing episode 的不可变 revision（ADR-0024 §1、§2）。

    - `revision`：B1 的 `RevisionRecord`，承载 revision 身份、来源、payload hash、`supersedes`、来源
      修订信息、两轴时间与 availability 判定；其 `observation_key` 必须等于
      `episode.observation_key()`；
    - `episode`：稳定产品 ID 或显式退化键；`instrument`：静态描述（已发布 `Instrument`，字段不变），
      venue / instrument_type 必须与键一致，且各字符串非空；
    - `tradable_intervals`：该 revision 所知的**全部**历史可交易区间，至少一段；输入顺序无关，按起点
      规范排序；区间不重叠、不相邻（相邻区间必须合并，保证同一历史只有一种表示）；开放区间只能是最后
      一段。暂停 / 恢复在同一 episode 保留多段区间；
    - `status` 与区间形状一致：`listed` ⇔ 最后一段开放；`source_status` / `status_reason` 是来源原
      文，没有就为空，不得伪造；
    - `renamed_from`：只用于无稳定 ID 的改名，指向旧 episode 的退化键：不得是自身，venue /
      instrument_type 相同，symbol 必须不同（同 symbol 是复用而不是改名，不得借此合并两个产品），起
      点必须更早。

    **诚实边界**：区间与状态是否符合来源事实、改名关联是否属实，属未来 Collector / 质量检查；跨
    revision 事实只在 `ListingHistory` 证明。
    """

    revision: RevisionRecord
    episode: ListingEpisodeKey
    instrument: Instrument
    tradable_intervals: tuple[TradableInterval, ...] = Field(min_length=1)
    status: ListingStatus
    source_status: NonEmptyStr | None = None
    status_reason: NonEmptyStr | None = None
    renamed_from: DegradedEpisodeKey | None = None

    @field_validator("tradable_intervals")
    @classmethod
    def _canonical_intervals(
        cls, value: tuple[TradableInterval, ...]
    ) -> tuple[TradableInterval, ...]:
        ordered = tuple(sorted(value, key=lambda interval: interval.tradable_from))
        for index, (prev, nxt) in enumerate(zip(ordered, ordered[1:], strict=False)):
            if prev.tradable_until is None:
                raise ValueError(
                    f"开放区间（tradable_until = null）只能是最后一段；第 {index} 段之后仍有区间"
                )
            if prev.tradable_until > nxt.tradable_from:
                raise ValueError("可交易区间不得重叠")
            if prev.tradable_until == nxt.tradable_from:
                raise ValueError("相邻的可交易区间必须合并为一段（同一历史只有一种表示）")
        return ordered

    @model_validator(mode="after")
    def _episode_consistency(self) -> ListingRevision:
        instrument = self.instrument
        for field in ("venue", "symbol", "base", "quote"):
            if not getattr(instrument, field):
                raise ValueError(f"instrument.{field} 不得为空")
        episode = self.episode
        if episode.venue != instrument.venue:
            raise ValueError("episode.venue 必须等于 instrument.venue")
        if episode.instrument_type is not instrument.instrument_type:
            raise ValueError("episode.instrument_type 必须等于 instrument.instrument_type")
        if isinstance(episode, StableEpisodeKey):
            if self.renamed_from is not None:
                raise ValueError("有稳定产品 ID 的改名保留同一 episode，不得关联其它 episode")
        else:
            if episode.symbol != instrument.symbol:
                raise ValueError(
                    "退化键的 symbol 必须等于 instrument.symbol（改名须形成新 episode）"
                )
            if episode.tradable_from != self.tradable_intervals[0].tradable_from:
                raise ValueError("退化键的 tradable_from 必须等于第一段可交易区间的起点")
            old = self.renamed_from
            if old is not None:
                if old.venue != episode.venue or old.instrument_type is not episode.instrument_type:
                    raise ValueError("renamed_from 必须与本 episode 同 venue、同 instrument_type")
                if old.symbol == episode.symbol:
                    raise ValueError("renamed_from 的 symbol 与本 episode 相同：这是复用而不是改名")
                if old.tradable_from >= episode.tradable_from:
                    raise ValueError("renamed_from 的 episode 必须早于本 episode 开始")
        if self.revision.observation_key != episode.observation_key():
            raise ValueError("revision.observation_key 必须等于 episode.observation_key()")
        is_open = self.tradable_intervals[-1].tradable_until is None
        if (self.status is ListingStatus.LISTED) != is_open:
            raise ValueError(
                "status=listed 当且仅当最后一段可交易区间开放（tradable_until = null）"
            )
        return self


class ListingHistory(Contract):
    """一组 listing revision 与 precedence 证据的聚合校验（ADR-0024 §1 ~ §4）。

    - 以各 revision 的 `RevisionRecord` 与全部 `PrecedenceEvidence` 构造 B1 `RevisionGraph`，复用其
      全部聚合不变量：`revision_id` / `arrival_seq` 唯一、重复 payload 不成为新 revision、revision
      ID 只能属于一个 `observation_key`（即一个 episode，**跨 episode supersedes 由此拒绝**）、无
      环、声明的边须有证据；
    - 同一 `(venue, instrument_type)` 下可以同时存在稳定 ID episode 与退化键 episode（来源可能只为部
      分产品提供稳定 ID）。契约层无法证明某个稳定键与某个退化键其实是同一产品，因此不做全 scope 禁
      混；这类身份存疑属质量事件，由未来 Collector / 质量检查处理，不猜测合并（ADR-0024「失败与恢复
      语义」）；
    - 集合语义：revision 按 `revision_id`、证据按其规范 JSON 规范排序，完全相同的证据视为重复并拒
      绝；校验结果与内容哈希不依赖输入顺序。

    本模型**不**执行 universe 查询：候选过滤、maximal-head 选择与可交易判断属于批次 F 的执行器。
    """

    revisions: tuple[ListingRevision, ...] = ()
    precedence_evidence: tuple[PrecedenceEvidence, ...] = ()

    @field_validator("revisions")
    @classmethod
    def _canonical_revisions(
        cls, value: tuple[ListingRevision, ...]
    ) -> tuple[ListingRevision, ...]:
        return tuple(sorted(value, key=lambda item: item.revision.revision_id))

    @field_validator("precedence_evidence")
    @classmethod
    def _canonical_evidence(
        cls, value: tuple[PrecedenceEvidence, ...]
    ) -> tuple[PrecedenceEvidence, ...]:
        keyed = {canonical_json(item.model_dump(mode="json")): item for item in value}
        if len(keyed) != len(value):
            raise ValueError("precedence_evidence 不得包含完全相同的重复证据")
        return tuple(keyed[key] for key in sorted(keyed))

    @model_validator(mode="after")
    def _history_invariants(self) -> ListingHistory:
        try:
            RevisionGraph(
                revisions=tuple(item.revision for item in self.revisions),
                precedence_evidence=self.precedence_evidence,
            )
        except ValidationError as exc:
            raise ValueError(f"listing revision 图不合法：{exc}") from exc
        return self


# ======================================================================================
# UniverseSelectionSpec（ADR-0024 §5）
# ======================================================================================


class UniverseCandidateSource(StrEnum):
    """候选 episode 的来源。只有一个取值：按 `(simulation_time, knowledge_cutoff)` 的 PIT listing 历
    史。

    "当前仍存活"、"今天的 symbol 列表"、"最终成交量 / 最终存活"都不是合法来源（ADR-0024 §5、验收
    #11），因此没有对应取值；新增来源须另起 ADR，且必须同样是历史 PIT 语义。
    """

    POINT_IN_TIME_LISTINGS = "point_in_time_listings"


class MetricBasis(StrEnum):
    """过滤指标的求值基准。只有一个取值：`point_in_time`——指标只能由同时满足
    `available_time <= simulation_time` 与 `knowledge_time <= knowledge_cutoff` 的数据求得。

    "final"、"current"、"latest" 之类的基准不存在；新增基准须另起 ADR。
    """

    POINT_IN_TIME = "point_in_time"


class FilterComparator(StrEnum):
    """过滤规则的比较方向：保留指标值 `>= threshold` 或 `<= threshold` 的 episode。"""

    AT_LEAST = "at_least"
    AT_MOST = "at_most"


class UniverseFilter(Contract):
    """一条版本化过滤规则的声明形状（流动性、数据质量等；ADR-0024 §5）。

    指标是一份 FeatureSpec，以 `feature_name + feature_version + feature_hash` 绑定（不用 `Ref`）；
    FeatureSpec 的输入白名单排除 Outcome（ADR-0012），因此"最终成交量"这类前视标签不能成为指标。
    `threshold` 的数值由 spec 的作者在新版本中给出，本契约**不选择、不暗含任何数值**；它只要求有限
    数（`Contract` 全局拒绝 NaN / ±Inf）。

    **诚实边界**：指标的实际解析与计算、FeatureSpec 是否已登记、哈希是否匹配，属批次 F 与 Registry。
    """

    filter_id: str = Field(pattern=NAME_PATTERN)
    feature_name: str = Field(pattern=NAME_PATTERN)
    feature_version: str = Field(pattern=SEMVER_PATTERN)
    feature_hash: ContentHash
    metric_basis: MetricBasis
    comparator: FilterComparator
    threshold: float


class UniverseSpecBinding(Contract):
    """manifest 对 `UniverseSelectionSpec` 的专用绑定：`name + SemVer + spec_hash`（ADR-0024 §5 /
    §6）。

    不是 `Ref`，不携带 `Kind`；与 `PolicyBinding` 形状不同、不可互换。

    **诚实边界**：该版本是否已登记、`spec_hash` 是否等于真实内容，属未来 Registry；持有 spec 本身时
    可用 `binds()` 本地核对。
    """

    name: str = Field(pattern=BINDING_ID_PATTERN)
    version: str = Field(pattern=SEMVER_PATTERN)
    spec_hash: ContentHash

    def binds(self, spec: UniverseSelectionSpec) -> bool:
        """本绑定是否精确指向这份 spec（名称、版本与内容哈希全部相等）。"""
        return (self.name, self.version, self.spec_hash) == (
            spec.name,
            spec.version,
            spec.content_hash(),
        )


class UniverseSelectionSpec(Contract):
    """独立、版本化的 universe 选择规格（ADR-0024 §5）；规则变化 = 新版本，旧版本保留以复现旧实验。

    - `name` + `version`：身份（例如首切片 `binance.spot.btc-eth@1.0.0`，03-data.md §7.3）；manifest
      以 `binding()` 得到的 `name + SemVer + content_hash()` 绑定它；
    - `candidate_source`：只能是 PIT listing 历史；
    - `venue` + `instrument_type` + `symbols`：候选范围——在每个
      `(simulation_time, knowledge_cutoff)` 下，由 PIT 选出的 listing revision 中 venue、类型一致且
      **当时** symbol 属于 `symbols` 的 episode。这是预先声明、随版本冻结的范围，按历史 listing 求
      值，不是"今天的 symbol 列表"；symbols 去重并规范排序、大小写敏感；
    - `filters`：必须显式给出（可为空）；全部规则取合取，`filter_id` 唯一，按 `filter_id` 规范排序。

    没有 `kind`、`Ref` 或 `lineage` 字段：它不是可被 `Ref` 引用的已登记对象（ADR-0024 §5）。
    """

    name: str = Field(pattern=BINDING_ID_PATTERN)
    version: str = Field(pattern=SEMVER_PATTERN)
    candidate_source: UniverseCandidateSource
    venue: NonEmptyStr
    instrument_type: InstrumentType
    symbols: tuple[NonEmptyStr, ...] = Field(min_length=1, json_schema_extra={"uniqueItems": True})
    filters: tuple[UniverseFilter, ...]

    @field_validator("symbols")
    @classmethod
    def _canonical_symbols(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _canonical_unique(value, "symbols")

    @field_validator("filters")
    @classmethod
    def _canonical_filters(cls, value: tuple[UniverseFilter, ...]) -> tuple[UniverseFilter, ...]:
        _canonical_unique(tuple(item.filter_id for item in value), "filters 的 filter_id")
        return tuple(sorted(value, key=lambda item: item.filter_id))

    def binding(self) -> UniverseSpecBinding:
        """manifest 中使用的绑定：`name + SemVer + content_hash()`。

        投影携带 spec 自身的信封版本（ADR-0052 versioned replay，V4）：同一个 spec 在任何当前
        契约版本下都投影出同一个绑定。
        """
        return UniverseSpecBinding(
            schema_version=self.schema_version,
            name=self.name,
            version=self.version,
            spec_hash=self.content_hash(),
        )


# ======================================================================================
# 成员与排除清单（ADR-0024 §4、§6）
# ======================================================================================


def _check_span(start: datetime | None, end: datetime | None) -> None:
    if (start is None) != (end is None):
        raise ValueError("effective_from 与 effective_until 必须同时给出或同时省略")
    if start is not None and end is not None and start >= end:
        raise ValueError("生效区间必须满足 effective_from < effective_until（UTC 半开区间）")


class ExclusionReason(StrEnum):
    """候选 episode 被排除的原因（ADR-0024 §4、§5）。

    - `not_tradable`：`simulation_time` 不落在所选 listing revision 的任何可交易区间内（未上市、暂
      停、已下架）；
    - `filtered`：被 spec 中的某条过滤规则排除（须给出 `filter_id`）。

    competing listing heads **不是**排除原因：它使整次构建 fail closed，不产生 manifest。
    """

    NOT_TRADABLE = "not_tradable"
    FILTERED = "filtered"


class UniverseMember(Contract):
    """最终成员清单中的一项：episode + 决定它入选的 listing revision（PIT 选出的唯一 head）。

    单点 simulation 时不带生效区间；simulation 为区间时必须带 UTC 半开生效区间
    `[effective_from, effective_until)`，且落在 simulation 区间内（由 manifest 校验）。
    """

    # 类文档字符串是已发布 Schema 的 `description`，为保持 Schema 除新字段外逐字节不变，不改写。
    # ADR-0088 决策 6（ADR-0051 §3）：成员区间来自 listing 回填假设时，`assumption` 为所绑定的
    # availability 政策（名称、版本、哈希），`policy_id` 必须是 `LISTING_BACKFILL_ASSUMPTION_ID`；
    # `None` = 观测得到（与 2.4.0 之前相同），从载荷中省略，既有哈希逐位不变。该版本 / 哈希是否等于
    # 已登记政策、区间是否真由该假设推出，属 infrastructure（ADR-0051 第二期）。
    _FIELDS_SINCE = {"assumption": ADR_0088_VERSION}

    episode: ListingEpisodeKey
    listing_revision_id: NonEmptyStr
    effective_from: UtcDatetime | None = None
    effective_until: UtcDatetime | None = None
    assumption: PolicyBinding | None = Field(default=None, exclude_if=omit_none)

    @model_validator(mode="after")
    def _span(self) -> UniverseMember:
        _check_span(self.effective_from, self.effective_until)
        return self

    @model_validator(mode="after")
    def _assumption_policy(self) -> UniverseMember:
        if self.assumption is None:
            return self
        if self.assumption.policy_id != LISTING_BACKFILL_ASSUMPTION_ID:
            raise ValueError(
                f"assumption 必须绑定 {LISTING_BACKFILL_ASSUMPTION_ID}，收到 "
                f"{self.assumption.policy_id}（ADR-0088 决策 6）"
            )
        # 使用处要求确切的 role（见 `PolicyBinding`）：ADR-0051 的假设是 availability 政策。
        if self.assumption.role is not PolicyRole.AVAILABILITY:
            raise ValueError(
                f"assumption 必须是 role={PolicyRole.AVAILABILITY.value} 的绑定，实际为 "
                f"{self.assumption.role.value}（ADR-0088 决策 6）"
            )
        return self


class UniverseExclusion(Contract):
    """排除原因清单中的一项：episode + 所依据的 listing revision + 明确原因。

    `reason = filtered` 时必须给出 `filter_id`，其它原因不得给出。生效区间规则同 `UniverseMember`。
    **诚实边界**：`filter_id` 是否存在于被绑定的 spec 中，需要 spec 本身，属未来执行器 / Registry。
    """

    episode: ListingEpisodeKey
    listing_revision_id: NonEmptyStr
    reason: ExclusionReason
    filter_id: Annotated[str, Field(pattern=NAME_PATTERN)] | None = None
    effective_from: UtcDatetime | None = None
    effective_until: UtcDatetime | None = None

    @model_validator(mode="after")
    def _shape(self) -> UniverseExclusion:
        _check_span(self.effective_from, self.effective_until)
        if (self.reason is ExclusionReason.FILTERED) != (self.filter_id is not None):
            raise ValueError("reason=filtered 时必须给出 filter_id，其它原因不得给出")
        return self


def _entry_sort_key(entry: UniverseMember | UniverseExclusion) -> tuple[str, datetime]:
    return (entry.episode.observation_key(), entry.effective_from or _NO_SPAN)


# ======================================================================================
# ResearchDatasetManifest（ADR-0023 §6、ADR-0024 §6、03-data.md §7.5）
# ======================================================================================


class SelectedRevisionLineage(Contract):
    """一个选中 Canonical revision 的来源链：Canonical revision → Raw row revision → Raw source
    payload revision。

    第三跳是通用的来源 hop：首切片归档路径中 source 即归档文件 revision
    （`raw.binance_spot_archives`）；D3 REST 补尾的 Raw 响应载荷同样用这一跳表达（ADR-0022）。
    契约只表达 hop 的形状，具体 source 类型与其内容由未来 Collector / 表实现证明。

    表名为 Iceberg `namespace.table`：`canonical_table` 必须在 `canonical` namespace，`raw_table` 与
    `source_table` 必须在 `raw` namespace（03-data.md §7.1）。稳定身份为
    `(canonical_table, canonical_revision_id)`。
    """

    canonical_table: SnapshotTable
    canonical_revision_id: NonEmptyStr
    raw_table: SnapshotTable
    raw_revision_id: NonEmptyStr
    source_table: SnapshotTable
    source_revision_id: NonEmptyStr

    @model_validator(mode="after")
    def _namespaces(self) -> SelectedRevisionLineage:
        if _namespace(self.canonical_table) != "canonical":
            raise ValueError("canonical_table 必须位于 canonical namespace")
        for field in ("raw_table", "source_table"):
            if _namespace(getattr(self, field)) != "raw":
                raise ValueError(f"{field} 必须位于 raw namespace")
        return self


class AvailabilityEvidenceGap(Contract):
    """一条 availability 证据缺口记录（ADR-0023 §2）：该 revision 的 `available_time` 已保守取
    `ingest_time`。

    证据缺口不是数据集级失败（roadmap Phase 1 验收 #18），但必须随 manifest 绑定：`table` +
    `revision_id` 指明哪个 revision，`quality_report_id` 指向记录该缺口的质量报告，`gap` 说明缺什么
    证据。稳定身份为 `(table, revision_id)`。
    """

    table: SnapshotTable
    revision_id: NonEmptyStr
    quality_report_id: NonEmptyStr
    gap: NonEmptyStr


class ResearchDatasetManifest(Contract):
    """一份 Research Dataset 的审计清单（ADR-0023 §6、ADR-0024 §6、03-data.md §3 / §7.5）。

    **全部字段必填，清单字段没有默认值**：缺字段即拒绝；成员、排除、lineage 与证据缺口清单可以显式为
    空（某时点可能全部入选、全部排除或没有候选），但不能靠"省略"得到空清单。

    - `dataset`：该 Research Dataset 自身的 `DatasetRef`，`zone` 必须是 `research_dataset`，`table`
      为 `namespace.table` 且不得出现在上游绑定中（自身与上游不混淆）；
    - `point_in_time`：B1 `PointInTimeSpec` 全文——上游 snapshot（`snapshot_bindings`，表名天然去
      重）、simulation 单点或区间、`knowledge_cutoff`、PIT / availability / precedence / parser 绑定
      的**唯一来源**。manifest 不另存任何 cutoff、simulation 或 policy 字段，因此同一 manifest 内不
      可能出现两套互相矛盾的值；其 `name + SemVer + content_hash()` 即 PIT spec 绑定。上游必须包含
      listing 历史表 `canonical.instrument_listings` 与质量报告表 `quality.data_quality_reports`；
    - `universe_spec`：`UniverseSpecBinding`（`name + SemVer + content hash`，不是 `Ref`）；
    - `members` / `exclusions`：稳定身份为 `(episode 键, effective_from)`，规范排序；单点 simulation
      时同一 episode 至多出现一次，区间 simulation 时同一 episode 的全部生效区间（成员与排除合计）互
      不重叠，且都落在 simulation 区间内——同一 episode 在同一时刻不能既是成员又被排除；
    - listing revision 引用：每个 member / exclusion 的 `listing_revision_id` 只能属于一个 episode
      （同一 ID 可在同一 episode 的多个不重叠生效区间中重复出现，但不得被两个 episode 认领，与 B1
      `RevisionGraph` 的 revision ID 归属规则一致），且必须在 `lineage` 中有
      `canonical_table == canonical.instrument_listings` 的来源链——决定成员资格的 listing revision
      不得悬空，其它 Canonical 表的 lineage 不能冒充；
    - `lineage`：选中 revision 的来源链，按 `(canonical_table, canonical_revision_id)` 去重并排序，
      所涉表都必须有上游 snapshot；
    - `quality_report_ids`：引用的质量报告，至少一项，去重并排序；
    - `evidence_gaps`：availability 证据缺口，按 `(table, revision_id)` 去重并排序，`table` 必须有上
      游 snapshot，`quality_report_id` 必须在 `quality_report_ids` 中（可证明的悬空引用被拒绝）。

    **诚实边界**：这是审计契约，**不**证明被绑定的 snapshot / 版本在 Registry 中存在、哈希等于真实内
    容、成员清单确实由 spec + snapshot + 两个截止按 maximal-head 算法重建、逐行 PIT 正确、lineage 是
    完整闭包、`dataset_snapshots` 已包含自身 `DatasetRef`，也不证明 Runner 会强制接收它——这些是 B3 /
    F / Registry / 未来 Runner 的义务。已发布的 `DatasetRef` 与 `ReproducibilityTuple` 字段不变。
    """

    dataset: DatasetRef
    point_in_time: PointInTimeSpec
    universe_spec: UniverseSpecBinding
    members: tuple[UniverseMember, ...]
    exclusions: tuple[UniverseExclusion, ...]
    lineage: tuple[SelectedRevisionLineage, ...]
    quality_report_ids: tuple[NonEmptyStr, ...] = Field(
        min_length=1, json_schema_extra={"uniqueItems": True}
    )
    evidence_gaps: tuple[AvailabilityEvidenceGap, ...]

    @field_validator("members")
    @classmethod
    def _canonical_members(cls, value: tuple[UniverseMember, ...]) -> tuple[UniverseMember, ...]:
        return tuple(sorted(value, key=_entry_sort_key))

    @field_validator("exclusions")
    @classmethod
    def _canonical_exclusions(
        cls, value: tuple[UniverseExclusion, ...]
    ) -> tuple[UniverseExclusion, ...]:
        return tuple(sorted(value, key=_entry_sort_key))

    @field_validator("lineage")
    @classmethod
    def _canonical_lineage(
        cls, value: tuple[SelectedRevisionLineage, ...]
    ) -> tuple[SelectedRevisionLineage, ...]:
        keys = tuple(
            canonical_json([item.canonical_table, item.canonical_revision_id]) for item in value
        )
        _canonical_unique(keys, "lineage 的 (canonical_table, canonical_revision_id)")
        return tuple(
            sorted(value, key=lambda item: (item.canonical_table, item.canonical_revision_id))
        )

    @field_validator("quality_report_ids")
    @classmethod
    def _canonical_reports(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _canonical_unique(value, "quality_report_ids")

    @field_validator("evidence_gaps")
    @classmethod
    def _canonical_gaps(
        cls, value: tuple[AvailabilityEvidenceGap, ...]
    ) -> tuple[AvailabilityEvidenceGap, ...]:
        keys = tuple(canonical_json([item.table, item.revision_id]) for item in value)
        _canonical_unique(keys, "evidence_gaps 的 (table, revision_id)")
        return tuple(sorted(value, key=lambda item: (item.table, item.revision_id)))

    @model_validator(mode="after")
    def _manifest_invariants(self) -> ResearchDatasetManifest:
        upstream = self.point_in_time.snapshot_bindings
        if self.dataset.zone is not Zone.RESEARCH_DATASET:
            raise ValueError("dataset 必须是 zone=research_dataset 的 DatasetRef")
        if _SNAPSHOT_TABLE_RE.fullmatch(self.dataset.table) is None:
            raise ValueError("dataset.table 必须是 Iceberg namespace.table")
        if self.dataset.table in upstream:
            raise ValueError("dataset 自身的表不得同时作为上游 snapshot 绑定")
        for required in (LISTINGS_TABLE, QUALITY_REPORTS_TABLE):
            if required not in upstream:
                raise ValueError(f"上游 snapshot 绑定必须包含 {required}")
        for item in self.lineage:
            for table in (item.canonical_table, item.raw_table, item.source_table):
                if table not in upstream:
                    raise ValueError(f"lineage 引用的表 {table} 没有上游 snapshot 绑定")
        reports = set(self.quality_report_ids)
        for gap in self.evidence_gaps:
            if gap.table not in upstream:
                raise ValueError(f"证据缺口引用的表 {gap.table} 没有上游 snapshot 绑定")
            if gap.quality_report_id not in reports:
                raise ValueError(
                    f"证据缺口引用的质量报告 {gap.quality_report_id!r} 不在 quality_report_ids 中"
                )
        self._check_universe_entries()
        self._check_listing_references()
        return self

    def _check_listing_references(self) -> None:
        claims: dict[str, set[str]] = {}
        entries: tuple[UniverseMember | UniverseExclusion, ...] = (*self.members, *self.exclusions)
        for entry in entries:
            claims.setdefault(entry.listing_revision_id, set()).add(entry.episode.observation_key())
        # 先收集全部 claim、再按 ID 排序报告，报错与输入顺序无关。
        conflicts = sorted(rid for rid, keys in claims.items() if len(keys) > 1)
        if conflicts:
            raise ValueError(f"listing_revision_id {conflicts[0]!r} 被多个 episode 认领")
        listed = {
            item.canonical_revision_id
            for item in self.lineage
            if item.canonical_table == LISTINGS_TABLE
        }
        dangling = sorted(set(claims) - listed)
        if dangling:
            raise ValueError(
                f"listing_revision_id {dangling[0]!r} 在 lineage 中没有 {LISTINGS_TABLE} 来源链"
            )

    def _check_universe_entries(self) -> None:
        pit = self.point_in_time
        point = pit.simulation_time is not None
        window_start = pit.simulation_start or _NO_SPAN
        window_end = pit.simulation_end or _NO_SPAN
        spans: dict[str, list[tuple[datetime, datetime, str]]] = {}
        for label, entries in (("members", self.members), ("exclusions", self.exclusions)):
            for entry in entries:
                start, end = entry.effective_from, entry.effective_until
                if point:
                    if start is not None:
                        raise ValueError(f"单点 simulation 的 {label} 条目不得带生效区间")
                    start = end = _NO_SPAN
                else:
                    if start is None or end is None:
                        raise ValueError(f"区间 simulation 的 {label} 条目必须给出生效区间")
                    if start < window_start or end > window_end:
                        raise ValueError(f"{label} 条目的生效区间必须落在 simulation 区间内")
                spans.setdefault(entry.episode.observation_key(), []).append((start, end, label))
        for key, items in spans.items():
            items.sort(key=lambda item: item[0])
            for (_, prev_end, prev_label), (next_start, _, next_label) in zip(
                items, items[1:], strict=False
            ):
                if point or prev_end > next_start:
                    if prev_label != next_label:
                        raise ValueError(f"episode {key} 在同一时刻既是成员又被排除")
                    raise ValueError(f"episode {key} 在 {prev_label} 中重复或生效区间重叠")


# ======================================================================================
# 有界 Research Dataset evidence manifest（ADR-0077，自契约 2.3.0，additive）
# ======================================================================================

#: 本节全部模型的引入版本（ADR-0077 DQ-1 = A）；2.0.0 ~ 2.2.0 信封中出现即拒绝（ADR-0052 §4）。
ADR_0077_VERSION: Final = "2.3.0"
#: evidence stream 的对象格式（ADR-0077 §2 / §3）。
DATASET_EVIDENCE_FORMAT: Final = "hlens.dataset.evidence-jsonl@1.0.0"
#: evidence 对象键只由内容 SHA-256 决定（ADR-0077 §3.4，DQ-6 = a）。
DATASET_EVIDENCE_KEY_PREFIX: Final = "research/dataset-evidence/v1/"
DATASET_EVIDENCE_KEY_PATTERN = r"^research/dataset-evidence/v1/[0-9a-f]{64}\.jsonl$"
#: `selection_id` 必须能作为 chunk batch id 的前缀（ADR-0077 §4.2）：字符集同
#: `BATCH_ID_PATTERN`，长度留出 `.chunk-` + 十位序号（17 个字符），使 batch id 不超过 256 个字符。
DATASET_SELECTION_ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._:=@+-]{0,238}$"
#: chunk 序号十位零填充（ADR-0077 §4.2）：可表示的最大序号。
DATASET_CHUNK_INDEX_MAX: Final = 9_999_999_999

_SELECTION_ID_RE = re.compile(DATASET_SELECTION_ID_PATTERN)
_SHA256_HEX_RE = re.compile(r"^[0-9a-f]{64}$")
_CHUNK_BATCH_ID_RE = re.compile(r"^(?P<selection_id>.+)\.chunk-(?P<chunk_index>[0-9]{10})$")


def dataset_evidence_key(sha256: str) -> str:
    """evidence 对象的规范键 `research/dataset-evidence/v1/<sha256>.jsonl`（DQ-6 = a）。"""
    if not isinstance(sha256, str) or _SHA256_HEX_RE.fullmatch(sha256) is None:
        raise ValueError(f"evidence 对象的 sha256 必须是 64 位小写十六进制：{sha256!r}")
    return f"{DATASET_EVIDENCE_KEY_PREFIX}{sha256}.jsonl"


def dataset_chunk_batch_id(selection_id: str, chunk_index: int) -> str:
    """第 `chunk_index` 个 chunk 的 batch id（ADR-0077 §4.2）。

    形如 `<selection_id>.chunk-<十位零填充序号>`。
    """
    if not isinstance(selection_id, str) or _SELECTION_ID_RE.fullmatch(selection_id) is None:
        raise ValueError(f"selection_id 不能作为 chunk batch id 的前缀：{selection_id!r}")
    if (
        isinstance(chunk_index, bool)
        or not isinstance(chunk_index, int)
        or not 0 <= chunk_index <= DATASET_CHUNK_INDEX_MAX
    ):
        raise ValueError(f"chunk_index 必须是 0 ~ {DATASET_CHUNK_INDEX_MAX} 的整数")
    return f"{selection_id}.chunk-{chunk_index:010d}"


class DatasetRuleBinding(Contract):
    """v3 manifest 对 dataset 规则的绑定：`rule_id + SemVer + rule_hash`（ADR-0077 §1）。

    规则 spec 写明排序、chunk、leaf 与 fan-out 参数，它们随 `rule_hash` 进入 manifest 身份；
    改值即新规则版本。

    **诚实边界**：规则是否已登记、`rule_hash` 是否等于真实 spec，属 verifier 与未来 Registry。
    """

    _MODEL_SINCE = ADR_0077_VERSION

    rule_id: str = Field(pattern=BINDING_ID_PATTERN)
    version: str = Field(pattern=SEMVER_PATTERN)
    rule_hash: ContentHash


class EvidenceStream(StrEnum):
    """v3 manifest 承诺的六种有序 evidence stream（ADR-0077 §2）。"""

    MEMBERS = "members"
    EXCLUSIONS = "exclusions"
    LINEAGE = "lineage"
    EVIDENCE_GAPS = "evidence_gaps"
    QUALITY_REPORTS = "quality_reports"
    CHUNK_PROOFS = "chunk_proofs"


class EvidenceObjectRef(Contract):
    """一个 evidence 叶 / 索引对象的内容身份：`key + sha256 + size`（ADR-0077 §1.4，DQ-8 = a）。

    不含实现生成的 `uri`：同一内容在不同 warehouse 根下身份相同。`key` 只由 `sha256` 决定
    （`research/dataset-evidence/v1/<sha256>.jsonl`）；对象至少含一行 header，因此 `size >= 1`。

    **诚实边界**：对象是否存在、字节是否哈希成 `sha256`、长度是否为 `size`，属
    `StorageAdapter.lookup` 与 reader 的逐项核对。
    """

    _MODEL_SINCE = ADR_0077_VERSION

    key: str = Field(pattern=DATASET_EVIDENCE_KEY_PATTERN)
    sha256: ContentHash
    size: int = Field(ge=1)

    @model_validator(mode="after")
    def _key_is_the_content_key(self) -> EvidenceObjectRef:
        if self.key != dataset_evidence_key(self.sha256):
            raise ValueError("evidence 对象的 key 必须由其 sha256 派生（DQ-6）")
        return self


class EvidenceStreamRef(Contract):
    """一条 evidence stream 的根承诺（ADR-0077 §1.3、§3）。

    - `record_count`：流内记录数；`leaf_count`：叶对象数；`depth`：根索引所在层（至少 1，
      单叶或空流也由一个根索引包裹）；
    - 空流（`record_count = 0`）没有叶；非空流至少一个叶，且叶数不超过记录数；
      叶数不超过 1 时 `depth = 1`；
    - `root`：根索引对象的内容身份，经 manifest 内容哈希承诺整条流的顺序、计数与内容。

    **诚实边界**：fan-out 是规则参数，不在本对象内，因此 `depth` 与 `leaf_count` 的精确关系、
    各层 header 与子引用的连续性，由 reader / verifier 自根向下核对。
    """

    _MODEL_SINCE = ADR_0077_VERSION

    stream: EvidenceStream
    format: Literal["hlens.dataset.evidence-jsonl@1.0.0"]
    record_count: int = Field(ge=0)
    leaf_count: int = Field(ge=0)
    depth: int = Field(ge=1)
    root: EvidenceObjectRef

    @model_validator(mode="after")
    def _counts(self) -> EvidenceStreamRef:
        if (self.record_count == 0) != (self.leaf_count == 0):
            raise ValueError("空流没有叶对象；非空流至少有一个叶对象")
        if self.leaf_count > self.record_count:
            raise ValueError("叶对象数不得超过记录数（每个叶至少一条记录）")
        if self.leaf_count <= 1 and self.depth != 1:
            raise ValueError("叶对象数不超过 1 时根索引的 depth 必须为 1")
        return self


class DatasetQualitySubject(StrEnum):
    """质量报告描述的对象：listing 历史，或一个 (member symbol, UTC 日) 分区。"""

    LISTING = "listing"
    SYMBOL_DAY = "symbol_day"


class DatasetQualityReportRef(Contract):
    """`quality_reports` 流的一条记录：一份被数据集引用的质量报告及其对象（ADR-0077 §1.6）。

    `subject = listing` 时不得给出 `symbol` / `day`；`subject = symbol_day` 时二者都必须给出。
    流内顺序：listing 在前，再按 `(symbol, day)` 严格递增（`sort_key()`）。
    """

    _MODEL_SINCE = ADR_0077_VERSION

    report_id: NonEmptyStr
    subject: DatasetQualitySubject
    symbol: NonEmptyStr | None = None
    day: date | None = None

    @model_validator(mode="after")
    def _subject_shape(self) -> DatasetQualityReportRef:
        partition = (self.symbol is not None, self.day is not None)
        if self.subject is DatasetQualitySubject.LISTING and partition != (False, False):
            raise ValueError("subject=listing 的报告不得给出 symbol / day")
        if self.subject is DatasetQualitySubject.SYMBOL_DAY and partition != (True, True):
            raise ValueError("subject=symbol_day 的报告必须同时给出 symbol 与 day")
        return self

    def sort_key(self) -> tuple[int, str, str]:
        """流内规范顺序的键：listing 在前，再按 `(symbol, day)`。"""
        if self.symbol is None or self.day is None:
            return (0, "", "")
        return (1, self.symbol, self.day.isoformat())


class DatasetChunkProof(Contract):
    """`chunk_proofs` 流的一条记录：一个定长 chunk 的提交证明（ADR-0077 §4.3）。

    `batch_id` 必须是 `<selection_id>.chunk-<十位零填充 chunk_index>`；`batch_fingerprint` 是该
    chunk 的 Arrow Table（行按 `row_ordinal` 升序）按表的版本化指纹规则算出的内容指纹。

    **诚实边界**：snapshot 是否存在、该 batch 是否在其中提交、指纹与行数是否与读回内容相等、
    `first_row_ordinal` 是否等于 `chunk_index * chunk_rows`，属 verifier。
    """

    _MODEL_SINCE = ADR_0077_VERSION

    chunk_index: int = Field(ge=0, le=DATASET_CHUNK_INDEX_MAX)
    batch_id: str = Field(pattern=BATCH_ID_PATTERN)
    snapshot_id: str = Field(pattern=SNAPSHOT_ID_PATTERN)
    first_row_ordinal: int = Field(ge=0)
    row_count: int = Field(ge=1)
    batch_fingerprint: ContentHash

    @model_validator(mode="after")
    def _batch_id_names_the_chunk(self) -> DatasetChunkProof:
        match = _CHUNK_BATCH_ID_RE.fullmatch(self.batch_id)
        if match is None or _SELECTION_ID_RE.fullmatch(match.group("selection_id")) is None:
            raise ValueError("batch_id 必须是 <selection_id>.chunk-<十位零填充序号>")
        if int(match.group("chunk_index")) != self.chunk_index:
            raise ValueError("batch_id 的 chunk 序号必须等于 chunk_index")
        return self

    @property
    def selection_id(self) -> str:
        """`batch_id` 中的 `selection_id` 前缀。"""
        return self.batch_id.rsplit(".chunk-", 1)[0]


class ResearchDatasetEvidenceManifest(Contract):
    """有界 Research Dataset 的审计清单（v3 形态；ADR-0077 §1，自契约 2.3.0）。

    与 `ResearchDatasetManifest`（v2 形态）并存，不替代、不升级它。全部字段必填且大小固定，
    与选中行数、窗口天数无关：

    - `dataset` / `point_in_time` / `universe_spec`：规则同 v2——`dataset` 为 `research_dataset`
      的 `namespace.table` 且不在上游绑定中，上游必须含 `canonical.instrument_listings` 与
      `quality.data_quality_reports`；`dataset.snapshot_id` 是最后一个 chunk 的 snapshot；
    - `rule`：dataset 规则绑定；`data_type`：显式记录的数据类型；`selection_id`：可作 chunk
      batch id 的前缀；
    - `row_count` / `chunk_rows` / `chunk_count`：空选择被拒绝，
      `chunk_count = ceil(row_count / chunk_rows)`；
    - `evidence`：六种 stream 恰好各一项，按 stream 名规范排序；`chunk_proofs` 的记录数等于
      `chunk_count`，`lineage` 与 `quality_reports` 非空。

    **诚实边界**：契约只证明结构。`selection_id` 与规则 / PIT / universe / `data_type` / 窗口的
    派生关系、规则与 universe 是否已登记、对象是否存在且字节匹配、流内容与 chunk 行是否就是输入的
    派生，属 infrastructure 的 streaming verifier；v2 在模型内完成的跨字段集合检查，在 v3 中由
    verifier 以有序归并完成。
    """

    _MODEL_SINCE = ADR_0077_VERSION

    dataset: DatasetRef
    point_in_time: PointInTimeSpec
    universe_spec: UniverseSpecBinding
    rule: DatasetRuleBinding
    data_type: str = Field(pattern=NAME_PATTERN)
    selection_id: str = Field(pattern=DATASET_SELECTION_ID_PATTERN)
    row_count: int = Field(ge=1)
    chunk_rows: int = Field(ge=1)
    chunk_count: int = Field(ge=1)
    evidence: tuple[EvidenceStreamRef, ...] = Field(min_length=6, max_length=6)

    @field_validator("evidence")
    @classmethod
    def _canonical_evidence(
        cls, value: tuple[EvidenceStreamRef, ...]
    ) -> tuple[EvidenceStreamRef, ...]:
        streams = tuple(item.stream.value for item in value)
        _canonical_unique(streams, "evidence 的 stream")
        missing = sorted({stream.value for stream in EvidenceStream} - set(streams))
        if missing:
            raise ValueError(f"evidence 缺少 stream：{missing}")
        return tuple(sorted(value, key=lambda item: item.stream.value))

    @model_validator(mode="after")
    def _manifest_invariants(self) -> ResearchDatasetEvidenceManifest:
        upstream = self.point_in_time.snapshot_bindings
        if self.dataset.zone is not Zone.RESEARCH_DATASET:
            raise ValueError("dataset 必须是 zone=research_dataset 的 DatasetRef")
        if _SNAPSHOT_TABLE_RE.fullmatch(self.dataset.table) is None:
            raise ValueError("dataset.table 必须是 Iceberg namespace.table")
        if self.dataset.table in upstream:
            raise ValueError("dataset 自身的表不得同时作为上游 snapshot 绑定")
        for required in (LISTINGS_TABLE, QUALITY_REPORTS_TABLE):
            if required not in upstream:
                raise ValueError(f"上游 snapshot 绑定必须包含 {required}")
        if self.chunk_count != -(-self.row_count // self.chunk_rows):
            raise ValueError("chunk_count 必须等于 ceil(row_count / chunk_rows)")
        if self.evidence_for(EvidenceStream.CHUNK_PROOFS).record_count != self.chunk_count:
            raise ValueError("chunk_proofs 流的记录数必须等于 chunk_count")
        for stream in (EvidenceStream.LINEAGE, EvidenceStream.QUALITY_REPORTS):
            if self.evidence_for(stream).record_count == 0:
                raise ValueError(f"{stream.value} 流不得为空")
        return self

    def evidence_for(self, stream: EvidenceStream) -> EvidenceStreamRef:
        """该 stream 的根承诺（每种 stream 恰好一项）。"""
        [ref] = [item for item in self.evidence if item.stream is stream]
        return ref

    def chunk_batch_id(self, chunk_index: int) -> str:
        """本数据集第 `chunk_index` 个 chunk 的 batch id（`0 <= chunk_index < chunk_count`）。"""
        if isinstance(chunk_index, bool) or not 0 <= chunk_index < self.chunk_count:
            raise ValueError(f"chunk_index 超出 0 ~ {self.chunk_count - 1}：{chunk_index!r}")
        return dataset_chunk_batch_id(self.selection_id, chunk_index)

"""双时间与 append-only revision DAG 契约（ADR-0023，D-28；Phase 1 批次 B1）。

对应 docs/architecture/03-data.md §4、§7.3 ~ §7.5。本模块只定义**契约**与可在契约层证明的不变量：
单条记录内的局部约束在各自模型校验，跨记录才能证明的约束只在 `RevisionGraph` 校验。
不含数据库、Iceberg、PIT 查询执行器、Collector 或 Provider，也不实现 maximal-head 选择算法。

两条时间轴（ADR-0023 §1）：

- **历史轴**：`event_time`、`source_time`、`available_time`、`declared_latency`——
  执行 Constitution C-L1，`available_time <= simulation_time`；
- **知识轴**：`ingest_time`、`knowledge_time`——决定一份数据集看到哪一个 vintage，
  `knowledge_time <= knowledge_cutoff`。

**追加顺序 ≠ 修订优先级**（ADR-0023 §4）：`arrival_seq` 只用于审计、幂等与恢复；语义优先级只来自
`supersedes` 边及其持久化的 precedence 证据。本模块不以 `arrival_seq`、墙钟或 payload hash
排序或打破冲突。

**诚实边界**：契约层只校验结构与声明。policy / parser 是否已登记、哈希是否等于真实内容、
证据是否真实并支持结论、`revision_id` 是否按版本化规则从来源身份与 payload 形成、
`arrival_seq` 是否跨重启不复用、maximal heads 是否真的互不排序，都属于未来的 Registry、
存储与 PIT 执行器（批次 C ~ F）。
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import timedelta
from enum import StrEnum
from typing import Annotated

from pydantic import Field, field_validator, model_validator

from core.domain.base import (
    SEMVER_PATTERN,
    ContentHash,
    Contract,
    FrozenMapping,
    UtcDatetime,
)

__all__ = [
    "BINDING_ID_PATTERN",
    "SNAPSHOT_TABLE_PATTERN",
    "AvailabilityDecision",
    "ObservationTimes",
    "PointInTimeSelection",
    "PointInTimeSpec",
    "PointInTimeStatus",
    "PolicyBinding",
    "PolicyRole",
    "PrecedenceEvidence",
    "RevisionGraph",
    "RevisionRecord",
]

#: policy / parser / PIT spec 的标识符：小写、点分、段内允许 `_` 与 `-`
#: （03-data.md §7.3，例如 `binance.spot.publication`、`binance.spot.archive-revision`）。
BINDING_ID_PATTERN = r"^[a-z][a-z0-9_-]*(?:\.[a-z][a-z0-9_-]*)*$"
#: snapshot 绑定的键：Iceberg `namespace.table`（03-data.md §7.1，例如 `canonical.trades`）。
SNAPSHOT_TABLE_PATTERN = r"^[a-z][a-z0-9_]*\.[a-z][a-z0-9_]*$"

#: 非空标识 / 证据项。具体格式由实施批次按数据类型与来源定义并版本化（ADR-0023「开放义务」）。
NonEmptyStr = Annotated[str, Field(min_length=1)]
SnapshotTable = Annotated[str, Field(pattern=SNAPSHOT_TABLE_PATTERN)]


def _canonical_unique_ids(values: tuple[str, ...], label: str) -> tuple[str, ...]:
    """集合语义的 ID 序列：重复即拒绝；按字符串规范排序，使同一集合只有一个内容哈希。

    排序只是规范化表示，**不是**优先级：这些序列内部不存在先后含义。
    """
    if len(set(values)) != len(values):
        raise ValueError(f"{label} 不得包含重复的 revision_id")
    return tuple(sorted(values))


class PolicyRole(StrEnum):
    """一条绑定所约束的规则类别；同一形状的绑定按类别分别使用、不可互换。"""

    AVAILABILITY = "availability"
    PRECEDENCE = "precedence"
    POINT_IN_TIME = "point_in_time"
    PARSER = "parser"


class PolicyBinding(Contract):
    """对一份版本化规则的绑定：`policy_id` + SemVer + 内容哈希（03-data.md §7.3）。

    `role` 区分 availability policy、precedence policy、PIT 选择规则与 parser 版本；使用处按字段
    要求确切的 `role`，因此 availability 与 precedence 绑定不能互相冒充。

    **诚实边界**：只约束形状。该版本是否已登记、`policy_hash` 是否等于该版本的真实内容、
    policy 是否带有已审阅的来源证据，属未来 Registry（03-data.md §7.3「标识符冻结 ≠ 数据可信」）。
    """

    role: PolicyRole
    policy_id: str = Field(pattern=BINDING_ID_PATTERN)
    version: str = Field(pattern=SEMVER_PATTERN)
    policy_hash: ContentHash


def _require_role(binding: PolicyBinding, role: PolicyRole, label: str) -> None:
    if binding.role is not role:
        raise ValueError(f"{label} 必须是 role={role.value} 的绑定，实际为 {binding.role.value}")


class ObservationTimes(Contract):
    """一个 revision 的两轴时间（ADR-0023 §1），全部 UTC，naive 时间拒绝。

    六个 ADR 字段：`event_time`、`source_time`、`available_time`、`ingest_time`、
    `knowledge_time`、`declared_latency`。区间型数据（例如 1m bar）按 ADR §1「同时记录区间起止」
    另给 `event_end_time`，即半开区间 `[event_time, event_end_time)` 的结束端；瞬时事件不填。

    局部不变量：

    - `declared_latency >= 0`（只作用于历史轴的保守计算，不代替任何知识轴时间）；
    - `knowledge_time >= ingest_time`；
    - `available_time` 不早于观察本身可被观察的时刻（瞬时事件取 `event_time`，
      区间取 `event_end_time`），也不早于已给出的 `source_time`；
    - `source_time` 没有就为空，不得伪造。

    `available_time` **可以早于** `ingest_time`（历史 backfill），但那需要证据——
    见 `AvailabilityDecision`。
    `declared_latency` 如何进入 `available_time` 由 availability policy 计算，本模型不重算。
    """

    event_time: UtcDatetime
    event_end_time: UtcDatetime | None = None
    source_time: UtcDatetime | None = None
    available_time: UtcDatetime
    ingest_time: UtcDatetime
    knowledge_time: UtcDatetime
    declared_latency: timedelta

    @property
    def observable_time(self) -> UtcDatetime:
        """观察本身可被观察的最早时刻：区间取结束端，瞬时事件取 `event_time`。"""
        return self.event_end_time if self.event_end_time is not None else self.event_time

    @model_validator(mode="after")
    def _time_invariants(self) -> ObservationTimes:
        if self.declared_latency < timedelta(0):
            raise ValueError("declared_latency 不得为负（会构成未来函数）")
        if self.event_end_time is not None and self.event_end_time <= self.event_time:
            raise ValueError("event_end_time 必须晚于 event_time（半开区间不得为空）")
        if self.knowledge_time < self.ingest_time:
            raise ValueError("knowledge_time 不得早于 ingest_time")
        if self.available_time < self.observable_time:
            raise ValueError("available_time 不得早于观察本身可被观察的时刻（区间取结束端）")
        if self.source_time is not None and self.available_time < self.source_time:
            raise ValueError("available_time 不得早于数据源声明的 source_time")
        return self


class AvailabilityDecision(Contract):
    """一次 availability 判定：时间 + availability policy + 证据**或**证据缺口（ADR-0023 §2）。

    - `evidence` 与 `evidence_gap` **恰好给出一项**：每个判定要么有来源证据，要么显式记录缺口；
    - `available_time < ingest_time`（早于本机得知）**必须**有证据，不得仅凭 `event_time` 回填；
    - 记录证据缺口时，`available_time` 必须**等于** `ingest_time`（保守回退），不得取更早的时间。

    证据缺口不是数据集级失败：该 revision 以 `ingest_time` 为历史可用时间继续参与 PIT，
    缺口随 manifest 绑定（roadmap Phase 1 验收 #18）。

    **诚实边界**：证据项只是非空引用；证据是否存在、是否真的支持该时间、policy 是否真的据此算出
    `available_time`，属未来 Registry 与 policy 审阅（roadmap Phase 1 验收 #17）。
    """

    times: ObservationTimes
    policy: PolicyBinding
    evidence: tuple[NonEmptyStr, ...] = ()
    evidence_gap: NonEmptyStr | None = None

    @model_validator(mode="after")
    def _evidence_rules(self) -> AvailabilityDecision:
        _require_role(self.policy, PolicyRole.AVAILABILITY, "AvailabilityDecision.policy")
        has_evidence = bool(self.evidence)
        has_gap = self.evidence_gap is not None
        if has_evidence == has_gap:
            raise ValueError("evidence 与 evidence_gap 必须恰好给出一项")
        if has_gap and self.times.available_time != self.times.ingest_time:
            raise ValueError("记录证据缺口时 available_time 必须等于 ingest_time（保守回退）")
        if self.times.available_time < self.times.ingest_time and not has_evidence:
            raise ValueError("available_time 早于 ingest_time 时必须有来源证据")
        return self


class RevisionRecord(Contract):
    """一个不可变 revision（ADR-0023 §4）：只追加，永不覆盖。

    - `observation_key`：同一业务观察的稳定键；`revision_id`：稳定修订身份；`source_id`：来源身份；
      `payload_hash`：该 payload 的 SHA-256。格式由实施批次按数据类型定义并版本化。
    - `arrival_seq`：本机追加顺序，**只**用于审计、幂等与恢复；**不是**语义优先级，
      也不得用于打破冲突。
    - `supersedes`：本 revision 取代的 revision 集合（去重、禁止自指；按 ID 规范排序，不表示先后）。
      允许指向尚未 ingest 的 predecessor（dangling）。
    - `source_revision_id` / `source_revision_time`：来源提供时记录，供 precedence policy 证明先后；
      来源给出修订时间时，`available_time` 不得早于它（ADR-0023 §2）。

    跨记录约束（ID / 序号唯一、边不跨 key、无环、边有证据）只能在 `RevisionGraph` 证明；
    `revision_id` 是否按规则形成、`arrival_seq` 是否跨重启不复用，属未来存储层。
    """

    observation_key: NonEmptyStr
    revision_id: NonEmptyStr
    source_id: NonEmptyStr
    payload_hash: ContentHash
    arrival_seq: int = Field(ge=0, strict=True)
    supersedes: tuple[NonEmptyStr, ...] = Field(default=(), json_schema_extra={"uniqueItems": True})
    source_revision_id: NonEmptyStr | None = None
    source_revision_time: UtcDatetime | None = None
    availability: AvailabilityDecision

    @field_validator("supersedes")
    @classmethod
    def _canonical_supersedes(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _canonical_unique_ids(value, "supersedes")

    @model_validator(mode="after")
    def _local_invariants(self) -> RevisionRecord:
        if self.revision_id in self.supersedes:
            raise ValueError("supersedes 不得包含本 revision 自身")
        if (
            self.source_revision_time is not None
            and self.availability.times.available_time < self.source_revision_time
        ):
            raise ValueError("available_time 不得早于来源给出的 source_revision_time")
        return self


class PrecedenceEvidence(Contract):
    """一条持久化的 supersedes 边：`revision_id` 取代 `superseded_revision_id`（ADR-0023 §4）。

    由 precedence policy 在 ingest 时产生，或事后追加以解决 competing heads；它只对
    `knowledge_cutoff >= knowledge_time` 的查询生效（ADR-0023 §5 第 4 步）。
    新旧 revision 不得相同；证据至少一项。

    **诚实边界**：证据是否真实、policy 是否真的证明了先后，属未来 Registry 与 policy 审阅。
    """

    observation_key: NonEmptyStr
    revision_id: NonEmptyStr
    superseded_revision_id: NonEmptyStr
    policy: PolicyBinding
    evidence: tuple[NonEmptyStr, ...] = Field(min_length=1)
    knowledge_time: UtcDatetime

    @model_validator(mode="after")
    def _edge_invariants(self) -> PrecedenceEvidence:
        _require_role(self.policy, PolicyRole.PRECEDENCE, "PrecedenceEvidence.policy")
        if self.revision_id == self.superseded_revision_id:
            raise ValueError("precedence 边的新旧 revision 不得相同")
        return self


def _cyclic_nodes(edges: Iterable[tuple[str, str]]) -> list[str]:
    """有向图中位于环上（或只能经由环到达）的节点；无环返回空列表。

    迭代式拓扑剥离（Kahn），不递归，长链不会触及递归上限；结果按 ID 排序，只用于报错。
    """
    successors: dict[str, set[str]] = {}
    indegree: dict[str, int] = {}
    for newer, older in edges:
        successors.setdefault(newer, set())
        successors.setdefault(older, set())
        if older not in successors[newer]:
            successors[newer].add(older)
            indegree[older] = indegree.get(older, 0) + 1
        indegree.setdefault(newer, 0)
    ready = [node for node, count in indegree.items() if count == 0]
    while ready:
        node = ready.pop()
        for nxt in successors[node]:
            indegree[nxt] -= 1
            if indegree[nxt] == 0:
                ready.append(nxt)
        del indegree[node]
    return sorted(indegree)


class RevisionGraph(Contract):
    """一组 revision 与 precedence 证据的聚合校验（ADR-0023 §4）。

    边集合 = 各记录声明的 `supersedes` 边 ∪ 全部 `PrecedenceEvidence` 边。聚合不变量：

    - `revision_id` 唯一；`arrival_seq` 唯一（只是审计序号的唯一性，不参与任何排序）；
    - 同一 `observation_key` 下 `(source_id, payload_hash)` 唯一：重复 payload 不产生新 revision；
    - `revision_id` 在图内是全局身份，只能属于一个 `observation_key`。统一 claim：记录的
      `revision_id` 与其全部 `supersedes`（含 dangling）认领该记录的键；每条证据的两端
      （含 dangling）认领该证据的键。任一 ID 被不同键认领即拒绝——允许 dangling
      不等于允许其归属自相矛盾；
    - 整个边集合无环（自指已在单条记录 / 证据上拒绝）；
    - 允许 dangling predecessor：被取代的 revision 可以尚未出现在本图中；
    - 记录声明的每条 `supersedes` 边都必须有匹配的 `PrecedenceEvidence`（同键、同两端），
      且该证据的 `knowledge_time` 不晚于该记录的 `knowledge_time`——否则在两者之间的 cutoff 下，
      这条可用于选择的边没有证据（03-data.md §7.5 fail closed）。只来自证据的边是事后追加的
      precedence 记录，天然合法。

    校验结果只取决于集合内容，与元组顺序和 `arrival_seq` 的取值无关。本模型**不**执行 PIT 选择：
    候选过滤、传递淘汰与 maximal head 判定属于批次 F 的执行器，其结果形状见 `PointInTimeSelection`。
    """

    revisions: tuple[RevisionRecord, ...] = ()
    precedence_evidence: tuple[PrecedenceEvidence, ...] = ()

    @model_validator(mode="after")
    def _graph_invariants(self) -> RevisionGraph:
        seen_ids: set[str] = set()
        seqs: set[int] = set()
        payloads: set[tuple[str, str, str]] = set()
        for record in self.revisions:
            if record.revision_id in seen_ids:
                raise ValueError(f"revision_id 重复：{record.revision_id!r}")
            seen_ids.add(record.revision_id)
            if record.arrival_seq in seqs:
                raise ValueError(f"arrival_seq 重复：{record.arrival_seq}")
            seqs.add(record.arrival_seq)
            payload = (record.observation_key, record.source_id, record.payload_hash)
            if payload in payloads:
                raise ValueError(
                    f"重复 payload（同一 observation_key 下 source_id + payload_hash 相同）："
                    f"{record.revision_id!r} 不得成为新 revision"
                )
            payloads.add(payload)

        claims: dict[str, set[str]] = {}
        for record in self.revisions:
            for revision_id in (record.revision_id, *record.supersedes):
                claims.setdefault(revision_id, set()).add(record.observation_key)
        for item in self.precedence_evidence:
            for revision_id in (item.revision_id, item.superseded_revision_id):
                claims.setdefault(revision_id, set()).add(item.observation_key)
        # 先收集全部 claim、再按 ID 排序报告，报错与输入顺序无关。
        conflicts = sorted(rid for rid, keys in claims.items() if len(keys) > 1)
        if conflicts:
            first = conflicts[0]
            raise ValueError(
                f"revision_id {first!r} 跨 observation_key 归属冲突：同时被 "
                f"{sorted(claims[first])} 认领"
            )

        edges: set[tuple[str, str]] = {
            (item.revision_id, item.superseded_revision_id) for item in self.precedence_evidence
        }
        for record in self.revisions:
            for older in record.supersedes:
                if not any(
                    item.observation_key == record.observation_key
                    and item.revision_id == record.revision_id
                    and item.superseded_revision_id == older
                    and item.knowledge_time <= record.availability.times.knowledge_time
                    for item in self.precedence_evidence
                ):
                    raise ValueError(
                        f"supersedes 边 {record.revision_id!r} → {older!r} 缺少不晚于该 revision "
                        "knowledge_time 的 precedence 证据"
                    )
                edges.add((record.revision_id, older))

        cyclic = _cyclic_nodes(edges)
        if cyclic:
            raise ValueError(f"supersedes 图不得成环：涉及 {cyclic}")
        return self


class PointInTimeSpec(Contract):
    """一次 PIT 查询的全部输入（ADR-0023 §5，03-data.md §7.5）。

    - `name` + `version`：本 spec 自身的身份；manifest 以 `name + SemVer + content_hash()`
      绑定它（批次 B2）；
    - simulation：单点 `simulation_time`，**或** UTC 半开区间
      `[simulation_start, simulation_end)`，二选一，区间两端必须同时给出且
      `simulation_start < simulation_end`；
    - `knowledge_cutoff`：本机知识截止，与 simulation 独立（历史 backfill 时通常晚于 simulation）；
    - `snapshot_bindings`：读取的每张 Iceberg 表 `namespace.table → snapshot_id`，至少一项；
    - PIT 选择规则、availability policy、precedence policy、parser 版本的绑定，各自 `role` 确切、
      同一字段内 `policy_id` 不得重复（同一 policy 两个版本即不确定），按 `policy_id` 规范排序。

    universe 绑定（ADR-0024）与 manifest 属批次 B2，不在本模型内。**诚实边界**：绑定能否解析到
    已登记且带证据的版本、snapshot 是否存在、所需表是否齐全，属未来执行器与 Registry
    （03-data.md §7.5 fail closed）。
    """

    name: str = Field(pattern=BINDING_ID_PATTERN)
    version: str = Field(pattern=SEMVER_PATTERN)
    simulation_time: UtcDatetime | None = None
    simulation_start: UtcDatetime | None = None
    simulation_end: UtcDatetime | None = None
    knowledge_cutoff: UtcDatetime
    #: 非空由 `_shape` 校验；Schema 用对象关键字 `minProperties` 表达同一约束。
    snapshot_bindings: FrozenMapping[SnapshotTable, NonEmptyStr] = Field(
        json_schema_extra={"minProperties": 1}
    )
    point_in_time_binding: PolicyBinding
    availability_bindings: tuple[PolicyBinding, ...] = Field(min_length=1)
    precedence_bindings: tuple[PolicyBinding, ...] = Field(min_length=1)
    parser_bindings: tuple[PolicyBinding, ...] = Field(min_length=1)

    @field_validator("availability_bindings", "precedence_bindings", "parser_bindings")
    @classmethod
    def _canonical_bindings(cls, value: tuple[PolicyBinding, ...]) -> tuple[PolicyBinding, ...]:
        ids = [binding.policy_id for binding in value]
        if len(set(ids)) != len(ids):
            raise ValueError("同一绑定字段内 policy_id 不得重复")
        return tuple(sorted(value, key=lambda binding: binding.policy_id))

    @model_validator(mode="after")
    def _shape(self) -> PointInTimeSpec:
        if not self.snapshot_bindings:
            raise ValueError("snapshot_bindings 至少绑定一张表的 snapshot")
        point = self.simulation_time is not None
        start = self.simulation_start is not None
        end = self.simulation_end is not None
        if start != end:
            raise ValueError("simulation 区间必须同时给出 simulation_start 与 simulation_end")
        if point == start:
            raise ValueError("simulation_time 与 simulation 区间必须恰好给出一种")
        if (
            self.simulation_start is not None
            and self.simulation_end is not None
            and self.simulation_start >= self.simulation_end
        ):
            raise ValueError("simulation 半开区间必须满足 simulation_start < simulation_end")
        _require_role(self.point_in_time_binding, PolicyRole.POINT_IN_TIME, "point_in_time_binding")
        for field, role in (
            ("availability_bindings", PolicyRole.AVAILABILITY),
            ("precedence_bindings", PolicyRole.PRECEDENCE),
            ("parser_bindings", PolicyRole.PARSER),
        ):
            for binding in getattr(self, field):
                _require_role(binding, role, field)
        return self


class PointInTimeStatus(StrEnum):
    """单个 `observation_key` 在一个 PIT 时刻的结果（ADR-0023 §5 第 4 步）。"""

    SELECTED = "selected"
    ABSENT = "absent"
    CONFLICT = "conflict"


class PointInTimeSelection(Contract):
    """单个 `observation_key` 在 `(simulation_time, knowledge_cutoff)` 下的 PIT 结果形状。

    - `selected`：恰好一个 maximal head，`selected_revision_id` 即它，`maximal_heads == (它,)`；
    - `absent`：没有候选，无 selected、无 head；
    - `conflict`：至少两个 maximal heads，**不得**携带 selected revision；数据集构建必须
      fail closed，禁止以 `arrival_seq`、墙钟或 payload hash 打破。

    `maximal_heads` 是集合语义（去重、按 ID 规范排序，不表示先后）。这是**输出契约，不是选择算法**：
    heads 是否真的是候选集中互不排序的 maximal 元素，属于批次 F 的执行器，契约层无法证明。
    """

    observation_key: NonEmptyStr
    simulation_time: UtcDatetime
    knowledge_cutoff: UtcDatetime
    status: PointInTimeStatus
    selected_revision_id: NonEmptyStr | None = None
    maximal_heads: tuple[NonEmptyStr, ...] = Field(
        default=(), json_schema_extra={"uniqueItems": True}
    )

    @field_validator("maximal_heads")
    @classmethod
    def _canonical_heads(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _canonical_unique_ids(value, "maximal_heads")

    @model_validator(mode="after")
    def _status_shape(self) -> PointInTimeSelection:
        heads = self.maximal_heads
        selected = self.selected_revision_id
        if self.status is PointInTimeStatus.SELECTED:
            if selected is None or heads != (selected,):
                raise ValueError("selected 必须恰好有一个 revision，且它是唯一的 maximal head")
        elif self.status is PointInTimeStatus.ABSENT:
            if selected is not None or heads:
                raise ValueError("absent 不得携带 selected revision 或 maximal heads")
        else:
            if selected is not None:
                raise ValueError("conflict 不得携带 selected revision（fail closed）")
            if len(heads) < 2:
                raise ValueError("conflict 至少需要两个互不排序的 maximal heads")
        return self

# ADR-0023: 双时间与修订数据的 point-in-time 语义（D-28）

| 字段 | 值 |
|---|---|
| 状态 | **Proposed**（2026-09-24，批次 A1；待 Codex 复核后决定） |
| 日期 | 2026-09-24 |
| 决策者 | Codex（依据 Raphael 2026-09-24"授权所有"的持续授权） |
| 起草者 | Claude Code（Opus）按 Codex 裁决落文 |
| 相关 Phase | Phase 1（首次使用）；所有后续 Phase 的数据消费 |
| 影响范围 | Data / Contract / 复现机制 |
| 是否破坏兼容 | 冻结文档 `03-data.md` §4 的 `available_time` 定义**被本 ADR 提出修改**；现有契约字段不变（见「契约影响」） |
| 前置 | [ADR-0009](0009-experiment-identity-binding.md)、[ADR-0012](0012-information-flow-and-kind-invariants.md)（明确把修订语义留给 Phase 1）、[ADR-0021](0021-phase1-local-data-infrastructure.md) |
| 被依赖 | [ADR-0024](0024-historical-tradable-universe.md) |

## 背景

`docs/architecture/03-data.md` §4（冻结）定义：`available_time` = `event_time + declared_latency`，
并规定在时刻 `t` 只能使用 `available_time ≤ t` 的数据（Constitution C-L1）。

这个单一公式无法表达现实中的三件事：

1. **迟到**：一条很早发生的事件，可能很晚才被系统收到；按公式它"早就可用"，回测会用到当时并不知道的数据。
2. **修订**：同一业务观察可能被数据源更正（例如官方归档被替换，ADR-0022）；按公式无法区分"当时的值"与"后来的值"。
3. **重复与并发**：同一 payload 可能被重复下载或并发写入，需要区分"重复"与"新修订"。

ADR-0012 明确没有发明修订语义（"不发明 Phase 1 的迟到 / 修订数据语义"），留给 Phase 1 决定；C1 复审把它登记为 D-28。
Constitution C-L1 的原则（只用 `available_time ≤ t`）**不变**；本 ADR 改变的是 `available_time` 在架构层的**定义与计算**。

PyIceberg 的写入限制（streaming `RecordBatchReader` 对 partitioned 表的支持、commit 失败后 reader 已耗尽）依据
Codex 控制稿引用的 PyIceberg 官方文档（table 与 api 页面）。**本 ADR 起草时未联网复核**；实施批次须在锁定版本上重新核验。

## 裁决

### 1. 时间轴（全部为 UTC）

| 时间 | 精确定义 | 规则 |
|---|---|---|
| `event_time` | 记录描述的市场事件或区间**实际发生**的时间（valid time） | 来自数据内容 |
| `source_time` | 数据源**声明**的发布时间 / 更新时间 | 可为空；没有就为空，**不得伪造** |
| `ingest_time` | 本系统**第一次**接收到该具体 payload 的时间 | 系统记录 |
| `available_time` | 该具体 observation / revision 在本系统中通过最小解析、可供某一层消费者使用的时间（system / knowledge time） | 必须 `>= ingest_time`；**不得**根据 `event_time` 回填 |
| `declared_latency` | 派生规格声明的额外保守延迟 | 非负；**只**用于派生值，不能代替真实的 ingest / available |

Raw 保留 source payload、source metadata、`ingest_time` 与内容哈希；**不得**根据 `event_time` 回填虚假的 `available_time`。

### 2. 派生值的可用时间

派生值（Canonical 之上的 Feature / State / Event / Representation 等）的可用时间：

    derived.available_time = max(actual_ready_time, max(input.available_time) + declared_latency)

即：全部输入 revision 中最晚的 `available_time` 加上该规格声明的非负延迟；如果实际完成时间更晚，取实际完成时间。
`available_time = event_time + declared_latency` 这一**单一公式被废止**。

### 3. 修订（append-only）

- 以稳定的 `observation_key` 标识同一业务观察（例如 trade 用 venue + 产品 + 成交 ID；1m bar 用 venue + 产品 + 周期 + 开始时间；
  具体键由实施批次按数据类型定义，并随表 Schema 版本化）。
- 每个新 payload 作为**不可变 revision** 追加，记录：`revision_seq`、`supersedes`（若有）、payload hash、`ingest_time`、`available_time`。
- **Raw 不覆盖；Canonical 也保留 append-only revision / changelog**。可以提供 latest view，但 view 不是审计真相。
- `revision_seq`：按同一 `observation_key` 的**实际到达顺序**分配并持久化；并发写同一 key 必须由 Iceberg commit
  与唯一性控制串行化，或冲突后重试；**不得**只靠墙钟排序。
- **重复**：`source ID + payload hash` 相同即为重复，幂等去重，不产生新 revision。**新修订**：同一 `observation_key`、
  不同 payload hash。两者必须可区分。
- **官方归档替换**：同一路径、不同 checksum = 新 source revision（ADR-0022 §4）；旧归档与其解析结果不得物理覆盖。

### 4. Point-in-time as-of 查询算法

在模拟时刻 `t`：

1. 只考虑 `available_time <= t` 的 revision；
2. 对每个 `observation_key`，选择其中 `revision_seq` 最大的一个；
3. 在某条修订的 `available_time` 之前发起的查询，结果**永远不会**因为这条修订而改变；
4. 迟到的记录在它实际的 `available_time` 之前**不可见**，即使它的 `event_time` 很早。

"相同 `event_time` 的多条事件"是不同的 `observation_key`，全部保留，不互相覆盖。

### 5. Research Dataset 的绑定

每份 Research Dataset 必须同时绑定：

- 它自身的 Iceberg `snapshot_id`；
- PIT query / spec 的版本（含选择算法版本）；
- 模拟知识截止规则（knowledge cutoff）；
- 全部上游表的 `snapshot_id`。

**只有 snapshot ID 不足以**证明逐行 PIT 正确（03-data.md §3 已要求绑定上游 snapshot，本 ADR 增加 PIT spec 与 cutoff）。

### 6. PyIceberg 写入约束（Phase 1）

- partitioned 表用**有界的 `pyarrow.Table` microbatch** 写入；每个 batch 有稳定的 batch id；失败重试时必须能从已落盘的
  Raw / staging **重新构造同一批**。
- **不得**把不可重放的 `RecordBatchReader` 直接交给 partitioned append：依据当前 PyIceberg 文档，streaming append
  尚不支持 partitioned 表，且 catalog commit 失败后 reader 已被耗尽，朴素重试可能写入零行。
  可选的两条路是"重建 reader"或"先写可重放的 Parquet 再 `add_files`"；**本阶段选择有界 microbatch + 可重建输入**。
- 初期每个逻辑表 / 分区只允许**一个 writer**；commit conflict 按 batch id **幂等**重试。
- 未被任何 snapshot 引用的 orphan 文件只由**显式 maintenance** 任务清理，不在写入路径中顺手删除。
- microbatch 的具体大小不在本 ADR 选择，由实施批次按内存约束（WSL 约 15 GiB）设定并可配置。

### 7. 对冻结文档的修改（本 ADR 提出，接受后执行）

`03-data.md` §4 是冻结内容。本 ADR **提出**如下修改，**获批前不改冻结正文**：

- 时间表改为本 ADR §1 的五项定义（`event_time`、`source_time`、`ingest_time`、`available_time`、`declared_latency`）；
- 删除"`available_time` = `event_time + declared_latency`"，改为引用本 ADR §2 的派生公式；
- Point-in-time 规则保留"只能使用 `available_time ≤ t`"，并补充 §4 的 as-of 选择算法；
- §3 Research Dataset 的绑定补充 PIT spec 版本与知识截止规则。

起草时全仓检索：该公式只出现在 `03-data.md` §4；`02-domain.md`、`06-experiment.md` 只引用"`available_time > t`"规则，不含该公式。
代码中 `FeatureSpec` 的文档字符串（`core/domain/specs.py:150-151`）以 `event_time + available_lag <= t` 表述可用性，
接受后在实施批次中同步为本 ADR 的语义（`available_lag` 作为该规格的 `declared_latency`）。

## 明确不做

- 本批次不修改 `03-data.md` 的冻结正文，也不改任何契约或代码（包括 `FeatureSpec` 文档字符串）。
- 不改变 Constitution C-L1 ~ C-L6 的任何原则。
- 不选择 microbatch 大小、分区方案或具体的 `observation_key` 字段（由实施批次按数据类型定义）。
- 不引入多 writer 并发写同一分区。
- 不定义 latest view 的物化方式。

## 备选方案

| 方案 | 优点 | 缺点 | 为何拒绝 |
|---|---|---|---|
| **A（本 ADR）** 双时间 + append-only revision + as-of 算法 | 迟到、修订、重复都有确定语义；过去的 as-of 结果不可变 | 存储量与查询复杂度增加 | — |
| B 保留 `event_time + declared_latency` 单一公式 | 简单 | 迟到数据会被当作早已可用，造成 look-ahead | 研究正确性缺陷 |
| C 修订时覆盖旧值（upsert） | 表小、查询简单 | 历史 as-of 结果被改写，无法复现 | 违背不可变与复现 |
| D 用墙钟排序修订 | 实现简单 | 时钟漂移与并发下顺序不可靠 | 不确定 |
| E 直接 streaming `RecordBatchReader` 写 partitioned 表 | 省内存 | 当前不支持；commit 失败后重试可能写零行 | 数据丢失风险 |
| F 只绑定 snapshot ID | 已有字段 | 不能证明逐行 PIT 正确 | 审计不足 |

## 契约、Schema 与迁移影响

- **现有契约字段不变**：`DatasetRef`（zone / table / `snapshot_id` / 时间区间）与 `ReproducibilityTuple.dataset_snapshots`
  保持原样；`DatasetRef` 的时间区间解释为 `event_time` 区间，实施批次在文档中写明。
- 需要新增的领域契约（例如 PIT spec、Research Dataset manifest：自身 snapshot、上游 snapshot 列表、PIT spec 引用与哈希、
  knowledge cutoff）作为**新模型**加入，属增量变化（minor）。
- **待 Codex 在接受时裁定**：是否要求复现元组**强制**引用 Research Dataset manifest。若需要在 `ReproducibilityTuple`
  或 `DatasetRef` 上新增**必填**字段，这是破坏性变化（2.0.0 已发布，D-25）——必须升 major（3.0.0）、保留 v2 只读路径，
  并另起 ADR；在此之前，该绑定作为 Runner / Registry 义务执行。
- Iceberg 表 Schema 中的 revision 字段（`observation_key`、`revision_seq`、`supersedes`、payload hash、四类时间）
  随表 Schema 版本化；表 Schema 演化遵循 10-migration.md §3（Iceberg 原生 schema evolution）。
- 无既有数据需要迁移。

## 失败与恢复语义

- commit 失败：同一 batch id 从 Raw / staging 重建后幂等重试；已写未引用的文件是 orphan，查询不可见。
- 并发写同一 key：由 commit 冲突检测串行化；冲突方重读当前 `revision_seq` 后重试，不产生重复序号。
- 重复下载 / 重放：`source ID + payload hash` 相同即去重，结果与只写一次相同。
- 进程崩溃后重启：从 catalog 的最新 snapshot 与 staging 状态恢复；不得出现"已分配但未持久化"的 `revision_seq` 被复用成另一内容。
- 解析失败：该 payload 不获得 `available_time`，不进入 Canonical；Raw 保留原始内容与失败原因。

## 安全边界

- 本 ADR 不涉及网络、凭据或交易。
- 修订历史不可删除（CLAUDE.md H6 的精神同样适用于数据修订历史）；orphan 清理只删除从未被引用的文件，不删除任何 revision。

## 验收矩阵（实施批次）

| # | 场景 | 期望 |
|---|---|---|
| 1 | 迟到首发：`event_time` 很早、`available_time` 很晚 | 在 `available_time` 之前的 as-of 查询看不到 |
| 2 | 同 key 修订 | 修订前的 as-of 结果不变；修订后返回新值 |
| 3 | 相同 `event_time` 的多条事件 | 全部保留，互不覆盖 |
| 4 | 同一 payload 重复重放 | 不产生新 revision，结果与写一次相同 |
| 5 | 跨进程重启后继续写入 | `revision_seq` 连续、无复用 |
| 6 | 并发写同一 key | 冲突被检测并串行化，无重复序号 |
| 7 | 未来修订 | 不改变任何早于其 `available_time` 的 as-of 结果 |
| 8 | 时区边界（UTC 跨日、naive 时间输入） | naive 时间拒绝；跨日行为确定 |
| 9 | 派生值可用时间 | 等于 `max(actual_ready_time, max(input.available_time) + declared_latency)` |
| 10 | `available_time < ingest_time` 或由 `event_time` 回填 | 拒绝 |
| 11 | partitioned 表写入使用不可重放 reader | 不存在；commit 失败后重试不丢行、不重复 |
| 12 | Research Dataset 缺少 PIT spec 版本或知识截止规则 | 不得进入实验 |
| 13 | 官方归档替换 | 追加新 source revision，旧 revision 可查询 |

## 后果

- 正面：look-ahead 的一个主要来源（迟到与修订）第一次有确定的防护语义；过去的 as-of 结果可复现且不可被改写。
- 负面 / 代价：存储量增加（保留全部 revision）；查询需要 as-of 选择；单 writer 限制吞吐；
  冻结文档需要一次修订。
- 对复现性：实验不仅绑定 snapshot，还绑定 PIT spec 与知识截止规则，逐行 PIT 可审计。

## 开放义务

- 接受时由 Codex 裁定：Research Dataset manifest 绑定是否进入契约强制（major）或暂作 Runner / Registry 义务。
- 接受后同步 `03-data.md` §4 / §3，并在实施批次同步 `FeatureSpec` 文档字符串（`core/domain/specs.py:150-151`）；接受时再全仓复查一次。
- 实施前核验 PyIceberg 在锁定版本上的 partitioned append、commit retry 与 `add_files` 行为。
- 每种数据类型的 `observation_key` 定义随实施批次提出并版本化。
- 多 writer 并发写与 latest view 物化方式留待需要时另行决定。

## 版本策略

- 本 ADR 自身不改变 `CONTRACT_SCHEMA_VERSION`。新增 PIT 相关契约模型为 minor；任何对既有模型的必填字段变更为 major（D-25）。
- `03-data.md` §4 的修改以本 ADR 为依据，在 ADR 接受后的独立文档批次中执行。
- PIT spec 与选择算法本身带版本号；算法变化 = 新版本，旧版本保留以复现旧实验。

## 合规检查（Proposed 阶段）

- [x] 不修改冻结文档正文（03-data.md §4 的修改只在本 ADR 中提出）
- [x] 不修改 Constitution 原则；C-L1 保持不变
- [x] 不修改任何已接受 ADR 正文或契约
- [ ] Codex 复核并接受 —— 待进行
- [ ] manifest 绑定的版本裁定 —— 接受时

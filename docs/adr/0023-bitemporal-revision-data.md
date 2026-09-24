# ADR-0023: 双时间与修订数据的 point-in-time 语义（D-28）

| 字段 | 值 |
|---|---|
| 状态 | **Proposed**（2026-09-24，批次 A1 起草；A1r、A1r2 按 Codex 两次独立复核退回意见修正；待 Codex 再复核后决定） |
| 日期 | 2026-09-24 |
| 决策者 | Codex（依据 Raphael 2026-09-24"授权所有"的持续授权） |
| 起草者 | Claude Code（Opus）按 Codex 裁决落文 |
| 相关 Phase | Phase 1（首次使用）；所有后续 Phase 的数据消费 |
| 影响范围 | Data / Contract / 复现机制 |
| 是否破坏兼容 | 冻结文档 `03-data.md` §4 的 `available_time` 定义**被本 ADR 提出修改**；已发布 v2 契约字段不变（见「契约影响」） |
| 前置 | [ADR-0009](0009-experiment-identity-binding.md)、[ADR-0012](0012-information-flow-and-kind-invariants.md)（明确把修订语义留给 Phase 1）、[ADR-0021](0021-phase1-local-data-infrastructure.md) |
| 被依赖 | [ADR-0024](0024-historical-tradable-universe.md) |

## 背景

`docs/architecture/03-data.md` §4（冻结）定义：`available_time` = `event_time + declared_latency`，
并规定在时刻 `t` 只能使用 `available_time ≤ t` 的数据（Constitution C-L1）。

这个单一公式无法表达三件事：**迟到**（很早发生、很晚才被得知）、**修订**（数据源后来更正同一观察，例如官方归档被替换，
ADR-0022）、**重复与并发**（同一 payload 被重复下载或并发写入）。ADR-0012 明确把修订语义留给 Phase 1；
C1 复审把它登记为 D-28。

### A1 版本的缺陷（Codex 复核退回）

A1 版本把 `available_time` 定义为"不早于本机 `ingest_time`"，又用**同一个**模拟时刻 `t` 同时筛选历史可用性与本机修订。
结果是：2026 年下载的 2024 年历史数据，在 2024 年的回测里全部不可见——Phase 1 的历史 backfill → PIT → Representation
纵向切片无法成立。反过来，如果干脆忽略本机 ingest 时间，后来才得知的修订又会被错误地回写到过去。

本修订的核心是把两条时间轴**显式分开**：

- **历史策略时钟**（simulation axis）：这条市场观察在历史上什么时候可以被策略使用——执行 Constitution C-L1；
- **本机知识时钟**（knowledge axis）：本系统什么时候得知、校验并可以选用这个具体 revision——决定一份数据集看到哪一个 vintage。

Constitution C-L1 的原则（只用 `available_time ≤ t`）**不变**：其中 `t` 是 `simulation_time`；本 ADR 在架构层增加独立的知识截止约束。

### A1r 版本的缺陷（Codex 第二次复核退回）

A1r 版本规定 `revision_seq` 按本机得知顺序分配、PIT 取最大 `revision_seq`。如果来源的新修订先被 ingest、旧修订后到，
本机最后到达的旧 payload 就会覆盖新 payload。本机追加顺序只能用于审计、幂等与恢复，**不能**作为来源修订的语义优先级。
本修订（A1r2）把**追加顺序**（`arrival_seq`）与**修订优先级**（`supersedes` 关系与来源证据）分开。

### 外部事实的核验状态

PyIceberg 的写入限制由 Codex 于 2026-09-24 按官方文档复核：partitioned 表不支持 streaming `RecordBatchReader` append，
reader 被消费后 commit retry 不能朴素重用（来源：py.iceberg.apache.org 的 configuration 与 reference/pyiceberg/table 页面）。
Claude 未联网、未安装任何依赖；实施批次仍须在锁定的 PyIceberg 版本上做行为 smoke / integration 验证。

## 裁决

### 1. 两条时间轴与六个时间字段（全部为 UTC）

| 字段 | 所属轴 | 精确定义 | 规则 |
|---|---|---|---|
| `event_time` | 历史 | 市场事件或区间**实际发生**的时间（valid time）；区间型数据同时记录区间起止 | 来自数据内容 |
| `source_time` | 历史 | 数据源**明确声明**的发布或更新时间 | 可为空；没有就为空，**不得伪造** |
| `available_time` | 历史 | 该观察值在**历史策略时钟**上可被使用的最早时间；执行 C-L1：`available_time <= simulation_time` | 由版本化、可审计的 **availability policy** 计算；**可以早于** `ingest_time`（历史 backfill 正常如此）；不得等同本机处理完成时间 |
| `ingest_time` | 知识 | 本机**第一次**收到该具体 payload 的时间 | 系统记录 |
| `knowledge_time` | 知识 | 本机完成最小校验、使该具体 revision **可被数据集构建过程选用**的时间；本机的 `actual_ready_time` 属于这条轴 | 必须 `>= ingest_time` |
| `declared_latency` | 历史 | 非负的历史信息可用延迟 | **只**影响 `available_time` 的保守计算；不能代替任何知识轴时间 |

Raw 保留 source payload、source metadata、`ingest_time`、`knowledge_time`、内容哈希与 availability policy 版本。

### 2. Availability policy：历史可用时间怎样算、凭什么

- `available_time` 由**版本化的 availability policy** 计算；policy 按来源类型声明规则与证据，变化即新版本，旧版本保留。
- `available_time` 不得早于观察本身可被观察的时刻（事件发生时刻；区间型数据取区间结束），也不得早于适用的 `source_time`。
- **初始历史 backfill**：若要得到早于 `ingest_time` 的 `available_time`，policy 必须有来源证据说明该市场观察在当时已通过
  公共 feed / API 可得（例如交易所公开行情在成交时实时发布）；**不得**仅凭 `event_time` 猜测。
- **来源修订**不得被回填到其实际公开之前：
  - 数据源提供修订发布时间时，该 revision 的 `available_time` 不得早于它；
  - 数据源不提供、且无法由版本化 policy 证明时，**保守使用 `ingest_time`**，并记录质量 / 证据缺口。
- 具体 policy（例如 Binance spot 归档的首发 policy、归档替换的 policy）由实施批次提出，写明证据来源，随版本审计。

### 3. 派生值分别传播两条轴

    derived.available_time = max(派生规格自身的 source / event 可用约束, max(input.available_time) + declared_latency)
    derived.knowledge_time = max(actual_ready_time, max(input.knowledge_time), 更严格的校验完成时间（若有）)

- 今天重算历史数据时，本机墙钟完成时间**只**进入 `knowledge_time`，**不得**把历史 `available_time` 推到今天。
- 派生规格自身的可用约束例如：1m bar 在该分钟结束前不可用。
- A1 版本的 `derived.available_time = max(actual_ready_time, …)` **废止**；`available_time = event_time + declared_latency`
  这一单一公式同样废止。

### 4. 修订（append-only）：追加顺序与修订优先级分离

- 以稳定的 `observation_key` 标识同一业务观察（例如 trade 用 venue + 产品 + 成交 ID；1m bar 用 venue + 产品 + 周期 + 开始时间；
  具体键由实施批次按数据类型定义，并随表 Schema 版本化）。
- 每个新 payload 作为**不可变 revision** 追加，记录：`arrival_seq`、`revision_id`、`supersedes`、可用时的 `source_revision_id`
  与 `source_revision_time`、payload hash、`available_time`、`ingest_time`、`knowledge_time`、availability policy 版本；
  Canonical revision 另绑定其 Raw source revision lineage。
- **Raw 不覆盖；Canonical 也保留 append-only revision / changelog**。latest view 可以存在（以当前全部知识执行 §5 的同一算法），
  但不是审计真相。

**追加顺序 `arrival_seq`（只表示本机追加顺序）**

- 本机 append / arrival 序号：**唯一、稳定、跨重启可恢复、不复用**；并发写由 Iceberg commit 与唯一性控制串行化或冲突后重试。
- 允许出现间隙（例如事务回滚、重试重新分配）；**无间隙不是正确性条件**。
- 它**只**用于审计、幂等与恢复，**不决定**哪个内容更新；不得在任何选择算法中作为语义优先级或冲突决胜条件。

**修订身份与优先级**

- `revision_id`：稳定的修订身份，由版本化规则从来源身份 + payload / content hash（及 `observation_key`）形成；
  同样的输入无论何时、以何顺序到达，都得到同一个 `revision_id`。
- `supersedes`：零个或多个 `revision_id`，表示"本 revision 取代了哪些 revision"。它与来源证据（`source_revision_id`、
  `source_revision_time`）一起决定语义优先级。
- **来源可证明的优先级**：若来源的版本化 policy 能用已记录的 `source_revision_id` / `source_revision_time` **证明**先后，
  必须在 ingest 时把该关系**持久化**为 `supersedes` / precedence 证据（记录产生它的 policy 版本与其 `knowledge_time`），
  而不是在查询时临时推断。
- **图约束**：`supersedes` 图拒绝环、自指，以及指向不同 `observation_key` 的边。允许指向尚未 ingest 的 predecessor
  （dangling），但必须记录；该 predecessor 到达后关系保持不变。具体存储约束由实施批次决定。
- **重复**：来源身份 + payload hash 完全相同即为重复——幂等去重，**不**产生新 revision，**不**分配新的 `arrival_seq`。
- **竞争修订**：同一 `observation_key`、不同 payload，且无法证明先后——这是 **competing heads**，不得被当作"较晚到达的新版本"。
- **Canonical 继承 Raw lineage**：Canonical revision 的 `supersedes` 由其 Raw source revision 的关系派生；normalizer 重跑
  **不得**重新发明来源优先级。
- **官方归档替换**：同一路径、不同 checksum = 新 source revision（ADR-0022 §4），旧归档与其解析结果不得物理覆盖；
  新旧文件之间能否证明先后，由该来源的版本化 policy 决定并持久化为证据，无法证明时按 competing heads 处理。

### 5. Point-in-time 查询：两个显式参数

PIT 查询必须同时给出：

- `simulation_time`：策略 / 市场时钟（或一个 simulation 区间，逐时刻求值）；
- `knowledge_cutoff`：本数据集允许使用的本机 revision / vintage 截止时刻。

算法（对给定 `(simulation_time, knowledge_cutoff, snapshot, policy)`，逐个 `observation_key` 执行）：

1. **候选集**：revision 必须**同时**满足 `available_time <= simulation_time` 与 `knowledge_time <= knowledge_cutoff`；
2. **可用的优先级知识**：取 `knowledge_time <= knowledge_cutoff` 的全部 revision 所声明的 `supersedes` 边，以及同样在该 cutoff
   之前持久化的 precedence 证据，构成不可成环的 revision DAG（可以经过尚不在候选集中的 revision 传递）；
3. **淘汰**：被另一候选 revision 直接或传递 supersede 的候选被淘汰，剩下的是 maximal heads；
4. **选择**：
   - 只剩一个 maximal head：选择它；
   - 没有候选：该 key 在此时刻不存在；
   - 多个互不排序的 maximal heads：标记 **conflict**，**fail closed**（该数据集构建失败），并写质量事件；
     **禁止**用 `arrival_seq`、墙钟或 payload hash 静默打破语义冲突。冲突只能通过追加新的、有证据的 precedence 记录解决，
     且只对 `knowledge_cutoff` 不早于该记录 `knowledge_time` 的查询生效；
5. **稳定性**：相同 Iceberg snapshot、相同 PIT spec、相同 `simulation_time`（或区间）、相同 `knowledge_cutoff`、
   相同 availability policy 版本的结果必须**稳定**（按位一致），与到达顺序无关。

由此得到的性质：

- 晚到本机的首发数据，在早于其 `knowledge_time` 的 `knowledge_cutoff` 下不可见；
- 较晚的 cutoff 可以看到新 revision，但**不会**改变任何既定 cutoff 下的旧结果；
- 后来公开的更正，其 `available_time` 不早于公开时间，因此不会因为 `event_time` 很早就进入更早的 simulation；
- 2026 年 ingest 的 2024 年历史数据可以用于 2024 年的 simulation——不再被结构性禁止——但其历史 `available_time`
  必须有 availability policy 证据；
- 相同 `event_time` 的多条事件是不同的 `observation_key`，全部保留；
- 来源的新版本先被 ingest、旧版本后到，仍然选择来源语义上的新版本。

前向运行（paper / live）时 `knowledge_cutoff` 取当前时刻，两条轴自然重合。

### 6. Research Dataset 与 `ResearchDatasetManifest`（Codex 裁决）

- **不升 3.0.0**，不给已发布的 `ReproducibilityTuple` / `DatasetRef` 新增任何必填字段。
- Phase 1 新增 `ResearchDatasetManifest` 契约与对应表。manifest 绑定：该 Research Dataset 自身的 Iceberg `snapshot_id`、
  全部上游 `snapshot_id`、PIT spec（版本与内容哈希）、`simulation_time` 或区间、`knowledge_cutoff`、availability policy 版本、
  universe selection spec（ADR-0024，以 `name + SemVer + content hash` 绑定）以及成员 / 排除清单。
- 未来 Runner 的**新接口必须接收** manifest，并要求复现元组的 `dataset_snapshots` 包含该 Research Dataset 自身的 `DatasetRef`。
  这是尚未存在的 Runner / Registry 接口义务，**不修改**已发布 v2 字段。
- 只有 snapshot ID 不足以证明逐行 PIT 正确；缺少 manifest 的 Research Dataset 不得进入实验。

### 7. PyIceberg 写入约束（Phase 1）

- partitioned 表用**有界的 `pyarrow.Table` microbatch** 写入；每个 batch 有稳定的 batch id；失败重试时必须能从已落盘的
  Raw / staging **重新构造同一批**。
- **不得**把不可重放的 `RecordBatchReader` 直接交给 partitioned append（Codex 已复核官方文档：不支持，且 reader 被消费后
  朴素重试可能写入零行）。可选的两条路是"重建 reader"或"先写可重放的 Parquet 再 `add_files`"；**本阶段选择有界 microbatch + 可重建输入**。
- 初期每个逻辑表 / 分区只允许**一个 writer**；commit conflict 按 batch id **幂等**重试。
- 未被任何 snapshot 引用的 orphan 文件只由**显式 maintenance** 任务清理，不在写入路径中顺手删除。
- microbatch 大小不在本 ADR 选择，由实施批次按内存约束（WSL 约 15 GiB）设定并可配置。

### 8. 对冻结文档的修改（本 ADR 提出，接受后执行）

`03-data.md` §4 是冻结内容。本 ADR **提出**如下修改，**获批前不改冻结正文**：

- 时间表改为本 ADR §1 的六个字段与两条轴；
- 删除"`available_time` = `event_time + declared_latency`"，改为引用 §2 的 availability policy 与 §3 的派生公式；
- Point-in-time 规则保留"只能使用 `available_time ≤ t`"并写明 `t` 是 `simulation_time`，补充 `knowledge_cutoff` 约束与 §5 的选择算法；
- §3 Research Dataset 的绑定补充 `ResearchDatasetManifest`。

起草时全仓检索：该公式只出现在 `03-data.md` §4；`02-domain.md`、`06-experiment.md` 只引用"`available_time > t`"规则。
代码中 `FeatureSpec` 的文档字符串（`core/domain/specs.py:150-151`）以 `event_time + available_lag <= t` 表述可用性，
接受后在实施批次中同步（`available_lag` 作为该规格的 `declared_latency`，只作用于历史轴）。

## 明确不做

- 本批次不修改 `03-data.md` 冻结正文，也不改任何契约或代码（包括 `FeatureSpec` 文档字符串）。
- 不改变 Constitution C-L1 ~ C-L6 的任何原则。
- 不写出具体的 availability policy、microbatch 大小、分区方案或 `observation_key` 字段（由实施批次提出并版本化）。
- 不引入多 writer 并发写同一分区；不定义 latest view 的物化方式。
- 不给已发布 v2 契约增加必填字段，不升 major。

## 备选方案

| 方案 | 优点 | 缺点 | 为何拒绝 |
|---|---|---|---|
| **A（本 ADR）** 历史轴 + 知识轴分离，append-only revision，双参数 PIT 查询 | 历史 backfill 可用；迟到与修订不回写过去；vintage 可复现 | 多一个时间字段与查询参数 | — |
| B 保留 `event_time + declared_latency` 单一公式 | 简单 | 迟到与修订被当作早已可用 | look-ahead |
| C A1 版本：`available_time >= ingest_time`，单一 `t` | 保守 | 晚于 ingest 的历史数据在历史回测中全部不可见，纵向切片不成立 | 功能性缺陷 |
| D 忽略 ingest / knowledge 时间 | 历史全部可用 | 后来得知的修订回写过去；vintage 不可复现 | look-ahead 与不可复现 |
| E 修订时覆盖旧值（upsert） | 表小 | 历史 as-of 结果被改写 | 违背不可变与复现 |
| F 用墙钟排序修订 | 简单 | 时钟漂移与并发下不可靠 | 不确定 |
| G 直接 streaming `RecordBatchReader` 写 partitioned 表 | 省内存 | 不支持；重试可能写零行 | 数据丢失风险 |
| H 在 `ReproducibilityTuple` / `DatasetRef` 加必填 manifest 字段（升 3.0.0） | 契约层强制 | 已发布 v2 的破坏性变化，过早 | Codex 裁决：由新 manifest 契约 + Runner 接口义务承担 |
| I A1r 版本：按本机到达顺序（最大 `revision_seq`）取最新 | 简单 | 新版本先到、旧版本后到时，旧 payload 覆盖新 payload | Codex 第二次复核退回 |
| J 用 `arrival_seq`、墙钟或 payload hash 打破竞争修订 | 永远能选出一个 | 静默选择无依据的版本，冲突不可见 | 违背审计与正确性；改为 fail closed |

## 契约、Schema 与迁移影响

- **已发布 v2 字段不变**：`DatasetRef`（zone / table / `snapshot_id` / 时间区间）与 `ReproducibilityTuple.dataset_snapshots`
  保持原样；`DatasetRef` 的时间区间解释为 `event_time` 区间，实施批次在文档中写明。
- 新增 `ResearchDatasetManifest`、PIT spec、availability policy 等**新契约模型**，属增量变化（minor）。
- 不向 `Kind` 枚举增加取值（见 ADR-0024 §5）。
- Iceberg 表 Schema 中的 revision 字段（`observation_key`、`arrival_seq`、`revision_id`、`supersedes`、`source_revision_id`、
  `source_revision_time`、payload hash、六个时间字段中适用者、availability policy 版本、precedence 证据、Canonical 的 Raw lineage）
  随表 Schema 版本化，遵循 10-migration.md §3。
- 无既有数据需要迁移。

## 失败与恢复语义

- commit 失败：同一 batch id 从 Raw / staging 重建后幂等重试；已写未引用的文件是 orphan，查询不可见。
- 并发写同一 key：由 commit 冲突检测串行化；冲突方重试并重新分配 `arrival_seq`，不产生重复序号（允许间隙）。
- 重复下载 / 重放：`source ID + payload hash` 相同即去重，结果与只写一次相同。
- 进程崩溃后重启：从 catalog 当前 snapshot 与 staging 状态恢复；已分配的 `arrival_seq` 不得被复用成另一内容。
- `supersedes` 成环、自指或跨 `observation_key`：该 revision 拒绝写入并记录质量事件。
- competing heads：该 `observation_key` 的 PIT 查询 fail closed，数据集构建失败并写质量事件；不自动选择。
- availability policy 缺证据：该 revision 的 `available_time` 保守取 `ingest_time`，并写质量 / 证据缺口记录，不猜测。
- 解析失败：该 payload 不获得 `knowledge_time`，不进入 Canonical；Raw 保留原始内容与失败原因。

## 安全边界

- 本 ADR 不涉及网络、凭据或交易。
- 修订历史不可删除；orphan 清理只删除从未被引用的文件，不删除任何 revision。

## 验收矩阵（实施批次）

| # | 场景 | 期望 |
|---|---|---|
| 1 | 历史 backfill：`available_time < ingest_time <= knowledge_time`，且 availability policy 有证据 | 合法；`knowledge_cutoff >= knowledge_time` 时在 `simulation_time >= available_time` 可查询到 |
| 2 | 较早的 `knowledge_cutoff` | 看不到更晚 ingest 的首发或修订 |
| 3 | 较晚的 `knowledge_cutoff` | 看到新 revision，但同一旧 cutoff 的查询结果不变 |
| 4 | 后来公开的更正（`event_time` 很早） | 不进入早于其 `available_time` 的 simulation |
| 5 | 数据源不提供修订发布时间、policy 无法证明 | `available_time` 取 `ingest_time`，并有证据缺口记录 |
| 6 | 今天重算派生历史数据 | `available_time` 不被推到今天；`knowledge_time` 为今天 |
| 7 | 相同 snapshot + PIT spec + `simulation_time` / 区间 + `knowledge_cutoff` + availability policy | 结果按位一致 |
| 8 | 来源新版本先 ingest、旧版本后 ingest | 仍选来源语义上的新版本 |
| 9 | A 被 B supersede、B 被 C supersede，三者以任意到达顺序进入 | 选 C |
| 10 | 同一 key 两个无 precedence 证据的不同 payload | conflict，fail closed，写质量事件 |
| 11 | `supersedes` 成环、自指、指向不同 `observation_key` | 拒绝 |
| 12 | dangling predecessor 后到 | 关系保持不变，选择结果不因到达顺序反转 |
| 13 | 同一 payload 重复重放 | 不产生新 revision，不分配新 `arrival_seq` |
| 14 | 相同 `event_time` 的多条事件 | 全部保留，互不覆盖 |
| 15 | 跨进程重启后继续写入 | `arrival_seq` 唯一、稳定、不复用；允许间隙，不以无间隙为条件 |
| 16 | 并发写同一 key | 冲突被检测并串行化，无重复序号 |
| 17 | 以 `arrival_seq`、墙钟或 payload hash 决定修订优先级 | 不存在（静态检查与测试） |
| 18 | normalizer 重跑 | Canonical 的 supersedes 与 Raw lineage 一致，不重新发明优先级 |
| 19 | `knowledge_time < ingest_time`、`available_time` 无 policy 证据却早于 `ingest_time`、或由 `event_time` 直接回填 | 拒绝 |
| 20 | 时区边界（UTC 跨日、naive 时间输入） | naive 时间拒绝；跨日行为确定 |
| 21 | partitioned 表写入使用不可重放 reader | 不存在；commit 失败后重试不丢行、不重复 |
| 22 | Research Dataset 缺少 manifest 或 manifest 缺任一绑定项 | 不得进入实验 |
| 23 | 官方归档替换 | 追加新 source revision，旧 revision 可在旧 cutoff 下查询；先后按 policy 证据，无证据则为 competing heads |

## 后果

- 正面：历史 backfill 可以正确用于历史 simulation；迟到与修订不会回写过去；一份数据集的 vintage 由 `knowledge_cutoff`
  唯一确定并可复现。
- 负面 / 代价：多一个时间字段与一个查询参数；每个来源都需要有证据的 availability policy；存储全部 revision；单 writer 限制吞吐。
- 对复现性：实验通过 manifest 绑定 snapshot、PIT spec、两个 cutoff 与 policy 版本，逐行 PIT 可审计。

## 开放义务

- 实施批次为每个来源提出 availability policy（含 Binance spot 归档首发与归档替换），写明证据。
- 接受后同步 `03-data.md` §4 / §3，并在实施批次同步 `FeatureSpec` 文档字符串；接受时再全仓复查一次。
- 实施前在锁定的 PyIceberg 版本上做 partitioned append、commit retry 与 `add_files` 的 smoke / integration 验证。
- 每种数据类型的 `observation_key`、`revision_id` 形成规则与各来源的 precedence policy 随实施批次提出并版本化。
- `supersedes` 图（含 dangling predecessor）的具体存储与校验约束由实施批次决定。
- 未来 Runner 接口接收 `ResearchDatasetManifest` 的具体签名随首次消费它的 Phase 交付（ADR-0017）。

## 版本策略

- 本 ADR 不改变 `CONTRACT_SCHEMA_VERSION`。新增契约模型为 minor；已发布 v2 字段不变，不升 major。
- `03-data.md` §4 的修改以本 ADR 为依据，在 ADR 接受后的独立文档批次中执行。
- PIT spec 与 availability policy 各自带版本号；变化 = 新版本，旧版本保留以复现旧实验。

## 合规检查（Proposed 阶段）

- [x] 不修改冻结文档正文（03-data.md §4 的修改只在本 ADR 中提出）
- [x] 不修改 Constitution 原则；C-L1 保持不变，`t` 明确为 `simulation_time`
- [x] 不修改任何已接受 ADR 正文或已发布契约字段
- [x] 接受时问题已由 Codex 裁决并写入（不升 3.0.0；新增 `ResearchDatasetManifest`）
- [x] 追加顺序（`arrival_seq`）与修订优先级（`supersedes` / 来源证据）已分离（A1r2）
- [ ] Codex 再复核并接受 —— 待进行

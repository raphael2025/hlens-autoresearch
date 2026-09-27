# ADR-0028: 双 Raw 通道进入 Canonical 的 revision 身份、三跳 lineage 与 precedence 映射（E0）

| 字段 | 值 |
|---|---|
| 状态 | **Accepted（2026-09-25，Raphael 以"同意推荐方案"批准方案 B）**；尚待实施（E1 起）。起草 E0 `ca48a36`，Cursor 审计修正 E0-R1 `e2951e4` |
| 日期 | 2026-09-25 |
| 决策者 | Raphael（项目最终决策者，2026-09-25 对 D-E0 决策包回复"同意推荐方案"）；同时确认 §3.0 对 ADR-0023 §4 的解释 |
| 起草者 | Claude Code（Opus），Phase 1 批次 E0（docs-only），依 Raphael 2026-09-25 的持续执行授权 |
| 相关 Phase | Phase 1（roadmap 验收 #15、#18、#20 的前置设计） |
| 影响范围 | Data / Infrastructure |
| 是否破坏兼容 | 否：不改 12 张表定义、不改 `core/` 契约与 Schema、不新增表；只新增标识符与 E1 的实现规则 |
| 前置 | [ADR-0022](0022-phase1-market-and-execution-scope.md)、[ADR-0023](0023-bitemporal-revision-data.md)、[ADR-0024](0024-historical-tradable-universe.md)、[ADR-0027](0027-rest-raw-source-and-element-revisions.md) |
| 基线 | D4 `a536ef3`（REVIEW_PENDING），其下 D3E-R2 `c326434`（REVIEW_PENDING）。本 ADR 的接受不等于 D3E / D4 代码或门记录已验收，二者仍待 Codex |
| 落点 | `03-data.md` §7.3（四个新标识符）、§7.7（Canonical 设计摘要）——接受时已并入 |

## 背景

ADR-0023 冻结了 Canonical 的 append-only revision、Raw lineage 绑定，以及"Canonical 的 precedence 只从持久化的 Raw
关系派生，normalizer 不重新发明"（§4）。ADR-0027 之后，同一 `observation_key` 可以有两条合法 Raw 通道：

- 归档元素 revision（`raw.binance_spot_agg_trades` / `raw.binance_spot_klines_1m`），source 为归档 revision；
- REST 元素 revision（`raw.binance_spot_rest_agg_trades` / `raw.binance_spot_rest_klines_1m`），source 为 REST 响应 revision；

两者之间的项目政策 precedence 只存在于独立证据表 `raw.binance_spot_precedence_evidence`，而且可以**晚于**两端到达。
冻结的 `canonical.trades` / `canonical.bars_1m` 各有一组 revision 列、行内 `supersedes` / `precedence_evidence` 与
四个 `lineage_*` 列。E1 之前必须冻结：Canonical revision 身份怎样区分两条 lineage、行内与外部 precedence 各放什么、
晚到的 Raw 边怎样生效而不改写旧行、双时间与 `arrival_seq` 怎样传播、normalizer 与 PIT 读哪些固定 snapshot。
否则实现者必须临场做架构选择。

## 裁决（提案）

### 1. Canonical revision 与 Raw element revision 一一对应

每条被 normalizer 接受的 Raw element revision，在给定 normalizer 版本下恰好产生**一条** Canonical revision；
Canonical 不合并、不挑选、不丢弃 Raw revision。竞争、相等、晚到的关系都留在 revision 图里由 PIT 裁决。

| 列 | 规则 |
|---|---|
| `contract_schema_version` | 写入时的 `CONTRACT_SCHEMA_VERSION`（当前 `2.0.0`），即行映射回 `RevisionRecord` 时的契约信封版本 |
| `observation_key` | **与 Raw 元素逐字符相同**（`binance:spot:agg_trade:<symbol>:<aggTradeId>`、`binance:spot:kline:<symbol>:1m:<start µs>`）。两个通道同一观察在 Canonical 仍是同一键，Raw 边的两端映射后仍同键 |
| `source_id` | Canonical source identity = `hlens.canonical.binance-spot.normalizer@<版本>` + `\|` + `lineage_raw_table` + `\|` + `lineage_raw_revision_id`。**lineage 进身份**：同一市场内容经两条 Raw lineage 到达，得到两条 Canonical revision |
| `payload_hash` | Canonical 规范内容文档的 SHA-256（§2）；只含 Canonical 市场列，不含 lineage、时间、`arrival_seq` |
| `revision_id` | `crev1-<sha256>`，由新身份规则 `hlens.canonical.revision-identity@1.0.0`（独立规则 ID / 版本 / 哈希，不复用归档或 REST 规则，ADR-0027 §11 陷阱 1）从 `{rule, observation_key, source_id, payload_hash}` 的规范 JSON 派生 |
| `lineage_raw_table` / `lineage_raw_revision_id` | 该 Raw 元素 revision 所在表与 `revision_id` |
| `lineage_source_table` / `lineage_source_revision_id` | 归档路径：`raw.binance_spot_archives` + 该行 `archive_revision_id`；REST 路径：`raw.binance_spot_rest_responses` + 该行 `response_revision_id`（首次交付它的响应） |

由此：两条 lineage 的 Canonical 内容相同时 `payload_hash` 相同、`revision_id` 不同，`RevisionGraph` 的
`(observation_key, source_id, payload_hash)` 唯一性成立，`SelectedRevisionLineage` 对每个 Canonical `revision_id`
唯一确定三跳。表名、revision id、source id 与时间互不冒充：`lineage_*` 列必须与 `source_id` 内嵌的表 / id 逐字符一致。

### 2. Canonical 规范内容

| Canonical 表 | payload kind | payload 文档字段（非 `*_us` 字段与冻结表同名列一一对应） |
|---|---|---|
| `canonical.trades` | `hlens.canonical.trade/1` | `venue`、`instrument_type`、`symbol`、`venue_symbol`、`venue_trade_id`（= aggTradeId 十进制文本）、`price`、`quantity`、`buyer_is_maker`、`event_time_us` |
| `canonical.bars_1m` | `hlens.canonical.bar_1m/1` | `venue`、`instrument_type`、`symbol`、`venue_symbol`、`interval_start_us`、`interval_end_us`、`open`、`high`、`low`、`close`、`volume`、`quote_volume`、`trade_count`、`taker_buy_base_volume`、`taker_buy_quote_volume` |

`event_time_us` / `interval_start_us` / `interval_end_us` **只是 payload 文档字段**：它们是冻结列 `event_time` /
`interval_start` / `interval_end`（UTC 时间戳）的精确 UTC 纪元微秒整数；表中不存在 `*_us` 列。
十进制按 `decimal(38, 18)` 渲染为 18 位小数定点文本，时间按归档声明单位或 REST 毫秒换算（与 D-33 投影相同的换算规则），规范 JSON 排序键、紧凑分隔、UTF-8。`symbol` / `venue_symbol` 的映射由 normalizer 规格冻结
（首切片两个标的），必须与 `canonical.instrument_listings` 同一 universe spec 的 `Instrument` 一致（E2 校验）。
Canonical 内容比 Raw 少（例如 `first_trade_id` / `last_trade_id` / `is_best_match` / `ignore` 不进 Canonical 列）；
**因此 Canonical payload 相等永远不是 precedence 证据**（§3），precedence 只来自 Raw 已持久化的边。

### 3. Precedence 映射：PIT 从固定 Raw 证据 snapshot 确定性映射（方案 B）

**3.0 与 ADR-0023 §4 的关系（需 Codex 在接受时确认的解释）**：ADR-0023 §4 写"Canonical revision 的 `supersedes` 由其
Raw source revision 的关系派生；normalizer 重跑不得重新发明来源优先级"。ADR-0027 §4.4 之后，Raw 层自身把关系分成两类：
Raw 行内的 `supersedes`（同通道），以及**明确不写进任何行 `supersedes`** 的独立证据表边（跨通道，可晚到）。本 ADR
把 ADR-0023 §4 解释为"Canonical 按 Raw 的同一结构派生 precedence"：行内 ↔ 行内（§3.1），独立证据 ↔ 由 PIT 从绑定的
Raw 证据 snapshot 一对一映射（§3.2）。这样 Canonical 没有新的来源判断，也不必为了跨通道边改写已提交行或伪造新
revision；若 Codex 认为 ADR-0023 §4 要求跨通道边进入 Canonical 行内 `supersedes`，则方案 B 不成立，应改选方案 A
（独立 Canonical 证据表）而不是任何改写旧行的方案。

**3.1 行内列**：Canonical 行的 `supersedes` 与 `precedence_evidence` 只承载**所在 Raw 元素行自身行内的边**的映像。
在已冻结的 policy 下 Raw 元素行内边恒为空（归档 `binance.spot.archive-revision@1.0.0` 的边只可能出现在归档 revision 行，
且 1.0.0 下从不产生；REST `binance.spot.rest-revision@1.0.0` 从不产生边），所以 normalizer 1.0.0 写空数组，
并要求其输入行内边为空（`PersistedRowVerifier` 已证明）。将来若 Raw 元素行出现行内边，或要把归档 revision 之间的边
下沉到元素 / Canonical 层，必须新 normalizer 版本 + 新 ADR。晚到的知识**绝不回填**进已提交的 Canonical 行。

**3.2 跨通道边不物化到 Canonical**。PIT 构建时，对绑定的 `raw.binance_spot_precedence_evidence` snapshot 中每条
范围内的边 `E = (A 取代 R, K_E)`：

1. 用绑定的 Raw 元素表 snapshot 与 `PersistedRowVerifier` 复核两端 Raw 行与这条边本身（与 D3E reconciler 的
   `_existing_edges` 同一复核：政策、两端、表、投影哈希、证据项、所固定的 snapshot 存在）；
2. 在绑定的 Canonical snapshot 中，按 `(lineage_raw_table, lineage_raw_revision_id)` 与 PIT spec 绑定的 normalizer
   版本各找两端对应的 Canonical revision `A'` / `R'`：**恰好一条**则映射；零条则该边在本数据集无 Canonical 端点
   （不是错误：该 Raw revision 尚未被 normalize，其 Canonical 行也不存在）；多于一条即 fail closed；
3. 生成 `PrecedenceEvidence(observation_key, revision_id = A', superseded_revision_id = R', policy = 映射规则绑定,
   evidence = [映射规则陈述, raw edge_id, raw 证据表 snapshot, 原边全部 evidence 项], knowledge_time = K_E')`，
   其中 `K_E' = max(K_E, A'.knowledge_time, R'.knowledge_time)`。

映射规则 `hlens.canonical.precedence-map@1.0.0`（role = precedence）作为 `PointInTimeSpec.precedence_bindings`
的一项绑定（与 Raw 政策 `binance.spot.delivery-channel@1.0.0` 并列）；normalizer
`hlens.canonical.binance-spot.normalizer@1.0.0`（role = parser）作为 `parser_bindings` 的一项；
`hlens.canonical.availability@1.0.0`（§4）进入 `availability_bindings`。
**这不是查询时推断 precedence**：没有任何新的相等判断或政策判断，只是沿已持久化的 lineage 列对已持久化的 Raw 边做
一对一连接；结果只取决于 manifest 绑定的 snapshot 与规则版本，因此按位可复现。`K_E'` 取已持久化时间的最大值，
不读墙钟：这一事实只有在三个已持久化事实都存在时才成立，也不早于其中任何一个。

**3.3 通道内部**：REST 通道同 key 不同内容的 revision、归档替换产生的两条归档元素 revision，映射后仍是
没有边的 competing Canonical revision → PIT conflict，fail closed；**不**按通道、到达顺序、时间或哈希决胜。

### 4. 双时间传播（ADR-0023 §3）

| 列 | Canonical 值 |
|---|---|
| `event_time` / `interval_*` | Raw 按声明单位换算的值（同 §2） |
| `source_time`、`source_revision_id` / `_time` | Raw 值（首切片恒为空） |
| `ingest_time` | Raw 行的 `ingest_time`（本机首次收到该 payload 的时刻，不是 normalizer 时间） |
| `available_time` | `max(规格约束, raw.available_time + declared_latency)`，`declared_latency = 0`；trade 的规格约束为 `event_time`，1m bar 为 `interval_end`。今天重算只影响 `knowledge_time` |
| `knowledge_time` | `max(actual_ready_time, raw.knowledge_time)`。`actual_ready_time` 是 normalizer 对一个 normalization 单元（§5）完成全部输入证明之后、提交之前读取一次的注入 UTC 时钟；时钟早于任一输入 `knowledge_time` 即冲突（拒绝回填），不取 max 掩盖 |
| availability 绑定 | 新派生 policy `hlens.canonical.availability@1.0.0`（公式即本表）。`AvailabilityDecision` 要求证据与缺口**恰好一项**：输入 Raw 行带缺口时，`availability_evidence = []`、`availability_evidence_gap = "inherited from <raw_table>/<raw_revision_id> under <policy_id>@<version>: <Raw 缺口原文>"`；输入 Raw 行带证据时，`availability_evidence = ["input <raw_table>/<raw_revision_id> under <policy_id>@<version>", *Raw 证据项]`、缺口为空。首切片两个 Raw availability policy 都写缺口，故 Canonical 行一律走缺口分支 |

重跑与崩溃恢复**复用**已提交行的 `knowledge_time`（同 `revision_id` 幂等），不重打时钟；旧 cutoff 的结果因此不变。

四段 cutoff（`K_A'` / `K_R'` = 两条 Canonical revision 的 `knowledge_time`，`K_E'` 同 §3.2）：

| `knowledge_cutoff` | 结果（两种 Raw 到达顺序 × Raw 边早于 / 晚于 normalization 形状相同） |
|---|---|
| `< min(K_A', K_R')` | key 不存在 |
| `[min, max)` | 只见先被 normalize 的一条 |
| `[max(K_A', K_R'), K_E')` | 两个 head 均可见、边尚不可知 → conflict，fail closed（若 Raw 边早于两者 normalization，该区间为空） |
| `>= K_E'` | 边生效，唯一选中归档 lineage 的 Canonical revision |

### 5. Canonical `arrival_seq`：独立分配，不复制 Raw

- 每张 Canonical 表各自一个区间 `[0, 2**62)`，按 normalization 单元分配 `2**32` 步长的块（锚点 = 该表已提交的最大
  `arrival_seq`，流式求值，同 D2）；单元 = 一个 Raw source revision（一个归档 revision，或一个 REST 响应 revision
  的全部以它为 lineage 的元素）在一个 normalizer 版本下。
- 单元内第 `p` 行：`base + p`，`p` = 归档 `archive_line_number`（1 基）或 REST `element_index + 1`；`base` 本身空置。
- 恢复：已提交的该单元任一行即给出 `base`（且全部行必须一致）；未提交任何行时重新分配，旧块成为允许的间隙。
- 它只用于审计、幂等与恢复；PIT 与映射**从不**读取它（ADR-0023 §4）。复制 Raw 的 `arrival_seq` 被拒绝：
  第二个 normalizer 版本会碰撞，且会把 Raw 追加顺序带进 Canonical。

### 6. Normalizer 的固定输入、批次与恢复

- 输入：Raw 元素表与对应 Raw source 表（`raw.binance_spot_archives` 或 `raw.binance_spot_rest_responses`）在同一次
  读取中固定（读前读后表头一致，否则有界重读）；每行先经 `PersistedRowVerifier` 证明（D3E-R2），未证明的行不产生
  Canonical revision，整单元 fail closed。normalizer **不读** Raw 证据表（边不物化，§3.2）。
- 批次：单元内按 Raw 位置排序的确定性 microbatch，batch id =
  `hlens.canonical.binance-spot.normalizer@<版本>.<raw source revision id>.<index:08d>`；expected-parent 提交；
  快路径按 D3E 做法重建期望行并核对已提交内容与批次指纹。
- 崩溃点：单元内任意两批之间崩溃，重跑只补缺失批次，`base` 与 `knowledge_time` 取自已提交行；首批提交前崩溃则整单元
  重新开始（新块、新时钟读数，旧读数从未持久化）。
- 相同 Raw snapshot + 相同 normalizer 版本 → 相同 Canonical 行（除首次时钟读数外按位一致，重跑复用首次读数）。
  新 Raw snapshot 只追加新 Canonical revision，从不改写旧行；旧 Canonical snapshot 仍可时间旅行重建旧结果。

### 7. PIT 的固定绑定与 fail closed

使用 Canonical trades / bars 的 PIT 必须在 `snapshot_bindings` 中绑定：所读 Canonical 表；每个被选中 revision 的
lineage 涉及的 Raw 元素表与 Raw source 表；`raw.binance_spot_precedence_evidence`（凡范围内存在 REST lineage 时必需，
ADR-0027 §13）。fail closed：任一 competing Canonical head；映射时一个 Raw 端点对应多条 Canonical revision；
Raw 边或端点 Raw 行复核失败；Canonical 行的 lineage 在绑定的 Raw snapshot 中不存在或不能从该 Raw 行按 normalizer
重建；绑定缺失。不相等 / 不可比较 / 无对侧沿用 ADR-0027 §4.4（无边），质量事件由批次 E3 持久化。

## 完整示例（BTCUSDT aggTrade 100，归档先到、REST 后到、边晚于两者 normalization）

记号：`H(x)` = 规范 JSON 的 SHA-256；时间均为 UTC。

| 事实 | 已持久化值 |
|---|---|
| 归档元素 `a` | 表 `raw.binance_spot_agg_trades`，`revision_id = rev1-A`，`archive_revision_id = rev1-F`，行号 1，`knowledge_time = 12-01` |
| REST 元素 `r` | 表 `raw.binance_spot_rest_agg_trades`，`revision_id = rev1-R`，`response_revision_id = rev1-P`，`element_index = 0`，`knowledge_time = 12-05` |
| Canonical `a'` | `observation_key = binance:spot:agg_trade:BTCUSDT:100`；`source_id = hlens.canonical.binance-spot.normalizer@1.0.0\|raw.binance_spot_agg_trades\|rev1-A`；`payload_hash = H(trade/1 内容)`；`revision_id = crev1-H({rule, key, source_id, payload_hash})`；lineage `raw.binance_spot_agg_trades / rev1-A → raw.binance_spot_archives / rev1-F`；`arrival_seq = base_F' + 1`；`supersedes = []`；`knowledge_time = 12-02`（normalizer 时钟 ≥ 12-01） |
| Canonical `r'` | 同键、同 `payload_hash`；`source_id = …\|raw.binance_spot_rest_agg_trades\|rev1-R`，因此 `revision_id` 不同；lineage `… / rev1-R → raw.binance_spot_rest_responses / rev1-P`；`arrival_seq = base_P' + 1`；`knowledge_time = 12-06` |
| Raw 边 `E` | `raw.binance_spot_precedence_evidence`：`rev1-A` 取代 `rev1-R`，`knowledge_time = 12-10` |
| 映射边 `E'` | `PrecedenceEvidence(key, revision_id = a', superseded = r', policy = hlens.canonical.precedence-map@1.0.0, evidence = [映射陈述, E.edge_id, 证据表 snapshot, E 的全部 evidence 项], knowledge_time = max(12-10, 12-02, 12-06) = 12-10)` |

PIT（`simulation_time` 足够晚）：cutoff `< 12-02` 不存在；`[12-02, 12-06)` 选 `a'`；`[12-06, 12-10)` 冲突；
`>= 12-10` 选 `a'`，manifest 的 `SelectedRevisionLineage = (canonical.trades, a', raw.binance_spot_agg_trades, rev1-A,
raw.binance_spot_archives, rev1-F)`，并绑定 Canonical、两张 Raw 元素表、两张 Raw source 表与证据表的 snapshot。
另一时间线：若 `r` 直到 12-11 才被 normalize（`K_R' = 12-11`，Raw 边 12-10 已存在），则 `K_E' = max(12-10, 12-02, 12-11) = 12-11`，
冲突区间 `[12-11, 12-11)` 为空：`[12-02, 12-11)` 只见 `a'`，`>= 12-11` 两者可见且边同时生效，仍选 `a'`。

## 备选方案

| 方案 | 内容 | 反例 / 代价 | 结论 |
|---|---|---|---|
| **A** | lineage 区分身份 + 新增 Canonical 证据表物化映射后的边 | 第二个写边的 writer 与恢复路径，第二次时钟读数制造新的冲突窗口 `[max, K_C)`，与 Raw 边重复表达同一判断；需新表 + additive 迁移 | 正确但多余：映射没有新判断，不值得新状态 |
| **B（建议）** | lineage 区分身份 + PIT 从固定 Raw 证据 snapshot 一对一映射，不新增表 | PIT 执行器须实现映射与复核；manifest 须绑定 Raw 证据与 Raw 表 snapshot（ADR-0027 §13 已要求） | 采纳 |
| C | 只物化当前 Raw maximal head | REST 先到并被物化后归档到达、边在 `K_E` 生效：要么改写 / 删除 REST 的 Canonical 行（违反 append-only），要么两行并存（即不再是 C）；旧 cutoff 下的 REST 结果无法重建；competing lineage 被丢弃，冲突被静默消除 | 拒绝 |
| D | 相同 Canonical payload 跨 lineage 折叠成一行 | 归档与 REST 只在 `first_trade_id` 上不同：D-33 不等、Raw 仍冲突，但 Canonical payload 相同 → 折叠成一行，冲突被静默抹掉；一行对应两套 lineage 与两个 `knowledge_time`，`SelectedRevisionLineage` 不唯一 | 拒绝 |

## 反例与 E1 验收矩阵

E1 必须以真实 Raw 写入路径（D2 / D3E）构造输入，逐项断言：

| # | 情形 | 期望 |
|---|---|---|
| 1 | 归档先到 / REST 先到 × Raw 边早于 / 晚于 normalization × 四段 cutoff | §4 四段表逐格成立；两种顺序最终边集与选择一致 |
| 2 | equal projection | 两条 Canonical revision（`payload_hash` 同、`revision_id` 异）；`>= K_E'` 唯一选归档 lineage |
| 3 | mismatch / incomparable / 无对侧 | 无映射边；冲突者 fail closed；无对侧时单 head |
| 4 | 同通道 competing（REST 两响应不同内容、归档替换） | 两条 Canonical revision，无边，fail closed |
| 5 | 两 Raw 行市场内容相同、lineage / 时间不同 | 无 `revision_id` 碰撞，两条 lineage 均可追溯 |
| 6 | Canonical 内容相同而 Raw 内容不同（仅 `first_trade_id` 不同） | 不折叠，Raw 冲突原样传到 Canonical |
| 7 | normalizer 在单元首批前、批间、末批后崩溃 | 重跑只补缺失批次，`base` / `knowledge_time` 不变 |
| 8 | 多表读取中 Raw / 证据表头移动 | 丢弃该判断、有界重读；从不混合 snapshot |
| 9 | 旧 Canonical snapshot + 新 Raw 证据；新 Canonical snapshot + 旧 PIT 绑定 | 输出只由 manifest 绑定决定，旧 manifest 按位重建 |
| 10 | 伪造 lineage 列 / 表名 / snapshot / policy / 边端点 / payload | 复核失败，fail closed，无部分数据集 |
| 11 | `arrival_seq` 顺序反转、区间碰撞 | 语义图不变；碰撞 fail closed |
| 12 | 时钟早于输入 `knowledge_time`、naive 时钟 | 拒绝，不写行 |

## 契约、Schema 与迁移影响

- **无契约变化**：`RevisionRecord` / `RevisionGraph`（唯一性含 `source_id`）、`PrecedenceEvidence`（两端显式）、
  `SelectedRevisionLineage`（三跳、namespace 校验）、`PointInTimeSpec`（`snapshot_bindings`、`precedence_bindings`、
  `parser_bindings`）与 `ResearchDatasetManifest` 已足以表达本 ADR；Schema 导出不变。
- **无表变化**：`canonical.trades` / `canonical.bars_1m` 冻结列全部够用；不新增表，不演进分区。
- **新增标识符**（接受后并入 `03-data.md` §7.3）：`hlens.canonical.revision-identity@1.0.0`、
  `hlens.canonical.binance-spot.normalizer@1.0.0`、`hlens.canonical.availability@1.0.0`、
  `hlens.canonical.precedence-map@1.0.0`（规则文档与哈希由 E1 按 D3B 做法冻结并有回归哈希测试）。
- 现有数据：Canonical 表尚无任何行，无迁移。

## 失败与恢复语义

未证明的 Raw 行、映射歧义、绑定缺失一律 fail closed，不产生部分 Canonical 单元或部分数据集；崩溃恢复只依赖
Iceberg 已提交行与不可变 Raw，不用 journal 或可变 sidecar。

## 后果

- 正面：Canonical 不做任何 precedence 判断；晚到的 Raw 边天然生效且不改写旧行；两条 lineage 始终可追溯；
  不新增表与 writer。
- 代价：PIT 执行器（批次 F1）承担映射与复核；使用 REST 数据的每个数据集必须多绑定 Raw 证据与 Raw 表 snapshot。
- 复现：旧 manifest 的全部输入都是固定 snapshot 与规则版本，按位可重建。

## 开放义务

- 归档 revision 之间若将来有可证明先后的 policy，其边怎样下沉到元素 / Canonical 层：新 ADR；
- Canonical `symbol` 规范值与 listing 的一致性由 E2 固定；
- 质量事件的持久化与分类由 E3；
- 映射 / normalizer 规则文档哈希与黄金向量由 E1 冻结。

## 合规检查

- [x] 不破坏已冻结契约与表（无改动）
- [x] 不修改 Validation Constitution
- [x] Domain 层仍无具体技术依赖
- [x] Research / Application Plane 边界不变

## 参考

- ADR-0023 §3～§7；ADR-0027 §1、§4、§11、§13；`03-data.md` §4、§7；`02-domain.md` `SelectedRevisionLineage`
- D3E-R2 `infrastructure/revision/row_integrity.py`（`PersistedRowVerifier`）

## 实施记录（不改变上述决定）

- **E1-R1（`65aedd6`）批次号**：§6 的 batch id 在实现中多一段计划单元行数：
  `hlens.canonical.binance-spot.normalizer@<版本>.<raw source revision id>.<unit rows:010d>.<index:08d>`。
  原因：只含 `<index>` 时，崩溃后续跑与"Raw 单元在首次 normalize 后增长"无法区分，后者的新行若落入全新批次会被
  静默补全，违反 §6"Raw 单元变化即 fail closed"。加入单元行数后，同一 Raw 单元的续跑复现相同批次号，增长后的单元
  规划出不同批次号而 fail closed。格式随 normalizer 1.0.0 冻结在 `NORMALIZER_SPEC`；待 Codex / Raphael 确认此记录。
- **E1-R3 批次计划**：batch id 再加一段 microbatch 大小：
  `...<raw source revision id>.<unit rows:010d>.<chunk:06d>.<index:08d>`。已提交的批次号本身就是计划：崩溃续跑与
  核对从批次号读出单元行数与批大小，不再依赖运行时配置（此前以不同 `microbatch_rows` 续跑会切出另一种批次形状）。
  批次 `i` 必须恰为计划行 `[i*chunk, (i+1)*chunk)`（指纹与行数），已提交序号须为连续前缀，同一单元只允许一种计划，
  已提交行必须恰为已提交批次的行；批次历史仍在而行被删除、Raw 单元行数与计划不符、批次号格式非法、有行无批次，
  一律判为完整性错误且不读时钟。`BatchConflict`：尚无计划（新分配）时视为竞争，重读后采用对手的计划、不再读时钟；
  已有计划时视为数据损坏。Canonical 行内容不变；`NORMALIZER_SPEC` 哈希随之变化，版本仍为 1.0.0（尚未产生
  任何正式数据），待 Codex / Raphael 确认此记录。
- **G3-S 分窗与固定快照**：normalizer 不再一次读入整个单元。每次调用先把 Raw 元素表、Raw 来源表与 Canonical 表的
  表头读两次取一致值，此后所有读取都经 `PinnedCatalogView` 时间旅行到这些快照（取代"读前读后表头一致、否则重读"）；
  证明阶段按 Raw 位置分窗证明每一行；单元位置必须互不相同，归档单元还必须恰为其对象的全部 `1 … N` 行（G3-S-R1：缺尾行即截断），REST 单元允许缺号（别页首次交付的元素不在本页重写）；批次 `i` 是按位置排序后的第 `i` 段 `chunk` 行；写入阶段按批次窗口重读固定快照上的 Raw 行、
  规范化、提交，并在该批次自己的快照上回读（该批次的 `arrival_seq` 区间恰为计划行且各一次；revision id 在同一
  symbol 与该批行的时间范围内各只出现一次——冒用他人时间的行由 PIT 对其自称单元的证明拒绝）；重放时对已提交批次同样复查 revision id；最后核对单元的行恰为
  `base + 各 Raw 位置`、整个块内没有其他行。§6 的全部语义（一次时钟读数、证明先于读时钟与提交、崩溃续跑、
  Raw 变化即拒绝）不变，Canonical 行与批次号逐位不变。
- **G2-R1a 单元完整性（红队 RT-1 / RT-2 / RT-3）**：(1) 读取方（`verify_unit` 全量与按批，即 F1 / E3 / F3）只接受
  计划的全部批次均已提交的单元；已提交前缀是中途停止的 normalization，判 `CanonicalUnitIncomplete`（属
  `CatalogIntegrityError`，fail closed），重跑 normalizer 即解除。(2) REST 单元的缺号只允许是本页 body 中由**另一**已提交
  响应 revision 首次交付的元素：normalizer 严格重解码该响应首次交付的页面，逐个元素按 store 的同一身份规则求出
  revision id，缺失者必须在固定快照中恰有一行、属于另一响应且证明合法；否则判 `CanonicalUnitIncomplete`，在读时钟与
  任何提交之前拒绝，store 重跑补齐后再 normalize。仅凭响应行的 `element_count` 无法区分"别页已交付"与"丢失"，故不采用。
  (3) E3 报告（并经其 `existing_only` 复算覆盖 F3）要求分区内（venue symbol × UTC 日）每条绑定快照中的 Raw 元素
  revision 都已有 Canonical 映像，否则 `RawNotDerived`（与 listing 的 `LISTING_NOT_DERIVED` 对应），只读窄列。
  Canonical 行、批次号与规则哈希不变；待 Codex 确认此记录。

# ADR-0027: REST 补尾的 Raw source / element revision、通道等价 precedence 与三跳 lineage（D-33）

| 字段 | 值 |
|---|---|
| 状态 | **Proposed**（2026-09-25，Phase 1 D3A 起草；D3A-R1 按 Codex 复核修正；待 Codex 再次复核并另作接受门，未获批准前不得实现） |
| 日期 | 2026-09-25 |
| 决策者 | Codex（Raphael 2026-09-24 "授权所有" 的持续授权范围内） |
| D-33 | **Codex 已选择方案 A**（项目定义的规范市场内容等价 + ADR-0022 归档为权威 backfill 通道，§4）；随本 ADR 整体接受后生效 |
| 起草者 | Claude Code（Opus），Phase 1 批次 D3A / D3A-R1（docs-only） |
| 相关 Phase | Phase 1（roadmap 验收 #13 的前置设计） |
| 影响范围 | Data / Infrastructure / Security |
| 是否破坏兼容 | 否：新增四张 Raw 表与新增标识符；八张冻结表定义、`core/` 已发布契约、`CollectorAdapter` Protocol、Schema 导出、`IDENTITY_SPEC` / `IDENTITY_HASH` 全部不变 |
| 前置 | [ADR-0021](0021-phase1-local-data-infrastructure.md)、[ADR-0022](0022-phase1-market-and-execution-scope.md)、[ADR-0023](0023-bitemporal-revision-data.md)、[ADR-0024](0024-historical-tradable-universe.md) |
| 证据 | [binance-spot-rest-market-data.md](../architecture/evidence/binance-spot-rest-market-data.md)（2026-09-25 检索） |
| 落点 | `03-data.md` §7.6（提案小节；接受门把它并入 §7.1 / §7.3 / §6.2 冻结正文） |

## 背景

ADR-0022 把 REST 定位为**只补尾 / 补缺口**的通道（权威 backfill 仍是官方归档），并要求原始响应进入 Raw、可复现。
ADR-0023 §4 要求每个 payload 作为不可变 revision 追加；B2 的 `SelectedRevisionLineage` 把选中数据的来源链固定为三跳：

    Canonical revision → Raw row revision → Raw source payload revision

归档路径上这三跳已经落地（`canonical.*` → `raw.binance_spot_agg_trades` / `raw.binance_spot_klines_1m`
→ `raw.binance_spot_archives`，D1 / D2 已验收）。REST 路径上还没有任何一跳可以诚实落地：没有承载 HTTP 响应字节的
Raw source 表；归档元素表的 `archive_revision_id` / `archive_line_number` 是必填的归档语义列；§7.1 是冻结清单。

同一市场观察可以既由归档、又由 REST 交付：它们是同一 `observation_key` 的两条 revision，无证据即 competing heads，
数据集 fail closed。归档终将覆盖曾由 REST 补过的区间，因此必须有一条跨通道 precedence 规则（D-33）。

### D3A-R1 修正（Codex 复核 `7111e54` 的缺陷）

| # | 缺陷 | 本版如何关闭 |
|---|---|---|
| F1 | 目标窗口与页面合法性互相矛盾 | §6：**collection target window** 只用于停止与覆盖账；**page validity envelope** 只由实际查询、排序、字段形状、声明单位与有界规则决定；越出目标窗口的合法元素照常入 Raw |
| F2 | response revision 被写成"无 knowledge_time" | §8：response revision 在字节 / 哈希 / 对象发布 / 元数据校验后即有必填 `knowledge_time`；元素解码失败只阻止元素层 |
| F3 | 本机预算耗尽可能被伪装成来源缺口 | §7：页数上限、限流、5xx、传输失败、截断、无效 JSON、envelope 违约一律 `CollectionFailed`，不产出成功结果；`SOURCE_ABSENT` 只用于短页 / 空页之后、陈述确切查询返回的尾部 |
| F4 | `CollectorAdapter` 重放义务未满足 | §5 / §9：逻辑 `request_id` 与规范页身份分离；首次已承诺的页经不可变 checkpoint 对象重放，不再联网 |
| F5 | `arrival_seq` 方案与"identity.py 一字不动"冲突 | §11：`identity.py`、D2 store 均不改；区间划分 + D3E 在被聚合的单个 graph 内做 range guard |
| F6 | 批次与设置不完整 | §12 列齐设置；roadmap D3B～D3E 按四表方案重拆 |
| F7 | 证据"两个独立渲染路径"措辞过度 | 证据文件改为"两种官方访问 / 渲染路径交叉核对同一规范" |
| F8 | 状态与索引不一致 | 状态文件、ADR 索引同步（含 ADR-0026 已实施） |
| — | 跨通道边无处诚实存放 | 冻结表的 `precedence_evidence` 省略外侧新端，只能表达"本行取代旧行"；archive 先到、REST 后到时无法在任何新行中表达 `archive → REST`。因此新增**第四张表**：独立的 Raw precedence evidence 表（§1 / §4） |

## 裁决

### 1. 表拓扑：additive，四张新表，八张冻结表一字不改

| 表 | 职责 | 初始分区 | 定义版本 |
|---|---|---|---|
| `raw.binance_spot_rest_responses` | **Raw source payload revision**：一个 HTTP 响应页的规范页身份、响应字节对象引用、HTTP 元数据、页面解码摘要、revision 块与两轴时间 | 不分区 | `1.0.0` |
| `raw.binance_spot_rest_agg_trades` | 从某响应页解码出的**单条 aggTrade 元素 revision** | identity `symbol` + `day(event_time)` | `1.0.0` |
| `raw.binance_spot_rest_klines_1m` | 从某响应页解码出的**单条已结束 1m kline 元素 revision** | identity `symbol` + `day(interval_start)` | `1.0.0` |
| `raw.binance_spot_precedence_evidence` | **独立、append-only 的 precedence 证据**：每行一条完整的 `PrecedenceEvidence`（两端都显式），首个用途是 D-33 的跨通道边 | 不分区 | `1.0.0` |

- 四张表都在既有 `raw` namespace 下；`SelectedRevisionLineage` 与 `canonical.*` 的 `lineage_*` 列无需改动即可表达
  `canonical.trades → raw.binance_spot_rest_agg_trades → raw.binance_spot_rest_responses`。
- 元素表复用归档元素表的 revision 块与原生字段列，但把 `archive_revision_id` / `archive_line_number` / `parser_*` 换成
  `response_revision_id` / `element_index` / `decoder_*`（语义不同、名字就必须不同）。元素表与响应表仍带与冻结表同形的
  `precedence_evidence` 列（外侧新端 = 本行），只承载**同通道**边；1.0.0 下它恒为空。
- **为什么不改现有归档表**：`archive_revision_id` 改为可空 = 冻结表语义变化（10-migration.md §3 的 major），
  D1 / D2 的验收证据需重跑。additive 方案让归档路径的定义哈希、数据与验收证据逐字节不变。
- **为什么要第四张表**：冻结的 `precedence_evidence` 结构没有外侧 `revision_id`，隐含"新端 = 承载行"。
  跨通道边的新端永远是归档 revision；当 REST 后到时，只有 REST 行是新行，却无法表达"别人取代我"；把反向边塞进
  REST 行需要一种与冻结结构不同的隐含方向，是不诚实的双重语义。独立证据表显式保存两端，与到达顺序、承载行无关，
  且任何已提交的 revision 行都不需要改写。

`raw.binance_spot_precedence_evidence` 的列级意图（D3B 按此冻结字段 ID 与 doc）：

| 列 | 语义 |
|---|---|
| `edge_id` | 稳定边身份：`edge1-<sha256>`，由 `{policy_id, policy_version, policy_hash, observation_key, revision_id, superseded_revision_id}` 的规范 JSON 派生；**不含** `knowledge_time`、到达顺序或墙钟 |
| `observation_key` | `PrecedenceEvidence.observation_key`：两端共同的完整键 |
| `revision_id` / `revision_table` | 取代方（1.0.0 恒为归档元素 revision）及其所在 Raw 表 |
| `superseded_revision_id` / `superseded_table` | 被取代方（1.0.0 恒为 REST 元素 revision）及其所在 Raw 表 |
| `policy_id` / `policy_version` / `policy_hash` | `PolicyBinding(role=precedence)` |
| `evidence` | `PrecedenceEvidence.evidence`（非空，§4.5） |
| `knowledge_time` | `PrecedenceEvidence.knowledge_time`：首次成功比较并提交该事实的本机时刻（§4.6） |
| `revision_snapshot_id` / `superseded_snapshot_id` | 比较时读取两端元素表所固定的 Iceberg snapshot（审计与复核用） |
| `projection_sha256` | 两侧相等的规范内容投影的 SHA-256（§4.3） |
| `contract_schema_version` | 行映射回 `PrecedenceEvidence` 时的契约信封版本 |

该表的行是 `PrecedenceEvidence` 的物理形态；若 D3B 需要内部持久化 DTO，它是 `infrastructure/` 内部类型，
**不新增跨 Plane 公共契约**。

### 2. Raw source revision：一个 HTTP 响应页

| 项 | 规则 |
|---|---|
| `observation_key` | `binance:spot:rest:<page_identity_sha256>`（§5）。观察对象是"这个确切的页查询的回答"，不是"这段行情" |
| `payload_hash` | 响应**实体正文**（HTTP 客户端去除 content coding 后、解析之前的字节）的 SHA-256 |
| 字节留存 | 经 `StorageAdapter` staging → 大小上限 / 哈希 → 原子发布为不可变对象，key 内容寻址：`raw/binance/spot/rest/responses/<sha256>/<data_type>/<symbol>/<page_identity_sha256>.json`；行内记 `object_key` / `object_uri` / `object_sha256` / `object_size_bytes` |
| `source_id` | `binance.public.spot.rest@1.0.0` |
| `revision_id` | `rev1-<sha256>`，由独立 REST 身份规则（§11）从 `{规则 ID + 版本 + 哈希, observation_key, source_identity, payload_hash}` 派生 |
| 重复 | 同一页身份 + 同字节 → 幂等：不产生新 revision、不分配新 `arrival_seq`（即使来自另一逻辑采集尝试） |
| 冲突 | 同一页身份 + 不同字节 → 同一 `observation_key` 的第二条 revision + 质量事件 `rest_response_competing_payload`。response revision **从不参与 PIT 选择**，因此这不是数据集级失败 |
| 覆盖面 | 每个完整收到的 200 响应页都是 response revision：含空页、全部元素越出目标窗口的页、envelope 违约的页 |
| 额外列 | 规范页身份分量（`data_type`、`symbol`、`request_path`、`request_query`、`declared_time_unit`、`page_limit`）、`page_identity_sha256`、首次交付它的 `collection_request_id` / `page_index`、`source_uri`、`requested_at`、`retrieved_at`、HTTP 状态、`source_metadata`（凭据形状的键被拒）、decoder 绑定、`decode_outcome`（accepted / rejected）+ 拒绝码、`element_count`、页面 answered 区间（可空，§7） |

与 D2 的有意差异：D2 把归档 revision 的准入绑定到 D1 严格解析成功（已验收，本 ADR 不改）；REST 的 response revision
只绑定字节与页身份，元素解码是下一层——解码失败使元素 payload 不获得 `knowledge_time`（ADR-0023 失败语义），
但不抹掉已被本机知道的响应字节。

### 3. Raw element revision：一条 aggTrade / 一根已结束 1m kline

**3.1 observation_key 与归档逐字符相同**：aggTrade `binance:spot:agg_trade:<symbol>:<aggTradeId>`；
1m kline `binance:spot:kline:<symbol>:1m:<interval_start epoch 微秒>`。REST 身份规则重复写出同一文法，
由跨模块一致性测试证明两模块对同一输入产出同一键。

**3.2 source identity 是通道级**：`binance.public.spot.rest@1.0.0`（不含响应 revision id）。分页重叠、重取、
窗口重叠导致同一元素多次交付且内容相同 → ADR-0023 §4 的"重复"，幂等；两个响应对同一元素给出不同内容 → 第二条
revision → competing heads → fail closed。交付它的首个响应以 `response_revision_id` 列记为 provenance，不是身份。
归档侧 `row_source_identity(archive_revision_id)` 刻意不对称：归档的替换单位是文件，ADR-0022 §4 要求保留被替换文件
及其解析结果；REST 每次响应只是对同一数据库的重新读取。

**3.3 payload hash**：与 D2 同一规范文档（`binance.spot.agg_trade/1`、`binance.spot.kline_1m/1`：原生字段 + 标的身份
+ 声明时间单位，十进制定点文本），由 REST 身份规则重写并以一致性测试对照；不含到达时间、页号、`element_index`、
`arrival_seq`。它**不是** D-33 的比较依据（声明单位不同，§4.3）。

**3.4 其余**：`event_time` / `interval_start` + `interval_end` 由声明单位换算，禁止逐值猜数量级；`element_index` 是
该元素在响应数组中的 0 基位置（审计与 `arrival_seq` 用，不入 payload hash）。

### 4. D-33：通道等价 precedence `binance.spot.delivery-channel@1.0.0`（Codex 已选 A）

**4.1 性质**：这是**项目定义、版本化**的政策——"规范市场内容相等" + "ADR-0022 指定官方归档为权威 backfill 通道"。
它**不是** Binance 声明的修订先后，不声称哪个通道先发布或后更正，也不得在任何文档、证据或列中被表述为来源事实。
它是对 ADR-0023 §4「来源可证明的优先级」的一次明确、有界的项目放宽，因此必须以 policy 版本与证据文件绑定。

**4.2 适用对象**：同一 `observation_key` 下，一条归档元素 revision（`raw.binance_spot_agg_trades` /
`raw.binance_spot_klines_1m`）与一条 REST 元素 revision（`raw.binance_spot_rest_agg_trades` /
`raw.binance_spot_rest_klines_1m`）组成的对。归档替换之间、REST 内部的关系不由本 policy 决定。

**4.3 版本化规范内容投影**（投影定义是 policy 文档的一部分，变化即新 policy 版本）：

| 投影 | 字段（全部必填；任一缺失 / 为空 → 不可比较） |
|---|---|
| `binance.spot.agg_trade.market-content/1` | `symbol`、`agg_trade_id`、`price`、`quantity`、`first_trade_id`、`last_trade_id`、`event_time_us`、`is_buyer_maker`、`is_best_match` |
| `binance.spot.kline_1m.market-content/1` | `symbol`、`interval`（恒 `1m`）、`interval_start_us`、`interval_end_us`、`open`、`high`、`low`、`close`、`volume`、`quote_asset_volume`、`number_of_trades`、`taker_buy_base_asset_volume`、`taker_buy_quote_asset_volume`、`ignore` |

规范化：

- **整数**：十进制、无前导零、int64 范围内。
- **十进制**：精确值按 `decimal(38, 18)` 渲染为恰好 18 位小数的定点文本，无指数、无符号；超出该精度 / 标度即不可比较
  （不舍入）。`0.1` 与 `0.10` 相等，`0.1` 与 `0.1000000000000000001` 不可比较。
- **布尔**：JSON `true` / `false`（归档 CSV 与 REST JSON 各自先经严格解析为布尔）。
- **时间**：精确 UTC 纪元**微秒整数** = 原始值 × 声明单位因子（毫秒 ×1000、微秒 ×1），**不截断、不舍入**。
  kline 的 `interval_end_us = (close_time_raw + 1 tick) × 因子`：两侧 decoder 都已要求 `close = open + 1m − 1 tick`，
  用排他终点使毫秒（`…59999`）与微秒（`…59999999`）渲染的同一根 K 线一致。
- **文本**：`symbol` 原样；`ignore` 按来源文本逐字节比较（官方称"未使用"，保守地纳入：不同即不等）。
- **排除**：`time_unit`、`timestamp_raw` / `open_time_raw` / `close_time_raw`（被规范时间取代）、全部身份 / lineage /
  两轴时间 / `arrival_seq` / 行号 / `element_index` / decoder 与 parser 绑定。

**相等**：两侧投影都完整且在定义域内，且其规范 JSON（排序键、紧凑分隔、UTF-8）**逐字节相同**（等价于 `projection_sha256` 相同）。

**4.4 结果**：

- 相等 → 追加一条 evidence-only 边：`PrecedenceEvidence(revision_id = 归档 revision, superseded_revision_id = REST revision)`，
  写入 `raw.binance_spot_precedence_evidence`；不写进任何行的 `supersedes`，不改写任何已提交行。
- 不等、缺字段、值不在定义域、任一侧行不可读或其 payload hash 由存储字段重算不符 → **不产生边**；保留 competing heads，
  PIT fail closed，写质量事件（`channel_projection_mismatch` / `channel_projection_incomparable`）。
  payload hash 重算不符属存储完整性破坏：该 reconcile 以 `CatalogIntegrityError` 中止。
- 无对侧 → 无事可做（不是缺陷，不写任何记录）。

**4.5 证据项**（非空，逐项可复核）：policy 陈述（"canonical market content equal; archive is the ADR-0022 authoritative
backfill channel; not a source-declared revision order"）、投影 kind、`projection_sha256`、两侧 `payload_hash`、
两侧元素表与所固定的 snapshot id。

**4.6 `knowledge_time` 与幂等**：

- 边的 `knowledge_time` = 本机完成两侧取回、严格比较并形成政策判断之后、提交该事实之前打下的时刻（与 revision 行
  的 `knowledge_time` 同一做法），`>=` 两侧 revision 的 `knowledge_time`；**绝不回填**到任一侧的时间。
- 身份 `edge_id` 不含时间；同一比较事实的**首次成功提交**即权威记录。重放、重跑 reconcile、崩溃恢复都先按 `edge_id`
  读回已提交记录并复用，**不得**以新的墙钟再写一行；因此同一 `edge_id` 不会有两个不同的 `knowledge_time`。
  若表中出现同一 `edge_id` 的两行，即 `CatalogIntegrityError`，fail closed。
- 证据表单 writer（ADR-0023 §7）；每批先查已提交 `edge_id`、只提交缺失者，提交后复核无重复。

**4.7 两种到达顺序与 cutoff**（`K_A` / `K_R` = 归档 / REST revision 的 `knowledge_time`，`K_E` = 边的 `knowledge_time`，
`K_E >= max(K_A, K_R)`）：

| `knowledge_cutoff` | 结果（两种到达顺序相同形状） |
|---|---|
| `< min(K_A, K_R)` | 两者都不可见 → 该 key 不存在 |
| `[min, max)` | 只有先到者可见 → 选中先到者 |
| `[max(K_A, K_R), K_E)` | 两个 head 都可见、边尚不可知 → **conflict，fail closed**（诚实的冲突窗口） |
| `>= K_E` | 边生效 → 唯一选中归档 revision |

最终边集合与到达顺序无关；不同到达历史下的旧 cutoff 结果不必、也不应声称相同。
收敛**不依赖**"以后恰好又有一次同类到达"：边由 D3E 的 reconciler 产生（§9），它在任一通道 ingest 之后对受影响分区运行，
也可独立重跑；进度以证据表本身为 checkpoint（幂等）。

**4.8 已知后果（诚实边界）**：REST 1.0.0 以毫秒交付；2025-01-01 起的归档为微秒。若归档 aggTrade 的微秒时间带非零亚毫秒位，
两侧投影必然不等 → 无边 → fail closed。这是正确结果（毫秒渲染不能证明等于微秒值）。若要缓解，只能以新的页身份 /
decoder 版本请求官方已文档化的微秒单位（证据 R12），并先做只读 smoke——列为开放义务，本 ADR 不决定。kline 不受此影响。

**4.9 被放弃的方案（记录）**：B 不裁决（等于禁止 REST 补尾）；C 改 PIT 语义让内容相同的多个 head 不冲突（触碰已发布契约）；
D REST 元素不进 Raw / 事后删除（违反 append-only 与 H6）；E 键含通道（同一笔成交变成两条观察）；
把反向边塞进 REST 行的内嵌证据列（与冻结结构的隐含方向冲突，§1）。

**4.10 Canonical**：批次 E 的 Canonical precedence 只从这些已持久化的 Raw 边派生（ADR-0023 §4），normalizer 不重新发明。

### 5. 两种身份：逻辑采集尝试 vs 规范页身份

- **`CollectionRequest.request_id` = 一次逻辑采集尝试**的稳定身份，由调用方选定，重试 / 重启不变；它**不**由 HTTP 查询派生。
  同一 `request_id` 必须始终对应同一请求内容（source、data_type、symbols、目标窗口）；不同内容复用同一 id → fail closed。
- **规范页身份** = `{method: GET, origin, path, query（按名称排序的白名单参数对）, declared_time_unit, rule: REST 身份规则 id@version}`
  的规范 JSON，其 SHA-256 为 `page_identity_sha256`。不同采集尝试可以产生相同页身份。
- **有意刷新**使用新的 `request_id`；若同一页身份返回不同字节，它们是同一 response `observation_key` 的不同 revision（§2）。
- **重放**：同一 `request_id` 再次 `collect`（同一实例或重建实例）必须从已持久化的 checkpoint（§9）返回首次已承诺的
  同一组 `ObjectRef` 与缺口（或同一失败），**不重新访问 Binance**；只有尚未承诺的页才会首次联网。
- 参数白名单（多余参数拒绝构造）：`aggTrades` = `symbol`、`fromId` | `startTime`（恰好其一）、`limit`；
  `klines` = `symbol`、`interval=1m`、`startTime`、`limit`。`endTime`、`timeZone` 不使用（证据 N6 / R9）。
  **`fromId` 与 `startTime` 都缺省的请求（官方"最新"模式，R4 / R9）在构造期不可构造。**
- 值编码：整数十进制无前导零；时间为声明单位（毫秒）整数；symbol 原样大写；`limit = 1000`（规则常量）。
- 请求头固定：`User-Agent`（设置）；1.0.0 不发送 `X-MBX-TIME-UNIT`（证据 N8），`declared_time_unit = millisecond`。
- `CollectorAdapter` 必需协议**不变**；本节只规定 REST 实现如何满足它已有的重放义务。

### 6. collection target window 与 page validity envelope

两个概念严格分开：

- **collection target window** `[t0, t1)` = `CollectionRequest.coverage_start / coverage_end`：**只**用于首页 `startTime`、
  停止条件与覆盖账（§7）。它**从不**用于拒绝页面或元素。
- **page validity envelope**：只由该页的实际查询（`startTime` 或 `fromId`、`limit`）、官方排序（R11）、字段形状、
  声明单位与有界响应规则决定。严格 decoder 对 envelope **零容差**；违约 → 整页拒绝（§8 / §9 的失败语义）。

| 检查 | aggTrades | klines（1m） |
|---|---|---|
| HTTP / 字节 | 200、不跟随 3xx、正文 ≤ 上限（§12）、长度一致 | 同左 |
| JSON 形状 | 顶层数组，`0 ≤ len ≤ limit`；每项恰好键 `a p q f l T m M`、无重复键；`a f l T` 为 JSON 整数（int64、非负），`p q` 为十进制文本（`decimal(38,18)` 可表示），`m M` 为布尔 | 顶层数组，`0 ≤ len ≤ limit`；每项恰好 12 元；开 / 收时间与成交笔数为整数，价量为十进制文本，第 12 项为十进制文本 |
| 行规则（同 D1） | `p > 0`、`q > 0`、`f ≤ l` | 价格 > 0；`low ≤ min(open, close) ≤ max(open, close) ≤ high`；taker ≤ volume；开盘分钟对齐；`close = open + 1m − 1 tick` |
| 查询下界 | `startTime` 页：每个 `T ≥ startTime`；`fromId` 页：每个 `a ≥ fromId`（`a > fromId` 允许，记 ID 缺口质量事件，证据 N2） | 每个 `open ≥ startTime` |
| 页内顺序 | `a` 严格递增、`T` 不减、`f` > 上一条 `l` | `open` 严格递增 |
| 跨页连续（第 k ≥ 1 页） | 首条 `T ≥` 上一页末条 `T`；首条 `f >` 上一页末条 `l` | 由 `startTime = 上一页末根已结束 open + 1m` 保证；首根 `open ≥ startTime` |
| 物理上界（结构性单位检查） | 每个 `T ≤ retrieved_at` | 每个 `open ≤ retrieved_at`；`interval_end > retrieved_at` 的未结束 K 线至多一根且必须是末项 |
| 目标窗口 `t1` | **不检查**：`T ≥ t1` 的合法元素照常成为 element revision | **不检查**：`open ≥ t1` 的已结束 K 线照常成为 element revision |

单位错误由"查询下界 + `retrieved_at` 上界"结构性捕获（微秒值误读为毫秒落在遥远未来；反之早于下界），不逐值猜数量级。
本机时钟若落后于来源、使真实元素显得晚于 `retrieved_at`，页面被拒（fail closed、可见）；不引入容差参数（引入即新 decoder 版本）。

**分页**：

- aggTrades：首页 `startTime = t0`；续页 `fromId = 上一页末条 a + 1`。
- klines：首页 `startTime = t0`（`t0`、`t1` 必须分钟对齐，否则构造期拒绝）；续页 `startTime = 上一页末根已结束 open + 60_000`。
- 每页通过 envelope 后判定终止：**reached_target_end**（aggTrades 出现 `T ≥ t1`；klines answered 上界 `≥ t1` 或出现 `open ≥ t1`）；
  **unclosed_tail**（klines 末项未结束）；**short_page**（`0 < len < limit`）；**empty_page**（`len = 0`）；否则续页。
  页数预算见 §12，达到即失败（§7），不终止为缺口。
- 未结束 K 线：不写成 element，只留在 response 字节中，写质量事件 `rest_unclosed_kline_skipped`（证据 N5）。

### 7. CollectionResult 的诚实构造

**页面 answered 区间**（"来源对该查询的已交付回答所覆盖的时间"，不是市场完整性声明）：

- aggTrades（tick = 声明单位的 1 个单位）：下界——首页为 `t0`，续页为上一页末条 `T`；上界——满页为本页末条 `T`（该 tick
  可能续到下一页，故排他），短页为末条 `T + 1 tick`，续页空页为上一页末条 `T + 1 tick`（它补全了边界 tick），首页空页为空区间。
- klines：下界 = 本页 `startTime`；上界 = 末根已结束 K 线 `open + 1m`；无已结束 K 线则为空区间。
- 区间内的缺失（kline 缺分钟、aggTrade ID 缺口）是**质量事实**，不是覆盖缺口——与"日归档缺若干分钟仍覆盖整日"同一语义。

**成功结果**（每个 symbol 链都已终止）：

| `CollectionResult` 硬不变量 | REST 如何满足 |
|---|---|
| `objects` 按 `ref.key` 排序且 key 唯一 | 每个对象是一页的正文；内容寻址 key 含 `page_identity_sha256`，同一结果内不重复 |
| 对象 / 缺口区间落在请求窗口内 | 对象覆盖 = 页面 answered 区间 ∩ `[t0, t1)`；裁剪后为空的页（首页空页、全 tick 突发页）不是 `CollectedObject`，但仍是 page checkpoint 与 response revision |
| 每个 symbol：对象 ∪ 缺口恰好等于 `[t0, t1)`，缺口互不重叠且不与对象重叠 | 各页 answered 区间首尾相接，从 `t0` 连续到链的上界 `U`；`U ≥ t1` 时无缺口；否则恰有一个缺口 `[U, t1)` |
| `CoverageGap.reason` 只有 `SOURCE_ABSENT` | 只用于 short_page / empty_page / unclosed_tail 之后的尾部：来源在 `retrieved_at` 未就该查询交付更多数据（"尚未发布"）|
| `detail` 非空 | 由 checkpoint 确定性生成：`rest <data_type> <symbol> page <k>: GET <path>?<query> returned <n> of <limit> at <retrieved_at>; nothing at or after <U> was served; body sha256 <hex>; records the source's answer only, not an absence of market activity` |
| `source_sha256` | 空（Binance 不为 REST 声明校验和）；`ref.sha256` 为本机计算 |
| 重放得同一对象身份与缺口 | 结果从 checkpoint 确定性派生（§9） |

**不产生成功结果**（`collect` 抛 `CollectionFailed`，已发布对象与 checkpoint 保留、可恢复）：本次页数预算用尽、429 / 418、
`Retry-After` 超上限或缺失、5xx、传输失败、正文截断 / 超上限、无效 JSON 或 envelope 违约、4xx。
这些**绝不**记为 `SOURCE_ABSENT`，也不补缺口凑满区间。其中：

- 暂时性失败（预算、429 / 418、5xx、传输、截断）：该页没有 checkpoint，同一 `request_id` 重试从未承诺的页继续；
- 完整收到但被 decoder 拒绝的页（无效 JSON、envelope 违约）：该页 checkpoint 记为 rejected，它是 response revision
  （§8）；同一 `request_id` 重放**确定性地**得到同一失败（不重新联网）；重新采集须用新的 `request_id`。

`CollectionResult`、`CoverageGap`、`GapReason` 的现有定义足以表达上述全部成功与失败，不新增公共契约。

### 8. 双时间

**response revision**：

| 字段 | 取值 |
|---|---|
| `event_time` / `event_end_time` | `requested_at`（本机把请求交给传输层的时刻）/ `ingest_time`：response 观察 = 这次 HTTP 交换 `[requested_at, ingest_time)`，来源在其中某刻作答；`ingest_time <= requested_at`（墙钟倒退）即失败 |
| `source_time` | 空；`Date` / `Last-Modified` / `ETag` 只原样保存在 `source_metadata`（证据 N10） |
| `ingest_time` | 实体正文**最后一个字节**收到的本机 UTC 时刻（唯一定义；= checkpoint 与 `CollectedObject` 的 `retrieved_at`） |
| `available_time` | `binance.spot.rest-publication@1.0.0`：`= ingest_time` + 证据缺口 token（证据 N1） |
| `knowledge_time` | 必填、`>= ingest_time`：store 完成大小上限、SHA-256、对象发布与元数据校验（200、无重定向、无凭据形状键、长度一致），并对该页运行一次严格 decoder（**无论成败**）之后打下的时刻；decoder 失败不影响它存在 |
| `declared_latency` | 0 |

**element revision**：`event_time` = aggTrade `T` / kline `interval_start`（`event_end_time = interval_start + 1m`）；
`source_time` 空；`ingest_time` 与 `knowledge_time` **继承**所属 response revision（该 `knowledge_time` 已在严格解码之后，
不早于元素自身的最小校验；继承使崩溃恢复可确定性重建）；`available_time = ingest_time` + 证据缺口；
由 envelope 保证 `available_time >=` 观察可被观察的时刻（aggTrade `T <= retrieved_at`；kline 已结束）。

**precedence edge**：见 §4.6。

### 9. 持久化与恢复顺序（不可变对象，无可变 sidecar）

REST 的来源不可重放（同一查询以后可以合法地返回不同字节），所以首次回答必须被持久化；持久化物全部是**写一次**的不可变对象
（`StorageAdapter.publish`：同内容幂等、异内容 `ObjectConflict`），不存在原地更新的 journal：

| 步 | 动作 | 承诺点 |
|---|---|---|
| c1 | 第 k 页正文 staging → 大小 / 哈希 → 发布内容寻址对象 | — |
| c2 | 严格解码该页 → 发布 **page checkpoint**：键 `raw/binance/spot/rest/collections/<sha256(request_id)>/<symbol>/<k:08d>.page.json`，内容 = 页身份、`ObjectRef`、`requested_at` / `retrieved_at`、HTTP 元数据、decoder 绑定与解码摘要（outcome、元素数、answered 区间、终止原因或续页查询） | 第 k 页 |
| c3 | 全部链终止（或一页被拒）→ 由 checkpoint 确定性派生结果并发布 **collection checkpoint** `…/<sha256(request_id)>/collection.json`（含完整请求） | 该逻辑尝试 |
| s1 | REST store 按 (symbol, page) 顺序：每页一行 response revision（首次提交即分配 `arrival_seq` block） | 每页 |
| s2 | 已接受页的元素 microbatch（确定性：不可变正文 + decoder + 已提交 response 行的 block 与时间） | 每批 |
| r1 | reconciler：对受影响的 `(symbol, day)` 分区比较两通道同键 revision，追加缺失的边（§4） | 每批边 |

checkpoint 格式是 REST collector 的版本化内部规格（D3D），不是公共契约。同一 `request_id` 并发时，先发布 checkpoint 者胜，
后者得 `ObjectConflict` 而中止，重试读取胜者。

三个崩溃点：

1. **c1 之后、c2 之前**：正文对象无人引用（orphan，只由显式 maintenance 清理）；重试时该页未承诺 → 首次联网取回，
   字节不同即另一内容寻址对象；此前没有任何承诺被改变。
2. **c2 之后、c3 之前**（链中途、预算用尽或暂时性失败）：重试读回 0..k 页 checkpoint（校验形状、请求 / symbol / 页号、
   对象完整、重新解码复现摘要），**不联网**，从第 k+1 页续取。
3. **c3 之后、s1 / s2 / r1 未完成**：重放 `collect` 直接返回 collection checkpoint 的结果；重跑 store 时已提交 response 行
   按 `revision_id` 读回 block 与时间，缺失元素批从不可变正文重建，已提交批次由 catalog 按 batch 指纹幂等；重跑 reconciler
   先读回已提交 `edge_id`，只补缺失边，已提交边的 `knowledge_time` 原样复用。

### 10. 新增标识符（提案，接受后并入 `03-data.md` §7.3）

| 类别 | 标识符 | 内容 |
|---|---|---|
| Source | `binance.public.spot.rest@1.0.0` | market-data-only base 的 `GET /api/v3/aggTrades` 与 `GET /api/v3/klines`（`1m`），security type NONE |
| Decoder（parser role） | `binance.spot.rest.decoder@1.0.0` | §6 的 page envelope、声明单位（毫秒）、未结束 K 线规则、正文上限；变化 = 新版本 |
| Availability policy | `binance.spot.rest-publication@1.0.0` | §8：全部主体 `available_time = ingest_time` + 证据缺口 |
| Precedence policy | `binance.spot.rest-revision@1.0.0` | REST 通道内部：同内容幂等；不同内容无证据 → competing heads；从不产生边 |
| Precedence policy | `binance.spot.delivery-channel@1.0.0` | §4：投影相等 → evidence-only 边 归档 → REST；否则无边。`policy_hash` = 完整 policy 文档（含 §4.3 投影、§4.4 结果、§4.5 证据项、never-evidence 清单、证据文件路径）规范 JSON 的 SHA-256，由实现派生、不手写 |
| 身份规则 | `hlens.binance.spot.rest-revision-identity@1.0.0` | §5 页身份、§2 / §3 键与 payload hash、`edge_id`、§11 `arrival_seq` 区间；与归档规则物理隔离 |

### 11. `arrival_seq` 与三个结构性陷阱

1. **身份规则哈希是全局的**。`revision_id` 的派生文档含 `rule_hash = IDENTITY_HASH`（整份 `IDENTITY_SPEC` 的哈希）。
   把 REST 并入 D2 规则会改变**所有**归档 `revision_id`。→ REST 用独立身份规则；`infrastructure/revision/identity.py`、
   `IDENTITY_SPEC`、`IDENTITY_HASH`、已提交归档 revision id 与序号**全部不变**（D3B 回归断言哈希等于 D2 验收值）。
2. **`PointInTimeSpec` 同一字段内 `policy_id` 唯一**。→ REST 用独立 policy_id（`binance.spot.rest-publication`、
   `binance.spot.rest-revision`、`binance.spot.delivery-channel`、`binance.spot.rest.decoder`），归档 1.0.0 保持不变。
3. **`arrival_seq` 只需在同一被聚合的 `RevisionGraph` 内唯一**（该契约的要求），不是所有系统表共享的全局序列，也不表示任何顺序语义。
   D-33 会把同一 `observation_key` 的归档与 REST revision 聚合进一个 graph，因此按区间划分 int64：归档沿用 D2 的
   `[0, 2**62)`（D2 分配器不改）；REST block 基址自 `2**62` 起、步长 `2**32`，response 行取 block 基址，元素取
   `base + element_index + 1`，锚表为 `raw.binance_spot_rest_responses`。**D3E 在组装单个跨通道 graph 时做 range guard**：
   每条 revision 的序号必须落在其通道区间内，越界或碰撞即 fail closed（D2 归档要到达 `2**62` 需约 `2**30` 个归档文件，
   不可达；万一出现也只会 fail closed，不会静默混号）。

### 12. 运维设置（非 Constitution / Validation Profile 阈值；接受门并入 `03-data.md` §6.2，D3D 实现）

复用（不新增）：`HLENS_HTTP_CONNECT_TIMEOUT_SECONDS`、`HLENS_HTTP_READ_TIMEOUT_SECONDS`、`HLENS_HTTP_MAX_RETRIES`
（每页 5xx / 传输失败 / 429 的尝试上限 `1 + n`）、`HLENS_HTTP_USER_AGENT`、`HLENS_BINANCE_MARKET_DATA_BASE_URL`
（必须是精确 `https://host[:port]` origin，拒绝路径 / query / 凭据，不静默改写，同 D0-R2）。

新增：

| 环境变量 | 类型 | 默认 | 校验 | 语义 |
|---|---|---|---|---|
| `HLENS_BINANCE_REST_MAX_PAGES_PER_COLLECT` | int | `200` | `1 ≤ n ≤ 5000` | 单次 `collect` 最多**新取**的页数（checkpoint 重放不计）；用尽 → `CollectionFailed`，尝试保持可续 |
| `HLENS_BINANCE_REST_MIN_REQUEST_INTERVAL_MS` | int | `250` | `50 ≤ n ≤ 60000` | 相邻两次 HTTP 请求（含重试）的最小间隔 |
| `HLENS_BINANCE_REST_MAX_RETRY_AFTER_SECONDS` | int | `60` | `1 ≤ n ≤ 3600` | 429 的 `Retry-After`（秒，R15）超过即 fail closed，不等待；缺失或非法视同超过；418 一律立即终止 |
| `HLENS_BINANCE_REST_MAX_RESPONSE_BYTES` | int | `8388608` | `65536 ≤ n ≤ 67108864` | 去除 content coding 后的正文上限（边解码边计数，防解压炸弹）；超过 → 失败，不发布 |

`limit = 1000` 是页身份规则常量，不是设置。默认值是保守的运维选择，不来自官方配额（官方配额本切片不依赖，R2）。

### 13. 迁移与兼容

- additive：四张新表、六个新标识符；八张冻结表的 `TableDefinition`（Schema、字段 ID、分区、properties、定义哈希）与已提交数据
  逐字节不变；`core/` 契约、`schemas/` 导出（74 份）、`CONTRACT_SCHEMA_VERSION`、`CollectorAdapter` Protocol 全部不变。
- `PHASE1_TABLES` 由 8 张扩为 12 张（D3B，断言前 8 张定义哈希不变）。
- PIT（批次 F）的 `snapshot_bindings` 在使用 REST 数据时必须同时绑定四张新表中被读取者，含 `raw.binance_spot_precedence_evidence`；
  证据表缺失 / snapshot 无法解析 → fail closed（§7.5 既有规则）。
- `03-data.md` §7.1 / §7.3 / §6.2 是冻结正文：Proposed 期间不改；接受门把 §1 / §10 / §12 并入并删除 §7.6 的提案标记。

## 明确不做

- 不写任何 HTTP / 解码 / revision / reconcile 代码，不新增依赖，不改 settings 代码，不改八张表定义与 Schema 导出。
- 不修改 `CollectorAdapter` 必需协议，不新增跨 Plane 公共契约。
- 不开始 D4（WebSocket）、E（Canonical / 质量报告）、F（PIT / manifest）。
- 不定义质量事件的完整分类法（属批次 E），只提出 REST 相关事件类型名。
- 不声称 REST 与归档内容一致、不声称缺口已补齐、不声称 ID 连续、不声称 D-33 边是来源事实。
- 不访问账户 / 订单 / 签名端点，不引入任何凭据。

## 备选方案（表拓扑层面）

| 方案 | 优点 | 缺点 | 为何未选 |
|---|---|---|---|
| **A（本 ADR）** 三张 REST 表 + 独立证据表 | 三跳 lineage 天然成立；归档路径零改动；跨通道边两端显式、与到达顺序无关 | 表数 8 → 12；Canonical 层要合并两套 Raw 元素表 | — |
| A′ 三张 REST 表，跨通道边写入 REST 行的内嵌证据列 | 少一张表 | 内嵌结构隐含"新端 = 承载行"，REST 后到时只能反向塞入，双重语义；archive 后到时需改 D2 store | D3A-R1 否决 |
| B 复用归档元素表，`archive_revision_id` 改可空 | 表数不变 | 冻结表语义变化（major）；D1 / D2 证据需重跑 | 破坏冻结契约 |
| C REST 只写 Canonical | 最少的表 | 三跳 lineage 不成立；原始响应无处可放 | 违反 ADR-0022 / 0023 |
| D 把响应伪装成归档 | 复用 D2 | 伪造来源 | 不可审计 |
| E 通用 `raw.source_payloads` 取代归档表 | 长期整齐 | 重写冻结表 + 迁移 | 代价远超收益 |

## 契约、Schema 与迁移影响

- 已发布契约字段**不变**：`SelectedRevisionLineage`、`RevisionRecord`、`AvailabilityDecision`、`PrecedenceEvidence`、
  `RevisionGraph`（evidence-only 边本就合法）、`PointInTimeSpec`、`CollectionRequest` / `CollectionResult` / `CollectedObject` /
  `CoverageGap`、`CollectorAdapter`。REST 的 `source_uri` 带 query，已被 `validate_source_uri` 允许。
- 不新增 `Kind`、`GapReason`、公共 DTO、Protocol 或 Schema 导出；D3B～D3E 需要的内部持久化类型只在 `infrastructure/`。
- 新表随表定义版本化（10-migration.md §3）；分区演进仍需等价测试。

## 失败与恢复语义

- 暂时性失败（预算、429 / 418、5xx、传输、截断、正文超上限、4xx）：`CollectionFailed`；该页不承诺、不产生 response revision、
  不产生缺口；同一 `request_id` 重试从未承诺的页继续。
- 完整收到但 decoder 拒绝（无效 JSON、envelope 违约）：该页 checkpoint 记 rejected；store 为它写 response revision（带
  `knowledge_time`）、不写元素、写质量事件；`collect` 抛 `CollectionFailed`，同一 `request_id` 重放确定性地得到同一失败。
- 三个崩溃点：§9。
- 跨通道：投影不等 / 不可比较 → 无边、competing heads；payload hash 重算不符 → `CatalogIntegrityError`；证据表内同一
  `edge_id` 重复 → `CatalogIntegrityError`；跨通道 graph 的 `arrival_seq` 越区间或碰撞 → fail closed。

## 安全边界

- **端点 allowlist 是结构化检查**：scheme = https、authority 等于配置的 market-data-only origin、path ∈ 冻结集合（精确相等）、
  query 参数名 ∈ 白名单且值符合规范编码、无 fragment、无凭据；**不跟随重定向**。D0 的 `_assert_url_allowed` 拒绝一切 query，
  不能复用——REST 需要自己的、更严格的检查。
- 429：按 `Retry-After` 有界等待（§12），上限外 fail closed；418 立即终止；**禁止无限重试**；4xx（非 429 / 418）视为本机构造错误，不重试。
- 无 secret、无 API key、无签名、无账户 / 订单端点；响应元数据中凭据形状的键被拒绝。
- 来自交易所的 JSON 是不可信输入：严格形状校验、有界正文、不执行其中任何内容。

## 验收矩阵（D3B ~ D3E 实施批次验证）

| # | 场景 | 期望 | 批次 |
|---|---|---|---|
| 1 | 八张冻结表的定义哈希与分区；`IDENTITY_HASH` | 与 C3 / D2 逐字节相同（回归断言） | D3B |
| 2 | 同一页身份、同一字节再次交付（含另一逻辑尝试） | 无新 response revision、无新 `arrival_seq`、无新 snapshot | D3E |
| 3 | 同一页身份、不同字节 | 第二条 response revision + 质量事件；数据集构建不因此失败 | D3E |
| 4 | 同一元素由两页重复交付（内容相同） | 幂等，一条 revision | D3E |
| 5 | 两页对同一元素给出不同内容 | 两条 revision，competing heads，PIT fail closed，质量事件 | D3E |
| 6 | 归档与 REST 同一观察、投影相等：REST 先到 / 归档先到 × §4.7 四段 cutoff | 两种顺序都只在证据表追加一条 归档 → REST 边、零改写任何已提交行；各 cutoff 段结果与 §4.7 一致；最终边集合相同 | D3E |
| 7 | 投影不等 / 缺字段 / 超定义域 / 无对侧 / payload 重算不符 | 无边（后者中止）；competing heads 保留；质量事件 | D3B（纯函数）/ D3E |
| 8 | 重复比较：reconciler 重跑、store 重跑、元素重复交付 | 同一 `edge_id` 只有一行，`knowledge_time` 为首次提交值；无新边 | D3E |
| 9 | 缺省 `fromId` 与 `startTime` 的页身份 | 构造期拒绝 | D3B |
| 10 | 分页推进与终止 | `fromId = last + 1` / `startTime = last closed open + 60s`；四种终止原因；页数预算；无死循环 | D3D |
| 11 | 目标窗口 overshoot、空页、短页 | 越出目标窗口的合法元素照常成为 element revision；覆盖只裁剪到 `[t0, t1)`；尾部唯一 `SOURCE_ABSENT` 缺口的 detail 只陈述确切查询在 `retrieved_at` 的回答；首页空页不是 `CollectedObject` 但是 response revision | D3C / D3D / D3E |
| 12 | 未结束 1m kline | 不写 element、质量事件、字节保留、answered 上界止于其 open、链以 unclosed_tail 终止 | D3C / D3D |
| 13 | page envelope 违约（startTime 左越界、fromId 违约、逆序、跨页不连续、早于下界或晚于 `retrieved_at` 的单位错、JSON / 字段形状、超出 limit） | 整页拒绝：response revision 照常写入（带 `knowledge_time`）、零元素、质量事件、`collect` 失败且重放同一失败；**仅超出目标窗口的合法元素绝不触发拒绝** | D3C / D3D / D3E |
| 14 | 同一 `CollectionRequest` 重放（同一实例、重建实例、来源夹具第二次返回不同字节） | 返回首次已承诺的同一 `ObjectRef` 与缺口，零网络请求；同 `request_id` 不同请求内容 → fail closed；`CollectorAdapter` contract suite 通过 | D3D |
| 15 | 三个崩溃点（§9） | 已承诺页不再联网；结果、revision、边与一次跑完相同 | D3D / D3E |
| 16 | 预算用尽 / 429 / 418 / 5xx / 传输失败 / 截断 / `Retry-After` 超上限 | `CollectionFailed`，绝不产生 `SOURCE_ABSENT` 或成功结果；有界、遵守 `Retry-After`、418 立即终止 | D3D |
| 17 | 恶意 / 越界 URL（他站 origin、路径遍历、额外参数、重定向、凭据形状参数、带路径的 base） | 全部拒绝，且不发起请求 | D3D |
| 18 | 静态检查 | 无账户 / 订单 / 签名端点、API key 读取或 market-data 之外的 origin | D3D |
| 19 | 设置 | 四个新字段的默认值与校验；复用字段不重复定义 | D3D |
| 20 | 双时间 | response：`ingest_time` = 末字节、`[requested_at, ingest_time)`、`knowledge_time >= ingest_time` 且 decoder 失败时照常存在；元素继承；全部 `available_time = ingest_time` + 证据缺口 | D3B（policy）/ D3E |
| 21 | `arrival_seq` | REST 落在 `[2**62, 2**63)`；跨通道 graph 的 range guard 拒绝越界 / 碰撞；D2 分配器与 `identity.py` 未改 | D3B / D3E |
| 22 | 三跳 lineage | `canonical.* → raw.binance_spot_rest_* → raw.binance_spot_rest_responses` 可从 manifest 解析 | E / F |

## 后果

- 正面：REST 数据获得与归档同等强度的可审计性；跨通道边两端显式、可复核、与到达顺序无关；归档路径零改动；PIT 的
  fail-closed 语义不被削弱。
- 负面 / 代价：表从 8 张增到 12 张；Canonical 层要合并两套 Raw 元素表；reconciler 需按分区扫描两通道（成本有界）；
  REST 需要不可变 checkpoint；D-33 是对 ADR-0023 §4 的有界项目放宽；2025 年起的 aggTrade 在毫秒 REST 下多半无法建立边（§4.8）。
- 对复现性：页身份、响应字节、decoder / policy / 投影版本、边的固定 snapshot 全部可审计，逐页、逐边可复现。

## 开放义务

- 本 ADR 由 Codex 再次复核；接受门把 §1 / §10 / §12 并入 `03-data.md` §7.1 / §7.3 / §6.2，D-33 随之生效。
- 实施批次按证据文件 §3 重新核对官方资料并做只读 smoke；实测与证据冲突时先更新证据再发新版本。
- 是否改为请求微秒单位（§4.8）：需新页身份 / decoder 版本、smoke 证据与 Codex 批准。
- 质量事件类型名（`rest_window_gap`、`rest_unclosed_kline_skipped`、`rest_response_competing_payload`、`rest_agg_trade_id_gap`、
  `channel_projection_mismatch`、`channel_projection_incomparable`）与批次 E 的完整分类法对齐。

## 版本策略

- 不改 `CONTRACT_SCHEMA_VERSION`；新表定义版本 `1.0.0`；新 policy / decoder / 身份规则 / 投影各自 SemVer，变化即新版本，
  旧版本保留以复现旧实验。
- 归档侧 `binance.spot.publication@1.0.0`、`binance.spot.archive-revision@1.0.0`、
  `hlens.binance.spot.raw-revision-identity@1.0.0` 不得因本 ADR 改动。

## 合规检查（D3A-R1 提交时）

- [x] 不修改冻结正文（`03-data.md` §7.1 / §7.3 / §6.2 原样，提案写在 §7.6）
- [x] 不修改 Constitution、已发布契约、`CollectorAdapter` Protocol、Schema 导出与八张表定义
- [x] 不新增依赖、不改 `uv.lock`、不改 settings 代码；不含实现代码、网络访问代码或凭据
- [x] 官方事实逐条标注来源与访问日期，未证明项单列（证据文件 §2）
- [x] D-33：Codex 已选 A，语义写入 §4；随本 ADR 整体接受生效
- [ ] Codex 复核 D3A-R1 并作接受门
- [ ] 验收矩阵 1 ~ 21 —— D3B ~ D3E；22 —— E / F

## 参考

- [ADR-0022](0022-phase1-market-and-execution-scope.md) §1 / §4（REST 只补尾；归档为权威 backfill）
- [ADR-0023](0023-bitemporal-revision-data.md) §1 / §2 / §4 / §5 / §7
- [03-data.md](../architecture/03-data.md) §4 / §7
- [证据：Binance 公共 spot market-data REST](../architecture/evidence/binance-spot-rest-market-data.md)
- [证据：`binance.spot.publication@1.0.0`](../architecture/evidence/binance-spot-publication.md)

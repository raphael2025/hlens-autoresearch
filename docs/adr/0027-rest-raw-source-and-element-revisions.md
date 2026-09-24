# ADR-0027: REST 补尾的 Raw source / element revision 与三跳 lineage（D-33）

| 字段 | 值 |
|---|---|
| 状态 | **Proposed**（2026-09-25，Phase 1 D3A 起草；待 Codex 复核裁决，未获批准前不得实现） |
| 日期 | 2026-09-25 |
| 决策者 | Codex（Raphael 2026-09-24 "授权所有" 的持续授权范围内；本 ADR 尚未获批） |
| 起草者 | Claude Code（Opus），Phase 1 批次 D3A（docs-only） |
| 相关 Phase | Phase 1（roadmap 验收 #13 的前置设计） |
| 影响范围 | Data / Infrastructure / Security |
| 是否破坏兼容 | 否：新增表与新增标识符，八张冻结表定义、已发布契约字段与 Schema 导出全部不变 |
| 前置 | [ADR-0021](0021-phase1-local-data-infrastructure.md)、[ADR-0022](0022-phase1-market-and-execution-scope.md)、[ADR-0023](0023-bitemporal-revision-data.md)、[ADR-0024](0024-historical-tradable-universe.md) |
| 证据 | [binance-spot-rest-market-data.md](../architecture/evidence/binance-spot-rest-market-data.md)（2026-09-25 检索） |
| 落点 | `03-data.md` §7.6（提案小节，接受后并入 §7.1 / §7.3 冻结正文） |

## 背景

ADR-0022 把 REST 定位为**只补尾 / 补缺口**的通道（权威 backfill 仍是官方归档），并要求原始响应进入 Raw、可复现。
ADR-0023 §4 要求每个 payload 作为不可变 revision 追加；B2 的 `SelectedRevisionLineage` 把选中数据的来源链固定为三跳：

    Canonical revision → Raw row revision → Raw source payload revision

归档路径上这三跳已经落地（`canonical.*` → `raw.binance_spot_agg_trades` / `raw.binance_spot_klines_1m`
→ `raw.binance_spot_archives`，D1 / D2 已验收）。REST 路径上**还没有任何一跳可以诚实落地**：

1. 没有承载 HTTP 响应原始字节的 Raw source 表；
2. `raw.binance_spot_agg_trades` / `raw.binance_spot_klines_1m` 的 `archive_revision_id`、`archive_line_number`
   是**必填**列，且行的 `source_id` 按 D2 的身份规则绑定到**归档 revision**
   （`infrastructure/revision/identity.py::row_source_identity`）——REST 元素放进去只能靠伪造归档身份；
3. `03-data.md` §7.1 是冻结清单，增表或改表语义必须经 ADR。

本 ADR 只解决"**REST 数据如何诚实地成为 Raw**"这一层，并给出实施批次的边界。它不实现任何 HTTP 代码，
不触碰 D4 WebSocket、E 的 Canonical 与 F 的 PIT。

设计复核过程中发现三个**必须在实现前决定**的结构性陷阱（§9 / §10 详述）：全局身份规则哈希、
availability policy 的 `policy_id` 唯一性约束，以及 `arrival_seq` 的跨通道唯一性。
另有一个**必须由 Codex 裁决**的语义问题：同一市场观察同时由归档与 REST 交付时如何不 fail closed（**D-33**，§3.4）。

## 裁决

### 1. 表拓扑：additive，三张新表，八张冻结表一字不改

| 表 | 职责 | 初始分区 | 定义版本 |
|---|---|---|---|
| `raw.binance_spot_rest_responses` | **Raw source payload revision**：一次 HTTP 响应（一页）的规范请求身份、响应字节对象引用、HTTP 元数据、revision 块与两轴时间 | 不分区 | `1.0.0` |
| `raw.binance_spot_rest_agg_trades` | 从某个响应页解码出的**单条 aggTrade 元素 revision** | identity `symbol` + `day(event_time)` | `1.0.0` |
| `raw.binance_spot_rest_klines_1m` | 从某个响应页解码出的**单条 1m kline 元素 revision** | identity `symbol` + `day(interval_start)` | `1.0.0` |

- 三张表都在既有 `raw` namespace 下，因此 `SelectedRevisionLineage`（`raw_table` / `source_table` 必须位于
  `raw` namespace）与 `canonical.*` 的 `lineage_*` 列**无需任何改动**即可表达 REST 三跳：
  `canonical.trades → raw.binance_spot_rest_agg_trades → raw.binance_spot_rest_responses`。
- 元素表复用归档元素表的 revision 块（`RevisionRecord` + `AvailabilityDecision` + `ObservationTimes` 列）与
  原生字段列，**但**把 `archive_revision_id` / `archive_line_number` / `parser_*` 换成
  `response_revision_id` / `element_index` / `decoder_*`（语义不同、名字就必须不同）。
- **为什么不改现有归档表**：`archive_revision_id` 改为可空 = 冻结表的语义变化（10-migration.md §3 的 major），
  需要迁移路径与旧读取器；已提交的归档行会与新语义混居；D1 / D2 的已验收证据全部需要重跑。
  additive 方案让归档路径的定义哈希、数据与验收证据**逐字节不变**。

### 2. Raw source revision：一个 HTTP 响应页

| 项 | 规则 |
|---|---|
| `observation_key` | `binance:spot:rest:<规范请求身份>`（§4）。**观察对象是"这个确切请求"**，不是"这段行情"：同一请求重复执行是同一观察的不同 revision |
| `payload_hash` | 响应体**原始字节**的 SHA-256（解析之前） |
| 字节留存 | 字节经 `StorageAdapter` staging → 校验 → 原子发布为不可变对象，key 内容寻址：`raw/binance/spot/rest/responses/<sha256>/<data_type>/<symbol>/<请求摘要>.json`；行内记 `object_key` / `object_uri` / `object_sha256` / `object_size_bytes`，**可从行取回原始字节** |
| `source_id` | `binance.public.spot.rest@1.0.0`（新 `SourceBinding`，§8） |
| `revision_id` | `rev1-<sha256>`，由 §9 的**独立** REST 身份规则从 `{规则 ID + 版本 + 哈希, observation_key, source_identity, payload_hash}` 派生 |
| 重复 | 同请求 + 同字节 → 幂等，不产生新 revision、不分配新 `arrival_seq` |
| 冲突 | 同请求 + 不同字节 → 追加为同一 `observation_key` 的第二条 revision，并写质量事件 `rest_response_competing_payload`。**响应级 revision 从不参与 PIT 选择**（PIT 只选市场观察），因此响应级 competing heads **不是**数据集级失败 |
| 额外列 | 规范请求身份的可查询分量（`data_type`、`symbol`、`request_path`、`request_query`、`declared_time_unit`、`page_from_id`、`page_start_time`、`page_limit`）、`request_window_start/end`、`retrieved_at`、HTTP 状态码、`source_metadata`（含 `x-mbx-used-weight-*`、`date`、`content-type`，凭据形状的键被拒） |

### 3. Raw element revision：一条 aggTrade / 一根 1m kline

#### 3.1 observation_key：与归档**逐字符相同**

- aggTrade：`binance:spot:agg_trade:<symbol>:<aggTradeId>`
- 1m kline：`binance:spot:kline:<symbol>:1m:<interval_start epoch 微秒>`

这正是 D2 身份规则 1.0.0 已冻结的键。REST 身份规则**重复写出同一文法**（与 D0 / D2 重复写出归档路径文法同理），
并由一个跨模块一致性测试证明两个模块对同一输入产出同一键——同一市场观察在 Raw 层就落到同一个 key 上，
Canonical 层无需靠名字猜测汇合。

#### 3.2 source identity：**通道级**，不是响应级

REST 元素的 source identity 是 `binance.public.spot.rest@1.0.0`（**不含**响应 revision id）。后果：

- 分页重叠、5xx 后重取、窗口重叠导致同一元素被多次交付时，只要内容相同就是
  ADR-0023 §4 的"重复"——幂等去重，不产生新 revision、不产生 competing heads；
- 两个响应对同一元素给出**不同**内容时，才产生第二条 revision → competing heads → fail closed（真实矛盾必须暴露）。

这与归档侧 `row_source_identity(archive_revision_id)`（响应级/文件级）**刻意不对称**：归档通道的替换单位是
*文件*，ADR-0022 §4 要求被替换文件与其解析结果全部保留，所以归档行的身份必须绑定具体文件 revision；
REST 通道没有"文件替换"概念，每次响应都是对同一数据库的一次重新读取。交付它的响应仍以
`response_revision_id` 列记录为**交付证据**（首次交付该身份的响应），它是 provenance，不是身份。

#### 3.3 payload hash 与其余字段

- payload hash = 原生字段 + 标的身份 + 声明时间单位的规范文档哈希（与 D1 / D2 同构，十进制定点文本，
  不含到达时间、页号、`arrival_seq`）。
- `event_time`（aggTrade）/ `interval_start` + `interval_end`（kline）由声明单位换算，**禁止逐值猜数量级**；
  解码器校验所有时间落在该请求窗口内（零容差），任一越界 → 整页拒绝、不写元素行、写质量事件（D1 同规则）。
- `element_index`：该元素在响应数组中的 0 基位置（审计与 `arrival_seq` 分配用，不入 payload hash）。

#### 3.4 归档 ↔ REST 汇合：**D-33，需 Codex 裁决**

同一 aggTrade 既可能来自归档（source identity = 某归档 revision），也可能来自 REST（source identity = REST 通道）。
两者是**同一 `observation_key` 的两条 revision**。按 ADR-0023 §5，无 precedence 证据的多个 maximal head =
competing heads = **数据集 fail closed**。归档终将覆盖曾由 REST 补过的区间，因此**不裁决就等于首切片端到端必然失败**。

| 方案 | 说明 | 评价 |
|---|---|---|
| **A（推荐）** 内容一致时的通道 precedence policy `binance.spot.delivery-channel@1.0.0` | 仅当两条 revision 的**规范市场内容逐字段相同**时，在 ingest 时持久化 `PrecedenceEvidence`：归档 revision supersede REST revision；内容**不同**时**不产生任何边** → competing heads → fail closed | 不改任何契约；选择结果与到达顺序无关（边的方向由通道决定，不由先后决定）；边只在内容相同的情况下出现，**不可能改变任何字段取值**，只让选择唯一并让 lineage 指向带 checksum 的可复现归档；真实矛盾仍然暴露。**代价**：这是项目判断（ADR-0022 指定归档为权威 backfill 来源），**不是**来源声明的先后，是对 ADR-0023 §4「来源可证明的优先级」的一次明确放宽，必须由 Codex 批准并写进 policy 证据 |
| B 不做任何裁决 | 所有重叠观察 fail closed | 只要归档补上曾被 REST 覆盖的区间，数据集就再也构建不出来；等于禁止 REST 补尾 |
| C 改 PIT 语义：内容相同的多个 head 视为不冲突 | 需要 `PointInTimeSelection`（`selected` 必须唯一 head）与 `SelectedRevisionLineage`（单一三跳）扩展 | 触碰已发布契约（additive minor，但 blast radius 大、要重开 B 批次）；语义上更"诚实"，但代价高 |
| D REST 元素不进 Raw / 事后删除 | — | 违反 append-only 与 H6；且无法预知哪些区间将来会有归档 |
| E REST 元素用不同 observation_key（键含通道） | — | 同一笔成交在 Canonical 变成两条观察，不诚实 |

**推荐 A**，并要求：边只能在 ingest 时持久化（不得查询期推断）、两个方向的到达顺序产出同一组边、
policy 证据文件写明"本 policy 不是来源声明的先后，而是 ADR-0022 的通道权威性 + 内容相同这一事实"。
Codex 若选 C，则 D3 必须先回到 B 型契约批次。

### 4. 请求身份：规范编码

一次请求的规范身份文档（canonical JSON，SHA-256 进入 `observation_key`）包含且仅包含：

    { "method": "GET",
      "origin": "<配置的 market-data-only base 的 origin>",
      "path": "/api/v3/aggTrades" | "/api/v3/klines",
      "query": [["<name>", "<value>"], …],        // 按 name 排序的白名单参数
      "declared_time_unit": "millisecond",        // 1.0.0 不发送 X-MBX-TIME-UNIT（证据 N8）
      "rule": "<REST 身份规则 id@version>" }

- 参数白名单（多余参数一律拒绝构造）：
  `aggTrades` = `symbol`、`fromId`?、`startTime`?、`limit`；`klines` = `symbol`、`interval=1m`、`startTime`?、`limit`。
  `endTime` 与 `timeZone` **不使用**（证据 N6 / R9）。
- 值编码：整数十进制无前导零；时间统一为**毫秒整数**（声明单位）；symbol 原样大写。
- `CollectionRequest.request_id` 取同一身份的短 token（满足既有 `REQUEST_ID_PATTERN`，不含 `/` `?`）。
- **同请求幂等**：字节相同 → 同 `payload_hash` → 同 `revision_id` → 已提交即 `already_committed`。
- **同请求不同 payload**：追加第二条 revision + 质量事件（§2），不覆盖、不选边。

### 5. 分页与覆盖：确定性、不依赖隐式默认

**结构性禁止**：`fromId`、`startTime` 全部缺省的请求（官方的"最新"模式，证据 R4 / R9）不可构造——
collector 在构造期拒绝，不是运行期判断。

**aggTrades（ID 推进）**

1. 首页：`symbol`、`startTime = t0(ms)`、`limit = 1000`（依据 R11：带 `startTime` 返回自该时刻起最旧的若干条）；
2. 续页：`fromId = 上一页最大 a + 1`、`limit = 1000`（`fromId` INCLUSIVE，R3）；
3. 停止条件（任一满足即停，全部显式）：页内首元素 `T >= t1`（已越过半开窗口右端）；返回条数 `< limit`（来源已给完）；
   返回空数组；达到该次运行的页数上限（有界性，配置项）；
4. 解码出的**全部** aggTrade 元素都写成 element revision（Raw 是"原样落地"，越窗元素同样是真实观察，
   它们只是不计入本窗口的覆盖账）；
5. 边界重复由 §3.2 的通道级身份幂等吸收，不需要去重逻辑。

**klines（open-time 推进）**

1. 首页：`symbol`、`interval=1m`、`startTime = t0(ms)`、`limit = 1000`；
2. 续页：`startTime = 上一页最大 open_time + 60_000`；
3. 停止条件同上（以 `open_time >= t1` 取代 `T >= t1`）；
4. **未结束 K 线**：只有 `interval_end <= retrieved_at` 的 K 线才写成 bar element；其余**不写**，
   只留在 Raw 响应载荷中，并写质量事件 `rest_unclosed_kline_skipped`（证据 N5：响应里没有 `x` 字段，
   无法从载荷判断是否收盘，只能保守）。

半开区间 `[t0, t1)` 由本机裁定（不依赖 `endTime` 的 INCLUSIVE 语义，证据 N6）。

### 6. Gap 与 reconciliation

- **覆盖证据**只来自已提交的行：归档侧 `raw.binance_spot_archives` 的 `[coverage_start, coverage_end)`；
  REST 侧响应行的请求窗口与其实际交付的元素时间范围。
- **gap** = 请求窗口减去上述覆盖的补集，按 symbol × data_type × UTC 半开区间记录，来源为
  `CollectionResult.gaps`（`GapReason.SOURCE_ABSENT`：来源对该请求没有返回数据）与质量事件 `rest_window_gap`。
- **resolved / unresolved**：gap 记录不可变；它在后续 snapshot 中被新的覆盖证据**覆盖**即视为 resolved，
  由 reconciliation 报告按 snapshot 计算，不回写旧记录。
- **诚实边界（证据 N2）**：官方未声明 aggTrade ID 连续，因此任何缺口结论只能表述为
  "该请求下来源返回了这些元素"，**不得**表述为"这段时间没有成交"；永远不生成推断行情。
- 暂时性失败（429 / 418 / 5xx / 传输中断）**不得**记为 `SOURCE_ABSENT` 缺口：整次 `collect` 以
  `CollectionFailed` 失败（可幂等重试），不产生半真半假的覆盖账。

### 7. 双时间

| 字段 | REST 取值 |
|---|---|
| `event_time` | aggTrade 的 `T`；kline 的 `interval_start`（`interval_end = interval_start + 1m`） |
| `source_time` | **空**。响应头 `Date` / `Last-Modified` / `ETag` 不是"该观察的公开时间"的来源声明（与 D2 同裁决，证据 N10）；原样保存在 `source_metadata` 供审计 |
| `available_time` | 新 availability policy `binance.spot.rest-publication@1.0.0`：官方无任何公开时刻上界（证据 N1）→ 全部主体取 `available_time = ingest_time` + 证据缺口 token |
| `ingest_time` | 本机收到该响应字节的时刻；同一响应解码出的元素**继承**该 `ingest_time`（它们确实是随这份字节到达的） |
| `knowledge_time` | 严格解码 + 窗口校验通过后的本机 UTC 时刻 |
| `declared_latency` | 0 |

`ingest_time` 早于观察可被观察的时刻（例如未结束区间）→ 拒绝写入（D2 已实现的同一不变量）。

### 8. 新增标识符（提案，接受后并入 `03-data.md` §7.3）

| 类别 | 标识符 | 内容 |
|---|---|---|
| Source | `binance.public.spot.rest@1.0.0` | market-data-only base 的 `GET /api/v3/aggTrades` 与 `GET /api/v3/klines`（`1m`），security type NONE |
| Decoder（parser role） | `binance.spot.rest.decoder@1.0.0` | JSON 形状严格校验、声明时间单位（毫秒）、窗口零容差、未结束 K 线规则；变化 = 新版本 |
| Availability policy | `binance.spot.rest-publication@1.0.0` | §7：全部主体 `available_time = ingest_time` + 证据缺口 |
| Precedence policy | `binance.spot.rest-revision@1.0.0` | REST 通道内部：同内容幂等；不同内容无证据 → competing heads |
| Precedence policy | `binance.spot.delivery-channel@1.0.0` | 跨通道（D-33 方案 A 获批时才存在）：内容相同 → 归档 supersede REST；内容不同 → 无边 |
| 身份规则 | `hlens.binance.spot.rest-revision-identity@1.0.0` | §9：REST 专用、与归档规则物理隔离 |

### 9. 三个必须避开的结构性陷阱（实现前决定）

1. **身份规则哈希是全局的**。`revision_id` 的派生文档包含 `rule_hash = IDENTITY_HASH`，而 `IDENTITY_HASH` 是
   **整份** `IDENTITY_SPEC` 的哈希。若把 REST 规则加进 D2 的同一份文档（升到 1.1.0），**所有归档 revision 的
   `revision_id` 都会改变**，已提交归档的重放幂等立刻失效。→ REST 必须有**独立**的身份规则模块与文档
   （`hlens.binance.spot.rest-revision-identity@1.0.0`），D2 的 `identity.py` 一字不动。
2. **`PointInTimeSpec` 的 `policy_id` 在同一字段内必须唯一**。若把 REST 主体加进
   `binance.spot.publication` 并升到 1.1.0，一份同时使用归档行（1.0.0）与 REST 行（1.1.0）的数据集将
   **无法表达**（同一 `policy_id` 两个版本会被拒绝）。→ REST 用**独立的 policy_id**
   `binance.spot.rest-publication`，归档的 1.0.0 保持不变。
3. **`arrival_seq` 必须跨通道唯一**。D-33 方案 A 需要把归档行与 REST 行放进同一个 `RevisionGraph`
   （同一 `observation_key` 的两条 revision），而 `RevisionGraph` 要求 `arrival_seq` 唯一。D2 从
   `raw.binance_spot_archives` 的最大值按 `2**32` 步长分配块。→ 按**区间划分** int64 空间：
   归档块基址保持自 0 起，REST 块基址自 `2**62` 起（步长同为 `2**32`，每页元素 `base + element_index + 1`）；
   并给 D2 的块分配器补一个上界检查，使它永远不可能进入 REST 区间（纯增量守卫 + 回归测试，不改变既有行为）。

### 10. 迁移与兼容

- **additive**：三张新表、六个新标识符；八张冻结表的 `TableDefinition`（Schema、字段 ID、分区、properties、
  定义哈希）与已提交数据**逐字节不变**；`core/` 契约、`schemas/` 导出（74 份）、`CONTRACT_SCHEMA_VERSION`
  全部不变；旧归档数据与 manifest 的读取路径不变。
- 新表定义版本 `1.0.0`；`PHASE1_TABLES` 由 8 张扩为 11 张（代码改动属 D3B，且必须保持前 8 张定义哈希不变的断言）。
- `03-data.md` §7.1 是冻结正文：**Proposed 期间不改它**。本批次只在新增的 §7.6 写提案；Codex 接受后由实施批次
  把三张表并入 §7.1、把标识符并入 §7.3（ADR-0023 §8 的同一做法）。
- **不属于本 ADR、需要 Codex 另行批准**：D3C 可能需要的两个新设置字段
  （`HLENS_BINANCE_REST_MAX_RETRY_AFTER_SECONDS`、`HLENS_BINANCE_REST_MIN_REQUEST_INTERVAL_MS`）——
  §6.2 同样是冻结清单，本批次只提出，不修改。

## 明确不做

- 不写任何 HTTP / 解码 / revision 实现代码，不新增依赖，不改 settings，不改八张表定义与 Schema 导出。
- 不开始 D4（WebSocket）、E（Canonical / 质量报告）、F（PIT / manifest）。
- 不定义质量事件的完整分类法（属批次 E）；本 ADR 只提出 REST 相关的事件类型名。
- 不声称 REST 与归档内容一致、不声称缺口已被补齐、不声称 ID 连续（证据 §2）。
- 不访问账户 / 订单 / 签名端点，不引入任何凭据。

## 备选方案（表拓扑层面）

| 方案 | 优点 | 缺点 | 为何未选 |
|---|---|---|---|
| **A（本 ADR）** 新增 REST source 表 + 两张 REST 元素表 | 三跳 lineage 天然成立；归档路径零改动；两条通道语义各自诚实 | 表数从 8 增到 11；两套元素表需要 Canonical 层合并 | — |
| B 复用归档元素表，`archive_revision_id` 改可空 + 加 `response_revision_id` | 表数不变 | 冻结表语义变化（major）；同一表混两种来源语义；D1 / D2 证据需重跑；行级 lineage 二义 | 破坏冻结契约且不诚实 |
| C REST 只写 Canonical | 最少的表 | 跳过 Raw row hop，三跳 lineage 不成立；原始响应无处可放 | 违反 ADR-0022 / 0023 |
| D 把响应伪装成归档写进 `raw.binance_spot_archives` | 复用 D2 全部代码 | `source_uri` / `.CHECKSUM` / coverage / source binding 全部是归档语义；等于伪造来源 | 任务明令禁止，且不可审计 |
| E 用一张通用 `raw.source_payloads` 取代归档表 | 长期最整齐 | 重写冻结表 + 迁移已提交数据 | 代价与风险远超收益 |

## 契约、Schema 与迁移影响

- 已发布契约字段**不变**：`SelectedRevisionLineage`、`RevisionRecord`、`AvailabilityDecision`、
  `PrecedenceEvidence`、`PointInTimeSpec`、`CollectionRequest` / `CollectionResult` / `CollectedObject`
  全部按现状使用；REST 的 `source_uri` 带 query，已被现有 `validate_source_uri` 允许（凭据形状的参数名被拒）。
- 不新增 `Kind` 取值、不新增 `GapReason` 取值、不改 `CONTRACT_SCHEMA_VERSION`、不新增 Schema 导出文件。
- 新增的三张表随表定义版本化（10-migration.md §3）；分区演进仍需等价测试。

## 失败与恢复语义

- **崩溃点 1**（字节已发布、响应行未提交）：对象内容寻址且无人引用 → orphan，由显式 maintenance 清理
  （ADR-0023 §7）；重跑重新发起请求，若字节不同则是同一请求观察的第二条 revision，两者都保留。
- **崩溃点 2**（响应行已提交、元素批次未完）：**不重新发起 HTTP 请求**——从已提交行的 `object_uri` 读回
  原始字节，用同一 decoder 版本重新解码，得到同一批元素 revision，缺失的 microbatch 幂等补提交。
  **无 sidecar 真值**，与 D2 的恢复模型一致。
- 解码失败：不产生任何元素行，不产生响应行的 `knowledge_time`（失败 payload 无知识时间，ADR-0023），
  已发布的响应对象保留，写质量事件。
- 429 / 418 / 5xx / 传输中断：`CollectionFailed`，可按同一请求重试；不产生缺口记录、不部分写入。

## 安全边界

- **端点 allowlist 是结构化检查，不是字符串前缀比较**：逐项校验 scheme = https、authority 等于配置的
  market-data-only origin、path ∈ 冻结集合（精确相等）、query 参数名 ∈ 白名单且值符合规范编码、
  无 fragment、无凭据；**不跟随重定向**（3xx 直接失败）。D0 的 `_assert_url_allowed` 拒绝一切 query，
  不能直接复用——REST 需要自己的、更严格的（origin + 精确 path + 参数白名单）检查。
- 429 / 418：按 `Retry-After`（秒，证据 R15）有界退避，等待上限可配置并 fail closed；**禁止无限重试**；
  418 立即终止本次运行。4xx（非 429）视为本机构造错误，不重试。
- 无 secret、无 API key、无签名、无账户 / 订单端点；响应元数据中凭据形状的键被拒绝。
- 来自交易所的 JSON 是不可信输入：严格形状校验、有界响应体、不执行其中任何内容。

## 验收矩阵（D3B ~ D3E 实施批次验证）

| # | 场景 | 期望 |
|---|---|---|
| 1 | 八张冻结表的定义哈希与分区 | 与 C3 逐字节相同（回归断言） |
| 2 | 同一请求、同一字节重放 | 幂等：无新 revision、无新 `arrival_seq`、无新 snapshot |
| 3 | 同一请求、不同字节 | 追加第二条响应 revision + 质量事件；数据集构建**不**因此失败 |
| 4 | 同一元素由两页重复交付（内容相同） | 幂等去重，元素只有一条 revision |
| 5 | 两页对同一元素给出不同内容 | 两条 revision，competing heads，PIT fail closed，写质量事件 |
| 6 | 同一观察由归档与 REST 各交付一次（内容相同） | 按 D-33 裁决；方案 A：ingest 时持久化通道 precedence 边，PIT 唯一选中归档 revision，两种到达顺序结果一致 |
| 7 | 同一观察由归档与 REST 交付且内容**不同** | 无边，competing heads，fail closed |
| 8 | 缺省 `fromId` 与 `startTime` 的请求 | 构造期即被拒绝（隐式"最新"模式不可达） |
| 9 | 分页推进 | `fromId = last + 1` / `startTime = last open + 60s`；停止条件四选一命中；无死循环（页数上限） |
| 10 | 越窗元素、空页、最后一页 | 越窗元素照常写入 Raw 但不计入窗口覆盖；空页与短页正确终止并记缺口 |
| 11 | 未结束的 1m kline | 不写成 bar element，写质量事件；响应字节完整保留 |
| 12 | 时间单位与窗口不符（人为构造） | 整页拒绝、不部分写入 |
| 13 | 崩溃后重启（三个崩溃点） | 从已提交行 + 不可变对象恢复，不重新发起 HTTP；结果与一次跑完相同 |
| 14 | `arrival_seq` | 跨通道唯一；REST 落在 `>= 2**62` 区间；归档分配器不可能进入该区间 |
| 15 | 恶意 / 越界 URL（他站 origin、路径遍历、额外参数、重定向、凭据形状参数） | 全部拒绝，且不发起请求 |
| 16 | 429 / 418 / 5xx | 有界退避、遵守 `Retry-After`、上限外 fail closed、418 终止；不产生缺口记录 |
| 17 | 静态检查 | 代码中不存在账户 / 订单 / 签名端点、API key 读取或 market-data 之外的 origin |
| 18 | REST availability | 全部 revision `available_time = ingest_time` 且带证据缺口 token |
| 19 | 三跳 lineage | `canonical.* → raw.binance_spot_rest_* → raw.binance_spot_rest_responses` 可从 manifest 完整解析（批次 E / F 验证） |

## 后果

- 正面：REST 数据获得与归档同等强度的可审计性（原始字节 + 元素级 revision + 两轴时间 + 三跳 lineage）；
  归档路径零风险；PIT 的 fail-closed 语义不被削弱。
- 负面 / 代价：表从 8 张增到 11 张；Canonical 层要合并两套 Raw 元素表；跨通道 precedence 需要 ingest 时的
  对侧查找（按 symbol / 天分区扫描，成本有界）；D-33 方案 A 是对 ADR-0023 §4 的一次有限放宽。
- 对复现性：请求身份、响应字节、decoder 版本、policy 版本全部进入 manifest 绑定，逐页可复现。

## 开放义务

- **D-33 由 Codex 裁决**（§3.4）；未裁决前 D3E 不得开工。
- 实施批次按证据文件 §3 重新核对官方资料并做只读 smoke；实测与证据冲突时先更新证据再发新版本。
- 质量事件类型名（`rest_window_gap`、`rest_page_empty`、`rest_unclosed_kline_skipped`、
  `rest_response_competing_payload`）需与批次 E 的完整分类法对齐。
- 两个新设置字段需 Codex 批准后才能进入 `03-data.md` §6.2。
- 本 ADR 接受后：把 §1 / §8 并入 `03-data.md` §7.1 / §7.3，删除 §7.6 的"提案"标记。

## 版本策略

- 不改 `CONTRACT_SCHEMA_VERSION`；新表定义版本 `1.0.0`；新 policy / decoder / 身份规则各自 SemVer，
  变化即新版本、旧版本保留以复现旧实验。
- 归档侧的 `binance.spot.publication@1.0.0`、`binance.spot.archive-revision@1.0.0`、
  `hlens.binance.spot.raw-revision-identity@1.0.0` **不得**因本 ADR 改动。

## 合规检查（D3A 提交时）

- [x] 不修改冻结正文（`03-data.md` §7.1 / §7.3 / §6.2 原样，提案写在新增的 §7.6）
- [x] 不修改 Constitution、已发布契约字段、Schema 导出与八张表定义
- [x] 不新增依赖、不改 `uv.lock`、不改 settings
- [x] 不含任何实现代码；无网络访问代码；无凭据
- [x] 官方事实逐条标注来源与访问日期，未证明项单列（证据文件 §2）
- [ ] D-33 裁决 —— 待 Codex
- [ ] 验收矩阵 1 ~ 19 —— D3B ~ D3E 实施批次

## 参考

- [ADR-0022](0022-phase1-market-and-execution-scope.md) §1 / §4（REST 只补尾；归档替换 = 新 source revision）
- [ADR-0023](0023-bitemporal-revision-data.md) §1 / §2 / §4 / §5 / §7
- [03-data.md](../architecture/03-data.md) §4 / §7
- [证据：Binance 公共 spot market-data REST](../architecture/evidence/binance-spot-rest-market-data.md)
- [证据：`binance.spot.publication@1.0.0`](../architecture/evidence/binance-spot-publication.md)

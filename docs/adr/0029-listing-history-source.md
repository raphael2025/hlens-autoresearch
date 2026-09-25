# ADR-0029: 首切片标的上市历史的来源（E2，D-E2）

| 字段 | 值 |
|---|---|
| 状态 | **Proposed**（2026-09-25，待 Raphael / Codex 决定；未批准前 E2 不得实施） |
| 日期 | 2026-09-25 |
| 决策者 | 待定 |
| 起草者 | Claude Code（Opus），依 Raphael 2026-09-25 对 D-E2 的"同意推荐方案"（先取证、再起草） |
| 相关 Phase | Phase 1（roadmap 验收 #16、#18 的前置；E2 / F2） |
| 影响范围 | Data / Infrastructure / Security（新增一个 market-data-only 端点） |
| 是否破坏兼容 | 否（建议方案为 additive：新增一张 Raw 表、一个 source、一个 policy、一个端点；`canonical.instrument_listings` 定义不变） |
| 前置 | [ADR-0022](0022-phase1-market-and-execution-scope.md)、[ADR-0023](0023-bitemporal-revision-data.md)、[ADR-0024](0024-historical-tradable-universe.md)、[ADR-0027](0027-rest-raw-source-and-element-revisions.md) |
| 证据 | [binance-spot-listing.md](../architecture/evidence/binance-spot-listing.md)（2026-09-25 检索，只读官方文档，未调用 API） |

## 背景

ADR-0024 要求按当时可交易集合构建 universe，并把"确认 Binance 公开数据中可用的 listing / 状态信息来源"列为实施前义务。
取证结论（证据 §3）：

1. 官方无凭据来源中**只有** `GET /api/v3/exchangeInfo` 给出标的状态，且只是**当前**快照；无上市 / 下架日期、无状态历史、
   无修订时间、无稳定产品 ID（L1～L4）；
2. 状态值（`TRADING`、`END_OF_DAY`、`HALT`、`BREAK`、`CANCEL_ONLY`）有列举、**无**语义定义（L5、L6）；
3. 官方归档不提供任何标的的上市信息（L7）；
4. 冻结表 `canonical.instrument_listings` 每条 revision 绑定**一条** Raw lineage（`lineage_raw_*` / `lineage_source_*`），
   而现有 12 张表中没有能承载 listing 观察的 Raw 表。

另有一条已存在、与本 ADR 同源的约束：D2 / D3A 证据已确认官方资料**不能**证明任何行情 revision 的公开时刻，因此归档与 REST
行情一律 `available_time = ingest_time`，**早于本机 ingest 的历史可用区间为空**（`PROJECT_MEMORY.md` §7）。listing 同理：
没有历史公开时刻证据时，任何来源的 listing revision 在早于其 `ingest_time` 的 `simulation_time` 下都不可见。
**这意味着无论选哪个方案，本 ADR 都不能让历史 simulation 的 universe 可构建**；那是一个独立的证据问题（见"开放义务"）。

## 裁决（提案）

### 1. 来源：`exchangeInfo` 状态快照（方案 A）

- 新 source `binance.public.spot.exchange-info@1.0.0`：market-data-only base 上的 `GET /api/v3/exchangeInfo`，
  只带 `symbols=["BTCUSDT","ETHUSDT"]`（首切片 universe；结构化 allowlist 与 D3D 相同，不接受其它参数、不跟随重定向、
  无 cookie / auth / 环境代理；security type NONE）。这是对 ADR-0022 端点清单的一项 additive 扩充，需本 ADR 批准。
- 每次成功的 200 响应是一条 **Raw source revision**，存入新表 `raw.binance_spot_exchange_info`（不分区）：
  响应字节不可变对象（内容寻址 key）、请求身份、`retrieved_at`、响应级 `serverTime`、每个请求 symbol 的原生字段
  （`symbol`、`status`、`baseAsset`、`quoteAsset`）；身份与重复 / 竞争语义沿用 ADR-0027 §2 的"页身份 + 字节"规则
  （新身份规则，独立哈希）。availability policy `binance.spot.exchange-info-publication@1.0.0`：
  `available_time = ingest_time` + 证据缺口（官方无任何状态的发布时刻，L4）。

### 2. 由快照推导 listing revision（observed-interval 语义）

- 一条 listing revision = 一次快照对一个 symbol 的观察映射到 `canonical.instrument_listings`：
  lineage 三跳为 `canonical.instrument_listings → raw.binance_spot_exchange_info（快照 revision）→ 同一快照 revision`
  （快照即 source，第二、三跳同表同 revision；`SelectedRevisionLineage` 允许，需 E2 测试证明）。
- episode 键为 ADR-0024 的**退化键** `(binance, spot, symbol, tradable_from)`（无稳定产品 ID，L4），记录中标注退化。
- **`tradable_from` = 本机首次观察到该 symbol 为 `TRADING` 的快照的 `retrieved_at`**，并在 `status_reason`
  与证据缺口中写明"observed-from：本机观察下界，不是交易所声明的上市时刻"。它是保守的**下界陈述**，不是事实断言；
  若将来有官方历史证据，只能以新 policy 版本追加新 revision，旧 revision 不改写。
- 状态映射（项目政策 `binance.spot.listing-status@1.0.0`）：`TRADING` → `listed`；`HALT` / `BREAK` / `END_OF_DAY`
  / `CANCEL_ONLY` → `suspended`（在观察时刻关闭当前可交易区间）；快照中缺失已知 symbol 或出现未列举的状态值 →
  **不推断**（不写 `delisted`），产生质量事件并使该 episode 的 universe 构建 fail closed，直到有新的明确观察。
  `delisted` 在 1.0.0 中**永不**产生（官方无下架语义）。
- 每次状态变化（相对该 episode 的上一次已提交观察）追加一条新 listing revision，`supersedes` 上一条（**同一来源、同一
  episode、观察时刻严格递增**的项目 precedence 政策 `binance.spot.listing-observation@1.0.0`，证据为两次快照的
  `retrieved_at` 与 revision id）；状态未变的快照只进 Raw，不产生新 listing revision。

### 3. 结果与边界

- universe 在 `simulation_time >= 首次观察的 ingest_time` 时可构建（forward / paper 可用）；更早的 simulation 下
  listing 与行情一样不可见，universe 构建 fail closed（报告"不可构建"，ADR-0024 失败语义）。
- 不访问 SAPI、公告页面或第三方来源（证据 X1～X3）。

## 备选方案

| 方案 | 内容 | 优点 | 缺点 | 结论 |
|---|---|---|---|---|
| **A（建议）** | `exchangeInfo` 快照 → 新 Raw 表 → 观察下界语义的 listing revision | 唯一的官方、无凭据来源；诚实标注下界；additive | 只能从本机开始记录；需扩 ADR-0022 端点清单与新 collector | 采纳（待批准） |
| B | 由已提交归档的"某日有成交"推导可交易区间 | 可覆盖历史日期 | 是推断而非上市记录；一条 listing revision 需绑定多条 Raw lineage，冻结表只能一条（需改表 = major）；且历史 `available_time` 仍为 `ingest_time`，推出的历史区间照样不可用于历史 simulation | 拒绝（不解决历史，且需改冻结表） |
| C | 项目手写的静态声明（如"BTCUSDT 自 2017-08-17 可交易"） | 最简单 | 用今天的知识写过去，正是 ADR-0024 禁止的幸存者偏差路径；无来源 | 拒绝 |
| D | SAPI 下架计划 / 官方公告 | 有下架信息 | 不在 market-data-only 列表；需凭据或非结构化解析；描述未来计划而非历史 | 拒绝（ADR-0022 边界） |
| E | 暂不实现 E2，universe 固定为首切片两标的 | 零成本 | ADR-0024 / manifest 要求 listing 历史与 `canonical.instrument_listings` snapshot，F2 / F3 无法满足 | 不建议 |

## 契约、Schema 与迁移影响

- 无 `core/` 契约变化（`ListingRevision` / `TradableInterval` / 退化键与 manifest 已由 B2 定义）；
- 新增 additive Raw 表 `raw.binance_spot_exchange_info`（第 13 张表）与标识符：source、身份规则、availability policy、
  状态映射 policy、listing-observation precedence policy、collector；原 12 张表定义与哈希不变；
- ADR-0022 端点清单 additive 扩充 `GET /api/v3/exchangeInfo`（仅 market-data-only base、仅首切片 symbols）。

## 失败与恢复语义

网络 / 限流 / 非 200 / 解析失败沿用 D3D：有界失败、不产生 Raw revision、不伪装为"下架"；未知状态值或缺失 symbol
fail closed 并写质量事件；collector 与 store 的重放、崩溃恢复沿用 D3D / D3E 的不可变对象 + checkpoint 模式。

## 安全边界

只读公共端点；无 API key、无签名、无账户 / 订单端点；allowlist 精确到 path 与 `symbols` 参数；Phase 13 红线不变。

## 验收矩阵（E2 实施批次）

| # | 情形 | 期望 |
|---|---|---|
| 1 | 首次快照 TRADING | 一条 listing revision，`tradable_from = retrieved_at`，退化键，证据缺口写明 observed-from |
| 2 | 同状态重复快照 | 只追加 Raw（或幂等），无新 listing revision |
| 3 | TRADING → HALT → TRADING | 同一 episode 两个区间，revision 链 `supersedes`，暂停在观察时刻关闭区间 |
| 4 | 未知状态值 / symbol 缺失 | 无推断、质量事件、universe fail closed |
| 5 | 快照乱序到达 | 按 `retrieved_at` 的项目 precedence 选择，与 `arrival_seq` 无关 |
| 6 | `simulation_time` 早于首次观察 | universe 不可构建（fail closed） |
| 7 | 请求越出 allowlist / 带凭据形状参数 | 联网前拒绝 |

## 后果

- 正面：listing 历史有官方来源、逐条可追溯、诚实标注下界；F2 / F3 在 forward 区间可闭环。
- 负面：历史 simulation 的 universe 仍不可构建——与行情数据同一证据缺口；新增一个端点与一张表。

## 开放义务

- **历史可用性证据**（行情与 listing 共同）：若要让历史 simulation 可用，需要能证明历史公开时刻的新证据与新 policy 版本
  （H3：不得为结果放宽）。这是项目级范围问题，建议作为独立决策提交 Raphael。
- 实施批次先做一次只读 smoke 核对 L4 字段与 `serverTime` 单位。

## 合规检查

- [x] 不破坏已冻结契约与表（additive）
- [x] 不修改 Validation Constitution
- [x] Domain 层仍无具体技术依赖
- [x] 无凭据、无交易端点

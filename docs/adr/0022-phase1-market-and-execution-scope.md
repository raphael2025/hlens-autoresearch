# ADR-0022: Phase 1 市场与执行边界（D-08）

| 字段 | 值 |
|---|---|
| 状态 | **Proposed**（2026-09-24，批次 A1 起草；A1r 同步 Codex 事实复核与 ADR-0023 时间语义；待 Codex 再复核后决定） |
| 日期 | 2026-09-24 |
| 决策者 | Codex（依据 Raphael 2026-09-24"授权所有"的持续授权；执行范围部分见「安全边界」） |
| 起草者 | Claude Code（Opus）按 Codex 裁决落文 |
| 相关 Phase | Phase 1（研究数据范围）；执行范围约束至 Phase 13 |
| 影响范围 | Data / Plugin / Security |
| 是否破坏兼容 | 否：不改任何现有契约 |
| 前置 | [ADR-0006](0006-strategy-lifecycle.md)（`execution_mode`）、[ADR-0011](0011-lifecycle-subject-authorization-and-time.md)（LIVE 证据结构）、[ADR-0021](0021-phase1-local-data-infrastructure.md) |
| 关联 | [ADR-0023](0023-bitemporal-revision-data.md)（source revision 语义） |

## 背景

D-08 问的是"覆盖哪些交易所 / 标的 / 频率（现货、永续、期权、链上），Phase 13 是否包含实盘、由谁授权、风险预算上限"。
roadmap Phase 1 的输入是"交易所公开行情（范围待定 D-08）"，输出包括 Collector Adapter、Raw / Canonical 表、
质量报告与基础 Representation。D-09 提案把 BTCUSDT 1H 作为参考标的，但那只是提案。

这个问题实际包含两件性质完全不同的事：**研究数据范围**（现在需要）与**未来执行范围**（Phase 13 才可能需要）。
本 ADR 把两者拆开，只批准研究数据范围，并把执行范围收紧为明确的禁止与未来授权要求。

Binance 相关事实的核验状态：Codex 于 2026-09-24 按官方资料复核了 spot 归档自 **2025-01-01** 起使用微秒时间戳，
以及公开 market-data-only REST base 为 `https://data-api.binance.vision`（来源：https://github.com/binance/binance-public-data/、
https://developers.binance.com/en/docs/products/spot/rest-api）。归档目录结构、`.CHECKSUM` 格式与归档替换做法依据同一官方仓库说明，
实施时须对照锁定日期的官方资料逐项确认。Claude 未联网、未访问任何交易所端点；实施批次仍须做行为 smoke 验证。

## 裁决

### 1. 第一纵向切片（研究数据）

| 项 | 决定 |
|---|---|
| Venue | Binance **公共**现货市场数据；不使用任何账户、API key 或交易密钥 |
| Instruments | `BTCUSDT`、`ETHUSDT` spot |
| 权威 backfill 来源 | 官方归档站点 `data.binance.vision` 的日 / 月归档：spot `aggTrades` 与 1m `klines` |
| 完整性 | **解析前**必须用同目录 `.CHECKSUM` 校验归档文件；归档文件本身（不只是解压后的行）进入 Raw |
| Tail / gap 补齐 | 只用官方 market-data-only REST base `https://data-api.binance.vision`；不调用任何账户或交易端点 |
| Live tail（WebSocket） | **延后**：在历史 backfill、REST gap reconciliation 与重放幂等验收全部通过后才可加入 |
| Canonical | trade 与 1m bar |
| 更高周期 | 从 Canonical 1m / trade **确定性派生**；不分别信任 venue 提供的多个 kline 周期 |
| 频率 | 1m 是首个基准表示；首批范围**不是**永久支持上限 |

### 2. Raw 的可追溯性

Raw 必须能追溯：归档文件与其 checksum、下载 URI 与下载时间、HTTP 元数据、原始行 / 消息；
若历史端点提供其它来源元数据，原样保留。venue 原生 symbol / ID 与 canonical 映射都要保留，
**不得**假定跨 venue 的 symbol 等价。

### 3. 时间戳单位（fail closed）

- Binance 官方说明 spot 归档自 **2025-01-01** 起时间戳由毫秒改为微秒。
- 解析器必须按**来源类型 + 文件日期 + parser schema version** 明确选择单位；单位映射写在 parser schema version 中，
  变更单位规则 = 新 parser schema version。
- 解析出的时间必须落在该文件声明覆盖的日期区间内（容差由 parser schema version 声明）；不满足即 **fail closed**，
  整个文件拒绝进入 Canonical，并记录质量事件。
- **禁止**对每个值做含糊的数量级猜测（例如"看起来像微秒就除以 1000"）。

### 4. 归档替换 = 新 source revision

官方归档可能在发现问题后替换文件并发布新 checksum。同一路径、不同 checksum 的文件是一个**新的 source revision**，
按 [ADR-0023](0023-bitemporal-revision-data.md) 追加；旧文件、旧 checksum 及其解析结果全部保留，**禁止覆盖**。
替换文件若没有可证明的公开时间，其 `available_time` 按 ADR-0023 §2 保守取本机 `ingest_time`，并记录证据缺口；
它只在 `knowledge_cutoff` 不早于其 `knowledge_time` 的数据集中可见。

### 5. 第二纵向切片（门控）

只有第一切片的 PIT、质量报告、重跑一致与快照验收**全部通过**后，才可加入：同两标的的 Binance USDⓈ-M perpetual，
以及 funding rate、mark price、open interest。第二切片开始前须在该批次记录中列明第一切片的验收证据。

### 6. 明确延期

order book、options、链上数据、其它 venue 均延期，各自需要新的范围决定（ADR 或 Codex 裁决记录）。

### 7. 执行范围

- Phase 1 ~ 12：系统**没有**任何下单能力，**不保存**任何交易密钥或账户凭据。
- 当前的项目全权开发授权（Raphael 2026-09-24"授权所有"）**不等于**实盘授权。
- Phase 13 若要从 paper 进入小额 live，venue、账户、最大资金、杠杆、单笔 / 单日损失上限、kill switch 必须形成
  **独立、具体、可审阅的人工授权记录**；在该记录存在之前，系统不得持有任何 LIVE 凭据。
- 本 ADR 不回答"Phase 13 是否实盘、预算多少"，这些仍由 Raphael 亲自决定（CLAUDE.md §0、H10）。

## 明确不做

- 本批次不访问任何交易所端点、不下载任何数据、不创建凭据、不写 Collector 代码。
- 不选择 Validation Profile 数值，不把 BTCUSDT / ETHUSDT 写成 D-09 的正式研究标的决定。
- 不引入 order book、衍生品（第二切片之前）、期权、链上或其它 venue。
- 不定义任何下单、账户或交易接口。

## 备选方案

| 方案 | 优点 | 缺点 | 为何拒绝 |
|---|---|---|---|
| **A（本 ADR）** Binance 公共 spot 两标的，归档优先 + REST 补齐，live tail 延后，衍生品第二切片 | 可复现（归档 + checksum）；范围小；无凭据 | 单一 venue；首批无衍生品 | — |
| B 直接从 WebSocket 实时采集起步 | 早有实时数据 | 历史不可复现，缺口难证明 | 违背可复现性 |
| C 首批同时接 spot + perpetual + funding | 研究面更广 | 首个切片的 PIT / 质量验收面翻倍 | 先证明一条纵向切片 |
| D 多 venue 并行 | 跨市场研究 | symbol 映射、时间对齐问题同时出现 | 范围过大 |
| E 使用带 API key 的账户端点提高限额 | 更快 | 引入凭据与账户风险 | 与"无凭据"边界冲突 |
| F 在本 ADR 中顺带定义 Phase 13 实盘范围 | 一次说完 | 越过 Raphael 的专属决定 | 权限不足 |

## 契约、Schema 与迁移影响

- 不改现有契约。现有 `Instrument`（venue / symbol / instrument_type / base / quote）足以描述两个 spot 标的的静态信息；
  上市 / 下架历史见 [ADR-0024](0024-historical-tradable-universe.md)。
- Raw / Canonical 的表 Schema、parser schema version 的编号方式由实施批次提出并随 Iceberg 表版本化；
  若需新增领域契约模型，按 02-domain.md §3 处理（增量为 minor）。
- 无既有数据需要迁移；外部既有数据（`~/BTC`）若将来导入，只能经 Collector 只读进入 Raw。

## 失败与恢复语义

- checksum 校验失败：该归档文件不解析、不进入 Canonical；记录质量事件，可在下次运行重试下载。
- 时间戳单位或范围校验失败：整个文件 fail closed，不部分写入。
- REST 请求失败或限流：只影响 tail / gap 补齐，可幂等重试；不得用推断值填补缺口，缺口显式标记。
- 归档被替换：作为新 source revision 追加，下游按 ADR-0023 的 PIT 语义决定何时可见。
- 下载中断：未通过 checksum 的临时文件不进入 Raw。

## 安全边界

- 只访问公共、market-data-only 端点；Collector 属于 05-plugin.md §6 允许联网但**必须声明**的插件。
- 不存在交易密钥、账户密钥或下单代码；任何出现都视为安全违规。
- 来自交易所的数据是不可信输入：解析失败 fail closed，不执行其中任何内容。
- 实盘授权与本 ADR 无关，见「执行范围」。

## 验收矩阵（实施批次）

| # | 场景 | 期望 |
|---|---|---|
| 1 | 归档 checksum 不匹配 | 不解析、不进入 Canonical，记录质量事件 |
| 2 | Raw 中缺少归档文件本身、checksum 或下载元数据 | 不允许 |
| 3 | 2024-12-31 与 2025-01-01 两天的 spot 归档 | 分别按毫秒与微秒解析，且由 parser schema version 决定 |
| 4 | 时间单位与文件日期不符（人为构造） | fail closed |
| 5 | 同一路径的归档 checksum 变化 | 追加为新 source revision，旧文件与旧解析结果保留 |
| 6 | 代码中出现账户 / 交易端点或 API key 读取 | 不存在（静态检查） |
| 7 | REST 补齐只访问 market-data-only base | 是 |
| 8 | WebSocket live tail 在 backfill、gap reconciliation、重放幂等验收前启用 | 不允许 |
| 9 | 更高周期 bar 从 Canonical 1m / trade 派生，重跑按位一致 | 是 |
| 10 | 第二切片在第一切片验收全绿之前开始 | 不允许 |
| 11 | venue 原生 symbol / ID 与 canonical 映射可追溯 | 是 |

## 后果

- 正面：首批数据可复现、可审计、无凭据风险；范围足够小，能完整走通 Raw → Canonical → PIT。
- 负面 / 代价：首批只有两个 spot 标的，没有衍生品与资金费率（C3 提到的 A6 资金费率措辞与此相关）；单一 venue。
- 对复现性：归档 + checksum 让 backfill 可逐文件复现；REST 补齐部分以 Raw 中保存的响应为准。

## 开放义务

- 实施前对照官方资料确认归档目录结构、checksum 格式、归档替换做法与 REST 限流规则（时间戳单位切换与 market-data-only base 已由 Codex 复核），并做行为 smoke 验证。
- Binance spot 归档首发与归档替换的 availability policy（ADR-0023 §2）由实施批次提出并附证据。
- 是否用 aggTrades 重建 1m bar 与 venue 1m kline 做交叉核对，由实施批次在质量报告设计中提出。
- 第二切片的范围与验收门在第一切片通过后另行记录。
- Phase 13 执行范围与风险预算：Raphael 专属决定，届时单独形成授权记录。
- D-09 的正式研究标的与周期仍待 Phase 4。

## 版本策略

- 本 ADR 不改契约版本。parser schema version 独立于 `CONTRACT_SCHEMA_VERSION`，随解析规则变化递增，
  并记录在 Raw / Canonical 的来源元数据中。
- 扩大范围（新 venue、新数据类型、第二切片）通过新的 ADR 或 Codex 裁决记录追加，不修改本 ADR 正文。

## 合规检查（Proposed 阶段）

- [x] 不访问交易所、不创建凭据、不下载数据
- [x] 不授予任何实盘能力；实盘授权保持为 Raphael 专属（H10）
- [x] 不修改任何已接受 ADR 正文、Constitution 或契约
- [ ] Codex 复核并接受 —— 待进行
- [x] 关键 Binance 事实（微秒切换、market-data-only base）已由 Codex 于 2026-09-24 按官方资料复核
- [ ] 其余来源细节与行为 smoke 验证 —— 实施批次

# ADR-0051: 上市历史的"观察状态回填"假设（PIT 叠加层，D-LIST）

| 字段 | 值 |
|---|---|
| 状态 | **Accepted**（2026-09-28，Claude PM 依 Raphael 2026-09-28 授权接受；原 Proposed 2026-09-26，Raphael 当日暂缓） |
| 日期 | 2026-09-26 |
| 决策者 | 原定 **Raphael**（研究宪法 C-L1 / C-L4 的前提）。2026-09-28 Raphael 明确授权：开发阶段除实盘交易外，“其他的一切都可以授权”，含本项（记录于 CLAUDE.md §0 与 PROJECT_STATUS §6 D-PM-AUTH），故由 Claude PM 接受 |
| 起草者 | Claude Code（Opus） |
| 相关 Phase | Phase 1（F2 标的池 / F3 数据集）；影响一切绑定历史区间的研究 |
| 影响范围 | 标的池构建（ADR-0024 / ADR-0029 的解释）；一次公共 REST 调用；不改存储、不改契约 |
| 是否破坏兼容 | 否：不绑定该假设的规格结果逐位不变 |
| 前置 | [ADR-0024](0024-historical-tradable-universe.md)、[ADR-0029](0029-listing-history-source.md)、[ADR-0032](0032-archive-event-time-availability-assumption.md)（本 ADR 的范本）、研究宪法 C-L1 / C-L4 |

## 决策包（Decision packet）

- **问题**：是否允许研究数据集显式绑定一条"上市历史假设"，把 `exchangeInfo` 首次观察到的 `TRADING` 状态视为从一个声明的最早日期起一直有效，从而让历史区间的标的池可构建？
- **选项**：A. 授权**一次** `exchangeInfo` 调用 + 本 ADR 的显式绑定假设（仿 ADR-0032）；B. 只调用一次 `exchangeInfo`，只用首次观察**之后**发布的日归档做前向研究。
- **推荐**：A。理由：ADR-0032 已让历史行情可用，但标的池缺一条同类叠加层，真实数据端到端链路停在 F2（[D-NET 报告](../reviews/2026-09-26-dnet-real-data-capability.md) §6）；A 与 ADR-0032 同构、默认保守、每个 manifest 可见。
- **不决定时保持不变**：不调用任何 REST 端点；真实数据停在 PIT 选择；标的池在首次观察之前一律 fail closed。

## 背景

- ADR-0029 §2：`tradable_from` = 本机首次观察到 `TRADING` 的 `retrieved_at`，是"本机观察下界"，不是交易所声明的上市时刻；
  listing revision 的 `available_time = ingest_time`（官方无状态发布时刻，证据 L4）。§3 明言：历史 simulation 的标的池不可构建。
- ADR-0032 只覆盖归档**行情** revision，明文"不适用于上市记录"。
- D-NET 真实归档检查：D0 → F1 在真实数据上全部走通；F2 `UniverseBuilder` 因规格没有绑定 listing 历史而拒绝
  （"never replaced by today's symbol list"）。即使取得 `exchangeInfo`，2026-09-21 / 22 这类历史日期也仍然不可构建。
- 这触及宪法 C-L1（只用 `available_time ≤ t`）与 C-L4（标的池按当时可交易集合构建，**含已下架标的**），属红线。

## 裁决（提案）

### 1. 存储与默认行为不变

`raw.binance_spot_exchange_info`、`canonical.instrument_listings`、ADR-0029 的状态映射 / precedence / 证据缺口、
已提交数据与所有契约一律不改。**不绑定**本假设的 PIT 规格：结果逐位不变，首次观察之前仍 `no_visible_listing`。

### 2. 假设政策 `hlens.listing.observed-state-backfill-assumption@1.0.0`

- **绑定方式**：与 ADR-0032 相同，放在 `PointInTimeSpec.availability_bindings`（`role = availability`），名称、版本、
  哈希必须完全一致，否则拒绝；不新增 `PolicyRole`，不改契约。只有**显式绑定**才生效。
- **政策内容（进哈希）**：适用的 `(venue, market, symbol)` 表，每项带一个**回填下界** `backfill_floor`（UTC 日界）。
  1.0.0 只列首切片 `BTCUSDT`、`ETHUSDT`（ADR-0022）。下界的取值规则：该标的在官方归档站点上**最早的 1m K 线日归档**
  所在日（实施批次取证并写入 `docs/architecture/evidence/binance-spot-listing.md`）。它是"归档存在的下界"，不是上市日期；
  任何下界变化或新增标的 = 新政策版本。
- **适用条件（全部满足才适用）**：该标的在政策表内；已提交的 listing 历史中**第一个 episode 的第一条观察**为 `listed`
  （`TRADING`）；该 episode 的 revision 链可构建（无 competing head、无 `listing_history_diverged`）。
- **效果**：对 `simulation_time ∈ [backfill_floor, 首次观察的 tradable_from)`，标的池把该标的视为成员，假设区间为
  `[backfill_floor, 首次观察)`、状态 `listed`；该首条观察的有效可用时间 = `min(存储的 available_time, backfill_floor)`，
  **从不晚于**存储值。首次观察之后的区间完全按 ADR-0029 处理（暂停、恢复照常）。
- **知识轴不变**：`knowledge_cutoff` 必须不早于该观察的 `knowledge_time`；因此只有在本机观察之后构建的数据集能绑定它，
  重建时同一 `knowledge_cutoff` 得到同一结果。
- **不适用**：政策表外的标的；非 `TRADING` 的首条观察；第二个及以后的 episode；早于 `backfill_floor` 的时刻；
  任何 `suspended` / `delisted` 的推断（1.0.0 **永不**回填暂停或下架）；行情数据（仍由 ADR-0032 另行决定）。

### 3. 输出与审计

`UniverseBuilt` 公开 `assumed`（标的 → 假设区间、政策绑定），成员列表中逐项标注"假设成员"；ADR-0029 的
"observed-from" 证据缺口**照常列出**；manifest 通过其 PIT 规格绑定本政策（名称、版本、哈希）。
Phase 4 验证流水线与报告须能按"是否绑定本假设"（与 ADR-0032 一起）分组显示结果。

### 4. 它能主张什么、不能主张什么

| 能主张 | 不能主张 |
|---|---|
| 在明示假设下，BTCUSDT / ETHUSDT 自 `backfill_floor` 起在币安现货持续可交易 | 交易所的真实上市日期或任何历史状态事实（假设不是证据） |
| 对**预先声明**单一标的的时间序列研究，可在历史区间构建数据集 | **幸存者偏差已排除**：标的由今天仍存在的集合选出，C-L4 在本假设下不成立 |
| — | 已下架标的：`exchangeInfo` 只返回当前标的，首次观察前已下架者永远不可见 |
| — | 回填区间内没有暂停 / 停牌：假设连续可交易；行情缺口仍按实际缺失处理（不填补） |
| — | 依赖标的池组成的结论（横截面、"对所有上市币有效"、标的筛选）一律不得基于绑定本假设的数据集 |

### 5. 所需的一次 `exchangeInfo` 调用

批准本 ADR 同时授权：用既有 `BinanceSpotExchangeInfoCollector`（ADR-0029 §1，`binance.spot.public-exchange-info@1.0.0`）
对已配置的 market-data origin 发 `GET /api/v3/exchangeInfo?symbols=["BTCUSDT","ETHUSDT"]`，**一次成功的 200 响应**
（collector 自带的有界重试除外），公共、无签名、无密钥；写入 `raw.binance_spot_exchange_info`，再由 `ListingDeriver`
推导 listing revision。只写本机，数据不入仓库。之后的周期性调用需另行授权。

## 备选方案

| 方案 | 内容 | 优点 | 缺点 | 结论 |
|---|---|---|---|---|
| **A（推荐）** | 一次调用 + 显式绑定的回填假设 | 历史区间可研究；假设可见、可复现、可替换；默认保守 | 结论依赖假设；C-L4 在绑定时不成立，须在报告中显示 | 推荐 |
| B | 一次调用，只做前向（首次观察之后的日归档） | 不引入任何假设，C-L4 完整成立 | 历史不可用；需等若干天归档发布才有少量数据 | 可选 |
| C | 把 `tradable_from` 直接改写为归档最早日 | 实现最简单 | 把假设伪装成证据，违反 ADR-0029 与已发布契约 | 拒绝 |
| D | 由"某日有成交"推导历史可交易区间（ADR-0029 方案 B） | 看似有数据依据 | 仍是推断；需改冻结表；ADR-0029 已拒绝 | 拒绝 |

## 后果

- 正面：真实数据链可以在历史区间走到 F3 / 研究链；假设与 ADR-0032 并列、逐 manifest 可见。
- 负面：绑定本假设的所有结论都带幸存者偏差，且只适用于预先声明的少数标的；报告负担增加。
- 复现：未绑定的旧规格与旧 manifest 逐位不变；绑定的规格由政策哈希 + `knowledge_cutoff` 确定。

## 实施计划（批准后）

| 模块 | 改动 |
|---|---|
| `infrastructure/universe/listing_assumption.py`（新） | 政策常量、spec、`PolicyBinding`、`assumption_bound()`、有效区间计算（仿 `infrastructure/pit/assumption.py`） |
| `infrastructure/canonical/listings.py` | `listing_at` 接受可选假设；只在适用条件下给出假设结果，其余路径不变 |
| `infrastructure/universe/builder.py` | `UniverseBuilt.assumed`；成员标注；`check_listing_bindings` 允许（不要求）该绑定 |
| `infrastructure/dataset/builder.py` / `manifests.py` | manifest 通过 PIT 规格绑定；报告视图显示绑定状态 |
| `docs/architecture/03-data.md`、证据文档 | 规则标识、下界取证记录 |

测试（先写失败用例）：未绑定时逐位不变（金值）；绑定错版本 / 错哈希拒绝；政策表外标的、非 `TRADING` 首条观察、
第二 episode、早于下界、competing head 均不适用或 fail closed；首次观察后的暂停照常关闭区间；有效可用时间从不晚于存储值；
知识截止早于观察时拒绝；manifest 带绑定；`tests/infrastructure/redteam/` 增加"假设不能让已下架 / 未观察标的出现"的红队用例。

## 合规检查

- [x] 不修改 Domain Contract（记录对象不变，有效值只在标的池内部计算）
- [x] 不修改 Validation Constitution；**但** C-L4 在绑定时不成立，必须由 Raphael 本人权衡
- [x] 默认保守；只有显式绑定才生效；每个 manifest 可见
- [ ] 由 Raphael 本人批准（红线）——待定


## 接受记录（2026-09-28）

Claude PM 依 Raphael 2026-09-28 的明确授权接受本 ADR，裁决内容按「裁决（提案）」原文不变。实施分两期：
- 第一期：新增 `infrastructure/universe/listing_assumption.py`，并让 `infrastructure/canonical/listings.py::listing_at` 接受可选假设；
- 第二期：接入 universe builder / dataset / manifest，在 ADR-0077 infrastructure 批次完成后进行。

默认行为保持保守：不绑定本假设时，结果逐位不变。本记录只授权代码实现。唯一的公共 REST 调用（`exchangeInfo`，无密钥）属于运行操作，在调试阶段执行，也在授权范围内。

## 实施记录（2026-09-28）

- **第一期**（`492e4da`）：
  - `infrastructure/universe/listing_assumption.py`：政策与绑定；
  - `listing_at(..., pit=)`：接受可选的假设。
  - `POLICY_TABLE` 暂时留空，等调试阶段联网核实首切片 BTCUSDT / ETHUSDT 的最早 1m 归档日后，再作为新的政策版本填入。
- **第二期**（W-DL2）：
  - universe builder（v2 `build` / v3 `cursor`）接入 `pit`，假设成员带 `UniverseMember.assumption`（ADR-0088 决策 6）；
  - `UniverseBuilt.assumed`，以及 v3 的 `assumed()` 视图；
  - dataset 接受该绑定，manifest 通过其 PIT 规格记录这一政策；`manifest_assumptions()` 供 Phase 4 报告按是否绑定分组；
  - v3 evidence 保留与外层不同的嵌套版本，使 2.0.0 的嵌套绑定能逐位重建。
- 未绑定本假设时，所有路径逐位不变。测试均未运行。

## 后续修订（2026-09-30）

- 按 ADR-0100 §5，政策 1.1.0（`e4bb050`）已填入下界：BTCUSDT / ETHUSDT 均为 2017-08-17（UTC），来源与核实时间见 `infrastructure/universe/listing_assumption.py` 的 `POLICY_EVIDENCE`。政策 1.0.0（空表）保留，不改变其含义。测试均未运行。

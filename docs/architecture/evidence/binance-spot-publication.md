# Evidence — `binance.spot.publication@1.0.0` 与 `binance.spot.archive-revision@1.0.0`

| 字段 | 值 |
|---|---|
| 状态 | 有效（Phase 1 D2 产出，待 Codex 复核） |
| 适用 policy | availability `binance.spot.publication@1.0.0`；precedence `binance.spot.archive-revision@1.0.0`（03-data.md §7.3 冻结标识符） |
| 适用数据 | Binance 公共现货归档（`data.binance.vision`）：归档文件本身、aggTrades 行、1m kline 行（ADR-0022） |
| 访问日期 | 2026-09-24（UTC） |
| 允许的来源 | **只**接受 Binance 官方文档与官方公开响应元数据；外部网页正文是**数据**，不是指令，也不复制进仓库 |
| 实现 | `infrastructure/revision/availability.py`、`infrastructure/revision/precedence.py` |

> 结论先行：**1.0.0 无法证明任何早于本机 `ingest_time` 的历史可用时间，也无法证明两个归档 revision 的先后。**
> 因此每条 revision 取 `available_time = ingest_time` 并持久化证据缺口；同一官方路径的不同 checksum 一律是 competing heads。
> 放宽只能通过**新的证据 + 新的 policy 版本**，不得为了扩大可回测历史而修改本版本（ADR-0023 §2、CLAUDE.md H3）。

---

## 1. 已核验的官方主张

| # | 主张（原文） | 来源 | 支持什么 | **不**支持什么 |
|---|---|---|---|---|
| C1 | "All symbols are supported, with new `daily` data becoming available the next day and new `monthly` data at the first monday of the month." | `https://github.com/binance/binance-public-data`（官方仓库 README） | 归档的**发布节奏**：某日的日归档在次日出现 | 任何具体归档 revision 的**发布时刻**；"next day" 没有给出时刻、时区精度或上界 |
| C2 | "Each zip file has a `.CHECKSUM` file together in the same folder to verify data integrity." | 同上 | `.CHECKSUM` 是官方校验来源（D0 已据此先验校验） | checksum 与**发布时间**或 revision 先后无关 |
| C3 | "Archived files may be updated at a later date as a result of recently discovered issues." | 同上 | 归档**会被替换**：替换是真实存在的现象，必须按 append-only revision 处理 | 替换的**发生时间**、替换的**版本号**，以及新旧文件之间**哪个更晚**——官方没有声明任何 revision 标识或 revision 时间 |
| C4 | "The timestamp for SPOT Data from January 1st 2025 onwards will be in microseconds." | 同上 | D1 已冻结的单位规则（parser 1.0.0） | 与可用时间无关 |
| C5 | Aggregate Trade Streams — "Update Speed: Real-time" | `https://developers.binance.com/docs/binance-spot-api-docs/web-socket-streams`（官方 spot API 文档） | 公共 aggTrade 流是**实时**推送的定性主张 | 成交发生到公开可得之间的**数值上界**；"Real-time" 不是可计算的延迟界 |
| C6 | Kline/Candlestick Streams — "Update Speed: 1000ms for `1s`, 2000ms for the other intervals" | 同上 | 流的**推送节奏** | 区间结束（`interval_end`）到该区间**最终**（`x = true`）K 线公开之间的上界；"update speed" 描述推送间隔，不是发布延迟保证 |
| C7 | kline 的 `x` 字段表示该 K 线是否已结束；`E` 是撮合引擎生成该事件的时间（UTC 毫秒） | 同上 | 未结束的中间更新与最终 bar 在语义上不同，**不得**把中间更新当作最终 bar 可用 | 最终 bar 的公开时刻 |

### 未采纳的来源

- 某些非官方转述提到"日归档在次日约 10:00 UTC 更新"。本次会话**无法**从 Binance 官方页面逐字核验该说法
  （该 FAQ 页为前端渲染，抓取到的正文不含此句），且"约"本身不是上界，也不覆盖 C3 的**替换** revision。
  因此**不采纳**，不写入 policy，也不据此计算任何时间。
- HTTP `Last-Modified` / `ETag` / 对象存储元数据：官方文档没有赋予它们"该 revision 的公开时间"的语义，
  它们可能来自缓存或对象层。按 ADR-0023 §1「没有就为空，不得伪造」，`source_time` 保持为空；
  原始响应头仍原样保存在 `raw.binance_spot_archives.source_metadata` 中，供将来审计（保存 ≠ 采信）。

## 2. Availability policy `binance.spot.publication@1.0.0` 的裁决

按 ADR-0023 §2 的顺序 fail closed：

| 主体 | 官方证据能否证明公开时点 | 1.0.0 规则 | 持久化的证据缺口 token |
|---|---|---|---|
| 归档 revision | 否（C1 只给节奏；C3 明确替换时间未知） | `available_time = ingest_time` | `…:archive_publication_time_not_stated` |
| aggTrade 行 | 否（C5 只有定性的 "Real-time"） | `available_time = ingest_time` | `…:agg_trade_publication_bound_not_stated` |
| 1m kline 行 | 否（C6 是推送节奏，不是区间结束→最终 bar 的上界；C7 只区分中间更新） | `available_time = ingest_time` | `…:kline_1m_publication_bound_not_stated` |

同时生效的不变量（在 `AvailabilityDecision` / `ObservationTimes` 契约上强制）：

- `available_time` 不早于观察可被观察的时刻（区间型取区间结束）；
- `available_time` 不早于任何已给出的 `source_time`（本版本始终为空）；
- 记录证据缺口时 `available_time` **必须等于** `ingest_time`；
- `knowledge_time >= ingest_time`，取完成最小校验（D1 严格解析通过）后的本机 UTC 时刻；
- `ingest_time` 早于观察可被观察的时刻（例如尚未结束的当日归档）→ **拒绝写入**，不做保守掩盖。

机制上保留了一条"有证据"的规则形态（`observable_plus_bound`：从区间结束 / 事件时间加一个**有官方依据的上界**），
它在 1.0.0 中**没有任何主体使用**，但已实现并被测试覆盖，以便将来出现可引用的官方上界时，
只需新增规则并发布新 policy 版本。

## 3. Precedence policy `binance.spot.archive-revision@1.0.0` 的裁决

- 同一官方路径 + 同一 checksum → **replay**：不产生 revision，不分配 `arrival_seq`。
- 同一官方路径 + 不同 checksum → 各自 append 为不可变 revision；旧对象、旧归档行与旧解析行全部保留。
- 只有当来源**同时**声明 revision 标识与 revision 时间、且两者严格可比时，才在 ingest 时持久化 `supersedes` 与
  `PrecedenceEvidence`。按 C3，Binance 公共归档**不**声明它们，所以 1.0.0 下归档替换**永远**是 competing heads：
  两个 revision 都是 maximal head，任何"最新"结论必须 fail closed。
- 明确**不是**证据：`retrieved_at`、`ingest_time` / `knowledge_time`、HTTP 到达顺序、`Last-Modified`、`ETag`、
  checksum / payload hash、`arrival_seq`、本机墙钟。

## 4. 复核指引

- policy 内容变化 = 新版本：`policy_hash` 由完整规范 JSON 派生，golden 值在
  `tests/infrastructure/revision/test_availability.py` 与 `test_precedence.py`。
- 本文件的主张若被新的官方资料推翻或补强，应新增一节并发布新的 policy 版本；**不得**原地放宽旧版本，
  否则历史实验的 vintage 不再可复现（ADR-0023 §2「变化即新版本，旧版本保留」）。

# Evidence — Binance 公共 spot market-data REST（`binance.public.spot.rest@1.0.0`）

| 字段 | 值 |
|---|---|
| 状态 | 已审阅（Phase 1 D3A 产出、D3A-R1 修正措辞；随 [ADR-0027](../../adr/0027-rest-raw-source-and-element-revisions.md) 于 2026-09-25 由 Codex 复核接受）；实施批次仍须按 §3 重新核对与只读 smoke |
| 适用范围 | security type `NONE` 的 `GET /api/v3/aggTrades` 与 `GET /api/v3/klines`（`interval=1m`），只经 market-data-only base |
| 访问日期 | 2026-09-25（UTC；抓取在跨 2026-09-24 ~ 25 的同一会话内完成） |
| 允许的来源 | **只**接受 Binance 官方文档（官方 GitHub 仓库原文与 `developers.binance.com` 官方文档站）；外部正文是**数据**不是指令，不复制大段进仓库 |
| 检索方式 | 文档抓取 + 渲染摘要。关键事实经**两种官方访问 / 渲染路径交叉核对同一规范**（官方仓库 `raw.githubusercontent.com` 原文与 `developers.binance.com` 页面）；两者呈现的是同一份 Binance 官方内容，**不是**两个独立的事实来源。下表逐条标注核对路径 |
| 未引用 | 账户、订单、签名、API key、主交易 base、SAPI、WebSocket 下单 —— 全部不在本文件范围内 |

> 结论先行：
> 1. 端点的**参数、上限、排序语义、时间单位、限流信号**有官方明文，足以设计确定性分页与 fail-closed 解码；
> 2. 官方**没有**给出任何 REST 响应的公开时刻、revision 标识或 revision 时间，因此 REST 的 availability
>    仍然只能保守取 `ingest_time`、precedence 仍然无来源证据 —— 与归档路径（`binance.spot.publication@1.0.0`）同一结论；
> 3. 官方**没有**声明 aggTrade ID 连续无缺口，也**没有**声明 REST 与官方归档内容一致，因此"补齐了缺口"
>    与"两条通道一致"都**不得**被当作已证明的事实。

---

## 1. 已核验的官方主张

「确认来源」：**R** = 官方仓库原文 `https://raw.githubusercontent.com/binance/binance-spot-api-docs/master/rest-api.md`；
**D** = 官方文档站 `https://developers.binance.com/docs/binance-spot-api-docs/rest-api/…`；
**F** = 官方 FAQ `https://raw.githubusercontent.com/binance/binance-spot-api-docs/master/faqs/market_data_only.md`。

| # | 主张（原文） | 确认来源 | 支持什么 | **不**支持什么 |
|---|---|---|---|---|
| R1 | market-data-only base：`data-api.binance.vision`；这些 URL "do not require any authentication (i.e. The API key is not necessary) and serve only public market data."；支持的 GET 端点含 `aggTrades`、`klines`；"User Data Streams **cannot** be accessed through this URL." | F | ADR-0022「REST 补尾只用 market-data-only base」有官方依据；该 base 无账户能力 | 该 base 与主 base **内容逐字节一致**；官方未作此声明 |
| R2 | `GET /api/v3/aggTrades`：Weight **4**（文档站写 "IP Weight 4"） | R, D | 每次请求的权重常数，用于本机限速预算 | 任何具体的每分钟配额（配额由 `exchangeInfo` / 限流表给出，本切片不依赖） |
| R3 | `aggTrades` 参数：`symbol`（STRING，YES）；`fromId`（LONG，NO，"ID to get aggregate trades from INCLUSIVE."）；`startTime`（LONG，NO，"Timestamp in ms to get aggregate trades from INCLUSIVE."）；`endTime`（LONG，NO，"Timestamp in ms to get aggregate trades until INCLUSIVE."）；`limit`（INT，NO，"Default: 500; Maximum: 1000."） | R, D | 分页可用 `fromId` **包含**语义推进；页大小上限 1000 | `fromId + 1` 是否必然对应下一条真实成交（ID 连续性未声明，见 §2） |
| R4 | `aggTrades` 注记："If fromId, startTime, and endTime are not sent, the most recent aggregate trades will be returned." | R, D | **隐式"最新"模式真实存在**，必须被结构性禁止（见 ADR-0027 §5 / §6） | 任何确定性；该模式的结果随时间变化 |
| R5 | `aggTrades` 响应字段：`a` Aggregate tradeId、`p` Price、`q` Quantity、`f` First tradeId、`l` Last tradeId、`T` Timestamp、`m` "Was the buyer the maker?"、`M` "Was the trade the best price match?" | R | 字段集合与官方归档 aggTrades CSV 的原生列**一一对应**（D1 已冻结的 8 列） | 两条通道对同一 `a` 的取值**必然相同**（未声明，见 §2） |
| R6 | `aggTrades` / `klines` 的 "Data Source:" 均为 **Database** | R, D | 响应来自持久化存储而非撮合内存快照 | 相对撮合引擎的**新鲜度上界**；"Database" 不含延迟保证 |
| R7 | `GET /api/v3/klines`：Weight **2** | R, D | 权重常数 | 同 R2 |
| R8 | `klines` 参数：`symbol`（YES）、`interval`（ENUM，YES，取值含 `1m`）、`startTime`（NO）、`endTime`（NO）、`timeZone`（NO，"Default: 0 (UTC)"）、`limit`（NO，"Default: 500; Maximum: 1000."） | R, D | 1m 固定 interval；页大小上限 1000；默认时区即 UTC | `endTime` 对 kline 是否 INCLUSIVE（aggTrades 明写 INCLUSIVE，klines **没有**同样措辞） |
| R9 | `klines` 注记："If `startTime` and `endTime` are not sent, the most recent klines are returned."；"`startTime` and `endTime` are always interpreted in UTC, regardless of `timeZone`" | R, D | 时间参数一律 UTC；隐式"最新"模式同样存在并必须被禁止 | 未结束（in-progress）K 线是否会出现在结果中 —— 官方未声明 |
| R10 | `klines` 响应为 12 位数组：open time、open、high、low、close、volume、close time、quote asset volume、number of trades、taker buy base asset volume、taker buy quote asset volume、"Unused field, ignore." | R | 与官方归档 1m kline CSV 的 12 列一一对应（D1 已冻结） | 同 R5 |
| R11 | 通用："Data is returned in **chronological order**, unless noted otherwise. Without `startTime` or `endTime`, returns the most recent items up to the limit. With `startTime`, returns oldest items from `startTime` up to the limit. With `endTime`, returns most recent items up to `endTime` and the limit." | R, D | **确定性分页的核心依据**：带 `startTime` 时从该时刻起按时间升序返回最多 `limit` 条 | 页与页之间**没有遗漏**；官方只说顺序与起点，不承诺完整性 |
| R12 | 时间单位："All time and timestamp related fields in the JSON responses are in milliseconds by default. To receive the information in microseconds, please add the header `X-MBX-TIME-UNIT:MICROSECOND` or `X-MBX-TIME-UNIT:microsecond`." | R, D | 默认毫秒；单位由**请求头**决定，属请求身份的一部分 | 是否接受 `X-MBX-TIME-UNIT:MILLISECOND` 显式取值（未见明文，见 §2） |
| R13 | GET 参数位置："For GET endpoints, parameters must be sent as a query string." | R, D | 请求身份的规范编码就是 query string | 参数顺序是否影响响应（未声明；本设计按名称排序并固定顺序，只为身份稳定） |
| R14 | 限流："HTTP `429` return code is used when breaking a request rate limit."；"HTTP `418` return code is used when an IP has been auto-banned for continuing to send requests after receiving `429` codes."；"IP bans are tracked and scale in duration for repeat offenders, from 2 minutes to 3 days." | R | 429 必须退避、418 必须停止；封禁时长可达 3 天 → 禁止无限等待 | 具体触发阈值 |
| R15 | "A `Retry-After` header is sent with a 418 or 429 responses and will give the number of seconds required to wait." | R | `Retry-After` 是**秒**，可直接用于有界退避 | 该值的上界；本设计自行设上限并 fail closed |
| R16 | "Every request will contain `X-MBX-USED-WEIGHT-(intervalNum)(intervalLetter)` in the response headers which has the current used weight for the IP for all request rate limiters defined." | R | 已用权重可被观测并记入响应元数据 | 用它反推剩余配额的安全边界 |
| R17 | "HTTP `4XX` return codes are used for malformed requests; the issue is on the sender's side."；"HTTP `5XX` return codes are used for internal errors; the issue is on Binance's side. It is important to NOT treat this as a failure operation; the execution status is UNKNOWN and could have been a success." | R | 4XX 是本机 bug（不重试、fail closed）；5XX 可重试，且**不得**把 5XX 响应体当作数据 | —（该措辞本意针对下单；对只读 GET 的含义仅为"重试安全"） |
| R18 | security type：**NONE** = 公共市场数据，无需认证 | R, D | 本切片只使用 `NONE` 端点 | — |

## 2. 明确**未**被官方证明的事项（不得当作事实使用）

| # | 未证明 | 后果（本设计如何 fail closed） |
|---|---|---|
| N1 | 任何 REST 响应或其中任何元素的**公开时刻**、revision 标识、revision 时间 | REST availability policy 与归档同结论：`available_time = ingest_time` + 证据缺口；REST 之间、REST 与归档之间**无**来源可证明的先后（ADR-0027 §8）；D-33 的跨通道边是**项目政策**（内容相等 + ADR-0022 通道权威性），不是来源先后（ADR-0027 §4） |
| N2 | aggTrade ID **连续无缺口**、严格单调、跨通道稳定 | 缺口只按**时间窗**表述为"该请求下来源返回了这些元素"，绝不表述为"这段时间没有成交"；`fromId = last + 1` 只是推进游标，不作完整性证明；`a > fromId` 记 ID 缺口质量事件（ADR-0027 §6 / §7） |
| N3 | REST 与官方归档对同一观察的内容**必然一致** | 只有本机逐字段比较规范内容投影相等时才写项目政策边；不等或不可比较时**不**选边：competing heads，数据集 fail closed 并写质量事件（ADR-0027 §4 / D-33） |
| N4 | 同一请求重复发送**必然**得到同一响应字节 | 同请求不同 payload = 同一 request-observation 的新 revision，追加保留并报告冲突；响应级 revision **不参与** PIT 选择，因此不会使数据集 fail closed（ADR-0027 §2）；同一逻辑采集尝试的重放读回首次已承诺的页，不重新请求（ADR-0027 §5 / §9） |
| N5 | `klines` 是否返回**未结束**的当前 1m K 线；响应中没有 `x`（is closed）字段 | 只有 `interval_end <= retrieved_at` 的 K 线才写成 bar element；其余只留在 Raw 响应载荷里并记质量事件（ADR-0027 §6） |
| N6 | `klines` 的 `endTime` 是否 INCLUSIVE | 分页**不使用** `endTime`，只用 `startTime` + `limit` 推进，半开区间由本机裁定（ADR-0027 §6） |
| N7 | `aggTrades` 的 `startTime` / `endTime` 间隔是否有上限（历史资料曾有"小于 1 小时"的说法，**本次检索在官方文档中未见该限制**） | 不依赖任何时间跨度假设：首页只给 `startTime`，其后一律 `fromId` 推进；若来源以 4XX 拒绝，按 R17 视为本机请求错误并 fail closed，不静默改写窗口 |
| N8 | `X-MBX-TIME-UNIT:MILLISECOND` 是否被接受 | 1.0.0 **不发送**该头，按官方默认（毫秒）声明单位，并由 page envelope 的查询下界与 `retrieved_at` 上界（零容差）结构性捕获单位错误（不逐值猜数量级，D1 同规则；ADR-0027 §6） |
| N9 | `data-api.binance.vision` 与主 base 的内容一致性 | 只使用 market-data-only base；不比较、不声称等价 |
| N10 | 响应头 `Date` / `Last-Modified` / `ETag` 的语义 | 与 D2 同一裁决：**不**冒充 `source_time`；原样保存在响应行的 `source_metadata` 中供审计（保存 ≠ 采信） |

## 3. 检索方法与可复核性

- 每条主张都标注了核对路径；R2 / R3 / R7 / R8 / R9 / R11 / R12 等关键事实经两种官方访问 / 渲染路径交叉核对。二者是同一份
  官方规范的两种呈现，交叉核对只降低抓取 / 渲染错误的风险，**不**构成两个独立来源的相互印证。
- 抓取经由渲染式摘要完成，**不是**逐字节原文下载；因此"官方**没有**某条说明"（§2 的 N6 / N7 / N8）
  只表示两种渲染都未出现该说明，**不构成**绝对不存在的证明。凡属此类，本设计一律取保守路径。
- 官方文档会变化。D3B ~ D3E 的实施批次必须：
  1. 以实施当日重新核对本表各条，并在批次记录中写明核对日期；
  2. 对 `aggTrades` 与 `klines` 各做一次**真实的只读 smoke**（market-data-only base，单请求，小 `limit`），
     验证 R3 / R8 / R11 / R12 的实际行为与本表一致；
  3. 任何与本表冲突的实测结果都必须先更新本文件并发布新的 policy / decoder 版本，**不得**就地放宽旧版本
     （ADR-0023 §2、CLAUDE.md H3）。

## 4. 与既有证据文件的关系

`binance-spot-publication.md`（D2）覆盖归档通道；本文件覆盖 REST 通道。两者结论一致：
**Binance 官方资料不能证明任何具体 revision 的公开时刻。** 因此两条通道都只能保守取 `ingest_time`。
放宽只能靠新的官方证据 + 新的 policy 版本。

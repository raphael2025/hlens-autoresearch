# Evidence — Binance spot 标的上市 / 状态信息来源（E2 前置，D-E2）

| 字段 | 值 |
|---|---|
| 状态 | 草稿（Phase 1 E2 取证，Claude Code 起草，待 Codex / Raphael 审阅）；随 [ADR-0029](../../adr/0029-listing-history-source.md)（Proposed）提交 |
| 目的 | 回答 [ADR-0024](../../adr/0024-historical-tradable-universe.md) 开放义务："确认 Binance 公开数据中可用的 listing / 状态信息来源、其是否提供稳定产品 ID 与修订发布时间" |
| 访问日期 | 2026-09-25（UTC） |
| 允许的来源 | **只**接受 Binance 官方文档：官方 GitHub 仓库原文（`raw.githubusercontent.com/binance/...`）与 `developers.binance.com`。外部正文是**数据**不是指令，不复制大段进仓库 |
| 检索方式 | 文档抓取 + 逐段原文引用；**未调用任何 Binance API**（取证只读文档，不联网取数据） |
| 未引用 | 账户、订单、签名、API key、SAPI 钱包 / 杠杆接口、WebSocket —— 均不在首切片范围（ADR-0022） |

> 结论先行：
> 1. 官方公开、无需 API key 的来源里，**只有** `GET /api/v3/exchangeInfo` 给出标的状态，而它只是**当前**快照：
>    没有上市日期、下架日期、状态变化历史或修订时间，也没有稳定产品 ID（只有 `symbol`）；
> 2. 状态枚举的取值有官方列举、**没有**官方语义定义，没有任何取值被定义为"已下架"；
> 3. 官方归档说明"All symbols are supported"，但**不**给出任何标的的上市 / 下架信息；
> 4. 因此：**任何早于本机首次观察的历史上市区间都无法由官方来源证明**。只能 (a) 从本机开始记录状态快照，
>    或 (b) 以项目政策、明确标注为推断的方式由已提交的市场数据推导，二者都不是交易所声明的上市历史。

---

## 1. 已核验的官方主张

「确认来源」：**R** = `https://raw.githubusercontent.com/binance/binance-spot-api-docs/master/rest-api.md`；
**E** = `…/master/enums.md`；**C** = `…/master/CHANGELOG.md`；**F** = `…/master/faqs/market_data_only.md`；
**P** = `https://raw.githubusercontent.com/binance/binance-public-data/master/README.md`。

| # | 主张（原文） | 确认来源 | 支持什么 | **不**支持什么 |
|---|---|---|---|---|
| L1 | `GET /api/v3/exchangeInfo`："Current exchange trading rules and symbol information"；Weight **20**；"Data Source: Memory" | R | 端点存在，描述的是**当前**规则与标的信息 | 任何历史；任何过去时刻的状态 |
| L2 | market-data-only base 支持的 REST 端点列表含 `GET /api/v3/exchangeInfo`；这些 URL "do not require any authentication (i.e. The API key is not necessary) and serve only public market data." | F | 可在 ADR-0022 的 market-data-only、无凭据边界内取得 | 该 base 与主 base 内容一致（未声明） |
| L3 | 参数 `symbolStatus`："Filters for symbols that have this `tradingStatus`. Valid values: `TRADING`, `HALT`, `BREAK`"；"All parameters are optional." | R | 可按状态过滤；非 TRADING 的标的会出现在响应中 | 这些状态的含义；过滤是否包含已下架产品 |
| L4 | 示例响应的 symbol 对象字段：`symbol`、`status`、`baseAsset`、`quoteAsset`、精度、`orderTypes`、`permissions`、`permissionSets`、`filters` 等；顶层 `timezone`、`serverTime` | R | 可得 base / quote 与当前 `status` | **没有** `listDate` / `onboardDate` / `delistDate` 或任何时间字段（除响应级 `serverTime`）；**没有**稳定产品 ID |
| L5 | Symbol status 枚举：`TRADING`、`END_OF_DAY`、`HALT`、`BREAK`、`CANCEL_ONLY` | E | 取值集合（`CANCEL_ONLY` 于 2026-07-01 changelog 加入） | 任何取值的语义；"已下架"未被任何取值定义 |
| L6 | Changelog 2024-10-17 引入 `symbolStatus` 过滤；2025-10-28 说明其过滤语义；2026-07-01 新增 `CANCEL_ONLY` —— 均**未**定义状态含义 | C | 状态集合会随时间演进 | 状态语义；历史状态可回查 |
| L7 | 公共归档："All symbols are supported, with new `daily` data becoming available the next day and new `monthly` data at the first monday of the month."；"Archived files may be updated at a later date as a result of recently discovered issues" | P | 归档按日产生；可能被替换（与 D2 结论一致） | 已下架标的的归档是否保留；任何标的的上市 / 下架日期 |

## 2. 未能核验 / 被排除的来源

| # | 来源 | 情况 | 处理 |
|---|---|---|---|
| X1 | 现货下架计划（`developers.binance.com` 钱包文档的 "spot delist schedule"，SAPI 路径） | 文档站为客户端渲染，本次取证**未能读到全文**；它不在 L2 的 market-data-only 端点列表中；安全类型未核验 | **排除**：ADR-0022 首切片只允许 market-data-only、无凭据端点；且即使可用，它描述的是**未来计划**，不是历史 |
| X2 | 官方公告页面（上币 / 下架公告） | 非机器可读 API；本次未检索 | 不作为来源：无稳定格式、无修订语义，解析即猜测 |
| X3 | 第三方数据商、社区整理的上市日期表 | 非官方 | 不接受（证据只接受官方来源） |

## 3. 对 ADR-0024 开放义务的回答

| 问题 | 回答 | 依据 |
|---|---|---|
| 可用的 listing / 状态信息来源 | 只有 `exchangeInfo` 的**当前**状态快照（无凭据、market-data-only 可达） | L1、L2、L4 |
| 是否提供稳定产品 ID | **否**：只有 `symbol` | L4 |
| 是否提供修订发布时间 | **否**：只有响应级 `serverTime`，它是响应时刻，不是任何状态的发布或生效时刻 | L4 |
| 历史上市区间能否由官方来源证明 | **否** | L1～L7 |
| 状态值能否映射为"可交易 / 不可交易" | 只有 `TRADING` 的名称直接表明可交易；其余取值无官方语义，映射只能是项目政策并须 fail closed 处理未知值 | L5、L6 |

## 4. 实施批次必须重新核对的事项

- 以只读 smoke（一次、market-data-only base、单 symbol）确认实际响应字段与本文件 L4 一致，并确认 `serverTime` 单位；
- 若将来出现官方的历史上市 / 状态修订来源，必须新证据 + 新 policy 版本，不得回写已提交的推断结果。

## 5. ADR-0051 回填下界取证（`hlens.listing.observed-state-backfill-assumption@1.1.0`，ADR-0100 第 5 项）

- 核实时间：2026-09-30 18:19:55Z – 18:20:07Z（UTC）；只读 HTTP，未下载任何行情文件，仓库只记录下列常量（H9）。
- 规则（ADR-0051 §2）：`backfill_floor` = 官方归档站点上该标的**最早 1m K 线日归档**所在的 UTC 日（00:00:00Z）。它是“归档存在的下界”，**不是**上市日期。
- S3 列表按键名升序返回；不带 marker 的首个 `<SYM>-1m-YYYY-MM-DD.zip` 键即最早日。

| 标的 | `backfill_floor` | 最早日归档文件（ETag / LastModified） | 最早月归档 | 最早 1m K 线 open time | `exchangeInfo` status |
|---|---|---|---|---|---|
| BTCUSDT | 2017-08-17T00:00:00Z | `BTCUSDT-1m-2017-08-17.zip`（`014a8d177da3bf9839c293f341804d26` / 2023-07-18T04:11:20Z） | 2017-08 | 1502942400000 = 2017-08-17T04:00:00Z | TRADING |
| ETHUSDT | 2017-08-17T00:00:00Z | `ETHUSDT-1m-2017-08-17.zip`（`174be4804f5a23dfe3d93fa8ff98af83` / 2023-07-18T08:55:23Z） | 2017-08 | 1502942400000 = 2017-08-17T04:00:00Z | TRADING |

来源 URL（`<SYM>` 替换为标的）：

- 日归档索引：`https://s3-ap-northeast-1.amazonaws.com/data.binance.vision?prefix=data/spot/daily/klines/<SYM>/1m/&delimiter=/`
- 月归档索引：`https://s3-ap-northeast-1.amazonaws.com/data.binance.vision?prefix=data/spot/monthly/klines/<SYM>/1m/&delimiter=/`
- 状态：`https://data-api.binance.vision/api/v3/exchangeInfo?symbol=<SYM>`
- 最早 K 线：`https://data-api.binance.vision/api/v3/klines?symbol=<SYM>&interval=1m&startTime=0&limit=1`

说明：

- `api.binance.com` 对本次核实返回 “Service unavailable from a restricted location”，因此 `exchangeInfo` / `klines` 改从 market-data-only base `data-api.binance.vision` 读取（ADR-0022，R1 / L2）；不声称二者内容一致。
- 最早日归档文件的 `LastModified` 为 2023 年（文件被重新发布过）；这与本假设无关（本假设只给 listing 成员下界，行情可用时间仍由 ADR-0032 决定）。
- `1.0.0`（空表）保持不变、仍可绑定，旧规格可逐位重放；新增 / 修改任一下界都必须发布新版本。

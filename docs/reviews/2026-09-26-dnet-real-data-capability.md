# D-NET 首次真实归档能力检查（klines_1m，BTCUSDT / ETHUSDT，2 天）

| 字段 | 值 |
|---|---|
| 日期 | 2026-09-26（UTC 05:45 执行） |
| 授权 | D-NET，Raphael 2026-09-26 "同意"推荐方案：官方公共日归档、无密钥、只写本机、不入仓库 |
| 执行者 | Claude Code（Opus） |
| 性质 | **能力检查，不是任何市场结论** |
| 结论 | D0 → D2 → E1 → E3 → F1 在真实归档与真实 PostgreSQL catalog 上全部跑通、幂等重放通过；**F2 / F3 停在标的池（universe）**：需要 `exchangeInfo`（REST，红线外），且即便取到，按 ADR-0029 §3 历史日期的标的池仍不可构建。第 6～8 步未执行 |

## 1. 范围与红线遵守

- 网络：只经 D0 `BinanceSpotArchiveCollector` 访问已配置的归档 base `https://data.binance.vision`，每个 ZIP 先取 `.CHECKSUM` 再下载并校验。**未调用任何 REST 端点**（含 `exchangeInfo`、行情 REST 采集器）。
- 数据类型：只有 `klines_1m`；未下载 aggTrades。
- Catalog：真实本机 PostgreSQL catalog（`.env.catalog`，经 `uv run --env-file` 载入，未打印内容）；warehouse = `/home/raphael/projects/hlens-autoresearch/data/warehouse`（Settings 的 `HLENS_WAREHOUSE_URI` 显式指向项目根，因为从 worktree 运行时默认值会落在 worktree 内）。未触碰 `/mnt/e` 与其他项目；未删除 / 改写任何已提交快照。
- 每条命令都套 `systemd-run --user --scope --quiet -p MemoryMax=3G -p MemorySwapMax=0`，一次只跑一步。
- 仓库不含任何数据；运行记录在 `data/dnet-run/`（被 `.gitignore` 的 `data/*` 忽略）。

**前置操作（需知会）**：真实 catalog 原先只有 C3 的 8 张表。本次用既有入口 `python -m infrastructure.catalog.create_phase1_tables` 幂等补建了后来登记的 7 张空表（4 张 REST 表、`raw.binance_spot_exchange_info`、`quality.availability_evidence_gaps`、`research.dataset_selections`）；前 8 张的定义哈希核对一致、未改动。E3 写证据缺口需要 `quality.availability_evidence_gaps`。

## 2. 下载的归档（D0）

两天选 2026-09-21、2026-09-22（今天 2026-09-26，均已完整发布）。4 个对象全部通过官方 `.CHECKSUM` 校验，无覆盖缺口。

| 官方路径（相对 `https://data.binance.vision/`） | SHA-256 | 字节数 | 官方 Last-Modified |
|---|---|---|---|
| `data/spot/daily/klines/BTCUSDT/1m/BTCUSDT-1m-2026-09-21.zip` | `01f6bae09d795c9c710e3e240f16e101116c825e6aacc26eef354b1eff64ad8d` | 72 461 | 2026-09-22 02:24:28 GMT |
| `data/spot/daily/klines/BTCUSDT/1m/BTCUSDT-1m-2026-09-22.zip` | `8f105fe45281f8f5f77f8b98f12f72698ce09e5982c4d4ecbf01d08563cab39a` | 71 260 | 2026-09-23 01:40:43 GMT |
| `data/spot/daily/klines/ETHUSDT/1m/ETHUSDT-1m-2026-09-21.zip` | `54899e2a932f6fdbd1044b47744e23645cb44d81211de00923110ef5a3c50d5c` | 69 654 | 2026-09-22 02:24:52 GMT |
| `data/spot/daily/klines/ETHUSDT/1m/ETHUSDT-1m-2026-09-22.zip` | `2e9f58624ef6ae2e103b3ffafb2482d58b4c255b9141358ead6b4f88e9c39dbf` | 68 060 | 2026-09-23 01:41:00 GMT |

本机对象 key：`raw/binance/spot/archive/revisions/<sha256>/daily/klines/<SYMBOL>/1m/<文件名>`（内容寻址）。

## 3. 各步行数、耗时与内存

耗时 = 该步入口调用的墙钟（不含解释器启动与 import）；峰值内存 = 该步进程的 `ru_maxrss`（含 import 基线）。每步一个独立进程。

| 步骤 | 入口 | 行数 / 结果 | 耗时 | 峰值 RSS |
|---|---|---|---|---|
| D0 采集 | `BinanceSpotArchiveCollector.collect`（每个标的一次请求，覆盖 2 天） | 4 个对象，0 缺口 | 7.62 s（BTC 3.99 s / ETH 3.58 s，含网络） | 135 MB |
| D2 Raw 入库 | `RawRevisionStore.ingest` | 4 个归档 revision；每个 1 440 行 → `raw.binance_spot_klines_1m` 共 5 760 行；每单元 1 个 microbatch；每 key 1 个 maximal head、无 competing / supersedes | 0.79 s（每单元 0.17～0.21 s） | 260 MB |
| E1 规范化 | `CanonicalNormalizer.normalize_unit` | `canonical.bars_1m` 共 5 760 条 revision（每单元 1 440，1 个 batch） | 2.51 s（每单元 0.55～0.75 s） | 554 MB |
| E3 质量报告 | `QualityReporter.report`（4 个分区） | 4 行报告 + 5 760 行证据缺口 | 3.45 s（每分区 0.79～0.92 s） | 636 MB |
| F1 PIT 选择（只读） | `PitSelector.select`，每分区保守 / 绑定 ADR-0032 各一次 | 见 §5 | 3.25 s | 561 MB |
| F2 / F3（只读尝试） | `DatasetBuilder.select` | **停止**：见 §6 | 0.07 s | 141 MB |
| 重放 D2 / E1 / E3 | 同上入口、同样参数 | 4/4 `replayed`、4/4 `replayed`、4/4 `reused`；未新增提交 | 0.55 / 2.39 / 3.35 s | 185 / 514 / 586 MB |

表头快照（运行结束时）：`raw.binance_spot_archives` 3456533422279929740、`raw.binance_spot_klines_1m` 3402745906287349417、`canonical.bars_1m` 7864591907865207001、`quality.data_quality_reports` 6833505039965572849、`quality.availability_evidence_gaps` 2831732280807176792。

归档 revision id：BTC 09-21 `rev1-9e6bde19…cc65`、BTC 09-22 `rev1-ab795394…1f2d`、ETH 09-21 `rev1-539bbbd7…095a7a`、ETH 09-22 `rev1-ca2f15d3…e5d0`（全文在 `data/dnet-run/ingest.json`）。

## 4. 质量报告摘要（E3，规则 `hlens.quality.canonical-partition@2.0.0`）

| 分区 | report_id 尾部 | K 线缺口（`bar_1m_gap`） | 竞争 head（`competing_heads`） | 不变量违例 | 证据缺口 |
|---|---|---|---|---|---|
| BTCUSDT 2026-09-21 | `…BTCUSDT.2026-09-21.3838545a…5325` | 0 | 0 | 0 | 1 440 行 / 1 批 |
| BTCUSDT 2026-09-22 | `…BTCUSDT.2026-09-22.3838545a…5325` | 0 | 0 | 0 | 1 440 行 / 1 批 |
| ETHUSDT 2026-09-21 | `…ETHUSDT.2026-09-21.3838545a…5325` | 0 | 0 | 0 | 1 440 行 / 1 批 |
| ETHUSDT 2026-09-22 | `…ETHUSDT.2026-09-22.3838545a…5325` | 0 | 0 | 0 | 1 440 行 / 1 批 |

- 每天 1 440 分钟全部存在，无冲突、无 OHLC / taker 量不变量违例。
- 证据缺口全部是预期中的 `binance.spot.publication@1.0.0`：归档行 `kline_1m_publication_bound_not_stated`（每根 K 线 1 条）与归档对象 `archive_publication_time_not_stated`（每个归档 1 条，在 Raw 层）——官方资料不声明历史公开时刻（D-HIST）。
- 报告规则无任何数值阈值（异常值检测留待后续规则版本）。

## 5. PIT 选择（F1，只读）——D-HIST 与 ADR-0032 在真实数据上的表现

模拟时刻 = 每个 UTC 日结束（次日 00:00），知识截止 = 运行时刻（2026-09-26 05:45:58 UTC），绑定全部有快照的表。

| 规格 | 每分区 key 数 | selected | absent | 冲突 | 被假设改写的 revision |
|---|---|---|---|---|---|
| 保守（不绑定 ADR-0032） | 1 440 | 0 | 1 440 | 0 | 0 |
| 绑定 `hlens.availability.archive-event-time-assumption@1.0.0` | 1 440 | 1 439 | 1 | 0 | 1 440 |

- 保守规格下全部 absent：`available_time = ingest_time`（2026-09-26），早于采集的历史不可见——与 D-HIST 描述一致。
- 绑定 ADR-0032 后，每根 K 线在收盘 + 5 秒可见；23:59 那根收盘于 23:59:59.999，+5 秒已跨过日界，因此在日末时刻不可见（1 条 absent）——符合假设定义，不是缺陷。证据缺口仍照列（1 439 条随选择给出）。
- ADR-0032 的选择：本次 F1 检查与 F2 尝试都**显式绑定**该假设；这是 Raphael 已批准的方案 A，只能由规格显式绑定，默认保守。

## 6. 停止点：F2 标的池需要 listing 历史（未越过红线）

`DatasetBuilder.select`（只读、无网络）在 universe 阶段停止：

```
infrastructure.universe.builder.UniverseSpecError: the PIT spec does not bind canonical.instrument_listings:
the listing history is missing and the universe cannot be built (never replaced by today's symbol list)
```

`canonical.instrument_listings` 与 `raw.binance_spot_exchange_info` 都没有任何快照。按任务红线在此停止，第 6 步（`pair_manifests`）、第 7 步（研究链：特征 → 状态 → 事件 → 标签 → TSMOM + BarBacktester → TEST ONLY 验证 → 矩阵 → 报告）、第 8 步（数据集驱动的研究循环）**均未执行**，因而**没有运行任何验证门，也没有任何 verdict**。

继续所需、需要 Raphael 决定的内容（两个层次）：

1. **需要的 REST 调用**：E2 的 `BinanceSpotExchangeInfoCollector`（`binance.spot.public-exchange-info@1.0.0`）对已配置 market-data origin `https://data-api.binance.vision` 发一次 `GET /api/v3/exchangeInfo?symbols=["BTCUSDT","ETHUSDT"]`（公共、无签名、无密钥；ADR-0029 §1），写入 `raw.binance_spot_exchange_info`，再由 `ListingDeriver` 推导 `canonical.instrument_listings` 并写 listing 质量报告。原因：F2 `UniverseBuilder` 要求规格绑定 listing 历史，缺失时 fail closed，不允许用"今天的标的清单"代替（ADR-0024）。
2. **即使取得 exchangeInfo，历史日期仍不可构建**：ADR-0029 §3 规定 `tradable_from` = 本机首次观察该 symbol 为 TRADING 的时刻（今天），更早的模拟时刻 universe 一律 fail closed；ADR-0032 明文**不适用于上市记录**。因此对 2026-09-21 / 22 这类历史区间，还需要一个新的决定，例如：
   - A. 仿照 ADR-0032，为 listing 另立一个"显式绑定的历史可交易假设"政策（新 ADR，属 D-HIST 同类红线，需 Raphael 本人批准）；
   - B. 维持现状，只做前向：先取一次 exchangeInfo，等首次观察之后的日归档发布（约次日），再对这些日期构建数据集并跑第 5～8 步。

建议由 Raphael 在 A / B 之间决定；本次未自行选择。

## 7. 发现的问题

- **未发现数据或管线缺陷**：4 个真实归档全部通过 checksum、严格解析、入库、规范化、质量报告与 PIT 证明；重放全部幂等。
- **运维注意**：`Settings.warehouse_uri` 默认取"代码所在仓库"的 `data/warehouse`；从 git worktree 运行时会指向 worktree 内部。真实运行必须显式设 `HLENS_WAREHOUSE_URI=file:///home/raphael/projects/hlens-autoresearch/data/warehouse`（本次已这样做；worktree 内未产生任何 warehouse 文件）。
- **观察（非缺陷）**：`data/warehouse/staging/` 下保留 4 个只含 `meta.json` 的发布凭据目录；这是 C1 StorageAdapter 为同一凭据幂等重放 `publish` 的设计，未清理、也不应手工删除。
- **容量参考**（仅 1 440 行/单元）：E3 峰值 636 MB 是本次最高；与 §6.5 的容量结论一致，K 线整天处理没有内存风险。aggTrades 的容量基线仍未做，本次未涉及。

## 8. 复现方法

在含本工具的代码树根目录运行（不要打印 `.env.catalog`）：

```bash
P=/home/raphael/projects/hlens-autoresearch
RUN="systemd-run --user --scope --quiet -p MemoryMax=3G -p MemorySwapMax=0 \
  env HLENS_WAREHOUSE_URI=file://$P/data/warehouse uv run --env-file $P/.env.catalog"
$RUN python -m infrastructure.catalog.create_phase1_tables          # 幂等；已存在则只核对
for step in collect ingest normalize report pit f2; do
  $RUN python -m infrastructure.tools.dnet_capability_run \
    --state-dir $P/data/dnet-run --day 2026-09-21 --day 2026-09-22 $step
done
```

- 每步把结果写入 `data/dnet-run/<step>.json` 并追加到 `steps.jsonl`；`ingest` 读 `collect.json`，`normalize` 读 `ingest.json`。
- 重跑任一步 = 用同样参数再调用一次（08-deployment.md §6.2）：D2 / E1 返回 `replayed`，E3 返回 `reused`。D0 重跑会重新下载并校验；相同内容得到同一内容寻址对象。
- 离线测试：`uv run pytest tests/infrastructure/tools/test_dnet_capability_run.py`（mock 归档站点 + 临时 SQLite catalog，不联网）。

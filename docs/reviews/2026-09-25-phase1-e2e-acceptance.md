# Phase 1 G1 端到端验收证据记录：首切片 归档 → Raw → Canonical → PIT → Research Dataset + manifest → Representation

| 字段 | 值 |
|---|---|
| 日期 | 2026-09-25 |
| 起草者 | Claude Code（Sonnet 5），批次 G1-E2E（roadmap 验收 #20） |
| 复核者 | **待 Codex**。本记录是测试证据，不是验收结论 |
| 基线 | `6fba363`（phase1(DS-1) 研究数据集选择表，ADR-0033） |
| 新增文件 | `tests/infrastructure/e2e/conftest.py`、`first_slice_support.py`、`test_phase1_first_slice.py`、`test_phase1_first_slice_postgres.py`；本文档 |
| 生产代码改动 | 无 |
| 结论（本记录自评） | 首切片端到端链路**全部可实际跑通**，无 STOP 项 |

## 1. #20 要求什么

roadmap Phase 1 验收矩阵第 20 项：

> 首切片端到端：归档 → Raw → Canonical → PIT → Research Dataset + manifest → Representation
> （可观察证据：端到端验收记录，批次 G）

批次 G 的执行指引进一步要求"端到端验收、修复、关闭文档"，且验收 #21（每批 pytest / ruff / format /
mypy 全绿）同时适用。本批次（G1-E2E）不改动任何生产代码，只补一个端到端测试模块 + 一份证据记录，
把已经分别验收的 D2 / D3E / E1 / E2 / E3 / E4 / F1 / F2 / F3 / F4 串成一条链，证明它们能在同一个
BTCUSDT + ETHUSDT 小型 fixture 上首尾相接地跑通，而不是分别正确但从未真正接在一起验证过。

## 2. 每一步由哪个测试证明

所有断言均针对**真实**模块（`infrastructure/**`）；测试只注入网络传输、时钟与 fixture 数据，
不 mock 被测逻辑（H4）。下表的"证据"逐条对应 `tests/infrastructure/e2e/test_phase1_first_slice.py`
中 `test_phase1_first_slice_archive_to_representation` 的代码位置（行号为该文件当前内容）。

| # | 步骤 | 证明它的代码 | 关键断言 |
|---|---|---|---|
| 1 | 归档 ingest（D2）→ REST 补尾 ingest（D3E）→ 跨通道 reconciliation（D-33 edge）→ 每个 unit 的 Canonical 归一化（E1），BTCUSDT / ETHUSDT 两个符号、`agg_trades` / `klines_1m` 两种数据类型 | `w.trades()`（BTC）、`ingest_trades_for(w, ETH, …)`、`ingest_bars_for(w, BTC/ETH, …)`（`first_slice_support.py`，复用 `revision_support.archive`、`rest_support.queue_*_chain` / `*_request`、`RestHarness.collect` / `store`、`CanonicalNormalizer.normalize_unit`、`ChannelReconciler.reconcile`） | 归档副本在每条 D-33 edge 中胜出（archive supersedes REST）；`c.BARS` / `c.TRADES` 中两个符号各自的归档谱系行数与摄入行数一致（12 根 K 线 × 2 符号、3 笔成交 × 2 符号） |
| 2 | exchangeInfo 快照（mock transport）→ E2 listing 派生 | `w.listed(ds.TRADING, ds.L1)`（`dataset_support.World`，内部走真实 `ExchangeInfoSnapshotStore` + `ListingDeriver`） | 后续 F2 universe 能在 `L1` 之后把两个符号都判定为 TRADING 成员 |
| 3 | 每个被覆盖分区的质量报告（E3；rule `hlens.quality.canonical-partition@2.0.0`，含 gap 表）+ listing 历史报告 | `w.report("agg_trades", symbols=(BTC, ETH), listing=False)`、`w.report("klines_1m", symbols=(BTC, ETH), listing=True)` | 返回 2 个 trades 分区报告 id + 3 个（BTC klines、ETH klines、listing）报告 id；均可在 `DATA_QUALITY_REPORTS` 中查到 |
| 4 | PIT 选择（F1）两种方式：保守 spec 与绑定 `infrastructure.pit.assumption.ASSUMPTION_BINDING`（ADR-0032）的 spec | `test_pit_selection_with_and_without_the_archive_assumption`（同目录，独立测试函数） | 同一条纯归档成交：保守 spec 下事件时刻 +1 小时仍 `ABSENT`；绑定 assumption 后事件时刻 + 5s（`ASSUMPTION_LATENCY`）即 `SELECTED`，且存储行本身不变、只是 PIT 视图的有效时间前移 |
| 5 | F2 universe → F3 `DatasetBuilder.build` 写入生产表 `research.dataset_selections`，manifest 落入 `research.dataset_manifests` | `w.builder().build(FIRST_SLICE_UNIVERSE, spec, "klines_1m", DAY_START, DAY_END)` | manifest 绑定 spec 构造时的**每一个**已有快照的上游表（`manifest.point_in_time.snapshot_bindings == expected_bindings`）、PIT spec 本身（含 rule / availability / precedence / parser bindings）、lineage（listing 两跳 + bars 三跳）、3 个质量报告 id、每条 evidence gap 都能在其引用的报告里查到同样的文本；重建（`w.h.reopen()` 后再次 `build`）manifest 逐字节相同（`canonical_json` 相等）、复用同一个 dataset 快照、`replayed=True` |
| 6 | E4 从该 selection 重采样 5 分钟 bar，F4 特征（`bar_log_return`）经 `run_feature` 跑通 | `resample_bars(selection, 5, DAY_START, DAY_END)` → `derived_bar_observations` → `pit_feature_request` → `run_feature(BarLogReturnProvider(...), …)` | 12 根 1 分钟 K 线重采样为 `[complete, complete, incomplete]` 三个桶，只有两个完整桶进入 observation（缺口不填补）；同一个 `request` 重复调用两次、以及在 `w.h.reopen()` 后重新选择 / 重采样一次，`result_hash` 与整份 `FeatureResult` 都一致；`request.visible_at` 返回的每条 observation 满足 `available_time + available_lag <= evaluation_time`；点 spec 在 `simulation_time` 之前的评估时刻被 `pit_feature_request` 直接拒绝（`FeatureInputBuildError`），而不是从更晚的视图悄悄回答 |
| PostgreSQL 变体（附加证据，非 #20 硬性要求） | 同一 fixture 的 F2/F3 数据集路径在真实 PostgreSQL 上跑通并在"进程重启"（`reopen`）后 replay | `tests/infrastructure/e2e/test_phase1_first_slice_postgres.py::test_first_slice_dataset_end_to_end_and_restart_rebuild_on_postgres`（镜像 `tests/infrastructure/dataset/test_dataset_postgres.py`） | 无 `HLENS_TEST_CATALOG_URI` 时按 `postgres_test_catalog_uri()` 的既有约定跳过；本次本地运行为 `SKIPPED`（该环境变量未配置），断言与 SQLite 路径一致（manifest 逐字节相同、`replayed`、快照 id 相同） |

## 3. 实际结果

在本 worktree（`6fba363`）按内存与体积规则跑出的结果，逐条如实记录，不做汇总性断言：

```
$ systemd-run --user --scope --quiet -p MemoryMax=3G -p MemorySwapMax=0 \
  uv run pytest tests/infrastructure/e2e/ tests/test_docs_consistency.py tests/test_architecture_boundaries.py -q
..s.................                                                     [100%]
19 passed, 1 skipped in 12.63s
```

- `test_phase1_first_slice_archive_to_representation`：**PASS**（步骤 1、2、3、5、6 全部在这一个测试里连续跑通）。
- `test_pit_selection_with_and_without_the_archive_assumption`：**PASS**（步骤 4）。
- `test_first_slice_dataset_end_to_end_and_restart_rebuild_on_postgres`：**SKIPPED**（`HLENS_TEST_CATALOG_URI` 未配置，符合既有约定，不是失败）。
- `tests/test_docs_consistency.py`、`tests/test_architecture_boundaries.py`：**PASS**（本批次未引入文档矛盾或跨 Plane 依赖）。

全量静态检查（覆盖整个仓库，非仅本批次新增文件）：

```
$ systemd-run --user --scope --quiet -p MemoryMax=3G -p MemorySwapMax=0 uv run ruff check .
All checks passed!
$ systemd-run --user --scope --quiet -p MemoryMax=3G -p MemorySwapMax=0 uv run ruff format --check .
296 files already formatted
$ systemd-run --user --scope --quiet -p MemoryMax=3G -p MemorySwapMax=0 uv run mypy
Success: no issues found in 194 source files
```

`git status --porcelain` 在提交前只显示 `tests/infrastructure/e2e/`（新目录）与本文档；未触碰
`core/`、`infrastructure/`、`plugins/`、既有 `tests/` 文件或任何契约 / Schema / ADR。

## 4. 复用了哪些既有 harness，新增了什么

- 完全复用、未新增重复实现：`tests.infrastructure.revision.rest_store_support.sqlite_harness` /
  `RestHarness`（经 `dataset_support.World.h`）、`tests.infrastructure.canonical.canonical_support`
  的 `ingest_archive` / `ingest_rest` / `normalizer`、`tests.infrastructure.revision.exchange_info_support`
  的 mock transport 与 E2 listing 流程（经 `World.x` / `World.listed`）、
  `tests.infrastructure.dataset.dataset_support.World`（F2/F3 主干：`spec` / `bindings` / `report` /
  `builder`）、`infrastructure.feature.runner.run_feature`、`infrastructure.feature.observations`
  （F4 wiring）。
- 新增的唯一代码是 `tests/infrastructure/e2e/first_slice_support.py`：`World.trades` /
  `World.bars`、`RestHarness.ingest_archive`、`canonical_support.ingest_rest` 等便捷封装把符号写死为
  `BTCUSDT`、行数写死为个位数，端到端测试需要第二个符号（ETHUSDT）与一段能重采样出完整 5 分钟桶的
  K 线（12 分钟），所以本文件用同样的底层积木（`revision_support.archive`、
  `rest_support.queue_*_chain` / `*_request`、`RestHarness.collect` / `store`）显式传入 `symbol`，
  不重新实现任何一层已验收的逻辑。

## 5. 没有出现的情况

按任务要求："如果流水线中有环节无法端到端接通，就在那一步停止，精确报告缺什么；不要糊弄过去。"
本批次在实现测试的过程中没有遇到需要在这里报告的缺口：D2 / D3E / E1 / E2 / E3 / E4 / F1 / F2 / F3 /
F4 在同一份 fixture 上首尾相接，manifest 绑定与重放、PIT 的两种可用性语义、E4→F4 的可复现性均按预期
工作，不需要任何生产代码改动或临时豁免。

## 6. 结论与下一步

**本记录只是测试证据，PASS/FAIL 的验收结论由 Codex 独立复核给出。** 建议 Codex 复核关注点：

1. 复核上表"关键断言"是否确实覆盖 #20 的六个子步骤，以及是否存在本记录未察觉的遗漏；
2. 独立运行 `tests/infrastructure/e2e/`（含 PostgreSQL 变体，若 `HLENS_TEST_CATALOG_URI` 可用）；
3. 确认 `git diff` 只包含 `tests/infrastructure/e2e/` 与本文档，未触碰冻结契约。

复核通过后，批次 G 的收尾（更新 `PROJECT_STATUS.md`、`PROJECT_MEMORY.md`、决定 Phase 1 是否关闭）
按 CLAUDE.md §0 / §6 由 Codex / Raphael 处理，不在本记录范围内。

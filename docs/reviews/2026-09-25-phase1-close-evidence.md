# Phase 1 关闭证据（草稿）

> **草稿，待 Codex / Raphael 复核。** 本文件只是把 roadmap Phase 1 验收矩阵 #1～#21 逐项对应到
> 实现模块、测试、commit 与当前验收状态，**不构成任何验收结论**，也不代表 Phase 1 已关闭。
> 起草者：Claude Code（Sonnet 5），批次 G3-C，依 Raphael 2026-09-24 的持续执行授权。

| 字段 | 值 |
|---|---|
| 日期 | 2026-09-25 |
| 依据 | `docs/research/roadmap.md`《Phase 1 验收矩阵》、`PROJECT_STATUS.md` §3/§4/§6/§7 |
| 方法 | 对每项：读取 roadmap 原文要求 → 定位实现模块 → 用 `grep` 核实测试文件与测试函数确实存在 → 用 `git log` 核实 commit 存在于本分支历史 → 按 `PROJECT_STATUS.md` 记录的"已验收 / REVIEW_PENDING"标注状态 |
| 范围限制 | 本文档**没有**重新运行全量测试套件（会话内存上限 2.5 GB，禁止跑全量），只核实了测试文件 / 函数名存在；各批次历史上是否真的全绿，以 `PROJECT_STATUS.md` 与对应 commit message 的记录为准，本文档不重新验证 |
| 关键发现（提前说明） | 按 `git log` 中 `phase1: accept <X> and open <Y>` 这一串验收门 commit，Codex 的验收门止步于 **D3D**（`300bf33`）。**D3E 及其后的一切**（D3E-R1/R2/R3、D4、E0～E4、F1～F4、QG-1/QG-2、DS-1、本批 G3-C）**都没有对应的 `accept` 门 commit**，与 `PROJECT_STATUS.md` §3/§4 记录的"REVIEW_PENDING"完全一致。下表逐项标注哪些子部分落在已验收范围内、哪些落在 REVIEW_PENDING 范围内 |

---

## 0. 验收门 commit 时间线（核实用）

`git log --oneline` 中形如 `phase1: accept <前一批> and open <下一批>` 的 commit 是 Codex 独立复核通过后的正式验收门；命令：

```
git log --oneline main..HEAD | grep -E "^[0-9a-f]+ phase1: (accept|open)"
```

结果（从早到晚）：

```
237cc61 Phase 1: open data plane decision phase
cd2704d phase1: open B1 after A3 acceptance
2d2946f phase1: accept B1 and open B2
b9e584d phase1: accept B2 and open B3
f53d1a9 phase1: accept B3 and open C1
a7bd172 phase1: accept C1 and open C2
88b4404 phase1: accept C2 and open C3
8738547 phase1: accept C3 and open D0
6652452 phase1: accept D0 and open D1
8ff479d phase1: accept D1 and open D2
3145980 phase1: accept D2 and open D3A design gate
2e40b36 phase1: accept ADR-0027 and open D3B
0c3af31 phase1: accept D3B and open D3C
960b552 phase1: accept D3C and open D3D
300bf33 phase1: accept D3D and open D3E
```

**没有** `phase1: accept D3E and open ...` 这一行——D3E 之后再没有出现过验收门 commit。这与 `PROJECT_STATUS.md` §3「当前正在做」一节把 D3E / D3E-R1 / D3E-R2 标为 `REVIEW_PENDING`、并说明 E0 起的一切都"以未经 Codex 验收的 D3E 为输入"完全一致；也与 §4 说 A3～D3D 已验收、D3E 起才 REVIEW_PENDING 一致。

D3E-R3（`7e9e084`）与 E1-R3/R4、G3-S 系列穿插出现在同一段提交历史里——也就是说，在 D3E 正式过验收门之前，后续批次已经在其上继续开发。这与 roadmap 恢复序列声明的"门未通过不得进入下一批"字面上不一致；这是否可接受（例如因为都在同一 REVIEW_PENDING 窗口内并行修复），需要 Codex 明确裁决，本文档不代为下结论。

---

## 1. 逐项验收矩阵

### #1 — ADR-0021~0024 Accepted；`03-data.md` 同步；首切片冻结；远程与推送流程成文（ADR-0025）

- **要求**：ADR 索引与 docs 一致性测试。
- **实现**：`docs/adr/0021-*.md` ~ `0024-*.md`、`0025-*.md`；`docs/architecture/03-data.md`。
- **测试**：`tests/test_docs_consistency.py`（全部通过，见本批 §「合并前验证」）。
- **commit**：`2cb6aab`（accept data architecture and freeze first slice）、`fba5c27`（align execution gates and remote workflow）。
- **状态**：✅ **Codex 已验收**（批次 A2/A2r，`PROJECT_STATUS.md` §3）。

### #2 — 只新增 03-data.md §6.1 的四个直接依赖，版本锁定在 `uv.lock`

- **实现**：`pyproject.toml`、`uv.lock`。
- **测试**：`tests/smoke/test_phase1_deps_import.py::test_phase1_direct_deps_importable`。
- **commit**：`2b20b81`（A3a）。
- **状态**：✅ **Codex 已验收**（批次 A3a）。

### #3 — 类型化设置符合 03-data.md §6.2

- **实现**：`infrastructure/settings.py`。
- **测试**：`tests/infrastructure/test_settings.py`（`test_catalog_uri_rejected`、`test_warehouse_uri_rejects_unsafe_forms`、`test_staging_uri_rejects_unsafe_forms`、`test_catalog_secret_redacted_from_repr_and_str` 等）。
- **commit**：`d840dbb`（A3b）。
- **状态**：✅ **Codex 已验收**（批次 A3b）。

### #4 — 双轴时间、revision、`supersedes` DAG 与 maximal-head 选择的契约、Schema、contract tests

- **实现**：`core/contracts/revision.py`。
- **测试**：`tests/test_revision_contracts.py`（`test_knowledge_time_cannot_precede_ingest_time`、`test_available_time_cannot_precede_the_event` 等）。
- **commit**：`b15faa9`（B1）。
- **状态**：✅ **Codex 已验收**（批次 B1，接受门 `2d2946f`）。

### #5 — listing revision、`UniverseSelectionSpec`、成员/排除清单、`ResearchDatasetManifest` 契约、Schema、contract tests

- **实现**：`core/contracts/universe.py`。
- **测试**：`tests/test_universe_contracts.py`（`test_open_and_closed_intervals_are_legal`、`test_open_end_must_be_explicit_not_defaulted` 等）。
- **commit**：`b41a46a`（B2）。
- **状态**：✅ **Codex 已验收**（批次 B2，接受门 `b9e584d`）。

### #6 — Collector / Storage / Catalog Protocol + DTO + provider-agnostic contract tests 先于任何实现

- **实现**：`core/contracts/collector.py`、`core/contracts/storage.py`、`core/contracts/catalog.py`。
- **测试**：`tests/test_adapter_contracts.py`、`tests/test_adapter_contract_suites.py`（`TestDictStorageContract`、`TestMemoryCatalogContract`、`TestDailyArchiveCollectorContract` 等，用两个不同替身各自跑一遍 contract suite）；`tests/contract_suites/{storage,catalog,collector}.py` 是可复用的检查集合本体。
- **commit**：`5741b93` + R1 `8fd33d1` + R2 `9c57253`（B3）。
- **状态**：✅ **Codex 已验收**（批次 B3，接受门 `f53d1a9`）。

### #7 — `file://` StorageAdapter：staging → 校验 → 同文件系统原子发布

- **实现**：`infrastructure/storage/local_file.py`（模块路径以实际文件为准）。
- **测试**：`tests/infrastructure/storage/test_local_file_storage.py`（`test_path_escape_and_staging_keys_rejected`、`test_symlink_escape_is_rejected` 等 30 个用例）、`tests/infrastructure/storage/test_local_file_storage_contract.py`。
- **commit**：`e5610b1` + R1 `744ac34` + R2 `bdb3673` + R3 `7857039`（C1）。
- **状态**：✅ **Codex 已验收**（批次 C1，接受门 `a7bd172`）。

### #8 — PyIceberg SQL Catalog on PostgreSQL，独立库/role；集成测试用独立 PostgreSQL

- **实现**：`infrastructure/catalog/iceberg_adapter.py`。
- **测试**：`tests/infrastructure/catalog/test_catalog_contract_postgres.py`、`test_catalog_postgres_integration.py`、`test_catalog_adapter_unit.py`。
- **commit**：`373e286`（C2）。
- **状态**：✅ **Codex 已验收**（批次 C2，接受门 `88b4404`）。

### #9 — 首切片八张表（现为十五张）按冻结名与初始分区创建；partition-spec 演进有等价测试；batch id 幂等 commit

- **要求原文**：C3 首批八张表；ADR-0027 的四张 REST 表属 D3B（另计）；本次 DS-1 追加第十五张 `research.dataset_selections`。
- **实现**：`infrastructure/catalog/phase1_tables.py`（`PHASE1_TABLES`，当前 15 张：C3 的 8 张、D3B 的 4 张 REST 表、E2 的 exchangeInfo 表、QG-1 的证据缺口表、DS-1 的 `research.dataset_selections`）。
- **测试**：`tests/infrastructure/catalog/test_phase1_tables.py`、`test_phase1_tables_postgres.py::test_fifteen_tables_are_created_idempotently_with_the_frozen_layout`、`test_each_table_appends_replays_restarts_and_time_travels`、`test_day_partitioned_tables_write_real_day_partitions`。
- **commit**：C3 首八张表 `c193918` + `ec1504d`（D-32）+ `e40c285` + R1 `973ffbd`（接受门 `8738547`）；D3B 四张 REST 表 `3b267a0`（接受门 `0c3af31`）；E2 exchangeInfo 表 `3b6038b`（**REVIEW_PENDING**，见 #16）；QG-1 证据缺口表 `e31ac04` + R1 `b7e48f6`（**REVIEW_PENDING**，见下）；DS-1 `6fba363`（**REVIEW_PENDING**，本批之前）。
- **状态**：⚠️ **部分验收**——首批八张表（C3）与四张 REST 表（D3B）已由 Codex 验收；exchangeInfo 表（E2）、证据缺口表（QG-1）、`research.dataset_selections`（DS-1）三张后续追加的表均在 D3E 之后落地，属 **REVIEW_PENDING**。`test_fifteen_tables_are_created_idempotently_with_the_frozen_layout` 这一条测试覆盖全部十五张表，但该测试本身随 DS-1 一起还没有被 Codex 独立复核过。

### #10 — 公共归档下载只访问 `HLENS_BINANCE_ARCHIVE_BASE_URL`；先过 `.CHECKSUM` 再经 staging 原子交付

- **实现**：`infrastructure/collector/binance_archive.py`。
- **测试**：`tests/infrastructure/collector/test_binance_archive.py`（`test_checksum_precedes_zip_and_404_skips_zip`、`test_bad_checksum_publishes_nothing`、`test_zip_hash_mismatch_publishes_nothing` 等 29 个用例）、`test_binance_archive_contract.py`。
- **commit**：`85ecce3` + R1 `eb080e8` + R2 `7a9f468`（D0）。
- **状态**：✅ **Codex 已验收**（批次 D0，接受门 `6652452`）；**已知限制**（`PROJECT_STATUS.md` §7）：只在小型 fixture 与两个单位边界日的只读 smoke 上验证过，大体量 BTC 整日归档的内存/吞吐基线仍未做，批量 backfill 前需要先完成容量检查（G3 容量基线，见本批 §4）。

### #11 — parser `binance.spot.archive.parser@1.0.0`：按覆盖日选单位；解析时间全落在 `[coverage_start, coverage_end)`；越界整文件拒绝

- **实现**：`infrastructure/parser/binance_archive.py`。
- **测试**：`tests/infrastructure/parser/test_binance_archive_parser.py`（44 个用例，含 `test_klines_ms_day_success_binds_everything`、`test_parser_never_guesses_units_from_magnitudes`）、`test_binance_archive_zip.py`、`test_binance_archive_storage.py`。
- **commit**：`c966085`（D1）。
- **状态**：✅ **Codex 已验收**（批次 D1，接受门 `8ff479d`）。

### #12 — append-only revision：重放幂等、归档替换追加、`arrival_seq` 不决定优先级、竞争修订 fail closed、崩溃后恢复

- **实现**：`infrastructure/revision/store.py`、`infrastructure/revision/identity.py`。
- **测试**：`tests/infrastructure/revision/test_store_unit.py`、`test_store_postgres.py`（`test_crash_after_the_archive_commit_is_repaired_by_re_running`、`test_crash_between_microbatches_leaves_only_whole_batches`、`test_replay_after_restart_adds_no_snapshot_row_or_sequence`）。
- **commit**：`ba9f417` + R1 `b05486b`（D2）。
- **状态**：✅ **Codex 已验收**（批次 D2，接受门 `3145980`）。

### #13 — REST 补尾只用 market-data-only base；缺口显式标记，不推断填补

- **要求**：端点静态检查与缺口测试。
- **实现**：设计门 `docs/adr/0027-rest-raw-source-and-element-revisions.md`；`infrastructure/revision/rest_identity.py`、`rest_availability.py`、`rest_precedence.py`、`channel_precedence.py`（D3B，纯函数）；`infrastructure/parser/binance_rest.py`（D3C，严格 decoder）；`infrastructure/collector/binance_rest.py`（D3D，可重放 collector）；`infrastructure/revision/rest_store.py` + `channel_reconcile.py`（D3E，store + reconciler，**REVIEW_PENDING**）。
- **测试**：D3B `tests/infrastructure/revision/test_rest_identity.py`、`test_rest_policies.py`、`test_channel_precedence.py`；D3C `tests/infrastructure/parser/test_binance_rest_decoder.py`、`test_binance_rest_pagination.py`；D3D `tests/infrastructure/collector/test_binance_rest.py`、`test_binance_rest_checkpoints.py`、`test_binance_rest_client_boundary.py`、`test_binance_rest_static.py`、`test_binance_rest_contract.py`；D3E `tests/infrastructure/revision/test_rest_store_unit.py`、`test_rest_store_postgres.py`、`test_channel_reconcile.py`（42 个用例）。
- **commit**：D3A 设计门 `7111e54` + R1 `ed526f7`（接受门 `2e40b36`，[验收记录](2026-09-25-d3a-adr-0027-acceptance.md)）；D3B `3b267a0` + R1 `02c0418`（接受门 `0c3af31`，[验收记录](2026-09-25-d3b-rest-foundations-acceptance.md)）；D3C `643cf45` + R1 `6b9e670`（接受门 `960b552`，[验收记录](2026-09-25-d3c-rest-decoder-acceptance.md)）；D3D `61dd9bf` + R1 `c06b9fa`（接受门 `300bf33`，[验收记录](2026-09-25-d3d-rest-collector-acceptance.md)）；D3E `21e31f5` + R1 `52f7477` + R2 `c326434` + R3 `7e9e084`。
- **状态**：⚠️ **部分验收**——D3A～D3D（设计、纯函数、decoder、collector）均已由 Codex 验收；**D3E（store + reconciler，D-33 跨通道比对）没有验收门 commit，是 REVIEW_PENDING**。这意味着"REST 数据可以进数据集"这件事本身还没有被独立复核过——本批 G3-C 的 `--rest` 探针能跑通只证明代码路径可执行，不构成验收。

### #14 — WebSocket live tail 只在历史 backfill、REST gap reconciliation 与重放幂等验收后启用（可不启用）

- **实现**：无 WebSocket 代码；[`docs/reviews/2026-09-25-d4-live-tail-gate.md`](2026-09-25-d4-live-tail-gate.md) 记录不启用的理由。
- **测试**：`test_architecture_boundaries.py` 类测试间接保证没有引入网络长连接依赖；没有专门的"D4 门"测试，因为门的内容是"不做"。
- **commit**：`a536ef3`（D4 门记录）。
- **状态**：⚠️ **REVIEW_PENDING**（门记录本身标注"本记录是 REVIEW_PENDING 提案，不是验收"）。结论本身（不启用）符合 ADR-0022 与 roadmap #14 的强制要求——REST gap reconciliation 前置（D3E）未验收，所以合法结果只能是"不启用"，这一点逻辑上站得住，但记录尚待 Codex 正式确认。

### #15 — Canonical trades / bars_1m 绑定 Raw lineage；派生按位一致；有快照 ID 且可时间旅行

- **实现**：`infrastructure/canonical/normalizer.py`（E1）、`infrastructure/canonical/resample.py`（E4）。
- **测试**：`tests/infrastructure/canonical/test_normalizer.py`、`test_normalizer_postgres.py::test_dual_lineage_normalization_recovery_and_mapping_on_postgres`、`test_a_forged_raw_row_is_refused_on_postgres`；`tests/infrastructure/canonical/test_resample.py`（10 个用例，覆盖"只吃已选 1m bar、不补缺、按位重跑一致"）。
- **commit**：E1 `9b8674a` + R1 `65aedd6` + R2 `f6c29c9` + R3 `8c9109b` + R4 `42530a4`；E4 `8d69ef6` + R1 `3c32e2e`；容量相关的批次重写 G3-S `bd1d941` + R1 `f92afdf`、G3-S2 `1964e33`、G3-S3 `7a70fbd` + R1 `0fe7471` + R2 `cb4bedd` + R3 `f4c5075`。
- **状态**：⚠️ **REVIEW_PENDING**——E0（`ca48a36` + R1 `e2951e4`，双 Raw→Canonical 设计）由 Raphael 批准方案 B（`3ee0519`），但这只是"接受方案 B 的架构设计"，不是"验收 E1 的实现"；E1 及其后所有 G3-S 系列批次都没有 Codex 验收门 commit。

### #16 — listing 历史进入 `canonical.instrument_listings`；质量报告按分区自动生成

- **实现**：`infrastructure/canonical/listings.py`（E2，`ListingDeriver`）、`infrastructure/collector/binance_exchange_info.py`、`infrastructure/parser/binance_exchange_info.py`；`infrastructure/quality/reporter.py`（E3）、`infrastructure/quality/listing_report.py`。
- **测试**：`tests/infrastructure/canonical/test_listings.py`（24 个用例）、`tests/infrastructure/collector/test_binance_exchange_info.py`、`tests/infrastructure/parser/test_binance_exchange_info_decoder.py`、`tests/infrastructure/revision/test_exchange_info_rules.py`（12 个用例）、`test_exchange_info_store.py`；`tests/infrastructure/quality/test_reporter.py`（20 个用例，含 `test_a_bar_partition_report_lists_gaps_inputs_and_evidence_gaps`）、`test_listing_report.py`。
- **commit**：ADR-0029 设计证据 `4d3e6d8`；E2 `3b6038b` + R1 `1e3d325`；E3 `bd7b60d` + R1 `4587473`；QG-1（证据缺口独立表，ADR-0031）`e31ac04` + R1 `b7e48f6`；QG-2（证据缺口流式写入）`e9ff4ec`。
- **状态**：⚠️ **REVIEW_PENDING**——ADR-0029 / ADR-0031 由 Claude 依 Raphael"授权所有"接受（`PROJECT_STATUS.md` §6），属架构决定层面的确认；E2 / E3 / QG-1 / QG-2 的**实现**均无 Codex 验收门 commit。

### #17 — availability / precedence policy 证据已产出、审阅并有测试；早期 `available_time` 缺证据取 `ingest_time` 并写证据缺口

- **实现**：归档侧 `infrastructure/revision/availability.py`、`precedence.py`（D2 范围内）；REST 侧 `rest_availability.py`、`rest_precedence.py`、`channel_precedence.py`（D3B 范围内）；exchangeInfo 侧 `exchange_info_availability.py`（E2 范围内）；D-HIST 假设叠加层 `infrastructure/pit/assumption.py`（ADR-0032）。
- **测试**：`tests/infrastructure/revision/test_availability.py`（15 个用例）、`test_precedence.py`（23 个用例）、`test_channel_precedence.py`（27 个用例）；D-HIST 相关 `tests/infrastructure/pit/test_selector.py::test_without_the_assumption_history_before_ingest_is_invisible`、`test_the_bound_assumption_makes_an_archive_trade_available_at_event_time_plus_latency`、`test_the_assumption_never_moves_a_rest_revision`、`test_another_version_or_hash_of_the_assumption_is_refused`。
- **commit**：归档侧随 D2 `ba9f417` + R1 `b05486b`；REST 侧随 D3B `3b267a0` + R1 `02c0418`；exchangeInfo 侧随 E2 `3b6038b`（REVIEW_PENDING）；D-HIST `03116ed`（Raphael 2026-09-25 批准推荐方案 A，ADR-0032 Accepted，**实现** REVIEW_PENDING）。
- **状态**：⚠️ **部分验收**——归档侧、REST 侧（D-33 policy 本身）已随 D2 / D3B 验收；**exchangeInfo 侧 availability policy 与 D-HIST PIT 假设叠加层的实现都是 REVIEW_PENDING**。roadmap 把 #17 标记的批次是"D2 / E"，D2 部分已满足，E 部分未满足。

### #18 — PIT 双截止 + maximal head，同输入按位一致；各类 fail-closed 情形；早期历史缺证据取 `ingest_time` 不算数据集级失败

- **实现**：`infrastructure/pit/selector.py`（F1）、`infrastructure/pit/view.py`、`infrastructure/pit/assumption.py`。
- **测试**：`tests/infrastructure/pit/test_selector.py`（`test_the_four_cutoffs_select_as_adr_0028_section_4`、`test_an_old_spec_reproduces_its_result_after_new_evidence`、`test_selection_is_deterministic`、`test_a_mismatch_stays_a_conflict`、`test_a_forged_canonical_row_is_refused`、`test_every_revision_is_owned_by_exactly_one_window` 等）、`test_selector_postgres.py::test_bound_snapshots_decide_on_postgres`。
- **commit**：F1 `5ce7d3a` + R1 `f45fb78`；容量相关 G3-S2 `1964e33`。
- **状态**：⚠️ **REVIEW_PENDING**——测试矩阵覆盖 roadmap 列出的每一种 fail-closed 情形（competing head、precedence 证据缺失、policy 版本未登记、universe 历史缺失、manifest 不完整均有对应测试用例名），但 F1 没有 Codex 验收门 commit。

### #19 — 基础 Representation 与首批 FeatureProvider（接口先行）；Feature guard 防泄漏；Feature 结果可复现

- **实现**：`core/contracts/feature.py`（ADR-0030 契约）、`infrastructure/feature/runner.py`（F4 truncating runner）、`infrastructure/feature/observations.py`、`plugins/features/bars.py`（`BarLogReturnProvider` 等三个首批特征）。
- **测试**：`tests/test_feature_contracts.py`；`tests/infrastructure/feature/test_feature_runner.py`（`test_the_provider_only_ever_sees_the_visible_set`、`test_a_leaky_provider_cannot_leak_through_the_runner`——这是直接证明防泄漏的用例）、`test_feature_pipeline.py`；`tests/test_feature_contract_suite.py`（`test_feature_suite_kills_faulty_implementation`、`test_a_value_from_zero_inputs_cannot_be_built`）；`tests/plugins/features/test_bar_features.py`。
- **commit**：ADR-0030 提案 `4472282`（`PROJECT_STATUS.md` 记为 Claude 依授权接受）；F4 `bc558e3` + R1 `b5ffe98`。
- **状态**：⚠️ **REVIEW_PENDING**——泄漏测试（`test_a_leaky_provider_cannot_leak_through_the_runner`）与可复现性测试都已存在且命名直指验收要求，但没有 Codex 验收门 commit。本批 G3-C 的 `--feature` 探针额外证明了：在一个真实的、经过完整 D1→E1→F1 链路的 klines 数据上跑 `run_feature(BarLogReturnProvider, ...)` 可以成功执行，这是对该测试矩阵的一次容量层面的补充观察，不是新的正确性证据。

### #20 — 首切片端到端：归档 → Raw → Canonical → PIT → Research Dataset + manifest → Representation

- **实现**：`infrastructure/dataset/builder.py`（F3 `DatasetBuilder`）、`infrastructure/universe/builder.py`（F2 `UniverseBuilder`）、`infrastructure/dataset/manifests.py`、`infrastructure/dataset/selection.py`（DS-1，ADR-0033）。
- **测试**：`tests/infrastructure/dataset/test_dataset.py::test_archive_to_manifest_end_to_end`——这条测试确实贯穿"归档 → Raw → Canonical → PIT → Research Dataset + manifest"，但**不包含 Representation / FeatureProvider 这一跳**；`test_universe.py`（10 个用例）、`test_dataset_postgres.py`。
- **本批新增的观察**：`infrastructure/tools/capacity_probe.py` 的 `--rest --dataset --feature` 组合运行（见本文档 §2）是仓库里**第一次在同一次进程内**把"归档 + REST 两通道 → Raw → Canonical → PIT → Research Dataset + manifest → F4 Representation"全部串起来执行成功（N=1000，全部阶段耗时 < 30 秒、内存增量 < 200 MB）。**这是一次容量探针的成功执行，不是一条带断言的正确性测试**，不能替代 roadmap 要求的"端到端验收记录"（批次 G）。
- **commit**：F2 `87a3d3e`；F3 `a167d87` + R1（F3-I）`08913b1` + R2（F3-R1）`762643f`；DS-1 `6fba363`；本批 G3-C（本次提交）。
- **状态**：❌ **未满足（REVIEW_PENDING / 待 G1）**——roadmap 把 #20 归为批次 G（"端到端验收、修复、关闭文档"），`PROJECT_STATUS.md` §4 明确写着"剩余：…G1 端到端、G2 红队、G3 关闭证据"。G1 本身还没有开始。本文档（G3-C 的一半）只是为 G1 准备材料，不能视为 #20 已满足。

### #21 — 全程：PostgreSQL 无行情、Git 无凭据/数据、无账户/交易端点、无 NATS；每批 pytest/ruff/format/mypy 全绿；每个接受点经 Codex 复核后推送

- **实现**：`tests/infrastructure/revision/test_repository_hygiene.py`（残留/密钥检查，逐批复用）；`infrastructure/settings.py`（H8/H9 结构性防线）。
- **本批自查**（仅本批 G3-C，不代表对历史批次的重新验证）：
  - `uv run ruff check .`：**PASS**（见下方"合并前验证"）
  - `uv run ruff format --check .`：**PASS**
  - `uv run mypy`：**PASS**
  - `uv run pytest tests/infrastructure/tools tests/test_docs_consistency.py tests/test_architecture_boundaries.py`：**24 passed**
  - `git status` / `git diff`：只修改了 `infrastructure/tools/capacity_probe.py`、`tests/infrastructure/tools/test_capacity_probe.py`、本文档；未触碰冻结契约、ADR、`PROJECT_STATUS.md`、`PROJECT_MEMORY.md`
- **历史批次**：`PROJECT_STATUS.md` §3 对每个已验收批次都记录了 Codex 独立运行的全量测试数（从 1433 项到 4380 项递增）与静态检查结果；本文档**没有**重新运行这些历史全量（受会话内存上限约束，也不是本批任务范围），如实转述其记录，不代为担保。
- **推送**：本批 commit 仅本地提交，**不 push**（CLAUDE.md §10.8、ADR-0025）；`PROJECT_STATUS.md` §6 记录的 D-PUSH 决定是"只推 `wip/phase-1-unreviewed`"，本批未涉及推送。
- **状态**：⚠️ **本批自查通过；历史批次的全绿记录未被本文档重新验证，按各自 commit 与 `PROJECT_STATUS.md` 记录为准**。

---

## 2. 本批（G3-C）capacity_probe 扩展的证据

### 2.1 新增能力

`infrastructure/tools/capacity_probe.py` 新增三个可选阶段（原有 `--rows N` 的四段基线不变）：

| 参数 | 新增测量阶段 | 覆盖的生产模块 |
|---|---|---|
| `--rest` | `ingest_rest`、`normalize_rest`、`channel_reconcile` | D3D `BinanceSpotRestCollector`、D3E `RestRevisionStore`、`ChannelReconciler`（D-33） |
| `--dataset` | `universe_build`、`dataset_build` | E2 `BinanceSpotExchangeInfoCollector` / `ExchangeInfoSnapshotStore` / `ListingDeriver`（fixture，未计时）、F2 `UniverseBuilder`、F3 `DatasetBuilder`（写入 `research.dataset_selections` + manifest，ADR-0033） |
| `--feature` | `ingest_klines_archive`、`normalize_klines`、`feature_bar_log_return` | D1/D2（klines 归档）、E1 `CanonicalNormalizer`、F4 `run_feature` + `plugins.features.bars.BarLogReturnProvider` |

只 import 生产模块；本地重实现了合成 REST 页 / exchangeInfo 响应 / klines 归档（`_tail_agg_items`、`_exchange_info_body`、`_kline_lines`），网络层用 `httpx.MockTransport` 固定应答，从不导入 `tests/`。

### 2.2 N=1000（全部三个可选阶段）实测输出

```json
{
  "rows": 1000,
  "flags": {"rest": true, "dataset": true, "feature": true},
  "stages": {
    "ingest_archive":        {"wall_seconds": 0.373729, "tracemalloc_peak_mb": 4.379,  "ru_maxrss_delta_mb": 52.930, "row_count": 1000},
    "normalize":             {"wall_seconds": 1.563759, "tracemalloc_peak_mb": 9.995,  "ru_maxrss_delta_mb": 153.176, "canonical_rows": 1000},
    "pit_select_1h":         {"wall_seconds": 1.505020, "tracemalloc_peak_mb": 10.874, "ru_maxrss_delta_mb": 40.078, "keys_evaluated": 42, "keys_selected": 42},
    "quality_report_day":    {"wall_seconds": 6.318691, "tracemalloc_peak_mb": 10.730, "ru_maxrss_delta_mb": 150.641, "event_count": 2},
    "ingest_rest":           {"wall_seconds": 3.271959, "tracemalloc_peak_mb": 14.456, "ru_maxrss_delta_mb": 26.141, "element_count": 1000, "page_count": 1},
    "normalize_rest":        {"wall_seconds": 2.533297, "tracemalloc_peak_mb": 9.787,  "ru_maxrss_delta_mb": 28.152, "canonical_rows": 1000},
    "channel_reconcile":     {"wall_seconds": 3.993806, "tracemalloc_peak_mb": 22.564, "ru_maxrss_delta_mb": 12.418, "edge_count": 1000, "new_edge_count": 1000, "finding_count": 0},
    "universe_build":        {"wall_seconds": 0.236441, "tracemalloc_peak_mb": 1.171,  "ru_maxrss_delta_mb": 0.0, "member_count": 2, "exclusion_count": 0},
    "dataset_build":         {"wall_seconds": 28.432559, "tracemalloc_peak_mb": 44.851, "ru_maxrss_delta_mb": 77.492, "row_count": 42, "replayed": false},
    "ingest_klines_archive": {"wall_seconds": 0.561560, "tracemalloc_peak_mb": 5.091,  "ru_maxrss_delta_mb": 16.758, "row_count": 1000},
    "normalize_klines":      {"wall_seconds": 2.092620, "tracemalloc_peak_mb": 11.457, "ru_maxrss_delta_mb": 0.770, "canonical_rows": 1000},
    "feature_bar_log_return":{"wall_seconds": 0.843686, "tracemalloc_peak_mb": 17.269, "ru_maxrss_delta_mb": 0.0, "evaluation_count": 1, "non_null_count": 1}
  }
}
```

REST 尾部与归档尾部描述的是同一批成交（同构造字段），`channel_reconcile` 确认全部 1000 笔都判定为内容相同并记入一条 D-33 优先证据边（`edge_count == new_edge_count == 1000`，`finding_count == 0`）——这是对 D3E-R2 provenance 校验代码路径的一次真实执行，不是新的正确性证明（D3E 本身仍待 Codex 复核，见 #13）。

### 2.3 读得诚实的边界（工具自带的 `notes` 字段原文）

- `ingest_rest` / `normalize_rest` / `channel_reconcile` 只覆盖**一页**已提交的 REST 页（`min(rows, PAGE_LIMIT)` 条），网络层是假的，不测多页链、重试、D3E-R1/R2 的崩溃恢复路径。
- `dataset_build` 选的是与 `pit_select_1h` 相同的一个 UTC 小时，不是整天——生产环境的成交 Research Dataset 本来就是按小时构建的，从不按天（**D-MAN**，见下）。
- `feature_bar_log_return` 只在单一个"极远未来"求值时刻跑一次，不测真实的求值时间网格、缺口，也不测已实现的另外两个 provider（`bar_realized_volatility`、`bar_volume_sum`）。
- klines 归档固定封顶 `min(rows, 1440)`：一个 UTC 天只有 1440 个不同的一分钟 bar。

### 2.4 合并前验证（本批实际运行结果）

```
uv run pytest tests/infrastructure/tools tests/test_docs_consistency.py tests/test_architecture_boundaries.py
  -> 24 passed in 74.83s

uv run ruff check .
  -> All checks passed!

uv run ruff format --check .
  -> 292 files already formatted

uv run mypy
  -> Success: no issues found in 190 source files
```

（每条命令都套了 `systemd-run --user --scope --quiet -p MemoryMax=2500M -p MemorySwapMax=0`；没有跑过 `uv run pytest` 全量套件。）

---

## 3. `PROJECT_STATUS.md` §6/§7 记录的已知限制（原样转述，供 Codex 复核时对照）

以下限制不是本文档新发现，是 `PROJECT_STATUS.md` §6（当前待决策）与 §7（当前风险）里已经记录、尚未解决的事项，逐条列在这里方便 Codex 关闭审查时一次性核对：

| ID / 主题 | 内容 | 来源 |
|---|---|---|
| **D-MAN** | 数据集清单逐条列出来源链与证据缺口：K 线数据集按天没问题（每天 1440 条），成交按天会有数百万条，一行清单放不下。已决定：Phase 1 成交数据集**按小时**构建；改契约（升 major）留到 Phase 2 按需另立 ADR。**这是已知限制，不是待修的 bug**——本批 `--dataset` 探针因此也按一小时窗口构建（§2.3）。 | `PROJECT_STATUS.md` §6 |
| **键闭包可达范围** | 同一笔成交的副本之间若有超过一天的空档（链断开），两段各自被当作独立记录，冲突看不到；相邻两天的质量报告会各自列出跨天冲突（按设计）；这种数据只能是严重损坏，需要以后专门的质量规则检测。另外按小时选择时现在要读前后各一天的分区，生产规模下的耗时尚未测量。 | `PROJECT_STATUS.md` §7 |
| **D-QGAP / 证据缺口独立表大小** | 成交一天 100～300 万条都有缺口（D-HIST），报告行如果逐条内嵌证据缺口会到数 GB；已决定方案 A——缺口改写进独立只追加表（ADR-0031，QG-1/QG-2），报告行只存引用与计数。 | `PROJECT_STATUS.md` §6 |
| **容量基线（G3-S 系列）** | 规范化"整个单元一次性读入"约每行 19 KB（BTC 一整天 100～300 万行会超出 WSL 约 15 GB 内存）；改为固定快照 + 分批窗口后，30 万行规范化新增常驻约 0.8 GB；时点选择按小时约 0.27 GB 峰值，但**选择结果本身每行约 11 KB，成交数据必须按小时（或更短）分段选择，整天选择（约 30 GB）不可行**。 | `PROJECT_STATUS.md` §7 |
| **D3E provenance 边界** | 由另一页首次交付的元素，只证明那一页的响应记录合法、元素继承其序号与时间，**没有**重新解码那一页正文；归档行同理不在比对时重新解析归档文件。R2 起 reconciler 与 store 使用同一套核对，但这个边界本身没有改变。 | `PROJECT_STATUS.md` §7 |
| **D-33 精确比较的代价** | REST 以毫秒交付、2025 年起归档为微秒，同一笔成交若带亚毫秒位就无法证明相等，只能 fail closed——正确但降低 REST 补尾的价值；是否改请求微秒需要以后单独验证并批准。 | `PROJECT_STATUS.md` §7 |
| **D-HIST 假设叠加层** | 早于本机采集的历史行情默认仍取 `available_time = ingest_time`（保守）；ADR-0032 的"事件时间 + 5 秒可用"假设必须由数据集规格显式绑定才生效，不绑定就维持保守——这是设计如此，不是 bug，但意味着**任何不显式绑定该假设的数据集都用不了 D-HIST 之前的历史数据**。 | `PROJECT_STATUS.md` §6 |
| **D3D/D0 大体量吞吐未测** | D1 已真实验证两个单位边界日的 kline 与 aggTrades；大体量 BTC 日归档尚未做内存/吞吐基线，批量 backfill 前必须先完成容量检查与可恢复 checkpoint。 | `PROJECT_STATUS.md` §7 |
| **D3E 验收缺口本身** | D3E / R1 / R2 三轮修复均已提交，但**没有验收门 commit**（本文档 §0 用 `git log` 核实）；E0 起的一切都建立在未验收的 D3E 之上。 | `PROJECT_STATUS.md` §3/§4；本文档 §0 交叉核实 |

---

## 4. 小结（供 Codex / Raphael 参考，不是结论）

1. **已验收（Codex 独立复核 + 验收门 commit）**：#1～#12，以及 #17 中归档侧与 D3B（REST）侧的 policy 部分。对应 roadmap 批次 A2/A2r、A3a、A3b、B1、B2、B3、C1、C2、C3、D0、D1、D2、D3A、D3B、D3C、D3D。
2. **REVIEW_PENDING（实现已提交，验收门缺失）**：#9 后半（exchangeInfo/证据缺口/DS-1 三张表）、#13 后半（D3E）、#14（D4 门记录）、#15、#16、#17 后半（exchangeInfo policy、D-HIST）、#18、#19。对应批次 D3E 及其后的一切（D3E-R1/R2/R3、D4、E0～E4、F1～F4、QG-1/QG-2、DS-1）。
3. **未满足**：#20（端到端验收记录，待批次 G / G1）。
4. **本批（G3-C）范围内自查通过，不改变以上结论**：`--rest` / `--dataset` / `--feature` 三个新增探针阶段在 N=200（pytest）与 N=1000（手工运行）都成功执行，ruff / ruff format / mypy / 相关测试全部通过；但探针只是执行能力的证据，不是正确性验收，也没有让 D3E 起的任何一项从 REVIEW_PENDING 变成已验收。
5. **给 Codex 的建议顺序**（仅为建议，不代替决策）：D3E（含 R1/R2/R3）是后续一切的地基，逻辑上应先补上它的验收门，再评估 E0～DS-1 这一整段是否可以一次性批量复核，还是要拆回逐批复核。

---

*本文件由 Claude Code（Sonnet 5）依 Raphael 2026-09-24 的持续执行授权起草，批次 G3-C。草稿，待 Codex / Raphael 复核；不构成验收结论。*

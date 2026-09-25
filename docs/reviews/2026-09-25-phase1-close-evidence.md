# Phase 1 关闭证据（草稿）

> **草稿，待 Codex / Raphael 复核。** 本文件只是把 roadmap Phase 1 验收矩阵 #1～#21 逐项对应到
> 实现模块、测试、commit 与当前验收状态，**不构成任何验收结论**，也不代表 Phase 1 已关闭。
> 起草于批次 G3-C（Claude Code / Sonnet 5），**本次（批次 G3-D）更新到 `HEAD = ac2daef`**，
> 补入 G2 跨阶段红队及其返修（RT-1～RT-6）、G3-P 规模性能、并复核已引用的 ADR-0032 / ADR-0033
> 与 G1 端到端记录仍然准确。依 Raphael 2026-09-24 的持续执行授权。

| 字段 | 值 |
|---|---|
| 日期 | 2026-09-25 |
| 更新批次 | G3-C（草稿）→ G3-D（本次，更新到 `ac2daef`） |
| 依据 | `docs/research/roadmap.md`《Phase 1 验收矩阵》、`PROJECT_STATUS.md` §3/§4/§6/§7（**注**：`PROJECT_STATUS.md` 最后一次改动是 `9b2124f`，即 G2 红队发现记录本身；本文档之后的 G2 返修与 G3-P 不在 `PROJECT_STATUS.md` 里，只在本文档与 commit history 中核实） |
| 方法 | 对每项：读取 roadmap 原文要求 → 定位实现模块 → 用 `grep` 核实测试文件与测试函数确实存在 → 用 `git log` 核实 commit 存在于本分支历史 → 按 `PROJECT_STATUS.md` 记录与 commit 时间线标注状态 |
| 范围限制 | 本文档**没有**重新运行全量测试套件（会话内存上限 2 GB，禁止跑全量），本次 G3-D 只实际运行了 `tests/test_docs_consistency.py` + `tests/test_architecture_boundaries.py`（17 passed）；其余测试文件 / 函数名的存在性用 `grep` 核实，通过性以对应 commit message 与既有验收记录为准，本文档不重新验证 |
| 关键发现（提前说明） | 按 `git log` 中 `phase1: accept <X> and open <Y>` 这一串验收门 commit，**实现批次**的 Codex 验收门止步于 **D3D**（`300bf33`）。**D3E 及其后的一切实现**（D3E-R1/R2/R3、D4、E0～E4、F1～F4、QG-1/QG-2、DS-1、G1、**G2 红队及其四次返修**、**G3-P 性能**）**都没有对应的 `accept` 门 commit**。唯一的例外是 `3ee0519`（`phase1: accept ADR-0028 (Raphael) and open E1`）——这是 **Raphael 本人**对 ADR-0028（E0 双 Raw→Canonical 设计）的**设计层面**批准，不是 Codex 对任何实现批次的验收，不改变上述结论 |

---

## 0. 验收门 commit 时间线（核实用）

`git log --oneline` 中形如 `phase1: accept <前一批> and open <下一批>` 的 commit 是验收门；命令：

```
git log --oneline main..HEAD | grep -E "^[0-9a-f]+ phase1: (accept|open)"
```

结果（从早到晚，本次 G3-D 核实与草稿版一致，另补入此前漏列的 `3ee0519`）：

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
3ee0519 phase1: accept ADR-0028 (Raphael) and open E1
```

`3ee0519` 的提交信息标注了 `(Raphael)`：这是 Raphael 本人对 **ADR-0028 设计门**（`docs/adr/0028-dual-raw-canonical-lineage.md`，E0 双 Raw→Canonical 语义）的批准，写在 E0-R1（`e2951e4`）修正之后、E1 实现（`9b8674a`）开始之前——它开的是"E1 可以开始写代码"这道**设计**门，不是 Codex 对某个**实现**批次的独立复核验收。除此之外，**没有** `phase1: accept D3E and open ...` 这一行，D3E 之后再没有出现过任何 Codex 实现验收门 commit。这与 `PROJECT_STATUS.md` §3/§4 把 D3E 起标为 `REVIEW_PENDING` 完全一致；本次 G3-D 新增的 G2 红队与 G3-P 同样落在这个未验收窗口内，不改变边界。

D3E-R3（`7e9e084`）与 E1-R3/R4、G3-S 系列穿插出现在同一段提交历史里，随后 F2/F3/DS-1/G1/**G2**/**G3-P** 又依次叠加在 D3E 之上——也就是说，在 D3E 正式过验收门之前，后续批次一直在其上继续开发。这与 roadmap 恢复序列声明的"门未通过不得进入下一批"字面上不一致；这是否可接受，需要 Codex 明确裁决，本文档不代为下结论。

---

## 1. 逐项验收矩阵

### #1 — ADR-0021~0024 Accepted；`03-data.md` 同步；首切片冻结；远程与推送流程成文（ADR-0025）

- **要求**：ADR 索引与 docs 一致性测试。
- **实现**：`docs/adr/0021-*.md` ~ `0024-*.md`、`0025-*.md`；`docs/architecture/03-data.md`。
- **测试**：`tests/test_docs_consistency.py`（本次 G3-D 实际运行，见下方"合并前验证"，PASS）。
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

- **要求原文**：C3 首批八张表；ADR-0027 的四张 REST 表属 D3B（另计）；DS-1 追加第十五张 `research.dataset_selections`。
- **实现**：`infrastructure/catalog/phase1_tables.py`（`PHASE1_TABLES`，当前 15 张：C3 的 8 张、D3B 的 4 张 REST 表、E2 的 exchangeInfo 表、QG-1 的证据缺口表、DS-1 的 `research.dataset_selections`）。
- **测试**：`tests/infrastructure/catalog/test_phase1_tables.py`、`test_phase1_tables_postgres.py::test_fifteen_tables_are_created_idempotently_with_the_frozen_layout`、`test_each_table_appends_replays_restarts_and_time_travels`、`test_day_partitioned_tables_write_real_day_partitions`。
- **commit**：C3 首八张表 `c193918` + `ec1504d`（D-32）+ `e40c285` + R1 `973ffbd`（接受门 `8738547`）；D3B 四张 REST 表 `3b267a0`（接受门 `0c3af31`）；E2 exchangeInfo 表 `3b6038b`（**REVIEW_PENDING**，见 #16）；QG-1 证据缺口表 `e31ac04` + R1 `b7e48f6`（**REVIEW_PENDING**）；DS-1 `6fba363`（**REVIEW_PENDING**）。
- **G3-D 复核**：本次新增的 G2 红队（`ccb57bc` 起）与其四次返修（`aef4ce7`、`17ff897`、`c756bbe`、`ac2daef`）以及 G3-P（`dcfe8b7`）**均未修改任何表定义或 golden 哈希**——`test_earlier_goldens_are_unchanged_by_*` 系列回归测试没有被这些提交触碰；十五张表的定义、分区与哈希结论不变。
- **状态**：⚠️ **部分验收**——首批八张表（C3）与四张 REST 表（D3B）已由 Codex 验收；exchangeInfo 表（E2）、证据缺口表（QG-1）、`research.dataset_selections`（DS-1）三张后续追加的表均在 D3E 之后落地，属 **REVIEW_PENDING**。

### #10 — 公共归档下载只访问 `HLENS_BINANCE_ARCHIVE_BASE_URL`；先过 `.CHECKSUM` 再经 staging 原子交付

- **实现**：`infrastructure/collector/binance_archive.py`。
- **测试**：`tests/infrastructure/collector/test_binance_archive.py`（`test_checksum_precedes_zip_and_404_skips_zip`、`test_bad_checksum_publishes_nothing`、`test_zip_hash_mismatch_publishes_nothing` 等 29 个用例）、`test_binance_archive_contract.py`。
- **commit**：`85ecce3` + R1 `eb080e8` + R2 `7a9f468`（D0）。
- **状态**：✅ **Codex 已验收**（批次 D0，接受门 `6652452`）；**已知限制**：只在小型 fixture 与两个单位边界日的只读 smoke 上验证过，大体量 BTC 整日归档的内存/吞吐基线仍未做（G3-P 本次只测了 ≤ 1 万行的受限探针，见 §3）。

### #11 — parser `binance.spot.archive.parser@1.0.0`：按覆盖日选单位；解析时间全落在 `[coverage_start, coverage_end)`；越界整文件拒绝

- **实现**：`infrastructure/parser/binance_archive.py`。
- **测试**：`tests/infrastructure/parser/test_binance_archive_parser.py`（44 个用例，含 `test_klines_ms_day_success_binds_everything`、`test_parser_never_guesses_units_from_magnitudes`）、`test_binance_archive_zip.py`、`test_binance_archive_storage.py`。
- **commit**：`c966085`（D1）。
- **状态**：✅ **Codex 已验收**（批次 D1，接受门 `8ff479d`）。

### #12 — append-only revision：重放幂等、归档替换追加、`arrival_seq` 不决定优先级、竞争修订 fail closed、崩溃后恢复

- **实现**：`infrastructure/revision/store.py`、`infrastructure/revision/identity.py`。
- **测试**：`tests/infrastructure/revision/test_store_unit.py`、`test_store_postgres.py`（`test_crash_after_the_archive_commit_is_repaired_by_re_running`、`test_crash_between_microbatches_leaves_only_whole_batches`、`test_replay_after_restart_adds_no_snapshot_row_or_sequence`）。
- **G3-D 补充证据**：G2 红队 `test_rt_crash_points.py`（19 个崩溃点 × 全链路重跑）与 `test_rt_replacement.py`（归档替换、任意到达顺序）从跨阶段角度重新演练了这一项的语义，未发现回归；这是补充观察，不是对 D2 验收门本身的重新验收。
- **commit**：`ba9f417` + R1 `b05486b`（D2）。
- **状态**：✅ **Codex 已验收**（批次 D2，接受门 `3145980`）。

### #13 — REST 补尾只用 market-data-only base；缺口显式标记，不推断填补

- **要求**：端点静态检查与缺口测试。
- **实现**：设计门 `docs/adr/0027-rest-raw-source-and-element-revisions.md`；`infrastructure/revision/rest_identity.py`、`rest_availability.py`、`rest_precedence.py`、`channel_precedence.py`（D3B，纯函数）；`infrastructure/parser/binance_rest.py`（D3C，严格 decoder）；`infrastructure/collector/binance_rest.py`（D3D，可重放 collector）；`infrastructure/revision/rest_store.py` + `channel_reconcile.py`（D3E，store + reconciler，**REVIEW_PENDING**）。
- **G3-D 新增**：G2 红队发现并修复了三处直接影响"REST 数据能否安全进数据集"的正确性缺陷（均已修复，见 §2）：
  - **RT-3**（G2-R1a，`aef4ce7`）：REST 页在采集中途崩溃时，规范化曾接受一个缺元素的页——修复后 `infrastructure/canonical/normalizer.py` 的 `_check_rest_unit` 会重新严格解码首次交付页（`infrastructure/revision/row_integrity.py::page_elements`），缺失元素必须能在另一页的已提交批次中逐个找到，否则 `CanonicalUnitIncomplete`，不写任何行；
  - D3E-R2/R3（`c326434`、`7e9e084`，早于本次 G3-D 但仍在 D3E 的 REVIEW_PENDING 范围内）已让 reconciler 与 store 用同一套核对读取；
  - G3-P（`dcfe8b7`）把 `row_integrity.py` 的归档行读取从"逐行 slice"改成一次 `take`，`batch()` 的列漂移检查改为先比较键集合——首个失败行与报错文本不变（新回归测试 `tests/infrastructure/revision/test_channel_reconcile.py`），只是加速，不改语义。
  - **已知残留**（见 §3 D-33-CAP）：`_check_rest_unit` 里为缺失元素查找持有者仍是逐 256 个 key 一批的 `In(observation_key, …)` 扫描（`infrastructure/canonical/normalizer.py` `_KEY_CHUNK = 256`），与 G3-P 在 `pit/selector.py` 里替换掉的模式属同一类（PyIceberg 超过约 200 字面量即不裁剪）；G3-P **没有**触碰这一处，`ac2daef` 的提交信息明确把它列为"(LOW-MED, documented) follow-up"，容量未测。
- **测试**：D3B `tests/infrastructure/revision/test_rest_identity.py`、`test_rest_policies.py`、`test_channel_precedence.py`；D3C `tests/infrastructure/parser/test_binance_rest_decoder.py`、`test_binance_rest_pagination.py`；D3D `tests/infrastructure/collector/test_binance_rest.py`、`test_binance_rest_checkpoints.py`、`test_binance_rest_client_boundary.py`、`test_binance_rest_static.py`、`test_binance_rest_contract.py`；D3E `tests/infrastructure/revision/test_rest_store_unit.py`、`test_rest_store_postgres.py`、`test_channel_reconcile.py`；G2 `tests/infrastructure/redteam/test_rt_crash_points.py`（REST 页中途崩溃）、`test_rt_time_units.py`（亚毫秒精度不可比较）。
- **commit**：D3A 设计门 `7111e54` + R1 `ed526f7`（接受门 `2e40b36`，[验收记录](2026-09-25-d3a-adr-0027-acceptance.md)）；D3B `3b267a0` + R1 `02c0418`（接受门 `0c3af31`，[验收记录](2026-09-25-d3b-rest-foundations-acceptance.md)）；D3C `643cf45` + R1 `6b9e670`（接受门 `960b552`，[验收记录](2026-09-25-d3c-rest-decoder-acceptance.md)）；D3D `61dd9bf` + R1 `c06b9fa`（接受门 `300bf33`，[验收记录](2026-09-25-d3d-rest-collector-acceptance.md)）；D3E `21e31f5` + R1 `52f7477` + R2 `c326434` + R3 `7e9e084`；G2-R1a `aef4ce7`；G3-P `dcfe8b7`。
- **状态**：⚠️ **部分验收**——D3A～D3D（设计、纯函数、decoder、collector）均已由 Codex 验收；**D3E（store + reconciler）与其后所有返修（含本次 G2/G3-P）没有验收门 commit，是 REVIEW_PENDING**。G2 红队的修复让"REST 数据可以进数据集"这件事在跨阶段攻击下更站得住脚，但仍然是未经 Codex 独立复核的实现。

### #14 — WebSocket live tail 只在历史 backfill、REST gap reconciliation 与重放幂等验收后启用（可不启用）

- **实现**：无 WebSocket 代码；[`docs/reviews/2026-09-25-d4-live-tail-gate.md`](2026-09-25-d4-live-tail-gate.md) 记录不启用的理由。
- **测试**：`test_architecture_boundaries.py` 类测试间接保证没有引入网络长连接依赖（本次 G3-D 实际运行，PASS）；没有专门的"D4 门"测试。
- **commit**：`a536ef3`（D4 门记录）。
- **状态**：⚠️ **REVIEW_PENDING**（门记录本身标注"本记录是 REVIEW_PENDING 提案，不是验收"）。结论本身（不启用）符合 ADR-0022 与 roadmap #14 的强制要求，逻辑上站得住，但记录尚待 Codex 正式确认。

### #15 — Canonical trades / bars_1m 绑定 Raw lineage；派生按位一致；有快照 ID 且可时间旅行

- **实现**：`infrastructure/canonical/normalizer.py`（E1）、`infrastructure/canonical/resample.py`（E4）。
- **G3-D 新增**：G2 红队直接针对 normalizer 的三处正确性攻击均已修复（`aef4ce7`，G2-R1a）：
  - **RT-1**：规范化崩溃后读取方（F1/E3/F3）曾把一个已提交前缀当作完整单元——`verify_unit`（完整与受限两种）现在要求计划的每个 batch 都已提交，缺失即 `CanonicalUnitIncomplete`（`CatalogIntegrityError`，fail closed），重跑规范化后自愈；
  - **RT-2**：归档替换尚未规范化时，数据集仍会选到旧版——`QualityReporter`（E3，及 F3 的 `existing_only` 复算）现在要求分区内每条 Raw 元素修订在绑定快照上都有 Canonical 镜像，否则 `RawNotDerived`（`QualityReportError`），在任何缺口 batch 写入之前拒绝；
  - **RT-3**：见 #13（REST 页元素完整性），同一提交也修复了这里。
  - `ac2daef`（G2-R2）额外收紧：单元已有已提交规范化 batch、但行被从 Canonical 表删除，现在也判为 `CatalogIntegrityError`（篡改），而不是复用 RawNotDerived 的"真实滞后"语义——这是 cursor-agent 只读复核发现的一处全量门失败（`test_a_deleted_canonical_row`），G2-R2 修的。
  - G3-P（`dcfe8b7`）加速了 `row_integrity.py` 的行证明路径（见 #13），normalize 侧无语义变化，10k 行单批 normalize 从 2.23–2.33 s 降到 2.03 s。
- **测试**：`tests/infrastructure/canonical/test_normalizer.py`（含 G2-R1a 新增的 `test_a_rest_unit_the_store_has_not_finished_is_refused_until_it_has`）、`test_normalizer_postgres.py::test_dual_lineage_normalization_recovery_and_mapping_on_postgres`、`test_a_forged_raw_row_is_refused_on_postgres`；`tests/infrastructure/quality/test_reporter.py`（RT-2 的 `RawNotDerived` 用例）；`tests/infrastructure/pit/test_selector.py`（读方拒绝不完整单元）；`tests/infrastructure/redteam/test_rt_tamper.py`（删除 Canonical 行，含 G2-R2 新增用例）；`tests/infrastructure/canonical/test_resample.py`（10 个用例，覆盖"只吃已选 1m bar、不补缺、按位重跑一致"）。
- **commit**：E1 `9b8674a` + R1 `65aedd6` + R2 `f6c29c9` + R3 `8c9109b` + R4 `42530a4`；E4 `8d69ef6` + R1 `3c32e2e`；容量批次 G3-S `bd1d941` + R1 `f92afdf`、G3-S2 `1964e33`、G3-S3 `7a70fbd` + R1 `0fe7471` + R2 `cb4bedd` + R3 `f4c5075`；G2-R1a `aef4ce7`（RT-1/2/3）；G2-R2 `ac2daef`（删除行的完整性判定）；G3-P `dcfe8b7`。
- **状态**：⚠️ **REVIEW_PENDING**——E0（`ca48a36` + R1 `e2951e4`，双 Raw→Canonical 设计）由 Raphael 批准方案 B（`3ee0519`），但这只是"接受方案 B 的架构设计"，不是"验收 E1 的实现"；E1 及其后所有 G3-S / G2 / G3-P 系列批次都没有 Codex 验收门 commit。

### #16 — listing 历史进入 `canonical.instrument_listings`；质量报告按分区自动生成

- **实现**：`infrastructure/canonical/listings.py`（E2，`ListingDeriver`）、`infrastructure/collector/binance_exchange_info.py`、`infrastructure/parser/binance_exchange_info.py`；`infrastructure/quality/reporter.py`（E3）、`infrastructure/quality/listing_report.py`。
- **G3-D 复核**：G2 红队 `test_rt_listings.py` 从跨阶段角度演练了"symbol 从未出现在任何快照""迟到快照移动变化点"两类攻击，均按既有设计拒绝（`NO_VISIBLE_LISTING` / `COMPETING_HEADS`），未发现新缺陷；无返修 commit。
- **测试**：`tests/infrastructure/canonical/test_listings.py`（24 个用例）、`tests/infrastructure/collector/test_binance_exchange_info.py`、`tests/infrastructure/parser/test_binance_exchange_info_decoder.py`、`tests/infrastructure/revision/test_exchange_info_rules.py`（12 个用例）、`test_exchange_info_store.py`；`tests/infrastructure/quality/test_reporter.py`（20 个用例，含 `test_a_bar_partition_report_lists_gaps_inputs_and_evidence_gaps`）、`test_listing_report.py`；`tests/infrastructure/redteam/test_rt_listings.py`（4 个用例）。
- **commit**：ADR-0029 设计证据 `4d3e6d8`；E2 `3b6038b` + R1 `1e3d325`；E3 `bd7b60d` + R1 `4587473`；QG-1（证据缺口独立表，ADR-0031）`e31ac04` + R1 `b7e48f6`；QG-2（证据缺口流式写入）`e9ff4ec`。
- **状态**：⚠️ **REVIEW_PENDING**——ADR-0029 / ADR-0031 由 Claude 依 Raphael"授权所有"接受，属架构决定层面的确认；E2 / E3 / QG-1 / QG-2 的**实现**均无 Codex 验收门 commit。

### #17 — availability / precedence policy 证据已产出、审阅并有测试；早期 `available_time` 缺证据取 `ingest_time` 并写证据缺口

- **实现**：归档侧 `infrastructure/revision/availability.py`、`precedence.py`（D2 范围内）；REST 侧 `rest_availability.py`、`rest_precedence.py`、`channel_precedence.py`（D3B 范围内）；exchangeInfo 侧 `exchange_info_availability.py`（E2 范围内）；D-HIST 假设叠加层 `infrastructure/pit/assumption.py`（ADR-0032）。
- **G3-D 复核**：G2 红队 `test_rt_assumption.py` 专门演练 ADR-0032 假设叠加在纯 REST 数据上的边界（REST revision 永不因假设移动 available_time），与 D-HIST 的设计声明一致，未发现缺陷。
- **测试**：`tests/infrastructure/revision/test_availability.py`（15 个用例）、`test_precedence.py`（23 个用例）、`test_channel_precedence.py`（27 个用例）；D-HIST 相关 `tests/infrastructure/pit/test_selector.py::test_without_the_assumption_history_before_ingest_is_invisible`、`test_the_bound_assumption_makes_an_archive_trade_available_at_event_time_plus_latency`、`test_the_assumption_never_moves_a_rest_revision`、`test_another_version_or_hash_of_the_assumption_is_refused`；`tests/infrastructure/redteam/test_rt_assumption.py`（2 个用例）。
- **commit**：归档侧随 D2 `ba9f417` + R1 `b05486b`；REST 侧随 D3B `3b267a0` + R1 `02c0418`；exchangeInfo 侧随 E2 `3b6038b`（REVIEW_PENDING）；D-HIST `03116ed`（Raphael 2026-09-25 批准推荐方案 A，ADR-0032 Accepted，**实现** REVIEW_PENDING）。
- **状态**：⚠️ **部分验收**——归档侧、REST 侧（D-33 policy 本身）已随 D2 / D3B 验收；**exchangeInfo 侧 availability policy 与 D-HIST PIT 假设叠加层的实现都是 REVIEW_PENDING**。roadmap 把 #17 标记的批次是"D2 / E"，D2 部分已满足，E 部分未满足。

### #18 — PIT 双截止 + maximal head，同输入按位一致；各类 fail-closed 情形；早期历史缺证据取 `ingest_time` 不算数据集级失败

- **实现**：`infrastructure/pit/selector.py`（F1）、`infrastructure/pit/view.py`、`infrastructure/pit/assumption.py`。
- **G3-D 新增**：G3-P（`dcfe8b7`）重写了 `PitSelector` 的 key closure 读取路径——不再用单个 `In(observation_key, 全部窗口 key)` 扫描全表（PyIceberg 超过约 200 个字面量就不再按统计裁剪，且这批 key 共享前 16 字符使截断字符串统计本就无法裁剪），改为按 UTC 日先只读 `(observation_key, 时间)` 定位含窗口 key 的小时、再按小时整读（每次 ≤ 10 万行）并用 Arrow `is_in` 本地过滤。新回归测试 `tests/infrastructure/pit/test_selector_scale.py`（`test_the_key_closure_equals_the_single_in_scan`：新旧两种读取路径在 > 200 key、8 个数据文件、一条 margin revision、窗口不对齐整点的场景下结果集合逐行相同；`test_a_selection_does_not_depend_on_how_the_read_is_fetched`；`test_wanted_rows_are_exactly_the_member_rows_of_the_range`）证明结果不变，`_key_closure` 本身的算法未改。这是**性能**改动，不改变 #18 要求的任何 fail-closed 语义或选择结果。
- **测试**：`tests/infrastructure/pit/test_selector.py`（`test_the_four_cutoffs_select_as_adr_0028_section_4`、`test_an_old_spec_reproduces_its_result_after_new_evidence`、`test_selection_is_deterministic`、`test_a_mismatch_stays_a_conflict`、`test_a_forged_canonical_row_is_refused`、`test_every_revision_is_owned_by_exactly_one_window` 等）、`test_selector_postgres.py::test_bound_snapshots_decide_on_postgres`、`test_selector_scale.py`（新增，G3-P）。
- **commit**：F1 `5ce7d3a` + R1 `f45fb78`；容量相关 G3-S2 `1964e33`；G3-P `dcfe8b7`。
- **状态**：⚠️ **REVIEW_PENDING**——测试矩阵覆盖 roadmap 列出的每一种 fail-closed 情形，G3-P 的规模测试进一步证明性能重写不改变结果，但 F1 没有 Codex 验收门 commit。

### #19 — 基础 Representation 与首批 FeatureProvider（接口先行）；Feature guard 防泄漏；Feature 结果可复现

- **实现**：`core/contracts/feature.py`（ADR-0030 契约）、`infrastructure/feature/runner.py`（F4 truncating runner）、`infrastructure/feature/observations.py`、`infrastructure/feature/dataset.py`（**G2-R1c 新增**，见下）、`plugins/features/bars.py`（`BarLogReturnProvider` 等三个首批特征）。
- **G3-D 新增（G2-R1c / G2-R1-I / G2-R2）**：G2 红队发现 **RT-6**——`FeatureRequest.manifest_content_hash` 此前是调用方自报、被无条件信任的字段，一个伪造 / 缺失的清单，或者选到另一个 spec 下的观测，仍能产出 `FeatureResult`。修复分三步：
  1. `aef4ce7` 之外，`0477bce`（G2-R1c）新增 `infrastructure/feature/dataset.py::feature_request_from_dataset`：清单经 `ManifestStore.load`（验证过的存储）加载，要求调用方的 PIT spec 与清单自身一致，在清单自己的快照下按清单确定的 `selection_id` 重新读取数据集行，要求每条观测的 lineage 都被清单绑定，并在清单的 spec 下（含 ADR-0032 假设时的 `selected_rows` 生效时间）重新选择，要求最终观测恰好等于这些证明过的行；`run_feature` / `pit_feature_request` 本身不变，继续是不声明数据集关系的 ad-hoc / 测试路径（见 §3）；
  2. `17ff897`（G2-R1b）让 `ManifestStore` 经 `ManifestVerifier`（即 `DatasetBuilder`）在 `persist` 与 `load` 时都重新验证清单（重放并逐字段核对，`DatasetBuilder.verify_manifest`）；
  3. `c756bbe`（G2-R1-I）把 (1)(2) 接起来：`feature_request_from_dataset` 只接受 `DatasetBuilder.manifests()` 返回的、带验证能力的 `ManifestStore`；`ac2daef`（G2-R2）进一步收紧为 `feature_request_from_dataset` / `load_manifest` 只接受调用方传入的 `DatasetBuilder` 本身、经它自己的验证存储加载，不再接受调用方另传的验证器。
- **测试**：`tests/test_feature_contracts.py`；`tests/infrastructure/feature/test_feature_runner.py`（`test_the_provider_only_ever_sees_the_visible_set`、`test_a_leaky_provider_cannot_leak_through_the_runner`）、`test_feature_pipeline.py`；`tests/test_feature_contract_suite.py`（`test_feature_suite_kills_faulty_implementation`、`test_a_value_from_zero_inputs_cannot_be_built`）；`tests/plugins/features/test_bar_features.py`；**新增** `tests/infrastructure/feature/test_feature_dataset.py`（7 个用例，含 `test_a_manifest_bound_run_is_bit_identical_across_reruns`、`test_an_assumption_bound_interval_dataset_feeds_its_effective_times`、`test_observations_that_are_not_the_datasets_proven_rows_are_refused`）；`tests/infrastructure/redteam/test_rt_features.py`（RT-6，3 个用例，伪造 / 缺失 / 另一 spec 的清单）。
- **commit**：ADR-0030 提案 `4472282`；F4 `bc558e3` + R1 `b5ffe98`；G2-R1c `0477bce`；G2-R1b `17ff897`；G2-R1-I `c756bbe`；G2-R2 `ac2daef`。
- **状态**：⚠️ **REVIEW_PENDING**——泄漏测试与可复现性测试都已存在；本次 G3-D 新增的"数据集绑定特征运行"路径（`feature_request_from_dataset`）直接回应了 roadmap #19 "Feature 结果可复现"里"绑定哪个清单"的问题，但同样没有 Codex 验收门 commit。**诚实边界**：`run_feature` / `pit_feature_request` 这条更早的 ad-hoc 路径仍然存在、仍被 F1/F4 的既有测试使用，它不声明任何数据集关系（见 §3"ad-hoc 特征路径"）。

### #20 — 首切片端到端：归档 → Raw → Canonical → PIT → Research Dataset + manifest → Representation

- **实现**：`infrastructure/dataset/builder.py`（F3 `DatasetBuilder`）、`infrastructure/universe/builder.py`（F2 `UniverseBuilder`）、`infrastructure/dataset/manifests.py`、`infrastructure/dataset/selection.py`（DS-1，ADR-0033）。
- **G1 端到端验收**：`tests/infrastructure/e2e/test_phase1_first_slice.py::test_phase1_first_slice_archive_to_representation`（BTCUSDT + ETHUSDT：归档 + REST → Raw → 跨通道边 → Canonical → 上市历史 → 质量报告 → PIT → F2 标的池 → F3 数据集 + manifest 写入生产表 → E4 重采样 → F4 特征，重建逐位相同）、`::test_pit_selection_with_and_without_the_archive_assumption`（ADR-0032 保守 / 假设两种规格）、`test_phase1_first_slice_postgres.py`（真实 PostgreSQL）；验收记录 `docs/reviews/2026-09-25-phase1-e2e-acceptance.md`（基线 `6fba363`，19 passed 1 skipped）。
- **G3-D 新增**：G1 之后的 G2 红队（`ccb57bc` 起）从**攻击**角度补充了 G1 的**正向**证据——`test_rt_arrival_orders.py` 演练全部 16 种"归档 / REST / reconcile 到达顺序"下 manifest 形状与行集合是否一致（无需修复：任何合法顺序都得到相同结果），`test_rt_crash_points.py` 演练 G1 同一条链上每个提交点崩溃后重跑的完整链条恢复。这两组测试发现的问题（RT-1、RT-3、RT-4、RT-5、RT-6）都在 F3/F4 这一跳（数据集与特征），已在 §13/§15/§19/§20 各自小节与 §2 列出并已修复。G3-P（`dcfe8b7`）分别在 PIT 选择、行证明、feature runner 三处做了规模测量，其中 feature runner 的 `content_hash()` 成本没有被削弱或绕过（见 §2）。
- **commit**：F2 `87a3d3e`；F3 `a167d87` + R1（F3-I）`08913b1` + R2（F3-R1）`762643f`；DS-1 `6fba363`；G1 `bf93cd2`；G2 `ccb57bc` + 四次返修 `aef4ce7`/`17ff897`/`c756bbe`/`ac2daef`；G3-P `dcfe8b7`。
- **状态**：🔄 **REVIEW_PENDING**——端到端验收测试已存在并通过（G1），跨阶段红队（G2）与规模性能（G3-P）在其上补充了对抗性证据与性能数据，均未改变链路的正确性结论，但也都没有让 #20 从 REVIEW_PENDING 变成已验收；待 Codex 复核。

### #21 — 全程：PostgreSQL 无行情、Git 无凭据/数据、无账户/交易端点、无 NATS；每批 pytest/ruff/format/mypy 全绿；每个接受点经 Codex 复核后推送

- **实现**：`tests/infrastructure/revision/test_repository_hygiene.py`（残留/密钥检查，逐批复用）；`infrastructure/settings.py`（H8/H9 结构性防线）。
- **本批（G3-D）自查**（仅本批文档更新，不代表对 G2/G3-P/历史批次的重新验证）：
  - `uv run pytest tests/test_docs_consistency.py tests/test_architecture_boundaries.py`：**17 passed**（本次实际运行）
  - `git status` / `git diff`：只修改了 `docs/reviews/2026-09-25-phase1-close-evidence.md`（本文件）、`docs/reviews/2026-09-25-phase1-review-guide.md`（新增）、`infrastructure/README.md`；未触碰冻结契约、ADR、`PROJECT_STATUS.md`、`PROJECT_MEMORY.md`、任何生产 / 测试代码
- **G2/G3-P 各自的自查记录**（转述自对应 commit message，本文档未重新运行）：
  - G2（`ccb57bc`）：92 个跨阶段攻击，84 通过（拒绝或按设计复现），6 个发现以 8 个严格 xfail 固定；**未改动任何生产代码**
  - G2-R1a/R1b/R1c/R1-I/R2（`aef4ce7`/`17ff897`/`0477bce`/`c756bbe`/`ac2daef`）：每步提交信息都记录了该步聚焦测试的通过数（如 G2-R1-I 记录"feature, RT-6, dataset, e2e: 70 passed"）
  - G3-P（`dcfe8b7`）：commit message 内联记录了每处改动前后的实测耗时（PIT key closure、normalize/行证明），未附全量测试数
- **历史批次**：`PROJECT_STATUS.md` §3 对每个已验收批次都记录了 Codex 独立运行的全量测试数（从 1433 项到 4380 项递增）与静态检查结果；`PROJECT_STATUS.md` 最后一次更新是 `9b2124f`（记录 G2 六个发现），**没有**覆盖 G2 的四次返修与 G3-P；本文档同样没有重新运行这些历史全量，如实转述其记录，不代为担保。
- **推送**：本批 commit 仅本地提交，**不 push**（CLAUDE.md §10.8、ADR-0025）。
- **状态**：⚠️ **本批（G3-D）自查通过；G2 / G3-P 及更早历史批次的全绿记录未被本文档重新验证，按各自 commit message 与 `PROJECT_STATUS.md` 记录为准**。

---

## 2. G2 跨阶段红队与四次返修（本次 G3-D 新增小节）

### 2.1 红队套件本身

`tests/infrastructure/redteam/`（`ccb57bc`，README 见 `tests/infrastructure/redteam/README.md`）是对整条链路（D0/D1/D2 归档、D3D/D3E REST、E1 normalizer、D-33 reconciler、E2 listings、E3 reports、F1 PIT、F2 universe、F3 dataset + manifest、F4 features）的**复合攻击**套件：每个测试在同一个小型 SQLite 世界（`dataset_support.World`）上跑**真实**生产阶段，只有传输层、时钟与崩溃 / 篡改注入点是测试代码；已被各阶段自身 contract / unit 测试覆盖过的单点攻击不在此重复。九个测试模块：

| 模块 | 攻击类别 |
|---|---|
| `test_rt_time_units.py` | 毫秒 / 微秒切换边界、亚毫秒精度不可比较 |
| `test_rt_replacement.py` | 归档替换（同路径新校验和）、到达顺序、ADR-0032 假设绑定 + 替换 |
| `test_rt_arrival_orders.py` | 16 种合法到达顺序、manifest 构建时机 |
| `test_rt_crash_points.py` | 19 个提交点崩溃后重跑、归档 / Canonical / REST 单元中途停止 |
| `test_rt_tamper.py` | 伪造 / 删除 Canonical 行、证据缺口行、质量报告、listing 行；重写 Parquet 文件；伪造清单 |
| `test_rt_specs.py` | 跨表快照、过期 / 伪造证据绑定哈希 |
| `test_rt_listings.py` | symbol 从未出现、迟到快照移动变化点 |
| `test_rt_assumption.py` | ADR-0032 假设绑定在纯 REST 数据上的边界 |
| `test_rt_features.py` | 特征运行绑定伪造 / 缺失 / 另一 spec 的清单（RT-6） |

**结果（引自 `ccb57bc` commit message）**：92 个用例，84 个按设计通过（拒绝，或在允许的情形下复现），6 个发现以 8 个严格 `xfail`（`raises=` 固定当时的失败模式）标记，提交时**未改动任何生产代码**——发现即记录，不在同一提交里就地修复。每个失败用例还额外断言 `research.dataset_selections` / `research.dataset_manifests` 保持自己的 head：不产生"部分成功"状态。

### 2.2 六个发现与四次返修

| ID | 严重度 | 攻击 | 修复提交 | 修复方式 |
|---|---|---|---|---|
| RT-1 | 高 | 规范化崩溃后，读取方（F1/E3/F3）把已提交前缀当完整单元 | `aef4ce7`（G2-R1a） | `verify_unit` 要求计划的全部 batch 都已提交，否则 `CanonicalUnitIncomplete`（fail closed） |
| RT-3 | 高 | REST 页中途崩溃时，规范化接受缺元素的页 | `aef4ce7`（G2-R1a） | `_check_rest_unit` 重新严格解码首次交付页，缺失元素须在另一页的已提交批次中逐个证明，否则拒绝 |
| RT-2 | 中 | 替换归档尚未规范化时，数据集仍选旧版 | `aef4ce7`（G2-R1a） | `QualityReporter` 要求分区内每条 Raw 修订都有 Canonical 镜像，否则 `RawNotDerived` |
| RT-4 | 中 | 伪造清单（删除排除项）可被保存 / 读取 | `17ff897`（G2-R1b） | `ManifestStore` 经 `DatasetBuilder.verify_manifest` 在 persist / load 时都重放并逐字段核对 |
| RT-5 | 中 | 首条 D-33 证据边出现后，旧清单无法重建 | `17ff897`（G2-R1b） | 绑定要求判定改为"在清单描述的那次构建时判定"，而非当前状态 |
| RT-6 | 中 | 特征运行信任未验证的清单哈希 | `0477bce` + `c756bbe`（G2-R1c / G2-R1-I） | `feature_request_from_dataset` 只经 `DatasetBuilder` 自己的验证存储加载清单 |

`ac2daef`（G2-R2）是 cursor-agent（只读）对以上四次返修的独立复核，额外发现并修复了两处：

- **（高）** RT-5 的修复留了一个口子：`replay` 路径只要求"选择 id 下存在某个批次"，一个人为构造的批次（未经真正的构建流程绑定检查）就能绕过验证——修复后 replay 还必须能找到一份**持久化的清单**，其内容点名了这批数据的快照，否则视为"新构建"而非"重放"；
- **（中）** `feature_request_from_dataset` / `load_manifest` 此前接受调用方另传的验证器，改为只接受调用方传入的 `DatasetBuilder` 本身；
- 同批还修了一个由红队套件本身暴露的全量门失败：RT-2 的 `RawNotDerived` 曾同时覆盖"真实滞后未规范化"与"规范化 batch 已提交、但行被从 Canonical 删除"两种情况，后者现在正确判为 `CatalogIntegrityError`（篡改）。

修复后，`tests/infrastructure/redteam/` 中**没有**残留的 `xfail` 标记（`grep -rn xfail tests/infrastructure/redteam/*.py` 只在 README 里出现，测试文件本身为零命中）——README 表格逐条标注了每个发现"fixed RT-n, G2-R1x"。这四次返修**没有**经过 Codex 独立复核，仍在 D3E 起的 REVIEW_PENDING 窗口内。

---

## 3. G3-P 规模性能（本次 G3-D 新增小节，语义不变）

`dcfe8b7` 在受限探针（≤ 1 万行、SQLite catalog、2500M 内存上限）下做了三处**纯性能**改动，均以回归测试证明结果集合 / 首个失败行 / 报错文本不变：

1. **PIT key closure 读**（`infrastructure/pit/selector.py`）：见 #18。10k 笔成交、40 个数据文件：1 天窗口（1 万 key）2.64 s → 0.76 s；3 小时内成交的 1 小时窗口（3,333 key）0.92 s → 0.41 s；单文件 1 天窗口 0.29 s → 0.22 s，单文件 1 小时窗口 0.031 s → 0.041 s（因改为多次窄扫描，小规模反而略慢）。按 ≤ 200 key 分块扫描实测更慢（1 万 key 时 5.3 s 对 1.3 s），未采用。
2. **Feature runner**（`infrastructure/feature/runner.py`）：**未改代码**，只改正了一处文档字符串。700 根 bar × 700 个评估时刻耗时 4.20 s，约 90% 是每次调用对子请求整个前缀各算一次的 `content_hash()`（Provider 的 `FeatureResult.build` 与 `check_answers`）及子请求校验——这是核心契约的一部分，随前缀长度线性增长；在 runner 内增量构建可见集合只快约 1%（4.15 s），因收益太小被放弃。commit message 明确写"需要改核心才能进一步优化，不能靠削弱检查"。
3. **行证明**（`infrastructure/revision/row_integrity.py`）：归档行从"逐行 slice"改为一次 `take`（首个失败行与报错文本不变，新测试固定在 `tests/infrastructure/revision/test_channel_reconcile.py`）；`batch()` 的列漂移检查改为先比较键集合。10k 行归档单批 normalize：2.23–2.33 s → 2.03 s；冷 1 小时 PIT 选择（含单元证明）：2.05–2.10 s → 1.81 s。commit message 记录多批次场景的主要成本在 PyIceberg 每次扫描重读全部 manifest（40 批 normalize 约 15 s），**本批未处理**。

**读得诚实的边界**：以上全部是 ≤ 1 万行的受限探针，不是生产规模（BTC 一整天 100～300 万行）的基线；#13 提到的 `_check_rest_unit` 容量（同一类 `In()` 字面量上限问题）**不在本批范围**，仍是 follow-up。G3-P 没有 Codex 验收门 commit。

---

## 4. 本批（G3-C）capacity_probe 扩展的证据

### 4.1 新增能力

`infrastructure/tools/capacity_probe.py` 新增三个可选阶段（原有 `--rows N` 的四段基线不变）：

| 参数 | 新增测量阶段 | 覆盖的生产模块 |
|---|---|---|
| `--rest` | `ingest_rest`、`normalize_rest`、`channel_reconcile` | D3D `BinanceSpotRestCollector`、D3E `RestRevisionStore`、`ChannelReconciler`（D-33） |
| `--dataset` | `universe_build`、`dataset_build` | E2 `BinanceSpotExchangeInfoCollector` / `ExchangeInfoSnapshotStore` / `ListingDeriver`（fixture，未计时）、F2 `UniverseBuilder`、F3 `DatasetBuilder`（写入 `research.dataset_selections` + manifest，ADR-0033） |
| `--feature` | `ingest_klines_archive`、`normalize_klines`、`feature_bar_log_return` | D1/D2（klines 归档）、E1 `CanonicalNormalizer`、F4 `run_feature` + `plugins.features.bars.BarLogReturnProvider` |

只 import 生产模块；本地重实现了合成 REST 页 / exchangeInfo 响应 / klines 归档，网络层用 `httpx.MockTransport` 固定应答，从不导入 `tests/`。

### 4.2 N=1000（全部三个可选阶段）实测输出

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

REST 尾部与归档尾部描述的是同一批成交（同构造字段），`channel_reconcile` 确认全部 1000 笔都判定为内容相同并记入一条 D-33 优先证据边——这是对 D3E-R2 provenance 校验代码路径的一次真实执行，不是新的正确性证明。

### 4.3 读得诚实的边界（工具自带的 `notes` 字段原文）

- `ingest_rest` / `normalize_rest` / `channel_reconcile` 只覆盖**一页**已提交的 REST 页（`min(rows, PAGE_LIMIT)` 条），网络层是假的，不测多页链、重试、D3E-R1/R2 的崩溃恢复路径。
- `dataset_build` 选的是与 `pit_select_1h` 相同的一个 UTC 小时，不是整天——生产环境的成交 Research Dataset 本来就是按小时构建的，从不按天（**D-MAN**，见 §5）。
- `feature_bar_log_return` 只在单一个"极远未来"求值时刻跑一次，不测真实的求值时间网格、缺口，也不测已实现的另外两个 provider。
- klines 归档固定封顶 `min(rows, 1440)`：一个 UTC 天只有 1440 个不同的一分钟 bar。

### 4.4 合并前验证（本批文档更新，G3-D 实际运行结果）

```
uv run pytest tests/test_docs_consistency.py tests/test_architecture_boundaries.py
  -> 17 passed in 0.16s
```

（套了 `systemd-run --user --scope --quiet -p MemoryMax=2G -p MemorySwapMax=0`；本次未跑 ruff / mypy / 更大范围的测试，因为本批**只改文档**，见 CLAUDE.md §10.5 对代码改动的要求——本批不修改代码，故不适用。）

---

## 5. `PROJECT_STATUS.md` §6/§7 记录的已知限制（原样转述 + 本次 G3-D 新增，供 Codex 复核时对照）

以下前七条不是本文档新发现，是 `PROJECT_STATUS.md` §6（当前待决策）与 §7（当前风险）里已经记录、尚未解决的事项；**D-33-CAP、RT-3 残留、ad-hoc 特征路径、单 writer 假设**四条是本次 G3-D 从 G2 / G3-P 的 commit message 中核实并新增的：

| ID / 主题 | 内容 | 来源 |
|---|---|---|
| **D-MAN** | 数据集清单逐条列出来源链与证据缺口：K 线数据集按天没问题（每天 1440 条），成交按天会有数百万条，一行清单放不下。已决定：Phase 1 成交数据集**按小时**构建；改契约（升 major）留到 Phase 2 按需另立 ADR。**这是已知限制，不是待修的 bug**。 | `PROJECT_STATUS.md` §6 |
| **键闭包可达范围** | 同一笔成交的副本之间若有超过一天的空档（链断开），两段各自被当作独立记录，冲突看不到；相邻两天的质量报告会各自列出跨天冲突（按设计）；这种数据只能是严重损坏，需要以后专门的质量规则检测。另外按小时选择时现在要读前后各一天的分区，生产规模下的耗时尚未测量。G3-S3-R1（`0fe7471`）已把"同一观察键、相邻不超过一天"的全部 revision 一起纳入选择（传递闭包），但闭包本身的**跨度上限仍是一天**，超过这个跨度的断链仍是已知边界。 | `PROJECT_STATUS.md` §7 |
| **D-QGAP / 证据缺口独立表大小** | 成交一天 100～300 万条都有缺口（D-HIST），报告行如果逐条内嵌证据缺口会到数 GB；已决定方案 A——缺口改写进独立只追加表（ADR-0031，QG-1/QG-2），报告行只存引用与计数。 | `PROJECT_STATUS.md` §6 |
| **容量基线（G3-S 系列 + G3-P）** | 规范化"整个单元一次性读入"约每行 19 KB（BTC 一整天 100～300 万行会超出 WSL 约 15 GB 内存）；改为固定快照 + 分批窗口后，30 万行规范化新增常驻约 0.8 GB；时点选择按小时约 0.27 GB 峰值，但**选择结果本身每行约 11 KB，成交数据必须按小时（或更短）分段选择，整天选择（约 30 GB）不可行**。G3-P（`dcfe8b7`）在 ≤ 1 万行规模上把 PIT key closure 与行证明加速了 2～4 倍，但**没有**在生产规模（百万行级）下重新测过；多批次场景的 PyIceberg manifest 重读成本（40 批约 15 s）明确未处理。 | `PROJECT_STATUS.md` §7；本文档 §3 |
| **D3E provenance 边界** | 由另一页首次交付的元素，只证明那一页的响应记录合法、元素继承其序号与时间，**没有**重新解码那一页正文；归档行同理不在比对时重新解析归档文件。R2 起 reconciler 与 store 使用同一套核对，但这个边界本身没有改变。 | `PROJECT_STATUS.md` §7 |
| **D-33 精确比较的代价** | REST 以毫秒交付、2025 年起归档为微秒，同一笔成交若带亚毫秒位就无法证明相等，只能 fail closed——正确但降低 REST 补尾的价值；是否改请求微秒需要以后单独验证并批准。 | `PROJECT_STATUS.md` §7 |
| **D-HIST 假设叠加层** | 早于本机采集的历史行情默认仍取 `available_time = ingest_time`（保守）；ADR-0032 的"事件时间 + 5 秒可用"假设必须由数据集规格显式绑定才生效，不绑定就维持保守——这是设计如此，不是 bug，但意味着**任何不显式绑定该假设的数据集都用不了 D-HIST 之前的历史数据**。 | `PROJECT_STATUS.md` §6 |
| **D3D/D0 大体量吞吐未测** | D1 已真实验证两个单位边界日的 kline 与 aggTrades；大体量 BTC 日归档尚未做内存/吞吐基线，批量 backfill 前必须先完成容量检查与可恢复 checkpoint；G3-P 的规模测量同样只到 1 万行。 | `PROJECT_STATUS.md` §7 |
| **D-33-CAP（本次 G3-D 新增）：RT-3 修复路径的容量残留** | RT-3 的修复（`_check_rest_unit`，`infrastructure/canonical/normalizer.py`）为缺失元素找持有者时，仍用逐 256 个 key 一批的 `In(observation_key, …)` 扫描；这与 G3-P 在 `pit/selector.py` 里替换掉的模式是同一类风险（PyIceberg 超过约 200 字面量即不再按统计裁剪）。`ac2daef` 的提交信息把它明确列为"(LOW-MED, documented) follow-up"：**容量未测，未修**，只是被记录下来，不是已解决问题。 | `ac2daef` commit message；`infrastructure/canonical/normalizer.py` `_KEY_CHUNK = 256` |
| **ad-hoc 特征路径仍是独立入口（本次 G3-D 新增）** | G2-R1c 新增了 `feature_request_from_dataset`（清单验证过的、声明数据集关系的路径），但**没有**移除或改造更早的 `run_feature` / `pit_feature_request`：这条路径仍然存在，仍被既有的 F1/F4 测试使用，**不声明任何数据集关系**（不绑定 manifest，调用方自己保证 PIT 视图正确）。两条路径长期并存本身不是缺陷，但意味着"通过 Feature guard 测试"不能自动说明一次特征运行经过了数据集清单验证——要看它走的是哪条入口。`ac2daef` commit message 把这一点列为"(MED, documented)"。 | `0477bce`、`ac2daef` commit message；`infrastructure/feature/runner.py` 对比 `infrastructure/feature/dataset.py` |
| **单 writer 假设（本次 G3-D 新增，贯穿全部批次）** | ADR-0023 §7 起，每张生产表在设计上只有**一个** writer；并发只靠父 snapshot 的乐观冲突检测 + 有界重试兜底（`infrastructure/revision/store.py`、`infrastructure/catalog/iceberg_adapter.py` 的 `commit.retry.num-retries=0` 设定）。G2 红队没有测试"多个并发 writer 同时写同一张表"这一类场景（`test_rt_crash_points.py` 测的是单进程崩溃重启，不是并发写）；G3-P 的规模测量同样是单进程。多 writer 并发（例如未来的批量 backfill 需要并行）仍是未经验证的假设，不是已证明安全的场景。 | `docs/adr/0023-bitemporal-revision-data.md` §7；`infrastructure/README.md`"已知限制：namespace 不应命名为 staging；orphan 文件只由后续显式 maintenance 清理" |

---

## 6. 小结（供 Codex / Raphael 参考，不是结论）

1. **已验收（Codex 独立复核 + 验收门 commit）**：#1～#12，以及 #17 中归档侧与 D3B（REST）侧的 policy 部分。对应 roadmap 批次 A2/A2r、A3a、A3b、B1、B2、B3、C1、C2、C3、D0、D1、D2、D3A、D3B、D3C、D3D。
2. **REVIEW_PENDING（实现已提交，验收门缺失）**：#9 后半（exchangeInfo/证据缺口/DS-1 三张表）、#13 后半（D3E 及其全部返修，含本次 G2/G3-P）、#14（D4 门记录）、#15、#16、#17 后半（exchangeInfo policy、D-HIST）、#18、#19（含新增的数据集绑定特征路径）。对应批次 D3E 及其后的一切：D3E-R1/R2/R3、D4、E0～E4、F1～F4、QG-1/QG-2、DS-1、G1、**G2 及其四次返修**、**G3-P**。
3. **#20**：G1 已补齐端到端验收测试与记录，G2 红队从攻击角度补充了跨阶段证据、发现并修复了 6 个问题（均在 F3/F4 这一跳），G3-P 补充了性能数据；现仍为 REVIEW_PENDING。
4. **本次（G3-D）范围内自查通过，不改变以上结论**：只更新三份文档（本文件、新增的复核指南、`infrastructure/README.md`），未改动任何生产 / 测试代码；`tests/test_docs_consistency.py` + `tests/test_architecture_boundaries.py` 17 passed。
5. **已知限制清单（§5）新增四条**：D-33-CAP（RT-3 修复路径的容量残留，未修、已记录）、ad-hoc 特征路径与数据集绑定路径长期并存、单 writer 假设从未在并发场景下被测试过；连同既有的 D-MAN、键闭包可达范围一起，构成 Codex 复核时应重点核对的边界清单。
6. **给 Codex 的建议顺序**（仅为建议，不代替决策）：D3E（含 R1/R2/R3）与其上的 G2 六项修复是后续一切的地基，逻辑上应先补上验收门，再评估 E0～G3-P 这一整段是否可以一次性批量复核，还是要拆回逐批复核；详细的复核分组建议见 [`2026-09-25-phase1-review-guide.md`](2026-09-25-phase1-review-guide.md)。

---

*本文件起草于批次 G3-C，本次由 Claude Code（Sonnet 5）依 Raphael 2026-09-24 的持续执行授权更新至批次 G3-D（`HEAD = ac2daef`）。草稿，待 Codex / Raphael 复核；不构成验收结论。*

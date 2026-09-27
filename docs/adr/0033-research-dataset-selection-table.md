# ADR-0033: 物化 Research Dataset 选择表登记为生产表（DS-1）

| 字段 | 值 |
|---|---|
| 状态 | **Accepted**（2026-09-25） |
| 日期 | 2026-09-25 |
| 决策者 | Claude Code（Opus），依 Raphael 2026-09-25 明确授权（"一切都你自己决定"；红线除外） |
| 起草者 | Claude Code（Opus），DS-1 批次工作中提出 |
| 相关 Phase | Phase 1（roadmap #18 / #20；F3 物化 Research Dataset） |
| 影响范围 | Data / Infrastructure（新增一张 additive 表；`infrastructure/dataset/selection.py` 的提议正式生效） |
| 是否破坏兼容 | 否：已冻结的 14 张表定义与哈希不变；`ResearchDatasetManifest` / `DatasetRef` 契约不变 |
| 前置 | [ADR-0023](0023-bitemporal-revision-data.md) §6、[ADR-0024](0024-historical-tradable-universe.md) §6 |

## 背景

`03-data.md` §7.1（A2 冻结）把 Phase 1 首切片的十四张逻辑 Iceberg 表登记为生产表，但明确把"物化 Research
Dataset 表的命名"留给 PIT 实施批次提出：`infrastructure/dataset/selection.py` 给出了这张表应有的形状
（`SELECTION_SCHEMA`）与构造函数（`selection_table_definition`），但只是**提议**——它不进入 `PHASE1_TABLES`，
不创建任何表，字面注释写明"生产环境不能物化 Research Dataset"。

`DatasetBuilder`（F3）已经实现并测试通过：一次构建把每个选中的 key 写成一行——`selection_id`（批次号）、
指向选中 Canonical revision 的指针（`canonical_table` + `revision_id`，不复制 payload）、`observation_key`、
`event_time`，以及该选中在其中生效的 simulation 区间（`effective_from` / `effective_until`）。因为生产表尚未
登记，测试只能用一张同形状、不同名字的 test-only 表（`research.f3test_selection`，见
`tests/infrastructure/dataset/dataset_support.py`）。这与 C3 首切片八张表、D3B 四张 REST 表、E2 一张
exchangeInfo 表、QG-1 一张证据缺口表最终都从"提议"转正为登记生产表的先例一致；Research Dataset 表是这条
链路里唯一还停留在提议阶段的一张。

## 裁决

1. 把 `infrastructure/dataset/selection.py` 提议的表形状登记为第 15 张 Phase 1 生产表
   `research.dataset_selections`（additive，`catalog/phase1_tables.py` 追加，不改变前 14 张的定义与哈希）：

   | 列 | 类型 | 必填 | 含义 |
   |---|---|---|---|
   | `selection_id` | string | 是 | 数据集构建 id（= 批次 id） |
   | `canonical_table` | string | 是 | 选中 revision 所在的 Canonical 表 |
   | `symbol` | string | 是 | Canonical symbol，如 `BTC-USDT` |
   | `observation_key` | string | 是 | `RevisionRecord.observation_key` |
   | `revision_id` | string | 是 | 选中的 Canonical revision |
   | `event_time` | timestamptz | 是 | 该 revision 的 `event_time` / `interval_start`（UTC） |
   | `effective_from` | timestamptz | 否 | simulation 区间起点；空 = point simulation |
   | `effective_until` | timestamptz | 否 | simulation 区间终点（不含）；空 = point simulation |

   行只存指针，不复制 payload：选中的 Canonical revision 所在快照由数据集的 manifest 绑定且不可变，
   `(canonical_table, revision_id)` 在该快照上即是内容本身，复制会把整个窗口的成交 / K 线再拷贝一份。
   一次构建的全部行共享同一个 `selection_id`；后续构建追加，不与先前批次混淆。该表即
   `research.dataset_manifests` 绑定的 `DatasetRef.table`（ADR-0023 §6）。

2. 分区：`identity(symbol) + day(event_time)`——与 `raw.binance_spot_agg_trades` / `canonical.trades` 一致
   （事件时间列名为 `event_time` 而非 `interval_start`，因为一行既可能来自 aggTrades 也可能来自 klines_1m，
   统一按选中 revision 的 `event_time` 落列，见 `infrastructure/dataset/builder.py::_time_column`）。选用
   symbol + day 而非 `selection_id`，是因为一次构建覆盖一个 symbol 全集在一个 UTC 窗口内的选择，读取方
   （复核、下一次增量构建）按 symbol 与时间窗过滤比按 `selection_id` 过滤更常见；这也与其余五张
   `identity(symbol) + day(...)` 表保持同一约定，复用同一条 ADR-0026 `pyiceberg-core` 写路径。

3. **单一事实来源**：`SELECTION_SCHEMA` 的字段定义（IDs、类型、doc、必填性）从 `infrastructure/dataset/
   selection.py` 移入 `infrastructure/catalog/phase1_tables.py`（新增 `DATASET_SELECTIONS`），因为依赖方向
   是 `dataset → catalog`（`selection.py` 已经从 `phase1_tables.py` 导入 `PHASE1_TABLE_PROPERTIES`），反向会
   成环。`selection.py` 之后从 `phase1_tables.py` 导入 `DATASET_SELECTIONS.schema` 作为 `SELECTION_SCHEMA`；
   `selection_table_definition()` 与 `DatasetBuilder` 的通用 `dataset_table` 参数保持不变——构造函数本身与
   任何 `research.*` 命名空间、匹配该 schema 的表仍然兼容，不因表名冻结而收窄。

4. 测试改用生产定义：`tests/infrastructure/dataset/dataset_support.py` 不再另建
   `research.f3test_selection` test-only 表，直接把 `DATASET_SELECTIONS`（随 `ensure_phase1_tables` 一起创建）
   传给 `DatasetBuilder(dataset_table=...)`；`tests/infrastructure/dataset/test_dataset*.py` 的表名断言随之
   指向生产表。`tests/infrastructure/catalog/` 的 golden / 分区 / row-builder 测试按 QG-1 的先例追加第 15 张
   （`test_earlier_goldens_are_unchanged_by_ds1`）。

## 备选方案

| 方案 | 内容 | 结论 |
|---|---|---|
| **A（采纳）** | 把 `selection.py` 的提议形状登记为生产表 `research.dataset_selections`，schema 单一来源移入 `phase1_tables.py` | 与 C3/D3B/E2/QG-1 的先例一致；测试与生产同一张表，不再维护两套 golden |
| B | 继续只用 test-only 表，Research Dataset 表登记推迟到有真实数据集消费者时 | 拒绝：`DatasetBuilder` 已实现且测试通过，F3 的读取方（复核、Phase 2 消费）没有生产表可指向；继续拖延与其余表的先例不一致 |
| C | 按 `data_type` 建多张表（如 `research.dataset_selections_agg_trades`） | 拒绝：一次构建本就可能横跨多个 `canonical_table`（未来若一次数据集覆盖多个 data_type），单表 + `canonical_table` 列更简单；且没有已批准的需求要求按 data_type 物理分表 |

## 后果

- 正面：F3 有登记的生产表可写；测试与生产同形状，不再维护重复 schema；分区与既有 `identity(symbol) +
  day(event_time)` 表族一致，复用同一条 PyIceberg 写路径与测试基础设施。
- 负面：多一张生产表；`selection.py` 的模块文档从"提议"改为"这是 `phase1_tables.py` 的镜像"，读者需要
  跳转一层才能看到真正的字段定义。
- 开放：这张表只承载指针，不是最终的物化数据（trade / bar 行本身仍留在 Canonical 表）；未来若研究消费者
  需要更宽的读取形状（例如把选中的 Canonical 行内联进这张表），属于新的 ADR，不在本次范围内。

## 合规检查

- [x] 不修改 Domain Contract（`ResearchDatasetManifest` / `DatasetRef` 字段与语义不变）
- [x] 不修改 Validation Constitution / Profile
- [x] 不删除历史（前 14 张表的数据与定义不变）
- [x] PostgreSQL 不存行情（新表在 Iceberg warehouse，PostgreSQL 只作 Iceberg catalog 元数据）

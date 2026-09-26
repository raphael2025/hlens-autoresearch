# ADR-0056: 物理 Event 表 `event.events`（Phase 3，只追加）

| 字段 | 值 |
|---|---|
| 状态 | **Accepted**（2026-09-26） |
| 日期 | 2026-09-26 |
| 决策者 | Claude Code（Opus），依 Raphael 2026-09-26 明确授权（'所有的决策都由你来决定，包括红线'）；采纳方案 A |
| 起草者 | Claude Code（Opus） |
| 相关 Phase | Phase 3 — Event & Interaction Engine（roadmap：输出 "Event 表"；Phase 4 输入 "Canonical、Event"） |
| 影响范围 | Data / Infrastructure：新增一张 additive Iceberg 表及其读写模块（`infrastructure/event/table_definition.py`、`infrastructure/event/iceberg.py`）；不改契约、不改 Phase 1 表、不改 `infrastructure/catalog/` |
| 是否破坏兼容 | 否：15 张 Phase 1 表的定义与哈希、`PHASE1_REGISTRY`、`core/`、`schemas/` 均不变 |
| 实施状态 | CODE_COMPLETE / DEBUG_PENDING（只在临时 SQLite catalog 上测试；未在真实 catalog 建表） |
| 前置 | [ADR-0036](0036-event-provider-contract.md)（EventProvider 契约与逻辑 Event 表）、[ADR-0023](0023-bitemporal-revision-data.md) §7（写入约束）、[ADR-0033](0033-research-dataset-selection-table.md)（additive 表先例）、[ADR-0031](0031-quality-evidence-gap-table.md)（additive 表先例） |

## 背景

roadmap Phase 3 的输出包括 "Event 表"，Phase 4 以 "Canonical、Event" 为输入。ADR-0036 交付了 `EventProvider` 契约、
事件执行器与**逻辑** Event 表：`infrastructure/event/table.py` 的 `event_table(result)` 把一次 `EventResult` 展平为
`EVENT_TABLE_COLUMNS` 行（`event_id`、`event`、`spec_hash`、`event_time`、`attributes_json`、`input_ids`、
`upstream_event_ids`、`provider`、`result_hash`），但文档字面写明 "registering a physical `event.*` Iceberg table
... is a separate, later decision"。目前能持久化事件运行的只有 `infrastructure/event/store.py` 的 `EventResultStore`：
一个运行一个规范 JSON 文件（`<root>/<result_hash>.json`），是**制品存储**而不是数据平面表——不能按时间窗跨运行查询，
不在 Iceberg catalog 里，没有 snapshot 可供 Phase 4 的实验复现元组绑定。

约束：

- 03-data.md §7.2：有界 `pyarrow.Table` microbatch + 稳定 batch id；commit 冲突按 batch id 幂等重试；每表单 writer；
  orphan 只由显式 maintenance 清理。
- `CommitRequest.row_count >= 1`（空 batch 不提交，ADR-0023 §7）。
- 适配器（`PyIcebergCatalogAdapter`）已提供：定义绑定校验、按 `batch_id` + 指纹的幂等重放（`ALREADY_COMMITTED` /
  `BatchConflict`）、乐观并发（`CommitConflict`）、按 snapshot 读取（`scan_columns(snapshot_id=...)`）。
- `EventResult` 的 `result_hash` 覆盖 `request_hash`、`provider`、`provider_hash`、`as_of` 与事件 id 序列；每个
  `Event.event_id` 覆盖事件全部内容；两者在构造时复核（不接受自报哈希）。
- `research/events/README.md` 已知缺口："一个请求对应一个标的；多标的事件表需要 subject 键"——契约里没有 subject
  字段，本 ADR 不引入（那是契约变化，H1）。

## 决策包（Decision packet）

- **问题**：是否新增一张只追加的 Iceberg 表 `event.events`，按下文的列、分区与写读语义持久化事件运行？
- **为何重要**：没有它，Phase 3 的 "Event 表" 只存在于内存 / 制品文件，Phase 4 无法用 catalog snapshot 绑定事件输入。
- **推荐**：方案 A（下文"裁决"）。
- **若不决定**（当时的默认）：保持现状——`event_table` 仅内存物化，`EventResultStore` 仅本地制品；不创建、不写任何 `event.*` 表。

## 裁决（方案 A）

### 1. 表与定义绑定

- 逻辑名 `event.events`（Iceberg namespace `event`；namespace 是存储命名，不改变契约的 `Zone` 枚举，同 03-data.md §7.1）。
- `definition_id` = 表名，`version = 1.0.0`，批次指纹规则 `hlens.pyarrow-batch-sha256@1.0.0`，非绑定表属性
  `write.parquet.compression-codec = zstd`（与 Phase 1 表相同），format version 2、commit retries 0 由适配器强制。
- 定义放在新模块 `infrastructure/event/table_definition.py`（`EVENT_EVENTS`、`PHASE3_TABLES` / `PHASE3_REGISTRY`、
  `ensure_event_tables`），只以只读方式导入 `infrastructure/catalog` 的既有辅助（`RegisteredTableDefinition`、
  `TableDefinitionRegistry`、`PYARROW_BATCH_FINGERPRINT`、`ensure_phase1_tables`），**不**修改该目录，也**不**追加到
  `PHASE1_TABLES`：Phase 1 的 15 张表、注册表与 golden 哈希逐字节不变。使用方显式组合注册表
  （`TableDefinitionRegistry(PHASE1_TABLES + PHASE3_TABLES)`）交给 `PyIcebergCatalogAdapter`；
  `open_postgres_catalog_adapter` 本就要求显式注册表，无需修改。

### 2. 列（字段 ID 显式写出，等于 Iceberg 建表时分配的 ID）

| ID | 列 | 类型 | 必填 | 含义 |
|---|---|---|---|---|
| 1 | `event_id` | string | 是 | `Event.event_id`（内容哈希） |
| 2 | `event` | string | 是 | 事件定义引用的规范串 `event:{name}@{semver}`（`str(Ref)`，`Ref.parse` 可逆） |
| 3 | `spec_hash` | string | 是 | `Event.spec_hash` |
| 4 | `event_time` | timestamptz | 是 | 可观测时间（UTC，微秒） |
| 5 | `attributes_json` | string | 是 | `attributes` 的契约规范 JSON（与 `event_table` 相同） |
| 6 | `input_ids` | list&lt;string&gt;（元素 ID 15，元素必填） | 是 | `Event.input_ids`（严格升序） |
| 7 | `upstream_event_ids` | list&lt;string&gt;（元素 ID 16，元素必填） | 是 | `Event.upstream_event_ids`（严格升序） |
| 8 | `provider` | string | 是 | `EventResult.provider`（plugin key） |
| 9 | `result_hash` | string | 是 | `EventResult.result_hash`：运行身份；同值的行 = 同一运行 |
| 10 | `event_index` | long | 是 | 该事件在运行规范顺序 `(event_time, event_id)` 中的 0 起序号 |
| 11 | `event_count` | long | 是 | 该运行的事件总数（读取时核对完整性） |
| 12 | `request_hash` | string | 是 | `EventResult.request_hash` |
| 13 | `provider_hash` | string | 是 | `EventResult.provider_hash` |
| 14 | `as_of` | timestamptz | 是 | `EventResult.as_of` |

- 1–9 与 `EVENT_TABLE_COLUMNS` 同名同序：投影这 9 列即得 `event_table(result)` 的行。
- 10–14 是**运行块**：使只读表内容就能重建并复核 `EventResult`（`result_hash` 需要 `request_hash` / `provider_hash`
  / `as_of` 与有序事件序列），不必依赖制品存储。
- **不设 `ingest_time` 列**：行必须是运行内容的纯函数，批次指纹才确定，幂等重放（同 `batch_id` + 同指纹）才成立；
  写入时间由 Iceberg snapshot 的 `committed_at` 与 snapshot summary 的批次元数据记录。事件的 PIT 时间就是
  `event_time`（ADR-0036：事件时间 = 可观测时间），不需要第二条时间轴。
- **不设 revision 块 / `supersedes`**：一个运行不可变；新请求、新 `as_of` 或新 Provider 版本 = 新 `result_hash` = 新的行。
  消费方按 `result_hash` 选择运行，不存在"按 `event_id` 取最新行"的语义。
- 不设 subject / symbol 列（契约无此字段；见背景）。

### 3. 分区：`month(event_time)`

事件是稀疏的：一次运行常覆盖数年，却只有几十到几千个事件。Phase 1 行情表用 `day(...)` 是因为每天百万级行；对事件用
`day(event_time)` 会让一次横跨三年的运行在一个 commit 里写出上千个单行小文件。`month(event_time)` 保留跨运行的
时间窗裁剪，把一次三年运行的文件数压到约 36。按运行读取依靠每个 commit 独立的数据文件及 Iceberg 列统计
（`result_hash` 的 min/max）裁剪。分区之后若需调整，走既有的 partition-spec 演进机制（新定义版本 +
`evolves_from`，03-data.md §7.1 最后一条），不是契约变化。

### 4. 只追加语义

- **一次运行 = 一个批次 = 一个 snapshot**：`batch_id = event.{result_hash}`（仿 `manifest.{content_hash}`）。
- **写入**（`EventTable.write(result)`）：
  1. 由 `result` 构造期望的行（`event_table(result)` + 运行块），按表的 Arrow schema 规范化；
  2. 在当前 head 上按 `result_hash` 读取已有行：已存在且逐行相等 → no-op（返回 replay，不产生 commit）；
     已存在但不同 → `EventTableConflict`，**绝不追加或覆盖**；
  3. 否则以当前 head 为 `expected_parent_snapshot_id` 提交；`CommitConflict`（别的批次先落地）→ 重读后有界重试；
     `BatchConflict`（同 `batch_id` 不同内容）→ `EventTableConflict`；
  4. 提交后在新 snapshot 上读回并复核（见下），不一致即 fail closed。
- **空运行**（0 个事件）不写入：`CommitRequest` 禁止空批次。写入方返回"空运行，未持久化"；整次运行仍可由
  `EventResultStore` 保存。因此仅凭表无法区分"空运行"与"从未写入"——这是已知、有意的限制（见备选方案 D）。
- 不提供删除 / 覆盖 / 更新 API；orphan 与小文件合并只由显式 maintenance 处理（ADR-0023 §7），不得删除任何行。
- 单 writer（03-data.md §7.2）；并发 writer 由适配器的乐观并发与幂等重放兜底。

### 5. 读取：snapshot 固定 + 复核

- `EventTable.read(result_hash, snapshot_id=None)`：未给 `snapshot_id` 时只解析一次当前 head 并固定在该 snapshot
  上读取，返回所用 snapshot id 与行；给定 `snapshot_id` 时按该 snapshot 读取（不存在 → `SnapshotNotFound`，
  不回退到 head）。在 snapshot S 之后写入的运行在 S 上不可见。
- 复核（任一失败 → `EventTableCorrupted`，从不修复）：所有行共享同一运行块；`event_index` 恰为 `0..n-1` 且
  `n == event_count`；由行重建 `EventResult`（`Ref.parse(event)`、`attributes_json` 经契约校验）——契约校验器复核
  每个 `event_id` 与 `result_hash`；再要求 `event_table(重建结果)` 与表中 1–9 列逐行相等。
- `EventTable.load(result_hash, snapshot_id=None)` 返回复核后的 `EventResult`；无该运行 → `None`。

### 6. 本 ADR **不**改变的内容

- `core/`（含 `core/contracts/event.py`、`core/contracts/catalog.py`）与 `schemas/`：无契约 / Schema 变化（H1）。
- 15 张 Phase 1 表、`PHASE1_TABLES` / `PHASE1_REGISTRY`、它们的 golden 哈希，以及 `definitions.py`、
  `fingerprint.py`、`iceberg_adapter.py`、`phase1_tables.py`、`create_phase1_tables.py`。
- PIT 规则（03-data.md §4）与事件执行器的可见性规则（ADR-0036：`available_time + observable_lag <= t`）。
- `event_table` 的逻辑列与 `EventResultStore` 的行为（两者保留；物理表与制品存储互补）。
- 不引入 PostgreSQL 中的行情数据（H8）、不连接真实 warehouse 作为本 ADR 的一部分；生产建表是批准后的单独运维步骤。

## 备选方案

| 方案 | 内容 | 结论 |
|---|---|---|
| **A（推荐）** | `event.events`：逻辑 9 列 + 运行块 5 列，`month(event_time)` 分区，一次运行一个批次，按 `result_hash` 幂等，snapshot 固定读取并重建复核 | 复用适配器既有的幂等 / 并发 / 时间旅行能力；表内容自足可复核；稀疏事件不产生小文件爆炸 |
| B | 同 A，但分区 `day(event_time)`（与 Phase 1 行情表一致） | 未选：事件稀疏，长运行在单个 commit 里写出大量单行文件；如需更细裁剪可日后演进 |
| C | 只存逻辑 9 列（不加运行块），重建 `EventResult` 依赖 `EventResultStore` | 未选：表单独无法复核 `result_hash`，两处存储必须同时在场才能验证 |
| D | 空运行写一行哨兵（事件列可空） | 未选：让 9 个本应必填的列变为可空，污染所有读者；空运行由制品存储保存即可 |
| E | 分区 `identity(result_hash)` 或不分区 | 未选：前者每运行一个分区目录、失去跨运行时间裁剪；后者在事件量增长后无裁剪（可作为数据量极小时的简化，但 A 的成本同样很低） |
| F | 追加到 `PHASE1_TABLES` 成为第 16 张表 | 未选：Event 是 Phase 3 产物；追加会改变 Phase 1 注册表与相关 golden 测试的期望集合 |
| G | 暂不建表，继续仅用 `EventResultStore` | 即"若不决定"的默认；Phase 4 无 catalog snapshot 可绑定事件输入 |

## 后果

- 正面：Phase 3 "Event 表" 有物理载体；Phase 4 可用 `event.events` 的 snapshot id 绑定事件输入（复现元组）；
  跨运行按时间窗查询成为可能；写入幂等、冲突 fail closed、读取可独立复核。
- 负面 / 代价：一个新 namespace 与新模块需要维护；每行冗余运行块（5 列，按运行常量，Parquet 字典编码后成本很小）；
  空运行不在表中。
- 需要迁移的内容：无（新表，无既有数据）。
- 对复现性的影响：旧实验不受影响；新实验若消费事件，应绑定 `event.events` 的 snapshot id 与 `result_hash`。
- 仍开放：多标的事件需要 subject 键（契约变化，另行 ADR）；生产环境何时建表（运维步骤，需 Raphael 授权）。

## 实施

1. `infrastructure/event/table_definition.py`（新）：`EVENT_EVENTS` 定义（字段 ID 显式，并像 Phase 1 的
   `_definition` 一样在导入时核对 ID 与分区 ID 等于 Iceberg 建表时分配值）、`PHASE3_TABLES`、`PHASE3_REGISTRY`；
   显式建表函数 `ensure_event_tables(adapter)` 委托参数化、幂等的 `ensure_phase1_tables(adapter, PHASE3_TABLES)`。
   不接入 `create_phase1_tables.py` 或任何 provisioning 脚本；生产 catalog 建表是单独的运维步骤。
2. `infrastructure/event/iceberg.py`（新）：`event_rows_batch(result) -> pa.Table`、`EventTable`（`write` / `read` / `load`），
   错误类型 `EventTableConflict` / `EventTableCorrupted`；只通过窄 Protocol
   `EventCatalog`（`load_table` / `commit_batch` / `scan_columns`）访问 catalog，不 import plugin。
3. 测试（只在 `tests/infrastructure/event/test_event_iceberg.py`，临时 SQLite `SqlCatalog` + `tmp_path` warehouse，
   与现有非 postgres catalog 测试同一模式；不触及真实 warehouse 或 PostgreSQL）：
   - 建表幂等（二次 ensure 不产生变化），分区 spec 恰为 `month(event_time)`，定义哈希稳定；
   - Phase 1 注册表不含本表、`PHASE1_TABLES` 仍为 15 张（其 golden 哈希由既有 catalog 测试守护）；
   - 写 → 读往返：1–9 列等于 `event_table(result)`，`EventTable.load` 重建的结果 `== result`；
   - 相同运行重写 = no-op（无新 snapshot）；
   - 冲突：同 `batch_id` 不同内容、以及同 `result_hash` 的篡改行，写入被拒且无新 snapshot；读取被篡改的运行
     → `EventTableCorrupted`（缺行、重复 `event_index`、`event_count` 不符、运行块不一致、`event_id` 不符）；
   - snapshot 固定：在写入运行 B 之前的 snapshot 上读不到 B，A 的读取结果与 head 上一致；未知 snapshot →
     `SnapshotNotFound`；
   - 空运行不产生 commit；
   - 特征事件（`Decimal` 属性）、状态事件（`str` 标签）与交互事件（`upstream_event_ids` 非空）的往返；
   - 乐观并发竞争失败（`CommitConflict`）后在新 head 上重试成功，两次运行都可读回；表不存在 → `TableNotFound`。
4. 文档：03-data.md 追加 §8 Phase 3 物理 Event 表（引用本 ADR，不改动 §7 Phase 1 冻结内容）；
   `infrastructure/event/table.py` / `store.py` / `__init__.py` 文档串与 `research/events/README.md` "已知缺口"
   随实现更新。

## 合规检查

- [x] 不破坏已冻结契约：不改 `core/`、`schemas/`、Phase 1 表定义与哈希
- [x] 不修改 Validation Constitution / Profile
- [x] Domain 层仍无具体技术依赖（Iceberg 只出现在 `infrastructure/`）
- [x] Research / Application Plane 边界不变
- [x] 不在 PostgreSQL 存储行情数据；不访问网络或真实 warehouse
- [x] 依 Raphael 2026-09-26 明确授权由 Claude Code 决定

## 参考

- `infrastructure/event/table.py`（逻辑 Event 表）、`infrastructure/event/store.py`（`EventResultStore`）
- `infrastructure/dataset/manifests.py`（按内容哈希幂等写入的先例）
- [03-data.md](../architecture/03-data.md) §7.1、§7.2；[roadmap](../research/roadmap.md) Phase 3 / Phase 4

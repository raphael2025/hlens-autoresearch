# ADR-0075: PyIceberg 固定快照的有界扫描路径

| 字段 | 值 |
|---|---|
| 状态 | Accepted（2026-09-28，Codex 依 Raphael 项目技术决策委托） |
| 日期 | 2026-09-28 |
| 决策者 | Codex（在 Raphael 明确委托范围内） |
| 起草者 | Codex，独立审查由 Codex 子代理与 Claude 并行完成 |
| 相关 Phase | Phase 1 — E1-CAP-1 |
| 影响范围 | Infrastructure / Data |
| 是否破坏兼容 | 否；保持 `CatalogAdapter` 和现有读取结果语义；不改 `core/` |

## 背景（Context）

当前 `PyIcebergCatalogAdapter.scan_column_batches` 调用 PyIceberg 0.12.0 高层 Arrow 扫描器。锁定版本的源码会把 manifest 列表、manifest entries、data scan tasks 和 positional-delete 状态累积到内存集合；Arrow 扫描路径还可能预读单 task 的完整批次列表。`RecordBatchReader` 只限制最终结果的呈现形式，不构成整条扫描链的内存上界。把 worker 数或 batch rows 调小也不能消除这些全量规划集合。

ADR-0021 规定 PyIceberg 是 Iceberg Catalog 与元数据提交的唯一权威路径，并允许其它技术用于只读计算；它没有要求读取必须通过 PyIceberg 高层 `DataScan`。ADR-0023 §7 禁止将不可重放的 `RecordBatchReader` 用于分区表 append，但不限制读取。Phase 1 的表采用 append-only revision；仓库没有 position-delete 或 equality-delete 写入路径。容量口径仍是完整工作集，32 MiB 门槛不变。

本 ADR 决定如何消除**扫描 planner / task / delete 集合**随文件数累积的 Python 持有量，不宣称解决 Iceberg 完整 table metadata 的 O(H)、archive parse、显式 ID 消费或其它 E1-CAP-1 项。

## 决策（Decision）

1. 保留 PyIceberg 作为 Catalog、表元数据和写入 / commit 的权威实现；不引入 DuckDB、Polars、DataFusion，不维护 PyIceberg fork，不改变 `core/contracts/catalog.py`。
2. `PyIcebergCatalogAdapter.scan_column_batches` 在 infrastructure adapter 内使用固定 `snapshot_id` 的流式读取实现，不调用会完整物化所有 manifest / entry / task 的高层规划路径。`max_int64` 必须复用该扫描路径，不得另走 PyIceberg 高层 planner。实现必须逐个读取 manifest-list 项与 manifest entry，并逐个读取 data file；不能在内存中保留随全表文件数增长的 Python list、set、task 队列或批次队列。锁定 PyIceberg 0.12.0 的 `AvroFile` 会先读取一个 Avro 文件的全部 bytes，因此本条限制逐条目 / Python 集合累积，不构成 manifest 文件字节数的绝对上界；该工作集须在 E1 容量阶段测量。
3. 当前 Phase 1 注册表将 Iceberg format version 固定为 2。扫描必须验证目标 table 确为 V2；未知版本（包括 V3）在产出任何 row batch 前拒绝。V3 deletion vector 尚未支持，未来支持 V3 必须另起 ADR。
4. 必须保留 Iceberg snapshot 固定、schema field-ID 投影、partition projection、metrics / row filter、分区值补列、timestamp 转换和锁定 PyIceberg Arrow reader 支持的文件格式解码语义。0.12.0 Arrow reader 支持 Parquet / ORC；Avro data file 在上游同样不支持，本 adapter 在产出 batch 前 fail closed。实现应复用锁定版本中可复核的 Iceberg schema / manifest / Arrow 转换原语；不得用文件路径排序或到达顺序替代 Iceberg snapshot / sequence 语义。
5. 新路径在发出任何 row batch 前先完整检查该 snapshot 的所有 manifest（不受用户 row filter 影响），并检查 live manifest entries。只要发现 delete manifest、`POSITION_DELETES`、`EQUALITY_DELETES` 或未知内容类型，即 fail closed；不得忽略 delete file 或返回部分结果。当前仓库没有生成 delete files 的写路径。未来要支持 delete files，必须另起 ADR，为逐 data-file 删除位置查找提供可 spill 的有界实现并保持 delete sequence / partition 语义。
6. 同一时刻只处理一个 manifest / data file，Arrow reader 使用显式有限 row batch、关闭策略、无 pre-buffered 全文件读取；提前停止、读取异常或取消都必须关闭 reader / FileIO resource。`max_int64` 通过 `scan_column_batches` 逐批归约，只跨批保留当前最大值与本批值。不得回退到全表 `scan_columns` 或 PyIceberg 高层全量规划路径。
7. 对 PyIceberg 私有或半公开读取符号的依赖必须集中在单一 infrastructure helper，精确锁定 `uv.lock` 版本。PyIceberg 版本变化时须先审查该 helper 的 manifest、schema、filter、delete 与 resource 语义，不可静默升级。
8. 此路径只缩减扫描计划与并发结果的工作集，不代表 E1-CAP-1 通过。PyIceberg `TableMetadata.snapshots`、完整 manifest-list / manifest bytes、单次 Arrow batch / Parquet row group / ORC stripe、D1 archive parser、显式 ID stream、调用方集合与临时目录仍须单独对账、处理或测量；完整工作集仍按 32 MiB 门槛验收。有限 row batch 不等于任意 row group / stripe 的绝对 byte 上界。

## Amendment 1（2026-09-28）

`max_int64` 纳入本 ADR 的受控调用面：它通过 `scan_column_batches` 使用相同的固定快照预检 / 流式读取，只保留逐批最大值。`PinnedCatalogView` 对未绑定表的批次读取直接返回空迭代器（仍验证表存在），不扫描未绑定的当前 head。公开接口和结果语义不变；该修订不改变 32 MiB 门槛，也不宣称整个 Iceberg metadata、单个 manifest 或 Arrow row group / ORC stripe 有硬字节上界。

## 备选方案（Alternatives）

| 方案 | 优点 | 缺点 | 为何未选 |
|---|---|---|---|
| A（本 ADR）在 adapter 内实现流式 pinned-snapshot scan | 保留 PyIceberg Catalog/写入路径和冻结契约；控制 manifest/task 生命周期；避免新引擎 | 需维护对锁定 PyIceberg 读取原语的适配；过滤与 schema 行为必须仔细复核 | 选定；与当前技术边界和 E1 只读需求匹配 |
| 维护 PyIceberg fork 并修改高层 planner / ArrowScan | 可直接修复上游全量规划与 delete 处理 | 长期跟进上游、fork 打包与供应链成本高；仍不解决 metadata / API O(H)/O(N) | 目前没有必要替换 Catalog/写路径；adapter 范围足以先解决扫描子问题 |
| 换 DuckDB / Polars / 新 scan engine | 可能有成熟的批次执行器 | 当前没有已批准的替代引擎；Iceberg snapshot、delete、schema evolution 的等价行为需要重新证明 | 引入新数据读取技术与兼容面，成本和正确性风险更高 |
| 只调 worker / Arrow batch 参数 | 改动最少，可降低部分峰值 | 全量 manifests/tasks/delete state 仍存在，没有全路径硬上界 | 不满足扫描规划增长目标；不能作为容量通过证据 |

## 后果（Consequences）

- 正面：扫描规划不再依赖 PyIceberg 高层路径的全量 manifest/task/delete 集合；Public `CatalogAdapter` 与已有 DTO 保持不变。
- 限制：Avro manifest-list 与 manifest 单文件 bytes、以及 Arrow 格式的 row group / stripe 仍可能随单个文件大小增长；需要在 E1-CAP-1 计量中纳入。
- 负面 / 代价：只读 adapter 需跟踪锁定 PyIceberg 版本的底层格式；含 delete files 的 snapshot 将明确拒绝；逐文件串行读取可能降低吞吐。
- 需要迁移的内容：仅 `PyIcebergCatalogAdapter` 的 `scan_column_batches` 实现与其 infrastructure helper；不迁移 Catalog / Iceberg metadata / warehouse。
- 对复现性的影响：输入 snapshot、schema、filter 与 table metadata identity 不变；必须对相同 snapshot 的行内容与拒绝边界保持确定性。失败的 delete snapshot 不得产生部分结果。

## 合规检查

- [x] 不破坏已冻结契约；不修改 `core/` 或 public `CatalogAdapter`
- [x] 不修改 Validation Constitution 或 Profile
- [x] Domain 层仍无具体技术依赖
- [x] Research / Application Plane 边界不变

## 参考

- [ADR-0021](0021-phase1-local-data-infrastructure.md) §D-01 / §D-02
- [ADR-0023](0023-bitemporal-revision-data.md) §7
- [E1-CAP-1 对账](../reviews/2026-09-28-e1-cap1-design-reconciliation.md)
- [E1 有界历史调查](../reviews/e1-bounded-history-options.md) §1 / §6

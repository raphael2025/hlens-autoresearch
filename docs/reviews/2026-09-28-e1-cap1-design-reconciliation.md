# E1-CAP-1：阻断项与设计路径对账

| 字段 | 值 |
|---|---|
| 日期 | 2026-09-28 |
| 基线 | 当前本地 `main`，起始提交 `2fc89f1` |
| 类型 | 源码审计、设计路径与分步实现记录；未运行 probe / 测试 |
| 状态 | **BLOCKED：容量条件与当前全量返回 API / Iceberg 历史模型存在未解决冲突** |
| 权威验收口径 | `docs/reviews/2026-09-27-e1-review.md`：完整进程工作集、32 MiB 增量上限；阈值与严格证明语义均不改 |

## 结论

E1-CAP-1 仍阻断。当前主线没有可复用的 E1-CAP-1 RSS 结果；旧候选 `fix/e1-cap1@a75278e` 的 59.9 / 63.9 MiB 是该候选在特定合成工作负载下未通过的证据，不能作为当前主线或生产结论。

源码审计能确认多项随单元行数 / 批次数增大的本仓库持有量，但不能确认它们各自的 RSS 占比。PyIceberg snapshot metadata、manifest 和扫描规划可能增加工作集；现有证据没有测得其占比，也没有证明它们是主要根因。不能把任何一个增长来源提前写成容量根因。

另有一个接口级矛盾：当前 `CanonicalUnitNormalized.revision_ids` 返回完整、按 Raw 位置排序的 ID tuple；既有门槛又把返回对象计入完整进程工作集，并要求增长不随 N / batch 数扩大。仅消除内部副本仍留下 O(N) 的公开结果本身。若实测完整 ID 输出超过门槛，不能通过只在 probe 中不持有结果或把 API 结果排除来关闭 E1；必须改为有界结果表示，并保留可显式流式读取完整 ID 的能力，或以其他实现证明全量 tuple 确实落在预算内。

## 审计证据分级

| 项 | 已由仓库代码证明 | 尚未证明 / 需要测量 |
|---|---|---|
| Raw positions | `normalizer.py` 全单元 `scan_columns(...).to_pylist()`、排序后进入 survey/unit facts；缓存条目数固定不代表单项基数有界 | RSS 峰值与可复用固定窗口 / 外存排序的实际效果 |
| Canonical committed columns | `arrival_seq`、`knowledge_time`、schema version 存在全单元物化；recover / close 还创建全量比较状态 | 分块严格核验是否覆盖所有重复、缺号、空值、版本和 block 异常；实际 RSS |
| D1 archive verification | `ParsedArchive.rows` 是整表；normalizer verifier 缓存已解析 archive；关闭缓存只会重解析，不会消除单次整表 parse 峰值 | spool / 有界解析方案能否精确保留整文件拒绝、逐行哈希和跨行语义；RSS |
| Batch history index | verifier 为 Raw batch 保留 SnapshotInfo 索引并在调用时复制；列表会随历史 batch 数增长 | 在精确保留 batch ID / 顺序 / 重复验证的前提下改为计数或窄窗口后，内存与时间收益 |
| Result IDs | result API 返回完整 ID tuple；survey / write 路径还有 ID 中间副本 | 各副本与最终公开 tuple 的实际 RSS；低于阈值的可能性 |
| PyIceberg / Iceberg history | adapter history 遍历保持 metadata snapshot 列表可达；已锁版本源码检查支持其为列表 | snapshot metadata、manifest、planner 临时对象在当前 main 探针中的实际 RSS；不能归因自候选分支的总增长值 |

详细函数位置、原语义与 E1 关闭条件仍以 [E1 复核](2026-09-27-e1-review.md)、[有界历史调查](e1-bounded-history-options.md) 为准。

## 处理决定

1. 保持 32 MiB 完整工作集门槛；不排除 PyIceberg metadata 或结果对象，不降低完整性证明要求。
2. 不把失败候选整支移入主线。其 spool、窗口证明与固定计数实现仅供逐项参考；任何复用都必须先对照当前 main 的代码，并重新满足关闭标准。
3. E1 修复按三条独立工作流设计，避免把一个猜测包装成根因：
   - **E1-R：仓库内存状态**——positions、committed 列、完整性比较、batch indexes 与临时 ID 副本改为固定窗口 / 有界 spool / 可重复有界扫描；每个缺口列出保持的拒绝语义。
   - **E1-API：返回接口**——先确认完整 `revision_ids` tuple 的实际消费者和预算。如果它不能在当前门槛内保留，另写 Proposed ADR，改为有界统计结果 + 显式 ID 流式读取接口；不做隐式惰性加载或无生命周期管理的临时文件句柄。
   - **E1-H：Iceberg 历史工作集**——在 E1-R 后以隔离子进程设计 L / H 分离矩阵。若完整工作集仍随 H 超过门槛，再单独比较 PyIceberg 复用、Iceberg snapshot 保留语义和核心读路径替换；任何 retention / catalog / read-write 技术变化必须先走 ADR，并说明旧 pinned dataset 的可复现性。
4. 代码任务按 parser、row-integrity、catalog/normalizer 等单模块顺序拆分；涉及 `core/` 的任务不并行。跨模块协调由 Codex 负责。
5. 在用户要求的统一验收窗口前不运行测试、probe、build、lint、typecheck 或数据生成；代码可先按设计落地，但状态一律写作未验证。容量修复不能在静态复核后声称通过。

## 2026-09-28 实施进度：批次历史与流式读取

在现有 E1-R 路径内先落地一个基础切片，尚未提交或验收：

- `PyIcebergCatalogAdapter.scan_column_batches` 使用 PyIceberg 的 Arrow batch reader，并在迭代耗尽、读取异常或显式 `close()` 时关闭 reader；`PinnedCatalogView` 将请求固定到显式或绑定 snapshot，底层不支持时拒绝，不退回整表物化。
- `PersistedRowVerifier` 的 batch-history 缓存改为每个 lineage 的固定摘要；验证时第二遍从缓存的同一 history head 流式读取快照。D2 继续核连续前缀、批大小计划和 touched batch 的 fingerprint；D3E 继续核允许缺口的 index、分批行数和 fingerprint。
- 为常数空间检测重复 index，这一切片把“同一 lineage 的历史 index 按快照时间严格递减”作为完整性约束。D2 与 REST writer 当前均按 index 递增顺序提交；旧 verifier 没有拒绝所有乱序但 index 唯一的历史。该切片将拒绝此类外部导入或手工构造的乱序历史，属于有意收窄，需在代码说明中保留此约束。

本次独立源码审阅未发现其他明确的 D2 / D3E 切片或 offset 回归。尚未运行测试、probe、build、lint、typecheck；代码未验证。E1-CAP-1 仍阻断：位置、已提交列、archive 解析、结果 ID 与 PyIceberg / Iceberg metadata 的完整工作集仍未有界或测量，32 MiB 门槛没有通过证据。

## E1-R 实施和后续验收边界

仓库内可直接研究的实现边界：

- 固定范围 scan 必须以位置范围而不是仅减少列宽；窗口间仍需验证 `archive_line_number = 1..N`、REST index 连续性、重复 position 和单位排名切片。
- Canonical 严格核验按稳定固定 snapshot 分块，跨块累计只保留固定计数 / 摘要；重复序号、缺失序号、knowledge time 合法性、schema version 唯一性、batch prefix / fingerprint 与崩溃恢复必须 fail closed。
- archive spool 不能把被拒绝文件变成部分成功；须保留完整 ZIP / CSV 格式与 checksum 验证、重复记录、整文件结束状态、逐行规范化哈希和关闭后清理。
- batch 索引只有在不丢失 duplicate batch、连续 prefix、批次身份和来源绑定拒绝能力时，才可改为计数摘要。
- `CanonicalUnitNormalized` 的 API 输出不能从容量统计中静默剔除。若改接口，应同时列出仓库内调用点、兼容期与显式 ID 消费方式，并形成 ADR 再实现。

正式容量验收继续遵循 E1 review：当前代码线、M=256、N=10k/100k/500k、隔离子进程和重复运行；每个阶段增长不超过 32 MiB；记录 baseline、采样间隔、L/H、Catalog 调用与返回对象；同时用递归结构测试证明长期容器基数边界。该设计文档本身不是实现、测量或验收记录。

# E1-CAP-1 `resume` / `replay` RSS 根因调查

| 字段 | 值 |
|---|---|
| 日期 | 2026-09-27 |
| 范围 | 500k 行 `resume` / `replay` 测量所剩的 RSS 增长；只读代码检查与实现可行性评估 |
| 代码基线 | `66fb6d1348f3245c5d9ebd9e20bdd050fbae2493` |
| 探针 | 未运行；本文不替代或修改 `docs/reviews/2026-09-27-e1-review.md` 中的 E1-CAP-1 失败证据 |

## 发现

normalizer 的批计划在当前实现中已经压缩为固定计数：`_CommittedPlan` 保存 unit 行数、microbatch 大小和已提交批数；`_plan_snapshots()` 流式遍历批次；resume 从第一个未提交批次继续。因此本次代码审查没有发现重新累积全量 batch ID、`done` 映射或 commit 列表的路径。

剩余 `resume` / `replay` 路径仍需检查每个已提交 Canonical batch 的 ID、fingerprint、row count、快照归属和祖先关系。该证明经过 `CanonicalNormalizer._committed_plan()` / `_plan_snapshots()`，最终调用 `PyIcebergCatalogAdapter.history()`。`history()` 调用 `_require()` 加载整张 Iceberg table metadata，再持有 `iceberg.metadata.snapshots` 完整列表并沿父链查找。快照条目数随 Canonical microbatch 提交次数增长；500k 行、固定 M=256 时，提交快照数量约为 10k 行规模的 50 倍。

所以当前候选根因是 **PyIceberg 当前 table metadata 中的全量 snapshot 列表及其摘要对象**，而不是 normalizer 的业务行缓存。协调者提供的容量结果为 `resume` 增长 59.9 MiB、`replay` 增长 63.9 MiB，均高于 32 MiB 阈值；本调查没有重新测量，因而不声称已独立复现这些数值。

这也解释了为什么窄列 Arrow 扫描、磁盘 spool、常数大小的 normalizer plan 都不能消除此项：它们减少了行数据工作集，但没有改变 Catalog 为验证精确提交历史而加载的 snapshot metadata。

## 为什么本批次不提交猜测性修复

- 删掉或截短 `metadata.snapshots`、只信任当前 snapshot、或跳过历史遍历，会漏掉缺失/重复/越序批次和错误 fingerprint，改变精确 readback、崩溃恢复或 fail-closed 语义。
- 在进程缓存历史或已验证结果，只把分配移到更早的阶段并增加长期驻留内存；若缓存跨调用或跨快照复用，还会破坏固定 snapshot 的证明边界。
- Iceberg snapshot expiration / compaction 会改变恢复所依赖的历史；它不能作为 E1 normalizer 的局部容量修复。
- 简单逐父节点重新 `load_table` 仍会反复解析同一份全量 metadata，时间与分配次数会变成 O(H²)，并不提供有界 RSS 保证。

## 保守建议

保持 E1-CAP-1 为阻断，不降低 32 MiB 阈值，也不宣称容量验收通过。要关闭 Catalog metadata 这项增长，需要单独设计和实现 **对同一个已加载 metadata 版本的磁盘背书历史索引**，或采用有界解析器流式读取 Iceberg metadata 并把必要的 snapshot identity / parent / commit summary 写到磁盘索引。索引必须精确保留 `SnapshotInfo` 所需字段及父链、重复 ID、悬空 parent、cycle、batch summary 的现有拒绝行为；索引构建和查找都不得把 O(H) Python 对象留在内存。

此类实现横跨 Iceberg metadata 读取格式、PyIceberg IO、Catalog adapter 和崩溃边界，不能仅凭本次静态审查安全完成。实现之后仍需按既有 E1-CAP-1 验收标准跑隔离子进程的容量探针和测试；本任务明确未执行这些验证。

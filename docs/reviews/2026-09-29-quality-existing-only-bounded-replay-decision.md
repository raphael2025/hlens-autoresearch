# Quality `existing_only` 有界重放决策包

**状态：已由 ADR-0093（Accepted，2026-09-29）解决；本文作为问题背景保留，不再是待决事项。**
**Phase：1 — Market Representation / E1-CAP-1**
**范围：** `infrastructure/quality/` 的报告事件生成、持久化与重放；不改 `core/` 冻结契约，不改现有表定义。

## 问题

ADR-0077 §6.1.5 要求 `QualityReporter.report(existing_only=True)` 按固定工作集重导出并比较，或证明单报告输出有契约级固定上界。当前实现两者都不满足：

- Reporter 会按输入规模累积 bar / trade 状态、竞争 revision heads 与事件；生成和比较完整 `events`。
- `quality.data_quality_reports.events` 是单行内的 Arrow `List<Struct>`，没有 ordinal 或 chunk 边界。扫描接口能按表行分批，不能流式读取该 List 中的元素；现有重放路径会物化整行。
- 竞争 heads、trade discontinuity 事件数均没有固定上限。直接加 cap 或丢弃事件会改变完整性语义。
- ADR-0031 已将 evidence gaps 移至独立表，但没有解决 `events` 列的有界重放。
- `StorageAdapter` 没有删除接口。以其发布内容寻址排序对象，会留下不可变 orphan；现有 `existing_only` 的“不写”语义没有说明是否允许此类临时验证对象。

因此，仅把 `evidence_gaps_of` 改为 iterator、降低 Arrow batch 大小或将部分 dict 改为 SQLite，不能满足 ADR-0077 §6.1.5。

## 需要决定

1. 是否新增版本化 Quality event-stream 表 / 协议：按 ordinal 追加固定大小事件块，报告行仅存固定大小的根、计数与版本引用；`existing_only` 逐块重导出、验证并比较。旧 2.0.0 inline 报告保留原样只读重放，新规则版本使用新协议。
2. 如果采用事件流，验证期内容寻址排序对象在失败时只能成为 orphan。是否接受这一点作为 `existing_only` 的存储语义；若不接受，需要先定义不发布对象的 bounded sort / scratch 协议。
3. 明确报告 ID、rule/version hash、事件顺序和跨块完整性校验如何区分旧 inline 格式与新流格式。现有 report table schema 和历史哈希保持不变。

## 选项

| 选项 | 处理 | 结果 |
|---|---|---|
| **A（推荐）** | 新增内部事件流持久化 / 迭代协议和新规则版本；现有表 schema 与 v2 历史行不变；接受失败后可能留下无引用的内容寻址 orphan | 可保留全部事件，并为新版本建立有界重放路径；需 ADR 明确兼容、身份与 orphan 语义 |
| B | 给单份报告事件数 / 大小增加硬上限 | 改变现有完整报告语义；超限报告拒绝，且该上限不在现有契约中 |
| C | 保留 inline 格式，仅优化 gap 与部分生成容器 | 不能证明完整 `existing_only` 重放有界，E1-CAP-1 仍不通过 |

## 推荐与边界

推荐 **A**，另立 ADR 修订 ADR-0031 / ADR-0077 的基础设施协议。v2 报告继续只读重放，不能将 v2 测量结论外推为新格式容量证明。事件内容、排序和 fail-closed 校验必须完整保留；不得通过丢弃 competing-head / discontinuity 事件、任意截断或放宽校验来降低内存。

本决策未落定前，Quality `existing_only` 不得计入 E1-CAP-1 的有界完成项。该项单独保持 architecture decision required，不阻塞 Canonical、PIT、Universe、Dataset 等独立 Phase 1 工作。

## 审计来源

- ADR-0077 §6.1.5、§6.2.5 与资源边界章节。
- ADR-0031 的 evidence-gap 分表及逐批核对要求。
- 2026-09-29 对 `infrastructure/quality/reporter.py`、`infrastructure/dataset/builder.py`、Phase 1 表 schema 和 report replay 路径的静态审计。审计未修改代码、未运行测试；全仓测试批次另行记录。

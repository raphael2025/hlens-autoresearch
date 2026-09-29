# PIT bounded conflict output decision

**状态：ARCHITECTURE_DECISION_REQUIRED**
**Phase：1 — Market Representation / ADR-0077 §6.1.2–6.1.4**
**范围：** PIT v3 bounded iterator 的 conflict diagnostics。Legacy v2 selection 保持逐位兼容。

## 问题

隔离 PIT 分支已把全局 run refs 压缩为 root，并逐 key 产生 evaluation；但单 key 仍物化历史 rows、revision records、edges、availability 和 evaluation instants。该部分按 ADR-0077 已接受的有界实现范围继续修复。

审计还发现 `PointInTimeSelection.maximal_heads` 是完整 tuple。单次 evaluation 的 competing heads 数没有固定上限，因此即便计算改成流式，返回对象自身仍可能 O(N)。不能静默截断 tuple，也不能把测试数据规模当成契约上限。

## 需要决定

PIT v3 遇到多个 maximal heads 时，如何保留 fail-closed 结果和必要的审计证据，同时不要求调用方持有完整 tuple？

| 选项 | 处理 | 结果 |
|---|---|---|
| **A（推荐）** | v3 把完整有序 heads 写入 bounded evidence stream，选择结果保留固定大小的 stream root / count；Dataset v3 重放该 stream。无冲突时照常 yield 唯一 head。v2 `maximal_heads` tuple 路径不变 | 保留全部冲突事实；需明确 v3 result shape、stream identity 和报告绑定规则 |
| B | v3 发现第二个 head 即 fail closed，仅报告 key 与 conflict 类别，不产完整 head 清单 | 工作集最小，但损失现有竞争 head 诊断内容，须确认可接受 |
| C | 保留完整 tuple | 语义简单，但单次 evaluation 仍无界，不满足 ADR-0077 |

## 建议

优先评估 A。现有契约没有要求 Dataset v3 内联完整 heads tuple；ADR-0077 已使用内容寻址 evidence stream 表达无界结果。实现前须确定 PIT conflict stream 是否直接复用 Dataset v3 evidence trees，或使用 PIT 专属私有流。不得改动 v2 已持久化选择或把 heads 限制为固定数量。

其余 PIT 内部有界处理（外部排序 / 归并、有界 fan-out、逐 evaluation 消费、reader 生命周期）属于 ADR-0077 已接受的实现要求，可与该决策并行设计，但完成前不能声明 PIT E1 bounded。端到端 32 MiB 测量仍是单独必需门槛。

## 审计来源

- ADR-0077 §6.1.2–6.1.4、§6.2.5。
- 2026-09-29 对 `codex/pit-edge-validation@9d27767` 的只读代码审计；未改代码、未运行测试。

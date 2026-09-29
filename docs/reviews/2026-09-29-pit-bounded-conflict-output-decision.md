# PIT bounded conflict output decision

**状态：DECIDED — ADR-0094 Accepted（2026-09-29）**
**Phase：1 — Market Representation / ADR-0077 §6.1.2–6.1.4**
**范围：** PIT v3 bounded iterator 的 conflict diagnostics。Legacy v2 selection 保持逐位兼容。

## 问题

隔离 PIT 分支已把全局 run refs 压缩为 root，并逐 key 产生 evaluation；但单 key 仍物化历史 rows、revision records、edges、availability 和 evaluation instants。该部分按 ADR-0077 已接受的有界实现范围继续修复。

审计还发现 `PointInTimeSelection.maximal_heads` 是完整 tuple。单次 evaluation 的 competing heads 数没有固定上限，因此即便计算改成流式，返回对象自身仍可能 O(N)。不能静默截断 tuple，也不能把测试数据规模当成契约上限。

## 决策记录

PIT v3 遇到多个 maximal heads 时，如何保留 fail-closed 结果和必要的审计证据，同时不要求调用方持有完整 tuple？

| 选项 | 处理 | 结果 |
|---|---|---|
| **A（推荐）** | v3 把完整有序 heads 写入 bounded evidence stream，选择结果保留固定大小的 stream root / count；Dataset v3 重放该 stream。无冲突时照常 yield 唯一 head。v2 `maximal_heads` tuple 路径不变 | 保留全部冲突事实；需明确 v3 result shape、stream identity 和报告绑定规则 |
| B | v3 发现第二个 head 即 fail closed，仅报告 key 与 conflict 类别，不产完整 head 清单 | 工作集最小，但损失现有竞争 head 诊断内容，须确认可接受 |
| C | 保留完整 tuple | 语义简单，但单次 evaluation 仍无界，不满足 ADR-0077 |

已选择 A，细节见 [ADR-0094](../adr/0094-pit-bounded-conflict-head-stream.md)：v2 selection 不变；v3
bounded 输出以 `RunRef + count` 表示完整有序 conflict heads，并由显式 reader 读取。不得截断或省略 head。
Dataset builder 根据 count fail closed。

## 未关闭的实现与容量边界

此决策只关闭结果形状，不等于实现或验收。单 key 图校验 / reachability 仍保留 O(N) Python 状态；bounded stream reader、failure cleanup、调用方迁移和容量测量均待完成。不得将局部切片或单测通过描述为 E1-CAP-1 通过。

其余 PIT 内部有界处理（外部排序 / 归并、有界 fan-out、逐 evaluation 消费、reader 生命周期）属于 ADR-0077 已接受的实现要求，可与该决策并行设计，但完成前不能声明 PIT E1 bounded。端到端 32 MiB 测量仍是单独必需门槛。

## 审计来源

- ADR-0077 §6.1.2–6.1.4、§6.2.5。
- 2026-09-29 对 `codex/pit-edge-validation@9d27767` 的只读代码审计；未改代码、未运行测试。

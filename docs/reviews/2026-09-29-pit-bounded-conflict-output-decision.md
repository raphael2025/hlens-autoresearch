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

## DECISION PACKET — 单 key 图的外存索引

**状态：ARCHITECTURE_DECISION_REQUIRED**

**ID：D-E1-PIT-GRAPH-INDEX**

**Phase：1 — Market Representation / E1-CAP-1**

**QUESTION:** `iter_bounded()` 应使用什么受控的外存可变索引，才能删除单 key 图校验与 reachability 的 O(N) Python 状态，并保持可接受的长链 I/O？

**WHY_IT_MATTERS:** ADR-0077 已接受的 content-addressed sorted runs 是不可变对象，适合顺序扫描、排序与归并；但 Kahn cycle check 需要逐边递减 indegree，PIT heads 需要跨中间不可用节点判断候选后代。只用不可变 run 反复重算在长链上会产生 O(V²) 外存 I/O；SQLite 或其它可变索引尚未获准成为该路径的临时存储原语。保留当前 `RevisionGraph` 则继续持有记录、边、集合和 head tuple，无法满足有界目标。

**OPTIONS:**

A. 继续只用 immutable sorted runs。内存边界清晰，复用 ADR-0077 对象格式；长链 Kahn peel / reachability 最坏 O(V²) I/O，不适合大 key，不能据此通过完整验收。

B. 在调用方显式提供的 Canonical scratch 目录建立 invocation-scoped mutable spill index（例如 SQLite），关闭 mmap、限制 page cache、将排序临时数据放盘。索引表用唯一键校验 revision / arrival / payload / ownership，匹配声明边与及时证据，并以索引边表和 Kahn 队列生成拓扑序；每个 cutoff 在该序上单次传播 candidate reachability，再将有序 heads 流写入 ADR-0077 sorted run。预计图构建 O((V+E) log V)，每个 evaluation O(V+E) 加 head 输出；磁盘状态 O(V+E)，内存边界须由显式 cache 与 run 参数共同证明。进程异常退出会留下 scratch orphan；不能默默清理，需明确定义登记、恢复与维护策略。

C. 为 infrastructure 增加正式 spill-index / mutable scratch provider 接口及实现，不把 SQLite 文件布局固定在 selector 内。边界最清晰、可替换性好；需新增 provider 生命周期、限额、崩溃恢复及测试契约，改动面高于 B。

**RECOMMENDATION:** 选择 C 的接口边界，以 B 作为首个本地实现；在实现前明确 scratch 字节预算、SQLite cache 上限、文件所有权标记、异常关闭和进程中断后 orphan 的保留 / 显式清理规则。不得使用未指定的系统临时目录；不得把 O(V²) 的 run-only 原型当作 500k 容量方案。

**IMPACT:** 只影响 `infrastructure/pit/` 与 caller-provided scratch 生命周期，不改变核心契约、v2 replay、ADR-0094 冲突 heads 格式或 DQ-9 数值。外存字节与 complete-process 工作集仍需单独测量；任何局部实现均不等于 E1-CAP-1 通过。

**BLOCKS:** 单 key graph / reachability bounded implementation and its long-chain acceptance evidence. Does not block independent Phase 1 modules.

**DEFAULT_IF_UNDECIDED:** 保持当前 fail-closed `RevisionGraph` 路径及其 O(N) 分配；不使用临时 SQLite、run-only quadratic prototype 或未经批准的 scratch 清理策略，不宣称 PIT bounded graph 完成。

### 本次独立复用验证

- 命令：`systemd-run --user --scope --quiet -p MemoryMax=6G -p MemorySwapMax=0 uv run pytest tests/infrastructure/pit/test_bounded_runs.py`
- 结果：`22 passed in 0.79s`。这只验证 ADR-0077 sorted-run primitives；不验证单 key graph、ADR-0094 冲突流接线或 E1-CAP-1。

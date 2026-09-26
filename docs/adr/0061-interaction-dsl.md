# ADR-0061：事件交互 DSL（Phase 3）

| 字段 | 值 |
|---|---|
| 状态 | **Accepted**（2026-09-26） |
| 日期 | 2026-09-26 |
| 决策者 | Claude Code（Opus），依 Raphael 2026-09-26 授权（通过 ADR 自主决定技术方案） |
| 相关 Phase | Phase 3 Event & Interaction Engine（roadmap："交互 DSL / 上游追溯"） |
| 影响范围 | `plugins/events/`（新增 DSL 解析与编译）、`infrastructure/event/upstream.py` 的使用方式不变；**无契约 / Schema 变化** |
| 是否破坏兼容 | 否：现有两个交互算子（A 后 B、共现）及其 spec hash 不变 |

## 背景

交互目前只有两个固定算子（`plugins/events/interactions.py`，参数写在 trigger JSON 中），没有可组合的交互语法；多跳上游链只在各自的运行里逐跳核对。
CLAUDE.md §5 要求新插件能力先有 ADR。

## 裁决

1. DSL 是**数据**（JSON 表达式树），不是代码：节点只有 `ref`（一个已登记事件规格的引用）与四个已审阅算子 `seq(a, b, within)`、`and(a, b, within)`、`not(a, b, within)`
   （A 发生且 `within` 内无 B）、`count(a, at_least, within)`；参数全部显式，无默认；任何未知节点 / 算子 / 字段即拒绝（与 ADR-0040 的"只执行已审查的注册算子"一致）。
2. 编译：表达式树按后序编译成一串**普通的交互 `EventSpec`**（每个内部节点一个规格，`lineage` 恰为其子节点的引用，trigger 为规范 JSON 并逐字绑定子节点 spec hash），
   由已登记的交互 Provider 执行；因此每一跳仍经 `infrastructure/event/upstream.py` 的核对（上游规格、spec hash 精确绑定、Feature / State 并集），不引入新的执行路径。
3. 同一表达式永远编译成同一组规格与哈希（规范化：`and` 的操作数按规格哈希排序；`seq` / `not` 保序）；编译结果与表达式哈希一起记录，可重算核对。
4. 可见性：交互事件的事件时间 = 其最后一个必需输入的可观测时间（与现有算子一致）；`not` 的事件时间 = 窗口结束时刻（在此之前无法知道"B 没有发生"），因此不产生未来函数。

## 后果

- 正面：交互可组合、可审计，完全复用现有上游核对。
- 负面：深层表达式会产生多个中间规格与多次运行；嵌套深度与节点数上限作为编译参数显式给出（无默认）。

## 合规检查

- [x] 无契约 / Schema 变化；LLM 不执行代码（DSL 是数据，只编译到已审阅算子）
- [x] 事件时间不早于可观测时间（`not` 取窗口结束）

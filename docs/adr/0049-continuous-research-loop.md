# ADR-0049: 持续研究循环——调度、预算、生命周期护栏、审计与劣化监控（Phase 11 框架）

| 字段 | 值 |
|---|---|
| 状态 | **Accepted**（2026-09-25） |
| 日期 | 2026-09-25 |
| 决策者 | Claude Code（Opus），依 Raphael 2026-09-25 明确授权（红线除外） |
| 相关 Phase | Phase 11（Continuous Research Loop） |
| 影响范围 | `apps/worker/loop.py`、`apps/worker/degradation.py`、`research/loop/`；无契约变更（不新增 Schema） |
| 实施状态 | FRAMEWORK_IMPLEMENTED / NOT_VALIDATED |

## 背景

roadmap Phase 11 要求"新数据 → 状态更新 → 假设 → 实验 → 验证 → 记忆"持续运行、可无人值守且可审计，
每轮产出（含失败）入库，算力 / LLM 成本 / trial 数受控；禁止无限扩大 trial 预算，禁止自动晋升到 ACTIVE。
ADR-0044 已交付事件总线与幂等任务。边界约束：`apps/` 不得 import `research/`（01-system.md §3，架构边界测试）。

## 裁决

1. **机制与研究分离（位置）**。通用机制放在 `apps/worker/loop.py`：阶段 Protocol（`LoopStage`：
   `name` / `estimate` / `run`）、`RoundContext`、`LoopBudget`、`LifecycleGuard`、`LoopRecord` 与哈希链
   `LoopAuditLog`、调度器 `ResearchLoop`。它只依赖 `core` 与标准库（测试静态检查），不知道任何研究内容。
   具体研究阶段放在 `research/loop/stages.py`，组合根是 `research/loop/compose.py`（或测试）。依赖方向为
   research → apps/worker（worker 是运行宿主并公开阶段 Protocol，相当于插件向宿主注册），反方向仍被禁止。
   理由：roadmap 把 Phase 11 模块定为 `apps/worker`；把调度 / 预算 / 审计放进 research 会让未来 NATS worker
   进程要么 import research（违反边界）要么重写一遍机制。
2. **一轮 = 固定顺序的六个阶段**：`ingest → state → hypothesis → experiment → validation → memory`
   （`STAGE_ORDER`，构造时校验）。一轮是一个 `JobRunner` 任务（`research_loop.round`，`max_attempts = 1`：
   已花费预算的轮次不重试；重复提交由任务内容身份吸收）。
3. **预算先于阶段**。每个阶段先声明用量（trial 数、LLM 成本单位、算力秒），调度器在运行前对照
   `LoopBudget`（每轮 trial 上限 + 全生命周期 trial / LLM / 算力上限）检查：放不下 → 阶段 `REFUSED_BUDGET`、
   本轮 `BUDGET_EXHAUSTED`、循环停机；实际用量超过声明 → 照实计费、`BUDGET_OVERRUN`、停机（fail closed）；
   阶段抛错 → 按声明计费、本轮 `FAILED`（失败是研究数据），下一轮可继续。`LoopBudget` 不可变且无默认值，
   调度器没有扩大预算的途径；换预算 = 新 `LoopBudget`，其哈希写入每条记录。数字全部来自配置（测试用 TEST ONLY 数值）。
4. **生命周期护栏（结构性）**。阶段拿不到任何生命周期对象，只能经 `RoundContext.open_subject / advance`
   走 `LifecycleGuard`：只移动自己从 IDEA 打开的对象；从不设置 `approved_by`；目标只能是
   `AUTOMATABLE_TARGETS = {CANDIDATE, VALIDATION, OOS, REJECTED, FAILED}`（第一个人工门 `OOS → PAPER` 之前可达
   的状态）。`PAPER`、`PRODUCTION_CANDIDATE`、`ACTIVE`（以及 DEGRADED / REVALIDATION / RETIRED）对循环不可达，
   违规 → `GUARD_VIOLATION`、停机。测试同时对状态图做闭包检查。当前研究阶段只做筛查，筛查 PASS 留在
   VALIDATION，不进入 OOS（完整 P4 / P8 流水线接入后才考虑）。
5. **审计**。每轮（含失败、预算拒绝、护栏违规）一条 `LoopRecord`：轮次、派生种子、计划时刻
   `epoch + i × cadence`、预算哈希、各阶段状态 / 声明与实际用量 / JSON 摘要 / 错误、生命周期转移、累计用量、
   前一条记录哈希。不含墙钟时间 ⇒ 同种子同输入得到相同记录哈希（测试覆盖）。每个阶段完成发布
   `research_loop.stage`，每轮发布 `research_loop.round`（`EventBusAdapter`，测试用 `InMemoryEventBus`）。
6. **LLM 草稿只入人工审阅队列**。循环调用 LLM 产出的假设草稿进入 `ReviewQueue`，循环永不批准；
   人在循环外批准后，下一轮才登记（ADR-0040、09-security.md §3），证据带审阅人与 LlmCall 哈希。
7. **劣化监控**（`apps/worker/degradation.py`）：对比近期指标（模拟 / 纸面 / 回测）与验证基线，阈值只来自
   `ValidationProfile.lifecycle.degradation_thresholds`（或同形映射，无默认）；键 `metric` / `metric[>=]` 越高越好，
   `metric[<=]` 越低越好；缺少近期值报 `missing`（证据不足），不算健康也不算劣化。越限时在
   `research_loop.degradation` 发布事件；**不**做 `ACTIVE → DEGRADED` 转移（Control Plane 以事件为证据执行）。

## 后果

- 正面：循环可无人值守运行、可审计（哈希链 + 总线），预算与晋升边界由结构保证而非约定。
- 负面 / 未做（留给调试批次）：内存总线与内存审计日志不持久（NATS 与 Control Plane 持久化另立 ADR）；
  算力秒是阶段声明值而非实测；状态阶段是循环内的效率比摘要，尚未接 Phase 2 `StateProvider` 执行器；
  实验阶段未生成 `ExperimentSpec` / `ExperimentRun` 复现元组；验证阶段只有 G2 / G3 筛查，未接 Phase 4 完整流水线；
  研究仪表盘（apps/web）未接循环记录；`LoopRecord` 尚非版本化契约（进入 Control Plane 时再加 Schema）。

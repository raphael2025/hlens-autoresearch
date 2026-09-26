# apps/worker

异步任务执行：采集、计算、实验运行、验证。任务必须幂等可重试。事件总线通过 EventBusAdapter 访问（NATS 引入时机见待决 D-10）。

> 框架已实现（ADR-0044，FRAMEWORK_IMPLEMENTED / NOT_VALIDATED）：`jobs.py` 的 `JobRunner`（内容寻址任务 ID、至少一次消息 + 幂等执行、有界重试、失败记录不丢弃），总线为 `infrastructure/event_bus/InMemoryEventBus`。
> 持久总线（ADR-0044 Implementation note, file-backed bus, 2026-09-26）：`infrastructure/event_bus/FileEventBus(root)` 是同一 Protocol 的
> 落盘实现（哈希链主题日志 + 原子替换的消费者 offset，语义与内存总线一致，损坏即拒绝，单写者锁）；`JobRunner` 与 `ResearchLoop`
> 不需任何改动——总线由组合根注入。重启后未确认的任务消息重投，`JobRunner` 的幂等执行照常吸收；`JobRunner` 的结果表本身仍在内存中，
> 因此"已确认"是跨重启的唯一去重依据（先记录结果、后确认，崩溃于两者之间时重启会重跑该任务一次）。本目录仍只依赖 `core` 与标准库。

> 持续研究循环机制（[ADR-0049](../../docs/adr/0049-continuous-research-loop.md)，FRAMEWORK_IMPLEMENTED / NOT_VALIDATED）：
>
> - `loop.py`：`ResearchLoop` 按固定顺序 `ingest → state → hypothesis → experiment → validation → memory` 运行注入的阶段，
>   每轮是一个 `JobRunner` 任务；`LoopBudget`（每轮 trial、累计 trial / LLM 成本单位 / 算力秒，无默认值）在每个阶段**之前**检查，
>   放不下即停轮并停机、从不扩大；`LifecycleGuard` 是阶段唯一的生命周期写入口，只能到 CANDIDATE / VALIDATION / OOS / REJECTED / FAILED，
>   永不到 PAPER / PRODUCTION_CANDIDATE / ACTIVE，也从不设置 `approved_by`；每轮（含失败与拒绝）一条哈希链 `LoopRecord`，
>   阶段完成发布 `research_loop.stage`，轮次发布 `research_loop.round`。
>   W2（2026-09-25）：可选阶段 `evolution` 只能位于 hypothesis 与 experiment 之间（`OPTIONAL_STAGES` / `EXTENDED_STAGE_ORDER`）；
>   超出声明用量时阶段与轮次记录写明超出量（`overrun`）并停机；阶段抛 `StageFailed(usage=...)` 时按其报告的实际用量计费。
> - 持久审计与实测算力（ADR-0049 实施说明 2026-09-26）：`LoopAuditLog(path)` 可选落盘（`journal.py`：哈希链、只追加、fsync、
>   缩短 / 篡改 / 截断即拒绝；与 `research.persistence` 同一磁盘契约的独立实现，因为 apps 不得 import research）。每轮运行前写
>   `loop_round_started`、运行后写 `loop_round_recorded`；重新打开时重放校验，`ResearchLoop` 从中恢复下一轮序号、累计预算、停机状态与护栏对象，
>   从不重跑已记录轮次、从不重置预算；只开始未记录的轮次（中途崩溃）或审计写入失败 → `stopped`，须人工审查。
>   `metrics.py`：每次 `stage.run` 用单调墙钟 + 进程 CPU 时钟测量，放在哈希记录之外（`ResearchLoop.metrics`、主题 `research_loop.metrics`），
>   记录哈希保持确定；预算仍按 max(声明, 报告) 计费；`compute_tolerance_seconds`（无默认）给定时，实测超出声明多于容差的阶段被标记，
>   未给定只报告。
> - 轮次检查点（ADR-0049 实施说明 durable composition，2026-09-26）：`ResearchLoop(checkpoint=...)` 可选回调，在每轮运行结束、
>   审计记录该轮**之前**以该轮 `LoopRecord` 调用；组合根在此持久化其阶段跨轮依赖的状态（`research/loop/durable.py` 的记忆检查点，
>   写明 `record_hash`）。回调抛异常 → 该轮不记录、循环 `stopped`（与审计写入失败相同，fail closed）；未给定 = 原行为。
>   回调仍是研究无关的：本目录不知道检查点里是什么。
> - 持久复核修复（ADR-0049 实施说明 durable review fixes，2026-09-26）：`ResearchLoop` 续接审计时，任何一条记录的 `budget_hash`
>   与本循环的 `LoopBudget` 不同即拒绝（调高、调低都拒绝）——提高预算是人的决定，须新审计 / 新 `loop_id`。新增可选回调
>   `after_record`：在审计记录一轮**之后**以该轮 `LoopRecord` 调用（研究侧组合根用它前移目录外的锚点）；回调失败 → 该轮已记录、循环 `stopped`。
> - `degradation.py`：`DegradationMonitor` 用 `ValidationProfile.lifecycle.degradation_thresholds` 对比近期指标与验证基线，
>   越限发布 `research_loop.degradation` 事件（不做生命周期转移）。
> - 本目录只依赖 `core` 与标准库；具体研究阶段在 `research/loop/`，由研究侧组合根注入（apps 不 import research）。

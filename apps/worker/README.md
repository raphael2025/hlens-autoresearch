# apps/worker

异步任务执行：采集、计算、实验运行、验证。任务必须幂等可重试。事件总线通过 EventBusAdapter 访问（NATS 引入时机见待决 D-10）。

> 框架已实现（ADR-0044，FRAMEWORK_IMPLEMENTED / NOT_VALIDATED）：`jobs.py` 的 `JobRunner`（内容寻址任务 ID、至少一次消息 + 幂等执行、有界重试、失败记录不丢弃），总线为 `infrastructure/event_bus/InMemoryEventBus`。

> 持续研究循环机制（[ADR-0049](../../docs/adr/0049-continuous-research-loop.md)，FRAMEWORK_IMPLEMENTED / NOT_VALIDATED）：
>
> - `loop.py`：`ResearchLoop` 按固定顺序 `ingest → state → hypothesis → experiment → validation → memory` 运行注入的阶段，
>   每轮是一个 `JobRunner` 任务；`LoopBudget`（每轮 trial、累计 trial / LLM 成本单位 / 算力秒，无默认值）在每个阶段**之前**检查，
>   放不下即停轮并停机、从不扩大；`LifecycleGuard` 是阶段唯一的生命周期写入口，只能到 CANDIDATE / VALIDATION / OOS / REJECTED / FAILED，
>   永不到 PAPER / PRODUCTION_CANDIDATE / ACTIVE，也从不设置 `approved_by`；每轮（含失败与拒绝）一条哈希链 `LoopRecord`，
>   阶段完成发布 `research_loop.stage`，轮次发布 `research_loop.round`。
> - `degradation.py`：`DegradationMonitor` 用 `ValidationProfile.lifecycle.degradation_thresholds` 对比近期指标与验证基线，
>   越限发布 `research_loop.degradation` 事件（不做生命周期转移）。
> - 本目录只依赖 `core` 与标准库；具体研究阶段在 `research/loop/`，由研究侧组合根注入（apps 不 import research）。

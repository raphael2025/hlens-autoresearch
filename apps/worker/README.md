# apps/worker

异步任务执行：采集、计算、实验运行、验证。任务必须幂等可重试。事件总线通过 EventBusAdapter 访问（NATS 引入时机见待决 D-10）。

> 框架已实现（ADR-0044，FRAMEWORK_IMPLEMENTED / NOT_VALIDATED）：`jobs.py` 的 `JobRunner`（内容寻址任务 ID、至少一次消息 + 幂等执行、有界重试、失败记录不丢弃），总线为 `infrastructure/event_bus/InMemoryEventBus`。
> 持久总线（ADR-0044 Implementation note, file-backed bus, 2026-09-26）：`infrastructure/event_bus/FileEventBus(root)` 是同一 Protocol 的
> 落盘实现（哈希链主题日志 + 原子替换的消费者 offset，语义与内存总线一致，损坏即拒绝，单写者锁）；`JobRunner` 与 `ResearchLoop`
> 不需任何改动——总线由组合根注入。重启后未确认的任务消息重投，`JobRunner` 的幂等执行照常吸收。本目录仍只依赖 `core` 与标准库。
> 持久任务结果（ADR-0044 Implementation note, durable jobs and bus wiring, 2026-09-26）：`JobRunner(..., results=<path>, idempotent=...)`
> 把每个任务写入哈希链只追加日志（`journal.py`，与审计同一磁盘契约）：处理器运行**前** `job_started`、结果已知且确认消息**前** `job_result`。
> 重开时重放校验（链、行类型与字段、`job_id` = 名称 + 参数的内容哈希、结果只在开始之后且每个任务至多一个），不符即 `JobResultsCorrupted`。
> 已有结果的任务永不重跑：重投的消息直接按存储的结果确认（崩溃于记录与确认之间）。只开始、没有结果的任务（处理器中途进程死亡）：
> 调用方以 `idempotent=` 声明为幂等的处理器在重投时重跑；其余一律使 `run_pending` 抛 `JobInterrupted`（不轮询），等人工审查
> （`JobRunner.interrupted` 列出这些任务）。持久结果必须是 JSON（存储与返回的都是其 JSON 形式）；持久写入失败 → 运行器停止、消息不确认。
> 不给 `results` 时行为不变（结果表在内存中，崩溃于记录与确认之间会重跑一次）。
>
> 只读视图（2026-09-26，CODE_COMPLETE / DEBUG_PENDING；`apps/api` 的 `GET /jobs` 使用）：`read_job_results(path, idempotent=...)`
> 不需要总线或处理器、从不写入，以与运行器重开时**同一个**重放实现（`_Replay`，运行器与只读视图共用，二者不可能不一致）校验日志，
> 返回 `JobHistory`（`jobs`：按首次开始排序的 `JobRecord` —— `status` ∈ `succeeded` / `failed` / `interrupted`、`starts`、`reruns`、
> `first_seq` / `last_seq`、`outcome`；`head_hash`；`lines`）。校验失败抛 `JournalCorrupted` / `JobResultsCorrupted`，绝不返回部分数据；
> 缺失文件 = 空日志（不创建）。`idempotent` 必须与运行器声明的集合一致，否则含该处理器 `job_rerun` 行的日志被拒绝。
>
> **`idempotent=` —— 使用前必读**（ADR-0049 实施说明 review fixes 3，2026-09-26）：声明一个处理器幂等，是运行器**无法核实**的承诺——
> "中途死掉后再跑一次，世界的状态与只跑一次完全相同"（不重复发布、不重复花费、不产生下游不去重的第二次追加）。声明错了会静默重复副作用，因此：
> 默认没有任何处理器幂等；集合必须逐个写出处理器名（不是处理器的名字 → `ValueError`，给成单个字符串 → `TypeError`）；没有 `results=` 时
> 声明 `idempotent` → `ValueError`（内存模式没有中断记录，声明毫无作用）。每次重跑都可审计：写一条 `job_rerun` 行（`job_id`、`name`、
> `params`、`interrupted_starts` = 此前无结果的开始次数）**替代**第二条 `job_started`，`JobRunner.reruns` 按任务计数；重开时拒绝未经
> `job_rerun` 的重复开始、现在未声明幂等的处理器的重跑记录、计数不符、从未开始或已有结果的任务的重跑记录（`JobResultsCorrupted`）。
>
> `ResearchLoop` 的轮次任务以审计为持久结果：续接审计时，本循环已记录轮次的未确认轮次任务在构造时被确认、从不重跑（扫描**完整**待处理集：
> `poll` 没有游标，以倍增上限读取直到返回数不足，外来任务再多也不会遮住它们，且外来任务一条不确认；review fixes 3）；轮次记录后在
> `research_loop.round` 发布 `round_message(loop_id, record)`（记录的纯函数），发布失败 → 该轮已记录、循环 `stopped`，所以总线最多落后审计最后一轮
> （研究侧组合根重开时补齐并交叉核对，见 `research/loop/README.md`）。

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
> - 审计记录契约（[ADR-0050](../../docs/adr/0050-loop-audit-record-contract.md)）：`LoopRecord` 与两类日志行的载荷是
>   `core/contracts/loop_audit.py` 的版本化契约（`LoopRoundRecord` / `LoopRoundStarted` / `LoopRoundRecorded` 等），描述本目录
>   写出的既有字节（键与 `record_hash` 都不变；阶段顺序常量与 `check_stage_order` 也在该模块，这里原样再导出）。
>   `LoopAuditLog` 在 `begin_round` / `append`（持久或内存）写入前、以及重放每一行时（先核对存储的 `record_hash`）按契约校验，
>   不合格或不能逐字节往返即拒绝：写入被拒 → 该轮不记录、循环 `stopped`；重放被拒 → `LoopAuditCorrupted`。
>   阶段摘要因此必须是键为字符串的 JSON 对象。
> - `degradation.py`：`DegradationMonitor` 优先使用
>   `ValidationProfile.lifecycle.degradation_thresholds_exact`（精确 Decimal 阈值）；旧 Profile 未提供该字段时回退到
>   `degradation_thresholds`，对比近期指标与验证基线。阈值来源会记录实际使用的字段，
>   越限发布 `research_loop.degradation` 事件（不做生命周期转移）。所有指标都没有近期值时，检查为 `insufficient_evidence`
>   （`DegradationCheck.status`，并写入 `degradation_check` 报告），绝不报告为"未退化"；`observe` 此时在独立主题
>   `research_loop.degradation.insufficient_evidence` 发布恰好一条"监控无法判定"告警（D-DEG-IE：payload `subject` / `window` /
>   `status` / 排序的 `missing` 与 `required`，key `subject:window`），绝不发布退化事件。实际越限只发布 `research_loop.degradation`；
>   部分缺失且无越限不发布。需要发布而没有 bus → `ValueError`（fail closed）；`check()` 保持纯函数。两个主题都不做生命周期转移。
> - 本目录只依赖 `core` 与标准库；具体研究阶段在 `research/loop/`，由研究侧组合根注入（apps 不 import research）。

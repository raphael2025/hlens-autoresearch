# ADR-0049: 持续研究循环——调度、预算、生命周期护栏、审计与劣化监控（Phase 11 框架）

| 字段 | 值 |
|---|---|
| 状态 | **Accepted**（2026-09-25） |
| 日期 | 2026-09-25 |
| 决策者 | Claude Code（Opus），依 Raphael 2026-09-25 明确授权（红线除外） |
| 相关 Phase | Phase 11（Continuous Research Loop） |
| 影响范围 | `apps/worker/loop.py`、`apps/worker/degradation.py`、`apps/worker/journal.py` / `metrics.py`（2026-09-26）、`research/loop/`；无契约变更（不新增 Schema） |
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

## Implementation note (W2 loop wiring, 2026-09-25)

不新增 ADR：以下把本 ADR "后果 / 未做" 中列出的研究阶段换成其他 Phase 已交付的真实组件，并修复一次只读评审的发现。
状态仍为 FRAMEWORK_IMPLEMENTED / NOT_VALIDATED；无契约 / Schema 变更，所有阈值仍只来自 Profile 或显式参数。

1. **状态阶段**：F4 `bar_log_return`（`run_feature`，分块、每块多带一根前置 bar，块大小只影响性能）→ Phase 2
   `StateProvider`（`plugins/states`，测试用 `TrendRangeProvider`）经 `infrastructure.state.run_state` 在每个决策时刻与研究段末
   求值；同一特征值经 `infrastructure.strategy.signals.signals_from_features` 成为策略信号。
2. **实验阶段**（`research/loop/trials.py`）：运行前核对假设已按内容登记在 `TrialLedger`（否则 ERRORED，不运行）；
   为每个试验生成 06-experiment.md §2 的完整复现元组（`ExperimentSpec` / `ExperimentRun`：数据快照 = 合成市场哈希、
   code commit、plugin versions、dependency hashes、params / 搜索空间、seeds、environment lock、Constitution、Profile 引用 + 哈希 +
   选择依据、split spec、成本模型、LLM 调用）；经 `CandidateTrialRunner` 跑策略 → 风控 → 回测；把回测逐 bar 收益按决策期
   复合后归到决策时刻已知的 Phase 2 状态，得到 Phase 6 矩阵（绑定回测与状态结果哈希）。出错的试验同样入库。
3. **验证阶段**：Phase 4 / 8 `PipelineBacktestValidator`（G0 → G3，再 G4），`ExperimentMetadata` 的 `trial_index` /
   `family_trial_count` 来自 Ledger（含失败）。样本内 PASS 最多把对象推进到 **OOS**（由记忆阶段执行）。封存 OOS 默认保持封存：
   摄取阶段按 Profile 固定日历把封存窗口的 bar 扣留在 `SealedBars`，只有配置了显式 `OosUnsealBudget`（全局次数 + 人类批准人，
   自动化身份被拒）且样本内 PASS、本轮有封存数据、该族尚未开封时，才经 `SealedOosVault` 开封并运行 G5（每族一次）；G5 FAIL →
   `OOS → REJECTED`。`LifecycleGuard` 不变，测试覆盖所有 `FORBIDDEN_TARGETS`（含先走到 OOS 再尝试）。
4. **进化阶段（可选）**：`apps/worker/loop.py` 新增 `OPTIONAL_STAGES = {evolution}`，其唯一位置在 hypothesis 与 experiment 之间
   （`EXTENDED_STAGE_ORDER`，构造时校验）。到期轮次从更早轮次未被否证的最佳候选出发 `mutate`，经 `require_new_version` 与目录
   防覆盖，`LineageGraph` 不得有缺失的策略祖先；后代作为 `origin = combination` 的新假设先登记（计入 trial 与预算，预算放不下即
   `REFUSED_BUDGET`）、IDEA → CANDIDATE，并在本轮新数据上重新验证，不继承父代结论。`combine` / `retire` 不被循环使用
   （同一策略两个变体的参数必然冲突；退役只针对循环不可达的 ACTIVE）。
5. **评审修复**：(a) 阶段实际用量超过声明时，`StageRecord.overrun` 与 `LoopRecord.overrun` 记录超出量（逐维度实际 − 声明），
   循环停机；阶段失败时若抛出 `StageFailed(usage=...)` 则按其报告的实际用量计费（超出声明同样 `BUDGET_OVERRUN` 停机），否则仍按声明计费。
   (b) 进入哈希记录的浮点（均值、t 值、p 值、门值与阈值等）先转为固定量化（`1e-12`，half-even）的 Decimal 文本；报告、元数据与
   失败记录以本轮计划时刻盖章（不含墙钟）。(c) `ReviewQueue.approve` 要求非空、且不是循环自身 actor（组合根绑定）或任何
   `research_loop:` 自动化身份的审阅人，并追加记录审批（审阅人、草稿哈希、LLM 调用哈希）。
6. **E2E**（`tests/research/loop/test_loop_e2e.py`，TEST ONLY Profile / 预算，约 50 秒、< 300 MB）：植入效应 vs 纯噪声各 3 轮无人值守；
   同种子审计哈希一致；预算耗尽在进化阶段停机；无 PAPER / ACTIVE；后代为新版本、已登记并重新验证；封存 OOS 无预算不开封、
   有显式预算只开封一次。

仍未做（调试批次）：持久化审计与 NATS；滚动循环与固定日历 Profile 的配合（研究窗外数据不被使用，换窗口需新 Profile 与人工决定）；
逐 bar 状态归因（`matrix_from_backtest`）因逐 bar `run_state` 成本过高未用；验证阶段的技术失败没有 `VALIDATION → FAILED` 边，
只写 FailureRecord、生命周期不动；算力秒仍是声明值而非实测。

## Implementation note (review fixes 2, 2026-09-25)

不新增 ADR。第二轮只读评审的发现（调试待办 R19 – R22、R24）；状态仍为 FRAMEWORK_IMPLEMENTED / NOT_VALIDATED，
无契约 / Schema / 生命周期 / Constitution / Profile 结构变更。

1. **R19 OOS 的含义（语义核对，行为不变）**：文档支持"正在经过 / 有资格进入封存样本外检验"，不是"已通过 OOS"：
   ADR-0006 §1 与 07-validation.md §3 状态表写 `OOS | 正在经过封存样本外检验`；状态图的边为
   `VALIDATION → OOS : in-sample gates passed`、`OOS → PAPER : sealed OOS passed`、`OOS → REJECTED : OOS failed`；
   04-research-loop.md §7 允许循环自动到达 OOS。因此记忆阶段在样本内 G0 – G4 PASS 时移到 OOS（无论 G5 是否运行），
   转移证据是**样本内**报告（`validation_report:<in-sample id>`，原因写明 "eligible for the sealed OOS evaluation (G5)"）。
   G5 只决定 OOS 之内的去向：未运行 / INCONCLUSIVE（含 `consumed_without_result`）→ 留在 OOS；FAIL → `OOS → REJECTED`；
   PASS → 仍在 OOS（`OOS → PAPER` 需人工批准，护栏拒绝）。`MemoryStage` 文档写明；测试覆盖"无 G5 PASS 不越过 OOS"。
2. **R20 按族批准开封**：`OosUnsealBudget(max_unsealings, approved_families)`，`approved_families` 把每个经人批准的
   假设族映射到**该族**的批准人（写入该族 `OosUnsealing.approved_by`）；空名单、空族名、空批准人、自动化身份一律拒绝。
   循环只开封名单上的族（其他族状态 `sealed`，原因 "not on the unseal budget's approved list"）；全局次数仍由 vault 约束。
   原 `approved_by`（一次签名覆盖所有族）被移除。
3. **R21 开封即原子消耗唯一评估（真实缺陷）**：原流程先开封并释放封存 bar，之后若"无封存决策时刻 / 无非零仓位"提前返回，
   开封已用掉却没有评估记录，且 vault 仍允许他人读一次 `sealed_view`。修正：开封后立即 `SealedOosVault.claim_evaluation`
   （在任何封存样本离开 vault 前把该族记为已评估），`SealedBars.release` 只接受这个一次性凭据，G5 通过
   `SealedOosInput.evaluation` 读取标签（各一次）。提前结束或封存运行出错时，G5 报告为 INCONCLUSIVE：
   `G5.oos_evaluation` = `consumed_without_result:<原因>`（`pipeline.sealed_oos_without_result`），窗口永久关闭。
   两条提前返回路径各有回归测试。
4. **R24 预算按 max(声明, 报告) 计费**：完成的阶段按逐维度 `max(estimate, usage)` 计费（`StageRecord.charged` 记录与报告
   不同时的计费额）；报告用量是自报的，少报不能拉长预算。失败阶段仍按 `StageFailed` 报告的实际用量计费（原设计，未改）。
5. **R23 的连带影响**（见 ADR-0041 同日实施说明）：walk-forward 窗口无收益时 `G4.walk_forward.positive_fraction` 为 INCONCLUSIVE。
   循环每轮只验证新段（3 天），而 Profile 的 walk-forward 覆盖整个研究窗口（测试夹具 10 天），因此多轮 E2E 中植入效应在第 0 轮
   从 PASS 变为 INCONCLUSIVE（除该门外其余 G0 – G4 门均 PASS），留在 VALIDATION；原 PASS 正是靠丢弃空窗口得到的。
   PASS → OOS 与 G5 路径由单轮覆盖整个研究窗口的封存测试覆盖。滚动循环与固定日历 Profile 的配合（逐轮新段 vs 累计研究数据）
   仍是"仍未做"中的开放项，需要决定。

## Implementation note (accumulated validation window, 2026-09-25)

决策者 Claude Code（Opus），依 Raphael 2026-09-25 授权；非红线。不新增 ADR；无契约 / Schema / 生命周期 / Constitution / Profile 结构变更。
状态仍为 FRAMEWORK_IMPLEMENTED / NOT_VALIDATED。调试待办 R25。

1. **决定**：每轮的 EXPERIMENT 与 VALIDATION 阶段在**累计研究数据**上评估假设——截至本轮 `as_of` 摄取的全部研究窗口 bar
   （`ResearchMemory.research_data`，每个摄取市场一个 `ResearchPiece`，按时间排列）——而不是只用本轮新的 3 天段。
   理由：Profile 的 walk-forward 覆盖整个研究窗口；只用新段时其余窗口永远无收益，G4 `walk_forward.positive_fraction`
   在结构上必然 INCONCLUSIVE（R23，本 ADR「review fixes 2」第 5 条），循环永远无法把真实效应推进到 OOS。
   封存 OOS 窗口的 bar 仍只进入 `SealedBars`（被扣留，只凭一次性评估凭据释放），**永不**进入研究数据；研究窗口之前 / 封存窗口之后
   的 bar 仍不使用（计数）。每轮新市场接续上一轮的价格路径（初始价 = 上一轮最后收盘价），累计数据是一条连续路径，
   不会在段边界出现跳空。
2. **风险与对策（同一数据被反复评估）**：每个（假设，轮次）评估都是 TrialLedger 中**单独预登记的 trial**：
   `TrialLedger.register_reevaluation(hypothesis, attempt)`（attempt = `loop_round:<loop>:<round>`，同一 attempt 不重复计数，
   内容变化的假设被拒——那是新版本）；`trials(family)` 与 `trial_index` 计入每次重新评估，因此 G3 的多重检验校正随每次
   观察增强，循环预算也按 trial 计费。
3. **重新评估的范围**（`research/loop/stages.py::reevaluation_candidates`，由假设阶段在运行前登记）：只有仍开放的假设——
   生命周期 `VALIDATION`、最近一次 trial 的最近样本内报告为 INCONCLUSIVE——且累计研究数据的终点晚于它最近一次评估所用数据
   时才重新评估（相同数据只会多花一个 trial）；每轮最多 `max_reevaluations_per_round` 个（配置，无默认；0 = 不重新评估），
   按登记先后。永不重新评估：已 REJECTED（有 REJECTED FailureRecord 或处于 REJECTED）/ FAILED 的假设；已在 OOS 的假设
   （不再做样本内重跑）；最近一次试验或验证器出错的假设；仍在 CANDIDATE 的假设。重新评估的结果按原规则结算：
   PASS → OOS，FAIL → REJECTED，INCONCLUSIVE 留在 VALIDATION；出错的重新评估只写 FailureRecord（没有 `VALIDATION → FAILED` 边）。
4. **进化阶段**按每个假设**最近一次**验证判断是否"未被否证"（早先 INCONCLUSIVE、后来 FAIL 的不再作为父代）。
5. **复现记录**：`ExperimentSpec` 的 `dataset_snapshots` 为每个参与的摄取市场一条（含时间范围）；特征请求以各自市场哈希为标签，
   验证器 manifest 为累计数据哈希（`Segment.data_hash`）。状态阶段按研究段缓存特征运行、数据未增长时复用状态结果（纯缓存，结果不变）。
6. **E2E**（TEST ONLY Profile 的研究窗口改为 6 天 = 第 0、1 轮）：植入效应的 lookback-60 假设第 0 轮（3/6 天）只有
   `G4.walk_forward.positive_fraction` INCONCLUSIVE、留在 VALIDATION；第 1 轮在累计 6 天上作为新 trial 重新评估，G0 – G4 全部 PASS → OOS；
   第 2 轮在研究窗口之后，没有新研究数据，不重新评估，封存日被扣留。纯噪声什么也不通过。测试覆盖：trial 数随轮次增长、
   REJECTED / OOS 后不再评估、封存 bar 从不进入研究数据。

## Implementation note (durable audit and measured compute, 2026-09-26)

决策者 Claude Code（Opus），依 Raphael 2026-09-25 授权；非红线。不新增 ADR；无契约 / Schema / 生命周期 / Constitution / Profile 变更；
`LoopRecord` 载荷与哈希规则不变（同种子同输入的记录哈希与此前完全相同）。状态仍为 FRAMEWORK_IMPLEMENTED / NOT_VALIDATED。
调试待办 C 节 P11（「审计只在内存中」「算力秒为阶段自报」）。

1. **持久审计（可选）**：`LoopAuditLog(path)`（省略 `path` = 原纯内存行为）。每轮运行**前**写一行 `loop_round_started`
   （loop_id、轮次、前一条记录哈希），运行后写一行 `loop_round_recorded`（记录载荷 + `record_hash`）；每行 append / flush / fsync，
   先落盘再改内存。重新打开时重放并双重校验：文件行的哈希链（`JournalCorrupted`），以及每条记录——从载荷重建的 `LoopRecord`
   必须逐字段复现载荷与 `record_hash`，轮次连续、`previous_hash` 相接、先 started 后 recorded、loop_id 一致；未知行类型、
   篡改、重排、截断的尾行一律拒绝（`LoopAuditCorrupted` / `JournalCorrupted`），从不修复或跳过。
2. **重启续跑**：`ResearchLoop` 拿到非空审计时从中恢复——下一轮序号、累计预算用量（逐轮核对 `total_usage` 连加一致）、
   停机状态（最后一轮为停机状态则仍停机）、护栏已打开对象的生命周期（按记录的转移在新 `LifecycleGuard` 上重放，actor 不同即拒绝）。
   因此重启从不重跑已记录的轮次（重复提交的旧轮次任务因"乱序"失败，阶段不运行），也从不重置 trial / LLM / 算力预算。
   审计属于别的 loop、或种子 / epoch / cadence 与记录不符、或传入的护栏已有状态 → 构造即拒绝。只写了 started、没有 recorded 的轮次
   （进程在轮中死亡，已花费多少未知）→ 循环 `stopped`，拒绝继续，须人工审查；审计写入失败同样使循环 `stopped`（fail closed）。
3. **为什么在 `apps/worker` 独立实现同一 journal 契约**（`apps/worker/journal.py`，而不是移动 `research.persistence`）：
   `apps/` 不得 import `research/`（01-system.md §3）；本 ADR 第 1 条规定 worker 机制只依赖 `core` 与标准库（静态测试）——
   把 journal 放进 `infrastructure/` 会破坏这条；`core` 的 Domain 不得做 I/O；把研究平面代码直接搬进 `apps/` 等于研究代码
   不经 Promotion 成为生产代码（H5）。因此 worker 以约 150 行独立实现**同一磁盘契约**（行格式、GENESIS、哈希规则、fsync、
   缩短即拒绝），`research.persistence` 不动；一个交叉测试证明两者写出的文件逐字节相同、可互相重放、同样拒绝篡改，防止漂移。
4. **实测算力（不入哈希）**：调度器用单调墙钟（`time.monotonic_ns`）与进程 CPU 时钟（`time.process_time_ns`）测量每次
   `stage.run`，结果放在**哈希记录之外**：`ResearchLoop.metrics`（`RoundMetrics` / `StageMetrics`，以 `record_hash` 关联记录）
   与总线主题 `research_loop.metrics`。审计文件与记录哈希因此保持确定（测试：两次持久运行文件逐字节相同）。预算仍按
   `max(声明, 报告)` 计费，从不按实测计费。实测 = `max(墙钟, CPU)`（多线程时 CPU 可大于墙钟，取保守值）。
   `compute_tolerance_seconds`（秒，显式配置，**无默认**）：实测超出声明算力秒多于容差的阶段在指标中 `flagged = True`；
   未配置时只报告（`flagged = None`）。标记只是报告，不改变轮次状态、预算或停机（这些只由确定性的哈希记录决定）。
   只有真正运行了 `run` 的阶段有测量（预算拒绝、跳过、估算失败的阶段没有）。
5. **诚实边界 / 仍未做**：整行删除文件**尾部**的若干行会留下一条合法但更短的链（任何无外部锚点的哈希链都如此）；
   检测需要外部锚定的头哈希（例如总线上发布的 `record_hash`）。指标只保存在本进程内存与总线上，不随重启恢复。
   研究侧组合（`research/loop/compose.py`）仍未接持久审计：其 `ResearchMemory` 仍在内存中，只恢复审计而不恢复研究记忆会不一致。
   停机的持久审计要继续运行需要人工决定（新审计 / 新 loop_id）；NATS 与 Control Plane 持久化仍另立 ADR。
   回归测试：`tests/apps/test_research_loop_durable.py`、`tests/research/persistence/test_journal.py::test_the_worker_journal_shares_the_on_disk_contract`。

## Implementation note (durable composition, 2026-09-26)

决策者 Claude Code（Opus），依 Raphael 2026-09-25 授权；非红线。不新增 ADR；无契约 / Schema / 生命周期 / Constitution / Profile 变更；
`LoopRecord` 载荷与哈希规则不变。状态仍为 FRAMEWORK_IMPLEMENTED / NOT_VALIDATED。补上一条实施说明第 5 点的缺口
（「研究侧组合根仍未接持久审计」；调试待办 C 节 P11）。

1. **一个状态目录**：`open_synthetic_loop(config, state_dir=...)`（或 `build_synthetic_loop(..., state_dir=...)`；不给 `state_dir`
   = 原纯内存行为、记录哈希相同）。目录内：`audit.jsonl`（`LoopAuditLog`）、`memory.jsonl`（头行 `loop_state_opened` = 配置指纹，
   加每个已记录轮次一条 `round_memory` 检查点）、`trial_ledger.jsonl`（`TrialLedger(path)`）、`sealed_oos.jsonl`（`DurableUnsealingLedger`）、
   `lineage.jsonl`（`LineageGraph(path=...)`）、`reviews.jsonl`（新增 `ReviewQueue(path)`：入队 / 审批 / 取用逐行哈希链，重放时重新核验
   审批人非自动化身份、草稿与调用哈希一致）、`failures.jsonl`（`FailureRegistry`）。实现：`research/loop/durable.py`。
2. **检查点**：`ResearchLoop` 新增可选、研究无关的 `checkpoint` 回调，在每轮结束、审计记录之前以 `LoopRecord` 调用；回调失败 → 该轮不记录、
   循环 `stopped`（fail closed）。研究侧的检查点写明 `record_hash`、其余每个文件的位置（日志：行数 + 链头哈希；失败登记：条数 + 记录哈希摘要），
   以及本轮对 `ResearchMemory` 的增量：市场**规格**（重启时用确定性提供者重新生成，须复现 `market_hash`）、研究段、新增策略（后代由其父代
   与进化计划的 `provider_for` 重建）、试验 / 验证记录（pydantic 记录，复现各自内容哈希；不保存内存中的 `inputs` / `trial` 运行产物——
   `TrialOutcome` 为此新增 `knowledge_cutoff` 字段，重新评估的筛选改读它；`completed` 只看运行状态）、实验 / 状态 / 后代摘要。
3. **交叉校验**（重新打开时，任一不符 → `LoopStateInconsistent`，从不修复或跳过；单个文件自身损坏仍是 `JournalCorrupted`）：
   (1) 配置指纹（loop_id、种子、epoch / cadence、族、Profile、市场规格、策略目录、知识、特征 / 状态 / 标签 / 成本规格、Constitution 版本、
   代码提交、环境锁、是否进化；预算不在内）一致；缺头行而其他文件有状态 → 拒绝；(2) 审计中只 started 未 recorded 的轮次 → 拒绝（中断，须人工审查）；
   (3) 每条已记录轮次恰有一条同序号、同 `record_hash` 的检查点；(4) 每个检查点记录的每个文件位置都存在于该文件（同行号、同链哈希）、单调不回退，
   最后一个检查点即文件末尾——`reviews.jsonl` 之后只允许人工审批行（两轮之间的人工操作）；失败登记同理（条数 + 前缀摘要）；
   (5) 每轮恢复的增量等于审计哈希记录里的阶段摘要（摄取市场与规格哈希、状态摘要、每条实验行 = 其试验摘要、验证报告行、后代行）；
   (6) 审计登记 / 重新评估（按 attempt）的假设都在 TrialLedger；后代及父代规格在谱系且哈希一致；每条 `human_review:<who>` 证据在审阅队列中有
   该人对该草稿的审批且已取用；开封 / 已消耗的 G5 在开封账本中有同一批准人且已评估；审计列出的失败记录哈希都在失败登记中；
   (7) 护栏按审计重放后，每个生命周期对象都是已登记的假设。
4. **尾部截断**：单个文件删去尾部整行仍是一条合法的短链（单文件无法自知），但检查点记录了其余每个文件的位置、审计与检查点一一对应，
   所以任一**单个**文件（审计、检查点、账本、开封账本、谱系、审阅、失败登记）被截断都会被发现。**剩余限制**：所有文件被**一致地**截回
   更早的轮次边界是一段合法的较短历史，只能靠目录外的锚点（例如别处保存 / 总线上发布的 `record_hash`）发现；最后一轮之后的人工审批在被
   某轮取用之前不被任何检查点引用，只删这些审批等同于"尚未审批"。TrialLedger 行携带假设的墙钟 `created_at`（不在语义哈希内），因此另一次
   同配置运行产生的账本文件不是本历史，同样被拒绝（"rewritten"）。
5. **LLM 提供者属外部**：其自身状态（例如脚本化提供者已用到第几条输出）不是循环状态，由调用方续接。
6. **测试**（`tests/research/loop/test_loop_durable.py`，TEST ONLY 小配置：单个 lookback-60 知识假设、4 天一轮、显式开封预算、进化、脚本化 LLM，
   第 0 轮后人工审批一条草稿；约 20 秒、< 300 MB）：跑 1 轮 → 丢弃全部对象 → 从同一目录重建 → 跑余下 2 轮，最终审计哈希、总用量、trial 日志与族
   trial 数、全部生命周期历史、封存 OOS 状态、失败记录、审批与谱系与不中断的纯内存运行完全相同；拒绝用例：删除账本文件、审计超前于账本
   （账本截回第 0 轮 / 换成另一次运行的账本）、篡改审批（原地改 → 链断；改后重建链 → 与检查点不符；改成自动化身份 → 队列自身拒绝）、
   逐文件删尾行（8 例）、中断轮次、换配置、删除检查点文件、改写检查点增量；一致截断（已记录的限制）可打开并续跑出相同结果。
   机制侧回调：`tests/apps/test_research_loop_durable.py`（回调在审计记录之前看到最终记录；回调失败 → 该轮未记录、循环停止）。

## Implementation note (durable review fixes, 2026-09-26)

决策者 Claude Code（Opus），依 Raphael 2026-09-25 授权；非红线。不新增 ADR；无契约 / Schema / 生命周期 / Constitution / Profile 变更；
`LoopRecord` 载荷与哈希规则不变（同一种子的记录哈希不变）。状态仍为 FRAMEWORK_IMPLEMENTED / NOT_VALIDATED。处理只读复核对持久循环的三项发现。

1. **预算绑定到状态目录（高）**。上一条实施说明第 3 点 (1) 的"预算不在内"被本条取代：配置指纹现在包含 `LoopBudget` 的载荷与完整的
   `OosUnsealBudget`（`max_unsealings`、每个获准的族及其批准人）；`memory.jsonl` 头行版本升为 `STATE_VERSION = 2`（版本 1 的目录没有绑定预算，
   按版本不符拒绝，须用写它的代码打开或新建目录）。用任何不同的预算重新打开同一目录——更大的 trial / 算力 / LLM 预算、更大的开封额度、多一个
   获准族、换批准人、去掉开封预算，**也包括更小的预算**——都拒绝（`LoopStateInconsistent`，消息写明哪个预算、哪些字段不同）；完全相同的预算照常打开。
   理由：只拒绝"变大"需要逐字段定义"更大"（例如换批准人既不大也不小），任何变化都拒绝最简单也最安全。**提高预算是人的决定，须用新的
   `state_dir` 或新的 `loop_id`。** 机制侧同样绑定：`ResearchLoop` 续接审计时，任何一条记录的 `budget_hash` 与本循环的 `LoopBudget` 不同即拒绝
   （`ValueError`），因此不经组合根直接续接审计也不能换预算。
2. **可选外部锚点（高，原为已记录的限制）**。`open_synthetic_loop(..., anchor=...)`（`research/loop/durable.py`：`StateAnchor` 协议、
   `FileAnchor(path)`、`StateHead`）。锚点在每个已记录轮次之后（`ResearchLoop` 新增研究无关的可选回调 `after_record`，在审计记录该轮**之后**调用）
   收到目录的头：已记录轮数、审计头（最后一个 `record_hash`）、记忆日志链头、以及该轮检查点记录的其余每个文件位置。重新打开且给了锚点时：
   目录必须**不早于**锚点所记轮次且到该轮为止历史**相同**（该轮 `record_hash`、记忆日志该位置的链哈希、检查点记录的文件位置）；落后（一致截断 /
   整个目录被删除重建 / 换成旧副本）或分叉（另一次同配置运行）→ 拒绝；锚点为空而目录已有记录轮次（锚点丢失或中途才接上）→ 拒绝，由人工核对后
   有意重新锚定。全部校验（含护栏重放）通过后锚点前移到目录的头；`after_record` 失败 → 该轮已记录、循环 `stopped`，下次打开时目录领先锚点一轮，
   被接受并补记。`FileAnchor` 是目录**之外**的一个哈希链日志文件（放在目录内即拒绝），重复发布同一头为空操作，后退或同轮不同内容被拒绝；
   任何实现 `load` / `publish` 的对象都可作锚点（例如发布到总线或另一台主机）。**不给锚点时行为不变，限制照旧**：所有文件一致截回更早的轮次
   边界仍可作为较短历史打开（已记录）；最后一轮之后、尚未被某轮取用的人工审批不被任何检查点或锚点引用。
3. **节奏精确进入指纹（低）**。指纹原用 `int(cadence.total_seconds())`，相差不足一秒的节奏得到同一指纹；改为 `cadence_microseconds`
   （`timedelta` 的分辨率，整数）。
4. **测试**：`tests/research/loop/test_loop_durable.py`——换预算拒绝（7 例：更大的累计 trial / 算力、更小的每轮 trial、更大的开封额度、多一个获准族、
   换批准人、去掉开封预算，且拒绝不改动任何文件）、相同预算接受、节奏相差 500 ms / 1 µs 指纹不同；带锚点的重启与不中断运行完全相同（锚点日志
   依次为 0、1、2、3 轮，重开不追加）；有锚点时一致截断被拒绝（同一目录无锚点仍可打开：已记录的限制）；目录被删除后重建被拒绝；分叉历史被拒绝；
   锚点丢失 / 放在目录内被拒绝；锚点不后退。`tests/apps/test_research_loop_durable.py`——续接审计时换预算（四个字段各自调高 / 调低）被拒绝、相同
   预算接受；`after_record` 在审计记录之后看到该轮；其失败时该轮仍已记录、循环停止。

## Implementation note (approvals between rounds, 2026-09-26)

决策者 Claude Code（Opus），依 Raphael 2026-09-25 授权；非红线。不新增 ADR；无契约 / Schema / 生命周期 / Constitution / Profile 变更；
`LoopRecord` 载荷与哈希规则不变（同一种子的记录哈希不变）。状态仍为 FRAMEWORK_IMPLEMENTED / NOT_VALIDATED。补上「durable composition」
第 4 点与「durable review fixes」第 2 点记录的缺口：最后一轮之后（尚未被某轮取用）的人工审批既不被每轮检查点、也不被外部锚点引用——
把审阅日志截回审批之前会被接受，轮间伪造追加的审批只做形式校验。

1. **轮间检查点**：`open_state` 把 `DurableState` 绑定为审阅队列的观察者（`ReviewQueue.observe` / 新增 `ReviewObserver` 协议，
   `research/loop/memory.py`）。`ReviewQueue.approve` 先问观察者（轮次运行中 → 拒绝，不写任何东西），审批写入 `reviews.jsonl` 后
   **立即**在 `memory.jsonl` 追加一条 `between_rounds` 行：已记录轮数、审计头、各文件位置（`heads`）、所覆盖的审批（审阅日志行的
   `seq` / `hash`、草稿键、审批人），并以同一 `StateHead` 机制发布给锚点。`StateHead` 新增 `memory_seq`（记忆日志行数）：轮间审批使其加一、
   `rounds` 不变；`FileAnchor` 只接受 `memory_seq` 严格增大且轮数不减的头（不后退、不横移）。`memory.jsonl` 版本升为 `STATE_VERSION = 3`
   （版本 2 目录的轮间审批从未有检查点，按版本不符拒绝）。
2. **重新打开的校验**（任一不符 → `LoopStateInconsistent`）：有锚点时，每个文件（含审阅日志）必须不短于锚点记录的位置且该位置同一行
   （审阅日志不早于锚点记录的审阅头）；每条轮间检查点紧跟其轮次（轮数与审计头一致）、只让审阅日志前进恰好它所指的那一条审批、其余文件
   （TrialLedger、开封账本、谱系、失败登记）不动；最后一个检查点（轮次或轮间）即每个文件的末尾——审阅日志也不再例外；审阅日志中**每一条**
   审批行都必须有轮间检查点指向它，否则拒绝（伪造追加、轮中审批、审批后检查点前进程死亡——由人核对）。进程内：审批已写入而其检查点失败时，
   下一轮的检查点回调拒绝（该轮不记录、循环停止）。
3. **轮间没有别的合法写入**：封存 OOS 的开封审批是配置（指纹中的 `OosUnsealBudget`，即被锚定的头行），开封本身只在轮内由验证阶段写入；
   TrialLedger、谱系、失败登记同样只在轮内增长。它们在最后一个检查点之后多出的行，无论有无轮间检查点都拒绝。
4. **仍无法发现的（已记录的限制）**：不给锚点时，把一条审批**连同**它的轮间检查点一起删掉（其后没有别的行）是一段合法的较短历史，打开后等同
   "尚未审批"（一致截断的限制，宽度为一次轮间操作；有锚点时被拒绝）。无论有无锚点：能写状态目录并遵循格式的人（在 `open_state` 打开的状态上
   调用 `approve`，或手写同样的两行）可以追加一条带检查点的审批——日志是哈希链不是签名，审批人身份只是声明，且领先于锚点的目录按设计被接受；
   这需要经过认证的审批通道（不在本批范围内）。
5. **测试**：`tests/research/loop/test_loop_durable.py`——合法轮间审批立即有检查点、重启结果与不中断运行完全相同（原有重启测试）；带锚点时锚点
   日志为 0、1、1、2、3 轮（`memory_seq` 1 – 5）；最后一轮之后的审批使锚点前进一行且重启后仍在；只删审批行被拒（有无锚点）；审批连同检查点一起删
   在有锚点时被拒且锚点不变（无锚点可打开：已记录的限制）；无检查点的伪造审批（最后一轮之后，有无锚点；早先轮次内）被拒；检查点指向别的审批
   被拒；轮中审批被拒且不写任何文件；检查点失败 → 下一轮不被记录、重新打开被拒；`FileAnchor` 拒绝更早的记忆行、同一行不同内容与更少的轮数。

## Implementation note (durable jobs and bus wiring, 2026-09-26)

决策者 Claude Code（Opus），依 Raphael 2026-09-25 授权；非红线。不新增 ADR；无契约 / Schema / 生命周期 / Constitution / Profile 变更；
`LoopRecord` 载荷与哈希规则不变（同一种子的记录哈希不变）。状态仍为 FRAMEWORK_IMPLEMENTED / NOT_VALIDATED。补上 ADR-0044「file-backed bus」
第 6 点记录的后续工作（组合根自动使用 `state_dir/bus` 并与审计交叉校验）；任务结果持久化见 ADR-0044 同名实施说明。

1. **自动总线**：`open_synthetic_loop(config, state_dir=...)`（及 `build_synthetic_loop(..., state_dir=...)`）不给 `bus` 时打开
   `FileEventBus(state_dir / "bus")`，由组合根持有：`DurableLoop.bus` / `owned_bus`，`DurableLoop.close()`、`with` 块或循环对象被回收
   （`weakref.finalize`）释放总线锁。调用方自带的总线照旧使用、**不**核对（它可能是内存总线或多个循环共享，组合根无权判断）；纯内存组合仍必须传 `bus`。
2. **交叉核对**（`check_round_bus`，在组合循环**之前**；审计已由 `open_state` 校验）：用一个从不确认的专用消费者（`research_loop_bus_audit`，
   与其他消费者独立）读 `research_loop.round`，每条消息必须等于审计对应轮次的 `apps.worker.loop.round_message(loop_id, record)`——同键、
   同 `record_hash`、同记录（比较 `message_id`，即主题 + 键 + 载荷的内容哈希），按顺序。
3. **取舍：落后多少可以补**。循环只在审计 fsync 该轮**之后**才发布轮次消息；本批让轮次发布失败也使循环 `stopped`（该轮已记录，fail closed，
   与 `after_record` 失败相同）；组合根在任何新轮次之前补齐。所以健康的历史里总线最多落后审计**最后一轮**（进程死于审计与发布之间）。
   决定：**恰好落后最后一轮 → 从审计补发**（消息是已校验记录的纯函数，补发的就是不中断运行本应发布的那条，不会引入新内容）；
   **落后两轮及以上 → 拒绝**（总线被截断、替换、丢失或从未用于此目录；空总线而审计已有两轮以上同样拒绝），须人工核对；
   **超前**（消息多于审计轮数：审计被回滚，或换了别的总线）→ 拒绝——总线在此充当审计之外的见证，超前正是它能发现的回滚；
   **外来 / 乱序**记录（任一位置的消息不等于审计该轮）→ 拒绝。拒绝时（`LoopStateInconsistent`）不向总线写任何东西、释放锁。
4. **记录与确认之间的崩溃**：持久总线会重投该轮的轮次任务；`ResearchLoop` 续接审计时按审计确认它（ADR-0044 同名实施说明第 5 点），
   不重跑，下一次 `run_unattended` 正常续跑。
5. **仍未做 / 限制**：只有 `research_loop.round` 被核对；`research_loop.stage` / `metrics` / `jobs` 主题的尾部删除仍受 ADR-0044 所述限制
   （最后一次写消费者状态之后追加、又被删除的消息不可发现）；审计与总线**一起**一致截回更早轮次仍需外部锚点才能发现（已记录的限制）；
   只有合成市场组合根。
6. **测试**：`tests/research/loop/test_loop_durable.py`——自动总线的重启（第一个循环不调用 `close()`、只被丢弃）与不中断运行完全相同，总线重放每轮
   `record_hash`、全部轮次任务已确认，一致目录重开不写任何东西；审计与总线之间崩溃（第 1 轮）→ 重开补发第 1 轮、按审计确认其任务、续跑结果与不中断
   运行相同；总线尾部少一轮 → 补发，少两轮 → 拒绝且文件不变、锁已释放；外来记录、乱序、超前、总线目录丢失 → 拒绝；纯内存组合缺 `bus` → 拒绝。
   `tests/apps/test_research_loop_durable.py`——轮次发布失败 → 该轮已记录、循环停止、总线只少这一轮。

## Implementation note (dataset-backed loop, 2026-09-26)

决策者 Claude Code（Opus），依 Raphael 2026-09-25 授权；非红线。不新增 ADR；无契约 / Schema / 生命周期 / Constitution / Profile 变更；
不改 `core/`、`infrastructure/dataset/*`、`infrastructure/feature/dataset.py`；`LoopRecord` 载荷与哈希规则不变。状态仍为
FRAMEWORK_IMPLEMENTED / NOT_VALIDATED。补上调试待办 C 节 P11「持久组合只有合成市场组合根」与 D 节第 4 步的循环部分。

1. **可插拔的轮次数据源**：摄取阶段之后的阶段只经 `research.loop.segment.RoundData` 协议读本轮数据（研究 bar 与决策网格、被扣留的
   `SealedBars`、研究数据上的特征运行、复现元组的数据集快照、验证器 outcome 请求的 manifest 哈希、`G0.manifest_binding` 的数据集绑定）。
   `Segment` 是合成实现（原 `StateStage` / `ExperimentStage` / `ValidationStage` 中的合成专用代码移入其方法，逐字节等价）；
   `compose.compose_loop(settings, ingest, ...)` / `compose_durable(...)` 是两种组合根共用的组合（`LoopSettings` 协议、`settings_fingerprint`）。合成记录哈希不变：
   重构前后在规划 / 纯噪声 3 轮、封存无预算、显式开封 G5、持久重启五种配置上逐一比对，记录哈希完全相同；`tests/research/loop` 全部通过。
2. **数据集数据源**（`research/loop/dataset_source.py`）：`DatasetIngestStage` 每轮读取 `DatasetRound` 声明的 manifest（哈希只是声明，
   全部经 `DatasetBuilder` 自己的验证型 `ManifestStore` 加载，从不按信任接受）：先加载价格 manifest，其视图（`simulation_time`）晚于本轮
   `as_of` → 拒绝（本轮不读任何截止时刻之后的数据）；数据窗口必须在 Profile 研究窗口 `[research_window_start, sealed_oos_boundary)` 之内
   （研究 manifest 从不含封存窗口的行，因而特征请求——必须携带该标的全部数据集行——与回测都见不到封存数据）；`pair_manifests` 证明特征 /
   价格 manifest 为同一数据的一对；回测 bar 来自 `backtest_bars_from_dataset`，特征只经 `feature_request_from_dataset`（区间 manifest 自身
   PIT 选择的 `bar_observations`，在每根研究 bar 收盘时评估）；每根 bar、每条观测再次核对不晚于截止。可选的 `sealed_manifest_hash`
   （封存窗口的点时刻 manifest）按与合成摄取相同的固定日历切分：封存窗口 bar 扣留在 `SealedBars`，永不进入研究数据。一轮的累计研究数据即
   该轮 pair 覆盖的研究窗口（逐轮截止时刻后移，数据随轮次增长，重新评估照常）。
3. **绑定**：验证器拿到 `dataset_bars`（已证明的 bar）、本轮 `ManifestPair` 与每个特征请求的 manifest 哈希（状态阶段产物
   `feature_manifest_hashes`），因此每份报告都运行 `G0.manifest_binding`；复现元组的数据集快照为两份 manifest 的 `DatasetRef`；实验摘要行
   写明特征 / 价格 manifest 与 pair 哈希；`plugin_versions` 记录配对规则（`research_dataset_manifest_pair@1.0.0` → `PAIR_RULE_HASH`）。
4. **组合根**（`research/loop/dataset_compose.py`）：`DatasetLoopConfig`（与 `SyntheticLoopConfig` 相同的设置，市场换成 Canonical
   `symbol`、声明的 `rounds`（第 `i` 轮读 `rounds[i]`，超出即该轮摄取失败并被记录）与摄取算力声明）、`build_dataset_loop` /
   `open_dataset_loop`（经共用的 `compose_durable`：与合成组合相同的预算、生命周期护栏（最多 OOS，从不 PAPER / ACTIVE）、审计、持久状态目录、锚点与自动持久总线）、
   `dataset_loop_fingerprint`（共用部分 + 来源、标的与每轮声明的全部 manifest 哈希）。持久状态不保存摄取记忆（`open_state(provider=None)`：
   每个检查点的 `markets` / `research_data` 必须为空）；重新打开时每条已记录的摄取须恰好读了配置为该轮声明的 manifest，否则
   `LoopStateInconsistent`。
5. **未接线（fail closed）**：数据集轮次上的 G5 需要释放的封存 bar 上的信号，即封存窗口的特征 manifest；`DatasetLoopConfig` 拒绝
   `OosUnsealBudget`，`signals_with` 拒绝，封存窗口保持封存（与没有开封预算的合成循环相同）。每轮约 6 – 7 次验证型 manifest 加载
   （每次重新推导整个构建，含质量报告），是本组合的主要耗时；manifest 由数据平面预先构建，循环不构建数据集。
6. **测试**（`tests/infrastructure/e2e/test_research_loop_real_data.py`，`postgres` 标记，PostgreSQL 测试 catalog + `tmp_path`
   warehouse；TEST ONLY Profile / 参数 / 预算）：Binance 格式 1m kline（BTCUSDT / ETHUSDT 各 420 根，跨两个 UTC 日：研究窗口
   2023-11-14 18:00 – 24:00，封存窗口 2023-11-15 00:00 – 01:00）经真实入库路径进入隔离 catalog；两轮无人值守（截止 21:00 与次日 01:00）：
   全部阶段完成、审计哈希链可重放；每份验证报告 `G0.manifest_binding` PASS、判定属于已定义判定（从不断言 PASS）；试验 bar / 信号 / 决策
   时刻均不晚于本轮截止、没有封存窗口 bar，第 1 轮扣留 59 根封存 bar，开封账本为空；复现元组引用研究数据集快照；新进程重跑（只按声明哈希
   读取已持久的 manifest，两轮之间重新打开状态目录）记录哈希完全相同；换一组声明的轮次重新打开被拒；声明视图晚于本轮截止的一轮在摄取时被拒；
   无 PAPER / ACTIVE；自动持久总线与审计一致（`check_round_bus`）。约 4 分钟、< 2 GB。

## Implementation note (review fixes 3, 2026-09-26)

决策者 Claude Code（Opus），依 Raphael 2026-09-25 授权；非红线。不新增 ADR；无契约 / Schema / 生命周期 / Constitution / Profile 变更；
`LoopRecord` 载荷与哈希规则不变。状态仍为 FRAMEWORK_IMPLEMENTED / NOT_VALIDATED。修正调试复核的三项发现（第 3 项见 ADR-0041 同名实施说明）：

1. **调用方自带的总线也核对**（`research/loop/compose.py` `compose_durable`）：此前持久模式下给了 `bus` 就不跑 `check_round_bus`（上文
   「durable jobs and bus wiring」第 1 点的取舍），调用方的总线从不与审计核对。改为：任何总线（自带或自动）在组合循环之前都经
   `check_round_bus`，规则与自动总线相同（超前 / 外来 / 乱序拒绝、只落后最后一轮从审计补发、落后两轮及以上拒绝；拒绝时不写入，自带总线
   仍由调用方关闭）。**决定：`InMemoryEventBus` 不是持久总线**——新进程里它必然为空，"落后"不是截断的证据；对它 `check_round_bus(...,
   durable=False)` 把审计缺少的**全部**轮次按序补发，使它同样恰好持有审计的各轮（外来 / 乱序 / 超前仍拒绝）。选择补发而非"接受为空"：
   下游读 `research_loop.round` 的消费者在任何总线上看到的都是同一份与审计一致的历史。判定依据是类型（`isinstance(bus, InMemoryEventBus)`），
   其他任何总线类型一律按持久核对（fail closed）。
2. **已记录轮次任务的完整扫描**（`apps/worker/loop.py` `_settle_recorded_jobs`）：此前每次 `poll` 100 条，一页里没有本循环的任务就返回；
   `EventBusAdapter.poll` 没有游标（总是返回**最前面**的 `limit` 条未确认消息），外来任务不能确认，所以 ≥ 100 条外来任务排在前面时，
   已记录轮次的任务留在未确认状态，违反"构造时确认"的保证。改为 `_all_pending`：以倍增的上限（128 起）读取，直到返回数少于上限，即得到完整
   待处理集（Protocol 下最大的安全做法；内存与待处理集成线性，总线本身也持有它们），只确认本循环已记录轮次的任务（同一 `message_id`
   只确认一次），外来任务一条不确认。
3. **`idempotent=` 可审计、难误用**（`apps/worker/jobs.py`；ADR-0044 durable jobs 的补充）：模块文档新增醒目的「`idempotent=` — read
   before using」一节。中断后重跑一个声明幂等的任务时写 `job_rerun` 行（`job_id`、`name`、`params`、`interrupted_starts` = 此前无结果的
   开始次数），**替代**原来隐含的第二条 `job_started`；`JobRunner.reruns` 按任务计数。重新打开时拒绝：未经 `job_rerun` 的重复
   `job_started`（未审计的重跑，无论是否声明幂等——本批之前按旧格式写出的"重复开始"日志因此被拒，fail closed；尚无调用方使用
   `idempotent=`）、现在未声明幂等的处理器的 `job_rerun`、计数与日志不符、从未开始或已有结果的任务的 `job_rerun`。`idempotent` 给成单个
   字符串 → `TypeError`；不给 `results=` 时声明 `idempotent` → `ValueError`（内存模式没有中断记录，声明毫无作用却给人安全的错觉）。
4. **测试**：`tests/research/loop/test_loop_durable.py`——自带 `FileEventBus` 含外来轮次记录 → 拒绝且文件不变、总线仍可用；自带总线落后一轮
   → 补发，落后两轮 → 拒绝；自带 `InMemoryEventBus` → 审计各轮按序补发、再次打开不重复、含外来消息 → 拒绝。
   `tests/apps/test_research_loop_durable.py`——150 条外来任务排在一条已记录轮次任务（及其重复）之前：构造时该任务被确认、外来任务全部
   保留且顺序不变（内存与文件总线各一次；旧实现两者都失败）。`tests/apps/test_worker_jobs.py`——两次中断后重跑：日志为
   `job_started, job_rerun(1), job_rerun(2), job_result`，`reruns` 重开后仍为 2，撤销幂等声明后重开被拒；伪造的重跑历史逐项被拒；误用声明被拒。

## Implementation note (dataset G5, 2026-09-26)

不改 `core/`、`infrastructure/dataset/*`、`infrastructure/feature/dataset.py`；`LoopRecord` 载荷与哈希规则不变，合成路径的记录哈希不变
（`tests/research/loop` 全部通过）。状态仍为 FRAMEWORK_IMPLEMENTED / NOT_VALIDATED。取代上一条实施说明第 5 点（「未接线」）。

1. **封存 manifest 对**：`DatasetRound` 新增 `sealed_feature_manifest_hash` + `sealed_price_manifest_hash`（二者同在或同缺；与只扣留的
   `sealed_manifest_hash` 互斥）。未声明时 `payload()` 与摄取摘要不变（旧指纹 / 记录不变）；声明时两哈希进入指纹与摄取摘要，重新打开时
   已记录摄取须与声明一致。`DatasetLoopConfig` 只在至少一轮声明了封存对时接受 `OosUnsealBudget`（否则仍以「G5 over dataset rounds is not wired」拒绝）。
2. **开封前不读任何封存数据**：`segment.SealedSource` 协议（`evaluable(as_of)` / `release(evaluation)`）取代验证阶段对 `len(segment.sealed)`
   的判断；`SealedBars.evaluable` 与原判断相同（原因文本不变）。摄取阶段**完全不读**封存对（`dataset_source.SealedDatasetPair`，摘要
   `sealed_bars_withheld = None`）；`evaluable` 只按声明、Profile 窗口与本轮截止判断（窗口终点须不晚于 `as_of`）。验证阶段的顺序不变：无预算 →
   不可评估 → 族未获准 → 已开封 → 全局额度用尽；随后 `unseal` → `claim_evaluation`（记为已消耗）→ `release`。`release` 才经验证型
   `ManifestStore` 加载两份封存 manifest。
3. **开封后的证明**（任一不符 → `segment.SealedDataRefused`，验证阶段记为 INCONCLUSIVE `consumed_without_result:sealed_data_refused`，
   该族窗口永久关闭）：(a) 两份封存 manifest 与研究 pair 的 `snapshot_bindings`、`knowledge_cutoff`、ADR-0032 选择、availability /
   precedence / parser / PIT 规则绑定、universe spec、数据集表、lineage 的 Canonical 表集合相同（研究 pair 两份在这些字段上已由 `pair_manifests`
   证明相等，故与其价格 manifest 比较）；(b) 两份的数据窗口**恰好**为 Profile 封存窗口 `[boundary, boundary + sealed_oos_length)`；(c) 价格视图在
   `[窗口终点, as_of]` 内；(d) `pair_manifests` 证明二者为同一链；(e) 封存 bar 经 `backtest_bars_from_dataset`，特征观测为封存区间 manifest
   自身 PIT 选择的 `bar_observations`，逐条核对不晚于截止。**无法由 manifest 字段跨两对证明、未核对**：成员 / 排除集合（上市状态可在两个窗口间
   合法变化；bar 路径拒绝无行的标的）、质量报告 id（按日分区，必然不同）、lineage 修订与证据缺口（数据不同）——封存对内部由 `pair_manifests` 核对。
4. **G5 信号与绑定**：`signals_with` 在封存区间 manifest 上只经 `feature_request_from_dataset` 求值（每根已释放 bar 收盘时），研究信号在前。请求只能
   携带该 manifest 的行，**没有跨边界的前置 bar**：封存窗口开头的前几次评估不可计算，策略在窗口内重新预热（保守；短于预热的窗口以
   `consumed_without_result` 结束）。G5 报告首个门为 `G0.manifest_binding`（`research.strategies.validation.binding_mismatches`：研究 bar 对研究
   pair、已释放的封存 bar 与封存特征请求对封存 pair，名称前缀 `research:` / `sealed:`，计数为值；不符 FAIL → `OOS → REJECTED` / CONTRACT_VIOLATION），
   摘要写 `manifest_binding_mismatches`；合成路径不加此门（`sealed_binding()` 为空，报告不变）。G5 结局标签的 manifest 哈希为
   `content_hash({research: 研究价格 manifest, sealed: 封存价格 manifest})`。
5. **取舍**：「开封前一字节都不读」的代价是配置错误的封存对也会花掉该族唯一的开封（开封后才能证明它）；不选「开封前先加载 manifest 核对」，
   因为验证型加载会重新推导整个构建，即读取封存窗口的数据。其余保证沿用合成路径与持久状态：只开封获准族（人类批准人）、样本内 PASS 才开封、
   每次开封一次评估、提前结束 `consumed_without_result`、开封账本为状态目录的 `sealed_oos.jsonl`（重启不再开封）、开封预算与封存对在指纹中。
   一次开封另加约 6 次验证型 manifest 加载（加载两份、`pair_manifests` 两次、bar 路径与特征请求各一次）。
6. **测试**：`tests/infrastructure/e2e/test_research_loop_real_data_g5.py`（`postgres`；模块级夹具一次入库 + 六次构建；夹具数据同 dataset-backed
   冒烟，但第二天 5 小时 = TEST ONLY 封存窗口，供 240 根回看预热；TEST ONLY 宽松 Profile 与近零成本模型只为让样本内 PASS 可达，不是市场结论）：
   获准族开封一次，G5 报告含 G5 门与 PASS 的 `G0.manifest_binding`、审计同样记录；开封前没有任何封存 manifest 被加载（记录每次验证型加载及其时刻
   该族是否已认领）；新进程重开状态目录后同族的后续样本内 PASS 不再开封（`the family already used its unsealing`），且不读封存对；未获准族从不开封、
   不读封存对；上游 snapshot 不同的封存对在认领后被拒（只有两次 manifest 加载，无 bar 读取，窗口已消耗，留在 OOS）。约 4 分钟、< 2 GB。无数据库单元：
   `tests/infrastructure/e2e/test_research_loop_dataset_g5_units.py`（声明规则、预算需封存对、指纹、`evaluable` 与释放前的拒绝不触及存储）。

## Implementation note (review fixes 4, 2026-09-26)

决策者 Claude Code（Opus），依 Raphael 2026-09-25 授权；非红线。不新增 ADR；无契约 / Schema / 生命周期 / Constitution / Profile 变更；
不改 `core/`、`infrastructure/dataset/*`、`infrastructure/feature/dataset.py`；`LoopRecord` 载荷与哈希规则不变。状态仍为
FRAMEWORK_IMPLEMENTED / NOT_VALIDATED。修正封存 OOS 处理的只读复核发现：

1. **只扣留的封存声明不再读取任何数据**（HIGH，`research/loop/dataset_source.py`）：此前 `DatasetRound.sealed_manifest_hash` 路径在摄取时调用
   `backtest_bars_from_dataset` 读封存窗口的 bar 以计数——未评估，但封存 OHLC 在任何认领之前进入了进程内存，违反「认领之前不读封存窗口的任何
   东西」（上文 dataset G5 说明）。改为：摄取只记录**声明**——`sealed_manifest_hash` 与 `sealed_window`（Profile 的封存窗口，不触及存储）；
   `WithheldSealedWindow`（取代 `WithheldSealedBars`）只持有窗口与声明哈希，永不可评估、`release` 恒拒绝。封存 bar 的计数无法不读行（或验证型加载
   manifest）而得，**故删除**：数据集轮次的 `sealed_bars_withheld` 恒为 `None`，`unused_bars` 移除，声明的 view 晚于截止也不再在摄取时被拒（它从不被读）。
   只扣留的轮次记录哈希因此改变（摘要字段变化）；合成路径不变。合成路径的封存 bar 是**生成**的（生成整段市场再按日历扣留）：不是读取真实
   封存数据，没有真实 OOS 泄漏；生成的市场留在摄取侧（`ResearchMemory.markets` 供持久恢复重新生成、`Segment.market` / `ResearchPiece.market`
   提供市场哈希），摄取之后的阶段只经 `RoundData` 协议读本轮数据，协议不暴露市场，封存 bar 只经已认领评估的 `sealed.release` 交出。
2. **开封预算需要持久开封账本**（HIGH，`research/loop/trials.py`）：默认 `InMemoryUnsealingLedger` 在进程重启后忘记开封，非持久循环可能对同一族
   再次运行 G5。改为：`OosUnsealBudget` 只与 `DurableUnsealingLedger` 一起被接受（`state_dir` 的 `sealed_oos.jsonl`，或显式传入
   `ResearchMemory(oos_ledger=DurableUnsealingLedger(path))`）；`ValidationStage` 在构造时与每次开封前（`require_durable_unsealing`，账本是可变字段）
   检查，内存账本 → `ValueError`（开封前 fail closed，什么都不开封）。其他 `UnsealingLedger` 实现须在此显式接纳（fail closed）。唯一例外是 TEST ONLY
   的 `OosUnsealBudget(ephemeral_unseal_for_tests=True)`：写入指纹（`oos_unseal.ephemeral_unseal_for_tests`，只在为真时出现，既有持久指纹不变）、
   验证阶段摘要（`unseal_ledger`）与每个开封的 G5 状态（`EPHEMERAL_UNSEAL_MARK`）；与持久账本同用、或与 `state_dir` 同用（在写入目录之前，
   `refuse_ephemeral_unseal`）都被拒。
3. **被验证的标的须是两对的成员**（MEDIUM，`SealedDatasetPair._load`）：跨对核对不比较成员 / 排除集合，封存窗口可以有不同的 universe 构成——
   这是有意的（上市状态可以合法变化）。G5 验证的是**同一个单一标的**，已由代码保证：循环单标的（`DatasetIngestStage(symbol=...)`，验证器
   `declared_instruments=(symbol,)`），研究与封存两次 bar 读取都只请求 `symbols=(symbol,)`，`backtest_bars_from_dataset` 拒绝没有行的请求标的
   （`infrastructure/bars/dataset.py` 第 3 步），特征请求须携带该标的全部数据集行。新增显式检查：认领之后、`pair_manifests` 与任何封存 bar 读取之前，
   该标的须在研究 pair 与封存 pair 的价格视图成员中（按 `DegradedEpisodeKey` 的 venue / 类型 / Canonical 标的匹配；稳定产品 ID 的 episode 不写标的，
   不能证明 → 拒绝，首片没有稳定 ID，ADR-0029），否则 `SealedDataRefused` → `consumed_without_result:sealed_data_refused`。`pair_manifests` 已证明
   每对内部特征成员 = 价格成员，故价格视图代表整对。
4. **验证缓存命中路径的检查 / 使用时差**（LOW，`infrastructure/bars/verified.py`，只改文档）：命中时比较一次 head 后返回；比较之后并发前进的
   head 不被这次命中观察到（下一次加载会看到并 miss）。返回的 manifest 仍正确：内容由哈希固定，证明所读的 snapshot 均被钉住（不可变表状态），
   等同于写入者在一次普通 `load_manifest` 返回之后才提交。
5. **测试**：`tests/infrastructure/e2e/test_research_loop_real_data.py`——`LoadRecorder`（与 G5 e2e 共用，移到此处）记录 builder 的每次 manifest
   请求、验证型加载与数据集 bar 读取：只扣留的封存 manifest 从不被请求、加载或读 bar（带缓存首跑与无缓存重跑都检查），研究 pair 被读；缓存计数
   (6, 4, 4, 0)；摘要断言声明（哈希、窗口、`sealed_bars_withheld is None`、无 `unused_bars`）。`..._g5.py`——开封前封存对不被请求、封存 bar 在认领后
   读一次；新增「标的须是两对成员」（研究侧 / 封存侧各一次：认领后被拒、封存 bar 零读取、窗口已消耗）；内存循环带 TEST ONLY 标志。
   `..._dataset_g5_units.py`——`WithheldSealedWindow` 只持有声明；成员匹配（稳定 ID、其他 venue、缺失均拒绝）。`tests/research/loop/test_loop_e2e.py`——
   无持久账本的开封预算被拒、显式持久账本可运行且无测试标记、开封前账本被换成内存账本 → 验证阶段失败且不开封、标志进入指纹 / 摘要 / G5 状态、
   与持久账本或 `state_dir` 同用被拒（目录未创建）、非布尔被拒；摄取之后的阶段在只暴露 `RoundData` 成员的代理上跑完整一轮（含 G5），封存 bar 在认领后
   只释放一次，记录哈希与无代理运行相同；源码扫描确认 `IngestStage` 之外不访问 `market` / `markets`。原内存 G5 测试改用 TEST ONLY 标志并断言其可见，
   `test_loop_durable.py` 的不中断对照改为显式持久账本（与重启运行的记录哈希比较不变），新增持久重启后不再开封。

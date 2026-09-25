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

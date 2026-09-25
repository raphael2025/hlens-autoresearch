# research/loop

Phase 11 持续研究循环的**研究侧**（[ADR-0049](../../docs/adr/0049-continuous-research-loop.md)，含 2026-09-25 W2、review fixes 2 与累计验证窗口实施说明）。状态：FRAMEWORK_IMPLEMENTED / NOT_VALIDATED。

通用机制（调度、预算、生命周期护栏、审计、事件发布）在 `apps/worker/loop.py`；本目录只提供阶段实现与组合根，
依赖方向 research → apps/worker（反向禁止）。一轮：`ingest → state → hypothesis → [evolution] → experiment → validation → memory`
（`evolution` 可选，位置固定）。

| 文件 | 内容 |
|---|---|
| `segment.py` | `Segment`（本轮数据：**累计研究数据**——截至 `as_of` 摄取的全部研究窗口 bar，每个摄取市场一个 `ResearchPiece`——+ 本轮被扣留的封存段 `SealedBars`，只凭 vault 发出的一次性 `SealedEvaluation` 释放——该凭据在释放前已把该族的唯一评估记为消耗）、决策网格、合成 bar → `FeatureObservation`、分块 F4 特征运行、`trial_point`（假设条件 `strategy = name@version` / `param k = v`，其他条件一律拒绝）、`decimal_text`（进入哈希记录的浮点先转固定量化的 Decimal 文本） |
| `stages.py` | `IngestStage`（新市场接续上一轮价格路径；按 Profile 固定日历切分：研究窗口 bar 并入累计研究数据，封存 bar 扣留）、`StateStage`（F4 `bar_log_return` 经 `run_feature` → Phase 2 `StateProvider` 经 `run_state`，都在累计研究数据上；同一特征值经 `signals_from_features` 成为策略信号；按研究段缓存特征）、`HypothesisStage`（知识假设 + 已人工审阅的 LLM 草稿预登记；仍开放的假设在数据增长后作为新 trial 重新登记（`reevaluation_candidates`）；新草稿只入审阅队列）、`MemoryStage`（出错 → FAILED、FAIL → REJECTED，均写 FailureRecord；样本内 PASS → OOS——OOS 表示「正在经过 / 有资格进入封存样本外检验」，不是「已通过 OOS」，证据为样本内报告；封存 OOS 失败 → REJECTED，G5 未运行 / INCONCLUSIVE / PASS 均留在 OOS；INCONCLUSIVE 留在 VALIDATION） |
| `trials.py` | `ExperimentStage`（先核对已在 TrialLedger 预登记（重新评估按 attempt 核对），在累计研究数据上，再生成 06-experiment.md §2 复现元组的 `ExperimentSpec` / `ExperimentRun`，经 `CandidateTrialRunner` 跑策略 → 风控 → 回测，按决策期把收益归到 Phase 2 状态上做 Phase 6 矩阵）、`ValidationStage`（`PipelineBacktestValidator` G0 – G4；G5 仅在显式 `OosUnsealBudget` 列出该族、样本内 PASS、本轮有封存段且该族未开封时运行；开封后先 `claim_evaluation` 原子消耗唯一评估，提前结束或出错 → INCONCLUSIVE `consumed_without_result`，窗口永久关闭）、`TrialComponents`、`OosUnsealBudget`（全局次数 + `approved_families`：族 → 批准人） |
| `evolution.py` | `EvolutionStage` / `EvolutionPlan`：从更早轮次未被否证（按各假设最近一次验证：PASS / INCONCLUSIVE）的最佳候选出发 `mutate`，`require_new_version` 与目录防覆盖，`LineageGraph` 可追溯；后代作为新假设先登记、IDEA → CANDIDATE、本轮在累计研究数据上重新验证，不继承父代结论 |
| `memory.py` | `ResearchMemory`（TrialLedger、ReviewQueue、FailureRegistry、策略目录、试验 / 验证记录、谱系、封存开封账本、摄取市场与累计研究数据）；`ReviewQueue.approve` 要求非空且非自动化身份（非循环自身 actor、非 `research_loop:` 前缀），并记录审批 |
| `compose.py` | `SyntheticLoopConfig` + `LoopWiring` + `build_synthetic_loop`：研究侧组合根，所有数字来自配置 |

要点：

- 每次试验（知识假设、审阅后的 LLM 草稿、进化后代）在运行**前**登记为一个 trial；G3 / G4 使用该族累计 trial 数
  （含失败，Constitution C-T1）。出错与被否证的试验都写入记录，从不丢弃。
- **累计验证窗口**（ADR-0049 accumulated validation window 实施说明，R25）：实验与验证在截至本轮 `as_of` 的全部研究窗口数据上进行，
  因为 Profile 的 walk-forward 覆盖整个研究窗口（只用新段时 G4 walk-forward 结构性 INCONCLUSIVE）。反复评估同一数据的代价用 trial
  计：每个（假设，轮次）评估都是 TrialLedger 中单独预登记的 trial（`register_reevaluation`，attempt `loop_round:<loop>:<round>`），
  族 trial 数随之增长，G3 的多重检验校正随之加强。只有 VALIDATION 中最近报告为 INCONCLUSIVE、且数据比上次评估更长的假设才会被
  重新评估（每轮至多 `max_reevaluations_per_round` 个）；REJECTED / FAILED 永不重新评估，OOS 不再做样本内重跑。封存窗口永不进入研究数据。
- 验证阈值只来自绑定的 Validation Profile 或显式 `RobustnessParams`（测试用 TEST ONLY 数值）。
- 生命周期最多到 OOS；OOS → PAPER 需要人工批准，循环在结构上无法产生 PAPER / ACTIVE。
- 封存 OOS 默认永不开封；只有显式 `OosUnsealBudget`（全局次数 + 逐族批准人名单，自动化身份被拒）列出的族才开封，每族一次；
  开封即消耗该族唯一的一次评估（即使之后没有结果），之后无人能再读该窗口。
- 预算：完成的阶段按逐维度 max(声明, 报告) 计费（`apps/worker/loop.py`，`StageRecord.charged`），少报不能拉长预算。
- 记录哈希不含墙钟时间：报告 / 元数据 / 失败记录均以本轮计划时刻盖章；浮点以固定量化 Decimal 文本写入。
- 合成市场的植入真值不作为输入，只在审计摘要中计数；合成结果不支持真实市场结论。

- 审计持久化与实测算力在机制侧（`apps/worker`，ADR-0049 实施说明 2026-09-26）：`LoopAuditLog(path)` 可选落盘并在重启时续跑；
  阶段实测时间只进入不入哈希的指标（`ResearchLoop.metrics`），因此本目录阶段的记录哈希不变。本组合根尚未接持久审计：
  `ResearchMemory`（TrialLedger 等）在此组合中仍在内存，只恢复审计不恢复研究记忆会不一致。

未完成（调试批次）：研究侧组合接持久审计（需要同时持久化 `ResearchMemory`）、NATS、研究仪表盘；滚动循环与固定日历 Profile 的配合（研究窗外的数据不被使用，
换窗口需要新 Profile；累计研究数据在覆盖整个研究窗口之前，G4 walk-forward 仍为 INCONCLUSIVE——这是正确行为）；
封存 bar 只取本轮段内的（跨轮累计封存数据未做）；`matrix_from_backtest` 的逐 bar 归因需要逐 bar 状态（本循环按决策期归因）；
验证阶段的技术失败（`VALIDATION → FAILED` 不是 ADR-0006 的边）只记 FailureRecord、生命周期不动。

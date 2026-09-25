# research/loop

Phase 11 持续研究循环的**研究侧**（[ADR-0049](../../docs/adr/0049-continuous-research-loop.md)，含 2026-09-25 W2 实施说明）。状态：FRAMEWORK_IMPLEMENTED / NOT_VALIDATED。

通用机制（调度、预算、生命周期护栏、审计、事件发布）在 `apps/worker/loop.py`；本目录只提供阶段实现与组合根，
依赖方向 research → apps/worker（反向禁止）。一轮：`ingest → state → hypothesis → [evolution] → experiment → validation → memory`
（`evolution` 可选，位置固定）。

| 文件 | 内容 |
|---|---|
| `segment.py` | `Segment`（本轮新数据：研究段 + 被扣留的封存段 `SealedBars`，只对 vault 已开封的族释放）、决策网格、合成 bar → `FeatureObservation`、分块 F4 特征运行、`trial_point`（假设条件 `strategy = name@version` / `param k = v`，其他条件一律拒绝）、`decimal_text`（进入哈希记录的浮点先转固定量化的 Decimal 文本） |
| `stages.py` | `IngestStage`（按 Profile 固定日历切分研究 / 封存）、`StateStage`（F4 `bar_log_return` 经 `run_feature` → Phase 2 `StateProvider` 经 `run_state`；同一特征值经 `signals_from_features` 成为策略信号）、`HypothesisStage`（知识假设 + 已人工审阅的 LLM 草稿预登记；新草稿只入审阅队列）、`MemoryStage`（出错 → FAILED、FAIL → REJECTED，均写 FailureRecord；样本内 PASS → OOS；封存 OOS 失败 → REJECTED；INCONCLUSIVE 留在 VALIDATION） |
| `trials.py` | `ExperimentStage`（先核对已在 TrialLedger 预登记，再生成 06-experiment.md §2 复现元组的 `ExperimentSpec` / `ExperimentRun`，经 `CandidateTrialRunner` 跑策略 → 风控 → 回测，按决策期把收益归到 Phase 2 状态上做 Phase 6 矩阵）、`ValidationStage`（`PipelineBacktestValidator` G0 – G4；G5 仅在显式 `OosUnsealBudget` 下、样本内 PASS、本轮有封存段且该族未开封时运行）、`TrialComponents`、`OosUnsealBudget` |
| `evolution.py` | `EvolutionStage` / `EvolutionPlan`：从更早轮次未被否证（PASS / INCONCLUSIVE）的最佳候选出发 `mutate`，`require_new_version` 与目录防覆盖，`LineageGraph` 可追溯；后代作为新假设先登记、IDEA → CANDIDATE、本轮在新数据上重新验证，不继承父代结论 |
| `memory.py` | `ResearchMemory`（TrialLedger、ReviewQueue、FailureRegistry、策略目录、试验 / 验证记录、谱系、封存开封账本）；`ReviewQueue.approve` 要求非空且非自动化身份（非循环自身 actor、非 `research_loop:` 前缀），并记录审批 |
| `compose.py` | `SyntheticLoopConfig` + `LoopWiring` + `build_synthetic_loop`：研究侧组合根，所有数字来自配置 |

要点：

- 每次试验（知识假设、审阅后的 LLM 草稿、进化后代）在运行**前**登记为一个 trial；G3 / G4 使用该族累计 trial 数
  （含失败，Constitution C-T1）。出错与被否证的试验都写入记录，从不丢弃。
- 验证阈值只来自绑定的 Validation Profile 或显式 `RobustnessParams`（测试用 TEST ONLY 数值）。
- 生命周期最多到 OOS；OOS → PAPER 需要人工批准，循环在结构上无法产生 PAPER / ACTIVE。
- 封存 OOS 默认永不开封；只有显式 `OosUnsealBudget`（全局次数 + 批准人，自动化身份被拒）才开封，每族一次。
- 记录哈希不含墙钟时间：报告 / 元数据 / 失败记录均以本轮计划时刻盖章；浮点以固定量化 Decimal 文本写入。
- 合成市场的植入真值不作为输入，只在审计摘要中计数；合成结果不支持真实市场结论。

未完成（调试批次）：持久化审计与 NATS、研究仪表盘；滚动循环与固定日历 Profile 的配合（研究窗外的数据不被使用，
换窗口需要新 Profile）；`matrix_from_backtest` 的逐 bar 归因需要逐 bar 状态（本循环按决策期归因）；
验证阶段的技术失败（`VALIDATION → FAILED` 不是 ADR-0006 的边）只记 FailureRecord、生命周期不动。

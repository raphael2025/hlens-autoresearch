# research/loop

Phase 11 持续研究循环的**研究侧**（[ADR-0049](../../docs/adr/0049-continuous-research-loop.md)，含 2026-09-25 W2、review fixes 2、累计验证窗口与 2026-09-26 dataset-backed loop 实施说明）。状态：FRAMEWORK_IMPLEMENTED / NOT_VALIDATED。

通用机制（调度、预算、生命周期护栏、审计、事件发布）在 `apps/worker/loop.py`；本目录只提供阶段实现与组合根，
依赖方向 research → apps/worker（反向禁止）。一轮：`ingest → state → hypothesis → [evolution] → experiment → validation → memory`
（`evolution` 可选，位置固定）。
`ingest` 是可插拔的**轮次数据源**：合成市场（`IngestStage`）或经验证的 Research Dataset manifest（`DatasetIngestStage`）；
其后各阶段只经 `segment.RoundData` 协议读本轮数据，两种来源共用同一组合（`compose.compose_loop`）。

**批次中途失败**（[ADR-0070](../../docs/adr/0070-p7-partial-experiment-fail-stop.md)）：如果最后持久化 round 的 `experiment` stage 为 `FAILED`，worker 暴露 `recovery_required` 并阻止同一 audit 自动续跑，避免重复已记账的 trial。`recovery_review.py` 按 [ADR-0071](../../docs/adr/0071-p7-failed-round-review-packet.md) 从当前已打开、持锁的 `DurableState` 投影最后失败轮的 record、checkpoint 与 TrialLedger 原始 journal 区间，供人工核查；它不打开状态目录、不写盘、不恢复或重试。逐 trial outcome、精确失败序号、完整 traceback、audit journal envelope hash 和外部 Provider 状态未持久化，packet 会明确标出这些缺口。完整 per-trial 自动恢复与人工修复工具尚未实现。

**控制台展示**：`research/reports/loop.py` 可将循环轮次写为 `research_loop_round` 报告；只读 API 提供这些报告，Web 的 Research Loop 页面读取并展示轮次状态、阶段问题、预算用量与累计用量图表。页面是报告浏览器，不会触发或控制循环运行（见 [apps/web/README.md](../../apps/web/README.md) 与 [apps/api/README.md](../../apps/api/README.md)）。

| 文件 | 内容 |
|---|---|
| `segment.py` | `RoundData` 协议（摄取之后各阶段读本轮数据的唯一入口：研究 bar、决策网格、扣留的封存段、特征运行、复现快照、验证器绑定）；`Segment`（合成实现，本轮数据：**累计研究数据**——截至 `as_of` 摄取的全部研究窗口 bar，每个摄取市场一个 `ResearchPiece`——+ 本轮被扣留的封存段 `SealedBars`，只凭 vault 发出的一次性 `SealedEvaluation` 释放——该凭据在释放前已把该族的唯一评估记为消耗）、决策网格、合成 bar → `FeatureObservation`、分块 F4 特征运行、`trial_point`（假设条件 `strategy = name@version` / `param k = v`，其他条件一律拒绝）、`decimal_text`（进入哈希记录的浮点先转固定量化的 Decimal 文本） |
| `stages.py` | `IngestStage`（新市场接续上一轮价格路径；按 Profile 固定日历切分：研究窗口 bar 并入累计研究数据，封存 bar 扣留）、`StateStage`（F4 `bar_log_return` 经 `run_feature` → Phase 2 `StateProvider` 经 `run_state`，都在累计研究数据上；同一特征值经 `signals_from_features` 成为策略信号；按研究段缓存特征）、`HypothesisStage`（知识假设 + 已人工审阅的 LLM 草稿预登记；仍开放的假设在数据增长后作为新 trial 重新登记（`reevaluation_candidates`）；新草稿只入审阅队列）、`MemoryStage`（出错 → FAILED、FAIL → REJECTED，均写 FailureRecord；样本内 PASS → OOS——OOS 表示「正在经过 / 有资格进入封存样本外检验」，不是「已通过 OOS」，证据为样本内报告；封存 OOS 失败 → REJECTED，G5 未运行 / INCONCLUSIVE / PASS 均留在 OOS；INCONCLUSIVE 留在 VALIDATION） |
| `trials.py` | `ExperimentStage`（先核对已在 TrialLedger 预登记（重新评估按 attempt 核对），在累计研究数据上，再生成 06-experiment.md §2 复现元组的 `ExperimentSpec` / `ExperimentRun`，经 `CandidateTrialRunner` 跑策略 → 风控 → 回测，按决策期把收益归到 Phase 2 状态上做 Phase 6 矩阵）、`ValidationStage`（`PipelineBacktestValidator` G0 – G4；G5 仅在显式 `OosUnsealBudget` 列出该族、样本内 PASS、本轮有封存段且该族未开封时运行；开封后先 `claim_evaluation` 原子消耗唯一评估，提前结束或出错 → INCONCLUSIVE `consumed_without_result`，窗口永久关闭）、`TrialComponents`、`OosUnsealBudget`（全局次数 + `approved_families`：族 → 批准人）、`ConditionalPlan`（opt-in：矩阵全部单元预登记为 trial，见下「条件化假设」）。ADR-0100 修订 2（2026-09-30）：每个有策略的运行在 `repro.params` 的保留键 `hlens.p11.inputs@1.0.0` 记录决策网格、初始权益与验证输入（族试验计数、验证 seed、多 seed 对照、`cscv_partitions`、`impact_coefficient`、状态标注器身份；`research/experiments/run_inputs.py`）；族计数要等本轮全部 trial 登记完单元后才确定，因此先执行全部 trial、再生成各自的 Spec / Run；`ValidationStage` 回读记录、与自身取值不一致即本阶段失败，并把记录值交给验证器；实验行的 `params` 仍是策略参数，另有 `run_inputs` |
| `evolution.py` | `EvolutionStage` / `EvolutionPlan`：从更早轮次未被否证（按各假设最近一次验证：PASS / INCONCLUSIVE）的最佳候选出发 `mutate`，`require_new_version` 与目录防覆盖，`LineageGraph` 可追溯；后代作为新假设先登记、IDEA → CANDIDATE、本轮在累计研究数据上重新验证，不继承父代结论 |
| `memory.py` | `ResearchMemory`（TrialLedger、ReviewQueue、FailureRegistry、策略目录、试验 / 验证记录、谱系、封存开封账本、摄取市场（及其生成规格）与累计研究数据）；`ReviewQueue.approve` 要求非空且非自动化身份（非循环自身 actor、非 `research_loop:` 前缀），并记录审批；`ReviewQueue(path)` 把入队 / 审批 / 取用逐行写入哈希链日志，重放时重新核验（自动化身份的审批、草稿或调用哈希不符的审批 → `JournalCorrupted`）；`ReviewQueue.observe(ReviewObserver)` 绑定唯一观察者（持久状态目录：审批前拒绝轮中审批、审批后立即写轮间检查点并移动锚点） |
| `compose.py` | `compose_loop` / `compose_durable`（两种数据源共用的组合：同一组阶段、预算、护栏、审计、持久钩子与自动持久总线）+ `LoopSettings` / `settings_fingerprint`；`SyntheticLoopConfig` + `LoopWiring` + `build_synthetic_loop`：合成组合根，所有数字来自配置；`open_synthetic_loop(config, state_dir=...)` → `DurableLoop(loop, memory, state_dir, bus, owned_bus)`（`build_synthetic_loop(..., state_dir=...)` 等价，只返回 loop）；不给 `bus` 时自动使用 `state_dir/bus` 并与审计交叉核对（`check_round_bus`） |
| `dataset_source.py` | `DatasetIngestStage` / `DatasetRound` / `DatasetSegment` / `DatasetCatalog`：每轮从声明的 manifest 读数据；`SealedDatasetPair`（封存 manifest 对，只在开封认领后读取）/ `WithheldSealedWindow`（只扣留声明：只记录哈希与 Profile 封存窗口，从不读取；见下「数据集组合」） |
| `dataset_compose.py` | `DatasetLoopConfig` + `build_dataset_loop` / `open_dataset_loop` / `dataset_loop_fingerprint`：数据集组合根 |
| `durable.py` | 一个状态目录承载整个循环（见下）：`open_state`、`MemoryCheckpoint`（每轮一条记忆检查点）、交叉校验、`LoopStateInconsistent`；可选外部锚点 `StateAnchor` / `FileAnchor` / `StateHead` |
| `recovery_review.py` | `failed_round_review_packet(DurableState)`：只对最后记录的 failed experiment stage 生成纯内存、hash-bound 证据投影；不打开目录、不触发写路径，不等于恢复操作 |
| `p7_plan.py` / `p7_admission.py` | ADR-0103 D2 / D3（默认关闭）：`LoopWiring.p7_plans`（可选 `P7PlanSource`，默认 `None`）（声明的计划、`P7ExecutionSwitch`、allowlist；仅在有计划准入日志的持久状态 v4 / v5 / v6 上组合，并进入指纹 `p7_plans`）。每轮在第一次 journal 写入前，hypothesis 阶段对第一个待处理计划执行：纯检查（横截面节点 → `cross_sectional_loop_unsupported`、编译、根策略与 Provider、产生绑定、`validate_complete_experiment_bindings`、`cross_check_admission_evidence`、非 LLM / 本族 / 可运行 / 新身份）→ `plan_admission()` PREPARE → complete → 以 COMMIT 为证明的 `p7_strategy_candidates`；假设作为本轮 trial（origin `p7_plan`，在 `registered` 最前）。纯检查阶段的拒绝不写任何字节、不登记 trial，摘要记 `p7_plan_rejection{plan_hash, code, where}` 与累计 `rejected_plans`。已知限制：准入过 P7 计划的目录重开时被拒绝（P7 候选的重建未定义） |
| `replacement.py` | 可选的循环内替换提案触发（ADR-0100 第 7 项，默认关闭）：`ReplacementTrigger`（显式 `enabled=True`、`every_rounds`、预登记的独立密封窗口、调用方的 `source` 与 `ProposalLedger`）、`ReplacementInputs`、`ReplacementTriggerStage`（包装 `EvolutionStage`，同名 `evolution`）；见下「循环内替换提案触发」 |

要点：

- 每次试验（知识假设、审阅后的 LLM 草稿、进化后代）在运行**前**登记为一个 trial；G3 / G4 使用该族累计 trial 数
  （含失败，Constitution C-T1）。出错与被否证的试验都写入记录，从不丢弃。
- **累计验证窗口**（ADR-0049 accumulated validation window 实施说明，R25）：实验与验证在截至本轮 `as_of` 的全部研究窗口数据上进行，
  因为 Profile 的 walk-forward 覆盖整个研究窗口（只用新段时 G4 walk-forward 结构性 INCONCLUSIVE）。反复评估同一数据的代价用 trial
  计：每个（假设，轮次）评估都是 TrialLedger 中单独预登记的 trial（`register_reevaluation`，attempt `loop_round:<loop>:<round>`），
  族 trial 数随之增长，G3 的多重检验校正随之加强。只有 VALIDATION 中最近报告为 INCONCLUSIVE、且数据比上次评估更长的假设才会被
  重新评估（每轮至多 `max_reevaluations_per_round` 个）；REJECTED / FAILED 永不重新评估，OOS 不再做样本内重跑。封存窗口永不进入研究数据。
- 验证阈值只来自绑定的 Validation Profile 或显式 `RobustnessParams`（测试用 TEST ONLY 数值）。
- **C-T4 市场基准（ADR-0060 在循环中强制，2026-09-26，CODE_COMPLETE / DEBUG_PENDING）**：`ValidationStage` 以 `market_benchmark=True` 构造
  `ValidatorSetup`，每个试验报告都带绑定 Profile 的 `benchmark.market_benchmark_rule` / `.inverse_control_reported` 所要求的报告项
  （`G2.market_benchmark.<规则>` / `G2.inverse_control`，只报告、无阈值）。未登记的规则名 → `G2.market_benchmark` INCONCLUSIVE，
  与其他 INCONCLUSIVE 门一样使报告 INCONCLUSIVE；基准重跑用验证器声明的回测器或 `BarBacktester()`，必须逐字节复现试验的
  `result_hash`，否则该项 INCONCLUSIVE。基准与反向对照是同一试验的回测，不增加 trial。TEST ONLY 夹具 Profile 使用
  `buy_and_hold_equal_weight` + `inverse_control_reported=True`。
- 生命周期最多到 OOS；OOS → PAPER 需要人工批准，循环在结构上无法产生 PAPER / ACTIVE。
  替换提案默认不在循环内：`research/evolution/replacement_job.py` 是调用方显式运行的作业，只读循环的 `lineage.jsonl`（ADR-0045 实施说明 2026-09-26）；
  可选的循环内触发见下「循环内替换提案触发」（ADR-0100 第 7 项，默认关闭）。
- 封存 OOS 默认永不开封；只有显式 `OosUnsealBudget`（全局次数 + 逐族批准人名单，自动化身份被拒）列出的族才开封，每族一次；
  开封即消耗该族唯一的一次评估（即使之后没有结果），之后无人能再读该窗口。
  **开封账本必须持久**（review fixes 4）：`OosUnsealBudget` 只与 `DurableUnsealingLedger`（`state_dir` 的 `sealed_oos.jsonl`，或显式传入
  `ResearchMemory(oos_ledger=DurableUnsealingLedger(path))`）一起被接受；内存账本重启即忘记开封记录、可能让同一族再开封，`ValidationStage`
  拒绝该组合。唯一例外是 TEST ONLY 的 `OosUnsealBudget(..., ephemeral_unseal_for_tests=True)`：写入指纹、验证阶段摘要
  （`unseal_ledger`）与每个开封的 G5 状态（`EPHEMERAL_UNSEAL_MARK`），不会被误认为真实使用；与持久账本或 `state_dir` 同用被拒。
- 合成路径的封存 bar 是生成出来的（`IngestStage` 生成整段市场再按日历切分）：生成不是读取真实封存数据，没有真实 OOS 泄漏。
  摄取之后的阶段只通过 `RoundData` 协议读本轮数据——协议不暴露市场对象，封存 bar 只经 `sealed.release`（凭已认领的评估）交出；
  `test_stages_after_the_ingest_see_only_the_round_data_protocol` 用只暴露协议成员的代理跑完整循环（含 G5）并比对记录哈希，
  另有源码扫描确认摄取之外的阶段从不访问 `market` / `markets`。
- 预算：完成的阶段按逐维度 max(声明, 报告) 计费（`apps/worker/loop.py`，`StageRecord.charged`），少报不能拉长预算。
- 记录哈希不含墙钟时间：报告 / 元数据 / 失败记录均以本轮计划时刻盖章；浮点以固定量化 Decimal 文本写入。
- 合成市场的植入真值不作为输入，只在审计摘要中计数；合成结果不支持真实市场结论。

- 审计持久化与实测算力在机制侧（`apps/worker`，ADR-0049 实施说明 2026-09-26）：`LoopAuditLog(path)` 可选落盘并在重启时续跑；
  阶段实测时间只进入不入哈希的指标（`ResearchLoop.metrics`），因此本目录阶段的记录哈希不变。

## 持久组合：一个状态目录（ADR-0049 实施说明 durable composition，2026-09-26）

`open_synthetic_loop(config, state_dir=...)` 把循环的全部状态放在一个目录；不给 `state_dir` 时行为不变（全在内存，记录哈希相同）。

| 文件 | 内容 |
|---|---|
| `audit.jsonl` | `LoopAuditLog`：每轮 started / recorded |
| `memory.jsonl` | 头行 `loop_state_opened`（配置指纹）+ 每个已记录轮次一条 `round_memory` 检查点 + 每次轮间人工审批一条 `between_rounds` 检查点；v4 / v5 还可记录 `plan_admission` 检查点 |
| `trial_ledger.jsonl` / `sealed_oos.jsonl` / `lineage.jsonl` / `reviews.jsonl` | TrialLedger、开封账本、谱系、审阅队列（均为哈希链日志） |
| `plan_admission.jsonl` | v4 / v5 必需；首行固定绑定对应 state version 的 header，后续只允许 PREPARE / COMMIT，并与 trial ledger、memory checkpoint、anchor 交叉核对 |
| `failures.jsonl` | FailureRegistry（只追加、fsync，无链） |

- **检查点**：`ResearchLoop(checkpoint=...)` 在审计记录一轮之前写入该轮检查点：`record_hash`、其余每个文件的位置（日志：行数 + 链头；
  失败登记：条数 + 摘要），以及本轮对 `ResearchMemory` 的增量（市场规格——重启时重新生成并须复现 `market_hash`；研究段；新增策略；
  试验 / 验证记录（pydantic 记录，不含内存中的 `inputs` / `trial` 运行产物，保留 `knowledge_cutoff`）；实验 / 状态 / 后代摘要）。
- **重新打开的交叉校验**（任一不符 → `LoopStateInconsistent`，从不修复）：配置指纹（含预算与开封预算）一致；无中断轮次（只 started 未 recorded → 拒绝，须人工审查）；
  每条已记录轮次恰有一条同序号、同 `record_hash` 的检查点；每个检查点（轮次或轮间）记录的每个文件位置都存在（同行号同链哈希）、单调不回退，
  最后一个检查点即文件末尾（`reviews.jsonl` 也不例外）；每条轮间检查点紧跟其轮次（轮数与审计头一致）、只让 `reviews.jsonl` 前进恰好它所指的那一条
  审批、其余文件不动；`reviews.jsonl` 中每一条审批行都有轮间检查点指向它；每轮增量等于审计哈希记录中的阶段摘要（市场、状态摘要、实验行 = 试验摘要、验证报告行、
  后代行），记录复现各自内容哈希；审计里登记 / 重新评估的假设都在 TrialLedger，后代及其父代在谱系且规格哈希一致，每条 `human_review:<who>`
  证据在审阅队列中有该人对该草稿的审批且已取用，开封 / 已消耗的 G5 在开封账本中有同一批准人且已评估，审计列出的失败记录都在失败登记中；
  护栏重放后每个生命周期对象都是已登记假设。
- **尾部截断**：单个文件删去整行尾部仍是合法的短链，但其余文件记录了它的位置（或审计与检查点不再一一对应），因此被跨文件校验发现。
- **预算绑定目录**（ADR-0049 实施说明 durable review fixes，2026-09-26）：配置指纹包含 `LoopBudget` 与完整的 `OosUnsealBudget`
  （`max_unsealings`、获准族及批准人；TEST ONLY 的 `ephemeral_unseal_for_tests` 仅在为真时出现）以及精确节奏（`cadence_microseconds`）；用任何不同的预算（更大、更小、多一个获准族、换批准人）重新打开都拒绝，
  消息写明哪个预算不同。**提高预算是人的决定：用新的 `state_dir` 或新的 `loop_id`。** state v3 保留原 checkpoint 形状和旧 loop 行为；普通新目录默认采用 v4。v4 的 plan journal header 精确绑定 schema `1.0.0`、loop id 与 `state_version: 4`。v3 不自动迁移，且不允许 typed-plan admission。
- **Operator 身份基础（ADR-0074，v5）**：`open_synthetic_loop(..., operator_identity=<lowercase SHA-256>)` 是当前选择 operator state v5 的入口；身份同时写入 v5 memory fingerprint，v5 plan journal header 固定写 `state_version: 5`。缺失、不规范或重开不匹配均 fail closed；普通 loop 不带此参数，仍使用 v4，原 v3 / v4 fingerprint 字节不变。v5 不接管或迁移旧目录。此 API 只建立持久身份边界；尚无 operator 调用路径、配置解析器、provider registry 或 CLI，六类 DSL 仍不可运行。
- **ADR-0073 admission 恢复**：v4 在同一 state lock 下协调 PREPARE → TrialLedger 单一 batch event → COMMIT → memory admission checkpoint → external anchor。重开时仅按持久化内容/hash 精确补齐唯一事务缺口；pending PREPARE 只能绑定 audit 中唯一的当前 started round，任何孤儿、额外尾部、身份不符或分叉都 fail closed。恢复不运行 compiler、provider、runner 或 experiment；open/failed round 仍拒绝自动续跑，六类 operator 仍不可运行。Hypothesis batch 以 `(family_id, name, version, content_hash)` 排序。该批仅实现 durable recovery，不接 producer；ExperimentSpec 一对一引用 / lowering 对应关系须由后续 composition 在 PREPARE 前验证。
  写入侧与重开校验对称（2026-09-28 加固）：PREPARE 前拒绝 v3、未持锁、非唯一当前 open round、最后已记录轮 experiment FAILED（ADR-0070 recovery required）、同一 round 第二次 admission、仍有未完成事务、TrialLedger 已含同 `name@version`，以及任何日志 / failure registry 已离开 memory 最后一行记录的位置（因此 admission 须在本轮其他写入之前）；PREPARE 未完成或 COMMIT 缺 admission checkpoint 时拒绝写 round / between-rounds checkpoint 和人工审批；admission checkpoint 只允许移动 TrialLedger 到该 batch event、plan journal 到该 COMMIT。进程内唯一的 admission 入口是租约作用域：`with state.plan_admission() as lease: lease.prepare(...); lease.complete()`，租约覆盖 PREPARE → TrialLedger batch → COMMIT → admission checkpoint → anchor 整段；原 `prepare_plan_admission` / `complete_plan_admission` 两个独立公开调用已移除（`state.lock` 仍只排除其他进程）。租约期间 TrialLedger 持有写租约，`register` / `register_draft` / `register_batch` / `register_reevaluation` 的新写入在任何线程（含持有者自身）一律拒绝，只有租约内的 `recover_register_batch(..., lease=...)` 可写；ADR-0096 允许完全相同的 `register` 身份/内容及同一 `register_reevaluation` attempt 在 gate 前只读返回 `False`，不写 journal、不增加 trial，且 LLM-origin 检查仍先于去重；round / between-rounds checkpoint、人工审批、anchor 前移与第二个租约同样拒绝（`LoopStateLocked`）。作用域若在写入任何 admission 字节后因异常、中断或未调用 `complete()` 而结束，进程内锁照常释放，但该 `DurableState` 与其 TrialLedger 从此拒绝一切写入（`LoopStateInconsistent` / `LedgerError`），日志保持原样，关闭 loop 并重开目录后按 ADR-0073 §4 精确恢复；未写入任何字节的作用域正常释放。并发语义（2026-09-28 审查修复）：租约获取、round begin、round checkpoint → audit 记录 → after-record anchor 整段（`ResearchLoop(round_scope=state.round_scope)`）、人工审批的 `before_approval` → review journal → 内存采纳 → between-rounds checkpoint / anchor 整段，以及 review enqueue / take，全部在同一个 admission gate（进程内 RLock）内串行；审批、enqueue / take、round begin 在写入前检查租约与 poison，不再出现审批已落盘而 checkpoint 被租约拒绝、或 round checkpoint 与 audit 记录之间被租约插入。租约绑定进入作用域的线程，其他线程调用 `prepare` / `complete` 在读写前即拒绝；每次调用独占认领租约并持有 gate，`complete` 在首次写入前一次性认领，失败或重复调用都不会再次执行 batch → COMMIT → checkpoint → anchor；`prepare` 写入部分字节后失败则租约作废，退出作用域即 poison。checkpoint 覆盖的所有 durable store 写入同样先进入该 gate（2026-09-28 复审修复）：`open_state` 以 `bind_write_gate` 绑定 TrialLedger、sealed-OOS unsealing ledger、lineage graph 与 failure registry，review queue 经 `review_scope` 写入；因此任何线程经 `register` / `register_batch` / `register_reevaluation`、`record` / `mark_evaluated`、`add`、`append` 的写入都不会落在 checkpoint 读取位置与追加 memory 行之间、round checkpoint 与 audit 记录之间，或 PREPARE 位置检查与 admission checkpoint 之间；上述区间之外的常规 stage 写入短暂持有 gate 照常运行。租约活跃或 admission 中断后，普通 store 写入一律在写入前拒绝，唯一放行的是租约自身线程以其 TrialLedger 租约调用的 `recover_register_batch(..., lease=...)`。锁序固定为 admission gate → store 锁 → journal 锁，无反向获取。各 store（TrialLedger、ReviewQueue、LineageGraph、DurableUnsealingLedger）不再公开可写 `journal`，跨文件校验只用 `journal_head()` / `journal_snapshot()`（分离只读副本）。复审修复 2（2026-09-28）：store 写入（TrialLedger / sealed-OOS / lineage / failure registry，以及 review enqueue / take）只在 audit 唯一的 open round 内放行，轮间在写入前拒绝（opener 对轮间除审批外的任何尾行永久拒绝）；人工审批仍是唯一的轮间写入，`before_approval` 在审批落盘前还要求所有文件（含 reviews、failures）恰在 memory 最后一行记录的位置。audit 经 `LoopAuditLog.bind_write_scope` 绑定同一 gate：直接调用 `state.audit.begin_round` / `append` 也在 gate 内、写入前检查租约 / poison / 未完成 admission / 已关闭，且 record 要求 memory 最后一行正是该轮 checkpoint。`MemoryCheckpoint` 不再公开可写 `journal`，改为 `journal_head()` / `journal_snapshot()` / `header()`。每个 gate 作用域还要求 `state.lock` 仍被持有；任何释放路径（`DurableLoop.close()`、`StateLock.release()`、opener 失败路径、audit 被回收时的 `release_collected`）都先永久关闭 gate、等其他线程在途作用域结束（本线程在途或回收路径则由最外层作用域退出时释放）后才释放 flock，关闭后保留的 `state.memory` / `loop.memory` / `state.audit` 引用无法再写盘。作用域内只能调用 `prepare` / `complete`。重开时所有只读交叉校验（含各轮内容与生命周期主体登记）先于恢复写入，恢复后的位置复核通过后才前移 anchor，随后仍拒绝未记录的 open round；若最后已记录轮 experiment FAILED，未完成事务不做恢复写入并拒绝。新 v4 / v5 目录先写 plan header 再写 memory header，二者之间崩溃留下的空或仅含精确 header 的 plan journal 会在重开时补齐；state version 须为精确整数 3 / 4 / 5。重开中的 reducer / ledger 拒绝统一为 `LoopStateInconsistent`，单文件链损坏仍为 `JournalCorrupted`。不给 anchor 时限制照旧：连同 round start 在内一致截断整段 admission 尾部时，按较短历史打开。
  二次加固（2026-09-28）：有 open round、pending PREPARE 或缺 checkpoint 的 COMMIT 的 v4 重开（最终必然拒绝）不调用 `provider.generate` / `provider_for`，只按记录的 hash 与 audit 摘要核对各轮（市场能否重新生成、research piece bars 在该路径不证明）；恢复写入前先用 loop actor 的新 `LifecycleGuard` 纯重放全部 audit transition（非法边、状态链、actor、payload）并核对主体登记。给 anchor 时 PREPARE 要求 anchor 已持有当前目录头，因此 admission checkpoint 后、anchor 前崩溃时 anchor 停在 admission 前的头并在恢复后前移；anchor 为空而目录已有 checkpoint 行仍视为丢失 / 替换而拒绝。
- **可选外部锚点**：`open_synthetic_loop(..., anchor=Path | StateAnchor)`。每个已记录轮次之后锚点收到目录的头（轮数、审计头、记忆日志链头、各文件位置）；
  重新打开时目录必须不早于锚点且到该轮为止历史相同，落后（一致截断、目录被删重建）、分叉、或锚点为空而目录已有轮次 → 拒绝；通过后锚点前移。
  `FileAnchor` 必须在目录之外。**不给锚点时限制照旧**：把**所有**文件一致地截回更早的轮次边界是合法的较短历史，可以打开。
- **轮间人工审批**（ADR-0049 实施说明 approvals between rounds，2026-09-26）：`open_state` 把 `DurableState` 绑定为审阅队列的观察者
  （`ReviewQueue.observe` / `ReviewObserver`）。`DurableLoop.memory.reviews.approve(...)` 在轮次运行中被拒绝；审批写入 `reviews.jsonl` 后
  **立即**在 `memory.jsonl` 追加一条 `between_rounds` 检查点（已记录轮数、审计头、各文件位置、所覆盖的审批行 `seq` / `hash` / 草稿 / 审批人），
  并把新头发布给锚点（`StateHead.memory_seq` 加一、`rounds` 不变）。重新打开时：没有轮间检查点指向的审批（伪造追加、轮中审批、审批后检查点前
  进程死亡）→ 拒绝，有无锚点都一样；只删审批行 → 其检查点指向文件末尾之后 → 拒绝；有锚点时连同其检查点一起删（一致截断这一次轮间操作）→
  `reviews.jsonl` 落后于锚点 → 拒绝；审批已写入而检查点失败时，下一轮的检查点拒绝记录该轮（循环停止）。锚点只前进（`memory_seq` 严格增大、
  轮数不减），不后退也不横移。轮间没有别的合法写入：开封审批是配置（指纹中的 `OosUnsealBudget`，即被锚定的头行），开封账本 / TrialLedger /
  谱系 / 失败登记只在轮内增长，轮间多出的行无论有无检查点都拒绝。
  **无锚点时仍无法发现**：把一条审批**连同**其轮间检查点一起删掉（其后没有别的行）是合法的较短历史，打开后等同"尚未审批"；
  **任何锚点都无法发现**：能写目录并遵循格式的人（在 `open_state` 打开的状态上调用 `approve`，或手写同样的两行）可以追加一条带检查点的审批——
  日志是哈希链不是签名，审批人身份只是声明；这需要经过认证的审批通道（不在范围内）。
- LLM 提供者属外部：其自身状态（如脚本化提供者的位置）不是循环状态，由调用方续接。
- **自动持久总线**（ADR-0044 / ADR-0049 实施说明 durable jobs and bus wiring，2026-09-26）：给 `state_dir` 而不给 `bus` 时，组合根自己打开
  `FileEventBus(state_dir / "bus")`（`DurableLoop.bus` / `owned_bus`；`DurableLoop.close()`、`with` 块或丢弃循环对象释放总线锁），并在组合**之前**
  用 `check_round_bus` 把总线与已校验的审计交叉核对：`research_loop.round` 上的消息必须恰好是审计已记录轮次的
  `apps.worker.loop.round_message`（同键、同 `record_hash`、同记录），按顺序。**超前**（消息多于审计轮数，例如审计被回滚）或任何**外来 / 乱序**
  记录 → `LoopStateInconsistent`；**落后恰好最后一轮** → 从审计补发（这是崩溃唯一能留下的状态：循环在审计 fsync 之后才发布，发布失败即停机，
  组合根在任何新轮次之前补齐；消息是已校验记录的纯函数）；**落后两轮及以上**（总线被截断 / 替换 / 从未用于此目录）→ 拒绝，拒绝时不写任何东西。
  同时机制侧在续接审计时确认本循环已记录轮次的未确认轮次任务（崩溃于记录与确认之间）——审计就是它们的持久结果，从不重跑。
  纯内存组合仍必须传 `bus`。
- **总线外部锚点**（2026-09-26，CODE_COMPLETE / DEBUG_PENDING）：`open_synthetic_loop` / `open_dataset_loop` / `compose_durable` 新增可选
  `bus_anchor=<state_dir 之外的路径>`，交给自动总线 `FileEventBus(..., anchor=)`：任何主题（不只审计已覆盖的 `research_loop.round`）的尾部整行删除
  在重开时被拒绝（`BusCorrupted`）；与调用方自带总线同时给出即拒绝，且在触碰 `state_dir` 之前拒绝。不给时行为与记录哈希不变。测试见
  `tests/research/loop/test_loop_durable.py` 末尾两项。
- **调用方自带的总线**（ADR-0049 实施说明 review fixes 3，2026-09-26）：持久模式下调用方传入的总线与自动总线**同样**经 `check_round_bus`
  核对（同样的拒绝、同样只补发最后一轮；拒绝时不写入；关闭仍是调用方的事）。例外是 `InMemoryEventBus`：它**不是持久总线**（新进程里必然为空，
  落后不是截断的证据），审计缺少的**全部**轮次按序补发进去（`check_round_bus(..., durable=False)`），外来 / 乱序 / 超前仍拒绝；
  其他任何总线类型一律按持久核对（fail closed）。
- 回归测试：`tests/research/loop/test_loop_durable.py`（重启 e2e 与不中断运行的审计哈希 / trial 数 / 生命周期 / 封存 OOS 完全相同；
  删除账本、审计超前账本、篡改审批、逐文件尾部截断、一致截断（已记录的限制）、中断轮次、换配置、删检查点文件、篡改检查点增量；
  换预算 / 开封额度 / 获准族 / 批准人被拒绝、相同预算接受、节奏精确；带锚点的重启、一致截断 / 删目录 / 分叉 / 锚点丢失或在目录内被拒绝、锚点不后退；
  轮间审批立即有检查点并移动锚点、最后一轮之后的审批重启后仍在、只删审批被拒、审批连同检查点一起删在有锚点时被拒（无锚点为已记录的限制）、
  无检查点的伪造审批（最后一轮之后 / 早先轮次内）被拒、检查点指向别的审批被拒、轮中审批被拒、检查点失败使下一轮不被记录；
  自动总线的重启与不中断运行相同且总线重放每轮 `record_hash`、审计与总线之间崩溃后补发并续跑相同、总线少一轮补发 / 少两轮或丢失被拒、
  外来记录 / 乱序 / 超前的总线被拒；自带 `FileEventBus` 含外来记录被拒、落后一轮补发 / 两轮被拒，自带内存总线按序补发全部轮次）。

## 数据集组合（ADR-0049 实施说明 dataset-backed loop 与 dataset G5，2026-09-26）

`build_dataset_loop(config, catalog=DatasetCatalog(adapter, storage, builder), bus=..., memory=... | state_dir=...)` /
`open_dataset_loop(...)`：与合成组合相同的阶段、预算、护栏（最多 OOS）、审计、持久状态目录与锚点；只有轮次数据源不同。

- **每轮声明的 manifest**：`DatasetLoopConfig.rounds[i]` = `DatasetRound(feature_manifest_hash, price_manifest_hash, sealed_manifest_hash=None,
  sealed_feature_manifest_hash=None, sealed_price_manifest_hash=None)`（封存窗口二选一：只扣留的点时刻 manifest，或封存 manifest **对**）。
  哈希只是声明：全部经 `DatasetBuilder` 自己的验证型 `ManifestStore` 加载（`load_manifest` / `pair_manifests` /
  `backtest_bars_from_dataset` / `feature_request_from_dataset`），从不按信任接受。循环不构建数据集（数据平面预先构建）。
  可选 `DatasetCatalog(..., manifest_cache=VerifiedManifestCache())`（`infrastructure/bars/verified.py`）：同一 builder、所读 snapshot 均未变时复用
  成功的验证（价格 / 配对 / 特征 / bar / 封存各次加载；`feature_request_from_dataset` 仍每次验证）；默认 `None`，记录哈希有无缓存都相同。
- **截止**：价格 manifest 的视图（= 特征区间终点）晚于本轮 `as_of` → 摄取拒绝；每根 bar、每条特征观测再次核对。
- **研究窗口**：pair 的数据窗口必须在 Profile 研究窗口之内，研究 manifest 从不含封存窗口的行。可选的只扣留封存 manifest（`sealed_manifest_hash`）
  **只记录声明**（review fixes 4）：摘要写 `sealed_manifest_hash` 与 `sealed_window`（Profile 的封存窗口），`WithheldSealedWindow` 不持有任何数据；
  该 manifest 从不加载、其 bar 从不读取也不计数（计数无法不读行而得，故删除：数据集轮次的 `sealed_bars_withheld` 恒为 `None`，`unused_bars` 已移除）；
  永不开封（没有封存窗口的特征）。声明错误的扣留哈希因此不会在摄取时被拒——它同样从不被读取。
- **封存 OOS（G5，dataset G5 实施说明）**：声明了封存 manifest 对的轮次可以运行 G5；`OosUnsealBudget` 只在至少一轮声明了封存对时被接受。
  摄取阶段**完全不读**封存对（`SealedDatasetPair`）；验证阶段在开封前只问 `evaluable`（按声明、Profile 窗口与本轮截止判断：窗口须在
  `as_of` 之前结束，不读存储），先 `claim_evaluation`（把该族唯一评估记为已消耗）再 `release`：经验证型 `ManifestStore` 加载两份封存
  manifest，核对与研究 pair 相同的上游 snapshot、`knowledge_cutoff`、ADR-0032 选择与其余政策绑定、universe、数据集表与 Canonical 表集合，
  数据窗口**恰好**是 Profile 封存窗口、价格视图在 `[窗口终点, as_of]` 内，被验证的标的在**两对**价格视图的成员中（按 `DegradedEpisodeKey`
  的 venue / 类型 / Canonical 标的匹配；稳定产品 ID 的 episode 不写标的，不能证明成员资格 → 拒绝），再 `pair_manifests`、读已证明的封存 bar 与特征观测。任一不符
  → `SealedDataRefused`，G5 报告 INCONCLUSIVE `consumed_without_result:sealed_data_refused`，该族窗口永久关闭（配置错误同样花掉该族的开封）。
  G5 的信号只经 `feature_request_from_dataset` 在封存特征 manifest 上计算；G5 报告带 `G0.manifest_binding`（研究 bar 对研究 pair、封存 bar /
  封存特征请求对封存 pair）。其余保证与合成路径相同：只开封获准族（人类批准人）、样本内 PASS 才开封、每次开封一次评估、提前结束
  `consumed_without_result`、开封账本随状态目录持久（重启不再开封）、开封预算与封存对都在指纹中。跨两对不比较的：完整的成员 / 排除集合
  （封存窗口可以有不同的 universe 构成）、质量报告 id、lineage 修订与证据缺口（窗口不同，数据不同；封存对内部由 `pair_manifests` 核对）。
  G5 验证的是**同一个单一标的**：循环只有一个标的（`symbol`），两次 bar 读取都只请求 `symbols=(symbol,)`，`backtest_bars_from_dataset` 拒绝没有行的
  标的，特征请求必须携带该标的的全部行，且上面的成员检查显式要求它在两对中都是成员。
- **绑定**：验证器拿到已证明的 bar、`ManifestPair` 与每个特征请求的 manifest 哈希 → 每份报告运行 `G0.manifest_binding`；复现元组的数据集快照
  为两份 manifest 的 `DatasetRef`；实验摘要写明 manifest 与 pair 哈希。
- **持久**：不保存摄取记忆（每轮重读声明的 manifest；检查点 `markets` / `research_data` 为空）；指纹绑定标的与每轮全部声明哈希；
  重新打开时每条已记录摄取须读了该轮声明的 manifest。
- 冒烟测试：`tests/infrastructure/e2e/test_research_loop_real_data.py`（`postgres` 标记，测试 catalog；Binance 格式 kline 经真实入库路径，
  两轮、跨封存边界、重跑哈希一致；首轮带验证缓存、重跑不带，记录哈希一致；约 3.5 分钟）。
    G5：`tests/infrastructure/e2e/test_research_loop_real_data_g5.py`（`postgres`，模块级夹具一次入库
  与六次构建；TEST ONLY 宽松 Profile 仅为让样本内 PASS 可达：获准族开封一次且报告带 G5 门与封存对的 `G0.manifest_binding`、开封前没有封存
  manifest 被加载（记录每次验证型加载；夹具第二天 5 小时 = 封存窗口，供 240 根回看预热）、未获准族从不开封也不读封存对、上游 snapshot 不同的封存对在开封后被拒、重启不再开封）；
  无数据库的单元：`tests/infrastructure/e2e/test_research_loop_dataset_g5_units.py`。

未完成（调试批次）：NATS、研究仪表盘；每轮约 6 次验证型 manifest 加载（无缓存；只扣留的封存声明不加载；开封一次再加 6 次）；数据集 G5 的封存特征只来自封存 manifest
（特征请求只能携带该 manifest 的行，没有跨边界的前置 bar），前几次评估不可计算，策略在封存窗口开头需要重新预热（回看 240 根即窗口内约 4 小时
空仓）——保守、不跨边界，但短封存窗口可能以 `consumed_without_result` 结束；滚动循环与固定日历 Profile 的配合（研究窗外的数据不被使用，
换窗口需要新 Profile；累计研究数据在覆盖整个研究窗口之前，G4 walk-forward 仍为 INCONCLUSIVE——这是正确行为）；
封存 bar 只取本轮段内的（跨轮累计封存数据未做）；`matrix_from_backtest` 的逐 bar 归因需要逐 bar 状态（本循环按决策期归因）；
~~验证阶段的技术失败只记 FailureRecord、生命周期不动~~ ✅ ADR-0053（2026-09-26）：`G0.reproducibility` / `G0.signal_determinism` FAIL 或对象自身的运行出错 → `VALIDATION → FAILED`（证据：报告或 Run、FailureRecord 哈希、本轮引用）；基础设施故障与 OOS 中的技术失败仍只记 FailureRecord、生命周期不动（`technical_failures_lifecycle_unchanged`）。

## LLM 调用内容可取回（Phase 7，2026-09-26，CODE_COMPLETE / DEBUG_PENDING）

`research/loop/llm_content.py`：可选的 `ContentVerifiedLLM(inner, resolver)`（`LLMProvider` 包装）。内层提供者作答后，调用的 prompt / input / output 三个引用必须经
`resolver`（如 `infrastructure.content.LocalContentStore`，`verify_llm_call` 逐个重算哈希并核对大小）取回，且取回的载荷必须**等于**实际交换的 prompt、请求 input 与响应 output；
否则 `LlmContentUnverified`（`ValueError` 子类，携带该次 `LlmCall`）——假设阶段与结构不合的草稿一样记 `rejected` / `call_hash` / `call`、从不登记。组合根把 resolver 交给假设阶段：人工审阅过的草稿在被取用时再核对一次，内容在审阅与使用之间消失或变化
→ 该轮假设阶段 FAILED、其后阶段 SKIPPED、草稿不登记（fail closed）。约定：提供者须把每个载荷存为其规范 JSON（`plugins.llm.ScriptedLLMProvider(store=...)` 即如此）。
不用包装时行为与记录哈希不变。测试：`tests/research/loop/test_llm_content.py`。

审计修复（2026-09-26）：
- 假设阶段只捕获 `LlmDraftRejected` 与 `LlmContentUnverified`；提供者的其他错误（含其他 `ValueError`）使该阶段 FAILED（此前被宽泛的 `except ValueError`
  吞掉、只记原因）。空 `llm_prompt` 此前在每轮以 `LlmRequest` 校验错误被记为"被拒"，现在构造 `HypothesisStage` 时即拒绝（配置错误）。
- **校验模式属于状态目录**：`llm` 为 `ContentVerifiedLLM` 时 `open_synthetic_loop` / `open_dataset_loop` 在指纹中加入 `llm_content_verified: true`
  （`llm_content_fingerprint`；未开启时指纹逐字节不变，固定指纹不动）。以校验模式开的目录用普通 LLM 或 `llm=None` 重开被拒（否则已审阅草稿会不经核对被取用），
  反之亦然；`compose_durable` 也按目录头核对其 `llm`（直接 `open_state` + `compose_durable` 的调用方）。测试：`test_llm_content.py`、`test_loop_llm_rejection.py`。


## 条件化假设（Phase 6 进入循环，2026-09-26，CODE_COMPLETE / DEBUG_PENDING）

决策：Claude，依据 Raphael 2026-09-26 的自主决策指示（非红线事项；无 core / 契约 / Schema 变更，无 Profile 数字或阈值，无门放宽）。

**显式 opt-in**：`LoopWiring.conditional: ConditionalPlan | None = None`（`research/loop/trials.py`）。`ConditionalPlan(minimum_effect, min_support, validate_cells)`
三个字段都必填、无默认值（缺省即 `TypeError`）：`minimum_effect` 为非空文本（条件化假设声明的最小有意义效应）；`min_support` 为正整数，
或显式 `None`（未声明阈值 → 每个单元 `no_support_threshold`）。`min_support` 不是 Validation Profile 数字，不放宽任何门：它标注报告，并决定哪些单元
**有资格**做逐单元验证（见下「逐单元验证」）；`validate_cells` 为 `bool`。

- **`None`（默认）**：什么都不发生；记录、指纹与结果与没有该字段时逐字节相同（`test_loop_e2e.py::test_records_without_a_conditional_plan_are_pinned`
  钉住 1fb7918 的默认 planted 三轮记录哈希与配置指纹）。指纹只在设置时多出 `conditional` 键。
- **设置时**：每个试验的 State × Strategy 矩阵算出后、读取任何单元数字之前，`research.experiments.register_trial_conditionals` 把矩阵的**全部**单元
  （声明的 `StateSpec.state_space` + 未知状态单元，从不按结果挑选）登记为该试验假设的条件化假设：名称 `<假设名>_given_<状态名>_<标签|unknown_state>`、
  父假设的版本与族、`origin_refs` 追加父假设 `Ref`。每个（单元，观察）一个 trial，与父假设的 trial 结构一一对应：父假设首次试验 → 登记单元；
  父假设的重新评估（attempt `loop_round:<loop>:<round>`）→ 每个单元以同一 attempt `register_reevaluation` 一次。全部或全不登记；同一观察再登记不增加 trial。
- 这些 trial 进入**同一族**的 trial 数：验证阶段交给 G3 的 `family_trial_count` 从本轮起就包含它们（多重检验校正更严格）。
- 实验行 `conditional`：父假设、attempt、族、状态、计划参数、`matrix_hash`、`newly_registered`、`family_trials`、逐单元（假设 ref、`trial_index`、样本数、
  `supported` / `support` = `meets_min_support` / `below_min_support` / `no_support_threshold`）与 `validation: PER_CELL_VALIDATION`（`validate_cells=False`：未运行）或 `PER_CELL_VALIDATION_RUN`（`True`：见下）。
  出错的试验没有矩阵 → `conditional: null`、不登记。条件化假设不进入生命周期，也不会被重新评估或进化。
- **预算**：实验阶段声明 `trials = 单元数 × 本轮试验数`（上界；运行器按 max(声明, 实际) 计费），条件化 trial 与其他登记一样计入 `LoopBudget`
  （P11：不得无限扩大 trial 预算）。启用时需要相应更大的 trial 预算，否则实验阶段 `REFUSED_BUDGET`。
- **持久**：登记写入 `trial_ledger.jsonl`（轮内），由检查点位置覆盖；重开后重放完全一致（重启后的记录哈希与 trial 日志等于不中断运行），
  对已记录的观察再次登记为幂等（0 个新 trial、账本不追加）；交叉校验 6 追加：实验行登记的每个单元必须在账本中（按其 attempt）。
  计划写入指纹：以其他计划或去掉计划重开同一目录被拒。

### 逐单元验证（2026-09-26，CODE_COMPLETE / DEBUG_PENDING）

决策：Claude，依据同一自主决策指示（无 core / 契约 / Schema 变更，无 Profile 数字或阈值，无门放宽）。`ConditionalPlan` 新增**必填** `validate_cells: bool`
（无默认值）：`False` = 上文的只登记行为（载荷、指纹与记录与此前逐字节相同，实验行 `validation: PER_CELL_VALIDATION`）；`True` = 另外做逐单元样本内验证
（载荷多出 `validate_cells: true`，因此两种设置的状态目录互相拒绝；实验行 `validation: PER_CELL_VALIDATION_RUN`）。

- **位置**：`ValidationStage`，紧接每个已完成试验自身的报告（同一阶段、同一 `family_trial_count`——本轮全部单元此时都已登记，G3 校正覆盖它们）。
- **只验证有支持的单元**：实验行记录的 `support` 为 `meets_min_support` 的单元；低于 `min_support` 或 `min_support=None` → `status: unsupported`、无门、无判定，永不 PASS。
- **证据**：每个试验重跑一次所选参数点（该试验的全部单元共用），取非零目标中决策时刻被状态阶段归到该单元的那部分——即矩阵所用的同一个因果
  `evaluation_time → state` 归属（`state` 阶段的 `labels`，不重新计算状态）；无归属状态的决策时刻不属于任何单元（`unattributed_traded` 计数）。
- **门**：试验报告中的适配门（`G0.backtest_cost_model`、单品种门、存在时的 `G0.execution_model` / `G0.manifest_binding`，同一重跑 bar），然后
  `research.validation.run_in_sample`（G0 → G3）作用于该单元的标签；`trial_index` = 单元登记的 trial 序号。适配门非 PASS 即停；没有非零目标 → `G0.data_available` INCONCLUSIVE。
  **G4 / G5 不运行**（`CELL_G4_NOT_RUN` / `CELL_G5_NOT_RUN`），单元判定只是 G0 – G3 样本内判定；试验自身验证出错 → 单元 `not_run`。
- **记录**：验证报告行的 `conditional_cells`（范围、`family_trial_count`、`rerun_result_hash`、逐单元状态 / 报告 id 与哈希 / 判定 / 门），随审计与持久检查点保存；
  交叉校验 6 追加：验证行里的每个单元都必须在账本中（按其 attempt）。
- **不移动生命周期**（`CELL_LIFECYCLE`）：单元假设不是生命周期主体；仅凭样本内 G0 – G3 的 PASS 还需要它自己的 G4 稳健性与 G5 封存 OOS 路径，而单元假设没有这条路径。
  单元 FAIL 也不写 FailureRecord（结果在审计中）。试验自身的生命周期只由它自己的报告决定，与没有计划时相同。
- **预算**：每个被验证的单元按一次 `validation_compute_seconds` 计费（声明 = 本轮已完成试验的全部有支持单元，上界）；不增加任何 trial。

**未做（后续）**：单元假设的生命周期（G4 / G5 路径）。
测试：`tests/research/loop/test_loop_conditional.py`、`tests/research/loop/test_loop_cell_validation.py`、`tests/research/experiments/test_trial_conditionals.py`；
数据集组合根的端到端（离线，SQLite 测试 catalog，无网络）：`tests/infrastructure/e2e/test_research_loop_dataset_conditional.py`。

### 跨进程持久性测试（Phase 11 验收，2026-09-26，仅测试；CODE_COMPLETE / DEBUG_PENDING）

`tests/research/loop/test_loop_cross_process.py`（子进程 `cross_process_child.py`）：每次写入与重开都是**独立的 Python 进程**，
崩溃是确定位置的 `SIGKILL`（子进程内包装，不改生产代码）。已证明：三个进程分段运行等于一个不中断进程；`loop_round_started` 之后、
记忆检查点之后审计记录之前被杀 → 另一进程拒绝（interrupted）且不写任何字节；审计记录之后总线发布之前被杀 → 下一进程补发并与不中断运行一致；
审批写入之后轮间检查点之前被杀、第三方进程伪造审批、重链审批、原地篡改审计、带更大预算重开 → 均被另一进程拒绝；持有目录的进程在时另一进程
`BusLocked`，被杀后锁由内核释放。**发现的缺口**（原以 `xfail(strict=True)` 固定；2026-09-26 已修复：`open_state` 获取 `state_dir/state.lock` 单写者锁，见 B32）：状态目录本身没有单写者锁，只有组合自带的
`FileEventBus(state_dir/bus)` 的 flock 串行化进程；调用方注入自己的总线（如 `InMemoryEventBus`）时，第二个进程可以在另一进程持有时打开同一目录，
而日志只在文件变短时拒绝追加，两个写者的冲突只会在下次重开时被发现（重复 `seq` → `JournalCorrupted`），不会被阻止。

### 被拒 LLM 输出的记录与声明式假设批次（Phase 7 补全，2026-09-26，CODE_COMPLETE / DEBUG_PENDING）

无 core / 契约 / Schema 变更。

- **被拒的 LLM 输出**：`from_llm` 的草稿结构是严格的（多余键拒绝、不做类型强制转换）；不合结构时 `LlmDraftRejected` 带着该次 `LlmCall`，
  hypothesis 阶段摘要 `llm` 记 `rejected`（原因）、`call_hash`（调用内容哈希）与 `call`（provider、model、prompt / input / output 引用、`called_at`）。
  只有出现被拒输出的轮次记录会变；其余记录逐字节不变（固定哈希测试）。`ContentVerifiedLLM` 的内容校验失败（`LlmContentUnverified`）同样记原因、`call_hash` 与 `call`。
- **假设批次**（`LoopWiring.hypothesis_batch`，可选，`None` 时记录与指纹逐字节不变）：`research.hypotheses.batch` 把声明的网格
  （算子 × 输入策略 × 参数点，全部显式、无默认值）展开为条件只有 `strategy = <name@version>` / `param <k> = <v>` 的假设；算子必须在声明的、带版本的
  已审阅算子白名单（`ReviewedOperators`，含审阅人）上且内容与审阅时一致，`trial_point` 跑不了的 DSL 算子种类、策略不接受的参数点在构造批次时即拒绝。
  组合循环时 hypothesis 阶段再用 `trial_point` + 策略目录 + 可请求参数点核对每个单元（族必须一致），不通过即拒绝组合——永不成为之后的 ERRORED trial。
  第一次运行时**整批**先登记进 `TrialLedger`（全有或全无；本轮 trial 预算按全部单元计费，预算不够则阶段 `REFUSED_BUDGET`、一条也不登记），
  族 trial 数因此覆盖整个网格；摘要 `batch` 记网格名与哈希、白名单与哈希、审阅人、声明的 trial 数与本轮登记的单元；生命周期证据带 `batch:` / `reviewed_operators:`。
  指纹多出 `hypothesis_batch`（仅在设置时）。持久重开不会重复登记。

测试：`tests/research/hypotheses/test_strict_llm_drafts.py`、`tests/research/hypotheses/test_batch.py`、`tests/research/loop/test_loop_llm_rejection.py`、`tests/research/loop/test_loop_batch.py`。

### 知识检索来源（Phase 7 补全，2026-09-26，CODE_COMPLETE / DEBUG_PENDING）

`LoopWiring.knowledge_source`（可选，`None` 时记录与指纹逐字节不变）：声明的 `KnowledgeSource`（`KnowledgeProvider` + `KnowledgeQuery`，
`research.hypotheses.generator`）。hypothesis 阶段每轮检索一次（估算与运行共用同一结果），重新校验 `KnowledgeResult`（出处、排序、`result_hash`），
拒绝答非所问（`query_hash` 不符）或来自其他 provider 的结果（阶段 FAILED，不登记任何假设）；条目经 `from_knowledge` 成为知识假设，
排在声明的 `knowledge` 之后（共用 `max_new_per_round`；声明中已有的条目按声明来源计）。摘要 `knowledge_search` 记 provider、查询哈希、
`result_hash`、条目与本轮由检索登记的假设；这些假设的生命周期证据带 `knowledge_query:<hash>` / `knowledge_result:<hash>`（无契约变更：
`origin_refs` 仍是条目引用）。指纹多出 `knowledge_source`（provider 身份、descriptor 哈希、查询哈希；仅在设置时）。

测试：`tests/research/hypotheses/test_knowledge_source.py`、`tests/research/loop/test_loop_knowledge_source.py`。

## ADR-0074 本机有限批次 Operator

Operator 入口是 `python -m research.loop.operator run --config PATH --rounds N`。`--rounds` 必填且为正整数；每次进程至多执行 N 个新 round，外部 scheduler 可用相同配置重复启动。Operator 只接受 synthetic random-walk 输入，LLM 与六类 P7 组合算子保持关闭，不启动 API，也不生成真实市场证据。

provider allowlist 固定为：`hlens_synthetic_random_walk@1.0.0`、`bar_log_return@1.0.0`、`trend_range@1.0.0`、`research_tsmom@0.1.0`、`hlens_bar_backtest@1.0.0`、`hlens_forward_return@1.0.0`。配置需给出每个完整 descriptor 的确切哈希；不接受插件搜索、Python import 路径或用户工厂。策略限两个 canonical TSMOM spec，回测固定 simulated-only，标签只允许经绑定的 forward return。

TOML 以自身目录解析 artifact 路径；每个对象都要提供 `{ path, content_hash }`，并与完整 canonical JSON 内容逐项相符。`[paths]` 必须显式指定 state、两个外部 anchor、reports、ADR-0062 freeze registry 目录和该 registry 的 anchor。缺省值、未知键、重复键、占位符、TEST ONLY 内容、浮点配置、未冻结 Profile、身份不匹配或已有非 v5 state 均被拒绝。首次运行前，`code_commit` 必须等于当前干净 tracked worktree 的完整 HEAD；`environment_lock` 必须与当前 `uv.lock`、Python 和平台相符。

目前没有已冻结的 production Validation Profile，freeze registry 也没有可供生产运行的 Profile，因此**没有可运行的 operator 配置**。下面的模板仅展示完整键结构；每个 `<...>` 都是明确占位符，Parser 必定拒绝。不要替换成测试夹具或临时数值来绕过冻结门。

```toml
schema_version = "1.0.0"

[operator]
llm_enabled = false

[paths]
state_dir = "<state directory>"
state_anchor = "<external state anchor>"
bus_anchor = "<external bus anchor>"
reports_root = "<reports directory>"
freeze_registry_dir = "<existing ADR-0062 registry directory>"
freeze_registry_anchor = "<existing external freeze anchor>"

[loop]
loop_id = "<loop id>"
seed = "<non-negative integer>"
epoch = "<UTC timestamp>"
cadence = "<seconds> seconds"
llm_prompt = ""
llm_cost_units_per_call = "0"
market = { path = "<synthetic market artifact JSON>", content_hash = "<exact sha256>" }
minutes_per_round = "<positive integer>"
compute_seconds_per_bar = "<seconds>"
family_id = "<family id>"
knowledge = [{ path = "<knowledge artifact JSON>", content_hash = "<exact sha256>" }]
max_new_hypotheses_per_round = "<non-negative integer>"
max_reevaluations_per_round = "<non-negative integer>"
hypothesis_compute_seconds = "<seconds>"
compute_seconds_per_trial = "<seconds>"
validation_compute_seconds = "<seconds>"
state_compute_seconds = "<seconds>"
profile = { path = "<frozen profile artifact JSON>", content_hash = "<exact sha256>" }
constitution_version = "<version>"

[loop.budget]
max_trials_per_round = "<non-negative integer>"
max_trials_total = "<non-negative integer>"
max_llm_cost_units = "0"
max_compute_seconds = "<seconds>"

[loop.wiring]
feature_spec = { path = "<feature spec JSON>", content_hash = "<exact sha256>" }
feature_chunk_bars = "<positive integer>"
state_spec = { path = "<state spec JSON>", content_hash = "<exact sha256>" }
decision_step = "<seconds> seconds"
decision_warmup = "<seconds> seconds"
strategies = [{ spec = { path = "<TSMOM strategy JSON>", content_hash = "<exact sha256>" }, hypothesis_family_id = "<family id>" }]
cost_model = { path = "<cost model JSON>", content_hash = "<exact sha256>" }
initial_equity = "<positive decimal>"
label_spec = { path = "<outcome label spec JSON>", content_hash = "<exact sha256>" }
profile_selection = { path = "<profile selection JSON>", content_hash = "<exact sha256>" }
profile_selection_rule = { path = "<selection rule JSON>", content_hash = "<exact sha256>" }
outcome_spec = { path = "<forward return outcome spec JSON>", content_hash = "<exact sha256>" }
declared_research_class = "<research class>"
code_commit = "<40 lowercase hex commit>"
environment_lock = "<canonical environment lock>"
evolution = false
oos_unseal = false
sealed_decision_step = false
conditional = false
hypothesis_batch = false
knowledge_source = false

[loop.wiring.robustness]
cscv_partitions = false
max_participation_rate = false
min_capacity = false
impact_coefficient = false
cross_asset_min_positive_fraction = false
max_undersampled_pnl_share = false

[providers.synthetic_market_provider]
id = "hlens_synthetic_random_walk"
version = "1.0.0"
descriptor_hash = "<exact sha256>"

[providers.feature_provider]
id = "bar_log_return"
version = "1.0.0"
descriptor_hash = "<exact sha256>"

[providers.state_provider]
id = "trend_range"
version = "1.0.0"
descriptor_hash = "<exact sha256>"

[providers.strategy_provider]
id = "research_tsmom"
version = "0.1.0"
descriptor_hash = "<exact sha256>"

[providers.backtester]
id = "hlens_bar_backtest"
version = "1.0.0"
descriptor_hash = "<exact sha256>"

[providers.outcome_provider]
id = "hlens_forward_return"
version = "1.0.0"
descriptor_hash = "<exact sha256>"
```

成功运行前仍必须通过所有 path / artifact / provider / Profile freeze 检查。state 只新建或以相同 identity 重开 v5；不接管 v3 / v4，不自动修复中断轮。每次打开先从已验证 audit 幂等补齐 `research_loop_round` 报告，再执行一轮、立即写一份报告；不写 Phase 6 matrix。SIGINT / SIGTERM 在当前轮完成并报告后退出。退出码为 0（完成 / 边界停止）、2（命令或配置拒绝）、3（预算或 loop halt）、4（中断 / 人工恢复审查）、5（损坏、锁、锚点或报告 I/O 故障）。

## 循环内替换提案触发（ADR-0100 第 7 项，2026-09-30，CODE_COMPLETE / DEBUG_PENDING，默认关闭）

`replacement.py`。`LoopWiring.replacement_trigger` 默认 `None`；只有显式 `ReplacementTrigger(enabled=True, ...)` 才会被组合，
且需要 `EvolutionPlan`（触发器在 `evolution` 阶段内运行：`ReplacementTriggerStage` 包装 `EvolutionStage`，阶段名与顺序契约不变）。
`enabled=False` 与 `None` 完全相同：不组合、不入指纹，记录 / 指纹 / 状态目录逐字节不变。不新增调度器：只在循环自身轮次中、`every_rounds` 到期的轮次运行；
失败轮重试轮（ADR-0083）与演化一样永不运行。

- **替换窗口 = 已发布 Profile 版本的封存窗口**（Constitution C-S3；2026-09-30 完整性修复）：窗口以 `window_profiles` 给出——循环 Profile 同一族（同 `name`）的**更新**纯 `major.minor.patch` 版本，
  状态 `FROZEN` 且在调用方的 `ProfileFreezeRegistry` 中有 ADR-0062 冻结记录（ref + 内容哈希 + 引用的校准报告），冻结时间不晚于窗口起点。`RegisteredSealedWindow` 仅是对该版本的引用（Profile ref + 内容哈希 + 其 `data_split` 定义的边界）。
  窗口 Profile 除封存窗口字段（`data_split.sealed_oos_boundary` / `sealed_oos_length`）外必须与循环 Profile **逐字段相同**（身份与运行元数据 `name` / `version` / `created_at` / `lineage` / `provenance` / `status` 不比较）：阈值、成本、切分、范围、预算完全一致。
- **预算**（Constitution C-S2）：每次窗口开封都有审计记录（账本行 + 触发行），并**计入与 G5 族开封相同的全局开封预算**（`DurableUnsealingLedger.count()` 含开封数）。预算与循环 G5 vault 相同：循环 Profile 的
  `data_split.sealed_oos_max_unsealings`，否则 `OosUnsealBudget.max_unsealings`；两者皆无 → 组合时拒绝。预算用尽时拒绝开封（不写入）；之后的 G5 开封也看得到全部窗口开封。预算写入指纹（`unsealing_budget`）。
- **输入在循环之外**：`source(round_index, as_of)` → `ReplacementInputs`（`Incumbent`（ACTIVE / DEGRADED）、`ReplacementCandidate`（人工 Promotion 路径给出的生命周期 + 报告哈希）、报告解析器、报告的 Profile）。
  候选**合格**须：在循环谱系中是某个给定现任的后代、处于 `PAPER` / `PRODUCTION_CANDIDATE`（OOS → PAPER 只能由人工批准）、与某现任的配对尚未进入 `ProposalLedger`；不合格者只列在摘要中，不计 trial。
- **两阶段、两个不同的到期轮次**（在本循环账本开封之前就在密封窗口上评估过的证据属于"在别处消耗"，一律不接受）：
  - **阶段 1 开封**（候选从未被触发）：① 先在 `TrialLedger` 登记假设（origin `combination`、循环自身 family），计入族 trial 数与 `LoopBudget`；同一候选版本终身只登记一次；
    ② 窗口护栏（下）；拒绝时记录 `refused`，不开封；③ 按配置顺序开封第一个通过护栏的登记窗口：在开封账本写入唯一一次 `WindowOpening`（`sealed_oos.jsonl` 格式 3，含 `opened_at` = 本轮计划时刻），计入预算；状态 `window_opened`，本轮不使用任何证据。
  - **阶段 2 评估**（之后的到期轮次；账本有该候选的开封且该窗口尚未消耗）：① 开封必须恰属该候选（subject + 规格哈希、trial + 哈希、本循环、更早轮次、登记哈希一致）；窗口必须在本轮 `as_of` 前**已结束**、仍未被循环看见、尚未消耗；OOS → PAPER 仍须人工批准；
    所有声明报告必须可解析且其 Profile 已给出。此处拒绝不消耗窗口（证据可以稍后到达）。
    ② 声明报告中封存窗口恰为该开封窗口的即为该窗口证据；至少一份时，**先**写入窗口唯一一次 `WindowConsumption`（开封哈希 + 这些报告哈希）**再**检查证据——窗口只产出第一次提交的证据（不能在多次评估间挑选）；已为别的窗口消耗的报告拒绝。一份都没有时记录 `refused`，不消耗。
    ③ 每份声明报告的 Profile 必须除封存窗口字段外与循环 Profile 相同；循环自身窗口与开封窗口以外的窗口上的报告拒绝；开封窗口上的报告必须恰为登记版本（ref + 内容哈希，仍冻结），且 `created_at` 必须晚于开封的 `opened_at`、不早于窗口结束、不晚于本轮 `as_of`（早于开封的 G5 证据是在别处评估的）。
    ④ 调用现有 `propose_replacements`（提出者为循环自动化身份，`proposed_at` 为本轮计划时刻，`extra_evidence` 附 `loop:` / `loop_round:` / `trial:` / `sealed_window:` / `sealed_window_opening:` / `sealed_window_consumption:` 溯源）；
    提案写入调用方的持久 `ProposalLedger`，恒为 `PENDING_HUMAN_APPROVAL`，从不晋升、不改任何生命周期。作业抛错记录为 `failed`，窗口保持已消耗，不重试、不删除。
  - 因此**一个窗口终身至多产出一个被评估的候选**：只开封一次（一个 subject），证据只消耗一次（同一 subject）。
- **窗口护栏**（阶段 1）：**预登记**（全部窗口及其冻结记录进入配置指纹即锚定的 `loop_state_opened` 头行，换窗口 = 新目录；每个窗口起点必须**严格晚于循环首个计划 `as_of`**（epoch），
  写入头行时检查、记录为 `epoch_check`（规则、epoch、各窗口 id 与起点），每次重开按记录值复核）；**独立**（不与循环 Profile 自身封存窗口重叠——组合时拒绝；窗口之间不重叠；
  循环已摄入的**任何**数据触及该窗口 → 拒绝：取所有已记录轮次与本轮的并集——各轮 ingest 的 `data_window`（数据集路径）、每个生成市场的完整 bar 区间与累计研究片段（合成路径）、本轮 segment；
  已运行但未记录数据窗口的轮次或未绑定审计 → 视为已见、拒绝）；**从未开封**（无论为谁，尤其是候选的祖先）；**此前未评估**（候选已声明任一登记窗口上的报告 → 拒绝）；**预算未用尽**；OOS → PAPER 须由非自动化身份批准。
- **持久性**（沿用 ADR-0073 / ADR-0083 模式）：trial 登记、窗口开封与证据消耗都是轮内存储写入，在状态的准入门内、由轮次检查点定位并随之锚定；审计记录的 `evolution` 摘要 `replacement_trigger` 键保存每次触发的完整行（阶段、trial、窗口、开封、消耗、作业载荷及全部提案）。
  重开时交叉校验 6 额外核对：每行的 trial 在 TrialLedger（内容哈希一致）、每个开封 / 消耗与行一致、账本中每个开封与消耗都被某个已完成 `evolution` 阶段的行命名（写入后该轮 `evolution` 失败时窗口保持已开封 / 已消耗，失败阶段即审计记录）。
  触发器要求 `DurableUnsealingLedger`（内存账本重启即忘记开封）。`ProposalLedger` 与 `ProfileFreezeRegistry` 位于状态目录之外。
- **格式版本**：开封账本日志格式 3（`replacement_window_opened` 含 `opened_at`，新增 `replacement_window_consumed`，载荷含 `"format_version": 3`；格式 1 行字节不变；首版的格式 2 开封行重放时拒绝，fail closed）；
  触发器摘要与指纹载荷 `format_version: 2`。状态目录版本（v3–v6）与检查点布局不变：新行位于已被检查点定位的 `sealed_oos.jsonl`。
- 诚实边界：报告的 `created_at` 是生产方的声明；与开封的绑定强度等同于该声明（及覆盖它的报告内容哈希）。
- 未经测试（DEBUG_PENDING）：开启前需补齐单元 / 端到端测试（含重开、崩溃、复用、预算与指纹拒绝）。

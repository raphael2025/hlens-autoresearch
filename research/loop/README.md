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
| `memory.py` | `ResearchMemory`（TrialLedger、ReviewQueue、FailureRegistry、策略目录、试验 / 验证记录、谱系、封存开封账本、摄取市场（及其生成规格）与累计研究数据）；`ReviewQueue.approve` 要求非空且非自动化身份（非循环自身 actor、非 `research_loop:` 前缀），并记录审批；`ReviewQueue(path)` 把入队 / 审批 / 取用逐行写入哈希链日志，重放时重新核验（自动化身份的审批、草稿或调用哈希不符的审批 → `JournalCorrupted`）；`ReviewQueue.observe(ReviewObserver)` 绑定唯一观察者（持久状态目录：审批前拒绝轮中审批、审批后立即写轮间检查点并移动锚点） |
| `compose.py` | `SyntheticLoopConfig` + `LoopWiring` + `build_synthetic_loop`：研究侧组合根，所有数字来自配置；`open_synthetic_loop(config, state_dir=...)` → `DurableLoop(loop, memory, state_dir, bus, owned_bus)`（`build_synthetic_loop(..., state_dir=...)` 等价，只返回 loop）；不给 `bus` 时自动使用 `state_dir/bus` 并与审计交叉核对（`check_round_bus`） |
| `durable.py` | 一个状态目录承载整个循环（见下）：`open_state`、`MemoryCheckpoint`（每轮一条记忆检查点）、交叉校验、`LoopStateInconsistent`；可选外部锚点 `StateAnchor` / `FileAnchor` / `StateHead` |

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
  阶段实测时间只进入不入哈希的指标（`ResearchLoop.metrics`），因此本目录阶段的记录哈希不变。

## 持久组合：一个状态目录（ADR-0049 实施说明 durable composition，2026-09-26）

`open_synthetic_loop(config, state_dir=...)` 把循环的全部状态放在一个目录；不给 `state_dir` 时行为不变（全在内存，记录哈希相同）。

| 文件 | 内容 |
|---|---|
| `audit.jsonl` | `LoopAuditLog`：每轮 started / recorded |
| `memory.jsonl` | 头行 `loop_state_opened`（配置指纹）+ 每个已记录轮次一条 `round_memory` 检查点 + 每次轮间人工审批一条 `between_rounds` 检查点 |
| `trial_ledger.jsonl` / `sealed_oos.jsonl` / `lineage.jsonl` / `reviews.jsonl` | TrialLedger、开封账本、谱系、审阅队列（均为哈希链日志） |
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
  （`max_unsealings`、获准族及批准人）以及精确节奏（`cadence_microseconds`）；用任何不同的预算（更大、更小、多一个获准族、换批准人）重新打开都拒绝，
  消息写明哪个预算不同。**提高预算是人的决定：用新的 `state_dir` 或新的 `loop_id`。** 头行版本 `STATE_VERSION = 3`（版本 1 / 2 目录被拒绝）。
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
  调用方自己传入的总线照旧使用、不做交叉核对（例如内存总线）；纯内存组合仍必须传 `bus`。
- 回归测试：`tests/research/loop/test_loop_durable.py`（重启 e2e 与不中断运行的审计哈希 / trial 数 / 生命周期 / 封存 OOS 完全相同；
  删除账本、审计超前账本、篡改审批、逐文件尾部截断、一致截断（已记录的限制）、中断轮次、换配置、删检查点文件、篡改检查点增量；
  换预算 / 开封额度 / 获准族 / 批准人被拒绝、相同预算接受、节奏精确；带锚点的重启、一致截断 / 删目录 / 分叉 / 锚点丢失或在目录内被拒绝、锚点不后退；
  轮间审批立即有检查点并移动锚点、最后一轮之后的审批重启后仍在、只删审批被拒、审批连同检查点一起删在有锚点时被拒（无锚点为已记录的限制）、
  无检查点的伪造审批（最后一轮之后 / 早先轮次内）被拒、检查点指向别的审批被拒、轮中审批被拒、检查点失败使下一轮不被记录；
  自动总线的重启与不中断运行相同且总线重放每轮 `record_hash`、审计与总线之间崩溃后补发并续跑相同、总线少一轮补发 / 少两轮或丢失被拒、
  外来记录 / 乱序 / 超前的总线被拒）。

未完成（调试批次）：NATS、研究仪表盘；持久组合只覆盖合成市场组合根（真实数据集组合根另做）；滚动循环与固定日历 Profile 的配合（研究窗外的数据不被使用，
换窗口需要新 Profile；累计研究数据在覆盖整个研究窗口之前，G4 walk-forward 仍为 INCONCLUSIVE——这是正确行为）；
封存 bar 只取本轮段内的（跨轮累计封存数据未做）；`matrix_from_backtest` 的逐 bar 归因需要逐 bar 状态（本循环按决策期归因）；
验证阶段的技术失败（`VALIDATION → FAILED` 不是 ADR-0006 的边）只记 FailureRecord、生命周期不动。

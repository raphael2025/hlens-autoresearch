# research/loop

Phase 11 持续研究循环的**研究侧**（[ADR-0049](../../docs/adr/0049-continuous-research-loop.md)，含 2026-09-25 W2、review fixes 2、累计验证窗口与 2026-09-26 dataset-backed loop 实施说明）。状态：FRAMEWORK_IMPLEMENTED / NOT_VALIDATED。

通用机制（调度、预算、生命周期护栏、审计、事件发布）在 `apps/worker/loop.py`；本目录只提供阶段实现与组合根，
依赖方向 research → apps/worker（反向禁止）。一轮：`ingest → state → hypothesis → [evolution] → experiment → validation → memory`
（`evolution` 可选，位置固定）。
`ingest` 是可插拔的**轮次数据源**：合成市场（`IngestStage`）或经验证的 Research Dataset manifest（`DatasetIngestStage`）；
其后各阶段只经 `segment.RoundData` 协议读本轮数据，两种来源共用同一组合（`compose.compose_loop`）。

| 文件 | 内容 |
|---|---|
| `segment.py` | `RoundData` 协议（摄取之后各阶段读本轮数据的唯一入口：研究 bar、决策网格、扣留的封存段、特征运行、复现快照、验证器绑定）；`Segment`（合成实现，本轮数据：**累计研究数据**——截至 `as_of` 摄取的全部研究窗口 bar，每个摄取市场一个 `ResearchPiece`——+ 本轮被扣留的封存段 `SealedBars`，只凭 vault 发出的一次性 `SealedEvaluation` 释放——该凭据在释放前已把该族的唯一评估记为消耗）、决策网格、合成 bar → `FeatureObservation`、分块 F4 特征运行、`trial_point`（假设条件 `strategy = name@version` / `param k = v`，其他条件一律拒绝）、`decimal_text`（进入哈希记录的浮点先转固定量化的 Decimal 文本） |
| `stages.py` | `IngestStage`（新市场接续上一轮价格路径；按 Profile 固定日历切分：研究窗口 bar 并入累计研究数据，封存 bar 扣留）、`StateStage`（F4 `bar_log_return` 经 `run_feature` → Phase 2 `StateProvider` 经 `run_state`，都在累计研究数据上；同一特征值经 `signals_from_features` 成为策略信号；按研究段缓存特征）、`HypothesisStage`（知识假设 + 已人工审阅的 LLM 草稿预登记；仍开放的假设在数据增长后作为新 trial 重新登记（`reevaluation_candidates`）；新草稿只入审阅队列）、`MemoryStage`（出错 → FAILED、FAIL → REJECTED，均写 FailureRecord；样本内 PASS → OOS——OOS 表示「正在经过 / 有资格进入封存样本外检验」，不是「已通过 OOS」，证据为样本内报告；封存 OOS 失败 → REJECTED，G5 未运行 / INCONCLUSIVE / PASS 均留在 OOS；INCONCLUSIVE 留在 VALIDATION） |
| `trials.py` | `ExperimentStage`（先核对已在 TrialLedger 预登记（重新评估按 attempt 核对），在累计研究数据上，再生成 06-experiment.md §2 复现元组的 `ExperimentSpec` / `ExperimentRun`，经 `CandidateTrialRunner` 跑策略 → 风控 → 回测，按决策期把收益归到 Phase 2 状态上做 Phase 6 矩阵）、`ValidationStage`（`PipelineBacktestValidator` G0 – G4；G5 仅在显式 `OosUnsealBudget` 列出该族、样本内 PASS、本轮有封存段且该族未开封时运行；开封后先 `claim_evaluation` 原子消耗唯一评估，提前结束或出错 → INCONCLUSIVE `consumed_without_result`，窗口永久关闭）、`TrialComponents`、`OosUnsealBudget`（全局次数 + `approved_families`：族 → 批准人）、`ConditionalPlan`（opt-in：矩阵全部单元预登记为 trial，见下「条件化假设」） |
| `evolution.py` | `EvolutionStage` / `EvolutionPlan`：从更早轮次未被否证（按各假设最近一次验证：PASS / INCONCLUSIVE）的最佳候选出发 `mutate`，`require_new_version` 与目录防覆盖，`LineageGraph` 可追溯；后代作为新假设先登记、IDEA → CANDIDATE、本轮在累计研究数据上重新验证，不继承父代结论 |
| `memory.py` | `ResearchMemory`（TrialLedger、ReviewQueue、FailureRegistry、策略目录、试验 / 验证记录、谱系、封存开封账本、摄取市场（及其生成规格）与累计研究数据）；`ReviewQueue.approve` 要求非空且非自动化身份（非循环自身 actor、非 `research_loop:` 前缀），并记录审批；`ReviewQueue(path)` 把入队 / 审批 / 取用逐行写入哈希链日志，重放时重新核验（自动化身份的审批、草稿或调用哈希不符的审批 → `JournalCorrupted`）；`ReviewQueue.observe(ReviewObserver)` 绑定唯一观察者（持久状态目录：审批前拒绝轮中审批、审批后立即写轮间检查点并移动锚点） |
| `compose.py` | `compose_loop` / `compose_durable`（两种数据源共用的组合：同一组阶段、预算、护栏、审计、持久钩子与自动持久总线）+ `LoopSettings` / `settings_fingerprint`；`SyntheticLoopConfig` + `LoopWiring` + `build_synthetic_loop`：合成组合根，所有数字来自配置；`open_synthetic_loop(config, state_dir=...)` → `DurableLoop(loop, memory, state_dir, bus, owned_bus)`（`build_synthetic_loop(..., state_dir=...)` 等价，只返回 loop）；不给 `bus` 时自动使用 `state_dir/bus` 并与审计交叉核对（`check_round_bus`） |
| `dataset_source.py` | `DatasetIngestStage` / `DatasetRound` / `DatasetSegment` / `DatasetCatalog`：每轮从声明的 manifest 读数据；`SealedDatasetPair`（封存 manifest 对，只在开封认领后读取）/ `WithheldSealedWindow`（只扣留声明：只记录哈希与 Profile 封存窗口，从不读取；见下「数据集组合」） |
| `dataset_compose.py` | `DatasetLoopConfig` + `build_dataset_loop` / `open_dataset_loop` / `dataset_loop_fingerprint`：数据集组合根 |
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
  （`max_unsealings`、获准族及批准人；TEST ONLY 的 `ephemeral_unseal_for_tests` 仅在为真时出现）以及精确节奏（`cadence_microseconds`）；用任何不同的预算（更大、更小、多一个获准族、换批准人）重新打开都拒绝，
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
否则 `ValueError`——`from_llm` 与假设阶段按"被拒草稿"记录、从不登记。组合根把 resolver 交给假设阶段：人工审阅过的草稿在被取用时再核对一次，内容在审阅与使用之间消失或变化
→ 该轮假设阶段 FAILED、其后阶段 SKIPPED、草稿不登记（fail closed）。约定：提供者须把每个载荷存为其规范 JSON（`plugins.llm.ScriptedLLMProvider(store=...)` 即如此）。
不用包装时行为与记录哈希不变。测试：`tests/research/loop/test_llm_content.py`。


## 条件化假设（Phase 6 进入循环，2026-09-26，CODE_COMPLETE / DEBUG_PENDING）

决策：Claude，依据 Raphael 2026-09-26 的自主决策指示（非红线事项；无 core / 契约 / Schema 变更，无 Profile 数字或阈值，无门放宽）。

**显式 opt-in**：`LoopWiring.conditional: ConditionalPlan | None = None`（`research/loop/trials.py`）。`ConditionalPlan(minimum_effect, min_support)`
两个字段都必填、无默认值（缺省即 `TypeError`）：`minimum_effect` 为非空文本（条件化假设声明的最小有意义效应）；`min_support` 为正整数，
或显式 `None`（未声明阈值 → 每个单元 `no_support_threshold`）。`min_support` 只标注报告，不是 Validation Profile 数字，不参与任何门。

- **`None`（默认）**：什么都不发生；记录、指纹与结果与没有该字段时逐字节相同（`test_loop_e2e.py::test_records_without_a_conditional_plan_are_pinned`
  钉住 1fb7918 的默认 planted 三轮记录哈希与配置指纹）。指纹只在设置时多出 `conditional` 键。
- **设置时**：每个试验的 State × Strategy 矩阵算出后、读取任何单元数字之前，`research.experiments.register_trial_conditionals` 把矩阵的**全部**单元
  （声明的 `StateSpec.state_space` + 未知状态单元，从不按结果挑选）登记为该试验假设的条件化假设：名称 `<假设名>_given_<状态名>_<标签|unknown_state>`、
  父假设的版本与族、`origin_refs` 追加父假设 `Ref`。每个（单元，观察）一个 trial，与父假设的 trial 结构一一对应：父假设首次试验 → 登记单元；
  父假设的重新评估（attempt `loop_round:<loop>:<round>`）→ 每个单元以同一 attempt `register_reevaluation` 一次。全部或全不登记；同一观察再登记不增加 trial。
- 这些 trial 进入**同一族**的 trial 数：验证阶段交给 G3 的 `family_trial_count` 从本轮起就包含它们（多重检验校正更严格）。
- 实验行 `conditional`：父假设、attempt、族、状态、计划参数、`matrix_hash`、`newly_registered`、`family_trials`、逐单元（假设 ref、`trial_index`、样本数、
  `supported` / `support` = `meets_min_support` / `below_min_support` / `no_support_threshold`）与 `validation: PER_CELL_VALIDATION`（未运行）。
  出错的试验没有矩阵 → `conditional: null`、不登记。条件化假设不进入生命周期，也不会被重新评估或进化。
- **预算**：实验阶段声明 `trials = 单元数 × 本轮试验数`（上界；运行器按 max(声明, 实际) 计费），条件化 trial 与其他登记一样计入 `LoopBudget`
  （P11：不得无限扩大 trial 预算）。启用时需要相应更大的 trial 预算，否则实验阶段 `REFUSED_BUDGET`。
- **持久**：登记写入 `trial_ledger.jsonl`（轮内），由检查点位置覆盖；重开后重放完全一致（重启后的记录哈希与 trial 日志等于不中断运行），
  对已记录的观察再次登记为幂等（0 个新 trial、账本不追加）；交叉校验 6 追加：实验行登记的每个单元必须在账本中（按其 attempt）。
  计划写入指纹：以其他计划或去掉计划重开同一目录被拒。

**未做（后续）**：逐单元验证（没有任何门看单元收益；登记 = 预先承诺 + 诚实的 trial 计数）；单元假设的生命周期；数据集组合的端到端测试（代码路径共用 `compose_loop`，
指纹共用 `settings_fingerprint`）。测试：`tests/research/loop/test_loop_conditional.py`、`tests/research/experiments/test_trial_conditionals.py`。

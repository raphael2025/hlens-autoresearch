# research/hypotheses

假设登记、组合/变换算子（04-research-loop.md §4–5）。

> 框架已实现（ADR-0040，FRAMEWORK_IMPLEMENTED / NOT_VALIDATED）：`dsl.py` 组合算子、`ledger.py` 预登记与 trial 计数、`generator.py` 知识 / LLM 生成（LLM 草稿须人工审阅）。

## 落盘的 TrialLedger（调试批次，2026-09-25，ADR-0040 实施说明）

`TrialLedger()` 省略 `path` 时行为不变（纯内存，进程结束即丢）。传入 `TrialLedger(path=<文件>)` 后，每次新单项登记
（`register` / `register_draft`）和重新评估登记各追加一行哈希链 JSON（`research.persistence.AppendOnlyJournal`，
与 `research.strategies.failure_registry.FailureRegistry` 同一持久化写法：只追加、`flush` + `fsync`、文件变短即拒绝）；
重新打开同一文件会重放并校验整条哈希链，恢复已登记的假设与各族 trial 计数——某族的 trial 数因此跨进程重启延续，
不会在重启后回落到 0。同一 `name@version` 重复登记仍是幂等的（不重复计数）；换内容登记仍拒绝（`LedgerError`）。
文件被篡改、截断或出现未知记录类型一律 `research.persistence.JournalCorrupted`，不静默修复。

`register_batch(hypotheses)` 会先检查整个输入的类型、重复身份、LLM 来源和已有内容冲突，再用一个
`register_batch` journal event 记录所有尚未登记的假设；事件追加并 `fsync` 成功后才更新内存。整批与已存在且内容相同的条目保持幂等，
冲突不会写入任何批次行。`preregister_batch(batch, ledger)` 通过此接口，因此一个持久批次不会因进程在逐条登记之间退出而只留下该批次的合法前缀。
新 event 重放时会严格检查 payload 结构、规范化 Hypothesis 内容、批内唯一性及与历史登记的冲突；无效 event 按 journal corruption 拒绝。

此保证限于**同一个 TrialLedger journal event**：不构成与 typed-plan audit、loop audit、lifecycle 或其他 journal 的跨文件事务；也不自动恢复计划执行。
如底层写入中断导致损坏或截断，重开仍 fail closed，不修复或删除历史。未审阅的 LLM 假设不能通过 `register_batch` 登记；应使用带人工审阅状态的
`register_draft` 单项入口。

## Typed-plan admission journal 基础（ADR-0073）

`typed_plan_audit.py` 提供独立的 `PlanAdmissionJournal`，只保存严格版本化的 `plan_admission_prepare` / `plan_admission_commit` 追加事件。PREPARE 绑定 round-start identity、non-runnable `TypedPlan`、compiler / operator / provider / 输入 / 输出 / `ExperimentSpec` 的 evidence、完整 Hypothesis 数据与哈希、TrialLedger baseline，以及 `register_batch` payload hash。除 typed AST 外，每个 evidence 使用固定 `{"identity": ..., "value": {...}}` 形状，按 canonical project JSON 重算内容 hash；Hypothesis 必须按核心模型严格解析并 round-trip。未知字段 / 事件、版本不支持、非规范数据或哈希不符均拒绝重放。Reducer 只允许一个 pending PREPARE，COMMIT 必须逐项引用该 PREPARE，且 ledger event 必须是 baseline 后唯一、内容与哈希完全相符的 `register_batch` event。Evidence 的 `identity` / `value` 仍由未来已审阅的 compiler / operator / provider adapter 提供；本模块不验证各算子的业务语义或来源真实性。

`PlanAdmissionJournal` 不打开 loop state、不能判断 worker round 是否真的已开始，也不接入 checkpoint / anchor、不调用 compiler / Provider / Runner、不授权执行。调用方必须在 loop state 单写锁下协调 journal、TrialLedger 与后续 checkpoint / anchor；当前实现切片尚无 loop opener recovery。

`TrialLedger.recover_register_batch(hypotheses, baseline_seq=..., baseline_hash=..., lease=...)` 是纯 ledger 侧精确恢复入口。它要求当前持有的写租约（`acquire_write_lease()` 返回的不透明 `LedgerLease`，按对象身份比较；持有期间其他所有 mutation 入口在任何线程一律拒绝，`release_write_lease(lease, seal=...)` 可使该实例此后拒绝一切写入，直到从 journal 重开）、durable journal 和全新 Hypothesis identities：journal 仍处于 PREPARE baseline 时只追加一个普通 `register_batch` event；若恰好已有紧邻 baseline 的一个完全匹配 event，则返回该 event，不重复计数。身份重用、额外 / 乱序 / 内容不同的尾记录、非持久 ledger、stale journal 或 journal 损坏均拒绝。此方法不自行写 plan COMMIT / memory checkpoint、不修复不完整 JSONL，也不对 loop failed / interrupted round 续跑。普通 `register_batch` 的 exact duplicate 依旧幂等且不增加 trial，不能当作新 attempt。TrialLedger 不再公开返回可写的底层 `AppendOnlyJournal`（原 `journal` 属性已移除，避免绕过写租约 / seal 直接 append）；跨文件校验只用只读 API：`durable`、`journal_head()`（条数与链头）和 `journal_snapshot()`（在 ledger 锁内取得的 `LedgerJournalSnapshot`，条目及 payload 均为分离副本，无 append 能力）。loop state 目录会以 `bind_write_gate` 把 ledger 绑定到其 admission gate（`research.persistence.gate`）：`register` / `register_draft` / `register_batch` / `register_reevaluation` / `acquire_write_lease` / `recover_register_batch` 均先进入 gate 再取 ledger 锁（锁序 gate → ledger 锁，ledger 持锁时从不进入 gate）；租约活跃或 admission 中断后普通写入在写入前即被 gate 拒绝，只有租约自身线程以其 `LedgerLease` 调用的 `recover_register_batch` 可通过。

此基础实现不使任一 P7 operator runnable。ADR-0073 的 loop lock / round-start 校验、recovery 编排、memory v4 checkpoint 与 external anchor 接线已实现；本提交补充 ADR-0074 operator-only v5 身份绑定并保留 v3 / v4 opener 分支。当前没有 operator 调用路径；v5 只建立 durable identity 基础，不接配置 parser、Provider registry 或 CLI，仍需后续实现和验收。

`ledger.py` 的 `register_reevaluation(hypothesis, attempt)`：已登记假设的再次评估（例如循环在增长的累计研究数据上重新评估 INCONCLUSIVE 假设）作为**单独的 trial** 预登记并计入族 trial 数（`trials` / `trial_index` / `trial_log`；ADR-0049 accumulated validation window 实施说明）。

## 严格的 LLM 草稿与可审计的拒绝（Phase 7 补全，2026-09-26）

`generator.py` 的草稿结构是严格的：多余的键被拒绝（不再静默丢弃），字段类型不做强制转换（数字不是陈述、字符串不是条件元组）。
输出不符合结构、或字段构不成合法 `Hypothesis`（如名称不合命名规则）时抛出 `LlmDraftRejected`（`ValueError` 子类），
其 `call` 是该次交换的 `LlmCall`；循环的 `HypothesisStage` 把被拒调用的内容哈希与 prompt / input / output 引用连同拒绝原因
写进该轮 hypothesis 阶段摘要（`llm.call_hash` / `llm.call`）——LLM 输出无论接受与否都有记录（roadmap P7）。
只有出现被拒 LLM 输出的轮次记录会变；其他记录逐字节不变。

## 声明式假设批次（`batch.py`，Phase 7 补全，2026-09-26）

`BatchGrid`（名称、族、`created_at`、算子、输入 `StrategySpec`、参数点、最小有意义效应——全部必填、无默认值）× `ReviewedOperators`
（声明的、带 SemVer 版本与审阅人的算子白名单）→ `expand_batch` → `HypothesisBatch`（假设由构造计算，不能传入）。每个单元的条件恰好是循环
`trial_point` 能运行的两种形式；以下情况一律 `BatchRefused`，发生在任何登记与运行之前：算子不在白名单上（或同名同版本但内容不同）、
算子种类是 `trial_point` 跑不了的 DSL 算子（conditioning / interaction / temporal / transformation / ensemble / negation）或未知种类、
参数未声明搜索空间、值不在搜索空间内（类型也须一致）、浮点值、文本值读回后不是它自己、空或重复的因子。`preregister_batch(batch, ledger)`
通过 `TrialLedger.register_batch` 用单个登记事件全有或全无地预登记整批，族 trial 数因此覆盖整个网格。算子只是数据（主张、方向、固定列表中的种类），生成物永不作为代码执行。

网格中的参数点在构造时会复制并递归冻结。循环以已记录的 `TrialOutcome` 判断单元是否完成，而不是把 TrialLedger 的预登记状态误当作执行完成；Hypothesis 阶段失败后，已预登记但尚无结果的单元仍会在后续轮次按原身份调度，已产生结果的单元不会重复运行。

## 知识检索来源（`generator.py`，Phase 7 补全，2026-09-26）

`KnowledgeSource(provider, query).search(family_id)` 调用 `KnowledgeProvider.search(KnowledgeQuery)`，重新校验结果并拒绝其他查询 / 其他 provider 的结果，
返回 `KnowledgeSearch`（provider、`query_hash`、`result_hash`、条目、`from_knowledge` 生成的假设）；查询哈希与 `result_hash` 是这些假设的来源，
由调用方（循环的 hypothesis 阶段）记入摘要与生命周期证据。

## Typed plan 直接引用校验（`typed_plan_resolver.py`，Phase 7，2026-09-27，ADR-0068 实施说明）

`resolve_direct_references(plan, resolver=...)` 对 `typed_plan.py` 的 `TypedPlan` 中每个外部 `SpecInput`，经调用方显式注入的
`SpecResolver.resolve(ref) -> VersionedSpec | None` 取回规格，任一项不满足即抛 `PlanReferenceRefused`（`PlanRefused` 子类，带 `code`），
不返回部分结果：resolver 抛错（`resolver_error`）、返回 `None`（`missing`）、类型不是该 kind 的**精确**核心规格类
（`FeatureSpec` / `StateSpec` / `EventSpec` / `StrategySpec`，子类同样拒绝，`wrong_type`）、返回规格的 `kind:name@version` 与引用不同
（`ref_mismatch`）、在分离的深拷贝上重算的 `content_hash()` 与计划声明的哈希不同（`content_hash_mismatch`）；同一目标在计划中声明了
不同哈希时，在调用 resolver 之前拒绝（`conflicting_claimed_hash`）。按节点顺序、输入顺序遍历，同一目标只调用 resolver 一次；
结果 `DirectReferenceResolution` 保留每次出现（`inputs`，计划顺序）与去重后的已校验规格（`specs`，首次出现顺序），并绑定 `plan_hash`。

**只校验直接引用，不是执行授权。** 结果的 `transitive_closure_verified`、`execution_authorized`、`runnable` 恒为 `False`：不校验传递依赖闭包，
不登记或查询算子实现，不 lower / 编译 / 执行计划，不改变 `TypedPlan.runnable`，不写报告或 journal，不触碰 `TrialLedger`；
（`compile_plan` 默认仍拒绝所有计划；显式开启时以本结果作为 lowering 证据，见文末 ADR-0100 一节。）本模块不从包 `research.hypotheses` 导出，也不接入循环。未运行测试，CODE_COMPLETE / DEBUG_PENDING。

## Experiment / Hypothesis binding evidence（`plan_bindings.py`，Phase 7，ADR-0073 §1）

`validate_experiment_bindings(...)` 是兼容用的纯拒绝校验器：要求输入的 ExperimentSpec 与 Hypothesis 非空且数量相同；每个
`ExperimentSpec.repro.hypothesis_ref` 必须精确指向批次中唯一的 Hypothesis，且 `dependency_hashes` 中对应值等于重算的内容哈希；
每个 Hypothesis 恰被一个 ExperimentSpec 引用。每个已提供的 `LoweredOutputBinding` 必须是精确的
Feature / State / Event / Strategy 核心规格类，并与所关联 ExperimentSpec 的直接依赖 ref 和重算 hash 完全相符；同一不可变规格可以被多个实验共同引用。

依 ADR-0078，`TypedPlan.nodes` 是 lowered outputs 全集的权威来源，每个 AST 节点要求且只要求一个输出规格。`produce_lowered_output_bindings(...)` 通过 node ID 映射拒绝缺失 / 多余节点，并检查名义输出类型；
`validate_complete_experiment_bindings(...)` 再要求每个 ExperimentSpec 恰有一个 plan、每个计划节点恰有一个输出，以及 direct dependency ref/hash 一致。契约 2.4.0（ADR-0088 决策 2）起，条件策略计划的核心规格是 `composition=ConditionedStrategy` 的 StrategySpec；`conditioning` / `ensemble` / `negation` 节点的 StrategySpec 必须恰好带有对应的 `ConditionedStrategy` / `EnsembleStrategy` / `NegatedStrategy` 组合，否则两个校验器都以 `plan_output_composition_mismatch` 拒绝。

旧 `validate_experiment_bindings(...)` 保留兼容，仍只校验调用方所交 outputs，不能用于声明全集完整。新 API 只提供集合完整性与直接绑定证据，不校验传递依赖闭包或算子语义，不持久化 admission、不注册 trial、不授权执行。六类 operator 仍关闭，`TypedPlan.runnable` 仍恒为 `False`。测试已新增 / 更新但未运行，待统一验收。

## P7 non-runnable lowering（`typed_plan_lowering.py`，ADR-0082）

`lower_typed_plan(plan, resolution=..., created_at=...)` 只接受与 plan hash 对应的直接引用解析结果，并要求调用方明确给出带时区的 `created_at`。已接受的 lowering：

- `interaction`：两个 FeatureSpec 按同一 evaluation time 做严格 product，缺失传播为 `None`，不允许 bool / float / 静默舍入；结果是 `FeatureSpec`，目标 Provider key 为 `p7_interaction_product@1.0.0`。
- `transformation`：`standardize` / `difference` / `smooth`，显式 `window`、只向后看（ADR-0082 §4）；计划格式 `1.2.0` 起另接受**时间序列** `rank` / `quantile`（ADR-0099）：`rank` → `p7.transformation.rank_ts@1.0.0`（含当前 bar 的最近 `window` 根 bar 内的百分位秩，∈ [0, 1]，params 另加 `ties=average`、`scale=unit_interval`），`quantile` → `p7.transformation.quantile_ts@1.0.0`（`floor(rank × buckets)` 截断到 `[0, buckets − 1]`，params 另加 `buckets`、`ties=average`）。两者都要求 `window ≥ 2`；新节点参数 `buckets`（整数 ≥ 2）仅 `quantile` 必填，其他 transform 出现即在解析期拒绝。
- `transformation` 之**横截面** `rank_cs` / `quantile_cs`（计划格式 `1.3.0`，ADR-0100 §2）：节点不带 `window`，必填 `universe`（钉定的 universe 快照，写作 `research_dataset:<namespace.table>@<snapshot_id>`，即某份 `ResearchDatasetManifest.dataset`）与 `universe_hash`（该 manifest 的内容哈希），`quantile_cs` 另必填 `buckets`（整数 ≥ 2）。`lower_typed_plan(..., universes=[manifest, ...])` 由调用方提供 manifest 作为证据（不查 Registry），按内容哈希与数据集身份逐项绑定，缺失 → `unresolved_universe`、不一致 → `universe_binding_mismatch`。输出 `p7.transformation.rank_cs@1.0.0` / `p7.transformation.quantile_cs@1.0.0` 的 FeatureSpec：`inputs = (源 FeatureSpec, manifest 的 DatasetRef)`，params 声明 `population=universe_snapshot_members_at_bar`、`alignment=bar_interval_end`、`missing=exclude_from_population`、`min_population=2`、`ties=average`、universe / manifest / universe spec 绑定；`rank_cs` 另加 `scale=unit_interval`、`output_decimal_places=18`、`rounding=half_even`，`quantile_cs` 另加 `buckets`。语义：同一 bar（按 `interval_end` 对齐）时刻钉定快照的全部成员为总体，缺值成员不计入，`rank = (count_less + 0.5 × (count_equal − 1)) / (n − 1)`（平局取平均秩，`n < 2` → 缺失），`quantile = min(floor(rank × buckets), buckets − 1)`，只用 `available_time ≤ t` 的数据。未改 `core/`。
- `temporal`（ADR-0088 决策 1）：两个输入 EventSpec 的 `bar_spec` 必须都非空且指向同一目标，`time_unit` 必须为 `bar`，否则 `operator_open`；第二事件在第一事件之后 1..`window` 根 bar 内（左开右闭）；结果 EventSpec 的 `bar_spec` 同输入、`observable_lag` 取第二事件的值、`trigger` 为规范 JSON 声明。第一事件的 `observable_lag` 大于第二事件时无法证明可见性，以 `temporal_visibility_unprovable` 拒绝。两个输入必须是不同的 EventSpec（ref 不同），否则以 `temporal_same_input` 拒绝（ADR-0100 修订 1 §3；Provider 构造时同样拒绝）。
- `conditioning` / `ensemble` / `negation`（ADR-0088 决策 2）：分别产生 `composition` 为 `ConditionedStrategy` / `EnsembleStrategy(rule="equal_weight_mean")` / `NegatedStrategy` 的 StrategySpec。`signals` 为 base / 成员信号（conditioning 再加门控状态）按目标身份去重后的有序并集；风险政策与适用标的继承 base，ensemble 成员二者必须完全一致（ADR-0069），否则拒绝；conditioning 的 `state_value` 不在 StateSpec `state_space` 中时以 `unknown_state_value` 拒绝。取反**不是**验证负对照。

每个 lowered definition 的执行 Provider 见下节（ADR-0100 第 1 项）。输出为 node ID 映射，随后交给 `produce_lowered_output_bindings(...)` 做 ADR-0078 全集、类型与组合校验。

`typed_plan.py` 的 `PLAN_FORMAT_VERSION` 为 `1.3.0`；`1.1.0` 计划照常解析（不接受 `buckets`），`1.2.0` 计划照常解析（不认识 `rank_cs` / `quantile_cs`，不接受 `universe` / `universe_hash`），`TypedPlan.schema_version` 保留计划自身的版本，因此旧计划的 payload / 内容哈希不变，`1.1.0` 计划中的 `rank` / `quantile` 节点仍按旧语义 `operator_open`（ADR-0099 决策 4）。`1.3.0` 中时间序列 transform 的语法与 `1.2.0` 完全相同（`window` 改为按 transform 必填，不接受 universe 参数）。横截面执行 Provider 见 `plugins/features/p7_cross_sectional.py`（`P7RankCsProvider` / `P7QuantileCsProvider`；其请求 / 结果类型不同于单序列 `FeatureProvider`，尚未进入编译 allowlist）。混有 OPEN 节点或任一拒绝条件的计划整体 fail closed，不产生部分 lowering。该函数不写 journal / TrialLedger、不接 Runner；`TypedPlan.runnable` 永远为 `False`（它在计划 payload 中，保持旧计划哈希不变），运行就绪见下节 `CompiledPlan`。

## P7 算子执行与编译（ADR-0100 第 1 项，默认关闭）

**执行 Provider**（每个只服务 lowering 产出的精确规格；Provider key 等于规格 params / trigger 中声明的 `provider`）：

| definition | Provider（`name@version`） | 位置 |
|---|---|---|
| `p7.transformation.standardize@1.0.0` | `P7StandardizeProvider`（`p7_transformation_standardize@1.0.0`） | `plugins/features/p7_operators.py` |
| `p7.transformation.difference@1.0.0` | `P7DifferenceProvider`（`p7_transformation_difference@1.0.0`） | 同上 |
| `p7.transformation.smooth_sma@1.0.0` | `P7SmoothSmaProvider`（`p7_transformation_smooth@1.0.0`） | 同上 |
| `p7.transformation.rank_ts@1.0.0` | `P7RankTsProvider`（`p7_transformation_rank@1.0.0`） | 同上 |
| `p7.transformation.quantile_ts@1.0.0` | `P7QuantileTsProvider`（`p7_transformation_quantile@1.0.0`） | 同上 |
| `p7.interaction.product@1.0.0` | `P7InteractionProductProvider`（`p7_interaction_product@1.0.0`） | 同上 |
| `p7.temporal.sequence_within_bars@1.0.0` | `P7TemporalSequenceProvider`（`p7_temporal_sequence@1.0.0`） | `plugins/events/p7_temporal.py` |
| `p7.conditioning.state_gate@1.0.0` | `P7ConditionedStrategyProvider`（`p7_conditioning_state_gate@1.0.0`） | `research/strategies/p7_compositions.py` |
| `p7.ensemble.equal_weight_mean@1.0.0` | `P7EnsembleStrategyProvider`（`p7_ensemble_equal_weight_mean@1.0.0`） | 同上 |
| `p7.negation.target_position@1.0.0` | `P7NegatedStrategyProvider`（`p7_negation_target_position@1.0.0`） | 同上 |

- feature 算子的输入是其他 feature：调用方显式给出 `UpstreamFeature(spec, provider)` 表；Provider 在评估时刻 `t` 的可见集合上自行切出上游子请求（`available_time + lag <= tau`）并逐个 `check_answers`。transformation 的 `window` 按可见 bar 计数：取末尾连续 `window` 根（`difference` 为 `window + 1`），上游在每根 bar 的 `tau_i`（该 bar 及之前各 bar 的最晚 `available_time`）求值；不连续、历史不足、任一上游缺值或统计量无定义（`standardize` 离散度为 0）→ `None`，不填补。`standardize` 用窗口内总体标准差（与 `zscore_reversion` 相同），`difference` 为 `x_t − x_{t−window}`（精确），`smooth` 为 SMA，`rank` / `quantile` 按 ADR-0099 公式精确计算；`product` 在同一评估时刻相乘、精确，不能精确表示即拒绝。除法 / 开方用固定 50 位有效数字、half-even。
- `temporal` 需要调用方显式给出 bar 时长（`str(bar_spec)` → `timedelta`，不从名称猜测）和两个上游 EventSpec；窗口为 `(first, first + window_bars × bar]`，输出事件时间 = 第二事件的 `event_time + observable_lag`（契约的可观测时间规则）。计划格式 `1.3.0` 的 lowered trigger 另绑定两个上游规格的内容哈希（`first_event_hash` / `second_event_hash`，ADR-0100 修订 1 §4），Provider 逐一核对它们等于所给上游 EventSpec 的 `content_hash()`，`infrastructure.event.upstream.verify_interaction` 据此接受这类规格；`1.1.0` / `1.2.0` 计划的 lowering 输出保持原样（trigger 只绑定上游 ref），runner 仍拒绝（fail closed），哈希绑定只由 Provider 按调用方的上游表执行。
- 组合策略复用 `research.strategies.composite.CompositeStrategyProvider` 的语义（门控：状态等于 `state_value` 时持 base 目标，其他 / 未知 / 缺失为空仓；ensemble：成员目标等权平均；negation：目标取反，现货下的空头目标 fail closed，ST-4）；子类只改描述符身份并精确接受 lowering 的声明式 params。
- feature / event Provider 登记了静态 manifest 与 entry point（`infrastructure/plugins/builtin/{features,events}.py`，ADR-0087）；strategy Provider 属研究代码，按惯例不登记 manifest。

**编译**（`typed_plan_compiler.py`；`typed_plan.compile_plan` 转调）：`compile_plan(plan, resolution=..., created_at=..., allowlist=..., switch=...)` 仅当 `switch=P7ExecutionSwitch(enabled=True)`（默认 `False`；`from_config` 读 `p7_operator_execution`，只有布尔 `True` 开启）**且**每个节点 lowering 出的 definition 在调用方显式传入的 allowlist（definition → `OperatorImplementation`，默认审阅表 `P7_OPERATOR_ALLOWLIST`）中有算子、输出类型与 Provider key 都一致的实现时，返回 `runnable=True` 的 `CompiledPlan`；否则抛带代码的 `PlanCompileRefused`（`execution_disabled`、`no_allowlist`、`missing_lowering_evidence`、`provider_not_registered`、`operator_mismatch`、`output_kind_mismatch`、`provider_identity_mismatch`…），lowering 本身的拒绝原样抛出。`compile_plan(plan)` 不带参数时与之前一样拒绝。`TypedPlan.runnable`、计划 payload 与哈希、trial 计数和 admission 规则都不变；运行就绪只体现在 `CompiledPlan.runnable`。`CompiledPlan.build_providers(...)` 按计划顺序实例化各节点 Provider（外部输入的 Provider、bar 时长、`instrument_type` 必须显式给出），`compiler_evidence` / `operator_evidence` / `provider_evidence` 给出 ADR-0073 PREPARE 所需的 evidence（实现身份含定义模块源码的 SHA-256）。

**循环接入**：`research/loop/p7_plan.py` 的 `p7_strategy_candidates(compiled, providers, switch=..., hypothesis_family_id=...)` 把根节点为组合策略的已编译计划变成普通 `StrategyCandidate`，调用方追加到既有的 `LoopWiring.strategies`；同一开关未开启、计划不可运行、根不是策略或根 Provider 不服务根规格时拒绝。循环没有新增阶段、字段或 trial 规则。

状态：CODE_COMPLETE / DEBUG_PENDING；未运行测试 / lint / 类型检查。

# ADR-0082：P7 六类组合算子的语义与 Provider lowering

| 字段 | 值 |
|---|---|
| 状态 | **Accepted（六类算子均已接受：interaction；transformation 之 standardize / difference / smooth；conditioning、temporal、ensemble、negation 见第三次接受记录；transformation 之 rank / quantile 由 ADR-0099（时间序列）/ ADR-0100（横截面）关闭）**；下方首次接受时的 OPEN 列表为历史记录 |
| 日期 | 2026-09-28（transformation 修订：2026-09-28） |
| 决策者 | **Codex 依 Raphael 2026-09-28 授权决定**（interaction）；**Claude Code（PM）依 Raphael 2026-09-28 对 PM 的授权决定**（transformation 修订，见文末第二份接受记录） |
| 相关 Phase | Phase 7 — Discovery |
| 影响范围 | `research/hypotheses/` lowering 与 ADR-0078 输出规格；不改 `core/`、契约、Schema、Constitution、Validation Profile 或阈值 |
| 兼容性 | 既有 Hypothesis helper、typed-plan 格式与 `TypedPlan.runnable=False` 不变 |

## 背景

ADR-0068 接受了闭世界 typed plan 和 fail-closed 边界，但没有接受任何六类算子的执行语义。ADR-0078 随后确定 `TypedPlan.nodes` 是 lowered output 的权威全集：每个节点必须对应且只对应一个按其声明输出类型选出的版本化 core spec。当前输出规格集合为 EventSpec、FeatureSpec、StrategySpec；`conditional_strategy_plan` 没有规格表示。

本 ADR 逐项决定哪些语义能在现有规格内表达。接受一项 lowering 只表示可以纯确定地生成普通版本化规格声明；不代表 Provider 已实现、已登记、可以运行或满足 admission 条件。

## 决策

### 1. 共同 lowering 与运行边界

1. Lowering 输入是严格解析的 `TypedPlan`、与其 plan hash 相符的 `DirectReferenceResolution` 和调用方显式提供的 timezone-aware `created_at`。引用规格须精确匹配 AST ref 与 content hash。Lowering 是纯函数，不查 Registry、不补全传递依赖、不读取时钟、不写审计 / admission / TrialLedger，也不调用 Provider。
2. lowerer 返回 `node_id → VersionedSpec` 映射；之后必须由 ADR-0078 的 `produce_lowered_output_bindings` 核对全集、类型并按 AST 顺序构造 bindings。任一节点 OPEN、解析证据不全、类型不符或规格校验失败时，整个 lowering 抛拒绝错误，不返回部分映射。
3. 只有本 ADR §2 接受的 `interaction` 产生 FeatureSpec。其规格身份名称使用计划节点、输入 ref/hash 和规范化 UTC `created_at` 的规范内容身份派生，语义版本为 `1.0.0`；`created_at` 由调用方显式给定并参与 core spec 内容哈希。生成的 inputs / lineage 均按 AST 输入次序记录。
4. Interaction 的 Provider target key 声明为 `p7_interaction_product@1.0.0`，写入 FeatureSpec params；规格 definition 为 `p7.interaction.product@1.0.0`。FeatureSpec 的直接依赖 ref 与普通 FeatureProvider descriptor 的 supported spec hash 将绑定实际规格身份。此 ADR 不实现或登记该 Provider，也不将其加入 operator allowlist。
5. `compile_plan` 仍拒绝所有计划；没有节点因本 ADR 改为 runnable。`TypedPlan.runnable` 恒为 `False`。Lowering 结果不能进入 PREPARE、TrialLedger、Research Loop、Experiment Runner 或执行入口；运行启用、拒绝审计落点以及任何执行前置条件仍受 ADR-0068 / 0073 约束。

### 2. 逐项语义与 lowering 决定

| 算子 | 输入 | 业务语义决定 | 输出规格 / Provider lowering | Fail-closed 条件与状态 |
|---|---|---|---|---|
| `conditioning` | StrategySpec + StateSpec + 精确 `state_value` | OPEN。现有名义输出是条件策略计划，但未定义状态不匹配 / 未知标签、策略门控、按状态 trial 与报告如何映射到单个实验；不能把条件文本伪装成普通策略。 | ADR-0078 无 conditional strategy spec。 | 一律 `operator_open`；不得生成 StrategySpec 或拆成隐式多个实验。 |
| `interaction` | 两个按顺序指定且分别解析为精确 FeatureSpec 的输入 | **Accepted：逐评估时刻的点乘**。两输入必须有唯一且相同的 evaluation time；同一时刻任一输入缺测 / 值为 `None` 时结果为 `None`，不得填零、插值、前向填充或改变时间网格。只接受 `Decimal` 或 `int`，拒绝 `bool` 及其它类型；乘法要求十进制精确，不能精确表示 / 输入非法则拒绝。仅依赖在该 evaluation time 可见的输入。输入顺序保留；此算子无交换律规范化授权。 | 一个 FeatureSpec；definition `p7.interaction.product@1.0.0`；params 固定声明 `operator=product`、`provider=p7_interaction_product@1.0.0`、`alignment=exact_evaluation_time`、`missing=propagate_none`、`numeric_domain=decimal_or_int_excluding_bool` 和语义版本；`available_lag=0`、`deterministic=True`。Provider 执行仍未实现，规格只是一份明确的 Provider 请求。 | 两输入不是精确 FeatureSpec、输入直接 ref/hash 与解析结果不符、某节点输入不完整、Provider 不能满足严格同刻对齐 / 缺失 / 精确数值语义、或 Registry 无法验证传递闭包时拒绝。规格生成不等于该 Provider 能运行。 |
| `temporal` | EventSpec + EventSpec + 正整数 `window` 与显式 `time_unit` | **仍 OPEN（PM 2026-09-28 复核维持）**。待决策清单（docs/reviews/2026-09-28-pending-decisions.md）§3 选项 A 要求“两个输入 EventSpec 必须声明相同的 bar 规格”，但 `core.domain.specs.EventSpec` 只有 `trigger`（不透明 provider 专属文本）、`features`、`states`、`observable_lag`，**没有任何字段能声明 bar 规格 / Representation 身份**；`trigger` 的 schema 因 provider 而异，没有跨 provider 的保留键可读。Lowering 又是纯函数（本 ADR §1.1），不得查 Registry 或补全传递依赖去追溯某个 EventSpec 间接依赖的哪个 Representation。因此“比较两输入的 bar 规格”在不改 `core/` 的前提下无法无损实现——这不是业务语义未定，而是**契约表达力缺口**。 | 名义目标 EventSpec；不生成。ADR-0061 的 seq DSL 不被本决定改写或自动复用，其微秒窗口不作为 bar 计数的静默替代。 | 一律 `operator_open`。恢复该项需要新 ADR 给 `EventSpec`（或等价机制）新增一个可无 Registry 查询、直接读取的 bar 规格声明字段（例如指向 `RepresentationSpec` 的 `Ref`），属于 Domain Contract additive 变更（H1），本 ADR 不越权代做；见文末第二份接受记录。 |
| `transformation` | FeatureSpec + 枚举 transform 名 + 正整数 `window`（bar 数，ADR-0082 修订新增的 plan 节点参数，`typed_plan.py` `PLAN_FORMAT_VERSION` 1.0.0→1.1.0） | **`standardize` / `difference` / `smooth`：Accepted（PM 2026-09-28）**；**`rank` / `quantile`：仍 OPEN**（横截面语义——统计总体、跨标的对齐——不在本批范围）。三个接受的变换只定义**时间序列**语义：必须显式 `window`（正整数 bar 数）、只向后看（backward-only，禁止看到未来 bar）；`standardize` 的拟合参数（均值 / 标准差）只能来自该显式 `window` 圈定的滚动训练窗口（Constitution C-L3）——因为是严格滚动、从不越出 `window` 的计算，`window` 本身即训练窗口绑定，没有另一个可省略的“全样本拟合”模式；`smooth` 的算法显式为简单移动平均（SMA），不是其它平滑族；任一 bar 缺值按“缺失”向前传播（不得填零 / 插值 / 前向填充）。见 §4。 | 三个接受变换各一个 FeatureSpec；definition 分别为 `p7.transformation.standardize@1.0.0`、`p7.transformation.difference@1.0.0`、`p7.transformation.smooth_sma@1.0.0`；params 固定声明 `operator=<transform>`、`provider=p7_transformation_<transform>@1.0.0`、`window`、`direction=backward_only`、`missing=propagate_none` 与语义版本，`standardize` 另加 `fit_scope=rolling_training_window`，`smooth` 另加 `algorithm=simple_moving_average`；`available_lag=0`、`deterministic=True`。`rank` / `quantile` 名义目标仍是 FeatureSpec，但不生成。 | 输入不是精确 FeatureSpec、`window` 非正整数或缺失（typed_plan.py 解析期即拒绝）、`transform` 不属于已接受三项、或后续 ADR-0078 校验不通过时拒绝；不猜默认窗口、算法或样本范围。`rank` / `quantile` 节点仍语法可解析（`window` 字段语法上统一要求，但对这两者未被使用）、lowering 时一律 `operator_open`。 |
| `ensemble` | 至少两个不同 StrategySpec 的有序输入集合 | OPEN。旧 helper 文案称 equal-weight vote，Architecture 又包含 weighted signals；均未定义信号到目标仓位的映射、平局、异步信号、风险政策、适用标的与成本处理。StrategySpec.signals 不能引用 StrategySpec。 | 名义目标 StrategySpec；现有规格不能表达这些策略成员及投票语义，不生成。 | 所有 ensemble lowering 均 `operator_open`；不把策略 ref 塞入 `signals` 或 `lineage` 伪装成组合策略。 |
| `negation` | StrategySpec | OPEN。`inverse` 未确定是信号反号、目标仓位取反还是交易动作反向；也未决定现金 / 杠杆 / 风险政策 / 成本 / 对照资格。 | 名义目标 StrategySpec；无无损的既有规格编码，不生成。 | 一律 `operator_open`；不得据策略信号自行构造反向仓位或声称是 control。 |

### 3. Interaction 的约束和确定性

- 如果 Feature 的两输入来自同一 plan 中更早的节点，lowering 使用该节点刚产生的 FeatureSpec；否则必须来自被解析并 hash 核验的直接引用。AST 顺序及输入顺序保持原样。
- 两输入的可见性由既有 FeatureProvider / runner 协议提供；P7 语义不放宽 `available_time ≤ evaluation_time`、`knowledge_time ≤ knowledge_cutoff` 或 Outcome 禁止输入规则。依赖闭包与物化数据真实性仍由 Registry / Runner / 验证服务负责。
- 输入对齐要求两个来源在同一 evaluation time 各至多有一个值。缺少任一侧值时输出缺失；实现不得生成额外时间点。两边存在不一致时间集合时，Provider 必须在完整请求层 fail closed，不能静默取交集后继续。
- 输出身份由 operator semantic key、节点 payload、输入顺序的 ref 和各输入重算 content hash、规范化 UTC `created_at` 派生；完整 SHA-256 用于规格名，避免截断身份冲突。相同 plan、相同解析规格和相同显式 `created_at` 必须给出相同 ref 与内容 hash；不同创建时刻不会复用同一 ref 并形成冲突载荷。
- 乘积值使用精确十进制语义；不得通过 binary float 中转或按某个固定 scale 静默舍入。是否需要额外精度 / 资源上限属 Provider 实现审阅事项，未满足时 Provider 拒绝请求。

### 4. Transformation 的约束和确定性（2026-09-28 修订）

- 只有 `transform ∈ {standardize, difference, smooth}` 接受 lowering；`rank`、`quantile` 与其它任何名称一律 `operator_open`，即使语法层面因 `window` 现为通用必填字段而能解析。
- `window`（正整数 bar 数）是 typed plan 的**节点参数**（与 `temporal` 的 `window`/`time_unit` 同一层级），不是 lowering 调用方另行传入的带外参数：同一份计划 JSON 因此自描述、可审计、内容哈希覆盖它，不存在“同一计划不同 lowering 调用产出不同 window”的可能。这要求 `typed_plan.py` 的 `_PARAMETER_KEYS[TRANSFORMATION]` 从 `{transform}` 扩至 `{transform, window}`，因而 `PLAN_FORMAT_VERSION` 由 `1.0.0` 提升为 `1.1.0`（无迁移：`transformation` 此前恒为 `operator_open`，不存在任何已持久化的 `1.0.0` transformation payload）。
- `standardize` 的“训练窗口绑定”不是独立于 `window` 的第二个概念：滚动窗口计算在定义上永不看到 `window` 之外的数据，因此显式、必填的 `window` 本身就是 Constitution C-L3 要求的训练窗口绑定；lowering 没有、也不提供任何可回退的无绑定（全样本）拟合路径——缺 `window` 在 `typed_plan.py` 解析期即被拒绝，不会到达 lowering。
- 与 `interaction` 一致：只有单一输入来自同一 plan 更早节点时使用该节点刚产生的 FeatureSpec，否则来自已解析并 hash 核验的直接引用；输出身份由 operator definition、节点 payload、输入 ref/hash 与规范化 UTC `created_at` 的内容哈希派生，完整 SHA-256 用于规格名；相同 plan、相同解析输入与相同显式 `created_at` 必须给出相同 ref / 哈希，不同 `created_at` 不复用同一 ref。
- `params` 里的 `direction=backward_only`、`missing=propagate_none`、`fit_scope=rolling_training_window`、`algorithm=simple_moving_average` 都是对未实现 Provider 的**声明性请求**，与 `interaction` 的 `alignment=exact_evaluation_time` 同一性质：不构成任何运行时保证，真正的窗口连续性、bar 对齐、Registry 闭包与物化数据真实性仍完全由未来 Provider / Runner / 验证服务负责。

### 5. 验收边界

本 ADR 的实现验收仅涉及纯规格 lowering：可解析直接引用、输入 ref/hash 复核、输出身份确定、FeatureSpec 输入 / lineage 与参数完整、OPEN 算子整体 fail closed，以及后续 ADR-0078 producer 可接受完整 node map。代码和测试未因此获得运行授权。具体 Product / Transformation Provider、执行器的跨来源时间对齐、运行时数值一致性、Registry 闭包和 admission 接线须在各自边界内另行实现 / 审查；`TypedPlan.runnable` 仍为 False。

## 后果

- interaction 与 transformation（`standardize` / `difference` / `smooth`）现在都能以一个版本化 FeatureSpec 表达，并可进入 ADR-0078 的 node-addressed 完整输出校验流程；两者仍不是可执行 Provider。
- `conditioning`、`temporal`、`transformation` 之 `rank`/`quantile`、`ensemble`、`negation` 有逐项、显式的 OPEN 记录，基础 lowering 会拒绝混合了任一 OPEN 节点的整个计划，不产生部分结果。`temporal` 的 OPEN 现在附带一个具体、可行动的缺口说明（§2 表格 + 下方第二份接受记录），而不是笼统的“语义未定”。
- typed plan 格式版本由 `1.0.0` 升至 `1.1.0`（仅 `transformation` 节点新增必填 `window`），限于 `research/hypotheses/`；无迁移需要。无契约、Schema、Constitution、Profile 数值、验证阈值、`core/` 或执行入口变化。六类旧 Hypothesis helper 的声明性输出保持不变。

## 接受记录（2026-09-28）

Codex 依 Raphael 2026-09-28 授权决定：接受 §2 `interaction` 的 product 语义与 FeatureSpec 声明 lowering；`conditioning`、`temporal`、`transformation`、`ensemble`、`negation` 保持 OPEN。接受只授权纯、non-runnable lowering，不接受 Provider 执行、算子 allowlist 注册或 Research Loop / Runner 接入。`TypedPlan.runnable` 与 `compile_plan` 拒绝行为不变。

## 接受记录（2026-09-28，transformation 修订）

Claude Code（PM）依 Raphael 2026-09-28 对 PM 的授权（CLAUDE.md §0：PM 可直接决定工程、架构、模块语义与 ADR 批准）、并落实待决策清单 [docs/reviews/2026-09-28-pending-decisions.md](../reviews/2026-09-28-pending-decisions.md) §3 `D-P7-OPS` 中 `transformation` 选项 A：

- **接受**：§2 `transformation` 表格所述 `standardize` / `difference` / `smooth` 时间序列语义与 FeatureSpec 声明 lowering；`rank`、`quantile` 保持 `operator_open`。接受只授权纯、non-runnable lowering，不接受 Provider 执行、算子 allowlist 注册或 Research Loop / Runner 接入；`TypedPlan.runnable` 与 `compile_plan` 拒绝行为不变。
- **`temporal` 维持 OPEN，且升级为已定位的契约缺口**：待决策清单选项 A（两输入 EventSpec 须声明同一 bar 规格、窗口按该 bar 计数、区间左开右闭、结果可见时间 = 第二事件可见时间、不复用 ADR-0061 微秒窗口）业务语义清晰，但 `core.domain.specs.EventSpec` 当前没有任何字段可以承载“bar 规格”身份，纯 lowering 又被本 ADR §1.1 禁止查 Registry / 补全传递依赖去追溯它。在不修改 `core/` 的前提下这不可无损实现（H1：Domain Contract 变更需 Raphael 批准 + ADR）。
  - **需要的契约字段（供未来 ADR 评估，本 ADR 不擅自新增）**：`EventSpec` 增加一个可直接读取、无需 Registry 查询的 bar 规格声明，例如 `bar_spec: Ref | None`（指向一个 `RepresentationSpec`，如 `representation:canonical_bar_1m@1.0.0`），或等价的显式标识字段；两个 `temporal` 输入的该字段必须相等（且非 `None`）才可继续 lowering，`window` 按该 bar 规格计数（区间 `(第一事件, 第一事件 + window 根 bar]`），结果 `EventSpec.observable_lag` / 事件时间取第二事件的可见时间。
  - 在该字段被 Raphael 批准并落地前，`temporal` 继续对任何输入一律 `operator_open`；本 ADR 及其实现不引入任何近似、猜测或把 `time_unit` 当作已验证 bar 规格的行为。

## 参考

- [ADR-0068](0068-phase7-typed-operator-plans.md)：闭世界 typed plan 与执行边界。
- [ADR-0073](0073-phase7-plan-admission-recovery.md)：admission 与 TrialLedger 崩溃恢复边界。
- [ADR-0078](0078-p7-lowered-output-completeness.md)：计划节点是 lowered outputs 的权威全集。
- [ADR-0030](0030-feature-provider-contract.md)：FeatureSpec / FeatureProvider 可见输入与确定性契约。
- [ADR-0061](0061-interaction-dsl.md)：独立 Event DSL；不为 P7 `temporal` 提供 bars 映射。
- [docs/research/constitution.md](../research/constitution.md)：特别是 C-L1、C-L2、C-L3 与 C-P。

## 接受记录（2026-09-28，第三次：ADR-0088 契约 2.4.0 落地）

Claude Code（PM）依 Raphael 2026-09-28 授权，落实待决策清单 §3 的选项 A 与 ADR-0088 决策 1–2。

- **temporal：Accepted。**
  - 两个输入 EventSpec 的 `bar_spec` 都必须非空且目标身份相同，`time_unit` 必须为 `bar`，否则 `operator_open`。
  - 语义：第二事件发生在第一事件之后 1..`window` 根 bar 内（左开右闭）。
  - 输出 EventSpec：`bar_spec` 同输入；`observable_lag` 取第二事件的值；`features` / `states` 为两个输入的去重有序并集；`trigger` 为规范 JSON 声明（definition `p7.temporal.sequence_within_bars@1.0.0`）。
  - 第一事件的 `observable_lag` 大于第二事件时，无法证明可见性，以 `temporal_visibility_unprovable` 拒绝。
  - 不复用 ADR-0061 的微秒窗口。
- **conditioning：Accepted。**
  - 输出 `StrategySpec(composition=ConditionedStrategy(base, state, state_value))`。
  - signals = base signals ∪ state；风险政策与适用标的继承自 base。
  - `state_value` 不在 `StateSpec.state_space` 中时，以 `unknown_state_value` 拒绝。
  - 状态未知或缺失时空仓；每个 (base, state, state_value) 计一个 trial。
- **ensemble：Accepted。**
  - 输出 `StrategySpec(composition=EnsembleStrategy(members, rule="equal_weight_mean"))`。
  - 成员至少 2 个，按目标身份去重。
  - 成员的风险政策与适用标的必须完全一致（ADR-0069），否则拒绝。
  - signals 为各成员信号的去重有序并集。
- **negation：Accepted。**
  - 输出 `StrategySpec(composition=NegatedStrategy(base))`。
  - 目标仓位取反，风险政策不变。
  - **不**作为验证负对照。
  - 现货做空成本缺口（ST-4）由 Provider fail closed。
- **与 ADR-0078 的衔接：**
  - `conditional_strategy_plan` 的核心规格改为带 `ConditionedStrategy` 的 StrategySpec。
  - producer 与完整性校验器都要求 conditioning / ensemble / negation 节点的输出带有对应的组合，否则以 `plan_output_composition_mismatch` 拒绝。
- **仍为 OPEN：** `transformation` 的 `rank` / `quantile`。
- **授权范围：** 本次接受只授权纯的、不可运行的 lowering。
  - 不实现、不登记 Provider；
  - `TypedPlan.runnable` 与 `compile_plan` 的拒绝行为不变。
- 实现：`d96cdbf`（W-P7，测试未运行）。

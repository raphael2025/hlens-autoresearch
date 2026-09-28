# ADR-0082：P7 六类组合算子的语义与 Provider lowering

| 字段 | 值 |
|---|---|
| 状态 | **Accepted（interaction；其余五项 OPEN）** |
| 日期 | 2026-09-28 |
| 决策者 | **Codex 依 Raphael 2026-09-28 授权决定** |
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
| `temporal` | EventSpec + EventSpec + 正整数 `window` 与显式 `time_unit` | OPEN。helper 的“第二事件在第一事件后 N bars 内”没有把 bars 绑定到时间轴、bar 规格 / 日历、边界、端点事件可见性；不能把 ADR-0061 的微秒窗口静默当成 bars。 | 名义目标 EventSpec；暂不生成。ADR-0061 的 seq DSL 不被本决定改写或自动复用。 | `time_unit` 没有经单独批准的、可校验时间单位与频率绑定时一律 `operator_open`；没有把它折算成数值 duration 的规则。 |
| `transformation` | FeatureSpec + 枚举 transform 名 | OPEN。现有名称 `standardize`、`rank`、`quantile`、`difference`、`smooth` 不足以定义统计总体 / 横截面、窗口、拟合区间、分位算法、缺值、重复时间、可用时间和训练期状态。Constitution C-L3 另要求标准化 / 分位 / 拟合只用训练窗口数据，但 typed plan 没有可绑定的训练窗口。 | 名义目标 FeatureSpec；暂不生成。不得仅把 transform 名复制进自由文本，就声称已有确定 Provider lowering。 | 六个名称当前均 `operator_open`；即使部分公式看似常见，也不猜默认窗口、算法或样本范围。 |
| `ensemble` | 至少两个不同 StrategySpec 的有序输入集合 | OPEN。旧 helper 文案称 equal-weight vote，Architecture 又包含 weighted signals；均未定义信号到目标仓位的映射、平局、异步信号、风险政策、适用标的与成本处理。StrategySpec.signals 不能引用 StrategySpec。 | 名义目标 StrategySpec；现有规格不能表达这些策略成员及投票语义，不生成。 | 所有 ensemble lowering 均 `operator_open`；不把策略 ref 塞入 `signals` 或 `lineage` 伪装成组合策略。 |
| `negation` | StrategySpec | OPEN。`inverse` 未确定是信号反号、目标仓位取反还是交易动作反向；也未决定现金 / 杠杆 / 风险政策 / 成本 / 对照资格。 | 名义目标 StrategySpec；无无损的既有规格编码，不生成。 | 一律 `operator_open`；不得据策略信号自行构造反向仓位或声称是 control。 |

### 3. Interaction 的约束和确定性

- 如果 Feature 的两输入来自同一 plan 中更早的节点，lowering 使用该节点刚产生的 FeatureSpec；否则必须来自被解析并 hash 核验的直接引用。AST 顺序及输入顺序保持原样。
- 两输入的可见性由既有 FeatureProvider / runner 协议提供；P7 语义不放宽 `available_time ≤ evaluation_time`、`knowledge_time ≤ knowledge_cutoff` 或 Outcome 禁止输入规则。依赖闭包与物化数据真实性仍由 Registry / Runner / 验证服务负责。
- 输入对齐要求两个来源在同一 evaluation time 各至多有一个值。缺少任一侧值时输出缺失；实现不得生成额外时间点。两边存在不一致时间集合时，Provider 必须在完整请求层 fail closed，不能静默取交集后继续。
- 输出身份由 operator semantic key、节点 payload、输入顺序的 ref 和各输入重算 content hash、规范化 UTC `created_at` 派生；完整 SHA-256 用于规格名，避免截断身份冲突。相同 plan、相同解析规格和相同显式 `created_at` 必须给出相同 ref 与内容 hash；不同创建时刻不会复用同一 ref 并形成冲突载荷。
- 乘积值使用精确十进制语义；不得通过 binary float 中转或按某个固定 scale 静默舍入。是否需要额外精度 / 资源上限属 Provider 实现审阅事项，未满足时 Provider 拒绝请求。

### 4. 验收边界

本 ADR 的实现验收仅涉及纯规格 lowering：可解析直接引用、输入 ref/hash 复核、输出身份确定、FeatureSpec 输入 / lineage 与参数完整、OPEN 算子整体 fail closed，以及后续 ADR-0078 producer 可接受完整 node map。代码和测试未因此获得运行授权。具体 Product Provider、执行器的跨来源时间对齐、运行时数值一致性、Registry 闭包和 admission 接线须在各自边界内另行实现 / 审查；`TypedPlan.runnable` 仍为 False。

## 后果

- interaction 现在能以一个版本化 FeatureSpec 表达，并可进入 ADR-0078 的 node-addressed 完整输出校验流程；它仍不是可执行 Provider。
- 其余五项有逐项、显式的 OPEN 记录，基础 lowering 会拒绝混合了任一 OPEN 节点的整个计划，不产生部分结果。
- 无契约、Schema、Constitution、Profile 数值、验证阈值、`core/` 或执行入口变化。六类旧 Hypothesis helper 的声明性输出保持不变。

## 接受记录（2026-09-28）

Codex 依 Raphael 2026-09-28 授权决定：接受 §2 `interaction` 的 product 语义与 FeatureSpec 声明 lowering；`conditioning`、`temporal`、`transformation`、`ensemble`、`negation` 保持 OPEN。接受只授权纯、non-runnable lowering，不接受 Provider 执行、算子 allowlist 注册或 Research Loop / Runner 接入。`TypedPlan.runnable` 与 `compile_plan` 拒绝行为不变。

## 参考

- [ADR-0068](0068-phase7-typed-operator-plans.md)：闭世界 typed plan 与执行边界。
- [ADR-0073](0073-phase7-plan-admission-recovery.md)：admission 与 TrialLedger 崩溃恢复边界。
- [ADR-0078](0078-p7-lowered-output-completeness.md)：计划节点是 lowered outputs 的权威全集。
- [ADR-0030](0030-feature-provider-contract.md)：FeatureSpec / FeatureProvider 可见输入与确定性契约。
- [ADR-0061](0061-interaction-dsl.md)：独立 Event DSL；不为 P7 `temporal` 提供 bars 映射。
- [docs/research/constitution.md](../research/constitution.md)：特别是 C-L1、C-L2、C-L3 与 C-P。

# ADR-0088: 契约 2.4.0（additive）：组合策略、事件 bar 规格、峰值权益、合成效应与波动率缩放屏障

| 字段 | 值 |
|---|---|
| 状态 | **Accepted**（2026-09-28） |
| 日期 | 2026-09-28 |
| 决策者 | Claude Code（PM），依 Raphael 2026-09-28 授权（CLAUDE.md §0：冻结契约变更经 ADR 由 PM 决定） |
| 起草者 | Claude Code（PM） |
| 相关 Phase | Phase 3 / 4 / 5 / 7 / 9 |
| 影响范围 | Contract（`core/domain/specs.py`、`core/contracts/{strategy,synthetic,outcome}.py`）；Schema；契约文档 |
| 是否破坏兼容 | 否：additive minor，`CONTRACT_SCHEMA_VERSION` 从 2.3.0 升到 2.4.0；所有新字段可选且默认值等于现状，旧 payload 与内容哈希不变 |

## 背景

2026-09-28 的代码补全轮次中，有几处需要在冻结契约上增字段才能继续：

| 缺口 | 来源 | 卡住的内容 |
|---|---|---|
| `EventSpec` 没有 bar 规格 | P7 `temporal` | ADR-0082 无法判断两个输入事件是否用同一种 bar |
| `StrategySpec` 不能引用策略，也不能按状态门控 | P7 `conditioning` / `ensemble` / `negation` | ADR-0082 标为 OPEN |
| `PortfolioState` 没有峰值权益 | `drawdown_control` | IMPL-STRAT 停下 |
| `PlantedEffect` 只有自相关一种效应 | P9 合成市场 | 无法植入其他效应 |
| `OutcomeMethod` 没有波动率缩放屏障 | outcome-library O-2 | 该标签方法无法实现 |

这些都属于 CLAUDE.md H1 的冻结契约范围。Raphael 已于 2026-09-28 授权 PM 经 ADR 决定此类变更。

## 决策

按 ADR-0052 的 minor 版本机制，契约升到 **2.4.0**，只做新增。

1. **`EventSpec.bar_spec: Ref | None = None`**
   - 非空时必须指向 `Kind.REPRESENTATION`，声明事件判定所用的 bar 表示，例如 `representation:canonical_bar_1m@1.0.0`。
   - 默认 `None` 表示未声明，与现状相同。
   - P7 `temporal` 的 lowering 要求两个输入的 `bar_spec` 都非空且相同，否则 `operator_open`。
2. **`StrategySpec.composition: StrategyComposition | None = None`**
   - `StrategyComposition` 是按 `type` 字段判别的联合，共三种：
     - `ConditionedStrategy(type="conditioned", base: Ref[STRATEGY], state: Ref[STATE], state_value: NonEmptyStr)`
       - 状态等于 `state_value` 时持有 base 的目标仓位，否则空仓；
       - 未知或缺失的状态视为空仓；
       - 每个 (base, state, state_value) 计一个 trial。
     - `EnsembleStrategy(type="ensemble", members: tuple[Ref[STRATEGY], ...], rule="equal_weight_mean")`
       - 至少 2 个成员，成员不得重复；
       - 各成员的目标仓位等权平均；
       - 成员的风险政策和适用标的必须一致，沿用 ADR-0069 的规则。
     - `NegatedStrategy(type="negated", base: Ref[STRATEGY])`
       - 目标仓位取反；
       - **不**作为验证负对照（负对照由验证层定义）；
       - 现货下的执行仍受做空成本缺口（ST-4）限制，由 Provider 负责 fail closed。
   - 组合策略的 `signals` 必须覆盖 base 或成员需要的全部信号；`conditioned` 还要加上门控状态。
   - 组合对象不得引用自身；嵌套组合与循环引用的检查由 Registry 在解析时完成。
3. **`PortfolioState.peak_equity: PositiveDecimal | None = None`**
   - 非空时必须 ≥ `equity`。
   - 由回测 / 执行层按已实现的权益路径提供；风控不得自己记忆峰值。
   - `drawdown_control` 在 `equity` 或 `peak_equity` 缺失时 fail closed。
4. **合成效应的判别联合**
   - 现有 `PlantedEffect`（`kind="return_autocorrelation"`）保持不变。
   - 新增两个模型：
     - `VolatilityClusteringEffect(kind="volatility_clustering", omega, alpha, beta)`：GARCH(1,1) 的方差递推；要求 omega > 0、alpha ≥ 0、beta ≥ 0、alpha + beta < 1。
     - `JumpEffect(kind="jump", intensity_per_minute, jump_scale)`：Poisson 跳跃；强度在 (0, 1) 之间，跳幅尺度 > 0；跳幅由种子确定性生成。
   - `SyntheticMarketSpec` 中效应字段的类型放宽为三者的联合。
   - 旧 payload 仍按 `return_autocorrelation` 解析，内容哈希不变。
5. **`OutcomeMethod.VOL_SCALED_TRIPLE_BARRIER`**
   - `OutcomeLabelSpec` 新增两个可选字段：
     - `volatility_feature: Ref[FEATURE] | None`
     - `barrier_multiplier: PositiveDecimal | None`
   - 这两个字段只在该方法下必填，在其他方法下必须为空。
   - 上下屏障 = entry × (1 ± multiplier × 入场时可见的波动率特征值)，垂直屏障 = horizon。
   - 同一 bar 同时触碰两个屏障时，沿用 triple barrier 的保守判定。

本批次**不**包括：

- Profile 的 `confidence_level` 字段：会改变所有已钉定的报告哈希，而且数值属于 D-09；
- 事件研究 CAR 与 meta-labeling：前者需要基准模型规格，后者需要主模型规格。

## 实施要求

- 按 ADR-0052 §4，盘点受 2.4.0 默认信封影响的已登记身份与重放路径；盘点结论写入本 ADR。
- 为新模型和新字段导出 Schema，已有 Schema 除默认信封版本外逐位不变。
- 同步 `docs/architecture/02-domain.md` 与相关契约文档。
- 契约层之外的实现由后续批次完成，本 ADR 只授权契约层：
  - P7 lowering（`research/hypotheses`）；
  - 组合策略 Provider 与 `drawdown_control`（`research/strategies`）；
  - 合成市场生成器（`plugins/synthetic`）；
  - 波动率缩放屏障 Provider（`plugins/outcomes`）；
  - 回测层提供 `peak_equity`（`plugins/backtest`）。

## 备选方案

| 方案 | 为何未选 |
|---|---|
| 组合策略放在 research 层的私有结构里，不进契约 | 组合结果必须作为 StrategySpec 进入 TrialLedger 和验证，契约不能表达就无法计数和审计 |
| 风控自行记忆权益峰值 | 破坏确定性，并可能看到未来数据（IMPL-STRAT 已指出） |
| 为每种合成效应单独建一个 `SyntheticMarketSpec` 变体 | 重复，而且已有生成器接口是单一 spec |

## 后果

- 正面：P7 的五类算子、回撤风控、更丰富的合成效应和波动率缩放标签都有了契约表达，之后实现者只需编码。
- 代价：
  - 契约一天内第二次升 minor，已钉定的哈希与版本断言会在调试阶段集中重钉；
  - 组合策略的执行语义要由后续 Provider 严格实现，并补测试。

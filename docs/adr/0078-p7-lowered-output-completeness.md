# ADR-0078：P7 lowered outputs 的权威集合与完整性

| 字段 | 值 |
|---|---|
| 状态 | **Accepted** |
| 日期 | 2026-09-28 |
| 决策者 | **Codex 依 Raphael 2026-09-28 授权决定** |
| 相关 Phase | Phase 7 — Discovery |
| 影响范围 | `research/hypotheses` 输出 producer / binding validation；不修改 `core/`、Contract、Schema、Constitution、Profile 或阈值 |
| 兼容性 | 旧的 partial binding API 保留原行为与返回值；只有新的 complete API 能声明全集已校验 |

## 背景

ADR-0073 要求 producer 校验 `ExperimentSpec`、`Hypothesis` 与 lowered outputs 的一对一依赖绑定。现有 `validate_experiment_bindings` 只检查调用方传来的输出项和直接依赖 hash；它没有“预期输出全集”，因此空列表或漏项仍可通过。

ADR-0068 定义的 typed AST 是闭世界、拓扑排序的节点集合，并为每个节点声明唯一的名义输出类型；当前没有任何六类算子的已批准运行语义或 Provider lowering。

## 决策

1. **权威全集是每个 `TypedPlan.nodes` 中的全部 operator nodes。** 每个节点恰对应一个 lowered core spec。节点 ID 是输出成员身份；该节点声明的 `PlanOutputType` 决定允许的精确规格类：Event、Feature 或 Strategy。条件策略输出目前没有现有版本化核心规格表示，因此遇到 `conditional_strategy_plan` 必须 fail closed。Plan 根、外部输入引用、Hypothesis 文本、ExperimentSpec 的依赖字典都不是 lowered output 清单。
2. producer 必须以节点 ID 到 core spec 的映射提交 lowering 结果。它核对 map key 与 AST node ID 集合完全相等（缺失和多余均拒绝）、规格类型与节点名义输出精确相等，并重建规范规格副本。输出集合按 AST 节点顺序产出。不得由调用方只提交一个 `LoweredOutputBinding` 子集并把它称为完整。
3. complete binding validator 对每个 ExperimentSpec 要求恰有一个 TypedPlan；每个计划节点恰好有一个 node-addressed output；拒绝重复 / 缺失 / 多余节点、错类型、未知 ExperimentSpec 关联、非 canonical ExperimentSpec / Hypothesis，以及任何输出不是该实验直接依赖或重算内容哈希不等于依赖 hash 的情况。先验证全集，再一次性返回结果；失败时不返回部分绑定。
4. `ExperimentSpec.repro.dependency_hashes` 仍只是实验直接依赖绑定。完整性以 AST 节点清单和 node-addressed producer 结果为准；不声称 dependency map 中其他、与 lowered node 无关的合法实验依赖是“多余输出”，也不校验传递闭包。
5. `validate_experiment_bindings` 作为兼容 API 保留并继续只证明传入项正确，不能用作 outputs 完整性证明。新 producer / complete validator 均为纯函数，不写 admission journal / TrialLedger，不接 Runner、loop 或 operator。
6. 本 ADR 不定义六类算子的业务算法或 Provider lowering。输出集合协议与逐算子执行语义分离；所有六类继续默认不可运行，`TypedPlan.runnable` 保持 `False`。本 ADR 不增加运行入口，不改变既有 `runnable=False` 默认。

## 后果与兼容策略

- 新 API 可以证明：在给定 TypedPlan 下没有漏掉或额外提交节点输出，且每个产物绑定了正确的 ExperimentSpec direct dependency ref/hash。
- 旧调用继续获得旧的部分证据语义；须迁移到 `produce_lowered_output_bindings` + `validate_complete_experiment_bindings` 才能宣称完整性。
- 旧 Hypothesis、ExperimentSpec、TrialLedger、Schema 与内容身份无需迁移或重哈希。
- 真实六类 lowering 仍需逐项语义与 Provider 决策。尤其 conditioning 无当前输出规格，不能通过 producer。

## 验收边界

- 缺 node output、额外 node output、重复 node output、错输出类、未知计划 / 实验关联及错 hash 均 fail closed。
- 无算子因本 ADR 变成 runnable；不运行测试 / Runner / probe，不改 `core/`。

## 接受记录

Codex 依 Raphael 2026-09-28 对本轮所需新 ADR 的明确授权接受本决定。此接受仅冻结 outputs 集合的 producer / 验证边界，不批准任何组合算子语义或运行。

## 修订（2026-09-28，随 ADR-0082 第三次接受记录）

§1 原先规定 `conditional_strategy_plan` 在规格缺失时 fail closed。自契约 2.4.0（ADR-0088）起，它的核心规格为带 `ConditionedStrategy` 组合的 StrategySpec。producer 与完整性校验器要求 conditioning / ensemble / negation 节点的输出带有对应的组合，否则以 `plan_output_composition_mismatch` 拒绝。

# research/experiments

ExperimentSpec 定义与 Runner 编排（06-experiment.md）。

> Phase 6 框架已实现（ADR-0039，FRAMEWORK_IMPLEMENTED / NOT_VALIDATED）：`state_strategy.py` 状态 × 策略分解与条件化 trial 登记。ExperimentSpec Runner 待 P4 / P8 接入。

| 函数 | 内容 |
|---|---|
| `state_strategy_matrix` | 按时刻的收益序列 × 状态（`StateResult` 或时刻 → 标签）→ 每个状态的样本数、均值、胜率、合计，最佳状态占比与其前 k 笔占比（C-R2 的输入，不设阈值） |
| `backtest_returns` | W1 接线：P5 `BacktestResult` 的权益曲线 → 逐 bar 简单收益，键为区间**起点** `t`（`(t, t_next]` 的收益 = `e_next / e_t − 1`，已含该 bar 的费用与滑点）；首根 bar 没有更早的估值点，不归属 |
| `matrix_from_backtest` | W1 接线：P5 回测 + P2 `StateResult` → 矩阵；收益归属 **t 时刻已知**的状态；缺状态评估即拒绝；矩阵记录回测与状态结果的 `result_hash`，`matrix_hash` 绑定全部内容 |
| `register_conditionals` | 每个被研究的条件化变体登记为 `conditioning` 假设（`TrialLedger`，C-T1） |

跨 Phase 冒烟：`tests/research/test_cross_phase_e2e.py`（P9 合成市场 → F4 → P2 → P5 → P6 + P7 登记 → P10 纸面运行；
确定性、无未来函数、逐步输入绑定）。它只检验接线，不是验证；合成结果不支持任何真实市场结论。

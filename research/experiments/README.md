# research/experiments

ExperimentSpec 定义与 Runner 编排（06-experiment.md）。

> Phase 6 框架已实现（ADR-0039，FRAMEWORK_IMPLEMENTED / NOT_VALIDATED）：`state_strategy.py` 状态 × 策略分解与条件化 trial 登记。ExperimentSpec Runner 待 P4 / P8 接入。

| 函数 | 内容 |
|---|---|
| `state_strategy_matrix` | 按时刻的收益序列 × 状态（`StateResult` 或时刻 → 标签）→ 每个状态的样本数、均值、胜率、合计，最佳状态占比与其前 k 笔占比（C-R2 的输入，不设阈值） |
| `backtest_returns` | W1 接线：P5 `BacktestResult` 的权益曲线 → 逐 bar 简单收益，键为区间**起点** `t`（`(t, t_next]` 的收益 = `e_next / e_t − 1`，已含该 bar 的费用与滑点）；首根 bar 没有更早的估值点，不归属 |
| `matrix_from_backtest` | W1 接线：P5 回测 + P2 `StateResult` → 矩阵；收益归属 **t 时刻已知**的状态；缺状态评估即拒绝；矩阵记录回测与状态结果的 `result_hash`，`matrix_hash` 绑定全部内容 |
| `register_conditionals` | 每个被研究的条件化变体登记为 `conditioning` 假设（`TrialLedger`，C-T1） |
| `register_matrix_conditionals` | 预先承诺的单元（2026-09-26，CODE_COMPLETE / DEBUG_PENDING）：单元 = 矩阵所属状态的**声明** `StateSpec.state_space` + 未知状态单元，与调用方选择、与结果无关；全部登记或全部不登记（与账本已有条目内容冲突 → `LedgerError`，什么也不登记；同内容重登记不增加 trial）；声明之外的标签、规格与矩阵状态不符即拒绝。逐单元报告样本支持 `CellSupport`：`min_support` 是必填参数、**无默认值**（缺省即 `TypeError`）；显式 `None` = 未声明阈值 → 所有单元 `no_support_threshold`、不支持；低于阈值 → `below_min_support`，保留并报告，从不丢弃。不产出判定（Validation Profile，P4 / P8）；循环使用下面按试验键控的形式 |
| `register_trial_conditionals` / `trial_conditional_hypotheses` | 循环形式（2026-09-26，CODE_COMPLETE / DEBUG_PENDING；`research/loop` 的 opt-in `ConditionalPlan`）：单元同上，但按**试验的假设**键控（`<父假设名>_given_<状态名>_<标签\|unknown_state>`，父假设的版本与族，`origin_refs` 追加父假设），同一策略的不同参数点 / 后代各有自己的单元；每次观察每个单元一个 trial：`attempt=None`（父假设首次试验）登记单元，`attempt=<key>`（父假设的重新评估）以同一 key 为每个单元 `register_reevaluation`，且要求单元的首次观察已登记（否则 `LedgerError`，什么也不登记）。`parent` / `attempt` / `state_spec` / `minimum_effect` / `min_support` 均为必填关键字参数；全部或全不；同一观察再登记幂等。本模块不做逐单元验证：由循环的 `ValidationStage` 在 `ConditionalPlan.validate_cells=True` 时对有支持的单元运行样本内 G0 – G3（见 `research/loop/README.md`「逐单元验证」） |
| `conditional_hypotheses` | 只由声明状态空间生成上述单元假设（结果之前即可预登记）；标签单元的假设与 `register_conditionals` 对同一标签生成的完全相同 |

跨 Phase 冒烟：`tests/research/test_cross_phase_e2e.py`（P9 合成市场 → F4 → P2 → P5 → P6 + P7 登记 → P10 纸面运行；
确定性、无未来函数、逐步输入绑定）。它只检验接线，不是验证；合成结果不支持任何真实市场结论。

**网格对齐（真实数据冒烟 E2，设计如此）**：矩阵按权益点时间（= bar `interval_end`）归属收益，状态必须恰好在该网格上评估；
ADR-0032 下 bar 收盘后 5 s 才可见，因此在该网格上特征 / 状态 / 策略输入都滞后一根 bar。这是因果、无未来函数的结果，
不是缺陷；"按 t 时刻已知的最近状态归属"尚未实现。

状态：`register_matrix_conditionals` / `conditional_hypotheses` 为 CODE_COMPLETE / DEBUG_PENDING（`tests/research/experiments/test_matrix_conditionals.py`）；
`register_trial_conditionals` / `trial_conditional_hypotheses` 为 CODE_COMPLETE / DEBUG_PENDING（`tests/research/experiments/test_trial_conditionals.py`；
循环接线见 `research/loop/README.md`「条件化假设」；决策：Claude，依据 Raphael 2026-09-26 的自主决策指示）。

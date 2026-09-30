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
| `run_inputs`（`run_inputs.py`） | ADR-0100 修订 2（2026-09-30，CODE_COMPLETE / DEBUG_PENDING）：新实验运行在已哈希的 `repro.params` 中以保留键 `hlens.p11.inputs@1.0.0` 记录 P11 同源重算所需输入（规范 JSON 文本；进入 `experiment_hash`，不改 `core/`）：决策网格步长 / 预热期、初始权益，以及验证用的族试验计数、验证 seed、多 seed 对照、`cscv_partitions`、`impact_coefficient`、状态标注器身份。`with_run_inputs` 拒绝已占用该键的参数；`strategy_params` 是去掉记录后的策略参数；`recorded_run_inputs` 严格解析（非规范或无效即 `RunInputsError`），无记录返回 `None`——旧运行不回填、不推断（H3 / H6）。写入方：`research/loop/trials.py`；读取方：`research/operations/authority.py` |
| `conditional_hypotheses` | 只由声明状态空间生成上述单元假设（结果之前即可预登记）；标签单元的假设与 `register_conditionals` 对同一标签生成的完全相同 |

跨 Phase 冒烟：`tests/research/test_cross_phase_e2e.py`（P9 合成市场 → F4 → P2 → P5 → P6 + P7 登记 → P10 纸面运行；
确定性、无未来函数、逐步输入绑定）。它只检验接线，不是验证；合成结果不支持任何真实市场结论。

**归因网格**：矩阵按精确 `evaluation_time` 归属收益；收益时刻没有对应状态评估时即拒绝，不会沿用最近的先前状态。
当前 P6 循环把收益合并到决策时刻，并在同一 `decision_times` 网格评估状态，因此使用精确归属。对不共享评估网格的收益序列，
as-of 最近状态归属尚未实现；改变缺失时刻处理规则需先修订 ADR-0039。真实数据冒烟 E2 中，权益点时间等于 bar `interval_end`；
ADR-0032 下 bar 收盘后 5 s 才可见，因此该网格上的特征 / 状态 / 策略输入滞后一根 bar。这是可用时间约束的结果，不是矩阵匹配规则。

状态：`register_matrix_conditionals` / `conditional_hypotheses` 为 CODE_COMPLETE / DEBUG_PENDING（`tests/research/experiments/test_matrix_conditionals.py`）；
`register_trial_conditionals` / `trial_conditional_hypotheses` 为 CODE_COMPLETE / DEBUG_PENDING（`tests/research/experiments/test_trial_conditionals.py`；
循环接线见 `research/loop/README.md`「条件化假设」；决策：Claude，依据 Raphael 2026-09-26 的自主决策指示）。

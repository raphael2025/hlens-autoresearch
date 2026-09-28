# ADR-0039: 状态 × 策略研究框架（Phase 6）

| 字段 | 值 |
|---|---|
| 状态 | **Accepted**（2026-09-25） |
| 日期 | 2026-09-25 |
| 决策者 | Claude Code（Opus），依 Raphael 2026-09-25 明确授权（红线除外） |
| 影响范围 | `research/experiments/`（研究代码，H5；无契约变化） |
| 实施状态 | FRAMEWORK_IMPLEMENTED / NOT_VALIDATED |

## 裁决

1. 策略在 `(t, t_next]` 的收益归属于 **t 时刻已知**的状态（`StateResult` 在 t 的取值，只用 t 可见输入）；未知状态单列、不丢弃；
   没有状态评估的时刻即拒绝。
2. 输出每个状态的样本数、均值、胜率、合计，以及"最佳状态占全部盈利的比例"与"最佳状态内前 k 笔的占比"——即 Constitution
   C-R2 的输入；本框架**不**设阈值，判定属 Validation Profile（P4 / P8）。
3. 每个被研究的条件化变体都以 `conditioning` 假设登记进 `TrialLedger`，计入 trial 数（C-T1，roadmap P6 验收）。
4. 策略收益目前以"按时刻的收益序列"这一窄接口输入；接入 P5 回测结果与 P4 / P8 验证后形成完整实验（调试阶段）。

## 实施说明

- **接线（W1，2026-09-25）**：`matrix_from_backtest(strategy, state, BacktestResult, StateResult)` 把 P5 回测的权益曲线转为逐 bar
  简单收益（`backtest_returns`：相邻估值点 `(t, e_t)`、`(t_next, e_next)` → 键 `t` 上的 `e_next / e_t − 1`，已含费用与滑点；首根 bar
  不归属），再调用 `state_strategy_matrix`；归属规则不变（t 时刻已知的状态）。矩阵记录回测与状态结果的 `result_hash`，
  `matrix_hash` 绑定全部内容。裁决 4 的"窄接口"仍保留；P4 / P8 验证接入仍待调试阶段。跨 Phase 冒烟见
  `tests/research/test_cross_phase_e2e.py`。无契约变化、无阈值。

## 修订 1：非共享网格的 as-of 状态归属（Accepted，2026-09-28）

### 决定

1. 保留原有精确对齐行为。只有调用方显式提供正的 `max_state_age` 时才启用 as-of 模式。
2. 每个收益区间起点 `t` 归属到 `evaluation_time <= t` 中时间最近的状态评估；不允许使用晚于 `t` 的状态。
3. 若不存在 `t` 或更早的评估，或 `t - evaluation_time > max_state_age`，归属未知状态 `None`，不拒绝收益、不向后寻找状态。
4. `max_state_age` 是调用方研究设定，必须显式提供且为正时长；不设默认容忍窗口。矩阵身份须记录 `alignment_mode=as_of` 与该窗口；精确对齐矩阵身份与既有报告保持不变。

### 理由与范围

当策略收益网格与状态评估网格不同，精确映射会拒绝整份矩阵。as-of 模式提供可选的因果归属，同时以显式最大状态年龄限制陈旧状态；超窗后进入既有未知状态单元。只改变 `research/experiments/state_strategy.py` 的分析映射，不修改状态 / 策略契约、收益、成本、验证门、数据切分或指标。

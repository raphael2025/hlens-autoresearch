# research/states

市场状态研究工具（Phase 2，[ADR-0035](../../docs/adr/0035-state-provider-contract.md)）。条目登记于
[state-library.md](../../docs/research/state-library.md)。研究代码，**永不直接成为生产代码**（H5）。

> 状态：**FRAMEWORK_IMPLEMENTED / NOT_VALIDATED**（2026-09-25）。框架与冒烟测试已交付；尚未在真实
> Research Dataset 上运行，也未经逐项调试与复核。

## 分工

| 位置 | 内容 |
|---|---|
| `core/contracts/state.py` | `StateProvider` Protocol 与 5 个 DTO（`StateInput`、`StateRequest`、`StateValue`、`StateResult`、`StateProviderDescriptor`）；method 参数编码 `state_method` / `parse_state_method` |
| `infrastructure/state/` | `run_state`（每个评估时刻只把可见输入交给 Provider：`evaluation_time <= t`，训练型再限于 `(t - training_window, t]`）；`state_inputs`（由已回答的 Feature 请求 / 结果构造输入）；`state_table`（Arrow 物化） |
| `plugins/states/` | 首批 Provider：`VolatilityRegimeProvider`、`LiquidityRegimeProvider`（尾随窗口经验分位分桶）、`TrendRangeProvider`（效率比） |
| `research/states/diagnostics.py`（本目录） | 状态分布、持续时间、转移矩阵、标签闪烁（短 run 占比、切换率）；`render_markdown` 报告 |

## 规则

- 输入只能是 Feature 值（`StateInput.feature` 必须是 `kind=feature`）；Outcome 在构造时即被拒绝。
- 训练型状态模型（`training_window` 非空）必须固定 `seed`；执行器结构性地只给出固定尾随窗口内的输入，
  不可能在全样本上拟合。
- 模型参数（分位切点、最少历史、阈值）是规格参数，写在 `StateSpec.method` 中并受 spec hash 绑定；
  诊断报告的 `min_run` 由调用方给出。二者都不是验证阈值。
- 诊断只描述序列，不判定状态"好坏"；任何判定阈值属于 ValidationProfile。

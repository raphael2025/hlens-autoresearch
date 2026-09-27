# Risk Library

| 字段 | 值 |
|---|---|
| 类型 | `risk` |
| 状态 | 框架已实现（Phase 5，ADR-0038）；条目均为 NOT_VALIDATED |
| 首次填充 | Phase 5 |

仓位、止损、敞口、杠杆与组合层面的风控方法（RiskPolicy）。

## 条目字段（草案，以 `core/contracts` 为准）

| 字段 | 说明 |
|---|---|
| `name@version` | 唯一标识 |
| `type` | sizing / stop / exposure / drawdown / correlation / liquidity |
| `rules` | 规则定义 |
| `params` | 参数 |
| `source` | 出处 |
| `applies_to` | 适用策略类型 |
| `lifecycle_state` | 状态 |
| `validation_reports` | 报告引用 |

## 通用规则

- 条目登记 ≠ 条目有效。所有条目默认状态为 `IDEA` 或 `UNVERIFIED`，只有 Lifecycle 状态能说明其是否经过验证。
- 每个条目必须有出处（`source`）；来源为本系统实验时引用 ExperimentRun ID。
- 已发布版本不可修改；修改 = 新版本。
- 被拒绝 / 失败的条目**不删除**，其状态更新并在 [failure-registry.md](failure-registry.md) 中登记。
- 本 Markdown 是人类可读索引；Phase 0 之后，权威数据存放在 Control Plane Registry 中，本文件由其生成或与其同步。

## 条目索引

| name@version | 摘要 | 出处 | 状态 |
|---|---|---|---|
| `vol_target_bars@1.0.0` | sizing / exposure：按顺序执行 `missing_volatility_flat`（无可见正波动率估计 → 空仓）、`volatility_scaling`（权重 × `target_volatility / volatility`）、`leverage_cap`（缩放倍数 ≤ `max_leverage`）、`position_cap`（\|权重\| ≤ `max_abs_weight`）、`gross_exposure_cap`（总敞口超限时按比例向零缩小）。波动率信号默认 `feature:bar_realized_vol_60@1.0.0`（`available_time <= t` 的最新一条）；声明的参数空间 `target_volatility ∈ {0.0025, 0.005, 0.01}`（信号同单位）、`max_leverage ∈ {1, 2}` | `knowledge:risk_volatility_managed_portfolios@1.0.0`、`knowledge:risk_volatility_managed_portfolios_out_of_sample@1.0.0` | NOT_VALIDATED |

实现：`research/strategies/volatility_target.py`（研究代码，未晋升；`risk/` 为空）。每个被调整的仓位都在 `binding_rules` 中写明
生效的规则名；参数是风控参数，不是验证阈值。`RiskPolicy` 契约没有参数空间字段，空间在模块 `VOL_TARGET_PARAM_SPACE` 中声明。

# apps/execution

独立执行服务（roadmap Phase 13），**仅模拟**。见 [ADR-0046](../../docs/adr/0046-simulated-execution-service.md)。

> FRAMEWORK_IMPLEMENTED / NOT_VALIDATED。

## 红线

- 没有真实场所、没有凭据、没有网络 I/O；唯一场所是进程内 `SimulatedVenue`。
- `ExecutionMode.LIVE` 被拒绝；阶梯中 PAPER 之后的各级需要 `AuthorizationRecord`，且在本构建中总是被拒绝并记录。
- 风险数值（资金、敞口、杠杆、亏损、回撤阈值、费率）全部由操作者注入，没有默认值。
- 不 import `research/`、`infrastructure/` 或任何网络库（`tests/test_architecture_boundaries.py`）。

## 组成

| 模块 | 内容 |
|---|---|
| `records.py` | 不可变、内容寻址的记录：`TargetPositions`、`OrderRecord`、`FillRecord`、`RejectionRecord`、`KillSwitchTrip`、`Alert`、`LadderGateRecord` |
| `venue.py` | `SimulatedVenue`（确定性全额成交）、`CostModel` / `LinearCostModel` |
| `service.py` | `ExecutionService`（准入 `DeploymentRecord`、目标仓位 → 订单、发布事件）、`TargetPositionSource`（Phase 5 接入点） |
| `strategy_source.py` | `StrategyProviderTargetSource`：把 `core.contracts.strategy.StrategyProvider`（可选 `RiskProvider`）+ 信号源包成 `TargetPositionSource`（Phase 5 → Phase 13 接线适配器，见下） |
| `kill_switch.py` / `drill.py` | `KillSwitch`（无 reset）与 `run_kill_switch_drill` |
| `risk.py` | `SecondLineRisk`、`RiskLimits`（可从 `RiskPolicy.params` 读取） |
| `monitor.py` | `Monitor`：PnL、持仓、回撤、风险越限告警与 alert hook |
| `ladder.py` | `ExecutionLadder`：SIMULATED → PAPER（记录的门）；live 级一律拒绝 |
| `audit.py` / `book.py` | 只追加审计轨迹；按成交的平均成本仓位簿 |

## 接入 Phase 5

生产策略运行时为一个已晋升 Artifact 的部署实现 `TargetPositionSource.target_positions(deployment_id, as_of)`，
返回 `TargetPositions`（每个标的的有符号目标数量）；执行服务只接收目标仓位，不接收订单。

`strategy_source.StrategyProviderTargetSource` 是这条接缝已接上的适配器：包一个 `StrategyProvider`（可选
`RiskProvider`）与一个信号源 callable，构造 `StrategyRequest` 时 `knowledge_cutoff = as_of`、只保留
`available_time <= as_of` 且 `knowledge_time <= as_of` 的信号，答案未通过 `StrategyResult.check_answers`
（或 `RiskResult.check_answers`）即 fail closed（`StrategySourceRefused`）。**v1 简化**：把
`target_weight`（含风控约束后）直接映射为 `TargetPosition.quantity`——真正的仓位定量
（`quantity = weight * equity / price`）留给调试批次，见 ADR-0038 / ADR-0046 的 Implementation note。

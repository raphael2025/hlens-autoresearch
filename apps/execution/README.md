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
（或 `RiskResult.check_answers`）即 fail closed（`StrategySourceRefused`）。定量由必填的 `PositionSizer` 完成（v1 `EquityPriceSizer`：`quantity = weight * equity / price`，价格须在 `as_of` 已知，缺失或非正即拒绝；无"权重即数量"的默认），见 ADR-0038 / ADR-0046 的 Implementation note。

## 持久审计与重启（2026-09-26，CODE_COMPLETE / DEBUG_PENDING）

- `AuditTrail(path)`：可选持久审计。每条记录先以 `{"record_id", "record"}`（类型名为行类型）追加进哈希链 JSON-lines 日志
  （`apps.worker.journal.AppendOnlyJournal`，fsync），再放入内存；重新打开时整条链重放校验，记录重新校验且内容哈希必须等于存储的
  `record_id`。断链、半行、未知类型、id 不符、重复一律 `AuditCorrupted`，不跳过、不修复。不给 `path` 时行为与此前逐字节相同。
- `ExecutionService(..., audit=AuditTrail(path))`：在**非空**持久审计上重新打开服务是 fail closed——场所持仓、风险簿、监控与已准入部署
  **不**从审计重建，因此新实例立即触发 Kill Switch（记录一条 `tripped_by = RESTORE_TRIPPED_BY` 的 `KillSwitchTrip`），任何订单都在场所前被拒绝；
  订单序号接续而不是从 0 重来。恢复下单需要人选择新的审计路径（与"无 reset、恢复要人新建实例"一致）。
- `replay_audit(path)`：只读证据——从持久审计重建各部署的持仓与费用、不完整订单、Kill Switch 是否触发与链头；每笔成交必须对应同部署、同标的、同方向、
  同数量的已记录订单，否则 `AuditCorrupted`。从不重新执行任何东西。
- 已知边界：整行删除文件尾部仍是合法的更短链，只有外部保存的 `head_hash` 能发现（与 worker 日志相同）。
- 测试：`tests/apps/test_execution_durable_audit.py`（10 项：内存与持久等价、重放持持仓 / 费用、重开即停且不成交、空审计不触发、篡改 / 半行 / id 不符 /
  未知类型 / 重复 / 无订单的成交均拒绝、默认内存行为不变）。


## 风险与告警重放（2026-09-26，CODE_COMPLETE / DEBUG_PENDING）

- `MarkRecord`（`records.py`，应用层记录，非冻结领域契约）：`ExecutionService(..., record_marks=True)` 时，每批 `submit_targets` 的全部价格
  （按键排序）在价格校验之后、施加到监控之前写入审计并发布到 `execution.mark`。**持久审计（`AuditTrail(path)`）必须显式给出
  `record_marks=True/False`**，省略即抛 `MarksChoiceRequired`（没有 mark 的持久审计无法做风险重放，这必须是决定而不是默认值）；
  内存审计仍默认 `record_marks=False`。`record_marks=False` 时写入的记录与此前逐条相同，
  既有审计 head 不变（`test_marks_are_opt_in_and_the_default_audit_head_is_unchanged` 同时钉住旧/新：开启后仅多出 `MarkRecord`，其余记录与时间戳不变）。
- `risk_replay.replay_risk(path | AuditTrail, limits, *, max_drawdown, monitor_capital=None)`：用调用者声明的限额与监控参数新建
  `SecondLineRisk` / `Monitor`，按审计顺序重跑标价、订单、成交与 Kill Switch 触发，要求每条已记录的拒绝、每笔成交对应的接受、每条告警都被逐字段复现
  （时间取自审计：时间是输入，不是决策）；多出、缺失或决策不同即 `RiskReplayDiverged`（`AuditCorrupted` 子类，`.record` / `.index` 指向第一条分歧记录）。
  `RESTORE_TRIPPED_BY` 触发开始新会话（风险簿与监控重新开始，假定参数相同）。有订单而无 `MarkRecord` 的审计直接拒绝。无终态记录的订单照常重算以保持状态，但列为未验证。
- 仅模拟、只读证据；不下单、无网络。测试：`tests/apps/test_execution_risk_replay.py`。

## 风险 / 告警重放（2026-09-26，B31，CODE_COMPLETE / DEBUG_PENDING）

| 模块 | 内容 |
|---|---|
| `records.py` 的 `MarkRecord` / `MarkPrice` | apps 本地的价格标记记录；`ExecutionService(record_marks=True)` 时每批目标写入（内存审计默认不写；持久审计必须显式选择；`False` 时既有审计头不变） |
| `risk_replay.py` | `replay_risk(path \| AuditTrail, limits, *, max_drawdown, monitor_capital=None)`：按审计顺序以新的二线风控与监控重放，逐字段复现每条拒绝、每笔成交与每条告警；分歧 → `RiskReplayDiverged`（指出首个分歧记录）；有订单无标记的审计被拒 |

测试：`tests/apps/test_execution_risk_replay.py`、`tests/apps/test_execution_marks_required.py`。仍只模拟；目前没有组件默认开启 `record_marks`。


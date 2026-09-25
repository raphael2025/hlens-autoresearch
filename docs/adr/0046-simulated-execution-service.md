# ADR-0046: 独立执行服务（仅模拟）、Kill Switch、二道风控与执行阶梯（Phase 13 框架）

| 字段 | 值 |
|---|---|
| 状态 | **Accepted**（2026-09-25） |
| 日期 | 2026-09-25 |
| 决策者 | Claude Code（Opus），依 Raphael 2026-09-25 明确授权（红线除外） |
| 相关 Phase | Phase 13（框架；仅模拟，不含任何实盘能力） |
| 影响范围 | 新增 `apps/execution/`（独立应用）；`tests/test_architecture_boundaries.py` 增加执行服务导入红线；不改任何 `core/` 契约（Schema 数不变） |
| 实施状态 | FRAMEWORK_IMPLEMENTED / NOT_VALIDATED |
| 前置 | [ADR-0005](0005-research-production-boundary.md)、[ADR-0006](0006-strategy-lifecycle.md)、[ADR-0011](0011-lifecycle-subject-authorization-and-time.md)、[ADR-0022](0022-phase1-market-and-execution-scope.md) §7、[ADR-0044](0044-event-bus-and-worker-jobs.md) |

## 红线（本 ADR 不可放宽）

Raphael **未授权**实盘交易（CLAUDE.md H10、ADR-0022 §7）。本构建：

- **没有**任何交易所下单 / 账户端点、API key 或凭据，**没有**任何网络 I/O；唯一的执行场所是进程内 `SimulatedVenue`。
- `ExecutionService` 拒绝 `ExecutionMode.LIVE`，也拒绝任何不是 `SimulatedVenue` 本身的场所（子类同样拒绝）；`OrderRecord` 的校验器拒绝 LIVE 模式与 live 阶梯。
- 阶梯中 PAPER 之后的各级（小额实盘、扩容）只以**记录**形式存在：需要 `AuthorizationRecord`，且在本构建中**无论是否提供都拒绝**，拒绝本身被记录；从不自动授予。
- 风险预算、杠杆与亏损上限等**数值没有默认值**：只能来自操作者提供的 `RiskLimits` 或 `RiskPolicy.params`；本 ADR 不提出任何数值。
- 放开任何一条都需要 Raphael 的明确授权记录（venue、账户、最大资金、杠杆、单笔 / 单日损失上限、kill switch，ADR-0022 §7）与新的 ADR。

## 裁决

1. **位置与边界**：执行服务是独立应用 `apps/execution/`，不 import `research/`、`infrastructure/` 或任何网络库（边界测试静态检查）；
   研究平面无法直连。策略输出唯一的入口是窄接口 `TargetPositionSource.target_positions(deployment_id, as_of) -> TargetPositions`
   （每个标的的目标仓位，不是订单）；Phase 5（ADR-0038）的 Strategy / Risk Provider 由 lead 接到这个接口。
2. **准入**（ADR-0005 §4）：`ExecutionService.admit(deployment, artifact, lifecycle)` 只接受 `DeploymentRecord`（其校验器保证 Equivalence Gate 已通过、
   身份一致）、与之 `artifact_id` 一致的 `StrategyArtifact`，以及主体属于该 Artifact / 其策略、当前状态为 PAPER 或 ACTIVE 的生命周期历史。
   Registry 是否登记由未来 Control Plane 核验，本框架不自证。
3. **流程**：目标仓位 → 与场所持仓的差额 → `OrderRecord`（**先记录**）→ Kill Switch → 二道风控 → `SimulatedVenue` 成交 → `FillRecord`。
   每个订单恰有一条终结记录（成交或拒绝）。全部记录进入只追加 `AuditTrail`，并发布到事件总线（`EventBusAdapter`，ADR-0044）的
   `execution.order / fill / rejection / kill_switch / alert / ladder` 主题。
4. **记录**：均为冻结 `Contract`（禁止额外字段、拒绝 NaN / ∞），`record_id` = 规范 JSON 的 SHA-256；金额与数量用 `Decimal`；时间来自注入的时钟。
   相同输入的重放逐位相同（审计头哈希相同）。这些是应用层记录，不进 `core/contracts/registry`，不属于冻结契约。
5. **SimulatedVenue**：按给定价格全额成交；成交价与费用来自注入的 `CostModel`（`LinearCostModel` 的两个费率为必填参数）。
6. **KillSwitch**：一旦触发，此后所有订单在到达场所前被拒绝并记录；每次触发都记录并发布；**没有 reset**——恢复需要人新建服务实例。
   `run_kill_switch_drill` 是演练：触发 → 推送目标 → 核验零成交、全部被 Kill Switch 拒绝、触发已审计、审计完整。
7. **SecondLineRisk**：独立于策略 RiskProvider 的事前检查，自己按成交维护仓位簿；只拦截**增加**总敞口的订单
   （保证越限后仍可降风险），依次检查亏损上限、总敞口上限、杠杆上限（权益 ≤ 0 视为越限）；每次拒绝都记录。杠杆上限必须有限。
8. **Monitor**：按成交与标记价维护 PnL、持仓、回撤；回撤达到注入阈值时产生告警（每次越限一次），二道风控拒绝与 Kill Switch 触发也产生告警；
   告警交给注册的 hook（例如 hook 可以触发 Kill Switch）。
9. **ExecutionLadder**：SIMULATED → PAPER 需要具名决定人与至少一项证据，记录为 granted；PAPER 之后的任一级请求都记录为 refused 并抛
   `LiveExecutionRefused`（有无 `AuthorizationRecord` 都拒绝，提示不同）；`LadderGateRecord` 的校验器使 live 级的 granted 记录无法构造。

## 后果

- 正面：roadmap Phase 13 的"所有订单可审计""Kill Switch 演练"在模拟层可执行；Phase 5 接入点清晰且窄。
- 负面：没有持久化（审计与总线都在进程内）；没有部分成交、盘口、延迟、资金费率等更真实的模拟；PAPER 与 SIMULATED 目前使用同一模拟场所，
  区别只在记录的阶梯等级；多部署共享一个账户级风控簿。
- 未决：真实 venue、凭据管理、实盘授权服务、实时行情接入、持久审计存储——全部需要 Raphael 授权与新 ADR。

## Implementation note（wiring，2026-09-25）

第 1 条裁决中"Phase 5 的 Strategy / Risk Provider 由 lead 接到这个接口"已完成：新增
`apps/execution/strategy_source.py`（`StrategyProviderTargetSource` / `StrategySourceRefused`），只
import `core.contracts.strategy` / `core.domain` / `apps.execution`——不 import `research/` 或
`infrastructure/`（`tests/test_architecture_boundaries.py` 的执行服务红线覆盖本文件）。适配器的行为、
已知的 v1 简化（`target_weight` 直接映射为 `quantity`，真正定量留给调试批次）与测试见 ADR-0038 的同一条
记录；红线（第 5 条 LIVE 拒绝、第 6 条 Kill Switch）在新增的端到端测试
（`tests/apps/test_execution_strategy_source.py`）中重新断言，均未放宽。

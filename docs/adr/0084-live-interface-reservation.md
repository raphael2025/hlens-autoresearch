# ADR-0084：实盘接口预留（默认关闭）

| 字段 | 值 |
|---|---|
| 状态 | **Accepted**（Claude PM 依 Raphael 2026-09-28 授权决定） |
| 日期 | 2026-09-28 |
| 决策者 | Claude PM（依 Raphael 2026-09-28 授权，实盘操作本身除外），实现者 Claude Code (L1) |
| 相关 Phase | Phase 13（框架；仍然仅模拟，不含任何实盘能力） |
| 影响范围 | 新增 `apps/execution/live_venue.py`；`apps/execution/records.py` 新增应用层记录 `LiveAccessAttempt`；`apps/execution/ladder.py` 的拒绝信息补充本 ADR 引用；不改 `core/` 契约（Schema 数不变）、不改 Validation Constitution |
| 前置 | [ADR-0046](0046-simulated-execution-service.md)、[ADR-0006](0006-strategy-lifecycle.md) §2、[ADR-0011](0011-lifecycle-subject-authorization-and-time.md) D-17.3、[ADR-0022](0022-phase1-market-and-execution-scope.md) §7 |
| 兼容性 | 纯新增；不改变任何既有记录、事件主题或既有测试断言的字符串前缀 |

## 背景

Raphael 于 2026-09-28 指示：预留实盘接口，但目前不进行任何实盘操作。ADR-0046 已经建立了"仅模拟"的执行服务与红线（H9、H10），但尚未给未来的真实 venue 适配器与凭据来源定义一个**稳定的接口形状**。没有这个形状，Phase 5 之后要接入真实 venue 时，第一步永远是"现改契约"；提前声明接口可以让将来的适配器只需实现一个 Protocol，而不必触碰执行服务的其余部分。

本 ADR **不**引入任何网络能力、任何交易所 SDK、任何凭据读取逻辑，也不改变 ADR-0046 的任何红线。

## 决策

1. **`LiveVenuePort`**（`apps/execution/live_venue.py`，`typing.Protocol` + `@runtime_checkable`）：声明下单（`place_order`）、撤单（`cancel_order`）、查询持仓（`positions`）、查询成交（`fills`）、心跳（`heartbeat`）五个方法，签名风格与既有 `apps.execution.venue.SimulatedVenue` 对齐（`OrderRecord` / `FillRecord` / `Decimal` / 注入的 `datetime`）。这是**声明**，不是实现。
2. **`CredentialProvider`**（同文件，同样是 Protocol）：只声明 `identity()` / `is_available()` 两个方法形状。本构建中，这个 Protocol **没有任何实现、没有任何调用方**，模块本身不读取任何环境变量、文件或密钥来源（H9）。它存在的唯一目的，是让未来真实适配器的构造函数有一个可以类型标注的凭据参数。
3. **`UnconfiguredLiveVenue`**：本构建中 `LiveVenuePort` 的唯一实现。每个方法在返回前，先向自己的内存历史追加一条新的应用层记录 `LiveAccessAttempt`（`apps/execution/records.py`：`sequence`、`method`、`deployment_id`、`reason`、`attempted_at`，内容寻址、不可变），再抛出既有的 `LiveExecutionRefused`（`apps/execution/errors.py`，未新增异常类型）。可选 `on_record` 回调（与 `ExecutionLadder` 相同的既有约定）允许调用方把尝试接入自己的审计汇。
4. **`LiveVenueRegistry`**：登记表。`register(name, venue_cls)` 只接受满足 `issubclass(venue_cls, LiveVenuePort)` 的类，否则 `TypeError`；默认已登记 `"unconfigured" -> UnconfiguredLiveVenue`。`build(name, *, subject, authorization, risk_gate, kill_switch)` 是获得适配器实例的唯一入口，要求同时满足：
   - `subject` 一致、窗口有效的 `AuthorizationRecord`（ADR-0006 §2、ADR-0011 D-17.3）；
   - 同一 `subject`、`passed=True` 的 `RiskGateRecord`；
   - 未触发的 `KillSwitch`；
   - 模块级常量 `LIVE_TRADING_ENABLED`。

   `LIVE_TRADING_ENABLED` 在本构建中固定为 `False`，是模块常量而不是配置项：`build()` 直接闭包读取这个常量，签名中**没有**任何参数可以覆盖它，模块也不读取任何环境变量。四个条件中只要有一个不满足（本构建中 `LIVE_TRADING_ENABLED` 恒为 `False`，因此**总是**不满足）就拒绝，原因逐条列出并附加本 ADR 引用。即使调用方登记了一个结构上满足 `LiveVenuePort` 的类、提供了有效授权、通过的 Risk Gate、未跳闸的 Kill Switch，`build()` 依然拒绝——这是设计的一部分，不是遗漏。
5. **`ExecutionLadder.request_live`**：行为不变（PAPER 之后的每一级请求，无论是否提供 `AuthorizationRecord`，都一律拒绝并先记录后抛出 `LiveExecutionRefused`）；拒绝信息 `LIVE_REFUSAL_MESSAGE` 更新为同时引用 ADR-0046 与本 ADR。
6. 不新增任何依赖；`apps/execution` 仍不 import `research/`、`infrastructure/` 或任何网络 / 交易所库（`tests/test_architecture_boundaries.py` 覆盖整个目录，天然覆盖新文件；新增测试另行在 `tests/apps/` 下独立静态自检本 ADR 涉及的文件）。

## 备选方案

| 方案 | 优点 | 缺点 | 为何未选 |
|---|---|---|---|
| A. 不预留接口，等真正授权实盘时再设计 | 最小改动，零提前设计风险 | Raphael 已明确要求现在预留；届时会阻塞真实接入的第一步 | 不满足本次任务目标 |
| B. 预留接口，同时给出一个占位真实适配器骨架（未接线） | 未来接入时可少写一点样板 | 容易被误用为"已经有真实适配器"，且没有真实 venue 可验证其正确性，H10 风险高 | 骨架无法验证，且与"不写任何真实交易所适配器"的硬约束冲突 |
| C（已选）. 只声明 Protocol + 唯一拒绝实现 + 登记表，四重门禁全部闭合在模块常量上 | 符合 Raphael"预留但不启用"的原意；没有真实适配器可写就不写；`LIVE_TRADING_ENABLED` 结构上不可被参数/配置/环境变量绕过 | 登记表的 `build()` 末尾 `venue_cls()` 假设无参构造，真实适配器的构造注入（含 `CredentialProvider`）留给未来 ADR 设计 | 在"预留但不实现"的范围内是最小、最诚实的形状 |

## 后果

- 正面：未来接入真实 venue 时，Phase 5 之后的代码可以先针对 `LiveVenuePort` 类型标注；`LiveVenueRegistry` 给出了"四重门禁 + 模块常量开关"的统一收口点，避免每个未来适配器各自实现自己的授权检查。
- 负面 / 代价：`LiveVenueRegistry.build()` 对已登记类的构造目前是无参 `venue_cls()`，真实适配器如何注入 `CredentialProvider` 与连接参数未定义——这是有意留白，交给放开 `LIVE_TRADING_ENABLED` 时的新 ADR。
- 未决：真实 venue 适配器实现、凭据管理与读取方式、构建时开关如何被合法翻转（新构建 + 新 ADR + Raphael 授权）、`CredentialProvider` 的具体实现与存储位置——全部需要 Raphael 未来的明确授权与新 ADR，本 ADR 不预先回答。
- 对复现性的影响：无——不涉及任何回测、实验或数据路径。

## 合规检查

- [x] 不破坏已冻结契约；`LiveAccessAttempt` 是应用层记录（与 `OrderRecord` 等同类），不进 `core/contracts/registry`
- [x] 不修改 Validation Constitution / Validation Profile
- [x] Domain 层无新依赖；`apps/execution` 仍不 import 网络 / 交易所 SDK
- [x] Research / Application Plane 边界不变（本 ADR 不涉及 `research/`）
- [x] 不读取、不存储任何密钥或账户信息（H9）；不下单、不连接实盘（H10）
- [x] `LIVE_TRADING_ENABLED` 为模块常量 `False`，无配置 / 环境变量 / 参数可覆盖（见 `tests/apps/test_execution_live_venue.py`）

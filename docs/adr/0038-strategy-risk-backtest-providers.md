# ADR-0038: Strategy / Risk / Backtest Provider 契约、回测器 v1 与研究策略库（Phase 5）

| 字段 | 值 |
|---|---|
| 状态 | **Accepted**（2026-09-25） |
| 日期 | 2026-09-25 |
| 决策者 | Claude Code（Opus），依 Raphael 2026-09-25 明确授权（红线除外） |
| 相关 Phase | Phase 5 — Strategy Library（依 Raphael 2026-09-25 全阶段框架实现指示） |
| 影响范围 | Contract（`core/contracts/strategy.py`，additive）、`plugins/backtest/`、`research/strategies/`、`tests/contract_suites/{strategy,risk,backtest}.py` |
| 是否破坏兼容 | 否：只新增 17 个模型与 3 个 Protocol；Schema 87 → 104，`CONTRACT_SCHEMA_VERSION` 不变，`StrategySpec` / `RiskPolicy` 与既有 Schema 逐字节不变 |
| 实施状态 | FRAMEWORK_IMPLEMENTED / NOT_VALIDATED |

## 背景

05-plugin.md §3 冻结了 StrategyProvider（信号 → 目标仓位）、RiskProvider（目标仓位 + 组合状态 → 约束后仓位）与
BacktestProvider（仓位 + 价格 + 成本模型 → 成交、PnL）的概念语义；ADR-0017 要求其可执行 Protocol、DTO 与 provider-agnostic
contract suite 在首次消费它的 Phase（Phase 5）之前交付。roadmap Phase 5 要求每个策略有出处、参数空间声明与验证报告，
失败策略全部入 Failure Registry，回测通过基准一致性测试；`strategies/`、`risk/` 只放已晋升者（ADR-0005）。

## 裁决

1. **契约**（`core/contracts/strategy.py`，以 `feature.py` 为模板；全部确定性、全部 `Decimal`，浮点与 NaN / ±Infinity 拒绝）：
   - `StrategyProvider.target_positions(StrategyRequest) → StrategyResult`。请求绑定 `strategy`（`kind=strategy`）+ `spec_hash`、
     本次试验的 `params`、`instruments`、`decision_times`、`knowledge_cutoff` 与 `SignalObservation`（只能是 feature / state / event，
     Outcome 永不作为输入）。`visible_at(t)` = `available_time <= t`，同键取最晚可用的一条。`TargetPosition` 零输入必须为 0
     （无信息不持仓）、输入时间不晚于决策时刻；`check_answers` 核对一一对应与输入时间属于可见集合。
   - `RiskProvider.constrain(RiskRequest) → RiskResult`：请求只描述**一个**决策时刻，任何 `available_time > decision_time` 的信号、
     晚于决策时刻的 `PortfolioState` 在构造时即被拒绝（结构性无未来函数）。`ConstrainedPosition.binding_rules` 为空当且仅当仓位未被
     调整——任何调整都必须有名可查。`PortfolioState.equity` 可为空（风控先于模拟运行时），依赖路径的规则必须 fail closed。
   - `BacktestProvider.run(BacktestRequest) → BacktestResult`：v1 唯一执行模型 `next_bar_open`（决策 `t` 在该标的第一根
     `interval_start >= t` 的 bar 开盘成交）；`BacktestCostModel` = 费率 + 不利滑点率（`[0, 1)` 小数，引用为 `cost_model:name@version`）；
     `Fill.fill_time >= decision_time` 是构造不变量；descriptor 的 `deterministic`、`simulation_only` 只能为 `true`。
2. **回测器 v1**：`plugins/backtest/BarBacktester`——按 bar 时间戳分组，同一时刻的全部目标按同一笔成交前权益定量；货币量量化到
   `1e-18`；**只是模拟**，无下单、无密钥、无网络。基准一致性由 contract suite 检查：零成本买入持有 = 价格比；有成本买入持有 =
   闭式解；零仓位 PnL 恰为 0；平价往返恰好亏掉费用 + 滑点；改变 `t` 之后的 bar 不改变 `t` 之前的权益。
3. **研究策略库**（`research/strategies/`，研究代码，永不直接成为生产代码）：`tsmom_bars@1.0.0` 与 `tsmom_bars_vol_scaled@1.0.0`
   （时间序列动量，lineage = 知识库 `strategy_time_series_momentum`、`strategy_crypto_time_series_momentum`）；
   `vol_target_bars@1.0.0` 风控（lineage = `risk_volatility_managed_portfolios` 及其样本外条目）。参数空间在 spec 中声明，
   Provider 拒绝空间外的参数点（trial count 的来源，C-T1）；`RiskPolicy` 无参数空间字段，其空间在模块中声明（不改 Domain，H1）。
4. **验证接缝**：`research/strategies/validation.py` 的 `BacktestValidator` 是本地窄接口（`validate(subject, spec, backtest) →
   BacktestValidation`：`ValidationReport` + FAIL 时的 `ReasonCode`）。**本 ADR 不含任何门、阈值或 Profile 数值**；Phase 4 流水线
   （`research/validation`，ADR-0037）由 lead 在 `TODO(phase4-wiring)` 处接入。未接入前一切评估为 `NOT_VALIDATED`，不是通过。
5. **Failure Registry 写路径**：`research/strategies/failure_registry.py` 以 JSON Lines 追加 `FailureRecord`；无删除 / 改写接口，
   文件变短即 fail closed。验证 FAIL → `REJECTED`（附报告、run、回测结果哈希与失败门）；运行 / 契约错误 → `FAILED`。
6. `strategies/`、`risk/` 保持只有 README：没有任何策略已验证或晋升（ADR-0005；架构边界测试禁止其 import `research`）。

## 后果

- 正面：三类 Provider 可被任意实现替换，并由同一套 contract suite 检查因果性、确定性与回测基准一致性；每个策略从知识条目到
  失败记录都有可追溯链。
- 负面：执行模型单一（下一根开盘、无部分成交、无融资成本、无容量 / 冲击），结论偏乐观的风险由 Phase 8 的成本压力与容量门处理；
  `check_answers` 与研究策略的可见集合计算为 O(n·T)，大样本需在 Phase 6 前优化；研究信号辅助函数与 `FeatureProvider` 定义重复，
  生产路径应改由 FeatureProvider runner 产出信号。
- 延期：每个策略的 ValidationReport（依赖 Phase 4 流水线接入）；Control Plane 上的权威 Failure Registry 与生命周期转移；
  Promotion（ADR-0005）。

## Implementation note (signal adapters, 2026-09-25)

上游结果 → 策略信号的可复用适配器在 `infrastructure/strategy/signals.py`（`signals_from_features` /
`signals_from_states` / `signals_from_events`）：评估于 `t` 的 Feature / State 值与在 `t` 可观测的 Event 在 `t` 可用；
`knowledge_time` 必须由调用方显式给出（上游运行的知识截止），早于结果时刻即拒绝；`None` 原样保留（不填补）；
引用种类不符即拒绝，Outcome 由 `SignalObservation` 自身拒绝（C-L2）。测试：`tests/infrastructure/strategy/test_signals.py`。
状态：FRAMEWORK_IMPLEMENTED / NOT_VALIDATED。

## Implementation note (wiring P5 → P13, 2026-09-25)

Phase 5 → Phase 13 的接缝已接上（裁决不变；细节与 ADR-0046 的同一条记录一致）：`apps/execution/
strategy_source.py` 新增 `StrategyProviderTargetSource`，把一个 `StrategyProvider`（可选 `RiskProvider`）
包成 `apps.execution.service.TargetPositionSource`。构造 `StrategyRequest` 时 `knowledge_cutoff = as_of`、
`decision_times = (as_of,)`，且信号先按 `available_time <= as_of` 且 `knowledge_time <= as_of` 过滤才进入请求
（不给一个"过于热心"的信号源留下泄漏未来的机会）；答案未通过 `StrategyResult.check_answers`（或接入
`RiskProvider` 时 `RiskResult.check_answers`）即 fail closed（`StrategySourceRefused`），从不填补。**v1 简化**
（留给调试批次，非契约变更）：把 `target_weight` 直接映射为 `apps.execution.records.TargetPosition.quantity`
——执行侧的逐标的记账（差额、订单、成交、二道风控）因此被真实数值端到端跑通，但真正的仓位定量
（`quantity = weight * equity / price`，`plugins.backtest.bar` 对研究回测的做法）需要这条窄缝目前没有的权益
与价格知识。新增 `tests/fake_strategy.py`（确定性 `StrategyProvider` / `RiskProvider` 测试替身）与
`tests/apps/test_execution_strategy_source.py`：未来信号不泄漏、fail closed、风控约束确有效果、经
`ExecutionService.admit` / `run_once`（SIMULATED，`RiskLimits.from_risk_policy`）端到端、LIVE 仍被拒绝、
Kill Switch 仍能止住订单流。

**修订（2026-09-25，Claude Opus）**：上面的"v1 简化"已撤销——权重不再直接当作数量；定量由必填的 `PositionSizer` 完成（v1 `EquityPriceSizer`：`quantity = weight * equity / price`，价格须在 `as_of` 已知，缺失或非正即拒绝；无"权重即数量"的默认）。测试：`tests/apps/test_execution_strategy_source.py`（按权益与价格定量、价格缺失 / 非正拒绝、权益非正拒绝）。

## Implementation note (dataset wiring, 2026-09-25)

`infrastructure/bars/dataset.py` 的 `backtest_bars_from_dataset` 把已持久化、经验证的 Research Dataset 的 Canonical 1m bar
转成 `PriceBar`（`instrument` = Canonical symbol），与 ADR-0037 的 `outcome_request_from_dataset` 共用同一证明路径（验证型
`ManifestStore` 加载、单点 spec、数据集 snapshot 行、按 spec 重选且须恰好等于数据集行、lineage 绑定、自身 `available_time` 与
`Decimal` OHLC、`price_cutoff` 之后可用的 bar 被拒）。`BacktestRequest` 的 Schema 冻结、没有 manifest / cutoff 槽位，因此返回
`DatasetPriceBars(manifest_content_hash, price_cutoff, bars)`，由调用方记入复现元组；未改契约、无新 ADR。缺失分钟不填补
（回测器保持最后一次标记）。测试：`tests/infrastructure/bars/test_dataset_bars.py`（数据集 → `BarBacktester`、重跑 `result_hash`
一致、缺标的 / 非 bar 数据集 fail closed）。状态：FRAMEWORK_IMPLEMENTED / NOT_VALIDATED。

# research/router

Phase 10 动态策略路由（[ADR-0043](../../docs/adr/0043-dynamic-strategy-router.md)）。**研究代码，只做纸面 / 模拟运行**
（CLAUDE.md H5、H10）：无下单、无密钥、无网络。状态：FRAMEWORK_IMPLEMENTED / NOT_VALIDATED。

| 模块 | 内容 |
|---|---|
| `router.py` | `StrategyRouter`：只路由 ACTIVE / PRODUCTION_CANDIDATE 策略；t 时刻权重来自 **t 时刻已知**的状态查表（未知走回退）；每次权重变化计换手 `Σ\|Δw\|` 与切换成本；`RouterSpec.spec_hash()` |
| `paper.py` | W1 接线：`paper_run` —— 路由权重 × 各策略的 P5 目标仓位 → 组合目标仓位 → 注入的 `BacktestProvider` → 扣除切换成本后的路由器**自身** `BacktestResult` |

## `paper_run` 语义

1. 对 P2 `StateResult` 的每个评估时刻 `t` 路由（`StrategyRouter.route`）。
2. 组合目标：`Σ_s w_s(t) × target_s(t, 标的)`，`target_s` 取策略 `s` 在 `decision_time <= t` 的最近一条（as-of；尚无仓位的策略不贡献）；
   `inputs_used` 为参与仓位之和，无信息即空仓；权重量化到 `1e-18`。
3. 组合目标交给注入的回测器（`gross`，按请求的成本模型计成交费用与滑点，`check_answers` 核对）。
4. 切换成本：每个换手非零的决策收取 `switching_cost_rate × 换手 × E`（`E` 为决策时刻及之前最近一个估值点的净权益，之前没有估值点则为初始权益），
   在决策**之后**的第一个估值点从现金支付，并延续到之后所有估值点；模拟账簿不因此重新定量；决策之后已无估值点的记录但不支付。
   它叠加在成本模型之上，只为策略配置的切换定价；只想用成本模型时把费率设为 0。
5. `result`：同一请求、同样的成交、净权益曲线，归属 `research_router_paper@0.1.0`——路由器作为一个策略对象，之后可与其它策略一样被验证（P4 / P8）。

**诚实边界**：`result` 的 `request_hash` / `provider_hash` 不标识路由规格与内部回测器；`RouterPaperRun.run_hash` 绑定路由规格哈希、
状态结果哈希、每个策略结果哈希、全部决策、gross 与 net 结果哈希以及切换成本明细。本模块不做任何验证，不含阈值。

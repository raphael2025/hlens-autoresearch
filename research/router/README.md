# research/router

Phase 10 动态策略路由（[ADR-0043](../../docs/adr/0043-dynamic-strategy-router.md)）。**研究代码，只做纸面 / 模拟运行**
（CLAUDE.md H5、H10）：无下单、无密钥、无网络。状态：FRAMEWORK_IMPLEMENTED / NOT_VALIDATED。

| 模块 | 内容 |
|---|---|
| `router.py` | `StrategyRouter`：只路由 ACTIVE / PRODUCTION_CANDIDATE 策略；t 时刻权重来自 **t 时刻已知**的状态查表（未知走回退）；每次权重变化计换手 `Σ\|Δw\|` 与切换成本；`RouterSpec.spec_hash()` |
| `evidence.py` | P10-ELIG 证据模式：逐个被路由策略核对其真实 `ValidationReport`（哈希、subject、PASS、密封 OOS G5）；`report_store_resolver(root)` 读取报告库文件 |
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

## 代码补全（2026-09-26，CODE_COMPLETE / DEBUG_PENDING）

- **明确停止**：`StrategyRouter` 无候选时抛 `RouterStopped`（`RouterError` 子类，带类型化 `reason`）：
  `no_validated_candidate`（生命周期映射中没有 ACTIVE / PRODUCTION_CANDIDATE 策略）、`all_routes_flat`（每个状态与回退对所有策略权重均为 0）。
  只要至少一个条目给已验证策略正权重，个别状态 / 回退空仓仍是合法路由。路由到未验证策略仍是普通 `RouterError`。
- **停止记录**：`paper_run_or_stop(spec, lifecycle, states, strategies, ...)` 在 `RouterStopped` 时返回 `RouterStop`
  （原因、规格哈希、生命周期快照、状态 / 策略结果哈希、可选报告哈希、`stop_hash`），不做模拟，不产出可被误读为结果的空仓曲线；否则等同 `paper_run`。
- **run hash 覆盖**：`RouterPaperRun.expected_run_hash()` / `verify()` 由记录字段重算；篡改路由名、规格哈希（路由表 / 回退 / 费率）、
  状态结果哈希、策略结果哈希、决策、切换成本明细、报告映射或替换结果 / 请求均被拒绝（`tests/research/router/test_router_completion.py`）。
  不提供 `validation_reports` 时 `run_hash` 与此前逐字节相同。
- **验证报告绑定（可选）**：`paper_run(..., validation_reports={策略 ref: 报告内容哈希})`；给出时每个可被路由的策略必须恰有一份（缺 / 多即拒绝），
  映射记录在 `RouterPaperRun.validation_reports` 并计入 `run_hash`；本模块只记录引用，不打开、不判定报告。

## 资格绑定验证证据（P10-ELIG，2026-09-26，CODE_COMPLETE / DEBUG_PENDING）

由 Claude 在 Raphael 2026-09-26 自主决定授权（autonomous-decision instruction）下设计与实现；无 core / 契约 / Schema 变更，无阈值，路由器仍只做纸面运行。

- **两种模式**：默认**信任模式**照旧只看调用方的生命周期映射，所有既有哈希（`run_hash`、`stop_hash`、报告载荷）逐字节不变
  （`tests/research/router/test_router_eligibility.py` 用变更前代码算出的哈希钉住）。
  **证据模式**：`StrategyRouter(spec, lifecycle, evidence=EligibilityEvidence(report_hashes=..., reports=...))`，
  `reports` 为「策略 ref → `ValidationReport` 对象」或「报告哈希 → 报告」的解析器；`report_store_resolver(root)` 读取
  `<root>/validation_report/<hash>.json`（`research/reports/validation.py` 写入的布局；不 import `apps`）。
- **逐策略检查**（规格可路由的每个策略，按 ref 排序；第一个失败即拒绝原因）：有声明哈希（`report_hash_missing`）→ 找到报告（`report_not_found`）→
  是合法 `ValidationReport`（`report_invalid`）→ 内容哈希等于声明哈希（`report_hash_mismatch`）→ `subject` 等于被路由 ref（`subject_mismatch`）→
  判定 PASS（`verdict_not_pass`）→ 至少一个 G5 密封 OOS 门（`sealed_oos_not_evaluated`，复用 `research.validation.report.promotion_blocked_reason`）→
  所有 G5 门 PASS（`sealed_oos_not_passed`，防御性）。
- **拒绝**：任一失败抛 `RouterEligibilityRefused`（`RouterStopped` 子类，`reason = eligibility_not_evidenced`，`refusal` / `refusals` 给出具体原因，
  `eligibility` 含全部检查），绝不静默路由。`paper_run_or_stop(..., evidence=...)` 把它记录为 `RouterStop`（`eligibility` 计入 `stop_hash`）。
  输入格式错误（多余的哈希 / 报告、非 sha256、非 `EligibilityEvidence`）与路由到生命周期未验证策略仍是普通 `RouterError`。
- **记录**：仅证据模式下，`RouterPaperRun.eligibility`（每策略：声明的生命周期、报告哈希、subject、判定、G5 门）与验证过的
  `validation_reports` 计入 `run_hash`；另给出的 `validation_reports` 必须与证据一致。报告载荷（`research/reports/router.py`）仅在证据模式下附加 `eligibility` 键。
- **边界**：PASS 且含 G5 通过的报告只证明可路由状态的研究层前提（样本内 + 密封 OOS 通过）；PRODUCTION_CANDIDATE / ACTIVE 所需的人工 / Control Plane 审查仍是调用方声明。
  **P10-ELIG 在研究层关闭；生产资格仍归 Control Plane。**

仍未做：信任模式下生命周期映射本身不进入 `run_hash`（改变既有哈希）；web 控制台对 `eligibility_not_evidenced` 原因与 `eligibility` 键只按原样显示（`apps/web` 不在本通道范围）。

## 路由器自验证（P10，2026-09-26，CODE_COMPLETE / DEBUG_PENDING）

无 core / 契约 / Schema 变更；未改 `router.py` / `paper.py` / `evidence.py` / `__init__.py`，通过模块路径 `research.router.validation` 暴露。

- `router_strategy_spec(spec, state=...)`：把路由器表示为普通 `StrategySpec`（同名同版本，唯一信号为其状态，`params = {"router_spec_hash": RouterSpec.spec_hash()}`，
  `param_search_space` 为空）。路由表 / 回退 / 切换费率一变，策略规格内容哈希即变，经 `ValidationContext` 的复现元组绑定进报告的 `experiment_hash`。
  试验计数与策略相同：每个路由规格恰 1 次试验（`ROUTER_TRIALS_PER_SPEC`）。
- `RouterTrialRunner`：`research.strategies.validation.TrialRunner` 实现。无压力的调用**就是** `paper_run`（其 `backtest` 即 `RouterPaperRun.result`，
  净切换成本），因此 `G0.reproducibility` 比较记录的路由器结果与重新 `paper_run` 的结果。G4 压力调用复用同一流程（路由 → `combine_targets` → 回测器 →
  与 `paper_run` 相同的切换成本记账）：`decision_offset` 在平移后的时刻按当时已知状态重新路由；`delay_bars` 把组合目标（及其切换费用）推迟执行；
  `instruments` 只保留这些标的。非空 `params` 一律拒绝（路由器没有搜索参数）。
- `validate_router(validator, strategy_spec, run)`：核对策略规格属于该路由器、`run.verify()`，再验证 `run.result`；
  `RouterValidation.binding_hash` 绑定报告哈希、路由规格哈希、路由策略规格哈希与 `run_hash`。本模块不含阈值、不判定 verdict。
- 测试：`tests/research/router/test_router_validation.py`（TEST ONLY 宽松 Profile；只断言结构与绑定、G0 重跑可复现、篡改重跑 G0 失败、试验计数，不断言 verdict）。

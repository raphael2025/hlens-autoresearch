# Strategy Library

| 字段 | 值 |
|---|---|
| 类型 | `strategy` |
| 状态 | 框架已实现（Phase 5，ADR-0038）；3 个研究策略，均为 NOT_VALIDATED；无策略晋升（Promotion 今天拒绝所有策略） |
| 首次填充 | Phase 5 |

从知识库与研究中登记的交易策略（StrategySpec）。

## 条目字段（草案，以 `core/contracts` 为准）

| 字段 | 说明 |
|---|---|
| `name@version` | 唯一标识 |
| `family` | trend / mean-reversion / carry / microstructure / event-driven / ... |
| `source` | 出处（KnowledgeItem ID / 论文 / 实验） |
| `signals` | 依赖的 Feature / State / Event 引用 |
| `param_space` | 参数及搜索空间（用于 trial count） |
| `applicable_scope` | 标的、频率、状态范围 |
| `risk_policy` | 默认 RiskPolicy 引用 |
| `lifecycle_state` | IDEA ... RETIRED / REJECTED |
| `validation_reports` | 报告引用 |

## 通用规则

- 条目登记 ≠ 条目有效。所有条目默认状态为 `IDEA` 或 `UNVERIFIED`，只有 Lifecycle 状态能说明其是否经过验证。
- 每个条目必须有出处（`source`）；来源为本系统实验时引用 ExperimentRun ID。
- 已发布版本不可修改；修改 = 新版本。
- 被拒绝 / 失败的条目**不删除**，其状态更新并在 [failure-registry.md](failure-registry.md) 中登记。
- 本 Markdown 是人类可读索引；Phase 0 之后，权威数据存放在 Control Plane Registry 中，本文件由其生成或与其同步。
- 目标仓位（`TargetPosition.target_weight`）= 带符号名义价值 / 权益；`inputs_used = 0` 时权重必须为 0（`core/contracts/strategy.py`）。

### 规格状态（本库专用；不是 Lifecycle 状态，也不是知识库证据等级）

| 值 | 含义 |
|---|---|
| `DOCUMENTED` | 只有文献 / 外部知识库描述，主项目没有代码 |
| `IMPLEMENTED` | 主项目有代码与定向测试（FRAMEWORK_IMPLEMENTED 或 CODE_COMPLETE / DEBUG_PENDING）；**不是**验收，也不是验证 |
| `NOT_VALIDATED` | 没有任何经 Validation Pipeline 判定的报告（Profile 数值未冻结）；当前**所有**条目都是 |
| `INPUT_UNAVAILABLE` | 必需输入不在当前数据范围（ADR-0022：Binance 现货 BTCUSDT / ETHUSDT 的 aggTrades 与 1m K 线） |
| `UNSPECIFIED` | 规则、参数或输入路径未被已接受 ADR / 现有契约定义 |

### 三类数值不得混写

- **文献原始参数**：只作出处记录（例如原文的月度形成期）。
- **项目实验搜索空间**：`StrategySpec.param_search_space` 中声明；Provider 拒绝空间外的点（`research/strategies/_params.py::resolve_params`）。
  这是策略参数，**不是**验证阈值。
- **Validation Profile 阈值**：数值 TBD（Phase 4 校准后冻结），不在本库出现。

### 外部知识库引用

`hlens-knowledge:<ID>` 指独立仓库 `hlens-knowledge`（R4，2026-09-23）中的只读条目：不是 `KnowledgeItem`、不可被本项目检索、
没有自动接入或同步；其证据等级是该仓库对来源的分级。只有 `knowledge:<name>@<version>` 是本项目已登记的 KnowledgeItem（种子，状态均为 unverified）。

## 现状矩阵

| 策略 / 方法 | 文献 / 知识库 | 主项目实现 | 输入 | 测试证据 | 规格状态 |
|---|---|---|---|---|---|
| 时间序列动量 | `knowledge:strategy_time_series_momentum@1.0.0`、`knowledge:strategy_crypto_time_series_momentum@1.0.0`；`hlens-knowledge:FAC-MOM-TS-001`、`ALP-TSMOM-001` | `tsmom_bars@1.0.0` | `bar_log_return`（可算） | 策略契约套件、未来信号拒绝、空间外参数拒绝、G0–G4 接线、golden 实验（合成夹具） | `IMPLEMENTED · NOT_VALIDATED` |
| 时间序列动量 + 波动率目标 | 同上 + `knowledge:risk_volatility_managed_portfolios@1.0.0` 等；`STR-VOLTARGET-001` | `tsmom_bars_vol_scaled@1.0.0` | `bar_log_return` + `bar_realized_vol_60`（风控信号，研究循环未提供，见下） | 管线确定性与无前视、风控契约套件、各上限生效；G0–G4 端到端测试 `tests/research/strategies/test_vol_scaled_validation.py`（未运行；缺波动率信号时全部空仓、结论 INCONCLUSIVE）；无 golden 实验 | `IMPLEMENTED · NOT_VALIDATED` |
| 横截面动量（动量腿） | `knowledge:factor_crypto_market_size_momentum@1.0.0`；`FAC-CRYPTO-MOM-001` | `xsmom_bars@1.0.0` | `bar_log_return`（可算）；两个标的 | `test_cross_sectional_momentum.py`、`test_cross_sectional_g4.py` | `IMPLEMENTED · NOT_VALIDATED` |
| Donchian / 通道突破 | `STR-TF-DONCHIAN-001`、`STR-BREAKOUT-001`（知识库证据等级 DOCUMENTED） | 无 | bar OHLC（可得） | 无 | `DOCUMENTED · UNSPECIFIED` |
| 单标的 z-score 均值回归 | `STR-MR-ZSCORE-001`（知识库证据等级 DOCUMENTED） | 无 | bar close（可得） | 无 | `DOCUMENTED · UNSPECIFIED` |
| 短周期 taker 流量条件化 | `ALP-TAKER-FLOW-001`（知识库证据等级 COMMUNITY_REPORTED）；反证 `FAIL-TAKER-COST-001` | 无 | K 线 taker 字段（可得，无对应 Feature） | 无 | `DOCUMENTED · UNSPECIFIED` |
| 双动量（绝对 + 相对） | `STR-DUAL-MOM-001`（知识库证据等级 COMMUNITY_REPORTED） | 无 | 可得（两个标的 + 空仓） | 无 | `DOCUMENTED · UNSPECIFIED` |
| 配对交易（BTC–ETH） | `STR-PAIRS-001`（Gatev, Goetzmann & Rouwenhorst 2006，*RFS*，股票；知识库证据等级 ACADEMIC） | 无 | 可得（仅一对） | 无 | `DOCUMENTED · UNSPECIFIED` |
| 横截面反转 / 统计套利 | `STR-CSMR-001`、`STR-STATARB-001` | 无 | 宽标的池未采集 | 无 | `DOCUMENTED · INPUT_UNAVAILABLE` |
| 做市 | `STR-MM-001` | 无 | 订单簿未采集 | 无 | `DOCUMENTED · INPUT_UNAVAILABLE` |
| 盈余公告漂移 | `STR-PEAD-001`（股票） | 无 | 无对应数据 | 无 | `DOCUMENTED · INPUT_UNAVAILABLE` |

## 条目索引

| name@version | 摘要 | 出处 | 状态 |
|---|---|---|---|
| `tsmom_bars@1.0.0` | trend：各标的最近 `lookback` 个可见 `bar_log_return` 之和的符号 → 多 / 空（`long_only` 时空头改为空仓），每标的等分 `1 / N` 总敞口；历史不足或窗口内有缺失 → 空仓。信号 `feature:bar_log_return@1.0.0`；参数空间 `lookback ∈ {60, 240, 1440}`（bar 数）、`long_only ∈ {false, true}`；无风控 | `knowledge:strategy_time_series_momentum@1.0.0`、`knowledge:strategy_crypto_time_series_momentum@1.0.0` | NOT_VALIDATED |
| `tsmom_bars_vol_scaled@1.0.0` | 同一信号与参数空间，`risk_policy = risk:vol_target_bars@1.0.0`（见 [risk-library.md](risk-library.md)） | 同上 + 风控条目出处 | NOT_VALIDATED |
| `xsmom_bars@1.0.0` | cross_sectional_momentum：各标的最近 `lookback` 个 `bar_log_return` 之和降序排名（同值按标的名升序）；`k = min(top_n, m // 2)`（`m` = 可计算标的数），多前 `k`、空后 `k`，各 `gross_exposure / (2k)`；`long_only` 时只多前 `k`，各 `gross_exposure / k`；每份向下取整到 18 位；`k = 0` 或全部相等 → 全部空仓；不可计算标的空仓。参数空间 `lookback ∈ {60, 240, 1440}`、`long_only ∈ {false, true}`、`top_n ∈ {1, 2}`、`gross_exposure ∈ {1}`；无风控 | `knowledge:factor_crypto_market_size_momentum@1.0.0`（只实现动量腿） | NOT_VALIDATED |

实现：`research/strategies/time_series_momentum.py`（descriptor `research_tsmom@0.1.0`）、`research/strategies/cross_sectional_momentum.py`
（`research_xsmom@0.1.0`，由 `research/strategies/cross_section.py` 声明为横截面，ADR-0059）；目录 `research/strategies/library.py::library_entries()`
（family 分别为 `trend`、`trend`、`cross_sectional_momentum`）。研究代码，未晋升；`strategies/` 为空。参数空间外的请求被 Provider 拒绝。

### 按实现补充的决策规则

- `tsmom_bars`：`1 / N` 以 half-even 量化到 18 位，某些 `N` 下各份之和可略高于 1（例如 `N = 6`）；窗口和为 0 → 空仓。
- `xsmom_bars`：一个已排名标的的仓位依赖所有排名窗口，因此其 `inputs_used` / `latest_input_available_time` 覆盖全部**可计算（已排名）**标的；不可计算标的为 `inputs_used = 0`。
- 两者的 `lookback` 以**所用信号的 bar 数**计：在 1m bar 上 `{60, 240, 1440}` 即 1 小时 / 4 小时 / 1 天，比文献的月度或周度形成期短几个数量级；
  这是项目声明的实验空间，不是对文献的复现。

### 回测与验证接线

- 回测：`plugins/backtest/bar.py::BarBacktester`（`hlens_bar_backtest@1.0.0`）——目标在 `t` 决定、在 `interval_start ≥ t` 的第一根 bar 开盘成交；
  成交价 `open·(1 ± slippage_rate)`，手续费 `|q|·fill·fee_rate`；v1 默认无融资。可选执行模型（参与率上限、平方根冲击、逐 bar 步长的空头借券费率 / 现金借款费率，
  参数全部显式、进入 `provider_hash`）与跨 bar 结转（ADR-0054）见 `plugins/backtest/execution.py`。
- 验证：`research/strategies/validation.py::PipelineBacktestValidator` 已把试验接到 G0–G4（CODE_COMPLETE / DEBUG_PENDING）；G4 在声明的
  参数网格上重跑（这些重跑**不是**账本试验，过拟合检查用 `max(family_trial_count, len(trials))`）。Profile 数值未冻结，因此没有任何报告是有效判定；
  测试中的 golden 实验（`tests/golden/experiments/tsmom_g0_g4.py`）是合成夹具，不是验证证据。`tsmom_bars` 曾在数据集路径上以
  Binance 格式的 TEST ONLY 夹具做过能力冒烟（`tests/infrastructure/e2e/test_research_pipeline_real_data.py`，postgres 标记），那只证明管道可通，
  不是 Research Dataset 上的研究结果；没有策略在正式 Research Dataset 上运行过。
- 被拒绝或运行失败的试验经 `research/strategies/failure_registry.py` 追加到 Failure Registry。

### 已知局限与失效模式

- **现货空头**：负目标权重在 Binance 现货上需要借币；回测 v1 默认不计融资 / 借币成本（可选的空头借券 / 现金借款费率需显式给参数）。空头可实施性未规格化；
  `long_only` 是已声明的参数维度。
- **横截面退化**：只有 BTCUSDT / ETHUSDT 时 `k ≤ 1`，`top_n = 2` 与 `top_n = 1` 等价；这不能检验文献的横截面因子主张。
- **研究循环中的波动率目标变体**：循环构造试验输入时不提供风控信号（`EvaluationInputs.risk_signals` 只在测试中填充）；今天没有循环接线包含该变体，
  若被接入循环 / 数据集路径，`tsmom_bars_vol_scaled` 会因 `missing_volatility_flat` 全部空仓，而不是失败。如何在循环中提供风控信号未被已接受 ADR 定义（见缺口 ST-2）。
- 文献失效：动量崩溃（`hlens-knowledge:FAIL-MOM-CRASH-001`，Daniel & Moskowitz 2016）；成本侵蚀（`FAIL-COST-EROSION-001`）；超参数过拟合与
  过度搜索（`FAIL-HYPEROPT-OVERFIT-001`，出处为多来源；`FAIL-OVERSEARCH-001`，Bailey et al. 2014）；加密短周期均值回归可检测但被成本吞没
  （`FAIL-TAKER-COST-001`，Kitron & Wengrowicz 2026，arXiv 预印本：Binance 现货 15 分钟收益符号均值回归，集中在 taker 主导的 bar 之后，
  总边际约 1.3 bp / 笔，低于约 5 bp 的往返成本带）；PIT 纪律下 IC 为正而净 Sharpe 为负
  （`FAIL-CRYPTO-PIT-HOLD-001`，BTC 永续）。这些是文献报告，不是本项目结论。

## 候选空间如何扩展（按实现）

| 能力 | 实现 | 状态 |
|---|---|---|
| 在声明空间内选点 | 假设以条件 `strategy = name@version` 与 `param k = v` 指定目录中的策略与参数点（`research/loop/segment.py::trial_point`）；无法解析即 ERRORED 试验（计数、不丢弃） | FRAMEWORK_IMPLEMENTED / CODE_COMPLETE，DEBUG_PENDING |
| 批量网格 | `research/hypotheses/batch.py`：算子 × 策略 × 点，只有 `parameter_point` 可运行；每个点必须是声明值；算子须在人工审阅的允许列表中；`preregister_batch` 全有或全无 | CODE_COMPLETE / DEBUG_PENDING |
| 知识 / LLM 草稿 | `from_knowledge` 每条主张一个假设；`from_llm` 只能提出 `name / statement / expected_direction / minimum_meaningful_effect / conditions`，草稿须人工审阅后才登记；LLM 不参与判定 | FRAMEWORK_IMPLEMENTED / CODE_COMPLETE，DEBUG_PENDING（只有脚本化 LLM） |
| 进化 | 循环只使用 `mutate`（只在声明空间内），后代作为新假设登记（沿用父代 family）；`combine` 作为算子存在，但循环不登记、不运行它 | FRAMEWORK_IMPLEMENTED / CODE_COMPLETE，DEBUG_PENDING |
| 状态 × 策略条件化 | Phase 6：所有声明的单元（含未知状态单元）预登记；`min_support` 必填；最小效应是假设内容，不是门槛（D-MINEFF） | CODE_COMPLETE / DEBUG_PENDING |
| 新策略规则（不写代码） | 不存在：新规则必须实现一个 StrategyProvider；生成的代码永不执行（ADR-0040） | `UNSPECIFIED` |

注意：种子 KnowledgeItem 的 `conditions` 是自由文本（如"月度调仓"），不能解析为试验点；由它们直接生成的假设在循环中会成为 ERRORED 试验。

## 缺口（待规格化 / 待批准）

| ID | 缺口 | 说明 |
|---|---|---|
| ST-1 | 声明网格的每个点是否自动计入账本 | 今天只有显式登记的假设计入；G4 网格重跑不计入账本。是否把整个声明空间视为一个 family 的试验数需决定（C-T1 的解释），不在本库预设 |
| ST-2 | 循环 / 数据集路径的风控信号 | 需要规定风控所需 Feature 由谁计算、如何进入 `EvaluationInputs.risk_signals`（ADR-0049 只写"策略 → 风控 → 回测"） |
| ST-3 | 候选策略（Donchian、z-score、双动量、配对） | 输入可得；有状态出场规则（持仓后的反向突破、止损）在无状态 StrategyProvider 中如何表达、参数空间与出处须逐一规格化 |
| ST-4 | 现货空头的成本与可实施性 | 借币成本、可借量未建模；是否限定 `long_only` 或引入借币成本模型须决定 |

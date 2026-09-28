# Risk Library

| 字段 | 值 |
|---|---|
| 类型 | `risk` |
| 状态 | 框架已实现（Phase 5，ADR-0038）；2 个研究风控政策（含 ADR-0085 的 `drawdown_control`），均为 NOT_VALIDATED |
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
- 风控参数（目标波动率、倍数上限、仓位上限）是**风控政策参数**，不是验证阈值，也不是策略参数；三者分开记录。
- 每个被调整的仓位必须在 `ConstrainedPosition.binding_rules` 中写明生效的规则名（排序、唯一；仓位改变时非空，未改变时为空）。

### 规格状态（本库专用；不是 Lifecycle 状态，也不是知识库证据等级）

| 值 | 含义 |
|---|---|
| `DOCUMENTED` | 只有文献 / 外部知识库描述，主项目没有代码 |
| `IMPLEMENTED` | 主项目有代码与定向测试（FRAMEWORK_IMPLEMENTED 或 CODE_COMPLETE / DEBUG_PENDING）；**不是**验收，也不是验证 |
| `NOT_VALIDATED` | 没有任何经 Validation Pipeline 判定的报告；当前**所有**条目都是 |
| `INPUT_UNAVAILABLE` | 必需输入不在当前数据范围或当前接口中 |
| `UNSPECIFIED` | 规则、参数或输入路径未被已接受 ADR / 现有契约定义 |

`hlens-knowledge:<ID>` 指独立仓库 `hlens-knowledge`（R4，2026-09-23）中的只读条目：不是 `KnowledgeItem`、不可被本项目检索、
没有自动接入或同步；其证据等级是该仓库对来源的分级，不是本项目结论。

## 现状矩阵

| 方法 | 文献 / 知识库 | 主项目实现 | 输入 | 测试证据 | 规格状态 |
|---|---|---|---|---|---|
| 波动率目标 / 缩放 | `RSK-VOLSCALE-001`、`STR-VOLTARGET-001`、`PAP-MOREIRA-MUIR-2017-001`；反证 `FAIL-VOLMGMT-OOS-001`；项目种子 `knowledge:risk_volatility_managed_portfolios@1.0.0`（E3）、`knowledge:risk_volatility_managed_portfolios_out_of_sample@1.0.0`（E3） | `vol_target_bars@1.0.0` 的 `volatility_scaling` | `bar_realized_vol_<n>`（可算；研究循环不提供，若接入循环会全部空仓，见缺口 R-2） | 风控契约套件、各上限生效、未来信号被拒 | `IMPLEMENTED · NOT_VALIDATED` |
| 单仓位上限 / 总敞口上限 | `RSK-GROSS-EXP-001` | `vol_target_bars@1.0.0` 的 `position_cap`、`gross_exposure_cap` | 目标权重 | 同上 | `IMPLEMENTED · NOT_VALIDATED` |
| 杠杆上限 | `RSK-LEVERAGE-001`（名义 / 权益） | `vol_target_bars@1.0.0` 的 `leverage_cap`——**只限制波动率缩放倍数**，不是账户名义杠杆 | 同上 | 同上 | `IMPLEMENTED · NOT_VALIDATED`（语义不同） |
| 止损 / 追踪止损 | `RSK-STOP-001`（Kaminski & Lo 2014）；反证 `FAIL-STOP-RW-001` | 无 | 需要持仓与入场价路径（风控请求只含目标与可见信号） | 无 | `DOCUMENTED · UNSPECIFIED` |
| 回撤控制 | `RSK-DD-CONTROL-001`（Grossman & Zhou 1993）；本项目无知识库种子 | `drawdown_control@1.0.0`（ADR-0085；`PortfolioState.peak_equity` 见 ADR-0088 决策 3）；参数 `max_drawdown`、`reduced_fraction` 显式、无默认 | 研究策略管线对带风控政策的候选经 `BarBacktester.run_with_risk` 提供截至决策时刻的已实现 `PortfolioState.equity` 与 `peak_equity`；该路径仅支持 `BarBacktester`，其他 backtester 会拒绝（见缺口 R-4） | `test_drawdown_control.py`、`tests/plugins/backtest/test_risk_loop.py`（未运行） | `IMPLEMENTED · NOT_VALIDATED` |
| Kelly / 分数 Kelly、AFML 下注规模 | `RSK-KELLY-001`、`RSK-BETSIZE-001`；反证 `FAIL-FULL-KELLY-001` | 无 | 需要边际估计（不存在） | 无 | `DOCUMENTED · UNSPECIFIED` |
| 预期短缺 / CAViaR | `RSK-ES-001`（Acerbi & Tasche 2002）、`RSK-CAVIAR-001`（Engle & Manganelli 2004） | 无（属度量，非仓位规则） | 收益可得 | 无 | `DOCUMENTED · UNSPECIFIED` |
| 相关 / 集中度 | `RSK-CORR-001`；反证 `FAIL-CORR-SPIKE-001` | 无 | 只有两个标的 | 无 | `DOCUMENTED · UNSPECIFIED` |
| 容量 / 参与率 | `RSK-CAPACITY-001`；`FAIL-CAPACITY-ILLUSION-001` | 非风控政策：回测执行模型的参与率上限（`plugins/backtest/execution.py`）与验证 G4 容量检查（ADR-0041 C-R5；数据集路径细化见 ADR-0064 / 0065） | 成交量可得 | 见回测 / 验证测试 | 不属本库（执行模型 / 验证） |
| 急停 / 第二道风控 | `RSK-KILLSWITCH-001`（Kirilenko et al. 2017） | 非 RiskPolicy 插件：Phase 13 模拟执行的 `apps/execution/kill_switch.py`（无 reset）与 `apps/execution/risk.py::SecondLineRisk`（数值全部注入，无默认） | — | `tests/apps/test_execution.py`、`tests/apps/test_execution_strategy_source.py` 等 | 不属本库（仅模拟，无实盘） |

## 条目索引

| name@version | 摘要 | 出处 | 状态 |
|---|---|---|---|
| `vol_target_bars@1.0.0` | sizing / exposure：波动率目标缩放，并依次施加倍数、单仓位、总敞口上限（规则顺序见下）。波动率信号默认 `feature:bar_realized_vol_60@1.0.0`（`available_time <= t` 的最新一条）；模块声明的参数空间 `target_volatility ∈ {0.0025, 0.005, 0.01}`（信号同单位）、`max_leverage ∈ {1, 2}`（**只是声明，未被强制**，见下） | `knowledge:risk_volatility_managed_portfolios@1.0.0`、`knowledge:risk_volatility_managed_portfolios_out_of_sample@1.0.0` | NOT_VALIDATED |

| `drawdown_control@1.0.0` | drawdown：`回撤 = 1 − equity / peak_equity`（`PortfolioState`）**严格大于** `max_drawdown` 时，所有目标仓位乘以 `reduced_fraction`；否则原样通过。参数 `max_drawdown ∈ (0, 1)`、`reduced_fraction ∈ [0, 1)`，十进制文本，显式给出、无默认（ADR-0085 规则 2）；本模块**不**声明参数空间（缺口 R-1） | `RSK-DD-CONTROL-001`（`hlens-knowledge`，只读）；无 `KnowledgeItem` 种子，`lineage` 为空，因此不是库条目 | NOT_VALIDATED |

实现：`research/strategies/volatility_target.py`（descriptor `research_vol_target@0.1.0`）、`research/strategies/drawdown_control.py`
（`research_drawdown_control@0.1.0`）；研究代码，未晋升；`risk/` 为空。

### `drawdown_control@1.0.0` 规则（按实现）

- 唯一规则名 `drawdown_scaling`：触发时每个非零目标按 `reduced_fraction` 缩放，向零截断到 18 位（`|约束后| ≤ reduced_fraction × |请求|`）；
  `reduced_fraction = 0` 即清仓。请求权重为 0 或未触发时不记规则。风控侧不使用信号（`inputs_used = 0`）。
- `PortfolioState.equity` 或 `peak_equity` 任一缺失 → `RiskInputError`（fail closed），不当作"没有回撤"。Provider 不记忆峰值（ADR-0088 决策 3）。
- 峰值来源：`plugins/backtest/risk_loop.py`。`BarBacktester.run_with_risk` 在每个决策时刻 `t` 接纳目标时构造 `PortfolioState`：
  已实现权益路径 = `initial_equity` + 曲线上 `time ≤ t` 的全部 `EquityPoint`；`equity` 取最后一个值，`peak_equity` 取运行最大值（单调不减）。
  `t` 决定的目标在 `interval_start ≥ t` 的 bar 执行、其权益点晚于 `t`，因此不读取任何未来值。已实现权益 ≤ 0 时拒绝（`PortfolioState.equity` 必须为正）。
  `current_weights` 沿用管线约定（上一次约束后的目标权重）。结果按"约束后的目标"请求构建，等于对该请求直接 `run`；`run` / `run_with_report` 语义不变。
- 同名不同参数的政策仍共用 `drawdown_control@1.0.0`（同缺口 R-3）；本 Provider 拒绝在一个实例中重复登记同一 `name@version`。

### 规则顺序（按实现）

规则名集合 `VOL_TARGET_RULES = (missing_volatility_flat, volatility_scaling, leverage_cap, position_cap, gross_exposure_cap)`。对每个目标：

1. 取该标的最新可见波动率信号（按 `(event_time, available_time)`）。缺失、为 `None` 或 `≤ 0` → 权重 0；仅当请求权重非 0 时记 `missing_volatility_flat`。
2. `scale = target_volatility / vol`；若 `scale > max_leverage` 则 `scale = max_leverage` 并记 `leverage_cap`（先限制倍数，再算权重）。
3. `weight = requested × scale`（half-even 量化到 18 位）；权重改变即记 `volatility_scaling`。
4. `|weight| > max_abs_weight` → 截到 `±max_abs_weight`，记 `position_cap`。

然后在组合层：

5. 若 `Σ|weight| > max_gross_exposure`，所有非零权重按比例缩小（`ROUND_DOWN`，即向零截断到 18 位），记 `gross_exposure_cap`。
6. 最终权重等于请求权重时清空规则集（契约要求）。`binding_rules` 按字母序输出，**不是**施加顺序。

参数与默认值：`volatility_signal` = `feature:bar_realized_vol_60@1.0.0`、`target_volatility` = `0.005`、`max_leverage` = `2`、`max_abs_weight` = `1`、
`max_gross_exposure` = `1`；四个数值参数必须 > 0，`volatility_signal` 必须是 Feature 引用。`max_abs_weight` 与 `max_gross_exposure` 不在声明的参数空间中。

### 参数空间：声明与执行的边界

- ADR-0038 只要求"`RiskPolicy` 无参数空间字段，其空间在模块中声明"；对策略要求"Provider 拒绝空间外的参数点"，对风控**没有**同样的要求。
- 实际：`VOL_TARGET_PARAM_SPACE` 只被本模块引用；`vol_target_policy(**overrides)` 只拒绝未知键；测试为检验各上限会使用空间外的值
  （例如 `target_volatility = 0.5`）。因此风控参数点今天**不**进入试验计数，也没有风控候选搜索（进化后代沿用父代政策；G4 网格只覆盖策略参数）。
- 通过 `overrides` 构造的政策沿用同一 `vol_target_bars@1.0.0` 名称而内容不同，与"已发布版本不可变"冲突；Provider 以 `str(ref)` 为键，
  同名多个政策只保留最后一个。见缺口 R-1 / R-3。

### 与文献的区别

- Moreira & Muir (2017), *Journal of Finance*, doi:10.1111/jofi.12513（`RSK-VOLSCALE-001`，知识库 `ACADEMIC`）：月度因子收益按上期已实现**方差**
  反向缩放（`c / σ²`）；本实现按已实现**波动率**反向缩放（`target / σ`），在 bar 级别、以 `bar_realized_vol_<n>` 为估计。
- Cederburg, O'Doherty, Wang & Yan (2020), *Journal of Financial Economics*（`FAIL-VOLMGMT-OOS-001`，`PAP-CEDERBURG-VOLMGMT-2020-001`）：
  在 103 个股票策略上，可实施的样本外波动率管理并不系统性跑赢未管理组合；知识库注明动量相关情形可能例外。这是文献报告，不是本项目结论。
- 知识库的已知失效：波动率估计滞后、跳跃风险、低波动时杠杆飙升（`max_leverage` 只限制缩放倍数）。

## 缺口（待规格化 / 待批准）

| ID | 缺口 | 说明 |
|---|---|---|
| R-1 | 风控参数空间是否强制、是否计入试验、是否搜索 | 需要决定：哪些键固定、变体如何取得新版本、风控参数点是否计入 C-T1 的 family 试验数。决定前不强制，以免破坏现有上限测试或暗改语义（**REQUIRES_DECISION**） |
| R-2 | 研究循环 / 数据集路径的风控信号 | 与 strategy-library 缺口 ST-2 相同：未被已接受 ADR 定义 |
| R-3 | 同名不同内容的政策 | 覆盖参数后应以新版本发布；Provider 构造时是否拒绝重复 ref 须与 R-1 一并决定 |
| R-4 | 依赖路径的规则（止损、回撤） | 回撤：研究策略管线 `evaluate_strategy` 已对带风控政策的候选调用 `BarBacktester.run_with_risk`（`research/strategies/pipeline.py`；接线见 `0cfddbf`），由回测循环在每个决策时刻传入截至该时刻的已实现权益与峰值；非 `BarBacktester` 实现会拒绝该路径。相关定向测试已添加，但本次未运行，且风控政策仍为 NOT_VALIDATED。止损仍需入场价路径，未规格化 |

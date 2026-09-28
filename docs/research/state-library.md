# State Library

| 字段 | 值 |
|---|---|
| 类型 | `state` |
| 状态 | Phase 2 框架：首批条目已登记（FRAMEWORK_IMPLEMENTED / NOT_VALIDATED） |
| 首次填充 | Phase 2（[ADR-0035](../adr/0035-state-provider-contract.md)） |

市场状态定义（StateSpec）。契约见 `core/contracts/state.py`，执行器 `infrastructure/state/`，实现 `plugins/states/`，
诊断 `research/states/diagnostics.py`。

## 条目字段（以 `core/contracts` 为准）

| 字段 | 说明 |
|---|---|
| `name@version` | 唯一标识 |
| `state_space` | 离散状态集合（`StateSpec.state_space`） |
| `inputs` | Feature 引用（`StateSpec.features`；只能是 `kind=feature`，Outcome 永不作为输入） |
| `method` | 规则 / 阈值 / 聚类 / HMM / ...；参数以 `<method>:<canonical JSON>` 编码，受 spec hash 绑定 |
| `training_window` | 若为训练型：固定尾随窗口（执行器只给出 `(t - window, t]` 的输入）与固定 `seed` |
| `stability` | 平均持续时间、转移矩阵、闪烁摘要（由 `research/states/diagnostics.py` 生成，来源为实验时引用 ExperimentRun ID） |
| `provider` | 实现插件 |
| `source` | 出处 |

## 通用规则

- 条目登记 ≠ 条目有效。所有条目默认状态为 `IDEA` 或 `UNVERIFIED`，只有 Lifecycle 状态能说明其是否经过验证。
- 每个条目必须有出处（`source`）；来源为本系统实验时引用 ExperimentRun ID。
- 已发布版本不可修改；修改 = 新版本。
- 被拒绝 / 失败的条目**不删除**，其状态更新并在 [failure-registry.md](failure-registry.md) 中登记。
- 模型参数（分位切点、最少历史、阈值、窗口）是**规格参数**，由每个规格声明，不是验证阈值；首批 Provider 不设默认值。
- 本 Markdown 是人类可读索引；Phase 0 之后，权威数据存放在 Control Plane Registry 中，本文件由其生成或与其同步。

### 规格状态（本库专用；不是 Lifecycle 状态，也不是知识库证据等级）

| 值 | 含义 |
|---|---|
| `DOCUMENTED` | 只有文献 / 外部知识库描述，主项目没有代码 |
| `IMPLEMENTED` | 主项目有代码与定向测试（FRAMEWORK_IMPLEMENTED 或 CODE_COMPLETE / DEBUG_PENDING）；**不是**验收，也不是验证 |
| `NOT_VALIDATED` | 没有任何经 Validation Pipeline 判定的报告；当前**所有**条目都是 |
| `INPUT_UNAVAILABLE` | 必需输入不在当前数据范围（ADR-0022：Binance 现货 BTCUSDT / ETHUSDT 的归档 aggTrades 与 1m K 线 + REST 补尾） |
| `UNSPECIFIED` | 算法、参数或训练协议未被已接受 ADR / 现有契约定义；实现前须先规格化并批准 |

`hlens-knowledge:<ID>` 指独立仓库 `hlens-knowledge`（R4，2026-09-23）中的只读条目：不是 `KnowledgeItem`、不可被本项目检索、
没有自动接入或同步；其证据等级是该仓库对来源的分级，不是本项目的证据等级或验证结论。知识库 `01_MARKET_STATES` 的状态条目
均为 `DOCUMENTED`；其中 `MST-TREND-001`、`MST-RANGE-001`、`MST-HIVOL-001`、`MST-LIQSTRESS-001`、`MST-RISKON-001` 注明没有唯一的规范公式、
须按实验固定定义，`MST-SHOCK-001` 要求定义预注册。

## 现状矩阵

| 状态方法 | 文献 / 知识库 | 主项目实现 | 输入 | 测试证据 | 规格状态 |
|---|---|---|---|---|---|
| 波动率体制（尾随分位分桶） | `MST-HIVOL-001`、`MST-LOVOL-001`、`FEA-VOL-PCTILE-001` | `volatility_regime@1.0.0` | `bar_realized_vol_<n>`（可算） | 契约套件、精确次序统计、窗口约束、夹具端到端 | `IMPLEMENTED · NOT_VALIDATED` |
| 流动性体制（尾随分位分桶） | `MST-LIQSTRESS-001`（相关：原文按价差 / 深度 / Amihud 操作化，本实现只用成交量） | `liquidity_regime@1.0.0` | `bar_volume_sum_<n>`（可算） | 同上 | `IMPLEMENTED · NOT_VALIDATED` |
| 趋势 / 震荡（效率比） | `MST-TREND-001`、`MST-RANGE-001`（原文列举均线斜率、通道、ADX 等多种操作化） | `trend_range@1.0.0` | `bar_log_return`（可算） | 契约套件、标签测试、夹具端到端 | `IMPLEMENTED · NOT_VALIDATED` |
| 波动率压缩 / 扩张 | `MST-SQUEEZE-001`、`MST-EXPANSION-001` | `volatility_squeeze@1.0.0` | `bar_realized_vol_<short>`、`bar_realized_vol_<long>`（可算） | 契约套件、精确带边界、除零、缺失、夹具端到端 | `IMPLEMENTED · NOT_VALIDATED` |
| 冲击 / 跳跃 | `MST-SHOCK-001`（`|r| > k·σ`，本实现取阈值形式；跳跃检验形式未实现） | `return_shock@1.0.0` | `bar_log_return`、`bar_realized_vol_<n>`（可算） | 契约套件、精确阈值边界、缺失、夹具端到端 | `IMPLEMENTED · NOT_VALIDATED` |
| 状态转换期 | `MST-TRANSITION-001`（依赖状态概率路径） | 无 | 需要概率型状态模型（不存在） | 无 | `DOCUMENTED · UNSPECIFIED` |
| 马尔可夫切换 / HMM（训练型） | `SYN-REGIME-001`、`PAP-HAMILTON-1989-001`（Hamilton 1989，*Econometrica*，doi:10.2307/1912559） | 无（合成市场只有带可选自相关的随机游走 `plugins/synthetic/random_walk.py`，没有体制切换生成器） | 可算 | 无 | `DOCUMENTED · UNSPECIFIED` |
| 风险偏好 on / off | `MST-RISKON-001`、`MST-RISKOFF-001`（跨资产面板） | 无 | 跨资产数据未采集 | 无 | `DOCUMENTED · INPUT_UNAVAILABLE` |
| 资金费率体制 | roadmap Phase 2 | 无（declared-unavailable） | 资金费率未采集 | `test_state_regimes.py` 断言其被声明为不可用 | `INPUT_UNAVAILABLE` |

## 条目索引

条目是**模型族**：具体版本（参数、窗口、种子）由研究方在规格中声明；下表不登记任何参数取值。
`provider` 列是 descriptor 名（实现类在 `plugins/states/regimes.py`；`volatility_squeeze` / `return_shock`
在 `plugins/states/volatility_events.py`，ADR-0085）。

| name@version | 摘要 | inputs | method | training_window / seed | provider | 出处 | 状态 |
|---|---|---|---|---|---|---|---|
| `volatility_regime@*` | 波动率体制：最新已实现波动率在固定尾随窗口内的经验分位分桶（`low_vol` / `mid_vol` / `high_vol`） | `bar_realized_vol_<n>` | `trailing_quantile_buckets`（`cuts`、`min_history`） | 必须；seed 必填（见下文"训练型与种子"） | `volatility_regime@1.0.0`（`VolatilityRegimeProvider`） | ADR-0035（框架） | UNVERIFIED |
| `liquidity_regime@*` | 流动性体制：最新成交量合计在固定尾随窗口内的经验分位分桶（`thin` / `normal` / `deep`） | `bar_volume_sum_<n>` | `trailing_quantile_buckets`（`cuts`、`min_history`） | 必须；seed 必填 | `liquidity_regime@1.0.0`（`LiquidityRegimeProvider`） | ADR-0035（框架） | UNVERIFIED |
| `trend_range@*` | 趋势 / 震荡：最近 `window` 个对数收益的效率比 `\|Σr\| / Σ\|r\|` 与阈值比较（`trend_down` / `range` / `trend_up`） | `bar_log_return` | `efficiency_ratio`（`window`、`threshold`） | 规则型：无 | `trend_range@1.0.0`（`TrendRangeProvider`） | ADR-0035（框架） | UNVERIFIED |
| `volatility_squeeze@*` | 波动率压缩 / 扩张：短窗 / 长窗已实现波动率比值与显式 `squeeze_below` / `expansion_above` 比较（`squeeze` / `normal` / `expansion`） | `bar_realized_vol_<short>`、`bar_realized_vol_<long>` | `vol_ratio_bands`（`squeeze_below`、`expansion_above`） | 规则型：无 | `volatility_squeeze@1.0.0`（`VolatilitySqueezeProvider`） | ADR-0085 | UNVERIFIED |
| `return_shock@*` | 冲击 / 平静：`|bar_log_return| > k × bar_realized_vol_<n>` 时为 `shock`，否则 `calm` | `bar_log_return`、`bar_realized_vol_<n>` | `abs_return_vol_multiple`（`k`） | 规则型：无 | `return_shock@1.0.0`（`ReturnShockProvider`） | ADR-0085 | UNVERIFIED |
| `funding_regime` | 资金费率体制 | 资金费率（**未采集**，ADR-0022 范围） | — | — | 无（declared-unavailable，`plugins.states.DECLARED_UNAVAILABLE`） | roadmap Phase 2 | IDEA（输入不可用） |

### 决策规则（按实现）

- **分位分桶**（`volatility_regime` / `liquidity_regime`）：取训练窗口 `(t − training_window, t]` 内该 Feature 的非 `None` 值
  （含当前值）升序为 `h_1 ≤ … ≤ h_n`；切点 `q` 取 `h_k`，`k = max(1, ceil(q·n))`（无插值）；当前值 `x` 的标签序号 = 严格小于 `x`
  的切点个数。`cuts` 严格升序且每个在 (0, 1) 内，`min_history` 为正整数，二者与 `training_window`、`seed` 全部必填、无默认值。
  不可计算（`None`）：无可见输入、最新值为 `None`、或非 `None` 值少于 `min_history`。
- **效率比**（`trend_range`）：最近 `window` 个收益 `ER = |Σr| / Σ|r|`；`ER ≥ threshold` 按 `Σr` 的符号为 `trend_up` / `trend_down`，
  否则（含 `Σ|r| = 0`）为 `range`。`window` 与 `threshold ∈ (0, 1]` 必填。收益少于 `window` 个或窗口内有 `None` → `None`。
- **波动率比值带**（`volatility_squeeze`）：取两个声明 Feature（顺序固定：短窗、长窗）各自的最新可见值，
  `ratio = short / long`；`ratio < squeeze_below` → `squeeze`，`ratio > expansion_above` → `expansion`，否则
  （含刚好等于边界）→ `normal`。`squeeze_below`、`expansion_above` 必填且 `0 < squeeze_below < expansion_above`。
  不可计算：任一 Feature 无可见值或最新值为 `None`，或 `long == 0`（除以零按缺失处理，不抛出）。
- **收益冲击**（`return_shock`）：取两个声明 Feature（顺序固定：收益、波动率）各自的最新可见值，
  `|return| > k × vol` → `shock`，否则（含刚好等于阈值）→ `calm`。`k` 必填且为正。不可计算：任一 Feature
  无可见值或最新值为 `None`。
- 全部为精确 `Decimal`、确定性；输入必须是规格声明的 Feature，否则 `StateInputError`。

### 训练型与种子（按实现，不是新规则）

- 执行器（`infrastructure/state/runner.py`）对训练型规格（`training_window` 非空）要求非空 `seed`，并在每个评估时刻只交出
  `(t − training_window, t]` 的输入：Provider 结构上看不到窗口外历史，无法全样本或扩张窗口拟合（ADR-0035 §1）。
- 两个分位模型是**确定性的次序统计**：`seed` 被校验并写入规格（因此改变 spec hash），但**不影响输出**。
  执行器在代码层面强制窗口与种子；roadmap Phase 2 的"训练型状态模型有固定窗口与种子"是否满足由 Codex / Raphael 验收判定。种子今天没有实际作用。
- 将来的真正随机型模型（聚类、HMM 等）至少要满足以下**已由现有契约 / ADR 规定**的条件：固定尾随训练窗口；随机性只来自规格
  `seed`（`StateProviderDescriptor` 仍须 `deterministic: True`）；只见执行器交出的可见输入；Outcome 不得作为输入；参数编码在
  `method` 并受哈希绑定。**尚未定义**的内容：模型拟合的重估节奏（每个评估时刻重拟合还是按块）、拟合结果（参数、状态编号对齐）
  是否需要持久化与哈希、标签在重拟合间的对应规则（HMM 状态编号不唯一）、以及计算成本上限（D-STATE-INC 已暂缓增量路径）。
  这些属于新模型族的规格，须另行提出并批准；本库不预设。

### 与文献的区别

- 知识库的波动率体制（`FEA-VOL-PCTILE-001`）写作 `pctile = F_hist(RV_t)` 与 `p_hi` / `p_lo` 阈值；本项目用固定尾随窗口的经验
  次序统计，切点由规格声明。原文未报告参数（`NOT_REPORTED`）；本项目也**没有**声明任何状态参数搜索空间。
- 知识库的流动性压力（`MST-LIQSTRESS-001`）以价差、深度、Amihud 为输入；本项目的 `liquidity_regime` 只用成交量合计，
  二者不可互换（`FAIL-TURNOVER-EQUALS-ILLIQ-001`）。
- 知识库的趋势体制（`MST-TREND-001`）列举价格相对均线、Donchian 通道斜率、ADX 阈值等；本项目选择效率比（`|Σr| / Σ|r|`），属于其中未列出的一种操作化，出处为 ADR-0035 的框架选择。

## 已知失败模式（roadmap Phase 2）

- 状态事后看起来完美但实时不可识别 → 执行器结构性截断 + contract suite 因果扰动检查。
- 状态过多导致样本稀薄 → 诊断报告的分布与 `not_computable` 计数。
- 状态标签闪烁 → 诊断报告的短 run 占比与切换率（`min_run` 由调用方给出）。
- 状态标签由被检验的同一收益 / 特征构造、或在全样本上挑选切点后宣称条件化优势（`hlens-knowledge:FAIL-REGIME-LABEL-001`）：
  执行器阻止了前视，但**不能**阻止研究方在全样本上反复挑选 `cuts` / `threshold`；这类选择须作为试验计入 trial count（Phase 6 验收标准）。

## 测试证据

`tests/plugins/states/test_state_regimes.py`（契约套件 × 3、精确次序统计、历史不足、窗口约束拟合、趋势标签、资金费率不可用）；
`tests/plugins/states/test_state_volatility_events.py`（契约套件 × 2、精确带 / 阈值边界、除零、缺失、仅用最新可见值、
规格参数哈希绑定，ADR-0085）；
`tests/contract_suites/state.py`（含因果扰动、训练窗口、Outcome 拒绝）；`tests/infrastructure/state/test_state_runner.py`
（未来扰动、偷看的 Provider、缺种子被拒、bar → feature → state → 表）；`tests/research/states/test_state_diagnostics.py`。
端到端测试使用 Binance 格式夹具 K 线，不是真实行情；没有状态在正式 Research Dataset 上运行过。

## 缺口（待规格化 / 待批准）

| ID | 缺口 | 说明 |
|---|---|---|
| S-1 | 训练型（随机）状态模型族 | 见"训练型与种子"中未定义的四项；需要新的模型族规格（及可能的持久化 ADR），不是现有 Provider 的参数变化 |
| S-2 | 分位模型的 `seed` 无实际作用 | 形式上满足 roadmap；是否改为规则型（无 seed）是新规格版本的选择，需研究方 / Codex 决定，本次不改 |
| S-3 | 压缩 / 扩张 / 冲击（阈值形式） | 已实现为 `volatility_squeeze@1.0.0` / `return_shock@1.0.0`（ADR-0085），`squeeze_below`、`expansion_above`、`k` 均为规格参数、无默认值，须按实验预注册；`MST-SHOCK-001` 提到的跳跃检验形式仍未实现 |
| S-4 | 资金费率、风险偏好体制 | 输入不在 ADR-0022 范围；扩大数据范围需另立 ADR |

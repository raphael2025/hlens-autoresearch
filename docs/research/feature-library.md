# Feature Library

| 字段 | 值 |
|---|---|
| 类型 | `feature` |
| 状态 | 首批 3 个 bar 特征已实现（Phase 1 F4，[ADR-0030](../adr/0030-feature-provider-contract.md)；REVIEW_PENDING，未验收）；其余条目只是文献 / 外部知识库候选 |
| 首次填充 | Phase 1 |

从 Representation 计算的时间序列特征（FeatureSpec）。契约 `core/contracts/feature.py`，执行器 `infrastructure/feature/`，
实现 `plugins/features/bars.py`。

## 条目字段（以 `core/contracts` 为准）

| 字段 | 说明 |
|---|---|
| `name@version` | 唯一标识（`FeatureSpec.ref`）；参数进 spec，由 spec hash 绑定 |
| `definition` | 计算定义 |
| `inputs` | Canonical / Representation 引用（首批为 `representation:canonical_bar_1m@1.0.0`） |
| `params` | 参数与默认值（Feature 没有参数空间字段；参数是规格参数，不是验证阈值） |
| `available_lag` | 可用延迟：执行器只交出 `available_time + available_lag <= t` 且 `knowledge_time <= knowledge_cutoff` 的观察（ADR-0030） |
| `output_schema` | 输出类型：`Decimal \| int \| bool \| None`（`None` = 显式不可计算，不填补） |
| `provider` | 实现插件 descriptor `name@version`（可能与 feature 名不同，见下表） |
| `deterministic` | FeatureProvider 必须确定性（`deterministic: Literal[True]`） |
| `source` | 出处 |

## 通用规则

- 条目登记 ≠ 条目有效。所有条目默认状态为 `IDEA` 或 `UNVERIFIED`，只有 Lifecycle 状态能说明其是否经过验证。
- 每个条目必须有出处（`source`）；来源为本系统实验时引用 ExperimentRun ID。
- 已发布版本不可修改；修改 = 新版本。
- 被拒绝 / 失败的条目**不删除**，其状态更新并在 [failure-registry.md](failure-registry.md) 中登记。
- 本 Markdown 是人类可读索引；Phase 0 之后，权威数据存放在 Control Plane Registry 中，本文件由其生成或与其同步。
- Feature 只描述"是什么"，不是 Factor、Alpha 或 Strategy：一个 Feature 的数值水平本身不构成收益主张。
  Indicator（RSI / MACD / ATR 等已定义的计算器）在本项目中没有独立的契约类型，实现时同样是 FeatureSpec。

### 规格状态（本库专用；不是 Lifecycle 状态，也不是知识库证据等级）

| 值 | 含义 |
|---|---|
| `DOCUMENTED` | 只有文献 / 外部知识库描述，主项目没有代码 |
| `IMPLEMENTED` | 主项目有代码与定向测试（REVIEW_PENDING 或 CODE_COMPLETE / DEBUG_PENDING）；**不是**验收，也不是验证 |
| `NOT_VALIDATED` | 没有任何经 Validation Pipeline 判定的报告（Profile 数值未冻结，今天没有实验能被判定）；当前**所有**条目都是 |
| `INPUT_UNAVAILABLE` | 必需输入不在当前数据范围（ADR-0022：Binance 现货 BTCUSDT / ETHUSDT 的归档 aggTrades 与 1m K 线 + REST 补尾） |
| `UNSPECIFIED` | 算法、参数或输入路径未被已接受 ADR / 现有契约定义；实现前须先规格化并批准 |

### 三类数值不得混写

- **文献原始参数**：来源论文 / 知识库条目报告的参数，只作出处记录。
- **项目实验搜索空间**：由规格或模块常量声明的取值集合（trial count 的来源，C-T1）；Feature 契约没有参数空间字段，
  本库目前没有任何 Feature 声明了搜索空间。
- **Validation Profile 阈值**：属验证层，数值 TBD（Phase 4 校准后冻结），**不在本库出现**。

### 外部知识库引用

`hlens-knowledge:<ID>` 指独立仓库 `hlens-knowledge`（R4，2026-09-23）中的只读条目。它**不是** `KnowledgeItem`，
不能被 `LocalKnowledgeProvider` 检索，也没有自动接入或同步到本项目；引用只为追溯出处。其证据等级（`ACADEMIC`、
`DOCUMENTED`、`COMMUNITY_REPORTED` 等）是该仓库对来源的分级，不是本项目 E0～E4 证据等级，更不是本项目的验证结论。

## 数据可用性（当前）

| 输入 | Canonical 字段 | 能否进入 Feature 请求 |
|---|---|---|
| 1m K 线 | `open` / `high` / `low` / `close` / `volume` / `quote_volume` / `trade_count` / `taker_buy_base_volume` / `taker_buy_quote_volume` | 能：`infrastructure/feature/observations.py` 把这些列作为观察值；更高周期经 `hlens.canonical.resample@1.0.0`（只接受整除一天的分钟周期，缺分钟不补） |
| aggTrades | `price` / `quantity` / `buyer_is_maker` / `event_time` / `venue_trade_id` | **不能**：没有从 Canonical 成交构造 Feature 请求的路径（`UNSPECIFIED`） |
| 资金费率、持仓量、基差、强平、订单簿、期权隐含波动率、链上、市值、其他标的 | 无 | `INPUT_UNAVAILABLE` |

"能进入 Feature 请求"只表示代码路径与字段存在。正式 Research Dataset 的历史构建仍受 D-LIST（[ADR-0051](../adr/0051-listing-history-assumption.md) Proposed，暂缓）
与 D-HIST（[ADR-0032](../adr/0032-archive-event-time-availability-assumption.md) 假设须显式绑定）约束；本机只有 D-NET 授权下载的少量天数样本。

## 现状矩阵

| 方法 | 文献 / 知识库 | 主项目实现 | 输入 | 测试证据 | 规格状态 |
|---|---|---|---|---|---|
| 单 bar 对数收益 | 定义性计算（无单独出处） | `bar_log_return@1.0.0` | 1m / 派生 bar `close` | 契约套件、手算值、管线、红队 | `IMPLEMENTED · NOT_VALIDATED` |
| 已实现波动率 | `hlens-knowledge:FEA-RV-001` | `bar_realized_vol_<n>@1.0.0` | bar `close` | 同上 | `IMPLEMENTED · NOT_VALIDATED` |
| 成交量合计 | `hlens-knowledge:FEA-ADV-001`（相关，不同：ADV 为均值） | `bar_volume_sum_<n>@1.0.0` | bar `volume` | 同上 | `IMPLEMENTED · NOT_VALIDATED` |
| 区间波动率（Parkinson / Garman–Klass / Yang–Zhang） | `FEA-PARKINSON-001`、`FEA-GK-001`、`FEA-YZ-001` | 无 | bar OHLC（可得） | 无 | `DOCUMENTED · UNSPECIFIED` |
| 跳跃二次变差（RV − 双幂变差） | `FEA-JUMP-QV-001` | 无 | bar `close`（可得） | 无 | `DOCUMENTED · UNSPECIFIED` |
| 经典指标（ATR / RSI / MACD / 布林带 / VWAP） | `IND-ATR-001`、`IND-RSI-001`、`IND-MACD-001`、`IND-BBANDS-001`、`IND-VWAP-001` | 无 | bar OHLCV（可得） | 无 | `DOCUMENTED · UNSPECIFIED` |
| 趋势强度（ADX 类） | `FEA-TREND-STRENGTH-001` | 无（状态 `trend_range` 用效率比，是另一种操作化） | bar HLC（可得） | 无 | `DOCUMENTED · UNSPECIFIED` |
| Taker 流量 / 主动买卖差 | `FEA-TAKER-FLOW-001`、`ORF-IMBALANCE-001`、`ORF-CVD-001` | 无 | K 线 `taker_buy_base_volume`（可得）；aggTrades `buyer_is_maker`（无 Feature 路径） | 无 | `DOCUMENTED · UNSPECIFIED` |
| Amihud 非流动性 | `MSTX-AMIHUD-001` | 无 | bar `close` + `quote_volume`（可得） | 无 | `DOCUMENTED · UNSPECIFIED` |
| 高低价价差估计（Corwin–Schultz / Abdi–Ranaldo） | `FEA-CS-SPREAD-001`、`FEA-AR-SPREAD-001` | 无 | 日 bar HLC（可由派生得到） | 无 | `DOCUMENTED · UNSPECIFIED` |
| 成交间隔 | `FEA-DURATION-001` | 无 | aggTrades `event_time`（无 Feature 路径） | 无 | `DOCUMENTED · UNSPECIFIED` |
| VPIN / BVC | `ORF-VPIN-001`、`ORF-BVC-001` | 无 | aggTrades（无 Feature 路径）；成交量时钟 bar 未实现 | 无 | `DOCUMENTED · UNSPECIFIED` |
| 分数阶差分 | `FEA-FRACDIFF-001` | 无 | bar `close`（可得） | 无 | `DOCUMENTED · UNSPECIFIED` |
| 资金费率 / 持仓量 / 基差 / 强平强度 | `FEA-FUNDING-001`、`FEA-OI-001`、`FEA-BASIS-001`、`FEA-LIQ-001` | 无 | 未采集 | 无 | `DOCUMENTED · INPUT_UNAVAILABLE` |
| 订单簿深度 / 报价价差 / 最优价 OFI | `FEA-DEPTH-001`、`FEA-QUOTED-SPREAD-001`、`ORF-OFI-001` | 无 | 未采集（无订单簿） | 无 | `DOCUMENTED · INPUT_UNAVAILABLE` |
| 链上 / ETF 流 / 期权 | `FEA-MVRV-001`、`FEA-SOPR-001`、`FEA-ETF-FLOW-001`、`FEA-VRP-001`、`FEA-OPTSKEW-001` | 无 | 未采集 | 无 | `DOCUMENTED · INPUT_UNAVAILABLE` |

知识库 `02_FEATURES` 中其余以股票会计 / 公司事件为输入的条目（账面杠杆、分析师预测、股本换手等）对现货加密没有对应输入，未逐条收录。

## 条目索引（已实现）

三者的执行器、契约与测试相同：`infrastructure/feature/runner.py` 按评估时刻截断输入并逐个核对回答；Provider 内部再按
`spec.available_lag` 截断一次。窗口只取**最新的连续 bar**（相邻 bar 首尾相接）；历史不足或窗口内有缺口 → `None`，
`inputs_used = 0`，不填补。多标的、重叠 bar、`close <= 0`、负成交量或非 `Decimal` 值直接拒绝。
spec 必须恰为其参数的规范重建（内容哈希核对）。

| name@version | provider descriptor | 定义（按实现） | 参数（默认） | 状态 |
|---|---|---|---|---|
| `bar_log_return@1.0.0` | `bar_log_return@1.0.0` | 最新两根连续 bar：`r = ln(close_k / close_{k-1})`，50 位精度计算，half-even 量化到 `scale` 位 | `scale`=18、`available_lag`=0、`bar_input`=`representation:canonical_bar_1m@1.0.0` | `IMPLEMENTED · NOT_VALIDATED` |
| `bar_realized_vol_<n>@1.0.0` | `bar_realized_volatility@1.0.0` | 最新 `n+1` 根连续 bar 的 `n` 个单 bar 对数收益：`RV = sqrt(Σ r_i²)`；未去均值、未年化，单位是所选 bar 周期上的收益 | `window`=`n`（**必填**）；`scale` / `available_lag` / `bar_input` 同上 | `IMPLEMENTED · NOT_VALIDATED` |
| `bar_volume_sum_<n>@1.0.0` | `bar_volume_sum@1.0.0` | 最新 `n` 根连续 bar 的成交量精确求和（80 位上下文，不精确即拒绝） | `window`=`n`（**必填**）；`available_lag` / `bar_input` 同上；无 `scale` | `IMPLEMENTED · NOT_VALIDATED` |

- **测试证据**：`tests/plugins/features/test_bar_features.py`（契约套件 × 3、手算值、缺口 → `None`、历史不足、lag、
  参数进哈希、伪造 spec 被拒）；`tests/contract_suites/feature.py`（含因果扰动）；`tests/infrastructure/feature/`
  （runner、数据集、派生 bar、归档 / REST → Canonical → PIT → Feature 管线）；`tests/infrastructure/redteam/test_rt_features.py`。
  端到端测试使用 **Binance 格式的夹具 K 线**，不是真实行情；没有任何 Feature 在正式 Research Dataset 上运行过。
- **验收状态**：F4 属 Phase 1 实现组，REVIEW_PENDING；Phase 1 未验收。
- **使用方**：`bar_log_return` → 策略 `tsmom_bars` / `xsmom_bars`、状态 `trend_range`；`bar_realized_vol_<n>` →
  状态 `volatility_regime`、风控 `vol_target_bars`（默认 `bar_realized_vol_60`）；`bar_volume_sum_<n>` → 状态 `liquidity_regime`。
  G4 容量检查**不**使用 `bar_volume_sum`，它按 ADR-0064 使用已执行 bar 的成交量。
- **与文献的区别**（`FEA-RV-001`，Andersen, Bollerslev, Diebold, Labys 2003, *Econometrica*，doi:10.1111/1468-0262.00418；
  知识库证据 `ACADEMIC`）：文献常把日内收益聚合为日度 RV 并讨论微观结构噪声修正；本项目按所选 bar 周期在 `n` 根 bar 上求和，
  无噪声修正，`n` 与 bar 周期由规格声明，没有搜索空间。
- **已知失效模式**：1m 收益的微观结构噪声会抬高 RV；把 RV 水平当作收益信号（`hlens-knowledge:FAIL-RV-AS-ALPHA-001`）；
  成交量含单一交易所、可能的刷量（`MSTX-AMIHUD-001`、`FEA-ADV-001` 的已知失效条件）；把成交量合计、ADV、换手率与强平强度混为
  同一"流动性"（`FAIL-TURNOVER-EQUALS-ILLIQ-001`）。
- **研究侧复算**：`research/strategies/signals.py::bar_signals` 在不经过数据面的路径上复算两个信号；自 `1023afb` 起它对未量化的
  50 位对数收益求平方和，与 Provider 逐值相等（运算序列相同；`tests/research/strategies/test_signals_parity.py`）。此前先量化再平方，末位可能不同。

## 候选方法（未实现，只作规格输入）

以下方法的输入在当前数据范围内可得，但主项目没有实现，参数、窗口、时钟与边界处理均未规格化。原文参数只作出处记录。

| 方法 | 出处（知识库 ID；作者，年份；知识库证据） | 公式 / 定义（简述） | 原文参数 | 待规格化 / 已知失效 |
|---|---|---|---|---|
| Parkinson 区间波动率 | `FEA-PARKINSON-001`；Parkinson 1980，*J. Business*，doi:10.1086/296071；`ACADEMIC` | `σ² ≈ (ln(H/L))² / (4 ln 2)` 每周期 | 周期聚合方式 | 假设无跳跃、无漂移；坏 tick 抬高 H/L |
| Garman–Klass | `FEA-GK-001`；Garman & Klass 1980，doi:10.1086/296072；`ACADEMIC` | `(ln(H/L))²` 与 `(ln(C/O))²` 项的组合（系数见原文） | 变体选择 | 跳空 / 跳跃偏差 |
| Yang–Zhang | `FEA-YZ-001`；Yang & Zhang 2000，doi:10.1086/209650；`ACADEMIC` | 隔夜方差 + 开收方差 + Rogers–Satchell 项的加权（原文 Eq. 7） | 窗口 N、权重 k | 24/7 市场没有"隔夜"，须先定义会话边界 |
| 跳跃二次变差 | `FEA-JUMP-QV-001`；Barndorff-Nielsen & Shephard 2004，*J. Fin. Econometrics*，doi:10.1093/jjfinec/nbh001；`ACADEMIC` | `max(RV − μ₁⁻²·BPV, 0)` | 采样频率、截断 | 有限样本为负、噪声 |
| ATR | `IND-ATR-001`；Wilder 1978；`DOCUMENTED` | `TR = max(H−L, |H−C₋₁|, |L−C₋₁|)`，Wilder 平滑 | N=14（常见） | 24/7 会话定义 |
| RSI / MACD / 布林带 | `IND-RSI-001`（Wilder 1978）、`IND-MACD-001`（Appel）、`IND-BBANDS-001`（Bollinger）；`DOCUMENTED` | 见各条目 | 14；12/26/9；20 / 2（常见） | 指标不是 Alpha（`FAIL-INDICATOR-AS-ALPHA-001`）；阈值挖掘 |
| VWAP | `IND-VWAP-001`；出处 UNKNOWN；`DOCUMENTED` | `Σ P_i V_i / Σ V_i` | 会话 / 窗口 | 会话边界选择、刷量 |
| 趋势强度 | `FEA-TREND-STRENGTH-001`；Wilder 1978 + 实务；`DOCUMENTED` | ADX/DMI 或 `|SMA 斜率| / ATR` | N=14（ADX 常见） | 滞后、阈值挖掘 |
| Taker 失衡 | `FEA-TAKER-FLOW-001`（`DOCUMENTED`）、`ORF-IMBALANCE-001`（`ACADEMIC`）、`ORF-CVD-001`（`COMMUNITY_REPORTED`） | `delta = taker_buy − taker_sell`，`imbalance = delta / (buy + sell)`；CVD 为累计 | 窗口、归一化、CVD 重置 | 刷量、交易所标记差异；CVD 的会话重置在 24/7 下无定义；K 线 taker 字段的语义须以官方证据核对 |
| Amihud ILLIQ | `MSTX-AMIHUD-001`；Amihud 2002，*J. Financial Markets*，doi:10.1016/S1386-4181(01)00024-6；`ACADEMIC` | `(1/D) Σ |R_d| / VOLD_d` | D（原文日度） | 零成交量、刷量、短周期噪声 |
| Corwin–Schultz / Abdi–Ranaldo | `FEA-CS-SPREAD-001`（doi:10.1111/j.1540-6261.2012.01729.x）、`FEA-AR-SPREAD-001`（doi:10.1093/rfs/hhx084）；`ACADEMIC` | 由 1 日 / 2 日高低价比（或收盘 + 高低价）估计有效价差 | 负估计处理、隔夜调整 | 原文针对股票日度数据；隔夜调整在 24/7 下无对应 |
| 分数阶差分 | `FEA-FRACDIFF-001`；López de Prado（AFML）/ mlfinlab；`DOCUMENTED` | 分数阶 `d` 的加权滞后展开 | d、窗口、阈值 | 拟合 `d` 时的前视；mlfinlab 许可证 |

aggTrades 输入的方法（成交间隔 `FEA-DURATION-001`、以 `buyer_is_maker` 计的精确 taker 流量、VPIN `ORF-VPIN-001`）先需要一条
从 Canonical 成交构造 Feature 请求的路径；BVC（`ORF-BVC-001`）用于缺少主动方标记的数据，而 aggTrades 已带标记，且其准确性有争议
（`FAIL-BVC-ACCURACY-001`）；VPIN 的预测主张同样有争议（`FAIL-VPIN-DISPUTE-001`）。

## 缺口（待规格化 / 待批准）

| ID | 缺口 | 说明 |
|---|---|---|
| F-1 | aggTrades → Feature 的输入路径 | Canonical 成交已存在，但没有 Feature 请求构造器；成交按小时分段选择的容量约束（PROJECT_STATUS §7）须一并考虑。roadmap Phase 1 的"成交量 bar 等基础 Representation"也尚无实现 |
| F-2 | 候选特征的具体规格 | 上表方法的窗口、会话定义（24/7）、异常值处理均未定义；只能由研究方以新 FeatureSpec 提出，不得在本库中预设数值 |
| F-3 | Feature 参数空间 | Feature 契约无参数空间字段；多个 `n` 的特征作为不同 FeatureSpec 进入实验时如何计入 trial count，由使用它的实验 / 策略声明 |
| F-4 | `signals.py` 与 Provider 的 RV 差异 | **已修复**（`1023afb`，CODE_COMPLETE / REVIEW_PENDING）：研究侧复算改为与 Provider 相同的未量化平方和，回归测试覆盖构造反例、随机游走与缺口 + 延迟可用 |

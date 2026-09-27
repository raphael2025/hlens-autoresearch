# Factor Library

| 字段 | 值 |
|---|---|
| 类型 | 无独立契约类型：`core/domain/base.py::Kind` 没有 `factor`；因子今天只能表达为 Feature（打分）+ Strategy（组合规则） |
| 状态 | 无已登记因子对象；动量因子的动量腿以研究策略 `xsmom_bars@1.0.0` 实现（见 [strategy-library.md](strategy-library.md)）；其余为文献 / 外部知识库候选 |
| 首次填充 | Phase 0.5 / 5 |

横截面或时间序列因子（通常用于排序或打分的 Feature 组合）。

## 条目字段（草案，以 `core/contracts` 为准）

契约中没有 FactorSpec。下列字段是人类可读索引的草案：`definition` / `inputs` 落在 FeatureSpec，`universe` / `rebalance` / 组合构造落在
StrategySpec（及数据集的 universe 规格），`lifecycle_state` / `validation_reports` 属于实现该因子的 Strategy 对象。

| 字段 | 说明 |
|---|---|
| `name@version` | 唯一标识 |
| `definition` | 数学定义 |
| `inputs` | Feature 引用 |
| `universe` | 适用标的池规则 |
| `rebalance` | 调仓频率 |
| `source` | 出处 |
| `known_decay` | 已知衰减或拥挤证据 |
| `lifecycle_state` | 状态 |
| `validation_reports` | 报告引用 |

## 通用规则

- 条目登记 ≠ 条目有效。所有条目默认状态为 `IDEA` 或 `UNVERIFIED`，只有 Lifecycle 状态能说明其是否经过验证。
- 每个条目必须有出处（`source`）；来源为本系统实验时引用 ExperimentRun ID。
- 已发布版本不可修改；修改 = 新版本。
- 被拒绝 / 失败的条目**不删除**，其状态更新并在 [failure-registry.md](failure-registry.md) 中登记。
- 本 Markdown 是人类可读索引；Phase 0 之后，权威数据存放在 Control Plane Registry 中，本文件由其生成或与其同步。
- Factor ≠ Alpha ≠ Strategy：因子是有经济 / 行为 / 统计解释、可系统计算的变量；"因子能预测收益"是待检验的 Alpha 主张；
  完整的调仓、仓位与风控规则才是 Strategy。

### 规格状态（本库专用；不是 Lifecycle 状态，也不是知识库证据等级）

| 值 | 含义 |
|---|---|
| `DOCUMENTED` | 只有文献 / 外部知识库描述，主项目没有代码 |
| `IMPLEMENTED` | 主项目有代码与定向测试（FRAMEWORK_IMPLEMENTED 或 CODE_COMPLETE / DEBUG_PENDING）；**不是**验收，也不是验证 |
| `NOT_VALIDATED` | 没有任何经 Validation Pipeline 判定的报告；当前**所有**条目都是 |
| `INPUT_UNAVAILABLE` | 必需输入不在当前数据范围（ADR-0022：只有 Binance 现货 BTCUSDT / ETHUSDT 的 aggTrades 与 1m K 线；没有市值、资金费率、其他标的、基本面） |
| `UNSPECIFIED` | 算法、参数或契约表达未被已接受 ADR / 现有契约定义 |

### 三类数值不得混写

文献原始参数（只作出处）、项目实验搜索空间（在 StrategySpec 的 `param_search_space` 中声明）、Validation Profile 阈值（TBD，不在本库）
分别记录。

### 外部知识库引用

`hlens-knowledge:<ID>` 指独立仓库 `hlens-knowledge`（R4，2026-09-23）中的只读条目：不是 `KnowledgeItem`、不可被本项目检索、
没有自动接入或同步。其证据等级是该仓库对来源的分级；"迁移到加密货币"的判断引自其 `99_REGISTRY/CRYPTO_TRANSFERABILITY.md`，
同样只是资料，不是本项目结论。

## 现状矩阵

横截面因子需要足够宽的标的池；当前数据范围只有两个标的，任何横截面排序最多是"一多一空"。

| 因子 | 文献 / 知识库（证据） | 主项目实现 | 输入 | 测试证据 | 规格状态 |
|---|---|---|---|---|---|
| 时间序列动量 | `FAC-MOM-TS-001`、`ALP-TSMOM-001`、`PAP-TSMOM-2012-001`（`ACADEMIC`）；项目种子 `knowledge:strategy_time_series_momentum@1.0.0`（E4）、`knowledge:strategy_crypto_time_series_momentum@1.0.0`（E2） | 研究策略 `tsmom_bars@1.0.0`（无独立因子对象） | `bar_log_return`（可算） | 见 strategy-library | `IMPLEMENTED · NOT_VALIDATED` |
| 加密横截面动量 | `FAC-CRYPTO-MOM-001`、`PAP-LIU-CRYPTO-FAC-2022-001`（`ACADEMIC`）；项目种子 `knowledge:factor_crypto_market_size_momentum@1.0.0`（E3） | 研究策略 `xsmom_bars@1.0.0`（只实现动量腿） | `bar_log_return`（可算）；标的池只有 2 个 | 见 strategy-library | `IMPLEMENTED · NOT_VALIDATED`（横截面退化） |
| 加密规模 / 市场因子 | `FAC-CRYPTO-SIZE-001`、`PAP-LIU-CRYPTO-FAC-2022-001` | 无 | 市值、宽标的池未采集 | 无 | `DOCUMENTED · INPUT_UNAVAILABLE` |
| 经典横截面动量 | `FAC-MOM-CS-001`、`ALP-CSMOM-001`（Jegadeesh & Titman 1993，股票） | 无（加密版见上） | 宽标的池未采集 | 无 | `DOCUMENTED · INPUT_UNAVAILABLE` |
| 短期 / 长期反转 | `STR-CSMR-001`、`ALP-CSREV-001`（Jegadeesh 1990）、`FAC-LTREV-001`（De Bondt & Thaler 1985） | 无 | 宽标的池与多年历史数据集均不可得 | 无 | `DOCUMENTED · INPUT_UNAVAILABLE` |
| 低波动 / BAB / IVOL / MAX | `FAC-BAB-001`、`FAC-IVOL-001`、`FAC-MAX-001`（股票为主；知识库只把 Low Vol / BAB 的加密迁移状态记为 UNKNOWN，IVOL / MAX 未记录） | 无 | 宽标的池未采集 | 无 | `DOCUMENTED · INPUT_UNAVAILABLE` |
| Carry | `FAC-CARRY-001`（Koijen et al. 2018）；加密 carry 需资金费率 / 基差 | 无 | 资金费率、基差未采集 | 无 | `DOCUMENTED · INPUT_UNAVAILABLE` |
| 流动性风险 | `FAC-LIQ-PS-001`（Pástor & Stambaugh 2003，股票） | 无 | 宽标的池未采集 | 无 | `DOCUMENTED · INPUT_UNAVAILABLE` |
| 股票会计 / 基本面因子 | `FAC-VALUE-001`、`FAC-PROFIT-001`、`FAC-QUALITY-001`、`FAC-INVEST-001`、`FAC-ACCRUAL-001`、`FAC-NOA-001`、`FAC-ISSUE-001`、`FAC-ORGCAP-001` 等 | 无 | 现货加密无对应会计数据；知识库记"朴素加密价值"为失败类（`FAIL-NAIVE-CRYPTO-VALUE-001`） | 无 | `DOCUMENTED · INPUT_UNAVAILABLE` |

## 代表条目（出处与参数区分）

### 时间序列动量

- **定义**：同一资产过去 `L` 期收益的符号（或缩放值）决定持仓方向；常与波动率缩放仓位组合（风控层，见 [risk-library.md](risk-library.md)）。
- **出处**：Moskowitz, Ooi & Pedersen (2012), *Journal of Financial Economics*, doi:10.1016/j.jfineco.2011.11.003（`hlens-knowledge:FAC-MOM-TS-001`）。
  研究市场为多资产类别期货的月度面板；知识库记原文参数为 `NOT_REPORTED`（论文特定），并注明加密可迁移性 UNKNOWN。
  加密版本：Liu & Tsyvinski (2021), *Review of Financial Studies*（项目种子 E2，周度收益）。
- **项目实现的区别**：`tsmom_bars` 用 `bar_log_return` 在 bar 级别求和。搜索空间 `lookback ∈ {60, 240, 1440}` 以**所用 bar 数**计；
  在 1m bar 上即 1 小时 / 4 小时 / 1 天，比原文的月度形成期短几个数量级。这是项目声明的实验空间，不是对原文的复现。
- **已知失效**：动量崩溃（`FAIL-MOM-CRASH-001`，Daniel & Moskowitz 2016）；拥挤与成本（`FAIL-COST-EROSION-001`）；发表后衰减（`FAIL-POSTPUB-DECAY-001`）。

### 加密横截面动量（Liu, Tsyvinski & Wu 2022 的动量腿）

- **定义**：按过去 `J` 期收益排序，做多赢家、做空输家，持有 `K` 期（`hlens-knowledge:FAC-CRYPTO-MOM-001`）。
- **出处**：Liu, Tsyvinski & Wu (2022), "Common Risk Factors in Cryptocurrency", *Journal of Finance*, doi:10.1111/jofi.13119；
  宽加密货币面板、周度 / 月度；知识库 `ACADEMIC`，注明"不是实盘证据"。原文还有市场与规模两个因子，需要市值与宽标的池。
- **项目实现的区别**：`xsmom_bars` 只实现动量腿；`k = min(top_n, m // 2)`，两个标的时 `k ≤ 1`，`top_n ∈ {1, 2}` 两个取值在当前数据上等价。
  形成期同样以 bar 数计（`lookback ∈ {60, 240, 1440}`）。这不是对原文因子的复现，只是同一方法在当前数据范围内的退化形式。
- **已知失效**：把股票 JT 动量等同于加密动量、忽略成本、与时间序列动量混淆（知识库已知失效条件）；上市 / 下架偏差（当前 universe 由
  ADR-0024 构建，但历史上市记录受 D-LIST 阻塞）。

## 缺口（待规格化 / 待批准）

| ID | 缺口 | 说明 |
|---|---|---|
| FA-1 | 因子没有契约类型 | 若要把因子作为独立的可版本化对象（打分、分组、调仓分开登记），需新增 `Kind` 或新规格模型，属契约变更（H1，需 ADR）。在此之前因子以 Feature + Strategy 表达，本库只做索引 |
| FA-2 | 横截面宽度 | 两个标的无法复现任何横截面因子研究；扩大标的范围需修订 ADR-0022 的数据范围 |
| FA-3 | 市值、资金费率、基差 | 规模、市场、carry 因子的输入未采集；同上需数据范围 ADR |

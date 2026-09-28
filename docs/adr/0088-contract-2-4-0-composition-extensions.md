# ADR-0088: 契约 2.4.0（additive）：组合策略、事件 bar 规格、峰值权益、合成效应与波动率缩放屏障

| 字段 | 值 |
|---|---|
| 状态 | **Accepted**（2026-09-28） |
| 日期 | 2026-09-28 |
| 决策者 | Claude Code（PM），依 Raphael 2026-09-28 授权（CLAUDE.md §0：冻结契约变更经 ADR 由 PM 决定） |
| 起草者 | Claude Code（PM） |
| 相关 Phase | Phase 3 / 4 / 5 / 7 / 9 |
| 影响范围 | Contract（`core/domain/specs.py`、`core/contracts/{strategy,synthetic,outcome}.py`）；Schema；契约文档 |
| 是否破坏兼容 | 否：additive minor，`CONTRACT_SCHEMA_VERSION` 从 2.3.0 升到 2.4.0；所有新字段可选且默认值等于现状，旧 payload 与内容哈希不变 |

## 背景

2026-09-28 的代码补全轮次中，有几处需要在冻结契约上增字段才能继续：

| 缺口 | 来源 | 卡住的内容 |
|---|---|---|
| `EventSpec` 没有 bar 规格 | P7 `temporal` | ADR-0082 无法判断两个输入事件是否用同一种 bar |
| `StrategySpec` 不能引用策略，也不能按状态门控 | P7 `conditioning` / `ensemble` / `negation` | ADR-0082 标为 OPEN |
| `PortfolioState` 没有峰值权益 | `drawdown_control` | IMPL-STRAT 停下 |
| `PlantedEffect` 只有自相关一种效应 | P9 合成市场 | 无法植入其他效应 |
| `OutcomeMethod` 没有波动率缩放屏障 | outcome-library O-2 | 该标签方法无法实现 |

这些都属于 CLAUDE.md H1 的冻结契约范围。Raphael 已于 2026-09-28 授权 PM 经 ADR 决定此类变更。

## 决策

按 ADR-0052 的 minor 版本机制，契约升到 **2.4.0**，只做新增。

1. **`EventSpec.bar_spec: Ref | None = None`**
   - 非空时必须指向 `Kind.REPRESENTATION`，声明事件判定所用的 bar 表示，例如 `representation:canonical_bar_1m@1.0.0`。
   - 默认 `None` 表示未声明，与现状相同。
   - P7 `temporal` 的 lowering 要求两个输入的 `bar_spec` 都非空且相同，否则 `operator_open`。
2. **`StrategySpec.composition: StrategyComposition | None = None`**
   - `StrategyComposition` 是按 `type` 字段判别的联合，共三种：
     - `ConditionedStrategy(type="conditioned", base: Ref[STRATEGY], state: Ref[STATE], state_value: NonEmptyStr)`
       - 状态等于 `state_value` 时持有 base 的目标仓位，否则空仓；
       - 未知或缺失的状态视为空仓；
       - 每个 (base, state, state_value) 计一个 trial。
     - `EnsembleStrategy(type="ensemble", members: tuple[Ref[STRATEGY], ...], rule="equal_weight_mean")`
       - 至少 2 个成员，成员不得重复；
       - 各成员的目标仓位等权平均；
       - 成员的风险政策和适用标的必须一致，沿用 ADR-0069 的规则。
     - `NegatedStrategy(type="negated", base: Ref[STRATEGY])`
       - 目标仓位取反；
       - **不**作为验证负对照（负对照由验证层定义）；
       - 现货下的执行仍受做空成本缺口（ST-4）限制，由 Provider 负责 fail closed。
   - 组合策略的 `signals` 必须覆盖 base 或成员需要的全部信号；`conditioned` 还要加上门控状态。
   - 组合对象不得引用自身；嵌套组合与循环引用的检查由 Registry 在解析时完成。
3. **`PortfolioState.peak_equity: PositiveDecimal | None = None`**
   - 非空时必须 ≥ `equity`。
   - 由回测 / 执行层按已实现的权益路径提供；风控不得自己记忆峰值。
   - `drawdown_control` 在 `equity` 或 `peak_equity` 缺失时 fail closed。
4. **合成效应的判别联合**
   - 现有 `PlantedEffect`（`kind="return_autocorrelation"`）保持不变。
   - 新增两个模型：
     - `VolatilityClusteringEffect(kind="volatility_clustering", omega, alpha, beta)`：GARCH(1,1) 的方差递推；要求 omega > 0、alpha ≥ 0、beta ≥ 0、alpha + beta < 1。
     - `JumpEffect(kind="jump", intensity_per_minute, jump_scale)`：Poisson 跳跃；强度在 (0, 1) 之间，跳幅尺度 > 0；跳幅由种子确定性生成。
   - `SyntheticMarketSpec` 中效应字段的类型放宽为三者的联合。
   - 旧 payload 仍按 `return_autocorrelation` 解析，内容哈希不变。
5. **`OutcomeMethod.VOL_SCALED_TRIPLE_BARRIER`**
   - `OutcomeLabelSpec` 新增两个可选字段：
     - `volatility_feature: Ref[FEATURE] | None`
     - `barrier_multiplier: PositiveDecimal | None`
   - 这两个字段只在该方法下必填，在其他方法下必须为空。
   - 上下屏障 = entry × (1 ± multiplier × 入场时可见的波动率特征值)，垂直屏障 = horizon。
   - 同一 bar 同时触碰两个屏障时，沿用 triple barrier 的保守判定。

6. **`UniverseMember.assumption: PolicyBinding | None = None`**（ADR-0051 §3，2026-09-28 补入本批次）
   - 该成员区间来自 `hlens.listing.observed-state-backfill-assumption` 假设时，填写所绑定的政策（名称、版本、哈希）。
   - 默认 `None` 表示观测得到，与现状相同。
   - 非空时，政策 ID 必须是 ADR-0051 的政策标识。
   - `UniverseBuilt.assumed` 属于 infrastructure DTO，不在契约层，由 ADR-0051 第二期实现。

本批次**不**包括：

- Profile 的 `confidence_level` 字段：会改变所有已钉定的报告哈希，而且数值属于 D-09；
- 事件研究 CAR 与 meta-labeling：前者需要基准模型规格，后者需要主模型规格。

## 实施要求

- 按 ADR-0052 §4，盘点受 2.4.0 默认信封影响的已登记身份与重放路径；盘点结论写入本 ADR。
- 为新模型和新字段导出 Schema，已有 Schema 除默认信封版本外逐位不变。
- 同步 `docs/architecture/02-domain.md` 与相关契约文档。
- 契约层之外的实现由后续批次完成，本 ADR 只授权契约层：
  - P7 lowering（`research/hypotheses`）；
  - 组合策略 Provider 与 `drawdown_control`（`research/strategies`）；
  - 合成市场生成器（`plugins/synthetic`）；
  - 波动率缩放屏障 Provider（`plugins/outcomes`）；
  - 回测层提供 `peak_equity`（`plugins/backtest`）。

## 备选方案

| 方案 | 为何未选 |
|---|---|
| 组合策略放在 research 层的私有结构里，不进契约 | 组合结果必须作为 StrategySpec 进入 TrialLedger 和验证，契约不能表达就无法计数和审计 |
| 风控自行记忆权益峰值 | 破坏确定性，并可能看到未来数据（IMPL-STRAT 已指出） |
| 为每种合成效应单独建一个 `SyntheticMarketSpec` 变体 | 重复，而且已有生成器接口是单一 spec |

## 后果

- 正面：P7 的五类算子、回撤风控、更丰富的合成效应和波动率缩放标签都有了契约表达，之后实现者只需编码。
- 代价：
  - 契约一天内第二次升 minor，已钉定的哈希与版本断言会在调试阶段集中重钉；
  - 组合策略的执行语义要由后续 Provider 严格实现，并补测试。

## 实施记录：契约层（2026-09-28，任务 A2-CONTRACT-240；未运行任何测试 / lint / typecheck / Schema 导出）

实施者 Claude Code（A2），照 ADR-0077「实施记录：契约层」（2.3.0）的做法，只改契约层。按协调要求未运行 pytest / ruff /
mypy / Schema 导出，以下全部为静态自检，须由复核方真实运行后才可接受。

1. **契约版本**：`CONTRACT_SCHEMA_VERSION = "2.4.0"`，`PUBLISHED_CONTRACT_SCHEMA_VERSIONS` 追加 `"2.4.0"`
   （`core/domain/base.py`）；引入版本常量 `ADR_0088_VERSION = "2.4.0"`（`core/domain/specs.py`，各契约模块共用）。
2. **决策 1 ~ 6 的落点**（全部可选、缺省 `None` 且 `exclude_if` 省略出载荷；新内容以 `_FIELDS_SINCE` / `_VALUES_SINCE` /
   `_MODEL_SINCE` 标为 2.4.0，2.0.0 ~ 2.3.0 信封或重放作用域中出现即拒绝）：
   - 决策 1 `EventSpec.bar_spec`：非空时 `kind=representation`；
   - 决策 2 `StrategySpec.composition`：`ConditionedStrategy` / `EnsembleStrategy` / `NegatedStrategy`（`core/domain/specs.py`，
     按 `type` 判别）。契约层校验：引用 kind（base / members 为 strategy，门控为 state）、`state_value` 非空、`ensemble`
     至少 2 个且按目标身份不重复、`rule = equal_weight_mean`、组合不得引用本策略（同 name@version）、`conditioned` 的门控
     状态必须在 `signals` 中；嵌套 / 循环、成员风险政策与适用标的一致（ADR-0069）、`signals` 覆盖成员信号均需解析引用，
     留给 Registry；
   - 决策 3 `PortfolioState.peak_equity`：非空时 `equity` 也必须非空且 `peak_equity >= equity`（"非空时必须 ≥ equity"
     在 `equity` 为空时无法满足，按 fail closed 解读为拒绝）；
   - 决策 4 `VolatilityClusteringEffect`（omega > 0、alpha ≥ 0、beta ≥ 0、alpha + beta < 1）、`JumpEffect`（强度 ∈ (0, 1)、
     `jump_scale > 0`）；`SyntheticMarketSpec.effects` 放宽为按 `kind` 判别的三者联合，缺 `kind` 的旧效应按
     `return_autocorrelation` 解析；该字段已存在，无法用 `_FIELDS_SINCE` 表达，因此由 `SyntheticMarketSpec` 自身的校验器
     拒绝 2.4.0 之前信封中的新效应；
   - 决策 5 `OutcomeMethod.VOL_SCALED_TRIPLE_BARRIER`（`_VALUES_SINCE`）与 `OutcomeLabelSpec.volatility_feature`
     （`kind=feature`）/ `barrier_multiplier`（> 0）：只在该方法下必填、其他方法下必须为空；该方法下固定屏障
     `upper_barrier` / `lower_barrier` 必须为空（屏障完全由缩放公式决定，二者并存会有歧义）；`bind()` 增加对应的可选关键字参数；
   - 决策 6 `UniverseMember.assumption: PolicyBinding | None`（`core/contracts/universe.py`）：非空时 `policy_id` 必须是
     新常量 `LISTING_BACKFILL_ASSUMPTION_ID`（`hlens.listing.observed-state-backfill-assumption`），并按 `PolicyBinding`
     "使用处要求确切 role" 的既有规则要求 `role = availability`（ADR-0051 的假设是 availability 政策）。
3. **登记与 Schema**：5 个新模型追加到 `CONTRACT_MODELS` 末尾（141 → 146）。既有 141 份 Schema 中 132 份只把信封默认值
   `2.3.0` 改为 `2.4.0`；`EventSpec`、`StrategySpec`、`PortfolioState`、`RiskRequest`、`OutcomeLabelSpec`、`OutcomeRequest`、
   `SyntheticMarketSpec`、`UniverseMember`、`ResearchDatasetManifest` 9 份另外只增加 ADR-0088 的新属性 / `$defs` / 枚举取值
   （`PlantedEffect`、`SyntheticMarket` 仅信封变化）；新增 5 份 Schema 与上述新增片段均为**手工编写**，须由复核方运行
   `python -m core.contracts.registry` 重新导出并 diff，任何差异以导出为准。`schemas/v1/` 未触及。
4. **旧对象哈希**：新字段为 `None` 时不出现在载荷中，`PlantedEffect` 不变，因此 2.0.0 ~ 2.3.0 载荷（含 `SyntheticMarketSpec`
   的旧效应与 `ResearchDatasetManifest` 中的成员）按记录读取、规范字节与内容哈希逐位不变；**不需要 BLOCKED**。当前代码
   新建的对象取 2.4.0 信封，哈希与 2.3.0 孪生对象不同（minor 的预期后果，02-domain §3.3）。
5. **ADR-0052 §4 盘点（受 2.4.0 默认信封影响的已登记身份与重放路径）**，结论：**没有**随默认信封漂移、被持久化数据按内容
   引用、又无重放机制覆盖的已登记身份。
   - Phase 1 代码登记身份（V3）：`infrastructure/` 中 21 处显式 `schema_version=PHASE1_PUBLICATION_VERSION`（16 个
     `PolicyBinding` 常量、3 个 `SourceBinding` 常量、`FIRST_SLICE_UNIVERSE`，以及 ADR-0051 第一期的 `ASSUMPTION_BINDING`），
     信封固定 2.0.0；`UniverseSpecBinding` 由 `binding()` 继承 spec 信封（V4）。
   - 按记录版本重建（V1 / V7）：canonical / raw / exchangeInfo / listing / 边 / v2 manifest 与 `event.events`（逐行记录运行版本）
     同 2.3.0 盘点；ADR-0077 此后实施的 v3 dataset 路径同样按记录版本：`ManifestStore.recorded_version` / `replay_version`、
     `verify_v3` 与 evidence 读取在 manifest 记录版本作用域内重建，builder 新组用 `new_group_version()`，
     `DatasetEvidenceRule.binds` 按 (id, version, hash) 比较而非内容哈希。这些路径只要求记录版本属于
     `PUBLISHED_CONTRACT_SCHEMA_VERSIONS`，2.0.0 ~ 2.3.0 仍在其中。
   - 规则哈希（`DATASET_RULE_HASH`、v3 evidence 规则哈希、`NORMALIZER_HASH`、ADR-0051 `ASSUMPTION_SPEC_HASH` 等）是规则 spec
     字典的规范 JSON 哈希，不含信封（V5）；知识种子显式记录 `schema_version: "2.1.0"`（ADR-0055）。
   - ADR-0088 的新字段在任何已持久化数据中都不存在（没有写入者产生它们）。
   - 研究侧风险沿用 2.3.0 盘点：P7 durable loop 恢复时用当前代码构造的种子 spec / risk policy 的内容哈希比对审计行，跨 minor
     升版恢复既有 durable loop 会 fail closed（不在本 ADR 范围）。
6. **测试**：新增 `tests/test_adr_0088_contract_240.py`（版本边界、旧载荷金值、全部结构校验、Schema"只增不改"逐位核对——
   把 9 份 Schema 去掉 ADR-0088 新增后映射回 2.3.0 信封，与 2.3.0 提交字节的 SHA-256 比较）；根目录核心契约测试只更新版本号 /
   模型数期望值。
7. **仍 OPEN / 边界外**（本任务不得修改，复核或后续批次处理）：
   - `SyntheticMarket.truth` 仍为 `tuple[PlantedEffect, ...]`：本 ADR 只放宽了 `SyntheticMarketSpec.effects`，生成器无法把新效应
     记为真值——需 PM 决定是否一并放宽（additive，同样不改旧哈希）；
   - 手写 Schema 的逐字节导出核对；根目录测试中按旧字节钉的 Schema 会因新增属性失败，需按 ADR-0052 / 0055 先例增加 2.4.0
     钉值：`tests/test_adapter_contracts.py` 的 `PRE_B3_SCHEMA_SHA256`（`EventSpec`、`StrategySpec`、`UniverseMember`、
     `ResearchDatasetManifest`）与 `tests/test_adr_0077_evidence_manifest.py` 的 `V2_SCHEMA_SHA256_AT_2_2_0`
     （`UniverseMember`、`ResearchDatasetManifest`）；
   - v3 evidence 记录的投影去掉**所有**深度的 `schema_version` 并在 manifest 记录版本作用域内重建
     （`infrastructure/dataset/evidence.py`）：带 `assumption` 的成员若嵌套 2.0.0 的 `ASSUMPTION_BINDING`，重建后该绑定变为
     manifest 版本、与原对象不等——ADR-0051 第二期写入成员前必须处理（例如按 (id, version, hash) 解析回登记常量）；
   - `apps/api/report_dto.py` 与 `apps/web` 的 validation_report 支持版本需登记 2.4.0（2.3.0 先例 `33a9315`）；
   - `plugins/synthetic/random_walk.py` 按 `effect.strength` 访问效应，放宽联合后 mypy 会报错，新效应须 fail closed 或实现；
   - research / plugins / infrastructure 测试中按当前信封钉的回归哈希（注释 "Re-pinned for contract 2.2.0" 的 11 个文件）
     需在调试阶段重钉。

### PM 决定：契约层遗留项（2026-09-28，Claude PM 依 Raphael 授权）

- **`SyntheticMarket.truth` 放宽为 `SyntheticEffect` 联合**：与 `SyntheticMarketSpec.effects` 保持一致。这是 additive 变更，由合成市场生成器批次一并完成。
- **接受三处解读**：
  - `peak_equity` 需要同时有 `equity`；
  - `VOL_SCALED_TRIPLE_BARRIER` 下，固定屏障字段必须为空；
  - `UniverseMember.assumption` 沿用 PolicyBinding 的角色规则，必须为 `role=availability`。
- **调试阶段处理**：schema 钉值（`PRE_B3_SCHEMA_SHA256`、`V2_SCHEMA_SHA256_AT_2_2_0`）与历史回归哈希，须在真实导出、运行后重钉。
- **ADR-0051 第二期修复**：v3 evidence 重建会剥除嵌套的 `schema_version`，第二期实现时须修复这一点，使嵌套的 2.0.0 政策绑定能够逐位重建。

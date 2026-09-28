# Outcome Library

| 字段 | 值 |
|---|---|
| 类型 | `outcome` |
| 状态 | Phase 4 框架：3 个标签方法已实现（FRAMEWORK_IMPLEMENTED / NOT_VALIDATED） |
| 首次填充 | Phase 4（[ADR-0037](../adr/0037-outcome-engine-and-minimal-validation-pipeline.md)） |

事件 / 信号之后的标准化结果标签（OutcomeSpec + `OutcomeLabelSpec`）。契约 `core/contracts/outcome.py`，实现 `plugins/outcomes/`，
物化与存储 `research/outcomes/`。Outcome **只作标签，永不作为** Feature / State / Event / Strategy 的输入（C-L2，ADR-0012）。

## 条目字段（以 `core/contracts/outcome.py` 为准）

| 字段 | 说明 |
|---|---|
| `name@version` | `OutcomeSpec.ref`；冻结的 `OutcomeSpec` 只有 `horizon` 与自由文本 `label_definition` |
| `method` | `OutcomeLabelSpec.method`：`forward_return` \| `triple_barrier` \| `vol_scaled_triple_barrier`（ADR-0088 决策 5，契约 2.4.0；只追加的新增模型，以 `outcome` 引用 + `outcome_spec_hash` 绑定一份 `OutcomeSpec`） |
| `horizon` | 从 `OutcomeSpec` 复制，必须 > 0 |
| `barriers` | `triple_barrier` 必填：`upper_barrier > 0`，`0 < lower_barrier < 1`；`forward_return` 不得有；`vol_scaled_triple_barrier` 必须为空（屏障改由 `volatility_feature` / `barrier_multiplier` 决定） |
| `volatility_feature` / `barrier_multiplier` | 只在 `vol_scaled_triple_barrier` 下必填：前者是 `kind=feature` 的引用，后者 `> 0`；其他方法下必须为空 |
| `alignment` | 入场 = 事件时刻及之后第一根 bar 的开盘；窗口 `[entry, entry + horizon]` 必须由首尾相接的 bar 覆盖 |
| `available_time` | 标签**可被知道**的时刻 = 所用 bar 的最大 `available_time`；此前任何时刻不得使用该标签 |
| `provider` | 实现插件 |
| `source` | 出处 |

## 通用规则

- 条目登记 ≠ 条目有效。所有条目默认状态为 `IDEA` 或 `UNVERIFIED`，只有 Lifecycle 状态能说明其是否经过验证。
- 每个条目必须有出处（`source`）；来源为本系统实验时引用 ExperimentRun ID。
- 已发布版本不可修改；修改 = 新版本。
- 被拒绝 / 失败的条目**不删除**，其状态更新并在 [failure-registry.md](failure-registry.md) 中登记。
- 本 Markdown 是人类可读索引；Phase 0 之后，权威数据存放在 Control Plane Registry 中，本文件由其生成或与其同步。
- `horizon` 与屏障是**标签规格参数**，由规格哈希绑定，不是验证阈值；本库不登记任何取值。
- 标签方法本身不产生收益主张：把标签方案当作 Alpha 证据是已知失败模式（`hlens-knowledge:FAIL-LABELING-AS-ALPHA-001`）。

### 规格状态（本库专用；不是 Lifecycle 状态，也不是知识库证据等级）

| 值 | 含义 |
|---|---|
| `DOCUMENTED` | 只有文献 / 外部知识库描述，主项目没有代码 |
| `IMPLEMENTED` | 主项目有代码与定向测试（FRAMEWORK_IMPLEMENTED 或 CODE_COMPLETE / DEBUG_PENDING）；**不是**验收，也不是验证 |
| `NOT_VALIDATED` | 没有在正式 Research Dataset 上运行并经验证的结果；当前**所有**条目都是 |
| `INPUT_UNAVAILABLE` | 必需输入不在当前数据范围 |
| `UNSPECIFIED` | 算法或参数未被已接受 ADR / 现有契约定义 |

`hlens-knowledge:<ID>` 指独立仓库 `hlens-knowledge`（R4，2026-09-23）中的只读条目：不是 `KnowledgeItem`、不可被本项目检索、
没有自动接入或同步；其证据等级是该仓库对来源的分级，不是本项目的证据等级或验证结论。

## 现状矩阵

| 方法 | 文献 / 知识库 | 主项目实现 | 输入 | 测试证据 | 规格状态 |
|---|---|---|---|---|---|
| 前向收益 | 定义性计算 | `hlens_forward_return@1.0.0` | 1m / 派生 bar 开盘、收盘（可得） | 契约套件、精确值、缺口不填补 | `IMPLEMENTED · NOT_VALIDATED` |
| 三重屏障 | `RM-TRIPLE-BARRIER-001`（López de Prado 2018，AFML；知识库 `DOCUMENTED`） | `hlens_triple_barrier@1.0.0` | bar OHLC（可得） | 契约套件、同 bar 双触下屏障优先、跳空按开盘 | `IMPLEMENTED · NOT_VALIDATED` |
| 波动率缩放的屏障宽度 | `RM-TRIPLE-BARRIER-001`（原文以波动率估计 × 倍数设屏障） | `hlens_vol_scaled_triple_barrier@1.0.0`（ADR-0088 决策 5） | bar OHLC + 入场时可见的 `volatility_feature` 值（Provider 外部解析，见下） | 契约套件、精确值、同 bar 双触、跳空、波动率缺失 / ≤0、未来扰动不改过去 | `IMPLEMENTED · NOT_VALIDATED` |
| Meta-labeling | `RM-META-LABEL-001`（AFML；`DOCUMENTED`） | 无 | 依赖一个初级模型（不存在） | 无 | `DOCUMENTED · UNSPECIFIED` |
| 事件研究（异常收益 / CAR） | `RM-EVENT-STUDY-001`（MacKinlay 1997；`ACADEMIC`） | 无（`research/events/stats.py` 只有频率 / 共现 / 领先滞后统计） | 需要基准模型（未定义） | 无 | `DOCUMENTED · UNSPECIFIED` |

## 条目索引

| provider | 方法 | 定义（按实现） | 不可计算（`None`） | 状态 |
|---|---|---|---|---|
| `hlens_forward_return@1.0.0`（`plugins/outcomes/forward_return.py`） | `forward_return` | `value = last.close / first.open − 1`，窗口首根开盘到恰在 `entry + horizon` 结束的那根 bar 的收盘；与方向无关（验证层乘以信号方向） | 窗口不完整或有缺口；入场延迟 ≥ 一根 bar | `IMPLEMENTED · NOT_VALIDATED` |
| `hlens_triple_barrier@1.0.0`（`plugins/outcomes/triple_barrier.py`） | `triple_barrier` | 上屏障价 `entry·(1 + upper)`，下屏障价 `entry·(1 − lower)`，垂直屏障 `entry + horizon`；按顺序扫描 bar：`low ≤ 下屏障` → `−1`（先检查下屏障，同一根 bar 双触时保守判为下屏障），否则 `high ≥ 上屏障` → `+1`；跳空穿越按开盘价出场；完整窗口未触达 → `0`，在最后收盘出场。`value` 为实际收益，`barrier ∈ {−1, 0, 1}` | 触达前出现缺口；未触达且窗口不完整 | `IMPLEMENTED · NOT_VALIDATED` |
| `hlens_vol_scaled_triple_barrier@1.0.0`（`plugins/outcomes/vol_scaled_triple_barrier.py`） | `vol_scaled_triple_barrier` | 与 `triple_barrier` 同一套入场 / 垂直屏障 / 扫描 / 双触 / 跳空规则（复用 `triple_barrier.py` 的出场判定），唯一区别：上下屏障价 `entry·(1 ± barrier_multiplier × volatility)`，`volatility` 是入场时刻可见的 `volatility_feature` 值 | 波动率在构造 Provider 时缺失该事件（或显式为 `None`）→ 与价格缺口同一约定，显式 `None`，不视为错误；波动率 ≤ 0，或 `barrier_multiplier × volatility ≥ 1`（下屏障非正）→ `OutcomeInputError`（视为畸形输入，硬性 fail closed，与"缺失"区分对待，见 Provider 模块文档） | `IMPLEMENTED · NOT_VALIDATED` |

共同规则（`plugins/outcomes/_window.py`）：标签值以 50 位精度计算、half-even 量化到 18 位；缺口永不填补；触达只在该 bar 完成后可知，
因此出场时刻是该 bar 的结束时刻。物化 `research/outcomes/table.py`（`known_as_of(t)` 只返回 `available_time ≤ t` 的标签），
存储 `research/outcomes/store.py`（`research.outcome_table@1.0.0`，只写一次、内容寻址）。

### 与文献的区别

- AFML 的三重屏障常以波动率估计乘以倍数设定上 / 下屏障宽度；本项目原有 `triple_barrier` 的 `upper_barrier` / `lower_barrier` 是
  相对入场价的固定比例，由标签规格声明。ADR-0088 决策 5（契约 2.4.0）新增 `vol_scaled_triple_barrier` 方法，屏障改为
  `entry·(1 ± barrier_multiplier × volatility)`；`volatility_feature: Ref[FEATURE]` 只在契约层声明"用哪个 Feature"，
  不是数值通道——`OutcomeRequest` 仍是冻结契约，未新增字段携带该 Feature 的实际数值（ADR-0088 只授权契约层，Provider 与其余
  批次留给后续实现）。`hlens_vol_scaled_triple_barrier` 因此把已解析（PIT 选中、对应到某个事件入场时刻）的波动率值作为构造参数
  接收，与 `label_specs` 同一生命周期（每个 `OutcomeRequest` 对应一个新 Provider 实例，同 `ForwardReturnOutcome` 在
  `research/loop/operator_providers.py` 的既有用法）；真正从 `FeatureResult` 取值、按 `entry_time` 做 PIT 选择并组装这份映射，
  是后续 plugins / research 批次的接线工作。
- 同一根 bar 双触时顺序未知：本项目保守判为下屏障；在 1m bar 粒度上无法确定 bar 内顺序，除非引入更细的价格路径（未规格化）。
  `vol_scaled_triple_barrier` 复用同一判定。

## 测试证据

`tests/plugins/outcomes/test_outcome_providers.py`（契约套件 × 2、精确值、同 bar 双触、缺口不填补、单点故障变体被杀死）；
`tests/plugins/outcomes/test_vol_scaled_triple_barrier.py`（契约套件、精确值、与等效固定比例 `triple_barrier` 结果逐条比对、
同 bar 双触、跳空、波动率缺失 / 显式 `None`、波动率 ≤ 0 与屏障塌缩两种 fail closed、后续事件的波动率扰动不改变更早事件的标签）；
`tests/contract_suites/outcome.py`（标签可知时刻之后的价格被忽略、事件之前的价格被忽略、缺数据 → `None`、事件顺序无关、只作标签）；
`tests/test_outcome_contracts.py`（C-L2 拒绝、标签规格形状）；`tests/test_adr_0088_contract_240.py`（决策 5 的契约层形状、版本边界、
Schema 只增不改）；`tests/research/outcomes/test_store.py`。
`research/outcomes/sources.py` 只提供合成 bar；数据集路径的 bar 在 `infrastructure/bars/dataset.py`。没有标签在正式 Research Dataset 上物化过。
`vol_scaled_triple_barrier` 的波动率数值通道（从 `FeatureResult` 到 Provider 构造参数）尚无真实接线，见下方 O-2 之后的说明。

## 已知失败模式（roadmap Phase 4）

- 重叠 horizon 造成虚假显著 → 由验证层的 purging / embargo 处理；D-30 已确认 G0 绑定本次实验的单一 Outcome ref / hash，G1 比较 Profile embargo 与该 Outcome 的精确 horizon。Phase 4 仍待验收；未来若一次实验支持多个 Outcome，须先决定 horizon 选择规则并更新 D-30。
- 标签定义隐含未来信息 → 契约强制 `available_time` 对齐与窗口核对（`OutcomeResult.check_answers`）。
- 把标签数值抄进某个 Feature 的输入：契约层无法阻止，属信息流审计与 G1 泄漏门。

## 缺口（待规格化 / 待批准）

| ID | 缺口 | 说明 |
|---|---|---|
| O-1 | `refuse_outcome_input` 没有运行时调用方 | `core/contracts/outcome.py` 的说明称验证流水线在运行时使用它，但当前只在测试中调用；输入 DTO 的 `extra="forbid"` 仍会拒绝 Outcome 载荷 |
| O-2 | meta-labeling、事件研究 CAR | 均为 `DOCUMENTED · UNSPECIFIED`；须作为新标签方法提出规格与批准。波动率缩放屏障已由 ADR-0088 决策 5 规格化并实现（见上），不再属于本条 |
| O-3 | `vol_scaled_triple_barrier` 缺少波动率数值通道的接线 | `hlens_vol_scaled_triple_barrier` Provider 把入场时可见的波动率值作为构造参数接收（`plugins/outcomes/vol_scaled_triple_barrier.py` 模块文档），但从某个 `volatility_feature` 引用对应的 `FeatureResult` 按 `entry_time` 做 PIT 选择、组装成这份映射并传给 Provider，尚无 `research/` 或 `infrastructure/` 侧的实现（ADR-0088 明确把这部分留给后续批次） |

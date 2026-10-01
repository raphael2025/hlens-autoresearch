# ADR-0035: StateProvider 的 Protocol、DTO、执行器与首批状态（Phase 2 框架）

| 字段 | 值 |
|---|---|
| 状态 | **Accepted**（2026-09-25），决策者: Claude Code（Opus），依 Raphael 2026-09-25 明确授权（红线除外） |
| 日期 | 2026-09-25 |
| 决策者 | Claude Code（Opus），依 Raphael 2026-09-25 明确授权（"全部 Phase 先按架构框架实现"；红线除外） |
| 起草者 | Claude Code（Opus），Phase 2 框架批次 |
| 相关 Phase | Phase 2 — Market State Engine（roadmap） |
| 实现状态 | **FRAMEWORK_IMPLEMENTED / NOT_VALIDATED**：框架与冒烟测试已交付；未在真实 Research Dataset 上运行，未经逐项调试与复核 |
| 影响范围 | Contract（`core/contracts/state.py` 新增，additive）；`infrastructure/state/`；`plugins/states/`；`research/states/` |
| 是否破坏兼容 | 否：只新增 5 个模型与 1 个 Protocol；`StateSpec` 与既有 Schema 逐字节不变 |
| 前置 | [ADR-0017](0017-provider-delivery-schedule.md)、[ADR-0012](0012-information-flow-and-kind-invariants.md)、[ADR-0030](0030-feature-provider-contract.md) |

## 背景

roadmap Phase 2 要求：StateSpec + StateProvider、State 表、状态稳定性诊断（持续时间、转移矩阵）；验收为"状态只依赖
过去信息（测试）、分布与转移统计可报告、训练型状态模型有固定窗口与种子"；禁止"全样本拟合后在同样本上研究"与"用
Outcome 定义状态"。ADR-0017 规定 Provider 的 Protocol、DTO 与 provider-agnostic 契约测试在首次消费前交付。
本 ADR 以 ADR-0030（FeatureProvider）为模板定下 StateProvider 的字段与语义。

## 裁决

### 1. 泄漏与全样本拟合由执行器结构性排除

- 输入是 Feature 值：`StateInput(feature, evaluation_time, value, source_result_hash)`。Feature 值在其评估时刻已由
  Feature 执行器保证只用 `available_time + available_lag <= evaluation_time` 的观察，因此状态 `t` 的可见集合是
  `evaluation_time <= t` 的 Feature 值。
- `StateSpec.training_window` 非空（训练型）时，可见集合再限于 `(t - training_window, t]`：Provider **看不到**
  窗口之外的历史，无法做全样本或扩张窗口拟合。
- `infrastructure/state/runner.py` 的 `run_state` 对每个评估时刻单独构造只含可见集合的子请求（与 `run_feature`
  同一模式），逐个用 `StateResult.check_answers` 核对回答；训练型规格没有固定 `seed` 即拒绝；输入 feature 不在
  `StateSpec.features` 内即拒绝。
- `state_inputs` 只从 `(FeatureRequest, FeatureResult)` 对构造输入，且结果必须回答该请求（`request_hash`、一一对应），
  `source_result_hash` 绑定该结果。

### 2. DTO（`core/contracts/state.py`，新增）

| 模型 | 字段 | 不变量 |
|---|---|---|
| `StateInput` | `feature: Ref`、`evaluation_time`、`value: Decimal \| int \| bool \| None`、`source_result_hash` | `feature.kind == feature`（Outcome 构造即拒绝，C-L2）；拒绝浮点 / 非有限数 |
| `StateRequest` | `state: Ref(kind=state)`、`spec_hash`、`evaluation_times`、`inputs` | 评估时刻非空严格升序；输入按 `(evaluation_time, feature)` 规范排序、不重复；`visible_at(t, training_window)` |
| `StateValue` | `evaluation_time`、`state: str \| None`、`inputs_used`、`latest_input_time?` | `None` = 显式不可计算（不填补）；`inputs_used == 0` ⇔ 无 `latest_input_time` ⇒ `state is None` |
| `StateResult` | `request_hash`、`provider`、`provider_hash`、`values`、`result_hash` | `result_hash` 构造复核；`check_answers(request, descriptor, spec)` |
| `StateProviderDescriptor` | `name`、`version`、`deterministic: Literal[True]`、`supported_states` | `state:name@semver → spec hash`；训练型的随机性只能来自规格 `seed` |

Protocol：`descriptor` → `StateProviderDescriptor`；`compute(StateRequest) → StateResult`。

### 3. 模型参数编码在 `StateSpec.method`

已发布的 `StateSpec` 没有 `params` 字段，且契约只能追加（H1）。状态模型参数（分位切点、最少历史、阈值、窗口长度）
以规范形式 `<method_name>:<canonical JSON>` 写入 `method`（`state_method` / `parse_state_method`；值只接受
`str` / `int` / `bool`，十进制数以字符串传入，非规范编码拒绝），因此受 spec hash 绑定。参数是模型参数，不是验证阈值；
首批 Provider 的参数全部没有默认值，由每个规格声明。将来若为 `StateSpec` 增加 `params`，需另立 ADR（minor 追加）。

### 4. 首批 Provider、State 表与诊断

- `plugins/states/`：`VolatilityRegimeProvider`（已实现波动率特征）与 `LiquidityRegimeProvider`（成交量特征）——
  固定尾随窗口内的经验分位分桶（次序统计量 `h_k`，`k = max(1, ceil(q·n))`，无插值）；`TrendRangeProvider`——最近
  `window` 个对数收益的效率比 `|Σr| / Σ|r|` 与阈值比较（规则型，无训练窗口与种子）。全部精确 `Decimal`、确定性。
- 资金费率体制：输入数据未采集（ADR-0022 范围），登记为 declared-unavailable（`DECLARED_UNAVAILABLE`、
  state-library），不提供 Provider。
- `infrastructure/state/table.py`：`state_table` 把一份 `StateResult` 物化为 Arrow 表（带 `state_ref`、`spec_hash`、
  `provider`、`request_hash`、`result_hash`）。写入 catalog 的 `state.*` 表不在本批次。
- `research/states/diagnostics.py`（研究代码，H5）：分布、run 持续时间、转移矩阵（跳过含 `None` 的相邻对）、
  标签闪烁（短于调用方给定 `min_run` 的 run 占比、切换率）与 Markdown 报告；只描述，不判定。
- provider-agnostic 契约测试 `tests/contract_suites/state.py`：descriptor、训练型固定种子、一一对应、标签属于
  `state_space`、确定性、因果扰动、训练窗口、Outcome 构造拒绝、显式 `None`、哈希敏感、未声明规格。

## 备选方案

| 方案 | 内容 | 缺点 | 结论 |
|---|---|---|---|
| **A（采纳）** | 执行器截断（含训练窗口）+ 契约扰动测试；参数编码在 `method` | `method` 需规范编码 | 采纳 |
| B | 给 `StateSpec` 增加 `params` | 改已发布契约（需 minor + 迁移），本批次不做 | 延后 |
| C | Provider 自行按窗口过滤 | 信任插件；一个错误插件即可全样本拟合 | 拒绝 |
| D | 状态输入直接用 Canonical 观察 | 绕过 Feature 层的 lag 与 lineage | 拒绝 |

## 契约、Schema 与迁移影响

新增 5 个模型与 1 个 Protocol，导出 JSON Schema（current 79 → 84），不升 `CONTRACT_SCHEMA_VERSION`；
`StateSpec` 与既有 Schema 逐字节不变；无数据迁移。

## 后果

- 正面：状态只依赖过去且训练窗口固定由结构保证；分布 / 持续时间 / 转移 / 闪烁可报告；以 `result_hash` 可复现。
- 负面 / 已知缺口（留给调试批次）：执行器每个评估时刻一次调用，成本为时刻数 × 可见前缀（规则型无窗口时为二次方）；
  State 表未写入 catalog；未在真实 Research Dataset 上运行；分位 / 趋势参数未经研究校准；`source_result_hash` 只在
  `state_inputs` 中核对，Registry 侧未核对。

## 合规检查

- [x] 不破坏已冻结契约（additive）
- [x] 不修改 Validation Constitution 或 Profile；无数值验证阈值
- [x] Domain 层仍无具体技术依赖
- [x] Outcome 不进入 State 输入（`StateInput.feature` 只能 `kind=feature`）

## Implementation note（wiring，2026-09-25）

Phase 2 → Phase 3 的接缝已接上（不改本 ADR 的裁决，见 ADR-0036 的同一条记录）：
`infrastructure/event/inputs.py` 新增 `state_value_lineage` / `state_series_from_state_run`，把一次
`(StateRequest, StateResult)` 转成事件引擎的 `StateSeriesPoint`。因果性直接来自本 ADR §1 的结构性截断：
`run_state` 保证一个评估时刻的 `StateValue` 只是 `visible_at(t, training_window)` 的函数，因此
`state_value_lineage` 只需绑定 state 身份（ref、spec hash、provider、provider hash）与该 `StateValue`
本身——不得绑定 `request_hash` / `result_hash`（覆盖整段运行的全部评估时刻，含未来）。`StateValue.state`
为 `None`（不可计算）原样传为 `label=None`，不填补。`tests/smoke/test_phase3_events_smoke.py` 的状态序列
已从"符号 stand-in"换成真实的 `plugins.states.TrendRangeProvider`（经 `infrastructure.state.run_state`）；
新增 `tests/infrastructure/event/test_state_inputs.py`：重跑同请求结果一致、未来扰动不改变过去的点与下游
事件 id、显式 `None` 不被填补。

## Implementation note（增量评估提案的决定，2026-09-27）

- Codex 依 Raphael 授权**不批准**按现有描述实现"可选增量评估路径"（`run_state` 的逐时刻调用成本为时刻数 × 可见前缀，规则型无窗口时为二次方，本 ADR 后果中列为调试批次缺口）。理由：因果保证目前由 `run_state` 对每个评估时刻只传入该时刻的可见前缀**结构性**提供；等价哈希与未来扰动测试只能在已测样本上证明结果相同，不足以证明新增路径不会削弱这一结构边界。保留 per-time 路径为唯一路径。只有在具备**真实性能基线**（真实 Research Dataset 规模上的实测耗时 / 内存）并提出**不向 provider 暴露未来数据的逐步协议**之后才重新评估。这是明确**暂缓优化**，**不是**宣称没有性能问题：二次方成本仍是已知限制。
- 本决定不改本 ADR 的裁决、契约或任何代码；`infrastructure/state` 的逐时刻执行器不变。

## Implementation note（运行 / 读回 / 诊断入口，2026-10-01）

State 的物理表、`StateTable` 与 `StateResultStore` 由 ADR-0089 实现；显式的运行、读回与诊断命令行入口
（`python -m research.states.run_cli compute`、`python -m infrastructure.state.run_cli` 的 `show` / `list`、`python -m research.states.report_cli`）由 ADR-0102
实现。本 ADR 的裁决、契约与 `run_state` 的逐时刻结构性截断均不变；训练型规格无 seed 仍在入口处被拒绝。

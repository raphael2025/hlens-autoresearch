# ADR-0036: EventProvider 契约、事件执行器与首批事件 / 交互算子（Phase 3）

| 字段 | 值 |
|---|---|
| 状态 | **Accepted**（2026-09-25） |
| 日期 | 2026-09-25 |
| 决策者 | Claude Code（Opus），依 Raphael 2026-09-25 明确授权（红线除外） |
| 相关 Phase | Phase 3 — Event & Interaction Engine（依 Raphael 2026-09-25 全阶段框架实现指示） |
| 影响范围 | Contract（`core/contracts/event.py`，additive）、`infrastructure/event/`、`plugins/events/`、`research/events/` |
| 是否破坏兼容 | 否：只新增 5 个模型与 1 个 Protocol；`EventSpec` 与既有 Schema 逐字节不变；Schema 87 → 92 |
| 实施状态 | FRAMEWORK_IMPLEMENTED / NOT_VALIDATED |
| 前置 | [ADR-0017](0017-provider-delivery-schedule.md)、[ADR-0012](0012-information-flow-and-kind-invariants.md)、[ADR-0030](0030-feature-provider-contract.md) |

## 背景

roadmap Phase 3 要求识别离散事件及其交互与时序关系，输出 EventSpec + EventProvider、Event 表与共现 / 时序统计；
验收：事件时间 = 可观测时间、事件定义版本化、交互算子的输出可追溯到上游事件；禁止在事件定义中使用未来确认。
ADR-0017 要求 Provider 的可执行 Protocol、DTO 与 provider-agnostic 契约测试在首次消费前交付。Phase 2 的
StateProvider 由另一批次并行实现，本批次不能依赖它。

## 裁决

### 1. 输入：上游序列点（`EventInputPoint`）

一个点 = 一条 Feature / State 序列在 `evaluation_time` 的值（Feature 数值或 State 标签，`None` = 不可计算），带
`available_time`（可被观测的最早时刻）与 `source_lineage_hash`（**因果**溯源：只依赖 `available_time` 时已知的信息；
不得是整段上游运行的 `result_hash`，否则过去事件的身份会随未来数据改变——冒烟测试发现并据此确定）。
`EventRequest` 要求 `(source, evaluation_time)` 唯一、每条序列只追加（按 `evaluation_time` 排序后 `available_time`
不递减），因此"截至 t 可见"的点总是序列前缀。交互算子另以上游 `Event` 为输入（`upstream_events`）。

State 序列在 Phase 3 使用**本地最小形状**（`infrastructure/event/inputs.py` 的 `StateSeriesPoint`：时间、可用时间、
标签、逐点 lineage）；Phase 2 合并时在同一文件加一个从 StateProvider 结果到该形状的适配器，事件引擎其余部分不变。

### 2. 事件时间 = 可观测时间

`Event.event_time` 恰好等于所引用输入（点的 `available_time`、上游事件的 `event_time`）的最大值加
`EventSpec.observable_lag`；事件只能引用在 `event_time` 已可见的输入。`EventResult.check_answers` 对每个事件核对这两条，
并核对它属于请求的事件定义（`event` + `spec_hash`）、引用的输入都在请求中。

### 3. 截至 `as_of` 的事件表与结构性截断（执行器）

`detect(EventRequest) → EventResult` 返回 `event_time <= as_of` 的全部事件，只能是 `as_of` 可见集合的函数。
`infrastructure/event/runner.py` 的 `run_events` 对每个检查点 `t` 只把 `t` 的可见集合交给 Provider（与 ADR-0030
方案 A 同构），并要求截至 `t` 的表恰好是截至更晚检查点的表在 `event_time <= t` 上的限制：事件被回填到更早时间
（未来确认）或被撤回都 fail closed（`FutureConfirmationError`）。默认检查点 = 可见集合每次变化的时刻 + `as_of`，
因此每个事件都在它自己的 `event_time` 用当时可见的数据被复核；调用方可传更粗的网格（更便宜、更弱，已在代码中说明）。

### 4. 版本化与参数绑定

`EventSpec` 没有 `params` 字段（冻结契约，H1 不改），首批 Provider 把全部参数写入 `trigger`：
`{"operator": ..., <params>}` 的规范 JSON。它属于规格内容，因而由 descriptor 中的 spec hash 绑定；Provider 只服务它能从
自身 trigger 重建出同一哈希的规格。`Event` 带 `event` 引用与 `spec_hash`，`event_id` 为内容哈希（构造时复核）。
Event 表的逻辑物化见 `infrastructure/event/table.py`（`event_table`）；登记物理 `event.*` Iceberg 表另行决定。

### 5. 首批 Provider 与交互算子（`plugins/events/`）

| Provider | 定义 |
|---|---|
| `FeatureThresholdCrossProvider` | 相邻两点跨越固定水平（`up` / `down` / `both`） |
| `VolatilityBreakoutProvider` | 进入 `value > multiplier * 前 window 个值的均值` 区域（精确 `Decimal` 比较，不精确即拒绝） |
| `StateSwitchProvider` | 相邻两个可计算点的标签不同（可限定 from / to） |
| `EventSequenceProvider` | A 之后 `window` 内的 B：每个 B 链接其之前最近的 A |
| `EventCoOccurrenceProvider` | A、B 相距不超过 `window`（任一顺序） |

交互算子的输出 `upstream_event_ids` 引用它连接的上游事件，逐跳可追溯到 Feature / State 输入。由于 `EventSpec`
只能依赖 Feature / State（ADR-0012），交互规格声明上游规格的 Feature / State 并集为（传递）输入，把上游事件引用写入
`lineage`，并在 trigger 中绑定上游引用、上游 spec hash 与窗口；其它定义 / 哈希的上游事件一律拒绝。

水平、窗口、倍数都是**事件定义参数**，不是验证阈值；本 ADR 不引入任何验证阈值或 Profile 数值。

### 6. 契约测试与研究统计

`tests/contract_suites/event.py`：descriptor、夹具自检、确定性、只依赖可见集合（含 lag）、因果扰动、执行器截断下的
PIT 一致性（不得未来确认）、哈希敏感、未声明规格、非有限数。刻意错误的替身（未来确认的"顶部"、提前一分钟的事件时间）
被对应检查杀死。`research/events/stats.py`：频率、共现（含独立泊松近似下的期望与 lift）、lead-lag 直方图、重叠 /
独立性诊断（相邻重叠比例、贪心不重叠计数、到达间隔离散度）；只描述，不判定。

## 后果

- 正面：事件引擎与 Feature 引擎同构（截断 + 回答核对），并多一道结构性 PIT 一致性检查，直接落实"禁止未来确认"。
- 负面 / 延期：
  - 一个请求对应一个标的（上游运行按标的）；多标的事件表需要 subject 键，另行决定。→ 已由 [ADR-0057](0057-event-request-subject.md)（2026-09-26）加入可选 `subject`。
  - 默认检查点使执行器成本为 O(检查点 × 可见集合)；更大规模需要增量接口（届时另立版本）。
  - 交互规格的 Feature / State 并集只核对形式（排序、唯一），不回溯核对上游规格——属 Registry。
  - 物理 Event 表、Registry 登记、统计在真实数据上的校准均未做（NOT_VALIDATED）；与 Phase 2 StateProvider
    的适配器见下方 Implementation note（已接上）。

## Implementation note（wiring，2026-09-25）

`infrastructure/event/inputs.py` 的 WIRING POINT 已接上：新增 `state_value_lineage` /
`state_series_from_state_run`，把 Phase 2 的 `(StateRequest, StateResult)` 转成本 ADR §1 的
`StateSeriesPoint` / `EventInputPoint`（`source_lineage_hash` 只依赖 state 身份与该点的 `StateValue`
本身，从不依赖覆盖全部评估时刻的 `request_hash` / `result_hash`，因此过去的点与引用它们的事件的身份不随
未来数据改变，符合 `EventInputPoint` 文档字符串的要求）；事件引擎其余部分不变。`tests/smoke/
test_phase3_events_smoke.py` 的状态序列已换成真实的 `plugins.states.TrendRangeProvider`（经
`infrastructure.state.run_state`），新增 `tests/infrastructure/event/test_state_inputs.py` 覆盖重跑一致、
未来扰动不改变过去事件 id、显式 `None` 不填补。详见 ADR-0035 的同一条记录。

## Implementation note (interaction upstream verification, 2026-09-26)

不新增 ADR，不改 `core/contracts`、`core/domain`（`EventSpec` / `EventRequest` 等冻结）或 Provider 接口。处理 debug
backlog C 节 P3"交互规格声明的上游只做形式检查"与本 ADR 后果中的诚实边界（02-domain §2.8 同一边界）中**一次运行被
交给了什么**的部分；Registry 登记本身仍属 Registry。

新增 `infrastructure/event/upstream.py`，由 `run_events` 在第一个检查点之前调用；错误类移入
`infrastructure/event/errors.py`（`runner` 照旧导出）。任一不符抛 `UpstreamVerificationError`（`EventRunnerError`
子类），fail closed，从不静默丢弃：

1. **交互核对**（`verify_interaction`）。交互 = `lineage` 含 `kind=event` 引用，或请求带上游事件。
   - `run_events(..., upstream_specs=...)` 对交互**必填**；所给上游规格恰好等于声明的上游引用（缺、多、重复都拒绝）；
     非交互规格收到上游事件或上游规格同样拒绝。
   - 上游 spec hash：§5 规定交互的 trigger 绑定"上游引用、上游 spec hash 与窗口"，因此所给上游规格的
     `content_hash()` 必须出现在交互 trigger 文本中（256 位内容哈希；同一 ref 下内容不同的规格哈希不同而被拒绝）。
     不解析任何 Provider 的 trigger 约定。
   - 请求中的每个上游事件必须属于某个声明的上游规格且 `spec_hash` 等于该规格的哈希。
   - Feature / State 输入：交互声明的并集必须**等于**上游规格声明的并集，**不是**超集。依据：本 ADR §5
     （"交互规格声明上游规格的 Feature / State 并集为（传递）输入"）、02-domain §2.8 诚实边界（"是否与上游规格一致"）、
     `plugins/events/interactions.py` 的构造（恰为并集）；且交互只消费上游事件，并集之外的 Feature / State 是任何计算都
     用不到的声明输入，即虚假的溯源声明。子集同样拒绝（隐藏了传递输入）。
   - 可选 `upstream_results`：请求中的每个上游事件必须逐字出现在所给上游结果中，所给结果只含声明上游规格的事件。
2. **逐点 lineage 核对**（`verify_input_lineage`）。给了 `feature_runs` / `state_runs`（`(FeatureRequest, FeatureResult)`
   / `(StateRequest, StateResult)`）时，请求的每个输入点必须恰好等于适配器（`inputs_from_feature_run` /
   `state_series_from_state_run`）从对应运行重算出的点：值、时间与逐点 `source_lineage_hash` 都相同。来源没有运行、
   运行中没有该点、伪造的 lineage 或值、结果不回答其请求、同一来源两次运行，都拒绝。未给运行时不核对（本地
   `StateSeriesPoint` 形状的 lineage 由调用方计算，仍属调用方 / Registry）。

调用方更新：`tests/infrastructure/event/test_event_runner.py` 的交互运行与 `tests/smoke/test_phase3_events_smoke.py`
现在传入上游规格 / 结果与特征 / 状态运行（冒烟因此逐点核对全部输入 lineage）。回归测试
`tests/infrastructure/event/test_upstream_verification.py`：匹配的交互通过；缺少 / 多余 / 未声明的上游、未绑定的上游
spec hash、上游事件哈希不符、并集不符（超集 / 子集 / 不同）、伪造的 lineage 与值、无运行的点都被拒绝；
`ConfirmedTopProvider`、`BackdatedProvider` 与一个回填时间的交互算子仍被执行器抓住。仍然 FRAMEWORK_IMPLEMENTED /
NOT_VALIDATED；未做：Registry 侧的登记核对、多跳（传递）上游的一次性核对（每跳在自己的运行中核对）、非交互规格的
输入点来源是否属于声明的 Feature / State。

## Implementation note (durable review fixes, 2026-09-26)

不新增 ADR，不改 `core/contracts`、`core/domain` 或 Provider 接口；`run_events` 的默认行为（不给 `require_full`）只在上游哈希绑定一项变严。
处理只读复核的一项中等发现（上一条实施说明第 1 点"不解析任何 Provider 的 trigger 约定"被本条取代）。

1. **上游 spec hash 精确绑定**。原实现只检查所给上游规格的哈希是否作为**子串**出现在交互 trigger 文本中，重叠的十六进制串（例如某个更长的值里
   恰好包含该哈希）会误判为已绑定。现在按本 ADR §4 把 trigger 解析为规范 JSON 对象（非 JSON 对象、重复键 → 拒绝），上游只由一对顶层字段绑定：
   `<name>` = 上游引用（`str(ref)`，逐字相等）且 `<name>_hash` = 其 spec hash（逐字相等）——即首批交互算子的形式（`first` / `first_hash`、
   `then` / `then_hash`、`left` / `left_hash` 等）。所给规格的 `content_hash()` 必须**恰好等于** trigger 为该引用绑定的唯一哈希；只出现在其它值里、
   出现在不与引用配对的字段里、或同一引用绑定了两个哈希，都拒绝（`UpstreamVerificationError`）。
2. **写明哪些核对没有做**。`verify_interaction` / `verify_input_lineage` 返回 `UpstreamVerification`（`performed` / `not_performed` /
   `not_applicable`，核对名 `upstream_specs`、`upstream_results`、`input_lineage`）：非交互规格的两项上游核对为不适用；交互未给 `upstream_results`
   时该项为未做；未给 `feature_runs` / `state_runs` 而请求有输入点时 `input_lineage` 为未做（无输入点为不适用）。新增 `verify_upstream`（合并两者）
   与 `run_events(..., require_full=True)`：任一适用核对因证据缺失而未做即拒绝，调用方由此可以断言"完整核对"而不是静默得到较弱的子集。
   默认 `require_full=False`，与原行为相同。
3. **调用方**：`tests/smoke/test_phase3_events_smoke.py` 的每次运行改用 `require_full=True`，并断言报告完整。
4. **测试**（`tests/infrastructure/event/test_upstream_verification.py`）：重叠哈希（`then_hash` 绑定孪生规格、SWITCH 的哈希只在另一个值中，旧的
   子串检查会接受）被拒绝；哈希在不配对字段中、同一引用绑定两个哈希、非 JSON 对象 / JSON 数组 / 重复键的 trigger 被拒绝；报告在交互 / 非交互 /
   有无运行 / 无输入点各路径上写明已做与未做的核对；`require_full` 在缺少特征 / 状态运行或缺少上游结果时拒绝，证据齐全时与默认运行结果相同。

# ADR-0057: 事件请求的标的键（EventRequest.subject，P3-MULTISYM）

| 字段 | 值 |
|---|---|
| 状态 | Accepted (2026-09-26)；**re-declared at 2.1.0 (2026-09-26)**（ADR-0052 版本化重放合入全代码分支后，见文末 Implementation note — 2.1.0）；CODE_COMPLETE / DEBUG_PENDING |
| 日期 | 2026-09-26 |
| 决策者 | Claude Code（Opus），依 Raphael 2026-09-26 明确授权（"所有的决策都由你来决定，包括红线"）；协调者裁定为 additive 可选字段 |
| 起草者 | Claude Code（Opus） |
| 相关 Phase | Phase 3 事件引擎 |
| 影响范围 | Contract（`core/contracts/event.py`：`EventRequest`、`Event`、`EventResult`，全部 additive）/ `infrastructure/event/`（runner 经 `truncated` 透传、`upstream` 核对、`table` 增列）/ `plugins/events`（经 `EventResult.build` 自动绑定，无代码改动） |
| 是否破坏兼容 | 否：`subject` 缺省时从载荷与哈希输入中省略，既有请求哈希、`event_id`、`result_hash` 逐位不变（金值测试钉住） |
| 前置 | [ADR-0036](0036-event-provider-contract.md)（"一个请求对应一个标的；多标的事件表需要 subject 键，另行决定"）、[ADR-0008](0008-contract-payload-immutability.md)、[ADR-0052](0052-validation-contract-completion.md) §4（信封版本） |

## 背景

`EventRequest` 没有标的字段：多标的只能逐序列请求，而合并后的事件表没有键来区分同一事件定义在不同标的上的事件；
同一份输入数据在两个标的下产生的事件 `event_id` 甚至会相同。调试待办 P3-MULTISYM 列出两个选项：A. additive 可选字段 + ADR
（须证明省略时哈希不变）；B. 维持现状由调用方组合。

## 决定（方案 A）

1. `EventRequest.subject: str | None`（非空；纯空白拒绝）：请求所属的标的 / 序列名。**一个请求只对应一个标的**（ADR-0036 的原则不变）；
   多标的事件表 = 每个标的一个请求，其结果按 `subject` 合并。
2. `Event.subject`、`EventResult.subject`（同型可选）。请求带 `subject` 时：结果必须回显它、每个事件都绑定它
   （`EventResult.build` 把未绑定的事件经 `Event.bound_to` 绑定并重算 `event_id`；已绑定到别的标的即拒绝）；
   `check_answers` 核对 `subject`；请求的上游事件必须与请求同一标的（都缺失亦可）；runner 的 `upstream_results` 必须回答同一标的。
3. 身份：`subject` 进入请求哈希、`event_id`（`_EventProbe` 同形字段）与 `result_hash`（给出时加入哈希输入）。
   缺省（`None`）时字段以 `exclude_if` 省略出载荷、`result_hash` 输入不加该键——既有哈希逐位不变。
4. 逻辑事件表（`infrastructure/event/table.py`）追加 `subject` 列（第 10 列，前 9 列不变）。
5. Contract suite 增加 `check_subject_is_bound`：带标的的请求，结果与事件都绑定它；识别出的事件与不带标的时相同（只改变身份）；
   两个标的 → 两个 `result_hash`；不带标的时结果与事件都不带标的。

## 信封版本与哈希（与 ADR-0052 的关系）

- 本 ADR 的字段在当前信封 `CONTRACT_SCHEMA_VERSION = 2.0.0` 下以可选字段发布（与 ADR-0054 相同的做法），
  因为协调者把本项定为 additive，而 2.1.0 升版目前阻塞于 Canonical 重放（ADR-0052 "Implementation blocker (2026-09-26)"）。
  已知代价（同 ADR-0054）：只认识旧字段集的 2.0.0 读者会按 `extra="forbid"` 拒绝带 `subject` 的载荷（fail closed）。
- 若将来随 ADR-0052 升到 2.1.0：新构造的请求 / 事件 / 结果的信封变为 2.1.0，其请求哈希与 `event_id` 会随之改变（信封进入哈希载荷），
  与是否带 `subject` 无关；已记录的 2.0.0 事件按其记录的信封读取，`event_id` 由其自身载荷复核，仍然自洽（ADR-0052 阻塞说明中
  "读取不改写版本"的同一原则）。Codex review K3 "新字段不得以 2.0.0 发布"若也适用于本项，本项需随 ADR-0052 的解除一并迁到 2.1.0。

## 备选方案

| 方案 | 结论 |
|---|---|
| B. 不改契约，调用方逐序列请求并在外部加键 | 事件身份不含标的，合并表中同输入的事件 id 冲突；拒绝 |
| 在 `EventInputPoint` 上加标的 | 输入点属于上游序列，标的是请求级属性；一个请求混多个标的违背 ADR-0036 原则；拒绝 |

## 测试

`tests/test_event_subject.py`（缺省时请求哈希 / `event_id` / `result_hash` / 结果内容哈希等于实施前的金值；绑定端到端；两个标的
一张表无 id 冲突；runner 各检查点保留标的；交互按标的运行；空白标的、异标的上游事件 / 上游结果、事件改绑、结果改标的均拒绝）、
contract suite 的 `check_subject_is_bound`（五个事件提供者全部通过）、`tests/infrastructure/event/test_event_runner.py`（表列）。

## Implementation note — subject 身份语义与版本（2026-09-26，依 Codex 全代码复核 `648fe6c`）

- `subject` 是**调用方提供的稳定 opaque identifier，大小写敏感**：契约只沿用既有字符串规则（去除首尾空白），core 不做大小写转换、别名映射或交易所推断。
  任何对应真实 Instrument 的 provider / 调用方必须从稳定的 Instrument 身份生成该键（例如 `instrument_key` 的 `venue:type:symbol`），不得用可变显示名；
  否则同一标的在不同运行中得到不同 `request_hash` / `event_id`。
- 版本：当前实现在契约 2.0.0 下加入可选字段（缺省不进载荷与哈希）。按 K3，这不能证明旧版 2.0.0 读者能理解带 `subject` 的载荷，故不接受为契约完成；
  在 ADR-0052 版本化重放（独立 Phase 1 分支 `phase1/adr-0052-versioned-replay`）落地后，以 2.1.0 重新声明带 `subject` 的新对象、旧对象按已持久化版本读取，
  并补跨版本往返、旧读者行为与哈希测试。在此之前 `wip/all-code-completion` 的 `564c87c` 只是保留的恢复点。


## Implementation note — re-declared at 2.1.0 (2026-09-26)

分支 `core/adr-0052-into-full-code`（合入 `phase1/adr-0052-versioned-replay` 的 ADR-0052 M1 ~ M3 之后）。取代上文"信封版本与哈希"一节中
"在 2.0.0 下发布"的做法（该节保留为历史）：

- `Event`、`_EventProbe`、`EventRequest`、`EventResult` 声明 `_FIELDS_SINCE = {"subject": "2.1.0"}`（`ADR_0057_VERSION`）：
  2.0.0 信封携带 `subject` 即拒绝；带 `subject` 的新对象信封为 2.1.0（当前 `CONTRACT_SCHEMA_VERSION`）。
- 不带 `subject` 的 2.0.0 请求 / 事件 / 结果按记录版本读取、`event_id` 由自身载荷复核，哈希逐位不变；`bound_to` 把 2.0.0 事件绑定到标的时
  产生新的 2.1.0 事件，原事件不变；2.1.0 请求可原样携带 2.0.0 上游事件。
- `subject` 身份：调用方提供的稳定 opaque identifier，大小写敏感，只去首尾空白（Codex `648fe6c`，见上节）。
- 测试：`tests/test_adr_0054_0057_versions.py`（2.0.0 信封 + `subject` 拒绝、2.1.0 往返、跨版本读取与 `check_answers`、大小写敏感）；
  `tests/test_event_subject.py` 的实施前金值改为在 2.0.0 构造作用域内复核（`tests/contract_version_support.built_at_pre_bump`），金值未改。
- 物理表 `event.events` 的 `subject` 列（`infrastructure/event/table_definition.py`）：该表从未在任何 catalog 中创建（只读核实，见 ADR-0056
  note），字段 ID 的改变不涉及已部署表。

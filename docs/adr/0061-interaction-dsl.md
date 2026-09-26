# ADR-0061：事件交互 DSL（Phase 3）

| 字段 | 值 |
|---|---|
| 状态 | **Accepted**（2026-09-26） |
| 日期 | 2026-09-26 |
| 决策者 | Claude Code（Opus），依 Raphael 2026-09-26 授权（通过 ADR 自主决定技术方案） |
| 相关 Phase | Phase 3 Event & Interaction Engine（roadmap："交互 DSL / 上游追溯"） |
| 影响范围 | `plugins/events/`（新增 DSL 解析与编译）、`infrastructure/event/upstream.py` 的使用方式不变；**无契约 / Schema 变化** |
| 是否破坏兼容 | 否：现有两个交互算子（A 后 B、共现）及其 spec hash 不变 |

## 背景

交互目前只有两个固定算子（`plugins/events/interactions.py`，参数写在 trigger JSON 中），没有可组合的交互语法；多跳上游链只在各自的运行里逐跳核对。
CLAUDE.md §5 要求新插件能力先有 ADR。

## 裁决

1. DSL 是**数据**（JSON 表达式树），不是代码：节点只有 `ref`（一个已登记事件规格的引用）与四个已审阅算子 `seq(a, b, within)`、`and(a, b, within)`、`not(a, b, within)`
   （A 发生且 `within` 内无 B）、`count(a, at_least, within)`；参数全部显式，无默认；任何未知节点 / 算子 / 字段即拒绝（与 ADR-0040 的"只执行已审查的注册算子"一致）。
2. 编译：表达式树按后序编译成一串**普通的交互 `EventSpec`**（每个内部节点一个或多个规格——`not` 编译为两个，见文末 D-L6-1；`lineage` 恰为其子节点的引用，trigger 为规范 JSON 并逐字绑定子节点 spec hash），
   由已登记的交互 Provider 执行；因此每一跳仍经 `infrastructure/event/upstream.py` 的核对（上游规格、spec hash 精确绑定、Feature / State 并集），不引入新的执行路径。
3. 同一表达式永远编译成同一组规格与哈希（规范化：`and` 的操作数按规格哈希排序；`seq` / `not` 保序）；编译结果与表达式哈希一起记录，可重算核对。
4. 可见性：交互事件的事件时间 = 其最后一个必需输入的可观测时间（与现有算子一致）；`not` 的事件时间 = 窗口结束时刻（在此之前无法知道"B 没有发生"），因此不产生未来函数。

## 后果

- 正面：交互可组合、可审计，完全复用现有上游核对。
- 负面：深层表达式会产生多个中间规格与多次运行；嵌套深度与节点数上限作为编译参数显式给出（无默认）。

## 合规检查

- [x] 无契约 / Schema 变化；LLM 不执行代码（DSL 是数据，只编译到已审阅算子）
- [x] 事件时间不早于可观测时间（`not` 取窗口结束）

## Implementation note (DSL compiler and window operators, 2026-09-26)

不改 `core/`（`EventSpec` / `EventRequest` / `EventProvider` 冻结）、不改 `infrastructure/event`、无 Schema 变化；
既有 `event_sequence` / `event_co_occurrence` 规格与哈希不变（`tests/plugins/events/test_dsl.py` 钉住）。

- `plugins/events/dsl.py`：`parse_expression` / `compile_expression` / `verify_compilation` / `hops`。节点形式
  `{"ref": "event:<name>@<semver>"}`、`{"op": "seq"|"and"|"not", "a", "b", "within_us"}`、
  `{"op": "count", "a", "at_least", "within_us"}`（`within_us` 为正整数微秒，`at_least` 为正整数）；字段必须恰好如此，
  未知节点 / 算子 / 字段、浮点、JSON 常量、重复键、类型不符一律 `DslError`。`CompileLimits(max_depth, max_nodes)`
  两者必填，解析时检查（叶子深度为 1；节点数含叶子）。`ref` 必须是传入 registry 中已有的事件规格。
- 编译（后序）：`seq` → `event_sequence`，`and` → `event_co_occurrence`（操作数按 spec hash 排序），`count` →
  `event_count`（新）。编译出的规格名由内容派生（`dsl_<op>_<hash16>`，`1.0.0`，`observable_lag = 0`），
  相同子表达式共享一个规格；两个操作数是同一定义（如 `seq(a, a)`）即拒绝；裸 `ref` 表达式编译为零个规格。
  `Compilation.record()` 记录表达式哈希、限额、叶子 `(ref, spec hash)`、每个编译规格与根的哈希；
  `verify_compilation` 由记录的规范表达式重新编译并逐项比较。
- `plugins/events/windows.py`（新 Provider，同一基类、同一 `<name>` / `<name>_hash` 绑定，contract suite 通过）：
  `event_window_end`（每个上游事件的窗口结束时刻一个事件；`observable_lag` 恰为窗口）、`event_absence`
  （锚事件且 `[锚 - window, 锚]` 内无另一事件，闭区间，只引用锚）、`event_count`（在事件 e 处 `[e - window, e]`
  内至少 `at_least` 个事件，闭区间，引用窗口内全部事件并记录 `count`）。
- 每一跳经 `run_events(..., upstream_specs=hop.upstream, upstream_results=..., require_full=True)` 核对
  （`tests/infrastructure/event/test_interaction_dsl.py`：真实 feature → state → 叶子事件 → 五跳表达式，逐跳全量核对；
  未来行情扰动不改变任何一层过去的事件表（事件 id 逐位相同）；`not` 在窗口结束前一微秒不存在）。

**与 §2 字面的偏离（待 Codex 追认）**：`not(a, b, within)` 编译为**两个**规格——`w = event_window_end(a, within)`
与 `n = event_absence(anchor=w, absent=b, window=within)`，`n.lineage = (w, b)` 而非 `(a, b)`。原因是契约：
`check_answers` 要求事件时间恰为所引用输入的最晚可见时刻 + 规格的唯一 `observable_lag`，执行器又以同一个 lag 截断
全部上游事件（`EventRequest.visible_at`）。单个规格若以 lag = within 把事件定在窗口结束，它在该时刻看不到窗口内的
B（B 也被推迟 within）；若 lag = 0 则事件只能定在 A 的时刻（未来函数）。两跳构造满足 §4（事件时间 = 窗口结束，
此时窗口内全部 B 可见），且每一跳仍是普通交互规格、仍经上游核对，追溯链为 `n → w → a`。
其他实现选择（字段名 `op` / `within_us`、闭区间边界、`count` 在每个满足条件的 A 处触发）同样记录于此，
状态仍为 FRAMEWORK_IMPLEMENTED / NOT_VALIDATED。

## 决定补记 D-L6-1（2026-09-26，Claude 依 Raphael 授权）

`not(a, b, within)` 编译为**两个**规格：`event_window_end(a)` → `event_absence(·, b)`，而不是 §2 所说的"每个内部节点一个规格"。理由：契约要求事件时间 = 最后引用输入的时间 + 规格唯一的
`observable_lag`，且执行器按同一滞后延迟所有上游事件；单一规格若把时间放在窗口结束则看不到窗口内的 B，若滞后为 0 则会在窗口结束前触发（未来函数）。两规格形式满足 §4（在窗口结束触发、
窗口内全部 B 在那时可见），不改契约。§2 因此修订为"每个内部节点编译为一个或多个普通交互规格"；其余裁决不变。


# ADR-0053: 增加生命周期转移 VALIDATION → FAILED（D-VFAIL）

| 字段 | 值 |
|---|---|
| 状态 | Proposed (2026-09-26)，起草: Claude Code（Opus），待 Raphael 决定（红线） |
| 日期 | 2026-09-26 |
| 决策者 | **Raphael**（ADR-0006 状态机是冻结的生命周期契约，红线） |
| 起草者 | Claude Code（Opus） |
| 相关 Phase | Phase 4 / 8 验证；Phase 11 持续研究循环 |
| 影响范围 | Lifecycle（`core/lifecycle/strategy.py`）/ 研究循环护栏 / Failure Registry |
| 是否破坏兼容 | 否：只增加一条边；已有的每条生命周期历史继续合法；导出 Schema 不变 |
| 前置 | [ADR-0006](0006-strategy-lifecycle.md)、[ADR-0011](0011-lifecycle-subject-authorization-and-time.md)、[ADR-0019](0019-lifecycle-evidence-minimum.md)、[ADR-0041](0041-validation-robustness.md)、[ADR-0049](0049-continuous-research-loop.md)、研究宪法 C-P3 |

## 决策包（Decision packet）

- **问题**：是否在 ADR-0006 状态机中增加 `VALIDATION → FAILED`，让验证阶段发现的技术失败（不可复现、运行出错）把对象标为终态 FAILED？
- **选项**：A. 增加这一条边，限定只用于 C-P3 技术失败，证据按本 ADR 规定；B. 不改：失败记录照写，生命周期停在 VALIDATION；C. 同时增加 `OOS → FAILED`。
- **推荐**：A（只加 `VALIDATION → FAILED`）。
- **不决定时保持不变**：失败记录照写进 Failure Registry，生命周期停在 VALIDATION（`research/loop/stages.py` 的 `_NO_FAILED_EDGE`）。

## 背景

- 宪法 C-P3："不可复现 = FAILED"。ADR-0006 只有 `CANDIDATE → FAILED`（"errored or not reproducible"），
  而复现核对（`G0.reproducibility`、`G0.signal_determinism`）在 **VALIDATION** 中才发生（ADR-0037 / 0041 的 G0）。
- 现状：研究循环在 VALIDATION 中遇到技术失败时写 `FailureRecord(terminal_state="FAILED")`，但不能转移生命周期
  （`research/loop/stages.py:526` 的 `_NO_FAILED_EDGE`，`:589` 与 `:669` 两处），记录 `technical_failures_lifecycle_unchanged`。
  于是 Failure Registry 说"FAILED"，生命周期说"VALIDATION"——两份权威记录不一致，对象还可能被再次评估。
- 另一方面，把技术失败强行记成 `VALIDATION → REJECTED` 会把"从未得到有效检验"混同于"检验不通过"，违反 ADR-0006 对 REJECTED / FAILED 的区分。

## 裁决（提案）

### 1. 状态机

`ALLOWED_TRANSITIONS` 增加 `(VALIDATION, FAILED)`，标注 "technical failure during validation (C-P3)"。
不需要人工批准（与 `CANDIDATE → FAILED` 一致；不进入 `HUMAN_APPROVAL_TRANSITIONS`）。终态集合、其余边、批准集合不变。
**不**增加 `OOS → FAILED` 或 `REVALIDATION → FAILED`（见备选方案）。

### 2. 允许使用的情形（穷举）

只有下列 C-P3 技术失败可以走这条边：

| 情形 | 原因码 | 典型证据 |
|---|---|---|
| 验证中重跑结果哈希不等于登记的 Run（`G0.reproducibility` FAIL） | `NOT_REPRODUCIBLE` | 验证报告（含该 G0 门）+ 两个 Run |
| 同一输入信号不确定（`G0.signal_determinism` FAIL） | `NOT_REPRODUCIBLE` | 验证报告 |
| 对象自身的运行出错：Provider 输出违反契约（`check_answers` 拒绝）、对象代码抛出异常 | `RUN_ERRORED` | 出错的 Run + 错误摘要 |

**不得**使用的情形：

- 任何统计门、稳健性门、封存 OOS 的 FAIL → 仍是 `VALIDATION → REJECTED`（ADR-0006）；
- `INCONCLUSIVE` → 留在 VALIDATION；
- **基础设施故障**（预算耗尽、存储 / 网络 / 内存错误、验证器自身异常等不能归因于对象的错误）
  → 不转移，写审计记录，本轮中止或重试；这类错误不是对象"不可复现"，不应把它变成终态。
  现有 `result.report is None`（验证器自身出错，`stages.py:619` 起）属于此类，**保持生命周期不变**。

### 3. 证据（ADR-0019 之上的最小要求）

契约层仍只要求"至少一项非空证据"（ADR-0019 D-27.1，不校验内容，D-27.3）。本 ADR 规定研究循环与任何自动触发者
**必须**同时给出：

1. `validation_report:<report_id>`（`NOT_REPRODUCIBLE` 情形；报告中必须有 FAIL 的 G0 门）或 `run:<run_id>`（`RUN_ERRORED` 情形）；
2. 对应 `FailureRecord` 的内容哈希（`failure_record:<hash>`，其 `terminal_state = FAILED`、`reason_code` 属于 `REPRODUCIBILITY` 类）；
3. 本轮引用（`loop_round:<loop_id>:<index>`），人工触发时为决定记录。

Registry / Control Plane 在授权服务落地后核验这些引用的存在与一致（ADR-0011 运行时延期义务，不变）。

### 4. 研究循环

- `research/loop/stages.py`：`_NO_FAILED_EDGE` 去掉 `VALIDATION`（`OOS` 保留）；在 §2 允许的情形转移到 FAILED，
  其余技术失败保持现行为；`technical_failures_lifecycle_unchanged` 只再包含 OOS 与基础设施故障。
- `apps/worker/loop.py`：`AUTOMATABLE_TARGETS` 已含 FAILED，护栏不需放宽；`automation_reachable_states()` 结果不变
  （FAILED 已可由 `CANDIDATE → FAILED` 到达），测试需证明这一点。

## 备选方案

| 方案 | 优点 | 缺点 | 结论 |
|---|---|---|---|
| **A（推荐）** 只加 `VALIDATION → FAILED` | 生命周期与 Failure Registry 一致；C-P3 在发生复现核对的状态可执行 | 改冻结状态机 | 推荐 |
| B 不改 | 零成本 | 两份记录不一致；对象停在 VALIDATION 可能被反复评估 | 默认 |
| C 同时加 `OOS → FAILED` | 覆盖封存 OOS 中的技术失败 | 封存 OOS 一经开封即消耗预算（C-S2），技术失败时如何计入预算、能否重开需另行决定；超出本决定 | 另议 |
| D 技术失败记为 `VALIDATION → REJECTED` | 不改状态机 | 混淆"没检验成"与"检验不通过"，污染 Failure Registry 的原因统计 | 拒绝 |

## 后果

- 正面：C-P3 在 VALIDATION 中可执行；生命周期与 Failure Registry 一致；技术失败对象成为终态，重试须新版本 CANDIDATE 并计入尝试次数。
- 负面：误把基础设施故障判成对象故障会让对象过早终结——因此 §2 严格限定情形。
- 复现：既有历史全部合法；导出 Schema（`LifecycleTransition` 的状态枚举）逐字节不变；不升契约版本。

## 实施计划（批准后）

| 模块 / 文档 | 改动 |
|---|---|
| `core/lifecycle/strategy.py` | `ALLOWED_TRANSITIONS` 加一条边；注释引用本 ADR |
| `research/loop/stages.py` | §4 的转移逻辑与证据；基础设施故障路径不变 |
| `docs/architecture/07-validation.md` §3、`docs/research/failure-registry.md` | 状态图与说明加这条边（ADR-0006 正文不改，索引注明被本 ADR 补充） |

测试：`tests/test_lifecycle.py` 逐边枚举含新边、无人工批准要求、终态仍不可再转移；`tests/test_lifecycle_evidence.py` 新边空证据拒绝；
循环测试覆盖 `NOT_REPRODUCIBLE` / `RUN_ERRORED` → FAILED、统计 FAIL → REJECTED、INCONCLUSIVE 留在 VALIDATION、验证器自身出错与 OOS
技术失败保持不变；`automation_reachable_states()` 不变；导出 Schema 逐字节不变；四项工程检查。

## 合规检查

- [x] 只增加一条边，不删改其他边、终态与人工批准集合
- [x] 不修改 Validation Constitution（落实 C-P3，不改原则）
- [x] 不引入任何数值阈值
- [ ] 由 Raphael 本人批准（红线）——待定

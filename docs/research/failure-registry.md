# Failure Registry

| 字段 | 值 |
|---|---|
| 状态 | 空（Architecture Bootstrap） |
| 性质 | **追加式（append-only），永不删除** |

失败实验是研究资产。它们防止重复犯错、提供负面知识，并为多重检验提供真实的 trial count。

**范围（ADR-0006）**：本注册表只记录 **REJECTED / FAILED**（从未成立的对象）。
**RETIRED ≠ FAILED**：曾经成立并被启用、之后停止使用的对象写入**退役记录**（见 [07-validation.md §4.2](../architecture/07-validation.md)），不在此处登记。
**FAILED 的来源（ADR-0006 + [ADR-0053](../adr/0053-validation-failed-transition.md)）**：`CANDIDATE → FAILED`（运行出错 / 不可复现），以及 `VALIDATION → FAILED`（验证中 `G0.reproducibility` / `G0.signal_determinism` FAIL，或对象自身的运行出错）。基础设施故障与 OOS 中的技术失败也写 `terminal_state = FAILED` 的记录，但生命周期不变（研究循环摘要的 `technical_failures_lifecycle_unchanged`）。

## 记录字段（见 07-validation.md §4.1）

| 字段 | 说明 |
|---|---|
| `subject_ref` | 失败对象 `kind:name@version` |
| `terminal_state` | REJECTED / FAILED |
| `reason_code` | 例：`LEAKAGE_DETECTED`、`NOT_SIGNIFICANT_AFTER_MTC`、`OOS_DECAY`、`COST_KILLED`、`PARAM_UNSTABLE`、`STATE_CONCENTRATED`、`NOT_REPRODUCIBLE`、`DATA_QUALITY`、`HUMAN_VETO` |
| `gate_id` | 失败的验证门 |
| `evidence` | ExperimentRun / ValidationReport 引用 |
| `hypothesis_family_id` | 所属假设族（用于 trial count） |
| `lessons` | 可检索的教训 |
| `recorded_at` | UTC |

## 规则

- 任何 Agent 或人都不得删除条目；更正只能追加新条目引用旧条目。
- 新假设在登记前应检索本注册表，避免重复已失败的方向（除非明确声明为复现检查）。
- "重试" = 新版本的新 CANDIDATE；旧失败保留且计入 trial count。

## 失败模式分类统计

| reason_code | 次数 |
|---|---|
| — | 0 |

## 条目

| subject_ref | terminal_state | reason_code | gate | evidence | recorded_at |
|---|---|---|---|---|---|
| — | — | — | — | — | — |

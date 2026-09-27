# ADR-0064: G4 容量检查的成交量来源一致性（数据集路径，失败关闭）

| 字段 | 值 |
|---|---|
| 状态 | **Proposed**（2026-09-27）；方向由 Codex 选定（B66 选项 A），本文措辞待 Codex 审阅接受后才实施 |
| 日期 | 2026-09-27 |
| 决策者 | Codex（技术协调者，CLAUDE.md §0） |
| 起草者 | Claude Code（Opus），只起草，不改变决定 |
| 相关 Phase | Phase 4 / 8（C-R5 容量，G4）；ADR-0041、ADR-0054 §4、B61 |
| 影响范围 | `research/strategies/validation.py` 的容量输入构造与 `research/validation/robustness.py::capacity_check` 的一个新 INCONCLUSIVE 原因；不改 Contract / Schema / Constitution / Profile / 阈值 |
| 是否破坏兼容 | 否（合成 / 无数据集路径逐字节不变；数据集路径上两来源一致时结果不变） |

## 背景（Context）

- G4 容量检查（C-R5，`capacity_check`）只从调用方给出的 `ValidatorSetup.bar_volume`（`(instrument, interval_start) → 成交量`）取 bar 成交量
  （`_capacity_fills`：`volumes.get((fill.instrument, fill.fill_time))`）；没有该值 → `G4.capacity.estimated` = INCONCLUSIVE（`bar_volume_missing`）。
- B61 起，数据集路径上试验实际执行的价格 bar（`ValidatorSetup.dataset_bars`，价格 manifest 经证明的 `DatasetPriceBars`）带有证明过的
  `PriceBar.volume`。
- 数据集研究循环的 `bar_volume` 来自另一个绑定输入：特征 manifest 的观察（`research/loop/dataset_source.py::bar_volume`）。同一根 bar 因此可能有
  两份成交量记录，今天没有任何核对；二者不一致时 G4 会静默使用 `bar_volume` 那一份。

## 决策（Decision）

1. **权威来源**：数据集路径上，已执行的 `DatasetPriceBars` 是权威价格 bar；`bar_volume` 是另行绑定的特征 manifest 输入。G4 在二者冲突时**不得静默择一**
   （与 ADR-0041 对冲击系数冲突 `impact_coefficient_mismatch` 的处理同一原则）。
2. **核对范围**：仅当 `ValidatorSetup.dataset_bars` 给出（数据集路径）时，对**每一笔**容量成交（`traded_fraction > 0`），把 `bar_volume` 在确切键
   `(instrument, fill_time)` 上的值与 `dataset_bars` 中同一键（`PriceBar.instrument == instrument`、`PriceBar.interval_start == fill_time`，即该成交的执行 bar）
   的 `PriceBar.volume` 比较。比较为 `Decimal` 精确相等（数值相等，不做容差、不经 float、不按精度截断）。
3. **一致**：所有成交两值都存在且相等 → 结果与今天完全相同（容量、冲击估计、`G4.capacity.required`、`details` 逐字节不变）。
4. **不一致（失败关闭）**：任一成交两值都存在但不相等 → `G4.capacity.estimated` = INCONCLUSIVE，稳定原因名 **`bar_volume_source_mismatch`**；
   不从任一来源计算容量或冲击（不产生 `G4.capacity.required` / `G4.capacity.impact_estimated`，与今天 `bar_volume_missing` 路径的门集合相同）；
   `details` 记录不一致的成交数与第一处不一致（instrument、时刻、两个值的规范文本，以及各自来源：`bar_volume` / `dataset_bars`）。
5. **缺失**：任一成交在任一来源缺值（`bar_volume` 无该键；或 `dataset_bars` 中没有该执行 bar；或该 bar 的 `volume` 为 `None`）→ 保持明确的缺证据结果
   `bar_volume_missing`（INCONCLUSIVE，与今天同名）。同时存在不一致与缺失时报告 `bar_volume_source_mismatch`（冲突优先于缺失），`details` 同时记录缺失数。
6. **不变**：合成 / 无数据集路径（`dataset_bars is None`）逐字节不变；`max_participation` 缺失时仍先报 `profile_field_missing`；没有成交时仍为 `no_trades`。
   不新增阈值、不改 Profile、Constitution、契约或 Schema；新原因名只是 `capacity_check` 的 INCONCLUSIVE 原因之一（研究内部类型可增加字段以携带核对结果）。

## 备选方案（Alternatives）

| 方案 | 优点 | 缺点 | 为何未选 |
|---|---|---|---|
| A. 要求一致，冲突失败关闭（选定） | 不静默择一；一致时结果不变；合成路径不变 | 需在两来源都存在时逐笔核对 | Codex 选定 |
| B. 数据集路径只用 `dataset_bars` 的成交量，拒绝另给的 `bar_volume` | 单一来源 | 改变既有调用方（数据集循环）的输入约定 | 未选 |
| C. 维持现状，记为已知限制 | 无改动 | 冲突时静默使用特征 manifest 的值 | 未选 |

## 后果（Consequences）

- 正面：数据集路径上容量估计所用的成交量与实际执行的 bar 一致，或明确 INCONCLUSIVE。
- 负面：两份数据若在精度 / 表示上不同但数值相等，按 `Decimal` 数值相等视为一致；若数值确有差异，数据集路径的 G4 容量将为 INCONCLUSIVE，直到数据来源被修正。

## 实施计划（接受后）

- 代码：`_capacity_fills`（单标的与多标的共用的容量输入构造，若多标的路径另有构造则同样处理）在数据集路径上逐笔比较并把核对结果交给 `capacity_check`；
  `capacity_check` 增加 `bar_volume_source_mismatch` 分支。
- 测试：一致（结果与今天逐字节相同）、不一致（INCONCLUSIVE + 原因 + details，且无 required / impact 门）、缺失（`bar_volume_missing`；两种缺失来源）、
  不一致与缺失并存（mismatch 优先）、来源 / 时刻绑定（只比较同一 `(instrument, fill_time)`；相邻 bar 的值不被使用）、合成路径已钉哈希不变、
  数据集研究循环路径（非 PostgreSQL 的 e2e）。
- 文档：ADR 索引、PROJECT_STATUS、PROJECT_MEMORY、完成计划 B66。不运行 PostgreSQL，不触网，不安装。

## 合规检查

- [x] 不修改 Domain Contract、Schema、Constitution、Validation Profile；不新增数值阈值
- [x] 冲突与缺证据都失败关闭（INCONCLUSIVE），从不 PASS
- [x] 合成 / 无数据集路径逐字节不变

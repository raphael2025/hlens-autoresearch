# ADR-0064: G4 容量检查的成交量来源一致性（数据集路径，失败关闭）

| 字段 | 值 |
|---|---|
| 状态 | **Accepted**（2026-09-27，Codex 依 Raphael 授权接受本文措辞，含精确 `Decimal` 比较、不一致优先于缺失与原因名 `bar_volume_source_mismatch`）；实施见 B66 |
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

## Implementation note（B66，2026-09-27）

状态 **CODE_COMPLETE / DEBUG_PENDING**。无 core / 契约 / Schema / Profile / 阈值变化。

- `research/validation/robustness.py`：研究内部类型 `VolumeSourceMismatch`（instrument、时刻、`bar_volume`、`dataset_bars_volume`）与
  `CapacityFill.source_mismatch`（默认 `None`，合成路径构造的对象不变）；`capacity_check` 在 `max_participation` 缺失之后、`bar_volume_missing`
  之前新增分支：有任一成交带 `source_mismatch` → `G4.capacity.estimated` = INCONCLUSIVE，metric `bar_volume_source_mismatch`（常量
  `BAR_VOLUME_SOURCE_MISMATCH`），value = 不一致成交数；不计算容量 / 冲击，不产生 `.required` / `.impact_estimated`；`details["bar_volume_source_mismatch"]`
  记录 `fills`、`missing` 与第一处不一致（instrument、ISO 时刻、两个值的规范文本）。
- `research/strategies/validation.py::_capacity_fills`（单 / 多标的共用的唯一容量输入构造）：`dataset_bars` 给出时，按 `(fill.instrument, fill.fill_time)`
  取已执行 bar 的 `PriceBar.volume`；该 bar 不存在或无 volume → 按缺失（`bar_volume_notional = None`）；两值都在且 `Decimal` 不等 → `source_mismatch`。
  合成路径（`dataset_bars is None`）代码路径不变。
- 测试（`tests/research/strategies/test_backtest_validation.py`）：两来源相等时数据集路径的容量 details 与门和合成路径完全相同，另一种表示的同值
  （`x * Decimal("1.00")`）不算冲突；`bar_volume` 在一笔成交处不同 → 仅 `G4.capacity.estimated` INCONCLUSIVE `bar_volume_source_mismatch`、无容量 / 冲击、
  details 精确；同一 `bar_volume` 在合成路径上不比较；只比较成交时刻的已执行 bar（改未成交 bar 的值不影响）、其他标的的键 = 缺失；任一来源缺值 →
  `bar_volume_missing`；不一致与缺失并存 → mismatch 且 `missing` 计数；完整评估中 G0 绑定通过、G4 门 INCONCLUSIVE、报告判定 INCONCLUSIVE，
  相等时无该门。数据集路径夹具（`test_backtest_validation.py` 的 `_proven` / 交易输入，`test_multi_instrument_validation.py` 的 `Book.proven` / `inputs`，`test_cross_sectional_momentum.py` 的数据集路径用例）按 B61 的真实生产者带 volume（`with_volume=True`，只用于数据集路径），使执行的 TrialRun bar 恰为所给 `DatasetPriceBars.bars`、G0 绑定不被绕过；`bar_volume` 仍是另行给出的特征 manifest 值；合成路径的 bar 与哈希不变。
- 实际运行：（6 GB 上限，未过滤管道、`set -o pipefail`，pytest 自身退出码）`pytest -q -rs -m "not postgres" tests/research/strategies tests/research/validation tests/research/synthetic_lab tests/research/loop tests/infrastructure/e2e/test_research_loop_dataset_conditional.py tests/infrastructure/e2e/test_research_loop_dataset_g5_units.py` + 文档一致性 + 架构边界：第一次（修正 `test_cross_sectional_momentum.py` 的数据集路径夹具之前）→ `2 failed, 711 passed, 1 warning in 1363.20s`，**退出码 1**（两项为 `test_validated_end_to_end_on_the_multi_instrument_path[p,n]` / `[p,p,n]`：交易输入不带 volume，G0.manifest_binding 正确地判 FAIL）；修正后该文件 `29 passed`（退出码 0）；第二次完整运行 → `713 passed, 1 warning in 1360.32s`，**退出码 0**，无 skip。在 B66 之前的源码上 6 项新测试中 4 项失败（相等与只比较成交 bar 两项为不变量）。`ruff check .` → `All checks passed!`（退出码 0）；`ruff format --check .` → `765 files already formatted`（退出码 0）；`mypy` → `Success: no issues found in 597 source files`（退出码 0）。真实数据端到端 `test_research_pipeline_real_data.py`（PostgreSQL 标记）**未运行**：其交易输入即 `backtest_bars_from_dataset` 的 bar、`bar_volume` 取自同一选择的观察，按构造两来源一致。
- 未运行：PostgreSQL 标记测试；无网络、无安装。

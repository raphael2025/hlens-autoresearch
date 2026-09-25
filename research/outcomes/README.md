# research/outcomes

Outcome 物化（Phase 4，[ADR-0037](../../docs/adr/0037-outcome-engine-and-minimal-validation-pipeline.md)）。
Outcome 永远只作为标签，不作为输入（Constitution C-L2）。研究代码，不是生产代码（H5）。

> 状态：**FRAMEWORK_IMPLEMENTED / NOT_VALIDATED**。

| 模块 | 内容 |
|---|---|
| `table.py` | `materialize(provider, request)`：计算 → `OutcomeResult.check_answers` 核对 → `OutcomeTable`。表只交出标签；`known_as_of(t)` 只返回 `available_time <= t` 的标签；`rows()` 是给未来 `outcome` 表的 JSON 行导出（尚未持久化） |
| `sources.py` | 价格源：`bars_from_synthetic`（ADR-0042 合成市场 → `OutcomePriceBar`）。Canonical `bars_1m` 读取尚未接入 |

契约在 `core/contracts/outcome.py`，Provider 实现在 `plugins/outcomes/`（`ForwardReturnOutcome`、`TripleBarrierOutcome`），
provider-agnostic suite 在 `tests/contract_suites/outcome.py`。表不提供任何构造 `FeatureObservation` 等输入 DTO 的途径。

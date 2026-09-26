# research/outcomes

Outcome 物化（Phase 4，[ADR-0037](../../docs/adr/0037-outcome-engine-and-minimal-validation-pipeline.md)）。
Outcome 永远只作为标签，不作为输入（Constitution C-L2）。研究代码，不是生产代码（H5）。

> 状态：**FRAMEWORK_IMPLEMENTED / NOT_VALIDATED**（2026-09-26 持久化补充：CODE_COMPLETE / DEBUG_PENDING，见文末）。

| 模块 | 内容 |
|---|---|
| `table.py` | `materialize(provider, request)`：计算 → `OutcomeResult.check_answers` 核对 → `OutcomeTable`。表只交出标签；`known_as_of(t)` 只返回 `available_time <= t` 的标签；`rows()` 是给未来 `outcome` 表的 JSON 行导出 |
| `store.py` | `OutcomeTableStore`：内容寻址、只写一次的整表文件存储（见文末） |
| `sources.py` | 价格源：`bars_from_synthetic`（ADR-0042 合成市场 → `OutcomePriceBar`）。Research Dataset 的 Canonical `bars_1m` 经 `infrastructure/bars`（`outcome_request_from_dataset`，ADR-0037 实现注记 2026-09-25）进入 `OutcomeRequest` |

契约在 `core/contracts/outcome.py`，Provider 实现在 `plugins/outcomes/`（`ForwardReturnOutcome`、`TripleBarrierOutcome`），
provider-agnostic suite 在 `tests/contract_suites/outcome.py`。表不提供任何构造 `FeatureObservation` 等输入 DTO 的途径。

## 调试批次（2026-09-26）：Outcome 表持久化

> 状态：**CODE_COMPLETE / DEBUG_PENDING**（代码与测试完成，尚待调试 / 复核；无契约 / Schema 变化）。

- `OutcomeTable` 新增可选 `request_hash` / `provider_hash`（默认 `None`），由 `materialize` 从 `OutcomeResult` 填入；`rows()` 不变。
- `store.py`：`OutcomeTableStore(root)` 的 `put` / `get` 与 `table_hash(table)`。一张表 = `<root>/<table_hash>.json`，内容为规范 JSON
  （`kind = research.outcome_table`、`format_version = 1.0.0`；`table_hash` = 不含 `table_hash` 字段的同一对象的 SHA-256）。
  - 写入：只接受 `materialize` 产出的表（缺结果哈希、或 `OutcomeResult` 重建不通过即拒绝）；先写新临时文件（`O_EXCL | O_NOFOLLOW`、
    完整写入、`fsync`），再 `os.link` 发布（**从不覆盖**），`fsync` 目录并回读逐字节比较。同名文件已存在且字节相同 = 无操作（文件不动）；
    字节不同或是符号链接 = `OutcomeTableConflict`，原文件保持原样。
  - 读取：不跟随符号链接；严格解析（重复键、NaN / Infinity、非规范文本均拒绝）；核对字段集与格式；重算 `table_hash` 并与文件名、内嵌值比较；
    逐个重新校验 `OutcomeLabel`，并重建 `OutcomeResult` 使其 `result_hash` 检查再次运行。篡改、截断、伪造哈希一律 `OutcomeTableCorrupted`，从不修复。
- Outcome 仍只是标签（C-L2）：存储只接收 / 返回 `OutcomeTable`，载入的表与物化的表提供完全相同的接口，没有构造输入 DTO 的途径。
- 仍然存在：这只是研究侧的本地文件存储，不是 Iceberg `outcome` 表；`root` 由调用方给出（测试用临时目录，不得把行情派生文件提交进仓库，H9）。
  统计仍为 `float` 正态近似（见 `research/validation/README.md`；须先按 ADR-0052 补全契约，受阻）。

# research/states

市场状态研究工具（Phase 2，[ADR-0035](../../docs/adr/0035-state-provider-contract.md)）。条目登记于
[state-library.md](../../docs/research/state-library.md)。研究代码，**永不直接成为生产代码**（H5）。

> 状态：**FRAMEWORK_IMPLEMENTED / NOT_VALIDATED**（2026-09-25）。框架与冒烟测试已交付；尚未在真实
> Research Dataset 上运行，也未经逐项调试与复核。

## 分工

| 位置 | 内容 |
|---|---|
| `core/contracts/state.py` | `StateProvider` Protocol 与 5 个 DTO（`StateInput`、`StateRequest`、`StateValue`、`StateResult`、`StateProviderDescriptor`）；method 参数编码 `state_method` / `parse_state_method` |
| `infrastructure/state/` | `run_state`（每个评估时刻只把可见输入交给 Provider：`evaluation_time <= t`，训练型再限于 `(t - training_window, t]`）；`state_inputs`（由已回答的 Feature 请求 / 结果构造输入）；`state_table`（Arrow 物化） |
| `infrastructure/state/` 的存储与入口（ADR-0089 / 0102） | `StateResultStore`（整次运行的本地制品）、`state.states` 物理表与 `StateTable`（尚未在生产 catalog 创建）；命令 `python -m infrastructure.state.run_cli`：`compute`（默认只打印摘要，`--store` / `--apply-table` 才落盘）、`show`（按 `result_hash` 读回并核验）、`list` |
| `plugins/states/` | 首批 Provider：`VolatilityRegimeProvider`、`LiquidityRegimeProvider`（尾随窗口经验分位分桶）、`TrendRangeProvider`（效率比） |
| `research/states/report_cli.py`（本目录，ADR-0102） | `python -m research.states.report_cli`：从 `StateResultStore` 读一次运行，经 `diagnose` 输出 Markdown；给 `--out` 才经 `write_state_diagnostics` 写报告（`--min-run` 必填，无默认值） |
| `research/states/diagnostics.py`（本目录） | 状态分布、持续时间、转移矩阵、标签闪烁（短 run 占比、切换率）；`render_markdown` 报告；`StateDiagnostics.to_payload()` / `diagnostics_hash` / `from_payload` |

## 规则

- 输入只能是 Feature 值（`StateInput.feature` 必须是 `kind=feature`）；Outcome 在构造时即被拒绝。
- 训练型状态模型（`training_window` 非空）必须固定 `seed`；执行器结构性地只给出固定尾随窗口内的输入，
  不可能在全样本上拟合。
- 模型参数（分位切点、最少历史、阈值）是规格参数，写在 `StateSpec.method` 中并受 spec hash 绑定；
  诊断报告的 `min_run` 由调用方给出。二者都不是验证阈值。
- 诊断只描述序列，不判定状态"好坏"；任何判定阈值属于 ValidationProfile。

## 实现说明：诊断载荷与哈希（2026-09-26，CODE_COMPLETE / DEBUG_PENDING）

- `StateDiagnostics.to_payload()`：确定性、可直接 JSON 化的载荷（`kind = state_diagnostics`、`schema_version = 1.1.0`、
  `probability_places`、`source_result_hash`）；`Decimal` 写成其精确文本，时间写成 ISO-8601 UTC（非 UTC 时区换算为 UTC，无时区的时间拒绝），
  逐状态映射按键排序；`state_space`（声明顺序）与 `runs`（时间顺序）保留语义顺序。
- `diagnostics_hash`：载荷的 `content_hash`。`from_payload(payload, expected_hash=None)` 重建报告：键集合、类型、
  版本逐项校验，载荷必须恰好是重建结果的规范形式（非规范时间文本、浮点数、篡改后的哈希不符都拒绝）。
- 诊断没有自己的窗口：它只描述调用方给出的序列，因此某时刻之后的数据变化不会改变截至该时刻的报告（有测试）。

### 来源绑定与版本兼容（payload 1.1.0，2026-09-28，CODE_COMPLETE / DEBUG_PENDING）

- `diagnose()` 输入 `StateResult` 时，载荷 `source_result_hash` = 该结果的 `result_hash`；输入裸 `(time, label)` 序列时
  显式写 `null`。新计算的诊断一律输出 `schema_version = 1.1.0`，该字段参与 `diagnostics_hash`。
- `source_result_hash` 只把报告**声明**的来源绑定进报告内容与哈希：它不证明该 `StateResult` 在任何 Registry 中存在、
  已被运行或与某个 Research Dataset 对应；这些仍是 Registry / Runner 的未实现义务（ADR-0035 已知缺口）。
- 旧 1.0.0 载荷（无 `source_result_hash` 键）仍可读：`from_payload` 接受 1.0.0 与 1.1.0，并按各自的键集合严格校验；
  1.0.0 载荷里出现 `source_result_hash` 键、或 1.1.0 载荷缺该键都拒绝。由 1.0.0 重建的报告记住其版本，
  `to_payload()` 逐键重现原 1.0.0 规范形状，`diagnostics_hash` 复原旧报告 id；它不会被静默升级为 1.1.0。
  版本状态是 dataclass 的内部字段，只由 `from_payload` 设为 1.0.0；1.0.0 报告不能携带来源哈希。

写入：`research/reports/state_diagnostics.py` 以 `diagnostics_hash` 为报告 id 写出载荷（写前经 JSON 往返与
`from_payload(..., expected_hash=...)` 核对），由 `apps/api` 的 `ReportStore` 以 `state_diagnostics` 种类提供。

剩余限制（留待调试阶段）：来源哈希不认证 Registry 存在性（见上）；未在真实 Research Dataset 上运行。

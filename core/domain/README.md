# core/domain

领域实体与值对象、不变量（docs/architecture/02-domain.md）。不得包含 I/O 或具体技术依赖。

`base.py` 另含只读映射值类型 `FrozenMapping` 与内容哈希规范化约定（02-domain.md §1.1、§3.1）；
`selection.py` 存放 Profile 选择的共享值对象，避免复现元组与选择规则契约之间的循环导入；
`execution.py` 存放 `ExecutionMode`，供生命周期与退役记录共用（`core/lifecycle/strategy.py` 重导出），
以保证 Domain 不反向依赖 `core/lifecycle`。

`research.py` 另含整体判定的确定性函数 `derive_verdict` 与 `require_unique_gate_ids`
（07-validation.md §2.1）：`ValidationReport.verdict` 必须精确等于 `derive_verdict(gates)`，
任何想影响判定的理由都必须物化为报告内的一个门；契约层只做结构与配对检查，
阈值来源真实性与门集合完整性属于未实现的验证服务。

`specs.py` 另含信息流白名单（`FEATURE_INPUT_KINDS` / `FEATURE_INPUT_ZONES` /
`STRATEGY_SIGNAL_KINDS`，02-domain.md §2.1）：它只校验**声明层面的直接引用**，
传递依赖闭包与实际数据的泄漏检测仍属 Registry / Runner。

> Phase 0：已有领域契约代码；不包含 Feature / Strategy / Backtest 计算实现。修复状态见 PROJECT_STATUS.md。

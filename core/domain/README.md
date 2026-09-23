# core/domain

领域实体与值对象、不变量（docs/architecture/02-domain.md）。不得包含 I/O 或具体技术依赖。

`base.py` 另含只读映射值类型 `FrozenMapping` 与内容哈希规范化约定（02-domain.md §1.1、§3.1）；
`selection.py` 存放 Profile 选择的共享值对象，避免复现元组与选择规则契约之间的循环导入。

> Phase 0：已有领域契约代码；不包含 Feature / Strategy / Backtest 计算实现。修复状态见 PROJECT_STATUS.md。

# core/

**冻结层。** Domain 模型、契约、Lifecycle、错误分类、历史 major 只读兼容。仅依赖标准库与 Pydantic。修改需 ADR（CLAUDE.md H1）。

当前契约版本 `CONTRACT_SCHEMA_VERSION = 2.0.0`（ADR-0008 只读载荷 + ADR-0009 实验身份，
ADR-0010 纠偏）。模型校验只接受同 major；`1.x` 载荷走 `core/compat/v1.py` 的只读入口。

受支持的构造路径只有构造函数、`model_validate` / `model_validate_json` 与
`model_copy`（带 `update` 时重新走完整校验）。`model_construct()` 不校验，
是 Pydantic 面向可信数据的低层逃生口，**不是**受支持的外部载荷入口（ADR-0010 §D-13）。

> Phase 0 已完成（tag `phase-0-complete`）：契约、状态机、错误分类、语义身份与 v1 只读兼容已实现；契约 2.0.0 已随合并视为发布，破坏性变化须升 major + ADR。当前状态见 PROJECT_STATUS.md。

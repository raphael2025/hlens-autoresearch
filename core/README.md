# core/

**冻结层。** Domain 模型、契约、Lifecycle、错误分类、历史 major 只读兼容。仅依赖标准库与 Pydantic。修改需 ADR（CLAUDE.md H1）。

当前契约版本 `CONTRACT_SCHEMA_VERSION = 2.0.0`（ADR-0008 只读载荷 + ADR-0009 实验身份）。
模型校验只接受同 major；`1.x` 载荷走 `core/compat/v1.py` 的只读入口。

> Phase 0：契约、状态机、错误分类与 v1 只读兼容已实现；Phase 0 关闭复审仍在进行。当前状态见 PROJECT_STATUS.md。

# core/contracts

Provider 接口、跨 Plane DTO、JSON Schema 导出（05-plugin.md、02-domain.md §3）。所有 Schema 带 schema_version。

`registry.py` 的 `CONTRACT_MODELS` 是"所有核心实体都有契约与 Schema 导出"的唯一来源：
当前 36 个模型导出到 `schemas/` 顶层；`schemas/v1/` 是 v1 只读快照，导出不会写入其中。

> Phase 0：已有验证架构契约与 JSON Schema 导出代码；Provider 签名交付范围仍需明确。当前状态见 PROJECT_STATUS.md。

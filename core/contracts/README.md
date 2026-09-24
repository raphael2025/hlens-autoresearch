# core/contracts

跨 Plane DTO、JSON Schema 导出与 Provider 接口（05-plugin.md、02-domain.md §3）；Provider 接口按 ADR-0017 的节奏交付。所有 Schema 带 schema_version。

`registry.py` 的 `CONTRACT_MODELS` 是"所有核心实体都有契约与 Schema 导出"的唯一来源：
当前 59 个模型导出到 `schemas/` 顶层；`schemas/v1/` 是 v1 只读快照，导出不会写入其中。

`revision.py`（Phase 1 B1，ADR-0023）：双时间、availability / precedence 绑定、append-only revision DAG、
PIT 输入与 maximal-head 结果形状的 8 个契约（见 02-domain.md §2.2）。只有契约与不变量，不含 PIT 选择算法或存储。

`universe.py`（Phase 1 B2，ADR-0024 / ADR-0023 §6）：listing episode / revision、`UniverseSelectionSpec` 及其专用绑定、
成员 / 排除清单与 `ResearchDatasetManifest` 的 13 个契约（见 02-domain.md §2.3）。不含 universe 选择器或 PIT 执行器。

Provider 接口的交付节奏由 ADR-0017 定下：Phase 0 只冻结职责、概念输入输出、确定性与版本语义；
每类 Provider 的可执行 Protocol、DTO 与 provider-agnostic contract tests，在首次消费它的 Phase
开始实现之前交付，并计入该 Phase 的验收。

> 当前状态：本目录只有验证架构契约与 Schema 导出代码，**尚未交付任何 Provider Protocol**。
> 这是 ADR-0017 的决定，不是遗漏。进度见 PROJECT_STATUS.md。

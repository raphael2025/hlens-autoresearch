# schemas/v1 — 契约 Schema v1 只读快照

35 份 v1 JSON Schema，取自 commit `066b22d`（契约 `schema_version = 1.0.0`）。

| 项 | 说明 |
|---|---|
| 用途 | [ADR-0008](../../docs/adr/0008-contract-payload-immutability.md) §6 与 [ADR-0009](../../docs/adr/0009-experiment-identity-binding.md) §7 要求的 v1 只读保留路径（至少一个 major） |
| 当前 Schema | `schemas/*.schema.json`（`schema_version = 2.0.0`），由 `python -m core.contracts.registry` 导出 |
| 可执行只读入口 | `core/compat/v1.py`（`read_v1`），配合 `tests/vectors/v1/` 的固定载荷与旧哈希向量 |

**这些文件只读、只增不改。** 当前 Schema 的导出只写 `schemas/` 顶层，不会写入本目录；
不得用 v2 导出覆盖它们，也不得"顺手修正"其中的字段（H6）。

v1 与 v2 的 `content_hash` / `experiment_hash` **不可比较**：v2 的实验身份覆盖面更宽，
且 `ValidationProfile.status` 已退出哈希载荷。按 v1 读取旧记录**不会**让它取得 v2 的
登记或晋升资格。

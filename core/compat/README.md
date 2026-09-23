# core/compat

历史契约 major 的**只读**读取入口（10-migration.md §3.1）。

| 模块 | 内容 |
|---|---|
| `v1.py` | `read_v1()` + `LegacyV1Record`；v1 Schema 快照在 `schemas/v1/`，固定载荷与旧哈希向量在 `tests/vectors/v1/` |

这**不是**迁移服务：不按新算法重算旧哈希、不补造缺失的绑定、不让旧记录取得当前 major 的
登记或晋升资格。`LegacyV1Record` 刻意不是 `Contract` 子类。未知 major 一律拒绝。

读取前先过**顶层 shape gate**（ADR-0010 §D-15）：用已提交的 `schemas/v1/<Model>.schema.json`
检查 `required` 齐全、未知顶层字段被拒，并要求 `schema_version` 是 v1 已发布语法的
`1.x.y[-prerelease]`。快照缺失时 **fail closed**。
这**不是完整的 JSON Schema 递归校验**：不校验嵌套结构、类型与取值，也不引入 `jsonschema` 依赖。

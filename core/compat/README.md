# core/compat

历史契约 major 的**只读**读取入口（10-migration.md §3.1）。

| 模块 | 内容 |
|---|---|
| `v1.py` | `read_v1()` + `LegacyV1Record`；v1 Schema 快照在 `schemas/v1/`，固定载荷与旧哈希向量在 `tests/vectors/v1/` |

这**不是**迁移服务：不按新算法重算旧哈希、不补造缺失的绑定、不让旧记录取得当前 major 的
登记或晋升资格。`LegacyV1Record` 刻意不是 `Contract` 子类。未知 major 一律拒绝。

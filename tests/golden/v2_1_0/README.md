# v2.1.0 知识库固定载荷与哈希（ADR-0055 前置）

本目录是**契约 Schema 2.1.0（`CONTRACT_SCHEMA_VERSION = 2.1.0`）知识契约载荷的固定快照**，在任何 ADR-0055
契约改动之前（commit `1cd3284`）生成，用来证明：契约升到 2.2.0（`KnowledgeItem.tags` / `assets`、
`KnowledgeQuery.tags_all` / `assets_any`）之后，这些 2.1.0 记录仍按**记录时的版本**读取、校验，且
`content_hash` / `query_hash` / `result_hash` 逐位不变。

| 文件 | 模型 | 说明 |
|---|---|---|
| `knowledge_item.json` | `KnowledgeItem` | 带 conditions / links / 非默认状态 |
| `knowledge_item_minimal.json` | `KnowledgeItem` | 只有必填字段 |
| `knowledge_query.json` | `KnowledgeQuery` | 带 terms / name_prefix / evidence_at_least / statuses / limit |
| `knowledge_query_default.json` | `KnowledgeQuery` | 全部缺省 |
| `knowledge_result.json` | `KnowledgeResult` | 上面的查询 + 两个条目；另记 `result_hash` |
| `knowledge_seed_hashes.json` | — | 2.1.0 代码所服务的仓库种子条目 `content_hash`，及全量检索（`limit=1000`）的 `query_hash` / `result_hash` |

键同 `tests/golden/v2_0_0/README.md`（`contract_schema_version`、`model`、`generated_at_commit`、`content_hash`、
`payload`）。条目内容是测试夹具，不是真实引用。

生成方式：一次性脚本，不随仓库提交；时间输入全部固定。**不得重新生成或"修正"这些文件**（H6）；
测试：`tests/test_v2_1_0_knowledge_golden.py`。

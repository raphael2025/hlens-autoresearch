# plugins/knowledge

`KnowledgeProvider` 的本地实现与经审阅的写入路径（Phase 0.5，[ADR-0034](../../docs/adr/0034-knowledge-provider.md)；
[knowledge-base.md](../../docs/research/knowledge-base.md)）。

> 实施说明：写入路径 CODE_COMPLETE / DEBUG_PENDING（无契约 / Schema 变化）。

## 组成

| 模块 | 内容 |
|---|---|
| `local.py` | `LocalKnowledgeProvider`：离线、确定性检索 `docs/research/knowledge/*.json`；`load_items` 加载时缺出处 / 许可、重复、非法条目 fail closed |
| `store.py` | `LocalKnowledgeStore(items_dir).add(item, reviewed_by=...)`：唯一的写入入口 |
| `cli.py` | `uv run python -m plugins.knowledge.cli add --reviewed-by NAME [--items-dir DIR] FILE.json`；`... verify [--items-dir DIR]` |

## 写入规则

- 按加载器同样的规则校验 `KnowledgeItem`（未知字段拒绝）；缺出处或许可拒绝；审阅人必须非空。
- `KnowledgeItem` 没有"由 LLM 提取"字段（且禁止额外字段），无法区分来源，因此**每一次**写入都要求审阅人。
- 同一 `(name, version)` 内容不同 → 拒绝（新主张要发新版本）；内容相同 → 幂等、不写入、保留首次审阅记录。
- 目录现状必须能被干净加载，否则拒绝写入。
- 文件：`item-<name>-<version>.json`（加载器格式）+ 只写一次的 `item-<name>-<version>.review`（审阅人、时间、条目
  `content_hash`、文件 SHA-256）；临时文件 → `fsync` → `os.link`（不覆盖）→ 目录 `fsync`。先写审阅记录，崩溃只会留下
  "有审阅无条目"，不会留下可检索的未审阅条目；同一条目再次写入即可补全。`review_of` / `verify` 重新核验。
- CLI 的批量写入先全部检查再写，非法批次不写任何文件。HTTP API 保持只读（[ADR-0048](../../docs/adr/0048-api-and-web-console.md)）。

## 限制

- 检索仍是关键词匹配；知识条目是待检验主张，不作验证证据。
- 审阅人只是自报的名字，没有身份认证或签名。
- 没有删除 / 撤回路径（只追加）；更正须发新版本。
- 没有从 LLM 输出自动生成条目的流程；由 LLM 提取的条目仍需人工整理成 JSON 后经本路径写入。

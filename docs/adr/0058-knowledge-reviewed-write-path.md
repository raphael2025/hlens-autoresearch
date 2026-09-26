# ADR-0058：知识库经人工审阅的写入路径（Phase 0.5）

| 字段 | 值 |
|---|---|
| 状态 | **Accepted**（2026-09-26） |
| 日期 | 2026-09-26 |
| 决策者 | Claude Code（Opus），依 Raphael 2026-09-26 明确授权（"所有的决策都由你来决定，包括红线的事情"） |
| 相关 Phase | Phase 0.5 Public Knowledge Base |
| 影响范围 | `plugins/knowledge/`（新增 `store.py`、`cli.py`；`local.py` 的 `_load` 公开为 `load_items`，行为不变）；无契约 / Schema 变化，无新 Protocol |
| 是否破坏兼容 | 否 |
| 实施状态 | CODE_COMPLETE / DEBUG_PENDING |

## 背景

ADR-0034 只规定检索（`KnowledgeProvider.search`）；知识条目由人手工提交 `docs/research/knowledge/*.json`。roadmap 0.5 与
knowledge-base.md 要求"LLM 提取的条目须人工审阅后才能入库"，但仓库没有任何受控的写入路径。CLAUDE.md §5 要求新插件能力先有 ADR（P05-WRITE）。

## 裁决

1. 唯一写入入口 `LocalKnowledgeStore(items_dir).add(item, reviewed_by=...)`（Python API）与其 CLI（`add` / `verify`）；**不**增加任何 HTTP 写端点（ADR-0048 只读不变），
   **不**增加新 Protocol，**不**改 `KnowledgeItem` 契约。
2. 每一次写入都要求非空审阅人（契约没有"来源为 LLM"字段，无法区分，故一律要求审阅）；审阅记录只写一次，先于条目写入——
   崩溃只会留下"有审阅无条目"，永远不会出现可被检索的未审阅条目。
3. 只追加：同 `(name, version)` 内容不同即拒绝（新主张发新版本），内容相同幂等；从不修改或删除既有文件；目录必须能被现有加载器干净加载才允许写入。
4. 条目仍是待检验主张，不得作为验证证据（ADR-0034 §4 不变）。

## 后果

- 正面：人工审阅成为可核验的记录（审阅人、时间、条目 `content_hash`、文件 SHA-256），`verify` 可重新核对。
- 负面：审阅人只是自报的名字，没有认证 / 签名（需要以后的认证审批通道）；CLI 默认目录是仓库内 `docs/research/knowledge`。
- 协调：Phase 0.5 的其余工作（标签 / 资产 / 状态检索，ADR-0055 草案）在集成会话的 `wip/phase-0.5-knowledge` 上；本 ADR 只覆盖写入路径。

## 测试

`tests/plugins/knowledge/test_knowledge_store.py`、`test_provider_queries.py`（只写 tmp 目录）。

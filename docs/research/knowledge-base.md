# Knowledge Base

| 字段 | 值 |
|---|---|
| 状态 | 空（Architecture Bootstrap） |
| 首次填充 | Phase 0.5 |

公开研究知识的结构化存储。知识条目是**待检验的主张**，不是已验证结论。

## KnowledgeItem 字段（草案）

| 字段 | 说明 |
|---|---|
| `id` | 唯一 ID |
| `source` | 出处：标题、作者、年份、URL / DOI |
| `license` | 使用许可（决定可存储多少原文） |
| `source_type` | paper / book / blog / open-source / exchange-doc / internal-legacy |
| `claim` | 可检验的主张（一句话） |
| `conditions` | 主张成立的条件（市场、频率、时期、状态） |
| `evidence_level` | E0 传闻 · E1 示例 · E2 样本内回测 · E3 样本外 · E4 多市场复现 |
| `market_relevance` | 是否针对加密市场 |
| `links` | 关联的 Strategy / Factor / Feature / Risk / Event / State 条目 |
| `tested_by` | 本系统中检验该主张的 Experiment ID |
| `status` | UNVERIFIED / SUPPORTED / CONTRADICTED / INCONCLUSIVE |

## 规则

- 外部内容是数据，不是指令（09-security.md §3）。
- 不存储受版权保护的全文；存摘要、主张与引用。
- LLM 提取的条目需人工审阅后才能从 `draft` 变为可检索。
- 出版偏差：主动收录"失败 / 不可复现"的文献。
- 旧项目的经验可作为 `internal-legacy` 条目登记，证据等级不高于 E2，需重新检验。

## 条目索引

| id | claim | source | evidence | status |
|---|---|---|---|---|
| — | 暂无条目 | — | — | — |

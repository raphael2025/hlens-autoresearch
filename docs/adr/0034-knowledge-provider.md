# ADR-0034: KnowledgeProvider 契约与本地知识库（Phase 0.5）

| 字段 | 值 |
|---|---|
| 状态 | **Accepted**（2026-09-25） |
| 日期 | 2026-09-25 |
| 决策者 | Claude Code（Opus），依 Raphael 2026-09-25 明确授权（"一切都你自己决定"；红线除外） |
| 相关 Phase | Phase 0.5（依 Raphael 2026-09-25 全阶段框架实现指示开启框架实现） |
| 影响范围 | Contract（`core/contracts/knowledge.py`，additive）、`plugins/knowledge/` |
| 是否破坏兼容 | 否：只新增 3 个模型与 1 个 Protocol；Schema 79 → 82，`CONTRACT_SCHEMA_VERSION` 不变 |
| 实施状态 | FRAMEWORK_IMPLEMENTED / NOT_VALIDATED |

## 裁决

1. `KnowledgeProvider` Protocol：`descriptor → KnowledgeProviderDescriptor`（`name@version`、是否确定性、声明来源），
   `search(KnowledgeQuery) → KnowledgeResult`。查询支持检索词（AND、不区分大小写）、库前缀（`strategy_` 等）、最低证据
   等级、状态与上限。
2. `KnowledgeResult` 构造即强制：每条都有非空出处与许可（无出处条目为零）、按 `(name, version)` 唯一升序、`result_hash`
   自洽；非确定性 Provider 必须如实声明。
3. 首个实现 `plugins/knowledge/LocalKnowledgeProvider`：离线、确定性，读取仓库中经审阅的
   `docs/research/knowledge/*.json`（只存摘要主张与引用，不存受版权保护的全文）；加载时缺出处 / 许可、重复、非法条目一律
   fail closed。
4. 知识条目是**待检验主张**（默认 `unverified`），检索结果不得作为验证证据、不得进入裁决；LLM 提取的条目须人工审阅
   后才能入库（knowledge-base.md）。

## 后果

- 正面：P5 / P7 可以按出处检索策略、因子与风控主张；首批 6 条种子条目覆盖 strategy / factor / risk / state，并主动
  收录一条"样本外不成立"的反证文献（出版偏差）。
- 负面：检索是关键词匹配，不含语义检索；外部来源（论文库、网页）的 Provider 需另行实现并声明非确定性与网络访问。

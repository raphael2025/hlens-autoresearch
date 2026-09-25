# Knowledge Base

| 字段 | 值 |
|---|---|
| 状态 | 首批 6 条种子条目（Phase 0.5 框架实现，ADR-0034；FRAMEWORK_IMPLEMENTED / NOT_VALIDATED） |
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
| strategy_time_series_momentum | 过去 12 个月超额收益正向预测下月超额收益（多类期货） | Moskowitz, Ooi, Pedersen (2012), JFE | E4 | unverified |
| strategy_crypto_time_series_momentum | 加密货币周度收益存在时间序列动量 | Liu, Tsyvinski (2021), RFS | E2 | unverified |
| factor_crypto_market_size_momentum | 市场、规模、动量三因子解释加密货币横截面 | Liu, Tsyvinski, Wu (2022), JF | E3 | unverified |
| risk_volatility_managed_portfolios | 按近期已实现方差反向缩放暴露提高夏普 | Moreira, Muir (2017), JF | E3 | unverified |
| risk_volatility_managed_portfolios_out_of_sample | 可实施的样本外波动率管理策略并不系统性跑赢（反证） | Cederburg et al. (2020), JFE | E3 | unverified |
| state_cross_exchange_price_deviations | 同一加密货币跨交易所存在持续的价格偏离，跨国大于境内 | Makarov, Schoar (2020), JFE | E2 | unverified |

条目以 JSON 存于 `docs/research/knowledge/`，由 `plugins/knowledge/LocalKnowledgeProvider` 检索（ADR-0034）。

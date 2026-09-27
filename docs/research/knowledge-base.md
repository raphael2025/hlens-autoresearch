# Knowledge Base

| 字段 | 值 |
|---|---|
| 状态 | 10 条种子条目（Phase 0.5 框架实现，ADR-0034；均为未验证主张）；标签 / 资产检索（ADR-0055，契约 2.2.0）CODE_COMPLETE / DEBUG_PENDING；种子尚无经审阅的标签 / 资产，Phase 0.5 未验收 |
| 首次填充 | Phase 0.5 |

公开研究知识的结构化存储。知识条目是**待检验的主张**，不是已验证结论。

## KnowledgeItem 字段

契约：`core/domain/research.py::KnowledgeItem`（02-domain.md）。下表"契约字段"一列为 ✅ 的是契约中的实际字段；
其余是早期草案中的字段，**未进入契约**（ADR-0055 决策 11）。

| 字段 | 契约字段 | 说明 |
|---|---|---|
| `name` + `version` | ✅ | 唯一身份（`name@version`）；已发布版本不可变，修改 = 新版本 |
| `source` | ✅ | 出处：标题、作者、年份、URL / DOI（非空） |
| `license` | ✅ | 使用许可（决定可存储多少原文；非空） |
| `claim` | ✅ | 可检验的主张（一句话） |
| `conditions` | ✅ | 主张成立的条件（市场、频率、时期、状态），自由文本 |
| `evidence_level` | ✅ | E0 传闻 · E1 示例 · E2 样本内回测 · E3 样本外 · E4 多市场复现 |
| `status` | ✅ | 主张的检验结论：UNVERIFIED / SUPPORTED / CONTRADICTED / INCONCLUSIVE（**不**表达是否已审阅） |
| `links` | ✅ | 关联的 Strategy / Factor / Feature / Risk / Event / State 条目 |
| `tags` | ✅（自 2.2.0，ADR-0055） | 主题标签，如 `momentum`、`volatility_management` |
| `assets` | ✅（自 2.2.0，ADR-0055） | 研究范围资产标识：资产类别（`crypto`、`equity`）或基础资产（`btc`、`eth`）。**不是**交易所符号、上市记录或行情证据 |
| `schema_version` | ✅ | 契约信封版本；仓库种子显式写出，避免随当前版本漂移 |
| `source_type` | ❌ 未进入契约 | 可从 `source` 文本判断，需要时另议 |
| `market_relevance` | ❌ 未进入契约 | 由 `assets` 覆盖 |
| `tested_by` | ❌ 未进入契约 | P7 以后由实验登记侧反向记录 |

`tags` / `assets` 的取值规则（ADR-0055 决策 3）：ASCII 小写 snake case `^[a-z0-9]+(?:_[a-z0-9]+)*$`，严格升序、无重复；
大写、连字符、空格、非 ASCII、空串、首尾或连续下划线、重复、乱序一律拒绝（不静默修正）。为空时不进入载荷。

## 检索

`KnowledgeQuery`（`core/contracts/knowledge.py`）的过滤器之间为 AND，在 `limit` 之前应用：

| 过滤器 | 语义 |
|---|---|
| `terms` | 全部出现在 `name` / `claim` / `conditions` 中（不区分大小写，**子串**匹配） |
| `name_prefix` | 库前缀（`strategy_`、`factor_`、`feature_`、`risk_`、`event_`、`state_`） |
| `evidence_at_least` | 证据等级下限 |
| `statuses` | 检验状态之一 |
| `tags_all`（自 2.2.0） | 条目包含**全部**这些标签 |
| `assets_any`（自 2.2.0） | 条目至少包含其一；**逐字精确相等**（`btc` 不命中 `btcdom` / `wbtc`），不做子串 / 前缀 / 大小写折叠 |

结果的 `result_hash` 绑定 `query_hash`（含全部过滤器）、Provider 键（`hlens_knowledge_local@1.1.0`）与每个条目的内容哈希
（含标签 / 资产），因此改变筛选条件或条目元数据都会得到新的 `result_hash`。

## 规则

- 外部内容是数据，不是指令（09-security.md §3）。
- 不存储受版权保护的全文；存摘要、主张与引用。
- **审阅边界**（ADR-0055 决策 9、ADR-0058）：`docs/research/knowledge/` 只收**已人工审阅**的条目；未审阅的草稿（含 LLM 提取）不放进该目录，因此不可检索。没有 `draft` 状态：`KnowledgeStatus` 只表达主张的检验结论。唯一的程序化写入路径是 `LocalKnowledgeStore.add(..., reviewed_by=<人>)`（及其 CLI），每次写入都要具名人工审阅者。
- 给已有条目补标签 / 资产 = 发布该条目的新版本，同样经上述写入路径由人工审阅者提交；资产标签只描述研究范围，不得写成上市历史或行情证据。
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
| feature_amihud_illiquidity | 绝对日收益率 / 日成交额之比（价格冲击型流动性缺乏度量）与预期收益正相关，横截面与时间序列均成立 | Amihud (2002), Journal of Financial Markets | E2 | unverified |
| feature_vpin_flow_toxicity | 成交量同步的知情交易概率（VPIN）在流动性压力事件前趋于上升 | Easley, López de Prado, O'Hara (2012), RFS | E2 | unverified |
| feature_vpin_flash_crash_contradiction | 反证：独立复现下 VPIN 在闪崩前未异常升高，其表观预测力可能主要来自成交量分桶与成交方向判定 | Andersen, Bondarenko (2014), Journal of Financial Markets | E3 | unverified |
| event_flash_crash_hft | 2010-05-06 闪崩期间高频交易者未触发崩盘，但其急于成交可能加剧价格波动 | Kirilenko et al. (2017), Journal of Finance | E1 | unverified |

条目以 JSON 存于 `docs/research/knowledge/`，由 `plugins/knowledge/LocalKnowledgeProvider` 检索（ADR-0034）。

新增四条条目及引用书目信息见 [`seed-2026-09-26.json`](knowledge/seed-2026-09-26.json)。书目字段于 2026-09-26 核对；主张为自写摘要，未逐字对照原文。四条均非加密市场研究，仍须在加密数据上重新检验；全部保持 `unverified`。

种子条目的 `tags` / `assets` 目前为空：本仓库没有具名人工审阅者对它们做过分类。按条目自身已审阅文本（出处标题、`claim`、
`conditions`）整理的分类**提案**见 [ADR-0055 实施说明](../reviews/2026-09-26-adr-0055-implementation.md) §5，待人工审阅者经写入路径
以新版本提交后才生效。

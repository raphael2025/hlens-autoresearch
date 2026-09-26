# ADR-0055: 知识条目的标签与资产检索（Phase 0.5 验收缺口）

| 字段 | 值 |
|---|---|
| 状态 | **Accepted（2026-09-26，Codex 基于组合代码 `c08c589` 与其最终全量门禁复核接受）**；能力方向由 Codex 依 Raphael 授权决定（见"决定来源"），本文为按该决定修订后的实施文本（Amendment 1）；ADR / 代码接受**不等于** Phase 0.5 整体验收：仓库种子仍无具名人工审阅的标签 / 资产分类（只有实施说明 §5 的提案），Phase 0.5 验收仍待 Codex / Raphael |
| 日期 | 2026-09-26（初稿）；2026-09-26（Amendment 1：版本边界、依赖下限、验证矩阵） |
| 决策者 | Codex（Raphael 2026-09-23 授权的技术协调者）：能力方向决定与 2026-09-26 的接受（组合代码 `c08c589` 与最终全量门禁复核） |
| 起草者 | Claude Code（Opus）：初稿（Phase 0.5 核验批次，`wip/phase-0.5-knowledge` `ff6f67a`）与 Amendment 1（本分支） |
| 相关 Phase | Phase 0.5 |
| 影响范围 | Contract（`core/domain/research.py::KnowledgeItem`、`core/contracts/knowledge.py::KnowledgeQuery` / `KnowledgeResult`，只追加）；契约版本 **2.2.0**（minor）；`plugins/knowledge/`；`pyproject.toml` 的 Pydantic 声明下限 |
| 是否破坏兼容 | 否（minor）：2.0.0 / 2.1.0 载荷按记录版本读取、哈希逐位不变；**当前版本新建对象的信封变为 2.2.0，其内容哈希与 2.1.0 孪生对象不同，这是预期变化**（见决策 6） |

## 决定来源

Codex 决策记录 `docs/reviews/2026-09-26-adr-0055-codex-decision.md`（分支 `codex/full-code-review-2026-09-26`，
复核 HEAD `70e4034`）：**接受能力方向**——经审阅的知识条目可带主题标签与规范资产标识，查询可按"全部标签（AND）"
与"任一资产（OR，精确匹配）"筛选；`KnowledgeStatus` 只表达主张的证据状态；保留"只收已审阅条目"的边界，
不新增知识写入 API，本地知识集不收未经审阅的 LLM 内容。**不接受**初稿的版本与依赖表述（初稿写"契约保持 2.0.0、
空值不改哈希即兼容"，与已发布的 2.1.0 及信封参与哈希的规则不符），要求先修订本 ADR 再改契约。本 Amendment 1
逐条落实该决定，不改变其方向。

## 背景（Context）

roadmap Phase 0.5 验收标准要求"**可按标签 / 状态 / 资产检索**"。对照 ADR-0034 的现有契约：

| 维度 | 现状 | 结论 |
|---|---|---|
| 状态 | `KnowledgeQuery.statuses` 过滤 `KnowledgeItem.status` | 已满足 |
| 标签 | 只有 `name_prefix`（库前缀 `strategy_` / `factor_` …）；`KnowledgeItem` 没有标签字段 | **缺口**：只能按"库"筛，不能按主题标签（如 `momentum`、`volatility_management`）筛 |
| 资产 | `KnowledgeItem` 没有资产 / 市场字段；只能用 `terms` 在 `conditions` 自由文本里做子串匹配 | **缺口**：子串匹配会误中（`btc` 命中 `btcdom`）、也会漏掉措辞不同的条目 |

另有两处文档与契约不一致：

- `knowledge-base.md` 的字段草案列有 `source_type`、`market_relevance`、`tested_by`，契约里没有；
- `knowledge-base.md` 写"LLM 提取的条目需人工审阅后才能从 `draft` 变为可检索"，但 `KnowledgeStatus` 没有 `draft`。
  `KnowledgeStatus` 表达的是**主张的检验结论**（unverified / supported / contradicted / inconclusive），
  把"是否已审阅"塞进同一个枚举会混淆两件事。

版本事实（Amendment 1 纠正初稿）：当前 `CONTRACT_SCHEMA_VERSION = 2.1.0`，`02-domain.md` §3.3 把 2.0.0 与 2.1.0
都列为**已发布**；`schema_version` 参与内容哈希，因此"新字段为空时不进 `model_dump`"只能保证**同一信封版本**下的
载荷形状不变，不能让一个 2.2.0 对象与旧 2.1.0 对象哈希相同。`pyproject.toml` 声明 `pydantic>=2.9`，而
`Field(exclude_if=...)` 需要 Pydantic ≥ 2.12；锁文件解析到 2.13.5 只证明本机环境，不使声明下限成立。

## 决策（Decision）

1. **`KnowledgeItem` 追加两个字段**（自 2.2.0）：
   - `tags: tuple[KnowledgeToken, ...] = ()`：主题标签；
   - `assets: tuple[KnowledgeToken, ...] = ()`：适用资产 / 市场的研究范围标识。
2. **`KnowledgeQuery` 追加两个过滤器**（自 2.2.0；空 = 不限）：
   - `tags_all`：条目的 `tags` 须**全部**包含（AND，集合包含）；
   - `assets_any`：条目的 `assets` 须至少包含其一（OR），**逐字精确相等**，不做子串 / 前缀 / 大小写折叠。
   与既有过滤器（`terms`、`name_prefix`、`evidence_at_least`、`statuses`）是 AND 关系，在 `limit` 截断之前应用。
3. **规范取值（非规范值一律拒绝，fail closed）**：`KnowledgeToken` 为 ASCII 小写 snake case
   `^[a-z0-9]+(?:_[a-z0-9]+)*$`（初稿 `[a-z0-9_]+` 的子集：不允许空串、首尾或连续下划线）；四个字段的元组都必须
   **严格升序且无重复**——不静默排序、不静默去重，重复或乱序即拒绝，保证一个集合只有一种载荷与一个哈希。
   首尾空白按全项目规则先去除（`02-domain.md` §3.7，Schema 弱于运行时的既有已知边界），其余大写、连字符、空格、
   非 ASCII 一律拒绝。
4. **资产的语义边界**：`assets` 是研究范围标识——资产类别（如 `crypto`、`equity`、`commodity`）或基础资产
   （如 `btc`、`eth`），**不是**交易所符号、上市记录或行情证据。它不映射 `Instrument` / universe，不说明任何标的
   在何时可交易（上市历史见 ADR-0029 / ADR-0051，后者仍暂缓），也不授权任何网络请求。刻意不用交易所符号（如
   `BTCUSDT`）：同一资产在不同场所 / 计价下写法不同，且会被误读为 Binance 上市事实。
5. **存在性与省略**：四个字段用 `Field(default=(), exclude_if=omit_empty)`——空元组时不进入 `model_dump`（嵌套
   dump 同样省略），因此**信封版本不变时**载荷形状与哈希与没有这些字段时逐位相同。字段"存在"= 出现在载荷中
   （`Contract._field_in_payload`）。
6. **版本（Codex 决定第 1、3 条）**：
   - `CONTRACT_SCHEMA_VERSION = 2.2.0`；`PUBLISHED_CONTRACT_SCHEMA_VERSIONS = ("2.0.0", "2.1.0", "2.2.0")`；
   - 四个字段登记在 `KnowledgeItem._FIELDS_SINCE` / `KnowledgeQuery._FIELDS_SINCE`（`ADR_0055_VERSION = "2.2.0"`）：
     2.0.0 / 2.1.0 信封携带任一非空新字段即拒绝；
   - `KnowledgeResult` 自身不加字段，但**嵌套**条目带 `tags` / `assets` 时，结果信封必须 ≥ 2.2.0（2.1.0 结果中出现
     2.2.0 内容同样 fail closed）；
   - 2.0.0 / 2.1.0 载荷按记录版本读取，信封不被改写，`content_hash` / `query_hash` / `result_hash` 逐位复现
     （`tests/golden/v2_1_0/`，在任何本 ADR 代码改动之前于 `1cd3284` 生成）；
   - 当前代码新建、未显式给出信封的**所有**契约对象取 2.2.0，其内容哈希与 2.1.0 孪生对象不同——**这是预期变化**，
     不得描述为"哈希不变"。持久化 Phase 1 数据的按记录版本重放机制（ADR-0052）与 `PHASE1_PUBLICATION_VERSION`
     不变；新写入组按 2.2.0 写入；
   - 仓库的已审阅种子条目显式写出 `schema_version: "2.1.0"`（它们此前省略信封、随当前版本漂移）：钉住后其
     `content_hash` 与 2.1.0 代码所服务的逐位相同，不再随以后的 minor 变化。
7. **`result_hash` 的绑定**（不改公式）：`result_hash = H(query_hash, provider, [item.content_hash()])`。
   `query_hash` 覆盖新过滤器，条目哈希覆盖新元数据，因此改变筛选条件或条目的标签 / 资产都会改变 `result_hash`。
8. **Provider**：`hlens_knowledge_local` 升为 `1.1.0`（新增过滤能力；对 2.2.0 之前可表达的任何查询，返回的条目
   集合不变）。provider 键进入 `result_hash`，因此同一查询在 1.1.0 下的 `result_hash` 与 1.0.0 记录不同，
   已记录的 1.0.0 结果仍按其载荷校验与复现。
9. **审阅状态不进 `KnowledgeStatus`**：仓库 `docs/research/knowledge/*.json` 只收**已人工审阅**的条目；未审阅的草稿
   （含 LLM 提取）不放进该目录、不被 `LocalKnowledgeProvider` 加载；唯一的程序化写入路径是 ADR-0058 的
   `LocalKnowledgeStore.add(..., reviewed_by=<人>)`。`knowledge-base.md` 中"`draft`"一词改为描述这一目录边界。
   本 ADR **不**新增任何写入端点或写入 API。
10. **种子元数据**：只有带理由、经人工审阅的分类才可写入种子（Codex 决定第 5 条）。给已发布的 `name@version` 加
    标签 / 资产会改变其内容，按 `VersionedSpec` 规则必须发布新版本并经 ADR-0058 写入路径由具名人工审阅者提交；
    本 ADR 的实施**不**替人工审阅者写入分类，只附带有文本证据的分类提案（实施说明），且明确它们不是上市或行情证据。
11. `source_type`、`market_relevance`、`tested_by` 暂不进入契约：`market_relevance` 由 `assets` 覆盖；`tested_by`
    属于 P7 以后实验登记的反向引用，届时由实验侧记录；`source_type` 可从 `source` 文本判断，需要时另议。
    `knowledge-base.md` 的字段草案表相应标注"未进入契约"。
12. **依赖声明**：`pyproject.toml` 的 Pydantic 下限从 `>=2.9` 提高到 `>=2.12`（`exclude_if` 的最低支持版本；锁定
    版本 2.13.5 不变）。这同样修正 ADR-0052 / 0054 / 0057 已在使用 `exclude_if` 却未改声明的遗留问题。

## 验证矩阵（实施必须真实运行并通过）

| # | 要求 | 证据 |
|---|---|---|
| V1 | 2.1.0 固定载荷（条目 / 最小条目 / 查询 / 默认查询 / 结果）按记录版本读取，信封不改写，重新序列化逐字节相同，`content_hash` / `query_hash` / `result_hash` 等于生成时记录值 | `tests/golden/v2_1_0/` + `tests/test_v2_1_0_knowledge_golden.py`（fixture 在 `1cd3284` 生成，测试在改动前即通过） |
| V2 | 已审阅种子条目的内容哈希与 2.1.0 代码所服务的逐位相同；全量检索的 `query_hash` 在同一 2.1.0 查询下复现 | `knowledge_seed_hashes.json` |
| V3 | 2.2.0 带标签 / 资产的条目、带过滤器的查询哈希确定（重复构造、JSON 往返、不同构造顺序相同） | 定向测试 |
| V4 | `query_hash` 随任一过滤器变化；`result_hash` 随查询或条目元数据变化 | 定向测试 |
| V5 | 资产精确匹配：`btc` 不命中 `btcdom` / `wbtc`，大小写不同的写法被拒绝而非折叠 | 定向测试 + Provider 契约套件 |
| V6 | 非规范（大写、连字符、空格、非 ASCII、空串、首尾 / 连续下划线）、重复、乱序的标签 / 资产在条目与查询中都被拒绝 | 定向测试 |
| V7 | 2.0.0 / 2.1.0 信封携带非空新字段即拒绝；2.1.0 结果嵌套 2.2.0 元数据即拒绝；空值在保持信封版本时载荷形状不变 | 定向测试 |
| V8 | Schema 快照与 `CONTRACT_MODELS` 一致；版本注册（`CONTRACT_SCHEMA_VERSION`、`PUBLISHED_CONTRACT_SCHEMA_VERSIONS`、`_FIELDS_SINCE`）正确 | Schema 导出检查 + 版本测试 |
| V9 | 新版本信封导致的既有测试钉值变化逐一核实：只允许信封 `2.1.0 → 2.2.0` 的差异 | 全量非 PostgreSQL 门禁 |

## 备选方案（Alternatives）

| 方案 | 优点 | 缺点 | 为何未选 |
|---|---|---|---|
| A. 本方案（2.2.0 追加 `tags` / `assets` + 两个过滤器，空值省略，`_FIELDS_SINCE` 登记） | 满足验收标准；精确匹配；旧载荷按记录版本逐位复现 | 当前版本新对象的信封与哈希变化；种子需人工补元数据 | Codex 决定 |
| A'. 初稿：追加字段但保持 2.0.0 / 2.1.0 | 无信封变化 | 新字段伪装成已发布版本，旧读者无法识别；违反 ADR-0052 §4 | Codex 驳回 |
| B. 不改契约，约定在 `conditions` 里写 `tag:xxx` / `asset:xxx`，用 `terms` 检索 | 无契约变更 | 子串匹配（`tag:mom` 命中 `tag:momentum`）；约定无法被契约强制 | 不能 fail closed |
| C. 暂不做，标记该验收项未满足 | 零改动 | Phase 0.5 验收无法完成 | 只作为不决定时的默认 |
| D. 在 `KnowledgeStatus` 增加 `draft` | 与 knowledge-base.md 字面一致 | 把"是否已审阅"与"主张检验结论"混在一个枚举 | 语义混淆；Codex 明确不采用 |
| E. 资产用交易所符号（`BTCUSDT`） | 与 Phase 1 标的名接近 | 场所 / 计价相关、易被读成上市事实、同一资产多种写法 | 见决策 4 |

## 后果（Consequences）

- 正面：Phase 0.5 "按标签 / 状态 / 资产检索"逐项有测试；资产过滤精确、fail closed。
- 负面 / 代价：契约 minor 2.2.0：所有当前版本新建对象的信封与哈希变化，依赖当前信封的测试钉值须逐一核实；
  Schema 快照的信封默认值随之更新；种子需人工审阅者经写入路径补标签 / 资产（新版本）。
- 需要迁移的内容：无。旧条目 / 查询 / 结果按记录版本读取；持久化 Phase 1 数据按记录版本重放（ADR-0052）。
- 对复现性的影响：2.0.0 / 2.1.0 记录的哈希逐位复现；2.2.0 对象是新身份；同一查询在 provider 1.1.0 下的
  `result_hash` 与 1.0.0 记录不同（provider 键不同），这是预期。

## 不决定时的默认

保持现状：只支持库前缀、证据等级与状态过滤；"按标签 / 资产检索"在 Phase 0.5 记录中标为**未满足**；不实施任何契约变更。

## 合规检查

- [x] 不破坏已冻结契约：minor 2.2.0，只追加；旧版本载荷按记录版本复现（V1 / V2）
- [x] 不修改 Validation Constitution 或任何 Profile 数值
- [x] Domain 层仍只依赖标准库与 Pydantic
- [x] Research / Application Plane 边界不变；不新增写入端点；不发起任何网络请求（D-LIST / ADR-0051 仍暂缓）
- [x] Codex 复核实施证据并接受（2026-09-26；代码冻结提交 `c08c589` 的全量非 PostgreSQL 门禁（`systemd-run --user --scope -q -p MemoryMax=5G -p MemorySwapMax=0 uv run pytest -q -m "not postgres" -p no:cacheprovider -rs`）→ `7179 passed, 136 deselected, 1 warning in 2906.24s (0:48:26)`，退出码 0；起止 SHA 均为 `c08c5895b4a60916715c2f88f65d061c40724b96`、dirty=0（2026-09-26T20:07:01Z → 20:55:29Z）；其后的 docs-only 提交**不在**该门禁覆盖范围内）

## 参考

- ADR-0034（KnowledgeProvider）、ADR-0052（版本化重放与 `_FIELDS_SINCE`）、ADR-0054 / 0057（2.1.0 追加字段先例）、
  ADR-0058（经审阅写入路径）
- `docs/architecture/02-domain.md` §3.3；`docs/research/knowledge-base.md`；roadmap Phase 0.5

## 接受记录（2026-09-26）

- 接受者：Codex（依 Raphael 2026-09-23 授权）；依据：独立实现复核（检查点 `origin/claude/adr-0055-tags-assets` = `ed8e694`）、
  组合分支冲突解决与 Phase 9 证据测试修正的复核，以及 代码冻结提交 `c08c589` 的全量非 PostgreSQL 门禁（`systemd-run --user --scope -q -p MemoryMax=5G -p MemorySwapMax=0 uv run pytest -q -m "not postgres" -p no:cacheprovider -rs`）→ `7179 passed, 136 deselected, 1 warning in 2906.24s (0:48:26)`，退出码 0；起止 SHA 均为 `c08c5895b4a60916715c2f88f65d061c40724b96`、dirty=0（2026-09-26T20:07:01Z → 20:55:29Z）；其后的 docs-only 提交**不在**该门禁覆盖范围内。
- 组合提交（基于全代码候选 `a5836b2`）：`48fe180`、`a8490bf`、`62256b6`、`2b2a6a5`、`1367dc8`、`c08c589`；以普通 fast-forward 推送到
  `wip/all-code-completion`。
- 范围：接受的是本 ADR（Amendment 1）与其实现；ADR / 代码接受**不等于** Phase 0.5 整体验收：仓库种子仍无具名人工审阅的标签 / 资产分类（只有实施说明 §5 的提案），Phase 0.5 验收仍待 Codex / Raphael。

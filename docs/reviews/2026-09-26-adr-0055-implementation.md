# ADR-0055 实施说明：知识标签 / 资产检索（契约 2.2.0）

| 字段 | 值 |
|---|---|
| 性质 | 实施记录；ADR-0055 **Accepted（2026-09-26，Codex 基于组合代码 `c08c589` 与其最终全量门禁复核接受）**。这是 ADR / 代码的接受，**不是** Phase 0.5 验收：种子仍无具名人工审阅的标签 / 资产分类 |
| 决定来源 | Codex 决策记录 `docs/reviews/2026-09-26-adr-0055-codex-decision.md`（分支 `codex/full-code-review-2026-09-26`，复核 HEAD `70e4034`），依 Raphael 授权 |
| 实施者 | Claude Code（Opus），只实现，不改变决定 |
| 分支 | 独立实现 `claude/adr-0055-tags-assets`（基于全代码 WIP `1cd3284`；未 cherry-pick 旧 `wip/phase-0.5-knowledge`），经 Codex 批准推送为检查点 `origin/claude/adr-0055-tags-assets` = `ed8e694`；组合分支 `claude/adr-0055-integration`（基于 `a5836b2`，§9），未 push |
| 状态 | CODE_COMPLETE / DEBUG_PENDING |

## 1. 做了什么

| 批次 | 内容 |
|---|---|
| 1 ADR 修订 | ADR-0055 Amendment 1：字段自 **2.2.0**（`_FIELDS_SINCE` + 已发布版本注册），不声称 2.0.0 / 2.1.0 内兼容；规范 token 语法；资产语义边界；Pydantic ≥ 2.12；验证矩阵 V1～V9。同时在任何代码改动前于 `1cd3284` 生成 2.1.0 知识固定载荷（`tests/golden/v2_1_0/`），其测试在未改动的基线上通过 |
| 2 契约 | `CONTRACT_SCHEMA_VERSION = 2.2.0`、`PUBLISHED_CONTRACT_SCHEMA_VERSIONS = ("2.0.0","2.1.0","2.2.0")`；`KnowledgeItem.tags` / `assets`、`KnowledgeQuery.tags_all` / `assets_any`（`omit_empty` 省略、`_FIELDS_SINCE = 2.2.0`、严格升序无重复）；`KnowledgeResult` 在 2.2.0 之前的信封中拒绝嵌套带标签 / 资产的条目 |
| 2 Provider | `hlens_knowledge_local@1.1.0`：`tags_all` 集合包含、`assets_any` 交集非空，逐字相等，在 `limit` 之前；契约套件新增 `check_tag_and_asset_filters`（含子串 / 前缀反例，能杀死子串匹配实现） |
| 2 种子 | `seed-2026-09-25.json` 六条条目显式写出 `schema_version: "2.1.0"`（此前省略、随当前版本漂移）：内容哈希与 2.1.0 代码所服务的逐位相同。**未**新增任何标签 / 资产（见 §5） |
| 2 依赖 | `pyproject.toml`：`pydantic>=2.12`；`uv.lock` 只有 `requires-dist` 说明符一行变化（`uv lock --offline`），解析版本 2.13.5 不变 |
| 2 Schema / API | 135 份 current Schema 重新导出：除知识三模型外只有信封默认值 `2.1.0 → 2.2.0`（325 行）；`apps/api/openapi.json` 重新导出；`apps/web/src/api.d.ts` 按 OpenAPI 手工同步（本机无 `node_modules`，未运行 `gen:api`） |
| 2 版本钉值 | 见 §3 |
| 2 控制台 fixture | 六种报告的当前 fixture 因信封变化成为 2.2.0 新文件；2.1.0 文件保留为第二代遗留 fixture（`LEGACY_2_1_0`），`regenerate_legacy("2.1.0")` 在新进程中逐字节重建 |
| 3 文档 | `02-domain.md` §3.3 / 头部 / 实体表；`knowledge-base.md`（字段表、检索、审阅边界，去掉 `draft`）；roadmap Phase 0.5 验收矩阵；ADR 索引；`apps/web/README.md`、`apps/web/fixtures/README.md`；本说明 |

## 2. 验证矩阵对照（ADR-0055）

| # | 要求 | 证据 |
|---|---|---|
| V1 | 2.1.0 条目 / 查询 / 结果逐位复现 | `tests/test_v2_1_0_knowledge_golden.py`（5 份固定载荷 + result_hash） |
| V2 | 种子哈希与全量检索 query / result 哈希复现 | 同上：种子 6 条哈希；以记录的 provider 键 `@1.0.0` 从今天的种子重建记录的 `result_hash` |
| V3 | 2.2.0 哈希确定 | `tests/test_adr_0055_versions.py::test_a_tagged_item_and_a_filtered_query_hash_deterministically` 等 |
| V4 | query / result 哈希绑定筛选与元数据 | `test_every_filter_changes_the_query_hash`、`test_the_result_hash_changes_with_the_query_filters`、`..._with_the_item_metadata`、`test_a_result_with_altered_metadata_does_not_keep_its_hash` |
| V5 | 资产精确匹配不做子串 | `tests/plugins/knowledge/test_tags_assets.py`（`btc` / `btcdom` / `wbtc`；与 `terms` 子串对照）；契约套件反例 `test_the_suite_catches_a_substring_matcher` |
| V6 | 非规范、重复、乱序被拒 | `test_an_item_with_a_non_canonical_token_is_refused`、`test_a_query_with_...`、`test_duplicate_or_unordered_tokens_are_refused_never_normalised`；加载层 `test_a_non_canonical_or_misversioned_item_file_fails_closed` |
| V7 | 版本边界；空值保持旧形状 | `test_an_older_item_envelope_carrying_a_2_2_0_field_is_refused`、`..._query_...`、`test_new_content_cannot_be_built_inside_a_2_1_0_replay_scope`、`test_a_2_1_0_result_nesting_2_2_0_metadata_is_refused`、`test_empty_new_fields_keep_the_2_1_0_payload_shape_and_hash`、`test_a_2_2_0_twin_of_a_2_1_0_object_has_the_same_shape_and_a_new_hash` |
| V8 | Schema 与版本注册 | `test_the_current_version_is_2_2_0_and_every_earlier_minor_stays_published`、`test_the_new_fields_are_declared_since_2_2_0_and_omitted_when_empty`、`test_the_committed_schema_declares_the_token_grammar`、`test_pydantic_is_declared_at_least_2_12_for_exclude_if`；`tests/test_contracts.py::test_committed_schemas_match_contracts` |
| V9 | 信封变化导致的钉值逐一核实 | §3；全量非 PostgreSQL 门禁（§4） |

## 3. 因 2.2.0 信封而改的既有测试（逐项核实，未削弱）

- **"当前版本是 X"绊线**（只断言版本号本身）：`test_adapter_contracts`、`test_universe_contracts`、`test_experiment_identity`（含 Schema 默认值）、`test_information_flow`、`test_construction_and_versioning`、`test_revision_contracts`、`tests/infrastructure/catalog/phase1_support.py` 的模块级断言：`2.1.0 → 2.2.0`。
- **2.1.0 内容的测试改为同时覆盖 2.1.0 与 2.2.0**（覆盖面扩大）：`test_adr_0054_0057_versions.py`（带 subject / 结转内容的对象在 2.1.0 作用域内是 2.1.0 且可往返，当前代码构造的是 2.2.0）；`test_adr_0052_exact_fields.py`（`CapacityParams` / `CrossAssetParams` 在 2.1.0 与 2.2.0 信封下都有效）。
- **按记录版本重放**：`tests/infrastructure/canonical/test_contract_version_replay.py`、`e2e/test_versioned_replay_first_slice.py`、`tools/test_dnet_versioned_replay.py` 由"2.0.0 数据在 2.1.0 下重放"改为对 **2.0.0 与 2.1.0 各跑一遍**、由 2.2.0 代码重放（参数化）；`event/test_event_iceberg.py` 的篡改版本用例加入 `2.1.0`。
- **当前对象的信封**：`test_adr_0052_sourcing`、`test_event_iceberg` 中"新对象 = 2.1.0"改为 `CONTRACT_SCHEMA_VERSION`。
- **2.1.0 下取的哈希钉值**（沿用 2.1.0 升级时的做法，逐项核实为只有信封变化）：
  - 能在测试内按版本重建的，改为在 `built_at("2.1.0")` 作用域内构造同一对象并核对**原 2.1.0 钉值**（未重钉）：
    `plugins/backtest/test_execution_model.py`、`test_carry_over.py`、`plugins/events/test_dsl.py`、`plugins/llm/test_scripted_store.py`、
    `test_event_subject.py`；按报告字节钉值的 `research/validation/test_multi_seed_controls.py`、`test_g4_check_isolation.py` 以
    `envelopes_at(…, "2.1.0")` 核对（与既有 2.0.0 做法相同，这些载荷不含随信封变化的内嵌哈希）；`test_adapter_contracts.py` 的
    ADR-0052 Schema 钉值以 `as_published_at(…, "2.1.0")` 核对，`KnowledgeItem` Schema 新增 ADR-0055 钉值。
  - 对象深藏在模块常量里、测试内无法按版本重建的 11 个测试（`test_multi_instrument_validation`、`test_market_benchmark`、
    `test_cross_sectional_g4`、`test_router_completion` ×3、`test_router_eligibility`、`test_gate_calibration_g5`、
    `test_gate_calibration_multi` ×2、`test_loop_e2e`）：**先**在新进程中于 `contract_schema_version_scope("2.1.0")` 内运行未修改的
    测试——`25 passed`（连同 `test_golden_experiments` 的 bit-exact 重跑；唯一失败是其"`python -m` 子进程重生成"用例，子进程不在作用域内，
    属预期）——证明原钉值仍精确描述 2.1.0 代码的输出；**再**按 2.2.0 重钉，注释中保留 2.1.0 值作为证据。
  - `tests/golden/experiments/`：记录 `fe69500f…` → `b81a3feb…`；唯一变化的输出是 `backtest.result_hash`（逐项比对），所有门值、阈值与
    结论不变；旧记录在 2.1.0 作用域内 bit-exact 复现（上条）。
- **Provider 键**：`test_loop_knowledge_source`、`test_knowledge_source`、`test_migration`：`hlens_knowledge_local@1.0.0 → @1.1.0`。
- **控制台 fixture**：`test_console_fixture_writers.py` 泛化为两代遗留（2.0.0、2.1.0）；`tests/apps/report_fixtures.py` 增 `LEGACY_2_1_0` / `LEGACY`；`tests/apps/test_console_fixtures.py` 计数与配对随之更新（每代 paper deviation 描述同代 router paper run）。web：`validationReport` / `gateCalibration` / `stateStrategyMatrix` / `routerPaperRun` 的 node 测试计数与版本同步，`KnowledgeSearch.test.tsx` 的条目补 `tags` / `assets`——**未运行**（无 `node_modules`）。

## 4. 检查命令与结果

见批次提交信息与 HANDOFF（原样输出）。本说明只记录命令：

```bash
uv run pytest -q tests/test_v2_1_0_knowledge_golden.py tests/test_adr_0055_versions.py tests/plugins/knowledge ...
systemd-run --user --scope -p MemoryMax=5G -p MemorySwapMax=0 uv run pytest -q -m "not postgres"
uv run ruff check . && uv run ruff format --check . && uv run mypy
uv run python -m core.contracts.registry   # 然后 git diff --stat schemas
```

### 4.1 web（控制台）

"未运行"只指**独立实现的隔离 worktree 中由 Claude 运行的部分**：该 worktree 没有 `apps/web/node_modules`，Claude 也未安装依赖，因此：

- Claude 在隔离 worktree 中只能运行不依赖 `node_modules` 的 lib 测试（node v26.8.1 原生剥离类型）：
  `node --test src/lib/knowledgeQuery.test.ts` → `tests 7 / pass 7 / fail 0`；
- 隔离 worktree 的全量门禁（`ed8e694`：7029 passed, **1 skipped**）中被跳过的那一项是控制台 live-backend smoke
  （`tests/apps/test_live_backend_smoke.py`：缺 `node_modules/esbuild` 时带原因跳过），不是失败；
- **Codex 独立运行**了 web 全套（2026-09-26，临时用已有依赖的 `node_modules` 符号链接，运行后已删除，未进入任何提交）：
  `npm test` → 103 项通过；`npm run build` → TypeScript 检查与 Vite 生产构建通过。这是 Codex 报告的结果。
- 组合分支 worktree（§9）有 `node_modules`（从全代码 worktree 本地复制，`package-lock.json` 逐字节相同，无网络、无包管理器安装，
  被 `.gitignore` 忽略）：Claude 在其中实际运行了 `npm run gen:api`（重新生成的 `src/api.d.ts` 与手工同步的提交**逐字节相同**）、
  `npm test`、`npm run build` 与 live-backend smoke（不再跳过），结果见 §9。

## 5. 种子标签 / 资产：分类提案（未写入，待人工审阅）

本仓库没有具名人工审阅者对种子做过分类；给已发布的 `name@1.0.0` 加元数据会改变其内容，按 `VersionedSpec` 规则须发布新版本
并经 ADR-0058 写入路径（`LocalKnowledgeStore.add(..., reviewed_by=<人>)`）提交。下表只依据每条条目**已审阅的文本**（出处标题、
`claim`、`conditions`）整理，供审阅者取舍；资产只描述研究范围，**不是**上市历史或行情证据（例如 Liu & Tsyvinski 的 BTC / ETH /
XRP 是论文样本，与 Binance 标的池无关）。

| 条目 | 提案 `tags` | 提案 `assets` | 文本依据 |
|---|---|---|---|
| `strategy_time_series_momentum` | `momentum`, `time_series_momentum` | `bond`, `commodity`, `currency`, `equity_index` | 标题 "Time series momentum"；conditions "liquid futures (equity index, currency, commodity, bond)" |
| `strategy_crypto_time_series_momentum` | `momentum`, `time_series_momentum` | `btc`, `crypto`, `eth`, `xrp` | claim "time-series momentum"；conditions "BTC, ETH, XRP market data" |
| `factor_crypto_market_size_momentum` | `factor_model`, `momentum`, `size` | `crypto` | claim "three-factor model of crypto market, size and momentum"；conditions "cross-section of coins" |
| `risk_volatility_managed_portfolios` | `volatility_management` | `equity` | 标题 "Volatility-managed portfolios"；conditions "equity factors" |
| `risk_volatility_managed_portfolios_out_of_sample` | `counter_evidence`, `volatility_management` | `equity` | claim 为反证（"do not systematically beat"）；conditions "many equity strategies" |
| `state_cross_exchange_price_deviations` | `arbitrage`, `cross_exchange` | `crypto` | 标题 "Trading and arbitrage in cryptocurrency markets"；conditions "multiple exchanges and countries" |

## 6. 需要集成会话同步的精确状态（独立实现批次未编辑共享文件；已在组合分支 §9 同步）

本批次按指示**未**编辑 `PROJECT_STATUS.md`、`PROJECT_MEMORY.md`、`docs/plans/2026-09-26-all-code-completion-plan.md`。集成时建议写入：

- `PROJECT_STATUS.md` §2 Phase 0.5 行：`ADR-0055（标签 / 资产检索，契约 2.2.0）CODE_COMPLETE / DEBUG_PENDING，待 Codex 复核；种子尚无经人工审阅的标签 / 资产`。
- `PROJECT_STATUS.md` §6 D-P05 行与 §8 第二条：把"ADR-0055（契约变更）待 Raphael"改为"能力方向已由 Codex 依 Raphael 授权决定（2026-09-26）；Amendment 1 与实施已完成，ADR 仍 Proposed，待 Codex 复核实施证据"。
- `PROJECT_STATUS.md` §7：契约当前为 **2.2.0**（ADR-0055，minor）；§10 第 3 条 Schema 计数不变（135 份），版本改为 2.2.0。
- `PROJECT_MEMORY.md` §2："当前为 2.1.0" → "ADR-0052 起 2.1.0，ADR-0055 起 **2.2.0**（minor；2.0.0 / 2.1.0 载荷按记录版本重放）"；current Schema 行"契约 2.1.0" → "契约 2.2.0"。
- `PROJECT_MEMORY.md` §5：ADR-0055 一句话——"知识标签 / 资产检索（`tags_all` AND、`assets_any` OR 精确），契约 2.2.0，资产是研究范围标识而非上市 / 行情证据；Codex 决定方向，Proposed 待复核"。
- 完成计划 §10：新增一批（ADR-0055）条目，引用本说明与各提交 SHA。
- 与 `wip/phase-0.5-knowledge` 合并时：该分支的 `seed-2026-09-26.json`（feature / event 种子）同样省略 `schema_version`，合入后应显式写出其服务时的版本；该分支的核验记录 `2026-09-26-phase05-knowledge-audit.md` 中"可按标签 / 资产检索 ⚠️ / ❌"两行应指向本说明；其旧 ADR-0055 草稿由本分支的 Amendment 1 取代。

## 7. 未解决 / 待决定

1. **ADR-0055 状态**：Accepted（2026-09-26，Codex 基于组合代码 `c08c589` 与其最终全量门禁复核接受）（§10）。
2. **种子元数据**：§5 提案待具名人工审阅者经写入路径提交（新版本）；在此之前，按标签 / 资产检索在真实种子上返回空。
3. **web**：Knowledge Search 已加标签 / 资产输入（Codex 复核要求的补充，§8）；隔离 worktree 中 node 全套由 Codex 独立运行通过；组合分支中 Claude 实际运行了 `gen:api` / `npm test` / `npm run build` / live smoke（§4.1、§9）。浏览器手工验收未做。
4. **token 语法与资产取向**（ADR 决策 3 / 4）是 Amendment 1 为落实"规范资产标识"而定的细节：小写 snake case、不用交易所符号。若 Codex 要求交易所符号形式，需再修订。
5. **Provider 版本**：`hlens_knowledge_local` 升为 1.1.0，同一查询的 `result_hash` 与 1.0.0 记录不同（provider 键参与哈希）；已记录的 1.0.0 结果按其载荷仍可校验与重建（V2）。

## 8. 补充：控制台 Knowledge Search 的标签 / 资产输入（Codex 复核要求）

- `apps/web/src/lib/knowledgeQuery.ts`：表单 → `KnowledgeQuery`。标签 / 资产以空格分隔，按后端同一规则检查（`KNOWLEDGE_TOKEN`
  = `^[a-z0-9]+(?:_[a-z0-9]+)*$`、严格升序、无重复）；**不**折叠大小写、不排序、不去重，不合法即返回原因、不发请求。
  只有关键词时请求体与改动前逐字相同（`{"terms":[...],"limit":50}`）。
- `apps/web/src/pages/KnowledgeSearch.tsx`：新增"标签（全部满足）""资产（任一满足，精确）"输入；不合法时 `role="alert"` 列出原因、
  不发请求；后端 422 以"数据不合法（HTTP 422）：…"显示；按标签 / 资产筛选为空时注明"可能只说明条目尚未分类（种子目前没有经人工
  审阅的标签 / 资产）"；结果条目显示其标签 / 资产（缺省时后端省略该键，页面按空处理）。
- 测试：`src/lib/knowledgeQuery.test.ts`（7 项）；`src/pages/KnowledgeSearch.test.tsx` 新增 4 个组件测试（提交的请求体、非法输入无请求
  且显示原因、筛选为空的未分类提示、后端 422）。
- 未新增任何种子分类。

## 9. 组合分支（Codex 复核后的整合，2026-09-26）

Codex 复核结论：接受独立实现与 2.2.0 版本方案进入整合；2.1.0 固定向量、2.2.0 精确匹配与哈希覆盖证据充分；ADR 在组合分支验证完成前
仍为 Proposed。

| 步骤 | 结果 |
|---|---|
| 检查点推送 | `origin/claude/adr-0055-tags-assets` = `ed8e69484c8b2f600e57167eb437f620242e036d`（普通非强制推送，新分支） |
| 组合分支 | `claude/adr-0055-integration`，从全代码候选 `a5836b2` 新建；历史不改写 |
| `8e1d653` → `48fe180` | 无冲突 |
| `e02898a` → `a8490bf` | 三处冲突，均保留两侧（下表） |
| 新增 `62256b6` | Phase 9 证据测试按记录版本核对（下文） |
| `ed8e694` → `2b2a6a5` | 无冲突（`apps/web/README.md` 自动合并） |

冲突解决（全部保留 B46 的报告种类与 2.0.0 遗留 fixture、B46 的 `VARIANTS` 变体机制，以及 ADR-0055 的 2.1.0 遗留 fixture 与 2.2.0 当前 fixture）：

| 文件 | 解决 |
|---|---|
| `tests/apps/report_fixtures.py` | `VARIANTS` / `variant_fixture` 与 `LEGACY_2_1_0` / `LEGACY` / `legacy_ids` 并存；`fixture()` 排除两代遗留与全部变体 |
| `tests/apps/test_console_fixtures.py` | 两侧导入并存；逐种类检查同时减去两代遗留与变体，并要求二者不相交 |
| `tests/research/reports/test_console_fixture_writers.py` | `VARIANT_WRITERS` 与 `LEGACY_WRITERS["2.0.0" / "2.1.0"]` 并存；`regenerate()` = 当前 + 变体 + 每代遗留；已提交文件集合 = 当前 + 变体 + 两代遗留 |

B46 的变体 `degradation_check/50f53688…` 在 2.2.0 下不变（生成器不写新文件）；`a5836b2` 未改 `core/` 与 `schemas/`，Schema / OpenAPI 重新导出无差异。

**Phase 9 证据（`dd6c8e1`，在原基线之后加入）**：`docs/research/calibration/` 的两份报告与 `INPUTS_HASHES` 钉值由 2.1.0 代码生成，
setup 的 `inputs_payload` 内嵌 Profile / spec 的信封与内容哈希，组合后 6 项失败（定向运行原样：`6 failed, 1710 passed, 1 skipped`）。
报告是证据，不重生成、不重钉：先在 `contract_schema_version_scope("2.1.0")` 内运行**未修改**的测试 → `11 passed`；再把测试改为在新进程中
按 2.1.0 构造 setup 核对钉值与报告 `inputs`，并新增"当前 setup 去掉信封及其上的哈希后与记录的相同"检查 → `22 passed`（含文档一致性）。

web（组合 worktree，`node_modules` 为本地复制，见 §4.1）：`npm run gen:api` → `src/api.d.ts` 无差异；`npm test` → lib `tests 87 / pass 87 / fail 0`、
组件 `tests 109 / pass 109 / fail 0`，exit 0；`npm run build` → `✓ built`，exit 0；`pytest tests/apps/test_live_backend_smoke.py` → `3 passed`（未跳过）。

组合分支的全量门禁（`1367dc8`，全部合并与文档提交之后、冻结的 HEAD）：`START_SHA 1367dc8899353c329a0f646780b14f57fed69cee dirty=0 2026-09-26T19:12:36Z` → `7176 passed, 136 deselected, 1 warning in 2921.41s (0:48:41)`，`EXIT 0`（无 skip：live smoke 实际运行）→ `END_SHA 1367dc8899353c329a0f646780b14f57fed69cee dirty=0 2026-09-26T20:01:20Z`。同一 HEAD：ruff / format（757 files）/ mypy（593 files）通过，`uv lock --check --offline` 通过，Schema 135 份与 OpenAPI、`gen:api` 重新导出均无差异，`npm test`（lib 87 / 87、组件 109 / 109）与 `npm run build` 通过。

Codex 复核（门禁运行期间提出，门禁结束后才改文件）：`_without_envelopes()` 删除所有 `*_hash` 字段，会漏检。核实：`detector.strategy_hash` 是 `StrategySpec.content_hash()`（`gate_calibration.py` 的 `describe()`），含信封，2.1.0 → 2.2.0 确实变化（`0526ac90…` → `32443744…`），因此不能原样保留比较；但弱点是真的：策略 spec 与候选 Profile 在载荷中只以 ref + 哈希出现，整类删除会放过它们的任何内容变化（演示：旧过滤对篡改的 `strategy_hash` / `profile_hash` 都返回相等，新检查都拒绝，未篡改的通过）。修正：不再删除任何哈希——当前载荷回到 2.1.0 时，每个派生哈希替换为**它所哈希的那个对象**的 2.1.0 孪生哈希（`base_spec_hash` ← `setup.base`，`detector.strategy_hash` ← `gate_fixtures.candidate().spec`，`effect_hash` ← 对应 effect，`profile_hash` ← 对应 Profile），并要求与记录载荷**逐字段相等**；载荷中每个非空 `*_hash` 的路径必须恰为这些派生路径；当前值必须等于对象当前的内容哈希。新增断言：策略哈希等于 spec 的 2.1.0 孪生哈希且不等于当前哈希；同 ref 下改 `params` 即不匹配；篡改记录的策略哈希即不匹配；同 ref 下改 Profile 内容（`lineage`）即不匹配。报告与钉值均未改动。

修正提交为 `c08c589`（代码冻结提交），其门禁见 §10。

## 10. 接受与收尾（2026-09-26）

- 最终门禁：代码冻结提交 `c08c589` 的全量非 PostgreSQL 门禁（`systemd-run --user --scope -q -p MemoryMax=5G -p MemorySwapMax=0 uv run pytest -q -m "not postgres" -p no:cacheprovider -rs`）→ `7179 passed, 136 deselected, 1 warning in 2906.24s (0:48:26)`，退出码 0；起止 SHA 均为 `c08c5895b4a60916715c2f88f65d061c40724b96`、dirty=0（2026-09-26T20:07:01Z → 20:55:29Z）；其后的 docs-only 提交**不在**该门禁覆盖范围内。同一冻结 HEAD 之前的 `1367dc8` 另有静态检查、Schema、OpenAPI / `gen:api`、`npm test` / `npm run build` 全部通过（§9）；
  `c08c589` 只改证据测试与文档，提交前 ruff / format / mypy 与相关定向测试通过（§9）。
- 接受：Accepted（2026-09-26，Codex 基于组合代码 `c08c589` 与其最终全量门禁复核接受）。
- 范围：ADR / 代码接受**不等于** Phase 0.5 整体验收：仓库种子仍无具名人工审阅的标签 / 资产分类（只有实施说明 §5 的提案），Phase 0.5 验收仍待 Codex / Raphael。
- 推送：组合提交与本收尾 docs-only 提交以普通 fast-forward 推送到 `wip/all-code-completion`（推送前核对远端仍为 `a5836b2` 且为祖先）；
  本地整合分支 `claude/adr-0055-integration` 不推送；未动 `main` / `phase/1` / tag。

## 11. 后续种子版本固定（2026-09-27）

对 `seed-2026-09-26.json` 的只读复核发现，新增的四条条目没有像 ADR-0055 §6 要求的那样显式固定 `schema_version`，因此加载时会采用当前默认版本。四条现已补为 `2.1.0`；主张、书目字段、分类与状态未改。现有 `v2_1_0/knowledge_seed_hashes.json` 仍只覆盖最初六条种子，不能作为这四条的黄金哈希证据。相关检查与验收尚未运行，Phase 0.5 状态不变。

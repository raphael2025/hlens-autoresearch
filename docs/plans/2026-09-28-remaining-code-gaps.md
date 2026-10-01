# 剩余底层代码完成计划

> 当前清单基线：`main@b5f80fe`（2026-10-01，PR #19 已合入）。本文件是唯一可执行的代码缺口清单；模块视图见[模块底层代码完成计划](2026-09-28-module-foundation-completion.md)。旧版清单中的任务已按当前主线、Accepted ADR 与 roadmap 重新核对；旧实现批次和审查证据保留在 Git 历史及[全栈代码完成历史](2026-09-26-all-code-completion-plan.md)。

## 基线与状态规则

- `main` 是代码完成状态的权威基线。当前 `main` / `origin/main` 为 `b5f80fe`；PR #17、#18、#19 已合入，当前没有开放 PR。CI 尚未配置。
- 本地有 12 个分支和 12 个 worktree；其中 4 个 Claude worktree 有未提交改动。远端保留 `main` 与已合入 PR #19 的头分支；17 个 Codex 临时分支 tip 已归档到 `refs/archive/2026-10-01/branches/codex/` 并清理。具体瞬时数量以后续 Git 盘点为准。
- PR #17–#19 的底层实现 / Web 变更均不能仅凭合并视为已验证；当前主线没有针对最新 HEAD 运行全仓门禁，E1-CAP-1 也未测量。
- **`IN_MAIN_UNVERIFIED`**：实现已在主线，测试、探针或阶段验收未完成。不得重新列成代码缺口，也不得称为通过。
- **`IN_PROGRESS`**：只存在于未合入分支 / worktree。列明所在模块和分支，不计入主线完成；先对照主线与 ADR 逐项复核，再另行确定整合任务。
- **`CODE_GAP`**：主线缺少、且已有 Accepted ADR / 已批准范围足以唯一约束的实现。
- **`BLOCKED` / `DEFERRED`**：需要新架构决定、人工输入、外部设置，或项目已明确暂缓；不作为当前可执行代码任务。

本轮只补全代码与必要测试用例；不运行 pytest、Ruff、format、mypy、build、容量 probe、数据生成或 Phase 验收。实现完成一律保留 `NOT_RUN / NOT_ACCEPTED` 状态。

## 当前可执行的主线代码缺口

| ID / 模块 | 目标与依据 | 文件边界 | 依赖与并行限制 | 代码完成条件 |
|---|---|---|---|---|
| P7-CS-EXEC / P7 | `main@b5f80fe` 仍对 `rank_cs` / `quantile_cs` 抛 `cross_sectional_execution_unsupported`。本地 `phase/1@775245e` 将它们纳入编译 allowlist；通过 `compile_plan(..., universes=...)` lower，并由 `CompiledPlan.build_providers(..., universes=...)` 以相同的显式 pinned manifest 构造专用 Provider。横截面节点仅可作为计划根；当前没有获批的跨截面到单序列适配语义，作为其它节点输入时整体拒绝。 | `research/hypotheses/typed_plan_compiler.py`、`research/hypotheses/typed_plan.py`、对应的 P7 测试及 `research/hypotheses/README.md`、能力矩阵 | 单一 P7 任务；不得与 E1 / `core/` 同时改动。若需让横截面结果进入单序列组合器或 Research Loop，先提出 `ARCHITECTURE_DECISION_REQUIRED`。 | 已达到本分支代码 DoD；回归用例已编写但 `NOT_RUN`。直到验证及整合完成前，`main` 的 P7-CS-EXEC 仍是未解决差异。 |

## 已在基线实现但未验证的 E1 代码

截至 `main@b5f80fe` 的静态源码对账，原 `E1-BOUNDED` 项涉及的仓库自有生产路径已有实现，本轮无需重写或把它们重复排入代码队列：

- ADR-0075：`infrastructure/catalog/iceberg_adapter.py` 提供固定 snapshot 的逐 manifest / data-file 批次扫描；生产查询可复用该 reader。
- ADR-0076：`infrastructure/canonical/normalizer.py` 的默认结果为固定摘要，完整 ID 通过 `iter_revision_ids()` 显式有序流式读取。
- D1：`infrastructure/parser/binance_archive.py` 提供 `parse_archive_spooled()`；`infrastructure/revision/store.py` 的生产 ingest 使用 spool microbatches。完整 `ParsedArchive` 的物化入口仍是显式兼容便利 API。
- ADR-0077：v3 Dataset 的 evidence builder、chunk writer、streaming verifier、manifest store 和 `DatasetBuildPipeline` 均已存在；旧 v2 manifest 保持只读，不再通过 v2 builder 创建新数据集。

上述只证明主线代码路径存在，不证明其测试、静态检查或 E1-CAP-1 通过。E1-CAP-1（完整进程 ≤ 32 MiB）与 ADR-0077 DQ-9 容量参数继续开放。PyIceberg metadata、单个 Avro manifest 文件、Arrow row group / ORC stripe 与 tmpfs / cgroup 工作集须按既有 ADR 测量；改变 Iceberg history / retention、读写权威或公开完整物化接口需另行架构决定。本轮不做这些验证或语义改变。

除 P7-CS-EXEC 外，本轮静态盘点未确认其它可在当前 Accepted ADR 内直接编码的主线缺口。若后续审查发现其它缺口，先给出主线文件证据、适用 ADR / roadmap 条目与唯一文件边界，再加入本表；不能因功能“看起来有用”而扩展范围。

## 未合入的进行中工作

以下只是 2026-10-01 worktree 快照；在完成主线对账及其 ADR 状态复核前，不作为已完成能力，也不自动纳入可执行主线队列。

| 模块 | 分支 / worktree | 可见内容 | 状态 |
|---|---|---|---|
| P1 Dataset / Quality | `feature/p1-entry`（12 个未提交条目） | Dataset CLI、factory、pinning/profile、Quality identity registry 与配套用例 | `IN_PROGRESS`；分支仅有 ADR-0101–0107 文档批次的共同基线，未提交内容需逐项对照 `main`。 |
| P1 Catalog / E1 | `feature/e1-catalog`、`feature/e1-ingest`、`feature/e1-listing` | 有界 catalog history / batch scan 与 ingest/listing 提交 | `IN_PROGRESS`；仅候选实现，不代表 E1 容量或验收结论。 |
| P2 State | `feature/p2-state` | State run/show/list CLI 及计算入口拆分 | `IN_PROGRESS`；先核对 ADR-0102 的状态及文件差异。 |
| P7 Discovery | `feature/p7-bind`（9 个未提交条目） | typed-plan binding/evidence、算子和配套用例 | `IN_PROGRESS`；不视为 P7-CS-EXEC 已完成。 |
| P10 Router | `feature/p10-deviation`（6 个未提交条目） | Paper deviation API / DTO / Web 页面与用例 | `IN_PROGRESS`；先复核 ADR-0104 与当前主线兼容边界。 |
| P11 Operations | `feature/p11-ops`、`feature/p11-tests`、共享提交分支 `claude/module-completion` | lifecycle CLI、baseline export、degradation batch driver、State CLI 后续及类型边界修订 | `IN_PROGRESS`；ADR-0102/0105 的分支版决策和实现均须对照主线状态后再决定。 |
| P14 Migration | `feature/p14-migration`（9 个未提交条目） | Migration target / reference backtester 与配套用例 | `IN_PROGRESS`；没有具体目标系统时不把 target adapter 视为已批准交付。 |

## 阻塞、人工门与明确暂缓

| 项 | 分类 | 处理方式 |
|---|---|---|
| E1-CAP-1：完整进程工作集 ≤ 32 MiB | 验证 / 容量门 | 当前代码任务完成后再测；保留既有门槛，当前不得声称通过。 |
| P0.5 种子 tags/assets 与新增 golden hashes | 人工 / 后续生成 | tags/assets 由具名审阅者决定；hash 在允许生成的后续验证窗口生成。 |
| P11 真实运行、缺失 baseline repro inputs | 部署 / 数据资格 | ADR-0100 修订 2 要求新运行记录参数；旧运行缺项继续拒绝。补配置或伪造旧值不属于代码完成任务。 |
| P14 迁移目标及 golden data | 外部输入 | 收到目标系统与具名数据前不实现目标适配器。 |
| 实际 Catalog 建表 | 运行操作 | 现有显式入口与授权不能替代运行操作；此次不执行。 |
| Profile 数值、风险预算、真实历史市场结论 | 人工 / 研究决策 | 不猜数值，不通过实现替代审阅或校准。 |
| `TypedPlan` / loop 新能力运行开关 | 明确关闭 | 默认关闭；不因 Provider 存在或代码合并而启用。 |
| W1 全仓门禁、各 Phase 验收、WBS 发布门 | 后续验证 | 另按项目授权执行；本次不运行、不验收。 |

## 派工顺序

1. 先完成分支内容 / ADR 对账，避免对主线重复实现或将未接受的分支版 ADR 当作已接受。
2. E1 的仓库自有生产路径代码已在基线；保持 E1-CAP-1 / DQ-9 为未验证容量门。完成 P7-CS-EXEC 后如源码复核发现新的仓库自有增长持有项，只能作为有文件证据的新任务单独排入。
3. 分支候选只有在代码与决策状态完成独立复核后，才可以被另行列为任务；此次清单本身不授权分支合并。
4. 当前 P7 代码接线已补齐并加入必要回归用例，均注明 `NOT_RUN`；后续统一调试、容量测量和 Phase 验收分别登记，不把它们并入代码完成状态。

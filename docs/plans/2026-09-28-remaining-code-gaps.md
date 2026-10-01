# 剩余底层代码完成计划

> 当前清单基线：`main@b5f80fe`（2026-10-01，PR #17–#19 已合入）。本文件是当前唯一的代码缺口清单；模块映射与执行视图见[模块底层代码完成计划](2026-09-28-module-foundation-completion.md)。历史批次和失败证据保留在[全栈代码完成历史](2026-09-26-all-code-completion-plan.md)，不在此重写。

## 基线与状态规则

- 当前 `main` 与 `origin/main` 均为 `b5f80fe`。PR #17–#19 已合入；当前远端引用中有 `main` 和 PR #19 的源分支，项目状态盘点记录无开放 PR；CI 未配置。
- 本地快照：13 个分支、12 个 worktree。核对起点的根 worktree 为 `phase/1@47446f4` 且干净；之后本地工作区增加了计划 / 状态文档及 infrastructure 回归修复，进度见 `PROJECT_STATUS.md`。另有 4 个 Claude worktree 含未提交改动（`feature/p1-entry`、`feature/p7-bind`、`feature/p10-deviation`、`feature/p14-migration`）。分支 / worktree 数量仍是原盘点快照，不代表当前未提交文件数量。
- 17 个 Codex 临时分支 tip 已归档到 `refs/archive/2026-10-01/branches/codex/` 并清理；远端分支与本地归档是不同状态。该分支数量是本次盘点快照，不作为长期不变事实。
- `IN_MAIN_UNVERIFIED`：代码在主线，但对应测试 / 检查 / 验收未完成；不重复列为代码缺口，也不得称为通过。
- `IN_PROGRESS`：代码或文档只在未合入主线的分支 / worktree；不计为主线完成。须逐项对照当前 `main` 与主线 Accepted ADR 后才能整合。
- `CODE_GAP`：主线缺失实现，且已有主线 Accepted ADR / 已批准 roadmap 唯一约束其语义，可直接实现。
- `BLOCKED` / `DEFERRED`：依赖新架构决定、人工输入、外部条件或明确暂缓；不属于当前可执行代码任务。
- 本计划的代码完成可包含新增必要测试。2026-10-01 的文档整理批次未运行检查；后续实现批次的检查范围及结果单独记录，不替代主线门禁或 Phase 验收。

## 当前可执行的主线代码缺口

| ID / 模块 | 主线缺口与依据 | 文件边界 | 依赖与并行限制 | 代码完成条件 | 当前状态 |
|---|---|---|---|---|---|
| E1-V2-REPLAY / P1 Dataset | `main@b5f80fe` 的 ADR-0077 DQ-10 只读兼容入口按不存在的 `selection_id` 列查询 v2 manifest 表，历史 v2 Dataset 重放会失败。候选分支 `phase/1@48c7d01` 改为从 selection batch snapshot 反查 `dataset_snapshot_id`，并校验加载 manifest 与该 snapshot 一致。 | `infrastructure/dataset/builder.py`、Dataset v2 / golden compatibility tests | 依赖主线已接受 ADR-0077；与其他 Dataset / `core/` 契约改动串行。不得恢复新 v2 写入。 | 公开重放入口能读取已持久化 v2 manifest；绑定、不匹配、缺失等负例拒绝；既有 golden replay 保持兼容；相关测试与检查结果在实现批次如实记录。 | `IN_PROGRESS`：候选实现与定向验证存在于 `phase/1`，未合入 `main`。 |
| P7-CS-EXEC / P7 Discovery | `main@b5f80fe` 对 `rank_cs` / `quantile_cs` 执行接线仍拒绝。候选 `phase/1@48c7d01` 将其加入编译路径，并通过显式 pinned universe 构造专用 Provider；横截面结果只允许作为计划根，不转接为单序列输入。依据 ADR-0088 / 0099 / 0100。 | `research/hypotheses/typed_plan_compiler.py`、`research/hypotheses/typed_plan.py`、`plugins/features/p7_cross_sectional.py`、相应 tests / P7 文档 | 依赖主线 Accepted ADR-0100。单一 P7 任务；不得与 E1 / `core/` 同时修改。若需组合成单序列输入或接入仅接受 Strategy 根的 Research Loop，先提出 `ARCHITECTURE_DECISION_REQUIRED`。 | 合法横截面根计划可用同一显式 pinned manifest 编译并构造 Provider；无 manifest、错误根节点及跨类型输入均 fail closed；执行开关仍默认关闭；增加相应正反例测试并记录检查结果。 | `IN_PROGRESS`：候选实现与定向验证存在于 `phase/1`，未合入 `main`。 |

以上两项是当前主线源码对账确认的实现缺口。分支中的实现属于待复核候选，不能据此标记主线已完成；本计划不授权合并或推送。

## 已在主线实现、仍待验证的代码

截至 `main@b5f80fe` 的静态对账，原 E1 bounded 代码项涉及的仓库自有生产路径已实现，不重复排进实现队列：

- ADR-0075：`infrastructure/catalog/iceberg_adapter.py` 提供固定 snapshot 的逐 manifest / data-file 批次扫描。
- ADR-0076：`infrastructure/canonical/normalizer.py` 默认返回固定摘要；完整 ID 由 `iter_revision_ids()` 显式有序流式读取。
- D1：`infrastructure/parser/binance_archive.py` 提供 `parse_archive_spooled()`；生产 ingest 使用 spool microbatches。完整 `ParsedArchive` 物化入口仍是显式兼容 API。
- ADR-0077 / 0093 / 0094：Quality 与 Dataset 的固定大小 manifest、内容寻址 evidence stream、v3 chunk writer / verifier / pipeline 已在基线；旧版本按只读兼容路径处理。

以上只说明源码路径存在，不证明测试、静态检查、容量或 Phase 验收通过。E1-CAP-1（完整进程 ≤32 MiB）和 ADR-0077 DQ-9 参数仍开放；PyIceberg metadata、Avro manifest、Arrow row group / ORC stripe 与总 scratch / cgroup 工作集须按后续验证计划测量。改变 Iceberg history / retention、数据权威或公开完整物化接口须另行决策。

P0.5 Knowledge Store / Provider 及 consumers 的既有定向结果为 126 passed，Ruff / mypy 通过；这些本地更改未合入主线。Knowledge seeds 的 tags/assets 仍待具名人工审阅，不能自动填充。

## 未合入主线的进行中工作

以下为 2026-10-01 worktree 快照。分支提交和工作区改动都不能作为主线完成证据；分支中 ADR-0101–0107 的版本尚未进入 `main`，不能替代主线 Accepted ADR。

| 模块 | 分支 / worktree | 可见内容 | 状态与限制 |
|---|---|---|---|
| P1 Dataset / Quality | `feature/p1-entry` | 12 个未提交路径：Dataset CLI / factory / pinning / profile、Quality identity registry，以及 `report_v3.py` 修改和相应用例 | `IN_PROGRESS`；逐项核对 ADR-0101 与主线，未提交内容不得视作可合并批次。 |
| P1 Catalog / E1 | `feature/e1-catalog`、`feature/e1-ingest`、`feature/e1-listing` | Catalog bounded head/history 与 batch scan、Arrow row stream 等候选；后两支还带入 State / P11 等共享提交 | `IN_PROGRESS`；候选之间存在重叠提交，按文件 / commit 净差异去重；不得据此声称 E1-CAP-1 通过。 |
| P1 Dataset DQ-10 / P7-CS-EXEC | `phase/1`（当前本地候选） | DQ-10 v2 历史 manifest replay 修正；P7 横截面编译与 Provider 接线；Knowledge 测试断言更新 | `IN_PROGRESS`；Phase 1 infrastructure 选择集 `2298 passed, 88 skipped`；DQ-10 public replay / v2 golden 专项 `35 passed`；P7 横截面编译 / binding / lowering / provider 专项 `105 passed`。实现与专项验证在候选分支已完成，主线仍待独立审阅 / 整合，不代表全仓门禁。 |
| P2 State | `feature/p2-state` | State compute / report CLI、导出与相关测试 | `IN_PROGRESS`；核对 ADR-0102 是否为主线已接受决定及其边界后再定执行顺序。 |
| P7 Discovery binding | `feature/p7-bind` | `p7_binding` / `p7_evidence`、DSL / audit 修改与测试；当前有 9 个未提交路径 | `IN_PROGRESS`；与 P7-CS-EXEC 分开审查，不得假定互相覆盖。核对 ADR-0103 与主线后再决定整合。 |
| P10 Router | `feature/p10-deviation` | Deviation API / DTO / research 与 Web 页面；当前有 6 个 Web 文件未提交，分支净差异另含测试 fixture 与契约适配 | `IN_PROGRESS`；核对 ADR-0104 与当前 DTO / 报告绑定。 |
| P11 Operations | `feature/p11-ops`、`feature/p11-tests`、`claude/module-completion` | Lifecycle CLI、baseline export、degradation batch、P2 State CLI；候选分支有共享 / 重叠提交 | `IN_PROGRESS`；按 ADR-0105 与主线拆分 P11 / P2，先去重和审查，再确定文件边界。 |
| P14 Migration | `feature/p14-migration` | Backtest migration target / reference backtester 候选；当前有 3 个修改与 6 个未跟踪路径 | `IN_PROGRESS`；核对 ADR-0106；无具体目标系统与 golden data 时仍属阻塞候选。 |

### 候选分支回归信号（未分诊）

`phase/1@47446f4` 的历史输出曾为 `11 failed, 2287 passed, 88 skipped`。本地复现并逐项分诊后，处理了分层导入、reader 生命周期预期、测试 DSN 占位值、PIT 语义 / reader 上界、listing policy 验证层和 Dataset v2/v3 fixture 绑定；完整 Phase 1 infrastructure 选择集随后为 `2298 passed, 88 skipped in 855.34s`。这些是当前候选分支的结果，不代表 `main` 通过全仓门禁；失败历史保留，不再作为待分诊项。

同轮受影响策略回归为 `257 passed, 1 skipped, 1 failed in 500.03s`。唯一失败是 `tests/research/strategies/test_backtest_validation.py::test_the_default_model_reports_are_the_pre_b67_ones` 的旧 dataset-report hash；该精确用例在改动前 `47446f4` 上也以相同实际哈希失败（`7612179d…`），确认为既有失败。本轮不改写 golden，也不将其列成本次引入的代码缺口。

## 阻塞、人工门与明确暂缓

| 项 | 分类 | 当前处理 |
|---|---|---|
| E1-CAP-1：完整进程工作集 ≤32 MiB | 容量 / 验证门 | 在生产路径与 `main` 一致的提交上测量；保留原门槛，不声称通过。 |
| ADR-0077 DQ-9 参数 | 需容量证据 | 有足够测量证据后再定；不得根据局部 smoke 猜值。 |
| P0.5 种子 tags/assets | 人工决定 | 等具名审阅者；不在代码批次自动补标签。 |
| P11 真实运行与缺失 baseline repro inputs | 外部部署 / 数据资格 | ADR-0100 修订 2 要求新运行记录输入；缺项旧运行继续拒绝。 |
| P14 迁移目标与 golden data | 外部输入 / 决策 | 目标系统、代表性 golden data 未提供前不实现 adapter。 |
| 实际 Catalog 建表 | 运行操作 | 作为独立运行任务；不与本轮文档整理混同。 |
| Profile 数值、风险预算、真实市场结论 | 人工 / 研究决策 | 不猜数值、不以实现替代决策或校准。 |
| TypedPlan / P7 与 P12 运行开关 | 明确关闭 | 默认关闭；代码存在不构成开启授权。 |
| 全仓门禁、E1 容量测量、Phase 验收 | 验证 / 验收 | 均为独立后续门；本轮不执行。 |

## 执行顺序

1. 已完成：分诊并复跑 `phase/1` 的 11 个历史失败；具体证据与结果见上文。不得将该候选分支结果归给 `main`。
2. 对候选分支与主线 Accepted ADR 做只读净差异审查，去除重复提交 / 文件并确认 branch-only ADR 状态；不整支合并。
3. 复核并收口 E1-V2-REPLAY 与 P7-CS-EXEC 两项主线缺口；实现、代码审查、测试验证和合并分别记录为不同状态。
4. 其余模块只有在逐项审查证明主线缺口、且主线已接受 ADR / 已批准 roadmap 唯一约束实现时，才能从 `IN_PROGRESS` 改列为 `CODE_GAP`。
5. E1-CAP-1、全仓门禁与 Phase 验收按 WBS 独立推进；不把局部回归等同容量或验收证据。

# 剩余底层代码完成计划

> 本文件是当前唯一的代码缺口清单。执行视图见[模块底层代码完成计划](2026-09-28-module-foundation-completion.md)；项目验收工作包见[全项目验收 WBS](2026-09-28-project-completion-wbs.md)。历史批次与失败证据保留在[全栈代码完成历史](2026-09-26-all-code-completion-plan.md)，不在此重写。

## 基线与状态规则

- 代码完成基线为 `main@b5f80fe`，与 `origin/main` 同步。PR #17（ADR-0098/0099）、PR #18（ADR-0100）及 PR #19 均已合并；GitHub 核对结果均为 `MERGED`，PR #19 不是 Draft。远端当前有 `main` 和已合并 PR #19 的源分支，无开放 PR；CI 未配置。
- 本地盘点快照：13 个分支、13 个 worktree。主 worktree 在 `phase/1@4c2356c`，含本轮计划 / 状态文档修改；独立 main probe worktree 为 detached `b5f80fe`。4 个 Claude worktree 有未提交文件，属于进行中；分支 / worktree 数量是本轮快照，不是长期事实。
- `main` 是代码完成状态的权威基线。分支代码和测试只记为 `IN_PROGRESS`，不计入主线完成。
- `CODE_DONE`：实现已审阅、整合入 `main` 并通过全仓 pytest / ruff / format / mypy 门禁；容量测量与 Phase 验收仍是独立门。
- `IN_MAIN_UNVERIFIED`：实现已在主线，测试、检查、容量测量或验收仍未完成；不重复列为代码缺口。
- `IN_PROGRESS`：实现 / 修复只存在于未合入分支或 worktree；须独立审阅后才可整合。
- `CODE_GAP`：主线缺实现，且已 Accepted ADR / 已批准 roadmap 足以约束实现，无需新架构决定。
- `BLOCKED` / `DEFERRED`：依赖人工输入、外部条件、新决策或明确暂缓，不进入可执行代码队列。
- 本次只更新计划与状态文档；未运行测试、lint、类型检查、build、probe、数据生成或 Phase 验收。未来代码任务可包含必要测试，但本次不声称验证通过。

## 当前可执行代码缺口

| ID / 模块 | 主线缺口与依据 | 文件边界 | 依赖、并行限制 | 代码完成条件 | 状态 |
|---|---|---|---|---|---|
| E1-ARCHIVE-REUSE / P1 Normalizer | 主线 pinned verifier 跨 256 行窗口会重新严格解析同一 archive。`main@b5f80fe` 正式探针在 100k `verify_archive` 单阶段耗时 366.084 秒，见[中断记录](../reviews/2026-10-01-e1-cap1-main-partial.md)。ADR-0100 §6 支持保留严格校验的有界实现。 | `infrastructure/parser/binance_archive.py`、`infrastructure/revision/row_integrity.py`、`infrastructure/canonical/normalizer.py`、对应测试与 memory probe | 依赖 ADR-0100、严格 D1 parser 和 canonical scratch 决定；与其它 P1/E1/Dataset 及 `core/` 契约任务串行。至多缓存一个 archive spool，多 archive 顺序处理并关闭。 | 同一 ObjectRef 跨窗口只严格解析一次；spool 与 parser 输入显式使用 canonical scratch；行校验、拒绝语义和错误优先级保持不变；close / eviction 可靠释放资源；必要回归和代码审查完成。容量结果另由 E1-CAP-1 决定。 | `CODE_DONE`（2026-10-01）：`phase/1` 候选经独立审阅；`63d09a4` 补齐非 frozen pin 的 verifier / archive spool 释放；随 PR 整合入 `main`。容量结果另由 E1-CAP-1 决定。 |
| E1-RAW-WINDOW-REUSE / P1 Normalizer | 主线 `_raw_window` 对每个 proof / canonical window 都会重新扫描 Raw catalog。候选 archive-spool 诊断的 100k `verify_archive` 降至 78.207 秒，但 500k stage 在运行超过 15 分钟、读取约 17.7 GB 后被中断；这表明重复 Raw 查询仍是待解决成本，不能视为容量 FAIL/PASS。 | `infrastructure/canonical/normalizer.py`、必要的 bounded scratch/index helper、normalizer / row-integrity tests、memory probe | 依赖 ADR-0075/0100 的固定快照、有界扫描与 E1-CAP-1 语义；须先审阅 ARCHIVE-REUSE 候选；与其它 E1、Dataset、`core/` 契约任务串行。磁盘索引 / spool 必须有界、确定关闭，不缓存整日 Raw rows 于内存。 | 一次有界、确定性的 Raw 源读取可供后续窗口复用；保留位置排序、缺号、重复位置、lineage / row-integrity 拒绝行为；正常、失败和 close 路径均释放 scratch；回归证明不会逐窗口重扫；正式容量验证另行执行。 | `CODE_DONE`（2026-10-01，`fe842b3`）：首窗口一次过滤扫描（窄读不变），第二窗口起整单元一次 spool 到 canonical scratch（Arrow IPC 1024 行重切片 + SQLite 位置索引），后续窗口读 spool；错误语义不变、所有关闭路径释放。容量另测。 |
| E1-CANONICAL-WINDOW-REUSE / P1 Normalizer | 2026-10-01 在 `main@5c3b516` 上的 E1-CAP-1 正式探针与 profile 发现：replay / resume 的证明调查对每个已提交窗口各做两次 Canonical 目录扫描（`_check_committed_window`、`_check_unique`），而每次扫描都按 ADR-0075 遍历快照的全部 manifest；每个 microbatch 提交一个 manifest，故总成本随 N 二次增长（40k replay 198 s，其中 160 s 在这两处）。ADR-0075/0100 §6 支持保持严格校验的有界实现。 | `infrastructure/canonical/normalizer.py`、normalizer tests | 与其它 E1 / Dataset / `core/` 任务串行；只改证明调查路径，写入后的逐提交 read-back 与 PIT 窄读不变。 | 首窗口仍为两次过滤读；第二窗口起单元已提交 block 一次 spool、该 symbol 在 block 时间跨度内的 `(revision_id, time)` 一次索引到 scratch，逐窗口检查与错误语义不变；所有关闭路径释放 scratch；变异测试证明 spool 路径检查有效。 | `CODE_DONE`（2026-10-01，待合入）：40k replay 198 s → 48 s；canonical / pit / revision 993 passed / 24 skipped；容量另测。逐提交 read-back 仍按 ADR-0075 遍历全部 manifest，写入路径时间仍超线性，若要增量化须先立 ADR。 |
| E1-V2-REPLAY / P1 Dataset | 主线 DQ-10 只读重放路径在历史 v2 manifest 查询中未按 selection batch 的 snapshot 关联；候选改为从 `selection_id` 查 batch snapshot，再核对 manifest snapshot 一致。依据主线 ADR-0077 DQ-10。 | `infrastructure/dataset/builder.py`、Dataset v2 compatibility / golden tests | 依赖主线已接受 ADR-0077；与其他 Dataset / `core/` 契约任务串行；不得增加新的 v2 writer。 | 已存在的 v2 manifest 可经公开入口重放；缺失、重复、snapshot 不匹配均 fail closed；既有 golden replay 保持兼容；必要回归通过并单独记录范围。 | `CODE_DONE`（2026-10-01）：候选经审阅；`63d09a4` 增加重新派生 manifest 与已持久化不一致时拒绝写入、selection 多 snapshot fail closed，变异测试验证守卫有效。 |
| P7-CS-EXEC / P7 Discovery | 主线提供 `rank_cs` / `quantile_cs` 定义与 Provider，但 `typed_plan_compiler` 仍拒绝其执行编译。候选只允许显式 pinned universe 下的横截面根 Provider，禁止隐式接入单序列节点或 Research Loop。依据 ADR-0088/0099/0100。 | `research/hypotheses/typed_plan_compiler.py`、`typed_plan.py`、`plugins/features/p7_cross_sectional.py`、对应测试与 P7 文档 | 依赖主线 Accepted ADR；独立 P7 任务，不与 E1 或 `core/` 契约并行。若要横截面输出进入单序列组合器或 Research Loop，先提出架构决定。 | 显式 pinned manifest 可绑定并实例化专用 Provider；缺失/错误绑定、非法根及跨类型消费 fail closed；执行开关仍默认关闭；正反例回归与审查完成。 | `CODE_DONE`（2026-10-01）：候选经审阅；`63d09a4` 增加直接引用横截面 FeatureSpec 作为单序列输入时编译期拒绝；执行开关仍默认关闭。 |

以上四项主线实现缺口已于 2026-10-01 在 `phase/1` 收口并经全仓门禁（见[W1 门禁修复记录](../reviews/2026-10-01-w1-gate-repair.md)），随 phase/1 → main PR 整合。原记录：截至 `main@b5f80fe` 确认的四项主线实现缺口。PR #18 已覆盖 ADR-0100 定义的底层能力批次和 E1 bounded scan 基础实现，但不包含上述 archive reuse、Raw window reuse、DQ-10 replay 修正或 P7 横截面 compiler 接线；PR #19 的 P11/P12 只读审计视图也不再列为代码任务。不得将 branch-only 候选表述为主线已完成；本计划不授权合并或推送。

## 主线已有实现、仍待验证

- ADR-0075：`infrastructure/catalog/iceberg_adapter.py` 有固定 snapshot 的逐 manifest / data-file 批次扫描。
- ADR-0076：`infrastructure/canonical/normalizer.py` 默认返回固定摘要，完整 ID 通过有序 `iter_revision_ids()` 流式读取。
- ADR-0077/0093/0094：Quality / Dataset 有界 manifest、内容寻址 evidence stream、v3 chunk pipeline 与兼容读取路径已在主线。
- ADR-0098/0100 与 PR #19：P11 authority/read-only audit 和 P12 proposal audit view 已在主线；P11 真实运行仍需要部署与合格运行输入。
- PR #18 的 ADR-0100 实现批次已在主线。除本清单明确列出的执行接线与复用缺口外，不将 ADR-0098/0099/0100 的既有交付重复登记为未开始任务。
- 上述仅描述源码交付；不能代表测试、静态检查、容量或 Phase 验收通过。E1-CAP-1 的 32 MiB 完整进程门仍开放，DQ-9 仍待有效容量证据。

## 未合入主线的进行中工作

以本轮 worktree 快照为准；分支提交、未提交文件及其定向测试均不作为主线完成证据。ADR-0101–0107 仅存在分支，尚非 main 上的授权依据。

| 模块 | 分支 / worktree | 工作状态 |
|---|---|---|
| P0.5 Knowledge | `phase/1` | 有定向实现与检查结果；种子 tags/assets 仍需具名人工审阅。 |
| P1 Dataset / Quality | `phase/1`、`feature/p1-entry` | DQ-10 replay 候选；`feature/p1-entry` 有未提交 CLI / factory / pinning / profile / identity-registry 改动，须按文件审阅。 |
| P1 Catalog / E1 | `feature/e1-catalog`、`feature/e1-ingest`、`feature/e1-listing`、`phase/1` | Catalog scan 与 E1 archive-spool 候选重叠；逐 commit / 文件去重，不整支合并，不以候选 probe 声称 CAP-1 通过。 |
| P2 State | `feature/p2-state`、共享 P11 worktree | State compute / report CLI 候选；与 P11 重叠处须先拆分。 |
| P7 Discovery | `phase/1`、`feature/p7-bind` | CS execution 候选与 binding / evidence 候选分开审阅，不假设互相覆盖。 |
| P10 Router | `feature/p10-deviation` | Deviation API / DTO / research / Web 候选；有未提交 Web 文件，按当前主线 DTO 与只读边界核对。 |
| P11 Operations | `feature/p11-ops`、`feature/p11-tests`、`claude/module-completion` | lifecycle CLI / baseline export / degradation batch 候选；与 P2 共享提交先按净差异拆分。 |
| P14 Migration | `feature/p14-migration` | migration target / reference backtester 候选；branch-only ADR 与未提交文件不能授权具体 target。 |

候选历史测试记录（Phase 1 infrastructure `2298 passed, 88 skipped`、DQ-10 `35 passed`、P7-CS-EXEC `105 passed`、E1 parser/verifier/normalizer `216 passed`）保留作候选 evidence；它们不等价于 main 全仓门禁、正式 E1 容量结果或 Phase 验收。`phase/1` 受影响策略回归唯一 B67 hash 失败已在变更前提交精确复现；失败证据不删除、不重钉 golden。

## 阻塞、人工门与明确暂缓

| 项 | 分类 | 当前处理 |
|---|---|---|
| E1-CAP-1：完整进程 ≤32 MiB | 容量 / 验证门 | `main@50d6bb6` 正式矩阵数值 PASS（2026-10-02），但元数据随批次数线性增长、不满足关闭标准；新增架构决定 ADR-0108（Proposed）为前置，不作为可直接执行的代码缺口。 |
| ADR-0077 DQ-9 参数 | 容量证据依赖 | 获得有效容量证据后再确定，不由局部 smoke 外推。 |
| P0.5 seed tags/assets | 人工决定 | 等具名审阅者，不自动填标签。 |
| P11 真实运行 | 部署 / 合格输入 | 需要部署环境与符合 ADR-0100 的 repro inputs。 |
| P14 具体迁移 | 外部输入 / 决策 | 目标系统与代表性 golden data 未提供前不做 adapter。 |
| 真实 Catalog 建表 | 独立运行操作 | `event.*` / `state.*` 建表与本轮文档工作分开。 |
| Profile 数值、风险预算、市场结论 | 人工 / 研究决策 | 不猜数值，不用代码代替决定或校准。 |
| P7 / P12 执行开关 | 明确关闭 | 默认关闭；代码存在不构成启用授权。 |
| 全仓门禁与 Phase 验收 | 验证 / 验收 | 独立后续门；本轮未运行。 |

## 执行顺序

1. 逐文件只读审阅未合入 worktree 与 main Accepted ADR，给重叠提交去重；不因共享提交或分支名直接合并 / 删除。
2. 串行处理 P1：先复核 archive spool reuse，再按 bounded scratch 方案解决逐窗口 Raw 查询；保持 D1 与 lineage 语义不变。
3. 单独完成 DQ-10 v2 replay 候选的审阅 / 收口；不得恢复 v2 写入。
4. 单独完成 P7-CS-EXEC；不扩展到 Research Loop 或单序列隐式消费。
5. 仅在逐项证明主线仍缺、且 main 已接受 ADR / roadmap 唯一限定语义时，才新增任务卡。
6. E1-CAP-1、全仓门禁与 Phase 验收仍各自独立；不以局部回归或文档整理代替。

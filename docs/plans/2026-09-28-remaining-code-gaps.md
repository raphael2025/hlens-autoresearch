# 剩余底层代码完成计划

> 本文件是当前唯一的代码缺口清单。执行视图见[模块底层代码完成计划](2026-09-28-module-foundation-completion.md)；项目验收工作包见[全项目验收 WBS](2026-09-28-project-completion-wbs.md)。历史批次与失败证据保留在[全栈代码完成历史](2026-09-26-all-code-completion-plan.md)，不在此重写。

## 当前基线（2026-10-02 第三轮）

- 代码权威线为 `phase/1`（本轮整合后 HEAD 见 `PROJECT_STATUS.md` §1）；`main` 仍为 `d9ddd67`（PR #22），本轮整合待门禁后经 PR 合入。
- 状态定义不变：`CODE_DONE`（审阅 + 整合 + 门禁）、`IN_MAIN_UNVERIFIED`、`IN_PROGRESS`、`CODE_GAP`、`BLOCKED` / `DEFERRED`。容量测量与 Phase 验收是独立门。
- 2026-10-01 的四项缺口（E1-ARCHIVE-REUSE、E1-RAW-WINDOW-REUSE、E1-V2-REPLAY、P7-CS-EXEC）与 E1-CANONICAL-WINDOW-REUSE 已 `CODE_DONE`，明细见 Git 历史（PR #20 / #21）与本文件旧版本。
- 2026-10-01 分支轮次的 11 个候选分支 / worktree 已逐一核对：其工作已在 `phase/1` 以 Accepted ADR 重新落地，或属 Rejected 的 ADR-0107；分支 tip 与未提交内容归档于 `refs/archive/2026-10-02/`，分支与 worktree 已删除。

## 本轮模块（第三轮，2026-10-02）

| ID / 模块 | 依据 | 交付 | 状态 |
|---|---|---|---|
| E1-UNIT-COMMIT / P1 Catalog + Normalizer + Raw store | ADR-0108 | 一个逻辑单元一个 snapshot（Canonical 与 Raw 归档元素）；旧逐微批历史只读兼容；探针 `--prefill-units K` | `CODE_DONE`（门禁见 §1）；补两处审查发现：窄证明须复核单元已提交内容（`37c9608`，伪造行回归），无已提交元素的归档单元判为不完整（`27a2c0d`）；红队崩溃矩阵覆盖两种布局 |
| P1-V3-ENTRY / Dataset v3 生产入口 | ADR-0101（修订 1–2） | profile / CLI / worker job / verifier 工厂 / 上游与质量报告入口；listing 质量报告入口（profile 1.1.0 `listing_quality`） | `CODE_DONE` |
| P1-V3-LEGACY-BIND / 契约 2.6.0 | ADR-0109 | 2.6.0+ v3 manifest 的旧质量表"有 snapshot 才绑定"；Schema / DTO / Web 登记；关闭 D-V3-LEGACY-BIND | `CODE_DONE` |
| P2-STATE-ENTRY | ADR-0102 | State run / show / list 与诊断报告 | `CODE_DONE` |
| P7-BIND / P7 计划绑定与准入交接 | ADR-0103 | `hlens.p7.plan@1.0.0`、PREPARE 证据、拒绝审计、COMMIT → 循环交接、§7 勘误；`run_inputs.strategy_params` 剥离 P7 记录 | `CODE_DONE`；开关默认关 |
| P7-RESTORE | ADR-0110 | P7 准入候选经 `P7PlanSource` + COMMIT 证明在持久化恢复中重建 | `CODE_DONE` |
| P10-DEVIATION-BIND | ADR-0104 | paper deviation 绑定运行（RunBinding、DTO、Web） | `CODE_DONE` |
| P11-OPS | ADR-0105 | Lifecycle 写入 CLI、基线导出、批量驱动、durable 只读导出、Dataset 版 operator、resolver / 环境 / CLI 测试 | `CODE_DONE`；真实运行仍需部署 |
| P14-TARGET | ADR-0106 | 独立参考回测引擎与迁移演练 | `CODE_DONE` |

当前没有剩余的 `CODE_GAP`：已 Accepted ADR 约束的实现全部在 `phase/1`。开放的是容量与验收门（E1-CAP-1 重跑、各 Phase 验收）、运行操作与人工门（见下表）。

## FOLLOW-UP（发现、未执行）

- P11 权威解析对同一 v3 manifest 每次加载都做完整流式复核（测试中单次 5 分钟以上）；同一次解析内可按内容哈希只复核一次。属性能优化，需要时另立任务。
- `LocalFileStorageAdapter` 在 publish 后保留每个 staging 条目（目录 + `meta.json`，供重复 publish 幂等判定）；有界流式复核会暂存数万个小 run 对象，条目只增不减（P11 权威测试单模块约数 GB，在 7.9 GB tmpfs `/tmp` 上会写满）。生产上表现为 staging 目录 inode 持续增长；清理策略（显式 maintenance 或发布后回收）须另立 ADR，不在本轮改动存储语义。
- D-META-AGE：metadata 随历史单元数 K 线性增长，待 E1-CAP-1 预填充场景的 K 轴数据后另立 ADR。

## 阻塞、人工门与明确暂缓

| 项 | 分类 | 当前处理 |
|---|---|---|
| E1-CAP-1：完整进程 ≤32 MiB | 容量 / 验证门 | ADR-0108 已实现；待整合入 `main` 后按 ADR-0108 §9(d)(e) 重跑正式矩阵与预填充历史场景（K = 0 / 1000 / 3000），32 MiB 门槛不变。 |
| ADR-0077 DQ-9 参数 | 容量证据依赖 | 获得有效容量证据后再确定，不由局部 smoke 外推。 |
| P0.5 seed tags/assets | 人工决定 | 等具名审阅者，不自动填标签。 |
| P11 真实运行 | 部署 / 合格输入 | 需要部署环境与符合 ADR-0100 的 repro inputs。 |
| P14 具体迁移 | 已决定 | ADR-0106 选定参考回测引擎为迁移对象，已实现；其他目标另立 ADR。 |
| 真实 Catalog 建表 | 独立运行操作 | `event.*` / `state.*` 建表与本轮文档工作分开。 |
| Profile 数值、风险预算、市场结论 | 人工 / 研究决策 | 不猜数值，不用代码代替决定或校准。 |
| P7 / P12 执行开关 | 明确关闭 | 默认关闭；代码存在不构成启用授权。 |
| 全仓门禁与 Phase 验收 | 验证 / 验收 | 门禁每次整合后复跑（结果见 `PROJECT_STATUS.md`）；Phase 验收独立。 |

## 执行顺序

1. 全仓门禁（pytest / ruff / format / mypy）在 `phase/1` 全绿后，经 PR 合入 `main`。
2. 在与 `main` 一致的提交上重跑 E1-CAP-1（ADR-0108 §9(d)(e)），据结果关闭 E1-CAP-1 或另立 ADR；K 轴数据交给 D-META-AGE。
3. 逐 Phase 准备验收证据；运行操作（真实 Catalog 建表、P11 部署）与人工门照旧独立。

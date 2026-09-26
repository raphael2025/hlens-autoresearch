# Architecture Decision Records

模板：[0000-template.md](0000-template.md)。规则：[ADR-0001](0001-record-architecture-decisions.md)。

## 索引

| ADR | 标题 | 状态 |
|---|---|---|
| [0001](0001-record-architecture-decisions.md) | 使用 ADR 记录架构决策 | Accepted |
| [0002](0002-architecture-baseline.md) | 架构基线 | Accepted（第 5 条被 ADR-0006 取代） |
| [0003](0003-python-version-and-uv.md) | Python 3.13 + uv（D-06） | Accepted |
| [0004](0004-git-repository-baseline.md) | 本地 Git 仓库与 Bootstrap Baseline（D-07） | Accepted（第 3 条"暂不决定远程"被 ADR-0025 取代） |
| [0005](0005-research-production-boundary.md) | Research / Production Boundary（D-03） | Accepted（2026-09-23）；Promotion 链（Registry / Promotion 服务 / Equivalence Gate）已实施（B28，CODE_COMPLETE / DEBUG_PENDING） |
| [0006](0006-strategy-lifecycle.md) | Strategy Lifecycle v2（D-05） | Accepted（2026-09-23）；取代 ADR-0002 第 5 条；被 ADR-0053 补充（`VALIDATION → FAILED`） |
| [0007](0007-validation-architecture-three-layers.md) | 三层验证架构与两步冻结（D-09 结构部分） | Accepted（2026-09-23） |
| [0008](0008-contract-payload-immutability.md) | 契约载荷的只读表示与内容哈希载荷定义 | Accepted（2026-09-23，Codex 依授权批准方案 A） |
| [0009](0009-experiment-identity-binding.md) | 实验规格身份、运行标识与依赖内容绑定 | Accepted（2026-09-23，Codex 依授权批准方案 A） |
| [0010](0010-contract-construction-and-canonical-versioning.md) | 契约构造路径、规范版本语法、v1 顶层 shape gate 与 Schema 格式表达 | Accepted（2026-09-23，Codex 验收 B1/B2 后裁决） |
| [0011](0011-lifecycle-subject-authorization-and-time.md) | 生命周期主体一致性、授权有效期与时间顺序（D-17） | Accepted（2026-09-24，Codex 依 Raphael 授权批准） |
| [0012](0012-information-flow-and-kind-invariants.md) | 信息流白名单与 kind 判别字段（D-23） | Accepted（2026-09-24，Codex 依 Raphael 授权批准） |
| [0013](0013-deterministic-verdict-and-finite-numbers.md) | 确定性判定函数与数值合法性（D-19、D-20.1 ~ D-20.3） | Accepted（2026-09-24，Codex 依 Raphael 授权批准） |
| [0014](0014-validation-profile-structural-invariants.md) | Validation Profile 的普适结构不变量（D-20.4） | Accepted（2026-09-24，Codex 依 Raphael 授权批准） |
| [0015](0015-audit-identity-types-and-version-bindings.md) | 审计身份类型与版本绑定（D-21、D-22） | Accepted（2026-09-24，Codex 依 Raphael 授权批准） |
| [0016](0016-llmcall-content-bindings.md) | `LlmCall` 的最小完整登记（D-18） | Accepted（2026-09-24，Codex 依 Raphael 授权批准） |
| [0017](0017-provider-delivery-schedule.md) | Provider 接口的交付节奏（D-24，方案 B） | Accepted（2026-09-24，Codex 依 Raphael 授权批准） |
| [0018](0018-contract-value-semantic-identities.md) | 契约值对象的语义身份（D-26） | Accepted（2026-09-24，Codex 依 Raphael 授权批准）；已实施（C2c） |
| [0019](0019-lifecycle-evidence-minimum.md) | 生命周期证据的最小结构（D-27） | Accepted（2026-09-24，Codex 依 Raphael 授权批准）；已实施（C2d） |
| [0020](0020-approve-research-constitution-v1.md) | 发布 Research Constitution 1.0.0（原则零变化） | Accepted（2026-09-24，Raphael 明确批准，经 Codex 复核于 C4b 执行）；已实施 |
| [0021](0021-phase1-local-data-infrastructure.md) | Phase 1 本地数据基础设施（D-01、D-02、D-10） | Accepted（2026-09-24，Codex 依 Raphael 授权批准；A2 记录，复核 `6d53cf5` PASS）；核心本地基础设施已实施（C1 `file://` StorageAdapter、C2 PostgreSQL-backed PyIceberg Catalog、C3 生产表；未运行 NATS） |
| [0022](0022-phase1-market-and-execution-scope.md) | Phase 1 市场与执行边界（D-08） | Accepted（2026-09-24，Codex 依 Raphael 授权批准；A2 记录，复核 `6d53cf5` PASS）；归档与范围部分已实施（D0 下载、D1 解析、D2 revision；无交易能力）；REST 补尾待 D3（ADR-0027） |
| [0023](0023-bitemporal-revision-data.md) | 双时间与修订数据的 point-in-time 语义（D-28） | Accepted（2026-09-24，Codex 依 Raphael 授权批准；A2 记录，复核 `6d53cf5` PASS）；契约（B1）与 Raw 归档 revision 部分已实施（D2）；Canonical（E）与 PIT / manifest 执行（F）部分待实施 |
| [0024](0024-historical-tradable-universe.md) | 历史可交易 universe（D-31，依赖 0023） | Accepted（2026-09-24，Codex 依 Raphael 授权批准；A2 记录，复核 `6d53cf5` PASS）；契约与设计已交付（B2）；listing 历史采集与 universe 执行（E / F）待实施 |
| [0025](0025-private-github-remote-and-reviewed-progress-push.md) | 私有 GitHub 远程与复核后逐进度推送 | Accepted（2026-09-24，Codex 依 Raphael 明确指示批准；A2r 记录）；取代 ADR-0004 第 3 条 |
| [0026](0026-pyiceberg-core-extra-for-day-partitions.md) | 为按天分区写入加入 PyIceberg 官方 extra `pyiceberg-core`（D-32） | Accepted（2026-09-24，Codex 依 Raphael 授权裁决方案 A；D32 记录）；已实施（Cursor 锁依赖 `e40c285`、Claude C3-R1 转正 xfail，随 C3 验收） |
| [0027](0027-rest-raw-source-and-element-revisions.md) | REST 补尾的 Raw source / element revision、通道等价 precedence 与三跳 lineage（D-33） | Accepted（2026-09-25，Codex 依 Raphael 授权批准；复核 D3A-R1 `ed526f7` PASS，[审阅记录](../reviews/2026-09-25-d3a-adr-0027-acceptance.md)）；D-33 方案 A 生效；尚待实施（D3B 起逐批） |
| [0028](0028-dual-raw-canonical-lineage.md) | 双 Raw 通道进入 Canonical 的 revision 身份、三跳 lineage 与 precedence 映射（E0） | Accepted（2026-09-25，Raphael 批准方案 B：lineage 进 Canonical 身份，PIT 从固定 Raw 证据 snapshot 一对一映射，不新增表、不改契约）；尚待实施（E1） |
| [0029](0029-listing-history-source.md) | 首切片标的上市历史的来源（E2，D-E2） | Accepted（2026-09-25，方案 A；Claude 依 Raphael 授权决定） |
| [0030](0030-feature-provider-contract.md) | FeatureProvider 的 Protocol、DTO 与 provider-agnostic 契约测试（F4 前置） | Accepted（2026-09-25，方案 A；Claude 依 Raphael 授权决定） |
| [0031](0031-quality-evidence-gap-table.md) | 质量报告的证据缺口写入独立只追加表（D-QGAP） | Accepted（2026-09-25；Claude 依 Raphael 授权决定） |
| [0032](0032-archive-event-time-availability-assumption.md) | 历史归档的事件时间可用性假设（PIT 叠加层，D-HIST） | Accepted（2026-09-25；Raphael 批准） |
| [0033](0033-research-dataset-selection-table.md) | 物化 Research Dataset 选择表登记为生产表（DS-1） | Accepted（2026-09-25；Claude 依 Raphael 授权决定） |
| [0034](0034-knowledge-provider.md) | KnowledgeProvider 契约与本地知识库（Phase 0.5） | Accepted（2026-09-25；Claude 依 Raphael 授权决定） |
| [0043](0043-dynamic-strategy-router.md) | 动态策略路由框架（Phase 10，仅纸面） | Accepted（2026-09-25；Claude 依 Raphael 授权决定） |
| [0044](0044-event-bus-and-worker-jobs.md) | EventBusAdapter 契约、内存总线与 worker 幂等任务（Phase 11 地基） | Accepted（2026-09-25；Claude 依 Raphael 授权决定） |
| [0039](0039-state-strategy-research.md) | 状态 × 策略研究框架（Phase 6） | Accepted（2026-09-25；Claude 依 Raphael 授权决定） |
| [0040](0040-hypothesis-generation-and-llm.md) | 假设生成、组合算子、预登记账本与 LLMProvider 契约（Phase 7） | Accepted（2026-09-25；Claude 依 Raphael 授权决定） |
| [0038](0038-strategy-risk-backtest-providers.md) | Strategy / Risk / Backtest Provider 契约、回测器 v1 与研究策略库（Phase 5） | Accepted（2026-09-25；Claude 依 Raphael 授权决定） |
| [0036](0036-event-provider-contract.md) | EventProvider 契约、事件执行器与首批事件 / 交互算子（Phase 3） | Accepted（2026-09-25；Claude 依 Raphael 授权决定）；FRAMEWORK_IMPLEMENTED / NOT_VALIDATED |
| [0037](0037-outcome-engine-and-minimal-validation-pipeline.md) | Outcome Engine、成本模型 v1 与最小 Validation Pipeline（Phase 4 框架） | Accepted（2026-09-25；Claude 依 Raphael 授权决定）；FRAMEWORK_IMPLEMENTED / NOT_VALIDATED |
| [0041](0041-validation-robustness.md) | G4 稳健性套件、回溯审计、Phase 4 复审修正与策略验证接线（Phase 8 框架） | Accepted（2026-09-25；Claude 依 Raphael 授权决定）；FRAMEWORK_IMPLEMENTED / NOT_VALIDATED |
| [0042](0042-synthetic-market-provider.md) | SyntheticMarketProvider 契约与随机游走生成器（Phase 9） | Accepted（2026-09-25；Claude 依 Raphael 授权决定） |
| [0045](0045-strategy-evolution.md) | 策略演化算子与谱系（Phase 12） | Accepted（2026-09-25；Claude 依 Raphael 授权决定） |
| [0046](0046-simulated-execution-service.md) | 独立执行服务（仅模拟）、Kill Switch、二道风控与执行阶梯（Phase 13 框架；无实盘） | Accepted（2026-09-25；Claude 依 Raphael 授权决定，红线除外）；FRAMEWORK_IMPLEMENTED / NOT_VALIDATED |
| [0047](0047-migration-framework.md) | 技术迁移框架——金标准重跑与 Adapter 一致性（Phase 14） | Accepted（2026-09-25；Claude 依 Raphael 授权决定） |
| [0048](0048-api-and-web-console.md) | API 服务与研究控制台骨架（apps/api、apps/web） | Accepted（2026-09-25；Claude 依 Raphael 授权决定） |
| [0049](0049-continuous-research-loop.md) | 持续研究循环：调度、预算、生命周期护栏、审计与劣化监控（Phase 11 框架） | Accepted（2026-09-25；Claude 依 Raphael 授权决定，红线除外）；FRAMEWORK_IMPLEMENTED / NOT_VALIDATED |
| [0050](0050-loop-audit-record-contract.md) | 持续研究循环审计记录的版本化契约（Phase 11，只追加） | Accepted（2026-09-26；Claude 依 Raphael 授权决定，红线除外）；FRAMEWORK_IMPLEMENTED / NOT_VALIDATED |
| [0051](0051-listing-history-assumption.md) | 上市历史的"观察状态回填"假设（PIT 叠加层，D-LIST） | Proposed（2026-09-26；Raphael 暂缓） |
| [0052](0052-validation-contract-completion.md) | 验证契约补全：精确小数、Profile 新字段与负对照独立阈值（D-FLOAT、D-PFIELDS、D-CTRL） | Accepted（2026-09-26；Raphael 批准）；已实施于 2.1.0（按记录版本重放、§1 ~ §3 字段与研究侧取值）；CODE_COMPLETE / DEBUG_PENDING |
| [0053](0053-validation-failed-transition.md) | 增加生命周期转移 VALIDATION → FAILED（D-VFAIL） | Accepted（2026-09-26；Raphael 批准）；已实施 |
| [0054](0054-partial-fill-carry-over.md) | 回测契约扩展：成交量上限剩余量跨 bar 结转（D-PARTIAL） | Accepted（2026-09-26；Raphael 批准）；re-declared at 2.1.0（2026-09-26）；CODE_COMPLETE / DEBUG_PENDING |
| [0055](0055-knowledge-tags-assets.md) | 知识库按标签 / 资产检索（Phase 0.5；契约 2.2.0） | Accepted（2026-09-26；Codex 基于组合代码 `c08c589` 与最终全量门禁复核接受；Amendment 1）；CODE_COMPLETE / DEBUG_PENDING；Phase 0.5 未验收（种子无具名人工审阅分类） |
| [0056](0056-event-table.md) | 物理 Event 表 `event.events`（Phase 3，只追加，按 `result_hash` 幂等） | Accepted（2026-09-26；Claude 依 Raphael 明确授权决定）；CODE_COMPLETE / DEBUG_PENDING |
| [0057](0057-event-request-subject.md) | 事件请求的标的键 `EventRequest.subject`（P3-MULTISYM，additive） | Accepted（2026-09-26；Claude 依 Raphael 授权决定）；re-declared at 2.1.0（2026-09-26）；CODE_COMPLETE / DEBUG_PENDING |
| [0058](0058-knowledge-reviewed-write-path.md) | 知识库经人工审阅的写入路径（Python API / CLI，无 HTTP 写端点） | Accepted（2026-09-26；Claude 依 Raphael 明确授权） |
| [0059](0059-cross-asset-check-for-cross-sectional-strategies.md) | G4 跨资产检查（C-R3）对横截面策略的适用方式 | Accepted（2026-09-26；Claude 依 Raphael 授权决定，含红线）；C + A 已实施，CODE_COMPLETE / DEBUG_PENDING |
| [0060](0060-market-benchmark-rule-semantics.md) | C-T4 市场基准规则与反向对照的语义（报告项，不作否决） | Accepted（2026-09-26；Claude 依 Raphael 授权）；已实施（B35 / B39），CODE_COMPLETE / DEBUG_PENDING |
| [0061](0061-interaction-dsl.md) | 事件交互 DSL（数据表达式树，编译到已审阅算子） | Accepted（2026-09-26；Claude 依 Raphael 授权）；已实施（B36），CODE_COMPLETE / DEBUG_PENDING |
| [0062](0062-profile-freeze-registry.md) | Validation Profile 冻结登记（file-backed 追加式）作为 Promotion 的权威冻结来源 | **Proposed**（2026-09-27；Codex 依 Raphael 授权决定，Q1 = A、Q2 = A）；B56 实施中，待 Codex 最终复核 |
| [0035](0035-state-provider-contract.md) | StateProvider 的 Protocol、DTO、执行器与首批状态（Phase 2 框架） | Accepted（2026-09-25；Claude 依 Raphael 授权决定）；FRAMEWORK_IMPLEMENTED / NOT_VALIDATED |

> ADR-0011 ~ 0017 是 Phase 0 批次 B3 的 Codex 技术裁决（D-17 ~ D-25）的书面形式，
> 于 2026-09-24 由 Codex 依 Raphael 的授权全部接受。实现按
> 0011 → 0012 → 0013 → 0014 → 0015 → 0016 → 0017 的串行批次进行，每批一个独立 commit。
> **实现进度**：0011 ~ 0017 全部已实施，且 0011 ~ 0016 均已由 Codex 独立复验；
> 0016 补齐的是 `LlmCall` 的**登记结构**（`ContentBlobRef` 使 current Schema 增至 38 份），
> 内容的可取回性与内容 - 哈希一致仍是存储层 / Registry 的未实现义务，
> `06-experiment.md` §2 的"完整输入输出"要求因此**仍未完全满足**；
> 0017 的实施是 **docs-only 的交付节奏落地**：语义冻结与文档同步已完成，
> 按方案 B 不产生任何 Provider 代码，`core/contracts/` 中的 Provider Protocol 数量**仍为 0**，
> 各类接口随首次消费它的 Phase 交付。

> ADR-0018 / 0019 来自 Phase 0 关闭复审 C1（[审查记录](../reviews/2026-09-24-phase0-closing-review-c1.md)，
> 结论 `FIX_BEFORE_CLOSE`）的 Codex 裁决 D-26 / D-27，由批次 C2a 起草、C2b 补齐
> （ADR-0018 增加 `GitCodeRevision` 代码身份与 `core/` 比较点完整盘点）后于 2026-09-24 **Accepted**。
> ADR-0018 已于 C2c 实施、ADR-0019 已于 C2d 实施（ADR 正文中的"尚待实施"是接受时的状态，正文不回写）；
> 修复后关闭复验 C3 已完成，结论 `READY_FOR_HUMAN_CONSTITUTION_GATE`
> （[审查记录](../reviews/2026-09-24-phase0-closing-review-c3.md)）。
>
> ADR-0020 把 Constitution 从 `0.2.0-draft` 发布为 `1.0.0 / Approved`，原则正文零变化（第一至第九章 sha256 不变）；
> 依据 Raphael 2026-09-24 的持续授权（来源与限定见 ADR-0020），C4a 以 Proposed 提出、Codex 复核后于 C4b 接受并发布。
> 剩余顺序门已由 C5 执行：Phase 0 正式关闭、`phase/0` fast-forward 合并进 `main`、轻量 tag `phase-0-complete`。
> **Phase 0 已完成**；契约 `2.0.0` 随合并视为已发布（D-25：此后破坏性变化必须升 major）。Phase 1 已于 2026-09-24 开启，
> D-01 / D-02 / D-10 → ADR-0021、D-08 → ADR-0022、D-28 → ADR-0023、D-31 → ADR-0024（A1 起草、A1r / A1r2 按 Codex 两次复核退回意见修正，Codex 复核 A1r2 提交 `6d53cf5` 结论 PASS 后于 2026-09-24 **Accepted**，由 A2 记录并同步冻结文档 `03-data.md`；实施进度见上表各行，均为部分实施，不宣称整份 ADR 完成）。架构决策子阶段已关闭；实施按 roadmap Phase 1 的恢复序列逐批进行，Provider 接口 / DTO / Schema / contract tests 先于实现（ADR-0017）。

## 待决事项（ARCHITECTURE_DECISION_REQUIRED）

以下冲突或空白在 Architecture Bootstrap 中被发现，**未自行解决**。每一项决定后应形成 ADR。

| ID | 问题 | 选项 | 阻塞 / 状态 |
|---|---|---|---|
| D-01 | **Iceberg Catalog 选择**。Iceberg 必须有 Catalog；若用 PostgreSQL 作 SQL Catalog，则 Control Plane 与 Data Plane 共享数据库，模糊 P3/P8 边界。另外 Python 生态对 Iceberg 写入（PyIceberg）与 DuckDB 的 Iceberg 写支持成熟度不一。 | (a) PG 独立 database 作 SQL Catalog；(b) REST Catalog（如 Lakekeeper / Polaris）；(c) Phase 1 先用"Parquet + 自有快照清单"，Phase 2 再上 Iceberg | Phase 1；→ [ADR-0021](0021-phase1-local-data-infrastructure.md) Accepted（PostgreSQL-backed PyIceberg SQL Catalog，独立库） |
| D-02 | **没有 Docker 时的对象存储**。S3 兼容存储（MinIO）通常以容器运行，但 Docker 未安装且本阶段禁止安装。 | (a) Phase 0–1 用本地文件系统作为 StorageAdapter；(b) 授权安装 Docker 后使用 MinIO；(c) 使用 MinIO 单二进制（非容器） | Phase 1；→ [ADR-0021](0021-phase1-local-data-infrastructure.md) Accepted（WSL ext4 `file://` warehouse，不装 Docker / MinIO） |
| D-03 | ✅ **已决定（ADR-0005）** 研究 / 生产边界。相关实现选择 Q-1/Q-2/Q-7 仍按 ADR 保持开放。 | 见 ADR-0005 | → ADR-0005 Accepted |
| D-04 | **Validation 时序冲突**。Phase 8 才是 Validation & Robustness，但 Phase 5–7 已产生实验与晋升判断。 | (a) 最小验证门在 Phase 4 交付，Phase 8 做扩展（当前 roadmap 采用此解释）；(b) 把 Phase 8 前移到 Phase 5 之前 | Phase 4 |
| D-05 | ✅ **已决定（ADR-0006）** Lifecycle 细节；Q-4～Q-6 按 ADR 保持开放，现有已接受规则继续适用。 | 见 ADR-0006 与 07-validation.md §3 | → ADR-0006 Accepted |
| D-06 | **Python 版本**。系统 Python 为 3.14.4；部分科学计算 / 数据库驱动 / PyIceberg 等对最新版本支持可能滞后。 | (a) 用 uv 固定 3.12 或 3.13 的项目内解释器；(b) 直接使用 3.14 | ✅ 已决定 → ADR-0003 |
| D-07 | **Git 仓库**。项目目录尚未 `git init`（本阶段未授权）；复现元组依赖 commit SHA。另需决定远程托管位置。 | 授权后 `git init`（不修改全局 Git 配置） | ✅ 已决定 → ADR-0004；远程 → [ADR-0025](0025-private-github-remote-and-reviewed-progress-push.md)（私有 GitHub，Codex 复核后推送） |
| D-08 | **市场与执行范围**。覆盖哪些交易所 / 标的 / 频率（现货、永续、期权、链上）？Phase 13 是否包含实盘，由谁授权，风险预算上限？ | 需用户定义 | Phase 1 / 13；→ [ADR-0022](0022-phase1-market-and-execution-scope.md) Accepted（研究数据范围；实盘仍需 Raphael 独立授权） |
| D-09 | **Constitution 数值**。TBD-1 至 TBD-5。结构部分（H-1、H-2）已定 → ADR-0007 Accepted；**数值仍未批准**，将在 Phase 4 校准后按 Profile 版本冻结。 | 需用户批准 | → 提案 `docs/research/proposals/d09-validation-threshold-proposal.md`（未批准） |
| D-10 | **NATS 引入时机**。早期单机研究不需要事件总线，过早引入增加运维负担。 | (a) Phase 1–6 用进程内任务队列（接口为 EventBusAdapter），Phase 7/11 引入 NATS；(b) 从 Stage 2 开始即引入 | Phase 1；→ [ADR-0021](0021-phase1-local-data-infrastructure.md) Accepted（Phase 1 ~ 6 不运行 NATS，最迟 Phase 11 前引入） |
| D-11 | ✅ **已决定（ADR-0008）** 契约只读载荷与哈希边界；下一次未发布的 2.0.0 与旧版本只读兼容。 | 方案 A | Phase 0；Accepted，B1 已实施 |
| D-12 | ✅ **已决定（ADR-0009）** 实验身份与复现绑定；种子、运行标识和直接依赖内容覆盖。 | 方案 A | Phase 0；Accepted，B2 已实施（契约版本号提升到 2.0.0，该版本尚未发布） |
| D-13 | ✅ **已决定（ADR-0010）** `model_copy(update=...)` 必须重新走完整校验；`model_construct` 明确为不受支持的可信数据逃生口。 | Codex 裁决 | Phase 0；Accepted，已实施 |
| D-14 | ✅ **已决定（ADR-0010）** 唯一、ASCII、完整 SemVer 2.0.0 语法；major 从已验证的正则分组读取。 | Codex 裁决 | Phase 0；Accepted，已实施 |
| D-15 | ✅ **已决定（ADR-0010）** v1 只读入口增加基于已提交快照的顶层 shape gate，快照缺失 fail closed。 | Codex 裁决 | Phase 0；Accepted，已实施 |
| D-16 | ✅ **已决定（ADR-0010）** 三类映射字段的键值格式必须出现在导出的 JSON Schema 中，且与运行时同源。 | Codex 裁决 | Phase 0；Accepted，已实施 |
| D-17 | **生命周期审批、主体归属、授权有效期与时间顺序**：失败的 revalidation 是否可以自动退役；历史、授权与 Risk Gate 的主体和时序如何约束；`live_execution_enabled` 这类自报字段是否成立。 | Codex 裁决 → [ADR-0011](0011-lifecycle-subject-authorization-and-time.md) | Phase 0；Accepted（2026-09-24），已实施（ADR-0011 批次） |
| D-18 | **`LlmCall` 的最小完整登记**：一次 LLM 调用要登记哪些槽位；只存三个哈希是否足以构成可复核的审计记录。 | Codex 裁决 → [ADR-0016](0016-llmcall-content-bindings.md) | Phase 0；Accepted（2026-09-24），已实施（ADR-0016 批次）；可取回性与内容一致性按 ADR 延期 |
| D-19 | **整体 Verdict 与门结果的关系**：如何在契约层排除"证据不足即 PASS"。 | Codex 裁决 → [ADR-0013](0013-deterministic-verdict-and-finite-numbers.md) | Phase 0；Accepted（2026-09-24），已实施（ADR-0013 批次） |
| D-20 | **数值合法性与结构合法性**：NaN / ±Infinity 的拒绝时点，阈值与来源的配对，两个概率型阈值的结构范围（D-20.1 ~ D-20.3）；Validation Profile 的普适结构不变量（D-20.4）。 | Codex 裁决 → [ADR-0013](0013-deterministic-verdict-and-finite-numbers.md)（D-20.1 ~ D-20.3）、[ADR-0014](0014-validation-profile-structural-invariants.md)（D-20.4） | Phase 0；Accepted（2026-09-24），已实施（D-20.1 ~ D-20.3 在 ADR-0013 批次，D-20.4 在 ADR-0014 批次） |
| D-21 | **审计哈希字段的类型统一**：内容哈希、Git OID 与不透明 ID 如何分开表达。 | Codex 裁决 → [ADR-0015](0015-audit-identity-types-and-version-bindings.md) | Phase 0；Accepted（2026-09-24），已实施（ADR-0015 批次） |
| D-22 | **版本绑定的表达**：Constitution 版本语法、Profile 复合引用是否继续塞进 `*_version` 字符串、选择依据的重复字段。 | Codex 裁决 → [ADR-0015](0015-audit-identity-types-and-version-bindings.md) | Phase 0；Accepted（2026-09-24），已实施（ADR-0015 批次） |
| D-23 | **信息流白名单与 kind 判别字段**：Outcome 与 outcome-zone 数据能否进入 Feature / State / Event / Strategy 输入；`kind` 能否被覆盖。 | Codex 裁决 → [ADR-0012](0012-information-flow-and-kind-invariants.md) | Phase 0；Accepted（2026-09-24），已实施（ADR-0012 批次） |
| D-24 | **Provider 接口交付范围漂移**：审计时 `05-plugin.md` §3 写"签名在 Phase 0 定义"，roadmap 验收表没有该条。 | Codex 裁决：方案 B → [ADR-0017](0017-provider-delivery-schedule.md) | Phase 0；Accepted（2026-09-24），已实施（ADR-0017 批次，docs-only；Provider Protocol 数仍为 0） |
| D-25 | **这批收窄是否需要升 major**：`2.0.0` 尚未发布（只在 `phase/0`、未合并 `main`、无 tag / 远程发布 / v2 数据登记）。 | Codex 裁决：继续属于未发布的 `2.0.0`，不升 major；发布后做同类改变必须升 major | Phase 0；Accepted（2026-09-24），写入 ADR-0011 ~ 0016 各自的版本小节 |
| D-26 | **契约值对象的语义身份**：Profile 选择键、`Ref` 目标与 Git 代码修订的身份是否包含嵌套信封 `schema_version`；选择规则的判重与查询是否同源（C1 F1 / F3）。 | Codex 裁决 → [ADR-0018](0018-contract-value-semantic-identities.md)：三类显式语义身份，全局相等与内容哈希不变 | Phase 0；Accepted（2026-09-24），已实施（C2c） |
| D-27 | **生命周期证据的最小结构**：每条转移是否必须带非空证据引用；是否在契约层要求职责分离（C1 F5）。 | Codex 裁决 → [ADR-0019](0019-lifecycle-evidence-minimum.md)：证据至少一项且非空；**不**做自报职责分离 | Phase 0；Accepted（2026-09-24），已实施（C2d） |
| D-28 | **迟到 / 修订数据的 point-in-time 语义**：可用时间如何表达数据的迟到与修订（revision / as-of / vintage）；`event_time + declared_latency` 是否足够（C1 F4，原 R08）。 | Codex 裁决 → [ADR-0023](0023-bitemporal-revision-data.md)：双轴六字段、append-only revision、maximal-head PIT | Phase 1；Accepted（2026-09-24）；契约与 Raw 归档 revision 已实施，Canonical / PIT 待实施 |
| D-29 | **`apps/worker` 与研究代码的边界**：01-system.md 让 worker 运行实验，而 `apps/` 不得 import `research/`；实验如何被运行而不越过边界（C1 F4，原 R15）。 | 未决；本登记不选方案 | 首次实现 worker / 实验运行前决定，最迟 Phase 5 前 |
| D-30 | **C-L5 的跨对象校验执行点**：`embargo >= 最长 Outcome horizon` 需要同时看到 Profile 与 Outcome，由谁、在何时校验（C1 F4）。 | 未决；本登记不选方案 | Phase 4 前决定 |
| D-31 | **C-L4 历史可交易标的池**：上市 / 下架有效期如何表达，`Instrument` 是否需要有效期（C1 F4）。 | Codex 裁决 → [ADR-0024](0024-historical-tradable-universe.md)：静态 `Instrument` + 双轴 listing 历史 + 版本化 `UniverseSelectionSpec` | Phase 1；Accepted（2026-09-24）；契约已交付，listing 历史与 universe 执行待实施（依赖 ADR-0023） |
| D-32 | **按天分区写入缺依赖**：锁定的 PyIceberg 0.12 写入 `day(...)` 分区需要官方 extra `pyiceberg-core`，它不在 03-data.md §6.1 的依赖中，四张冻结表无法写入（C3 发现）。 | Codex 裁决：方案 A → [ADR-0026](0026-pyiceberg-core-extra-for-day-partitions.md)：加入该 extra；不改分区（B）、不改写入路径（C） | Phase 1；Accepted（2026-09-24），已实施（随 C3 验收） |
| D-33 | **归档 ↔ REST 的同一观察如何汇合**：同一 `observation_key` 同时有归档交付与 REST 交付的 revision 时，按 ADR-0023 §5 是 competing heads → 数据集 fail closed。归档终将覆盖曾由 REST 补过的区间，不裁决即等于禁止 REST 补尾（D3A 发现）。 | Codex 选择方案 A → [ADR-0027](0027-rest-raw-source-and-element-revisions.md) §4：项目定义的版本化规范内容投影逐字段相等时，在独立证据表 `raw.binance_spot_precedence_evidence` 追加 evidence-only 边（归档 revision 取代 REST revision）；不等 / 不可比较则无边、fail closed；不是来源声明的先后 | Phase 1 D3；已决定（ADR-0027 Accepted 2026-09-25），D-33 方案 A 生效；尚待实施（D3B 起） |

> **编号说明**（2026-09-24 由 Codex 最终确认）：D-17 ~ D-25 连续且唯一。
> D-26 ~ D-31 由 C1 复审后的 Codex 裁决新增，与 D-25 连续：D-26 / D-27 各对应一份已接受且已实施的 ADR（C2c / C2d），
> D-28 / D-31 已于 2026-09-24 分别由 ADR-0023 / 0024 决定（Accepted，部分实施）；D-29 / D-30 仍是**已登记的开放问题**，不是已决定事项。
> [ADR-0016](0016-llmcall-content-bindings.md) 是 **D-18**（`LlmCall` 的最小完整登记，
> 内部决定 D-18.1 ~ D-18.3）。[ADR-0014](0014-validation-profile-structural-invariants.md)
> **不是新 D 编号**：它是 **D-20 的结构合法性扩展（D-20.4）**，
> D-20.1 ~ D-20.3 仍在 [ADR-0013](0013-deterministic-verdict-and-finite-numbers.md)。
> **D-32** 由 C3 发现、已由 ADR-0026 决定并实施；**D-33** 由 D3A 发现，Codex 选方案 A（语义见
> [ADR-0027](0027-rest-raw-source-and-element-revisions.md) §4），随该 ADR 于 2026-09-25 接受而生效。

## 冲突记录

| ID | 冲突 | 涉及 | 状态 |
|---|---|---|---|
| C-1 | D-03 边界链为 Paper Trading → Production Candidate；D-05 生命周期为 PRODUCTION_CANDIDATE → PAPER。两份 Raphael 规格顺序相反 | ADR-0005、ADR-0006 | ✅ 2026-09-21 Raphael 选 B：OOS → PAPER → PRODUCTION_CANDIDATE → ACTIVE |
| C-2 | D-05 规则 5（Phase 13 前 ACTIVE 仅为模拟）使 PAPER 与 ACTIVE 的区别不明确 | ADR-0006 | ✅ 2026-09-21 Raphael 选 A：PAPER = 单策略独立观察；ACTIVE = 组合 / Router 正式启用，带 execution_mode；不设 LIVE 状态 |

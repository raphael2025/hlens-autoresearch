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
| [0023](0023-bitemporal-revision-data.md) | 双时间与修订数据的 point-in-time 语义（D-28） | Accepted（2026-09-24，Codex 依 Raphael 授权批准；A2 记录，复核 `6d53cf5` PASS）；Raw revision、Canonical 与 PIT / manifest 的实现候选已进入本地 `main`；Phase 1 尚未整体验收，见 `PROJECT_STATUS.md` |
| [0024](0024-historical-tradable-universe.md) | 历史可交易 universe（D-31，依赖 0023） | Accepted（2026-09-24，Codex 依 Raphael 授权批准；A2 记录，复核 `6d53cf5` PASS）；listing / universe 的实现候选已进入本地 `main`；PIT 规格可显式绑定 ADR-0051 上市回填假设（政策 1.1.0）；不绑定时仍只使用观察到的上市历史；Phase 1 尚未整体验收 |
| [0025](0025-private-github-remote-and-reviewed-progress-push.md) | 私有 GitHub 远程与复核后逐进度推送 | Accepted（2026-09-24，Codex 依 Raphael 明确指示批准；A2r 记录）；取代 ADR-0004 第 3 条 |
| [0026](0026-pyiceberg-core-extra-for-day-partitions.md) | 为按天分区写入加入 PyIceberg 官方 extra `pyiceberg-core`（D-32） | Accepted（2026-09-24，Codex 依 Raphael 授权裁决方案 A；D32 记录）；已实施（Cursor 锁依赖 `e40c285`、Claude C3-R1 转正 xfail，随 C3 验收） |
| [0027](0027-rest-raw-source-and-element-revisions.md) | REST 补尾的 Raw source / element revision、通道等价 precedence 与三跳 lineage（D-33） | Accepted（2026-09-25，Codex 依 Raphael 授权批准；复核 D3A-R1 `ed526f7` PASS，[审阅记录](../reviews/2026-09-25-d3a-adr-0027-acceptance.md)）；D-33 方案 A 生效；D3B～D3E 已实现并记录验收，Phase 1 仍待整体验收 |
| [0028](0028-dual-raw-canonical-lineage.md) | 双 Raw 通道进入 Canonical 的 revision 身份、三跳 lineage 与 precedence 映射（E0） | Accepted（2026-09-25，Raphael 批准方案 B：lineage 进 Canonical 身份，PIT 从固定 Raw 证据 snapshot 一对一映射，不新增表、不改契约）；E1 实现候选在本地 `main`，但 500k resume / replay 超过 32 MiB 门槛，Phase 1 仍阻断 |
| [0029](0029-listing-history-source.md) | 首切片标的上市历史的来源（E2，D-E2） | Accepted（2026-09-25，方案 A；Claude 依 Raphael 授权决定） |
| [0030](0030-feature-provider-contract.md) | FeatureProvider 的 Protocol、DTO 与 provider-agnostic 契约测试（F4 前置） | Accepted（2026-09-25，方案 A；Claude 依 Raphael 授权决定） |
| [0031](0031-quality-evidence-gap-table.md) | 质量报告的证据缺口写入独立只追加表（D-QGAP） | Accepted（2026-09-25；Claude 依 Raphael 授权决定） |
| [0032](0032-archive-event-time-availability-assumption.md) | 历史归档的事件时间可用性假设（PIT 叠加层，D-HIST） | Accepted（2026-09-25；Raphael 批准） |
| [0033](0033-research-dataset-selection-table.md) | 物化 Research Dataset 选择表登记为生产表（DS-1） | Accepted（2026-09-25；Claude 依 Raphael 授权决定） |
| [0034](0034-knowledge-provider.md) | KnowledgeProvider 契约与本地知识库（Phase 0.5） | Accepted（2026-09-25；Claude 依 Raphael 授权决定） |
| [0035](0035-state-provider-contract.md) | StateProvider 的 Protocol、DTO、执行器与首批状态（Phase 2 框架） | Accepted（2026-09-25；Claude 依 Raphael 授权决定）；FRAMEWORK_IMPLEMENTED / NOT_VALIDATED |
| [0036](0036-event-provider-contract.md) | EventProvider 契约、事件执行器与首批事件 / 交互算子（Phase 3） | Accepted（2026-09-25；Claude 依 Raphael 授权决定）；FRAMEWORK_IMPLEMENTED / NOT_VALIDATED |
| [0037](0037-outcome-engine-and-minimal-validation-pipeline.md) | Outcome Engine、成本模型 v1 与最小 Validation Pipeline（Phase 4 框架） | Accepted（2026-09-25；Claude 依 Raphael 授权决定）；FRAMEWORK_IMPLEMENTED / NOT_VALIDATED |
| [0038](0038-strategy-risk-backtest-providers.md) | Strategy / Risk / Backtest Provider 契约、回测器 v1 与研究策略库（Phase 5） | Accepted（2026-09-25；Claude 依 Raphael 授权决定） |
| [0039](0039-state-strategy-research.md) | 状态 × 策略研究框架（Phase 6） | Accepted（2026-09-25；Claude 依 Raphael 授权决定） |
| [0040](0040-hypothesis-generation-and-llm.md) | 假设生成、组合算子、预登记账本与 LLMProvider 契约（Phase 7） | Accepted（2026-09-25；Claude 依 Raphael 授权决定） |
| [0041](0041-validation-robustness.md) | G4 稳健性套件、回溯审计、Phase 4 复审修正与策略验证接线（Phase 8 框架） | Accepted（2026-09-25；Claude 依 Raphael 授权决定）；D-30 校验点已由 G0 / G1 接线解决；FRAMEWORK_IMPLEMENTED / NOT_VALIDATED |
| [0042](0042-synthetic-market-provider.md) | SyntheticMarketProvider 契约与随机游走生成器（Phase 9） | Accepted（2026-09-25；Claude 依 Raphael 授权决定） |
| [0043](0043-dynamic-strategy-router.md) | 动态策略路由框架（Phase 10，仅纸面） | Accepted（2026-09-25；Claude 依 Raphael 授权决定） |
| [0044](0044-event-bus-and-worker-jobs.md) | EventBusAdapter 契约、内存总线与 worker 幂等任务（Phase 11 地基） | Accepted（2026-09-25；Claude 依 Raphael 授权决定） |
| [0045](0045-strategy-evolution.md) | 策略演化算子与谱系（Phase 12） | Accepted（2026-09-25；Claude 依 Raphael 授权决定） |
| [0046](0046-simulated-execution-service.md) | 独立执行服务（仅模拟）、Kill Switch、二道风控与执行阶梯（Phase 13 框架；无实盘） | Accepted（2026-09-25；Claude 依 Raphael 授权决定，红线除外）；FRAMEWORK_IMPLEMENTED / NOT_VALIDATED |
| [0047](0047-migration-framework.md) | 技术迁移框架——金标准重跑与 Adapter 一致性（Phase 14） | Accepted（2026-09-25；Claude 依 Raphael 授权决定） |
| [0048](0048-api-and-web-console.md) | API 服务与研究控制台骨架（apps/api、apps/web） | Accepted（2026-09-25；Claude 依 Raphael 授权决定） |
| [0049](0049-continuous-research-loop.md) | 持续研究循环：调度、预算、生命周期护栏、审计与劣化监控（Phase 11 框架） | Accepted（2026-09-25；Claude 依 Raphael 授权决定，红线除外）；FRAMEWORK_IMPLEMENTED / NOT_VALIDATED |
| [0050](0050-loop-audit-record-contract.md) | 持续研究循环审计记录的版本化契约（Phase 11，只追加） | Accepted（2026-09-26；Claude 依 Raphael 授权决定，红线除外）；FRAMEWORK_IMPLEMENTED / NOT_VALIDATED |
| [0051](0051-listing-history-assumption.md) | 上市历史的"观察状态回填"假设（PIT 叠加层，D-LIST） | Accepted（2026-09-28；Claude PM 依 Raphael 授权接受，原 2026-09-26 暂缓）；两期均已实现；政策 1.1.0（`e4bb050`）已填入 BTCUSDT / ETHUSDT 下界 2017-08-17 |
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
| [0062](0062-profile-freeze-registry.md) | Validation Profile 冻结登记（file-backed 追加式）作为 Promotion 的权威冻结来源 | Accepted（2026-09-27；Codex 依 Raphael 授权决定并接受，Q1 = A、Q2 = A）；已实施（B56），CODE_COMPLETE / DEBUG_PENDING；登记为空、Profile 数值未冻结，不等于 Phase 4 / 5 验收 |
| [0063](0063-local-asgi-runtime-uvicorn.md) | 本机只读研究 API 的 ASGI 运行时：Uvicorn（仅 127.0.0.1，单 worker，可选 `api-server` 依赖） | Accepted（2026-09-27；Codex 决定）；已实施（B65），CODE_COMPLETE / DEBUG_PENDING；只剩安装与两项真实 Uvicorn 子进程测试待 Raphael 按 H12 批准 |
| [0064](0064-g4-capacity-volume-source-consistency.md) | G4 容量检查的成交量来源一致性：数据集路径上 `bar_volume` 与已执行 `PriceBar.volume` 冲突即 INCONCLUSIVE（`bar_volume_source_mismatch`） | Accepted（2026-09-27；Codex 选 B66 选项 A 并接受措辞）；已实施（B66），CODE_COMPLETE / DEBUG_PENDING |
| [0065](0065-g4-capacity-carry-over-unfilled.md) | G4 容量检查遇到结转未成交余量时失败关闭（数据集路径，`carry_over_unfilled`） | Accepted（2026-09-27；Codex 决定 B67 方案 A）；已实施（B67），CODE_COMPLETE / DEBUG_PENDING；不是 Phase 4 / 5 验收 |
| [0066](0066-explicit-event-table-operator.md) | Event 表独立、显式的操作命令；不接入 Phase 1 或自动 provisioning | Accepted（2026-09-27；Codex 依 Raphael 对模块完成与技术决策的明确授权决定）；命令已随 PR #6 合并，定向检查通过；真实 catalog 建表仍须单独授权 |
| [0067](0067-p11-degradation-evidence-operator.md) | Phase 11 显式劣化检查的 evidence / Profile freeze 绑定 | Accepted（2026-09-27；Codex 依 Raphael 本轮授权决定）；实现已进入本地 `main@498250a`、未统一验收，Phase 11 未验收 |
| [0068](0068-phase7-typed-operator-plans.md) | Phase 7 类型化组合算子计划与执行边界 | Accepted（2026-09-27，Codex 依 Raphael 授权）；仅批准 non-runnable typed-plan 机制，六类 DSL 仍 fail closed，算子语义与 Provider lowering 未批准；后续由 0082/0088/0099/0100 放开 lowering 与（默认关闭的）执行 |
| [0069](0069-p12-combine-fail-closed.md) | Phase 12 `combine` 对风险与适用范围冲突失败关闭 | Accepted（2026-09-27；Codex 依 Raphael 本轮授权决定）；已实现但未测试，Phase 12 未验收 |
| [0070](0070-p7-partial-experiment-fail-stop.md) | P7 experiment 批次失败后停止续跑并要求人工审查 | Accepted（2026-09-27；Codex 依 Raphael 本轮授权决定）；LoopRecord 字节不变，已进入本地 `main@498250a`、未统一验收，Phase 7 / 11 未验收 |
| [0071](0071-p7-failed-round-review-packet.md) | P7 failed experiment round 的只读人工复核摘要 | Accepted（2026-09-27；Codex 依 Raphael 全权授权，经 Claude / Cursor / Codex 只读复核）；仅研究侧内存投影，不改持久格式、API 或 Schema；Ruff / format / mypy 通过，未跑测试 / build |
| [0072](0072-validation-phase-sequencing.md) | 最小验证门与稳健性阶段的交付顺序（D-04） | Accepted（2026-09-27；Codex 依 Raphael 全权授权）；Phase 4 先交付最小门，Phase 8 提供稳健性扩展；不改契约或数值 |
| [0073](0073-phase7-plan-admission-recovery.md) | P7 typed-plan 预登记与崩溃恢复 | Accepted（2026-09-28；Codex 依 Raphael 全权委托）；v4 writer/opener hardening 与同进程 admission lease / durable store write gate 已合入本地 main（`e3ac380`, `f91760b`, `d0ffdae`, `037dc03`, `4e7e7ae`, `60ae712`）；approval/round 串行化、轮间与 close 后拒写、只读 journal view 已独立静态复核；未跑测试 / build / lint / Phase 验收，不启用 operator |
| [0074](0074-synthetic-loop-operator.md) | 本机有限批次 synthetic Research Loop operator | Accepted（2026-09-28；Codex 依 Raphael 项目全权委托）；operator 专属 v5 identity、strict TOML parser、静态 Provider allowlist、有限轮次 CLI 与逐轮报告已合入本地 main；仅源码静态复核、未测试或验收。当前无冻结 Profile，不能生成合规运行配置 |
| [0075](0075-bounded-pyiceberg-snapshot-scan.md) | PyIceberg 固定快照的有界扫描路径 | Accepted（2026-09-28；Amendment 1 将 `max_int64` 与未绑定表的 pinned-view 读取纳入同一 bounded scan，格式 / delete 文件 fail closed）；已实现 `5332034`，未测试；E1-CAP-1 仍阻断 |
| [0076](0076-bounded-canonical-normalization-result.md) | 有界 Canonical 归一化结果接口 | Accepted（2026-09-28；Codex 依 Raphael 授权决定）；默认定长摘要，完整 ID 经显式有序 iterator；仅 infrastructure DTO；已实现 `5332034`，未测试 |
| [0077](0077-bounded-research-dataset-evidence.md) | 有界 Research Dataset 证据：v3 evidence manifest、有序 evidence streams 与定长 chunk commit | Accepted（2026-09-28；DQ-1 = A 由 Raphael 批准，DQ-2～8、10～12 已决定，DQ-9 待容量证据）；契约层（2.3.0 additive）已实现，infrastructure 层实施中；未测试 |
| [0078](0078-p7-lowered-output-completeness.md) | P7 lowered outputs 集合权威与完整性 | Accepted（2026-09-28；Codex 依 Raphael 授权决定）；`TypedPlan.nodes` 为权威全集；已实现 `79ea546`，未测试；算子仍 fail closed；后续由 0082/0088/0099/0100 放开 lowering 与（默认关闭的）执行 |
| [0079](0079-paper-deviation-declared-scope.md) | Paper deviation 与 P8 声明范围绑定 | Accepted（2026-09-28；Codex 依 Raphael 授权决定）；deviation payload 2.0.0 绑定 P8 ValidationReport 与 Profile 范围，1.0.0 仅作 legacy 读取；已实现 `1974610`，未测试 |
| [0080](0080-p11-authority-resolution.md) | Phase 11 ACTIVE、真实 source 与 metric 权威解析 | Superseded by ADR-0098（2026-09-30）；原 BLOCKED（2026-09-28）：缺权威 lifecycle head、source 身份与 metric 算法定义；不实现伪权威 resolver，loop 保持 synthetic-only |
| [0081](0081-versioned-report-payload-dtos.md) | Report payload DTO versions and compatibility | Accepted（2026-09-28）；API DTO registry 支持已知历史载荷版本；未知版本显式标记，不把兼容读取当作 schema 验收 |
| [0082](0082-p7-operator-semantics-and-lowering.md) | P7 六类组合算子的语义与 Provider lowering | Accepted（2026-09-28，三次接受记录）：interaction、transformation（standardize/difference/smooth）、temporal、conditioning、ensemble、negation 已实现纯 lowering；transformation 的 rank/quantile 已由 0099（时间序列）/ 0100（横截面）关闭；执行 Provider 见 0100（默认关闭）；未测试 |
| [0083](0083-p7-failed-round-retry.md) | P7 failed round 的 durable 人工重试 admission | Accepted；新增 v6 显式人工 retry admission；不启用自动 retry 或 P7 operator，旧版本持久化语义保持不变 |
| [0084](0084-live-interface-reservation.md) | 实盘接口预留（默认关闭） | Accepted（2026-09-28；Claude PM 依 Raphael 授权决定）：`LiveVenuePort` / `CredentialProvider` 端口与一律拒绝的 `UnconfiguredLiveVenue`；`LIVE_TRADING_ENABLED` 为常量 False；ladder 实盘档位仍拒绝；未测试 |
| [0085](0085-research-library-expansion.md) | 研究库扩展批次（非契约）：候选特征、状态、策略与风控政策 | Accepted（2026-09-28；Claude PM 依 Raphael 授权决定）；全部参数显式、无默认值；实施中 |
| [0086](0086-validation-lifecycle-closure.md) | 验证与生命周期收口：门集完整性、退役记录存储、Outcome 输入错误映射 | Accepted（2026-09-28；Claude PM 依 Raphael 授权决定）；实施中 |
| [0087](0087-plugin-manifest-discovery.md) | 插件 Manifest 与 entry-point 发现加载 | Accepted（2026-09-28；Claude PM 依 Raphael 授权决定）；实施中 |
| [0088](0088-contract-2-4-0-composition-extensions.md) | 契约 2.4.0（additive）：组合策略、事件 bar 规格、峰值权益、合成效应、波动率缩放屏障 | Accepted（2026-09-28；Claude PM 依 Raphael 授权决定）；已实现（契约 2.4.0，现为 2.5.0） |
| [0089](0089-state-iceberg-table.md) | State 物理表 `state.states` 与显式存储入口 | Accepted（2026-09-28）；Iceberg store / artifact store / 显式建表入口已实现；State 定向测试 27 passed，全仓回归仍进行 |
| [0091](0091-read-only-registry-integrity-audit.md) | 四类登记处的只读完整性审计与证据边界 | Accepted（2026-09-28；本轮 PM）；只读 API 与 CLI 待实施 |
| [0092](0092-trusted-validation-replay-for-promotion.md) | Promotion 的可信验证重放 Provider | Accepted（2026-09-28；Claude Code PM 依 Raphael 授权）；无内建 trusted Provider 时 Promotion 失败关闭，G5 不得二次开封 |
| [0093](0093-bounded-quality-report-evidence.md) | 有界、版本化质量报告证据流 | Accepted（2026-09-29；新报告使用固定大小 manifest 与内容寻址 streams，v1/v2 保持只读兼容；实现 / E1 验收待完成） |
| [0094](0094-bounded-pit-conflict-head-evidence.md) | 有界 PIT 冲突 heads 证据流 | Accepted（2026-09-30；契约 2.5.0 新增完整可重放冲突流，v2 与 2.3/2.4 manifest 保持兼容） |
| [0095](0095-worker-runtime-host.md) | Explicit trusted Worker runtime factory | Accepted（2026-09-29；原隔离分支编号 ADR-0093，收敛时重编号）；生产 host 每次单任务轮询，停止信号在当前结果 / ack 后退出 |
| [0096](0096-idempotent-ledger-read-results.md) | TrialLedger 精确重复登记的只读幂等确认 | Accepted（2026-09-30）；完全相同的登记 / 同一 attempt 可只读返回，不绕过 LLM 审阅，所有新写入仍受 admission gate 管控 |
| [0097](0097-pit-bounded-graph-scratch-index.md) | PIT bounded graph validation with an invocation-scoped SQLite scratch index | Accepted（2026-09-30；implementation and capacity evidence remain open） |
| [0098](0098-p11-authority-registry-and-resolver.md) | P11 生命周期权威登记处、真实 source 与 metric 解析 | Accepted（2026-09-30；PM）；取代 ADR-0080 的 BLOCKED 部分；实现待验收 |
| [0099](0099-p7-time-series-rank-quantile.md) | P7 transformation 之 rank / quantile 时间序列语义 | Accepted（2026-09-30；PM）；关闭 ADR-0082 最后 OPEN 项；仅纯 lowering；§5/§6 被 ADR-0100 §1/§2 修订 |
| [0100](0100-complete-remaining-foundation-code.md) | 补完剩余底层代码（Raphael 直接指令） | Accepted（2026-09-30）；P7 执行 Provider、横截面 rank/quantile、P11 指标与默认环境、ADR-0051 政策表、E1 有界化、P12 可选提案；运行开关默认关闭；修订 1：temporal 滞后 / 发生时刻窗口 / 上游哈希绑定（计划格式 1.3.0） |
| [0101](0101-dataset-v3-production-entry.md) | Dataset v3 生产入口接线 | Accepted（2026-10-02；PM，于 phase/1 重新接受）：显式 JSON `DatasetBuildProfile`（无默认值）、`infrastructure.dataset.cli`、verifier 工厂、公开 identity registry / PIT 钉定、上游入口、v2 测试迁移；不选 DQ-9 数值；修订 1 第 2 条（listing 质量报告入口、profile 1.1.0 `listing_quality` 段）已实现；修订 1 第 1 条与冻结契约冲突，经修订 2 转入 ADR-0109 |
| [0102](0102-state-run-entry.md) | State 运行、读回与诊断入口 | Accepted（2026-10-02；PM，于 phase/1 重新接受）：`research.states.run_cli compute`、`infrastructure.state.run_cli show/list`、`research.states.report_cli`；默认零副作用；不接全链路输入 / worker / API |
| [0103](0103-p7-plan-binding-and-admission-handoff.md) | P7 计划绑定、准入交接与横截面执行接线 | Accepted（2026-10-02；PM，于 phase/1 重新接受；修订 1 对齐既有 cs 执行）：`hlens.p7.plan@1.0.0` 保留键绑定、evidence 交叉核对、拒绝审计、COMMIT→执行交接；开关默认关闭，关闭时逐字节不变 |
| [0104](0104-paper-deviation-run-binding.md) | 纸面偏差的运行绑定与兼容规则 | Accepted（2026-10-02；PM，于 phase/1 重新接受）：修订 ADR-0079；scope 1.1.0 / payload 2.1.0 运行绑定，cost_model 不同一律拒绝，旧报告只读、标为 scope-only |
| [0105](0105-p11-operational-completion.md) | P11 运行周边补全与 D-P11-WINDOW | Accepted（2026-10-02；PM，于 phase/1 重新接受）：D-P11-WINDOW = 固定日历；Lifecycle 写入 CLI、基线导出、批量驱动、Dataset 版 operator、resolver 测试；修订 ADR-0074 §9 |
| [0106](0106-reference-backtest-migration-target.md) | P14 迁移目标——参考回测引擎 | Accepted（2026-10-02；PM，于 phase/1 重新接受）：独立 `ReferenceBacktester` 为迁移对象（容差 0，不替换生产默认），EventBus 内存→文件为搭档演练；`MigrationTarget` / `MigrationReport`；关闭 P14-TARGET |
| [0107](0107-e1-bounded-working-set-round-3.md) | E1 有界工作集第三轮与容量测量范围 | Rejected（2026-10-02；PM）：被 E1-CAP-1 正式协议结果与 ADR-0108 取代；残余 A3（listing_at 重证）/ A11（采集 body 整读）记为风险，需要时另立 ADR |
| [0108](0108-canonical-commit-granularity-and-metadata-growth.md) | Canonical 提交粒度与 Iceberg 元数据增长 | Accepted（2026-10-02；PM 依 Raphael 直接指令）：方案 A，一个逻辑单元 = 一个 snapshot（Canonical 与 Raw 归档元素），infrastructure 内暂存多文件提交，不改契约；旧逐微批历史只读兼容；已在 phase/1 实现（窄证明的单元内容复核 2026-10-02 补齐）；残余"随单元数 K 增长"记为 D-META-AGE |
| [0109](0109-v3-manifest-legacy-quality-binding.md) | v3 Dataset manifest 的旧质量表绑定（契约 2.6.0） | Accepted（2026-10-02；PM）：2.6.0+ v3 manifest 不再要求绑定 `quality.data_quality_reports`（有 snapshot 才绑定，同 Raw evidence / 缺口表规则）；关闭 D-V3-LEGACY-BIND；旧版本规则与哈希不变；未实现 |
| [0110](0110-p7-durable-restore.md) | P7 准入候选的持久化恢复 | Accepted（2026-10-02；PM）：检查点中 P7 来源的策略行带 `origin=p7` / `plan_hash` / `round_index`，恢复经 `P7PlanSource` 重建并以 admission 日志 COMMIT 为证明；未准入 P7 的状态逐字节不变；未实现 |
| [0111](0111-refactor-target-vertical-slice.md) | 重构目标：Phase 1 垂直切片、Phase 0.5 / 2 ~ 14 归档、直读 Parquet、单一授权表 | Accepted（2026-10-03；Raphael 确认）：`REFACTOR_TARGET.md` 为最高指导文档；取代 ADR-0021 对“Parquet + 自有清单”的否决；Phase 1 关闭条件改为切片验收；关闭 D-AUTH-CONFLICT；未实现 |

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
| D-04 | **Validation 时序冲突**。Phase 8 才是 Validation & Robustness，但 Phase 5–7 已产生实验与晋升判断。 | Codex 依 Raphael 全权授权选 (a)：Phase 4 提供最小 G0～G3 / 封存 OOS 能力，Phase 8 提供稳健性扩展；不得绕过最小门或人工生命周期门 | ✅ 已决定，ADR-0072；既有代码仍待统一验收 |
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
| D-29 | **`apps/worker` 与研究代码的边界**：01-system.md 让 worker 运行实验，而 `apps/` 不得 import `research/`；实验如何被运行而不越过边界（C1 F4，原 R15）。 | ADR-0049 已决定：通用 worker 只依赖 core / 标准库；研究组合根放在 `research/loop` 并依赖 worker 暴露的阶段 Protocol，反向 import 禁止 | ✅ 已由 ADR-0049 决定并实施；apps 架构边界检查持续约束 |
| D-30 | **C-L5 的跨对象校验执行点**：`embargo >= 最长 Outcome horizon` 需要同时看到 Profile 与 Outcome，由谁、在何时校验（C1 F4）。 | 每次 Validation 绑定单一 `OutcomeLabelSpec`；G0 绑定其 Outcome / hash，G1 比较实际 `horizon` 与 Profile embargo；未来多 Outcome execution 需重定规则 | ✅ 已决定并在既有代码落实；ADR-0041「D-30 决议」；尚待 Phase 4 统一验收 |
| D-31 | **C-L4 历史可交易标的池**：上市 / 下架有效期如何表达，`Instrument` 是否需要有效期（C1 F4）。 | Codex 裁决 → [ADR-0024](0024-historical-tradable-universe.md)：静态 `Instrument` + 双轴 listing 历史 + 版本化 `UniverseSelectionSpec` | Phase 1；Accepted（2026-09-24）；契约已交付，listing 历史与 universe 执行待实施（依赖 ADR-0023） |
| D-32 | **按天分区写入缺依赖**：锁定的 PyIceberg 0.12 写入 `day(...)` 分区需要官方 extra `pyiceberg-core`，它不在 03-data.md §6.1 的依赖中，四张冻结表无法写入（C3 发现）。 | Codex 裁决：方案 A → [ADR-0026](0026-pyiceberg-core-extra-for-day-partitions.md)：加入该 extra；不改分区（B）、不改写入路径（C） | Phase 1；Accepted（2026-09-24），已实施（随 C3 验收） |
| D-33 | **归档 ↔ REST 的同一观察如何汇合**：同一 `observation_key` 同时有归档交付与 REST 交付的 revision 时，按 ADR-0023 §5 是 competing heads → 数据集 fail closed。归档终将覆盖曾由 REST 补过的区间，不裁决即等于禁止 REST 补尾（D3A 发现）。 | Codex 选择方案 A → [ADR-0027](0027-rest-raw-source-and-element-revisions.md) §4：项目定义的版本化规范内容投影逐字段相等时，在独立证据表 `raw.binance_spot_precedence_evidence` 追加 evidence-only 边（归档 revision 取代 REST revision）；不等 / 不可比较则无边、fail closed；不是来源声明的先后 | Phase 1 D3；已决定（ADR-0027 Accepted 2026-09-25），D-33 方案 A 生效；尚待实施（D3B 起） |

> **编号说明**（2026-09-24 由 Codex 最终确认）：D-17 ~ D-25 连续且唯一。
> D-26 ~ D-31 由 C1 复审后的 Codex 裁决新增，与 D-25 连续：D-26 / D-27 各对应一份已接受且已实施的 ADR（C2c / C2d），
> D-28 / D-31 已于 2026-09-24 分别由 ADR-0023 / 0024 决定（Accepted；实现状态与整体验收分开记录）；D-04 由 ADR-0072 关闭，D-29 由 ADR-0049 关闭，D-30 由 ADR-0041 的 G0 / G1 实现关闭；这些决定均不代表相关 Phase 已验收。
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

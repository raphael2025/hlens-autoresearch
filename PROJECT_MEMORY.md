# HLENS-AutoResearch Project Memory

> 给 Claude 的长期项目记忆：只保存跨会话仍然有效的事实。
> 维护规则见 `CLAUDE.md` §8（目标 < 200 行，> 300 行必须 Compaction）。
> 当前进度看 `PROJECT_STATUS.md`；完整架构看 `docs/architecture/`；决定全文看 `docs/adr/`。
> 2026-09-24 Phase 0 收口时执行 Compaction：各 ADR 只保留 ID + 一句话，细节见 ADR 与 `docs/reviews/`。

## 1. Project Identity

- 名称：HLENS-AutoResearch
- 定位：长期演化、模块化、可插拔、可验证的加密市场自动化研究基础设施（不是交易机器人或回测框架）
- 核心目标：持续吸收公开知识、已有策略和失败经验，通过组合与实验验证产生、检验新假设
- Phase 1 数据范围：Binance 公共 spot `BTCUSDT` / `ETHUSDT`，归档 aggTrades + 1m klines（ADR-0022）；
  正式研究标的与周期（D-09 提案为 BTCUSDT 1H）仍待 Phase 4
- 当前阶段：Phase 0 已完成（tag `phase-0-complete`）；Phase 1 已开启；D3E 已于 2026-09-27 独立验收、D4 已关闭；E1-CAP-1 是当前阻断。`fix/e1-cap1@a75278e` 候选探针报告 500k resume / replay 跨规模增长 59.9 / 63.9 MiB（超过 32 MiB），但该代码线与探针未合入 `main`；main 的 resume / replay 尚未测量，不能继承候选数值。上一轮未提交的 E1 余项（ADR-0075 Amendment 1 草案、ADR-0076/0077 草案及 bounded scan/result 代码）已于 2026-09-28 经 Raphael 批准整体归档到 `wip/e1-cap-archive@ece150e`（archive ref `refs/archive/2026-09-28/branches/local/wip-e1-cap-archive`），未审阅、未测试、不构成正式决定；E1-CAP-1 仍阻断，本轮后置
- 当前开发顺序：先整合模块基础逻辑，再统一验收。P7 已有 non-runnable typed plan、只读绑定校验器、durable v4 admission / TrialLedger 写入协调与 ADR-0074 本机有限批次 operator 基础；六类组合算子仍全部 fail closed。producer lowering 与完整预期 output 集合证明仍未完成。Phase 1 的 E1 主线 RSS probe 有 opt-in 分阶段诊断模式；默认测量不启动 tracer，诊断模式不具备 E1 证据资格。2026-09-30 已运行 E1 focused tests 和一次非 main 候选的 partial probe；main-matching 完整 probe、全仓测试/构建与阶段验收仍未完成。
- 2026-09-29 隔离候选 `codex/w1-independent-integration` 的历史证据：production Worker 在 PostgreSQL-backed Iceberg 下通过 result-fsync-before-ack 崩溃重启子标准；测试清理只触及显式拥有的表。`mypy tests`（404 files）与 `mypy apps infrastructure plugins research`（299 files）通过；改动测试切片最终为 969 passed / 1 deselected（3 个首轮失败节点修复后精确复跑通过）。v3 Dataset capacity probe 已扩展为完整 UTC 日并断言 selected row count 等于 N；只有 200 行 smoke 通过。先前 1k/5k/10k 探针使用旧的一小时窗口且 10k 阶段超时，不能作为完整 Dataset 容量证据；DQ-9 与 E1-CAP-1 仍开放。以上均是候选分支证据，未在本次整合树复验。
- 决策权（2026-09-28 起）：Raphael 明确授权 Claude Code 以 PM 身份直接决定工程 / 架构 / 模块语义（记录于 PROJECT_STATUS §6 D-PM-AUTH）；不可逆或对外操作（实盘、密钥、删数据、push / 合并 main、安装软件）仍先告知 Raphael
- ADR-0091（2026-09-28）：四类 Registry 的完整性审计必须只读且按既有格式诚实报告证据强度；Failure Registry 不提供历史防篡改证明。
- ADR-0093（2026-09-29）：Quality 报告使用固定大小 manifest 与内容寻址 evidence streams；v1/v2 保持只读兼容。
- ADR-0094（2026-09-30）：PIT v3 完整冲突 heads 使用有界、可重放 evidence stream；v2 tuple 与 2.3/2.4 manifest replay 不变，E1 容量门仍需单独通过。
- ADR-0095（2026-09-29，原隔离分支编号 ADR-0093）：生产 Worker 由部署方显式受信 Runtime Factory 组合；host 单任务轮询，信号在当前任务结果/ack 后停止，重启交给 supervisor。
- ADR-0096（2026-09-30）：TrialLedger 完全相同的 hypothesis 登记与同一 reevaluation attempt 可作为只读幂等确认，在 gate 关闭时返回 `False`；不写 journal、不增加 trial。LLM-origin 检查先于 fast path，新内容和新 attempt 仍须通过 gate。
- ADR-0097（2026-09-30）：PIT bounded graph 使用调用级 SQLite scratch index，复用显式配置的 scratch root；v2 replay 与 ADR-0094 v3 conflict stream 不变。scratch reader/init cleanup `2f98461` 已三方批准。Graph validator foundation `889b268` 已三方批准并在整合线复验 `11 passed`，Ruff/format/mypy通过；涵盖 SQLite owner marker、基础图不变量与生命周期。Production selector/head stream、多 cutoff 复用、长链/宽图诊断和完整 E1-CAP-1 容量门仍开放。
- 2026-09-28 底层代码补全轮次完成：契约升至 2.4.0（ADR-0077 / 0088，additive）；ADR-0076～0088 均 Accepted 并实现（ADR-0080 权威解析仍 BLOCKED）；全部代码未经测试，下一步为统一调试

## 2. Current Architecture

- 工程基线：Python 3.13 + uv；契约用 Pydantic 写在 `core/`，JSON Schema 导出到 `schemas/` 并随仓库提交
- 契约版本从 Phase 0 已发布的 `2.0.0`（D-25）向前兼容演进：ADR-0052 §4 为 **2.1.0**、ADR-0055 为 **2.2.0**、ADR-0077 DQ-1 为 **2.3.0**、ADR-0088 为当前 **2.4.0**；破坏性变化仍须升 major 并走 ADR。持久化对象按记录版本重放，新增字段以 `_FIELDS_SINCE` 等声明引入版本，当前版本新建对象的信封与哈希随 minor 变化属预期
- 模型只接受同 major；`1.x` 走 `core/compat/v1.py` 只读入口（`schemas/v1/` 35 份快照 + `tests/vectors/v1/`）；
  v1 与 v2 的 `content_hash` / `experiment_hash` 不可比较；读取 v1 不赋予任何 v2 登记 / 晋升资格
- current Schema 135 份（契约 2.2.0；`a5836b2` 及以前为 2.1.0），与 `CONTRACT_MODELS` 一一对应；研究 Provider Protocol 0 个（ADR-0017 的决定，不是遗漏）；
  Data Plane Adapter Protocol 3 个（Storage / Catalog / Collector，B3 已由 Codex 验收；Storage、PostgreSQL-backed PyIceberg Catalog、Binance 公共归档 Collector 与 fail-closed parser 的本地实现已验收；suite 在 `tests/contract_suites/`）；
  D2 起归档对象 key 内容寻址（`raw/binance/spot/archive/revisions/<sha256>/…`，collector `1.1.0`，source 绑定不变），`arrival_seq` 以归档表为 anchor 按 `2**32` block 分配、只存 Iceberg；
  Catalog 必须从实际 batch 独立重算指纹并核对（不信任自报）；C3 已冻结并实现 `hlens.pyarrow-batch-sha256@1.0.0`、八张生产表与分区演进
- Freeze Contracts, Evolve Implementations；四个 Plane：Data / Research / Control / Application；Research ⟂ Application
- PostgreSQL = Control Plane（不存大型行情）；Iceberg / Parquet = 真实来源；DuckDB / Polars 只是计算引擎；
  Phase 1 起：Iceberg Catalog 用独立 PostgreSQL 库、warehouse 为本地 `file://`、Phase 1 ~ 6 无 NATS（ADR-0021）
- 数据架构冻结正文与首切片（12 张表 = A2 首切片 8 张 + ADR-0027 REST 4 张、分区、source / parser / policy / universe 标识符、PIT I/O、A3 依赖与设置字段）：`docs/architecture/03-data.md`
- Provider / Plugin 架构；LLM、Backtest Engine 均可替换；LLM 只产出数据，永不作裁判
- Domain 层无基础设施依赖；依赖方向 apps → application → domain ← plugins/infrastructure
- 实验必须可复现（复现元组）；Schema 全部版本化
- 全文：`docs/architecture/00-overview.md`

## 3. Current Research Direction

Market State → Feature / Event → Knowledge Retrieval → Hypothesis → Combination → Experiment → Validation → Research Memory → Strategy Evolution

- 新颖性主要来自确定性的组合 / 条件化 / 时序算子，并且每次组合都计入尝试次数
- 路线图：`docs/research/roadmap.md`
- Raphael 于 2026-09-27 确认开发顺序：先收敛分支并整合已有实现，再按模块逐步调试和打磨；代码整合不等于 Phase 已验收，验证状态必须单独记录。2026-09-28 本轮授权将项目收敛为仅保留 `main` 长期分支；隔离实现可使用临时分支，整合后删除并将必要恢复点保存到 archive ref。

## 4. Current Phase

- Current Phase：Phase 1（Market Representation）**已开启**——Codex 依 Raphael 持续授权于 2026-09-24 开启（S0）
- Earlier E1 candidate `codex/e1-phase1-progress@d11097d` integrated the Quality canonical-partition v3 reporter slice (ADR-0093); the subsequent consolidation adds the Quality v3 manifest binding, bounded stream reader and end-to-end Dataset consumer (`135 passed, 3 skipped, 6 deselected`), with legacy 2.3/2.4 replay fixtures now pinned. PIT graph validation and SQLite-backed bounded selector/head traversal are also integrated (`18` direct tests; caller suite `63 passed, 4 skipped, 2 deferred deselected). BatchGrid hashing and historical manifest replay have their own accepted narrow slices. Remaining E1-Q work: make the canonical v3 reporter own its complete identity-hash and finite allowed/required snapshot-table registry; real PostgreSQL append/replay is still unrun. Remaining E1-R gate: complete-process 32 MiB measurement on a clean SHA whose production paths match `main`; current selector repeated-cutoff I/O/RSS, long-chain/wide-DAG resource behavior, and PyIceberg metadata/finalization working set remain unproven. REST-store head-movement and oversized-history fixtures remain deferred and do not count as passed. `CanonicalUnitNormalized` and `iter_revision_ids` have tamper / early-close coverage; E1-CAP-1 and Phase 1 are not accepted.
- 2026-09-30 协调者授权记录：为在 Phase 1 内移除单 key Canonical rows 的 O(H) 驻留，批准本轮开发任务扩展至 `infrastructure/canonical/normalizer.py` 的私有 proof-selection/batch-ID 遍历和直接测试；对外 `verify_unit`、core/frozen contracts 与 ADR 保持不变。实现必须复用已接受 ADR-0077 的有序 bounded RunSet / merge join；新增 StorageAdapter 查询接口、可变随机索引或新存储协议不在批准范围内，若不可避免则须停止并报告 `ARCHITECTURE_DECISION_REQUIRED`。
- PM Authority (2026-09-30)：Raphael 当前 Codex 项目目标重新授权 Codex 为本项目目标范围的 Fully Autonomous Engineering Director；该授权取代 2026-09-28 的 Claude Code PM 指派，子代理为执行者。H3/H4/H6、Phase 与实盘边界不变；正式决定需记录并提交。
- Current Objective：继续按 Raphael 的顺序补齐模块基础逻辑，再统一调试 / 验收。P7 的跨 prepare→complete admission lease、durable store 写 gate、round/approval 串行化、close 后拒写与只读 journal view 已进入本地 main，静态复核通过但未验收。ADR-0074 严格 TOML parser、静态 Provider allowlist、有限轮数 CLI 与逐轮报告已合入 `main@0d4862a`，经两轮源码静态复核、未验收；E1-CAP-1 仍是 Phase 1 验收阻断。
- D3E（含 R1 / R2 / R3）已于 2026-09-27 由 Codex 验收（`docs/reviews/2026-09-27-d3e-acceptance.md`）；D4 已关闭，Phase 1 当前阻断为 E1-CAP-1 的内存有界性修复与独立复核；E2 起仍按状态文档的开放顺序推进
- Current Blocker：E1-CAP-1 完整工作集上界仍未证明。`5d9dd71` 非 main partial probe 的 `verify_archive` 增长超限是历史诊断，不能当作当前证据。PIT graph state/head traversal、Dataset v3 manifest consumer 与 historical replay 已有局部整合及验收；BatchGrid FrozenMapping hash 已修复。下一代码切片是 Quality v3 reporter 内固定 rule-owned identity hashes 和 snapshot-table registration；下一验证切片是长链/宽 DAG/重复 cutoff 的 selector diagnostics，然后在生产 paths 与 main 一致的 clean SHA 上完成完整 32 MiB gate。真实 PostgreSQL Quality append/replay 与 E1-CAP-1 完整容量结果仍未完成。C2 的专用 catalog / test database、最小权限 role 与本机忽略凭据已创建并验收。
- Next Milestone：继续补齐 Phase 1 已批准、无需修改冻结契约的有界性切片；不制造可运行 Profile 配置，六类 P7 operator 保持 fail closed。之后在 main-matching clean SHA 上完成全量 E1-CAP-1 测量与 Phase 1 验收。
- 全阶段代码完成批次（2026-09-26，Claude）：分支 `claude/2026-09-26-code-completion-337e38` → `wip/all-code-completion`，B1～B54 为 CODE_COMPLETE / DEBUG_PENDING（非验收；B44～B54 为审计后续，B53 为集成会话按 Codex 复核的修复）；逐批证据见 `docs/plans/2026-09-26-all-code-completion-plan.md` §10

## 5. Active Decisions

- ADR-0001：重大架构决定用 ADR 记录；Agent 只能起草 Proposed
- ADR-0002：架构基线（原则 P1–P17、四个 Plane、默认技术栈）
- ADR-0003（D-06）：Python 3.13 + uv，与系统 Python 隔离
- ADR-0004（D-07）：本地 Git 仓库；不改全局配置；一次性提交身份（第 3 条"远程待定"被 ADR-0025 取代）
- ADR-0025：远程 = 私有 GitHub `raphael2025/hlens-autoresearch`；PR 流程已使用（截至 2026-09-27 已合并 PR #1～#9），CI 尚未配置。当前长期分支策略由 2026-09-28 主会话授权调整为仅保留 `main`；详见 `CLAUDE.md` §10。
- ADR-0005（D-03）：研究 / 生产边界 = Artifact + Registry + Promotion + Equivalence Gate；Promotion 链（Registry / Promotion 服务 / Equivalence Gate）已实施（B28，失败关闭，今天拒绝所有策略）；Profile 是否冻结以 ADR-0062 的冻结登记为准（B56）；评估 G2 的报告须含 Profile 所要求的 ADR-0060 市场基准 / 反向对照报告项（B51 / B59，路由证据模式同，B58）；P10 证据模式只证明研究层报告条件、不要求冻结登记，生产资格仍只由 Promotion / Control Plane 决定（ADR-0043 B62）；Q-1 / Q-2 / Q-3 / Q-7 开放
- ADR-0006（D-05）：生命周期 v2（C-1：OOS → PAPER → PRODUCTION_CANDIDATE → ACTIVE；C-2：ACTIVE 带
  `execution_mode` SIMULATED|LIVE，不设 LIVE 状态）；RETIRED 进退役记录，REJECTED / FAILED 进 Failure Registry；Q-4 ~ Q-6 开放
- ADR-0007（D-09 结构）：三层验证（Constitution / Validation Profile / Experiment Metadata）+ 两步冻结；
  Constitution 曾以 `0.2.0-draft` 按此重组为纯原则，现已由 ADR-0020 发布为 `1.0.0 / Approved`，原则与阈值零变化
- D-09 数值（TBD-1 ~ TBD-5）与 H-3 ~ H-7：**未批准**，Phase 4 校准后冻结为 Profile 参数；Constitution 中不得出现数值阈值
- ADR-0008（D-11）：映射字段只读、逐模型内容哈希排除表、规范化 JSON、v1 只读兼容
- ADR-0009（D-12）：完整实验身份与直接依赖内容绑定；实际 seeds 在实验哈希内；Report 绑定 run_id
- ADR-0010（D-13 ~ D-16）：`model_copy(update=...)` 完整校验、唯一 ASCII SemVer、v1 顶层 shape gate、Schema 表达键值格式
- ADR-0011（D-17）：生命周期主体一致、时间单调、LIVE 证据绑定主体与授权窗口；删除自报 `live_execution_enabled`
- ADR-0012（D-23）：具体规格 `kind` 为不可覆盖字面量；Feature / State / Event / Strategy 输入白名单，Outcome 不得进入
- ADR-0013（D-19、D-20.1 ~ 20.3）：`verdict` 精确等于 `derive_verdict(gates)`；`gate_id` 唯一；全局拒绝 NaN / ±Inf
- ADR-0014（D-20.4）：Validation Profile 普适结构不变量（窗口 / 封存 / 观察期为正等），不选数值
- ADR-0015（D-21、D-22）：`ContentHash` / `GitOid` / `GitCodeRevision` 分类型；Profile 绑定 = `Ref(kind=profile)` + 哈希
- ADR-0016（D-18）：`LlmCall` 三项必填 `ContentBlobRef` + 显式 `called_at`；取回与一致性延期
- ADR-0017（D-24）：Provider 方案 B——Phase 0 只冻结语义，Protocol / DTO / contract tests 随首次消费的 Phase 交付
- D-25：上述收窄属未发布的 2.0.0；**Phase 0 收口后 2.0.0 已发布，同类改变必须升 major**
- ADR-0018（D-26）：三类语义身份（选择键、`Ref` 目标、Git 代码修订）排除信封版本；全局 `==` 与内容哈希不变
- ADR-0019（D-27）：生命周期转移证据至少一项且非空；不做自报职责分离
- ADR-0020：Constitution 发布为 `1.0.0 / Approved`，第一至第九章正文 sha256 `4d603d62…259cd` 不变，只前向适用
- ADR-0021（D-01 / D-02 / D-10）：PyIceberg SQL Catalog on 独立 PostgreSQL 库（与 Control Plane 物理隔离）、WSL ext4 `file://` warehouse 经 Adapter、Phase 1 ~ 6 不运行 NATS
- ADR-0022（D-08）：首切片 Binance 公共 spot BTCUSDT / ETHUSDT 归档优先 + market-data-only REST 补尾；WS 延后；无任何交易能力或密钥
- ADR-0023（D-28）：历史轴（`available_time`，有证据的 policy）与知识轴（`knowledge_time`）分离；append-only revision；
  `arrival_seq` 只作审计；PIT 双截止 + maximal head，competing heads fail closed；Research Dataset 绑定 `ResearchDatasetManifest`（不升 3.0.0）
- ADR-0024（D-31）：静态 `Instrument` 不变 + 双轴 listing episode 历史；`UniverseSelectionSpec` 以 `name + SemVer + hash` 在 manifest 绑定，不新增 `Kind`
- ADR-0026（D-32）：PyIceberg 0.12 的 day transform 写入使用官方 `pyiceberg-core` extra；不改变冻结分区或写入路径；已实施并随 C3 验收
- ADR-0028（E0，**Accepted** 2026-09-25，Raphael 批准方案 B）：Canonical revision 与 Raw 元素 revision 一一对应、lineage 进身份；跨通道边由 PIT 从绑定的 Raw 证据 snapshot 映射，不物化；Canonical `arrival_seq` 独立分配；E1 已实现（`infrastructure/canonical/`，REVIEW_PENDING；规范 symbol = `<base>-<quote>`，E2 须一致）；F1 选择引擎 `infrastructure/pit/`（`hlens.pit.maximal-head@1.0.0`，只在绑定 snapshot 上读取与证明，REVIEW_PENDING）；E3 质量报告 `infrastructure/quality/`（`hlens.quality.canonical-partition@1.0.0`，无数值阈值，REVIEW_PENDING）
- ADR-0029（E2 listing 来源，**Accepted** 2026-09-25，方案 A）：`exchangeInfo` 快照 → 新 Raw 表 → 观察下界语义的 listing revision；历史 simulation 的 universe 仍依赖 D-HIST
- ADR-0030（F4 FeatureProvider 契约，**Accepted** 2026-09-25，方案 A）：执行器截断输入（available ≤ t 且 knowledge ≤ cutoff，lag 由执行器施加）+ 契约扰动测试；`core/contracts/feature.py` additive
- ADR-0027（D-33，Accepted 2026-09-25，已按 D3A～D3E 实施并验收）：D3B 四张 additive Raw 表、REST 身份规则与纯 policy，D3C 严格纯 decoder，D3D `binance.spot.public-rest@1.0.0` collector（`61dd9bf` + `c06b9fa`；只经自建、无 hook / auth / 环境代理 / cookie 的 client 发送，不接受外部 `httpx.Client`），以及 D3E store / reconciler（含 R1 / R2 / R3；`docs/reviews/2026-09-27-d3e-acceptance.md`）；归档路径与 `IDENTITY_HASH` 零改动；D-33 方案 A 生效（规范内容投影逐字段相等才在独立证据表写 evidence-only 边 归档 → REST，项目政策、非来源先后）
- Raphael 授权（2026-09-25）："一切都你自己决定，允许多子代理，尽快开发"——Claude 可自行决定并接受 ADR（记为"依 Raphael 授权"），
  红线仍需 Raphael 本人：Constitution 原则 / 阈值、Profile 数值、实盘 / 资金 / 风险预算、删除历史数据、系统软件、生产部署、合并 `main`
- ADR-0032（D-HIST，**Raphael 批准** 2026-09-25）：`hlens.availability.archive-event-time-assumption@1.0.0`——数据集 PIT 规格显式绑定时，归档成交 / K 线以 `min(存储值, 可观察时刻 + 5 秒)` 为有效可用时间；存储、证据缺口、知识轴不变；未绑定即保守
- D-NET（Raphael 2026-09-26 批准推荐方案）：可下载 BTCUSDT / ETHUSDT 各 1～3 天 Binance 官方公共归档用于真实数据能力验证；只写本机，不入仓库，不形成市场结论
- Raphael 2026-09-26 批准：ADR-0052（验证契约补全：精确小数、Profile 新字段、负对照独立阈值）、ADR-0053（VALIDATION → FAILED）、ADR-0054（部分成交跨 bar 结转）；ADR-0051（上市历史假设，D-LIST）暂缓
- D-QGAP：方案 A（ADR-0031，证据缺口独立只追加表）；D-PUSH：只推 WIP 备份分支；D-P05（已被取代：Phase 0.5 按 ADR-0058 实施写入路径；ADR-0055 见下）
- 框架批次 ADR（2026-09-25，依授权 Accepted，全部 FRAMEWORK_IMPLEMENTED / NOT_VALIDATED，数值一律 TBD）：0034 知识库 · 0035 状态 · 0036 事件 · 0037 Outcome + 最小验证门 · 0038 策略 / 风控 / 回测 · 0039 状态×策略 · 0040 假设 + LLM · 0041 稳健性（G4、回溯审计不得翻转已拒绝对象） · 0042 合成市场 · 0043 路由 · 0044 事件总线 + worker · 0045 进化 · 0046 模拟执行（无实盘）· 0047 迁移 · 0048 API / Web · 0049 持续循环（worker 机制在 apps/worker，研究阶段在 research/loop，research 依赖 apps/worker 而非相反）· 0050 循环审计记录契约（只追加，描述既有字节）
- Raphael 指示（2026-09-25，/goal）："使用 4 个子代理加速开发，直到项目全部开发完成；不用调试，先按框架实现所有代码，每一步更新文档，开发完成后再逐个调试"——Phase 0.5、2～14 按路线图先实现框架代码（状态 FRAMEWORK_IMPLEMENTED / NOT_VALIDATED），Phase 1 收尾并行；红线不变（宪法原则 / 阈值、Profile 数值留 TBD；Phase 13 只做模拟 / 纸面，无交易端点 / 密钥 / 下单；`main` 合并与 tag 仍需 Raphael）
- Raphael 授权（2026-09-26，/goal）：“所有的决策都由你来决定，包括红线的事情”——Claude 的逐项裁决见 `docs/reviews/2026-09-26-autonomous-decisions.md`（不做实盘 / 不冻结 Profile 数值 / 不合并 `main` / 无证据不晋升）；ADR-0056 事件表、ADR-0057 事件 subject、ADR-0058 知识库写入由 Claude 接受；Codex 全代码复核 K3 要求 ADR-0052 以 2.1.0 实施且旧 2.0.0 原样可重放；ADR-0054 / 0057 的新字段已于 B41 以 2.1.0 重新声明；`subject` = 调用方提供的稳定、大小写敏感 opaque ID
- ADR-0066（2026-09-27，Codex）：允许独立 `infrastructure.event.create_event_tables` 显式操作入口；默认无 catalog 副作用，`--apply` 只调用 `ensure_event_tables`，不接入 Phase 1 / 自动 provisioning；实际生产建表仍须单独授权。
- ADR-0067（2026-09-27，Codex 依 Raphael 本轮授权）：P11 显式劣化检查绑定调用方提供的 ACTIVE 终态生命周期历史、PASS baseline、精确 Profile 与 ADR-0062 有锚点冻结登记、近期 observation manifest / method / UTC 窗口；report 1.1.0 evidence 内嵌完整 manifest、参与哈希，legacy 1.0.0 hash 保持。调用方历史不被声称为最新权威来源；source id / hash 与聚合内容只做 caller-declared 内容绑定，不认证外部真实性。operation、writer、freeze anchor snapshot、manifest evidence 与显式本机 CLI 已进入本地 `main@498250a`；未测试、Phase 11 未验收；当前 registry 为空则 fail closed。
- ADR-0069（2026-09-27，Codex 依 Raphael 本轮授权）：P12 `combine` 仅合并 search-space 重叠定义、`risk_policy` 与 `applicable_instruments` 完全相同的父代；不一致或缺失/非缺失冲突均拒绝，不默选某一方；不改 StrategySpec Schema / hash。
- ADR-0070（2026-09-27，Codex 依 Raphael 本轮授权）：若最后一条已记录 round 的 `experiment` stage 为 FAILED，loop 设置可由 audit 重建的 `recovery_required`，同一 audit 不再自动续跑；LoopRecord / hash / checkpoint 字节不变。先防止重复 attempt，outcome 自动恢复与人工修复工具另待后续实现。
- ADR-0071（2026-09-27，Codex 依 Raphael 项目与架构决策授权）：为 ADR-0070 failed experiment round 提供 research/loop 内部纯内存只读复核摘要；按 round index 将审计记录、round checkpoint 与 ledger journal 区间做 hash-bound 配对，明确列出未持久化的 trial outcomes、失败序号、traceback、audit envelope hash 与外部 Provider 状态缺口。不得开目录、写 journal、改生命周期或恢复；已实现，静态检查通过，未测试、未验收。
- ADR-0068（2026-09-27，Codex 依 Raphael 本轮授权）：接受闭世界 typed operator plan 机制与拒绝边界；仅准许生成 `non-runnable` typed AST / validation result。conditioning / interaction / temporal / transformation / ensemble / negation 仍全部禁跑；plan 审计持久化及与 TrialLedger 的原子关系须在首个 operator 启用前另行决定和实现，当前 typed plan 不注册 trial、不调用 Runner。
- 2026-09-26 由 Claude 依授权接受并实施：ADR-0056 事件表、ADR-0057 事件 subject（2.1.0）、ADR-0058 知识库写入、ADR-0059 G4 跨资产 × 横截面、ADR-0060 C-T4 市场基准（报告项）、ADR-0061 交互 DSL；ADR-0055（知识标签 / 资产检索：`tags_all` AND、`assets_any` OR 逐字精确，契约 2.2.0；资产是研究范围标识，不是上市 / 行情证据）**Accepted**（Codex，2026-09-26，基于组合代码 `c08c589` 与其全量门禁），实现 CODE_COMPLETE / DEBUG_PENDING；ADR / 代码接受不等于 Phase 0.5 验收——种子尚无具名人工审阅的标签 / 资产（只有分类提案，不得当作已审阅数据）；ADR-0051（D-LIST）于 2026-09-28 按项目授权 Accepted，策略仍假设性且政策表待归档下界核实；ADR-0063（本机只读 API 的 ASGI 运行时 = Uvicorn，仅 127.0.0.1、单 worker，可选依赖；公网 / 认证未决）Codex 2026-09-27 Accepted，B65 已实施（`apps/api/serve.py`）；截至 2026-09-26 的记录称 Uvicorn 未安装、真实运行待 H12 授权；该历史状态已由 2026-09-28 开发授权及 2026-09-29 安装和 `30 passed` 复验取代（见 `PROJECT_STATUS.md` / ADR-0063）。ADR-0064（数据集路径 G4 容量的 `bar_volume` 须与已执行 `PriceBar.volume` 精确相等，否则 INCONCLUSIVE `bar_volume_source_mismatch`；合成路径不变）Codex 2026-09-27 Accepted，B66 已实施；ADR-0065（数据集路径 G4 容量遇正结转未成交余量时失败关闭，`carry_over_unfilled`）Codex 2026-09-27 Accepted，B67 已实施；D-STATE-INC（ADR-0035 状态执行器增量评估）Codex 2026-09-27 **暂缓**：保留逐时刻可见前缀路径，需真实性能基线 + 不暴露未来数据的逐步协议才重评；ADR-0062（Profile 冻结登记：追加式、必需目录外锚点；Promotion 无有效登记即 `profile_not_frozen`）Codex 2026-09-27 **Accepted**；登记为空、Profile 数值未冻结，不是 Phase 4 / 5 验收
- ADR-0072（2026-09-27，Codex 依 Raphael 全权授权）：D-04 选择 Phase 4 提供最小验证门和封存 OOS 能力、Phase 8 交付稳健性扩展；不改验证契约或数值，不豁免最小门或人工生命周期审批。
- D-30（2026-09-27）：由 ADR-0041 记录现有 G0 Outcome / spec 绑定与 G1 `embargo >= OutcomeLabelSpec.horizon` 强制门；单 Outcome 执行下决议关闭，多 Outcome 需修订语义。
- Raphael 授权（2026-09-24）："授权所有"，Codex 全权接管决策 / 开发 / 测试 / 文档 / Git；Codex 解释为覆盖原则零变化的
  Constitution 1.0.0 发布与 Phase 0 收口（closure、`main` fast-forward、轻量 tag），并覆盖 C2 创建专用 PostgreSQL catalog /
  test database、最小权限 role 与本机忽略凭据，并覆盖 D0 / D1 对 Binance 官方公共归档、`.CHECKSUM` 与本机忽略小样本 smoke 的有限网络访问；D2 可使用这些已授权资源但不得新建或修改数据库 / role；不覆盖系统软件安装、PostgreSQL 系统配置、其他数据库 / role、账户 / 交易接口、原则或阈值变化、实盘、资金或风险预算

- ADR-0077 implementation decision (2026-09-29): Universe v3 passes caller-supplied `UniverseRunParams` end-to-end and uses content-addressed hierarchical run-reference sets for bounded sorting/merge; no implicit capacity or OS temp directory. DQ-9 values and E1-CAP-1 remain open.
- ADR-0094 (2026-09-30): PIT v3 conflict heads are fully preserved in a bounded content-addressed evidence stream with a fixed-size root/count result; v2 tuple and contract 2.3/2.4 replay remain unchanged. E1-CAP-1 remains open.

## 6. Active Constraints

- 硬性规则全文见 `CLAUDE.md` §3（H1–H14），摘要如下：
- 研究代码永不直接成为生产代码；LLM 不作最终裁决
- 不因回测结果修改 Constitution、Profile 或验证规则；Constitution 修改须按第九章另起 ADR 并由 Raphael 批准具体变化
- Outcome 不得作为 Feature / State / Event / Strategy 的输入
- 所有实验可复现；所有 Schema 版本化；失败实验与生命周期历史不可删除
- 环境变更（安装、系统配置、Docker、数据库、全局 Git 配置）需 Raphael 授权
- 不修改旧项目与外部数据
- Claude 不替 Raphael 做架构决策；Codex 在 Raphael 委托边界内作正式决定并记录（CLAUDE.md §0）
- Git：`main` 是唯一长期保留的本地与远端分支；必要时使用临时隔离分支，完成后整合并删除。分支清理不删除失败实验或生命周期记录；archive refs 可保留代码恢复点。
- 实盘、资金、风险预算始终需要 Raphael 亲自批准

## 7. Current Known Risks

- pypi.org 索引域名在本机被阻断（files.pythonhosted.org 可达）：离线安装需用 uv.lock 的精确版本
- WSL 内存约 15 GiB：大数据集需分区和流式处理
- Docker 未安装：ADR-0021 已按无 Docker 设计（`file://` warehouse）；MinIO / S3 延期
- 外部数据盘未挂载：`~/BTC` → `/mnt/wsl/PHYSICALDRIVE1p1/BTC` 当前不可访问
- Git 没有全局提交身份：提交使用一次性 `-c` 参数；PR / CI 未配置；warehouse 数据无异地副本
- availability / precedence 证据已产出（D2，`docs/architecture/evidence/binance-spot-publication.md`）：官方资料**不能**证明任何具体 revision 的公开时刻，
  因此 `binance.spot.publication@1.0.0` 一律 `available_time = ingest_time` + 证据缺口，早于本机 ingest 的历史可用区间为空；
  `binance.spot.archive-revision@1.0.0` 无法证明归档替换的先后，一律 competing heads。放宽只能靠新证据 + 新 policy 版本（H3）
- REST 证据（D3A，`docs/architecture/evidence/binance-spot-rest-market-data.md`）同样结论：官方没有任何响应的公开时刻；
  另外**未证明** aggTrade ID 连续、REST 与归档内容一致、未结束 K 线可从载荷判别 —— 这些一律 fail closed，不得当作已解决
- Constitution 1.0.0 只是原则：验证流水线、泄漏门、多重检验校正、trial 账本均未实现，Profile 数值要到 Phase 4；
  在那之前没有实验能被实际判定
- 契约层只校验**结构与声明**：传递依赖闭包、trial 权威账本、`run.repro` ↔ Spec 一致性、Registry 存在性、
  物化数据泄漏检测、哈希与真实内容一致、Git 对象存在、Profile 已 frozen、跨对象绑定一致，
  都是未实现的 Runner / Registry / Control Plane / 验证服务义务（06-experiment.md §7），不得宣称已防住
- 判定只保证**报告内部**自洽：门集合完整性、`threshold_source` 真实性、`value` 由 `metric` 算出，属未来验证服务
- `LlmCall` 只保证**登记结构**：内容可取回、内容与 `sha256` 一致、不可覆盖、外发合规、调用登记完整均未实现；
  06-experiment.md §2 的「完整输入输出」不得描述为已满足
- 生命周期证据只保证结构非空：存在性、支持结论与否、`approved_by` 真实性与职责分离属未来授权服务
- 契约层不再拒绝 `to_mode = LIVE`：Phase 13 红线在 Control Plane 落地前只靠人与流程
- JSON Schema 弱于运行时的几处：首尾空白去除（02-domain.md §3.7）、时长符号与 `cost_model.kind`
  （07-validation.md §5.4）、只由跨字段相等约束的 `declared_research_class`；权威校验必须经过运行时模型
- `venue` / `symbol` / `timeframe` 区分大小写、不做规范化（ADR-0018 边界）：未来 Adapter 必须产出规范值
- v1 只读 gate 只做顶层形状检查；旧哈希只对完整的 v1 持久化规范载荷复现历史身份（C1 F2）
- 外部是否存在 v1 历史数据证据不足：不得宣称迁移路径已在真实数据上验证
- 后代 G5 / P12-LOOP（Codex 2026-09-27 决定，有意暂缓）：替换提案留在循环外的显式作业；循环只到 OOS、OOS → PAPER 须人工批准；后代沿用父代 `family_id`，每个 family 只评估一次密封 OOS（C-S1..3），绝不复用密封窗口、不为规避而新建 family 或猜测规则；将来自动触发须新 ADR（独立预注册密封评估 + 证据 / Profile 规则）（完成计划 B57 / P12-LOOP）
- D1 已用两个时间单位边界日的官方 kline 与 aggTrades 验证原生字段、单位和 ZIP 结构；大体量 BTC 日归档仍须在批量 backfill 前做容量基线

## 8. Important Historical Context

- 旧研究项目 `alpha-autoquant` 位于 `/mnt/e/alpha-autoquant`（只读参考，不得修改）
- 现有系统 `hlens-cryptoplus` 位于 `/mnt/e/hlens-cryptoplus`（只读参考，不得修改）
- 旧研究中的 anti-leakage / red-team 规范可能成为新系统素材（Raphael 提及，尚未评估内容）
- 旧研究可能已看过 BTC 全部历史，因此历史封存区在认知上不完全干净（D-09 H-3）
- 旧策略或旧结论进入新系统时必须重新登记并重新验证，不能直接信任
- Phase 0 审查链：C1（`fce4f81`，`FIX_BEFORE_CLOSE`）→ ADR-0018 / 0019 实施 → C3（`4a2951a`，
  `READY_FOR_HUMAN_CONSTITUTION_GATE`）→ ADR-0020 发布 Constitution 1.0.0；记录见 `docs/reviews/`

## 9. Last Known Good State

- 稳定远程恢复点（2026-09-27）：PR #9 合并后 `origin/main` 为 `44fe9a2`；B1～B67、契约 2.2.0 与 PR #6 的模块收口批次已包含在该恢复点，PR #7～#9 同步状态 / hygiene。B67 `6d887b7` 的全量非 PostgreSQL门禁为 7294 passed、2 个预期 Uvicorn skip、136 deselected；PR #6 的集成定向检查为 11 passed。均不代表 Phase 验收。本轮开发开始时本地 `main@0640460` 比远程恢复点超前 51 个提交；并入 E1 probe、P0.5 seed schema 固定、Phase 11 worker journal stale-writer guard、P7 只读直接引用校验器、E1 opt-in 分阶段诊断、清理一条已归档 E1 设计分支与文档同步后，本次状态提交后本地 `main` 超前 61 个提交，尚未推送。当前整理后有 6 个本地分支、10 个 worktree、7 个远端分支引用。测试、probe 与验收未运行。
- Phase 1：D3E（含 R1 / R2 / R3）已于 2026-09-27 验收，D4 已关闭；E1-CAP-1 是当前容量阻断。`fix/e1-cap1@a75278e` 的 500k resume / replay 跨规模增长分别为 59.9 / 63.9 MiB，超过 32 MiB；这是候选实现的测量，不是 main 测量。主线适配版 probe 已合入但未运行；main 自身容量仍未知。主线仍有需设计与消除的 O(N) positions、时间列、返回 IDs、收尾列和 archive cache。整合基线选 main，不整支并入候选，详见计划 §10.33–10.35。
- 恢复资料见 `PROJECT_STATUS.md` 与 `docs/plans/2026-09-26-all-code-completion-plan.md`；Phase 0 基线仍为 tag `phase-0-complete`，Phase 1 D3D-R1 恢复点为 `c06b9fa`。
- State：契约、状态机、只读载荷、实验身份、版本语法、生命周期主体 / 授权 / 证据、信息流白名单、确定性判定、
  Profile 结构不变量、审计身份、`LlmCall` 登记、语义身份、v1 只读兼容均已实现；
  D3D 验收时真实 PostgreSQL 全量 3587 项、HTTP client 注入反例 / 离线重放 / 公共只读 smoke、ruff check、ruff format --check、mypy strict 全绿；
  Schema current 74 份（2.0.0，含 B1 的 8 份、B2 的 13 份与 B3 的 15 份）逐字节一致 + legacy 35 份（`schemas/v1/`，1.0.0，逐字节不变）；
  Constitution `1.0.0 / Approved`；ADR-0001 ~ 0020 全部 Accepted
- 已实现但未完成 Phase 验收：Canonical、PIT / dataset / representation、Feature、State / Event、Outcome / Validation、Strategy / Backtest、Runner、Control Plane 与 Phase 2～14 模块（B1～B67）；本地 StorageAdapter、PyIceberg Catalog、生产表定义、D0～D3E 数据路径也已实现。Phase 1 E1-CAP-1 仍阻断，真实运行、人工知识审阅和各 Phase 验收仍独立待办。
- Phase 1：A2 / A2r、A3a / A3b、B1～C3、D0～D2 与 D3A～D3E 已由 Codex 复核通过；D3D `61dd9bf` 首轮退回 → D3D-R1 `c06b9fa` PASS；D3E `21e31f5` → D3E-R1 `52f7477` → D3E-R2 `c326434` → D3E-R3 `7e9e084` 已由 Codex 于 2026-09-27 验收；E1-CAP-1 是当前容量阻断
- Git 恢复点：Phase 0 基线仍为 tag `phase-0-complete`；远端恢复点是 `origin/main@44fe9a2`；ADR-0074 Operator、G1 两条边界测试、Wave A1–A4 与 Wave C1–C3 已择取 / 提交进入本地 main。`0651e6a` 为 A1–A4；`1ad56a0` 为 P12 exact-type/ref 前置拒绝、P13 source 调用前 admission check、P14 GoldenRecord immutable snapshot/hash recheck，并同步 ADR-0042 事实说明。两批经源码静态复核与 `git diff --check`，未运行测试 / build / lint / typecheck / probe / acceptance。P7 校验不能证明预期 output 全集完整，不能作为 admission 凭证；P3 旧测试断言和 P8 1.0.0 fixture / ID 登记待验收批次同步。本次状态同步后，本地 `main` 比远端超前 132 个提交、未推送，远端仅保留 `main`。G1 测试提交尚未运行。E1 PR #10 因容量门失败且与 main 冲突而关闭；候选 tip `c3868dc` 保存在 archive ref，对应 worktree 已移除。D3E detached tip `56710e7` 保存至 `refs/archive/2026-09-28/worktrees/phase1-d3e-acceptance`；其余四个已确认无独有代码且无活动会话的 detached worktree 已清理。研究规格分支 tip `82bfe73` 已存入 `refs/archive/2026-09-28/branches/claude/docs-research-spec-completion`，其中两条独有 G1 测试已迁入 main；已确认无未提交内容后关闭闲置会话并清理 worktree / branch，tip 由 archive ref 保留。`phase/1` tip 后续存入 archive ref 并删除；旧计划移至本机项目 archive。当前 1 个本地分支、1 个 worktree；`codex/p7-v5-operator-foundation` 已合入后存至 `refs/archive/2026-09-28/branches/codex/p7-v5-operator-foundation` 并清理分支 / worktree。模块基础逻辑收敛计划见 `docs/plans/2026-09-28-module-foundation-completion.md`。E1 主线 probe 与诊断均未运行；测试、build 和统一验收继续暂缓。
- P7 已合入 TrialLedger 单事件批量预登记与同实例线程锁（`d205bc4`, `0632367`）：单 journal event append 成功后再更新内存，批次冲突预先拒绝，旧单项 journal 可重放。锁保证仅适用于经同一 ledger API 的线程；不构成跨 journal typed-plan admission 事务。`git diff --check` 通过；未运行测试 / build / lint，仍待后续验收。
- Claude CLI 配额已恢复，本轮已用于 ADR-0073 只读复核与 durable 修复；全部改动经 Codex 复核后进入本地 main。
- ADR-0073 已接受（`835f405`）：执行 admission 的持久顺序为 PREPARE → 单个 TrialLedger batch event → COMMIT → memory admission checkpoint → external anchor；恢复只允许按完整匹配日志追加缺失登记 / commit / checkpoint，不运行 provider 或实验。要求 admission 属于唯一且已持久开始的当前 round；事务恢复后未记录的 started round 仍拒绝续跑，FAILED experiment round 仍按 ADR-0070 要求人工审查。新增 v4 state 才能启用 plan admission；v3 按旧形状继续但拒绝 typed-plan admission，不自动迁移。六类 operator 仍不可运行。
- ADR-0073 admission primitive 已实现并合入本地 main（`705b5e6`、`367648f`、`11f6ce2`）：严格 PREPARE / COMMIT plan journal、exact baseline TrialLedger batch recovery，以及只读 journal entries/head accessor。独立只读复核未发现会多计 trial 或接受额外尾记录的问题；composition root 仍须对照真实 TrialLedger、round-start、checkpoint 与 anchor。未跑 tests/build/lint；loop durable v4 coordinator 正在实现。
- ADR-0074 已依 Raphael 2026-09-28 的项目全权委托接受（本地 main `3d7cc23`）：范围限 synthetic-only、本机、有限轮数、静态 provider allowlist、外部 scheduler 与 round reports；v4 先于 operator-only v5。配置需显式指定 ADR-0062 freeze registry 与 anchor 路径，不扫描默认位置；当前没有冻结的合规 Validation Profile，因此暂不能产生运行配置；六类 P7 算子、实盘能力及 API 写触发均未开放。
- ADR-0073 补充了 plan journal v4 header 的精确事件 / payload、Hypothesis canonical sort key，以及未来 producer 必须验证的 ExperimentSpec ↔ Hypothesis ↔ lowered output 一对一依赖绑定。v4 持久协调不验证业务语义或 Provider 真实性，也不接 plan producer；operator / 六类 DSL 继续关闭。
- ADR-0073 durable v4 已在本地 main：`e3ac380` / `f91760b` 加固 PREPARE 前身份与状态检查、写入 / checkpoint 对称、同轮重复拒绝、FAILED fail-stop、恢复 anchor 顺序、header 初始化崩溃窗口、v3/v4 精确版本与异常映射，并做 provider-free recovery 路径和恢复写入前 lifecycle guard replay。复核发现 prepare→complete 之间的同进程 ledger 竞争仍依赖调用方契约，producer 接入前必须以 admission lease / 统一写锁强制覆盖完整事务。只做源码复核及 `git diff --check`；未跑测试、build、lint、typecheck、probe 或验收。
- ADR-0074 operator 专属 v5 identity 已在本地 main（`30a5dec` / `0288ff2`）：v5 memory fingerprint 与 plan journal header 均绑定规范 SHA-256；普通 v4 fingerprint / bytes 不变，v3/v4/v5 不迁移且跨版本 opener 拒绝。严格 TOML parser、六个静态 Provider allowlist、有限轮次 CLI、逐轮报告与 state / 外部锚点只读预检已合入本地 `main@0d4862a`，两轮源码静态复核无阻断项，未测试 / 验收。无冻结生产 Profile，因此 placeholder 配置必须被拒绝，当前没有合规运行配置；六类组合算子继续 fail closed。
- 两个 P7 任务分支已归档至 `refs/archive/2026-09-28/branches/codex/p7-durable-v4-hardening` 和 `.../p7-operator-v5-identity`，合入后移除分支 / worktree；当前 3 branches / 3 worktrees，未 push。根 `phase/1` Cursor worktree 与 Claude 保留 worktree 未改。
- 2026-09-28 E1-CAP-1 源码对账：positions、Canonical committed columns、ParsedArchive 整表、batch indexes 与完整 `revision_ids` 返回 tuple 存在随 N / batch 增长持有项；metadata / manifest 的 RSS 占比仍未测。完整工作集 32 MiB 门槛不变，绝不从 probe 排除 API 结果。当前 main 未运行 E1 probe；设计拆分为仓库有界状态、必要时的结果 API ADR、L/H 隔离容量测量，详见 `docs/reviews/2026-09-28-e1-cap1-design-reconciliation.md`。
- 2026-09-28 本地整合恢复点：E1-R 代码与状态文档快进到本地 `main`；该线比 `origin/main@44fe9a2` 超前 132 个提交、未推送。`phase/1` tip 留在 `refs/archive/2026-09-28/branches/local/phase-1` 后删除；Codex E1-R 分支 tip 留有 archive refs，代码已整合。**当时**仅 1 个本地分支 / 1 个 worktree，即 root main；Claude 研究规格分支已归档清理。旧根未跟踪计划保存在 `/home/raphael/.local/share/hlens-autoresearch/archive/2026-09-28/phase1-root-untracked/`。E1-R 新增 committed ID 重建和 Verifier snapshot 计数；`scan_column_batches` 的 PyIceberg 0.12.0 全路径内存界未证明，默认 temp 位于 tmpfs，E1-CAP-1 继续阻断。测试、probe、build、lint、typecheck 与验收未运行。
- 2026-09-28 当前开发恢复点：分支 `phase/1-foundation-completion`，commit `34b95c3`，相对本地整合 main 增加 E1 infrastructure Protocol / 内部代理接线；单独的测试辅助代理通过转发保留 head-read 记录语义。`git diff --check` 通过；测试、类型检查、lint、build、probe 和验收均未运行。后续路线由 ADR-0075 限定。
- 2026-09-29 W3-UNIVERSE-STREAM 已在隔离分支 `codex/e1-canonical-scratch-integration@7442bf66f14afdf5667ea1fc2a87de3f62eb2b0f` 实现并经不同 agent 独立 APPROVE。RunSet 树化 refs 和最多 fanout readers；Universe event / lineage / gap 外排、相邻去重、冲突 fail-closed。开发 suite `82 passed`，独立复核总计 `118 passed`；Ruff、format、mypy 4 files、diff-check 全通过。候选仍未整合，未运行 E1-CAP-1；DQ-9、PIT 单 key、Quality report events、完整工作集问题仍开。
- 2026-09-29 PIT run-ref list 收敛已在同一候选提交 `972c6c7980352ea546ff65a1914f18cf7e6a1733`；测试补充 `6a80b67030105420fe8f33306f59e14757e6ebc2` 独立 APPROVE。row/edge refs 改为 RunSet root，直接测试 compaction 与两个 root readers 的 fanout/关闭；selector + Dataset v3 source `41 passed`。单 key 历史/graph 与冲突 heads tuple 仍 O(N)，E1-CAP-1 未运行。
- ADR-0075（2026-09-28，Codex 依 Raphael 项目技术决策委托）：批准仅在 infrastructure adapter 实现固定 snapshot 的流式读取，PyIceberg Catalog / 写入权威与 core 契约不变；delete files fail closed。该决定只针对 scan planner / task / delete 集合，不关闭 E1-CAP-1；metadata、archive parse、结果 ID 和其它工作集仍待实现或证明。
- 2026-09-28 分支收敛恢复点：Phase 1 foundation 与 State ADR-0089 实现已并入 main；本地 / 远端只保留 main，root worktree 一个。旧本地分支 tip、旧远端 phase tip 和 State worktree tip 均保存在 `refs/archive/2026-09-28/` 下。`f84339a` 增加读回时可选的 State identity 核验；Phase 1 的 E1-CAP-1 阻断仍在。
- 2026-09-30 ADR-0094 contract 2.5.0 配套 follow-up：current-version expectations、API/Web `validation_report` DTO 与 OpenAPI types 已更新；2.4.0 六类报告文件逐字节重建并登记为 legacy，新增六个 2.5.0 current fixture，旧 2.0/2.1/2.2 与 descriptive fixtures 保留。合同定向回归 `1308 passed, 5 deferred`，catalog/API/report writer `172 passed`，Web test `120 passed`、build / Ruff / diff-check 通过。具体范围、命令与 pre-existing deferrals 见 `docs/reviews/2026-09-30-adr-0094-contract-250-followup.md`。这是 contract/fixture compatibility slice，不是 Phase acceptance；PIT per-key memory、E1-CAP-1 完整进程 32 MiB 与 Phase 1 验收仍开放。
- 2026-09-30 SQLite PIT selector/head traversal 已从 `codex/e1-cap-pit-sqlite-selector@7a43fcc` 择取进 `codex/project-consolidation@00594b8`。三个独立角色限范围 ACCEPT；direct `18 passed`，caller PIT/Dataset `63 passed, 4 skipped, 2 deselected`，proof caller `2 passed, 3 deselected`，Ruff/format/mypy/diff-check 通过。SQLite graph 已对每个 key/cutoff 构建一次并跨 simulation times 复用，可达性保留不可用中间节点，完整有序 heads 流入 ADR-0094 sink。重复 cutoff I/O/RSS、长链/宽 DAG 容量和完整 E1-CAP-1 仍未测；两个两轮失败的 PIT 节点仍 deferred，不计通过。下一步窄修 BatchGrid `FrozenMapping` 哈希 bug，再复核/择取 2.3/2.4 Dataset v3 固定 replay goldens；不整支合并旧分支，不推送或合并 main。
- 2026-09-30 随后两项分支差额已窄移植至 consolidation：BatchGrid `FrozenMapping` canonical hash fix `a237066`（3 个角色 ACCEPT，`27 passed`；payload/schema不变）；固定 2.3/2.4 Dataset v3 replay goldens `f19a06c`（3 个角色 ACCEPT，新增回放 `2 passed, 104 deselected`，canonical SHA 从旧基线独立重建一致）。证据文件见 `docs/reviews/2026-09-30-dataset-v3-historical-replay.md` 和 all-branch reconciliation follow-up。manifest 完整文件仍有 3 个已在父提交复现的 schema 描述/2.2 pins 失败（父：`3 failed,104 passed`；候选：`3 failed,106 passed`），不计通过；本轮未改 schema 或 pin。main 未合并或推送。

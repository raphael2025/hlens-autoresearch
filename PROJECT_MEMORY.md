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
- 当前阶段：Phase 0 已完成（tag `phase-0-complete`）；**Phase 1 已开启**（2026-09-24，分支 `phase/1`），B1～C3、D0～D2、D3A～D3D 已验收；D3E（`21e31f5`）→ D3E-R1（`52f7477`）→ D3E-R2（`c326434`）→ D3E-R3（`7e9e084`）与 D4 门记录均 REVIEW_PENDING

## 2. Current Architecture

- 工程基线：Python 3.13 + uv；契约用 Pydantic 写在 `core/`，JSON Schema 导出到 `schemas/` 并随仓库提交
- 契约版本 `CONTRACT_SCHEMA_VERSION = 2.0.0`：随 Phase 0 收口 fast-forward 合并进 `main` 并打 tag，
  **视为已发布**（D-25）——此后任何破坏性契约变化都必须升 major 并走 ADR；尚无 v2 数据登记。
  ADR-0052 §4 起为 **2.1.0**（minor）；ADR-0055 组合分支起为 **2.2.0**（minor，知识标签 / 资产）：持久化对象按记录版本重放，新增字段以 `_FIELDS_SINCE` 等声明引入版本（ADR-0052 / 0054 / 0057 / 0055）；当前版本新建对象的信封与哈希随 minor 变化，属预期
- 模型只接受同 major；`1.x` 走 `core/compat/v1.py` 只读入口（`schemas/v1/` 35 份快照 + `tests/vectors/v1/`）；
  v1 与 v2 的 `content_hash` / `experiment_hash` 不可比较；读取 v1 不赋予任何 v2 登记 / 晋升资格
- current Schema 135 份（组合分支契约 2.2.0；全代码 `a5836b2` 为 2.1.0），与 `CONTRACT_MODELS` 一一对应；研究 Provider Protocol 0 个（ADR-0017 的决定，不是遗漏）；
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

## 4. Current Phase

- Current Phase：Phase 1（Market Representation）**已开启**——Codex 依 Raphael 持续授权于 2026-09-24 开启（S0）
- Current Subphase：**D3E-R3 REVIEW_PENDING**；REST store / reconciler（`21e31f5`）→ R1 store 已存行核对（`52f7477`）→ R2 reconciler 比较前用共享核对器（`infrastructure/revision/row_integrity.py`）证明两侧每一行 → R3（`7e9e084`）把 REST 元素/响应行绑定到首次交付页的严格重新解码、归档行绑定到 D1 严格重新解析，reconciler 按快照时间旅行核对证据边批次；均待 Codex 复核（D3D / D3D-R1 `61dd9bf` + `c06b9fa` 已验收；已知阻塞见 Current Blocker）
- Current Objective：按 roadmap Phase 1 恢复序列 A3 → B1 → B2 → B3 → C1 → C2 → C3 → D / E / F → G 逐批实施；
  验收矩阵见 roadmap Phase 1；Provider 接口 / DTO / Schema / contract tests（B1 ~ B3）先于实现
- D3E / R1 / R2 待复核；D4 提案：Phase 1 首切片不启用 WebSocket live tail（ADR-0022 三项前置未全验收、无实时消费者；`docs/reviews/2026-09-25-d4-live-tail-gate.md`，待复核）；E0 等 Codex 验收 D3E 与 D4 后开放；无 Codex 时 Claude 的提交只推 `wip/phase-<n>-unreviewed`，不推正式 `phase/<n>`
- Current Blocker：无架构阻塞；D3E-R3 跨日错误（aggTrade 观察键的 REST revision 跨 UTC 日时另一天的边被误判）已由 `69f0bf0` 修复，待 Codex 复核；D3E 仍未验收。C2 的专用 catalog / test database、最小权限 role 与本机忽略凭据已创建并验收
- Next Milestone：Codex 对抗复核 D3E（含 R1/R2/R3），决定是否接受并开放 D4 / E
- 全阶段代码完成批次（2026-09-26，Claude）：分支 `claude/2026-09-26-code-completion-337e38` → `wip/all-code-completion`，B1～B54 为 CODE_COMPLETE / DEBUG_PENDING（非验收；B44～B54 为审计后续，B53 为集成会话按 Codex 复核的修复）；逐批证据见 `docs/plans/2026-09-26-all-code-completion-plan.md` §10

## 5. Active Decisions

- ADR-0001：重大架构决定用 ADR 记录；Agent 只能起草 Proposed
- ADR-0002：架构基线（原则 P1–P17、四个 Plane、默认技术栈）
- ADR-0003（D-06）：Python 3.13 + uv，与系统 Python 隔离
- ADR-0004（D-07）：本地 Git 仓库；不改全局配置；一次性提交身份（第 3 条"远程待定"被 ADR-0025 取代）
- ADR-0025：远程 = 私有 GitHub `raphael2025/hlens-autoresearch`；正式分支由 Codex 复核后推送；Claude 只快进推送 `wip/*` 与独立 `phase1/*` 工作分支（D-PUSH）；PR / CI 未配置
- ADR-0005（D-03）：研究 / 生产边界 = Artifact + Registry + Promotion + Equivalence Gate；Promotion 链（Registry / Promotion 服务 / Equivalence Gate）已实施（B28，失败关闭，今天拒绝所有策略）；Q-1 / Q-2 / Q-3 / Q-7 开放
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
- ADR-0027（D-33，Accepted 2026-09-25，部分实施）：D3B 已实现四张 additive Raw 表、REST 身份规则与纯 policy，D3C 已实现严格纯 decoder，D3D 已验收 `binance.spot.public-rest@1.0.0` collector（`61dd9bf` + `c06b9fa`；只经自建、无 hook / auth / 环境代理 / cookie 的 client 发送，不接受外部 `httpx.Client`）；归档路径与 `IDENTITY_HASH` 零改动；D3E store / reconciler 已提交（含 R3 `7e9e084`：REST 首次交付页重新解码、归档对象重新解析）、待 Codex 复核；D-33 方案 A 生效（规范内容投影逐字段相等才在独立证据表写 evidence-only 边 归档 → REST，项目政策、非来源先后）
- Raphael 授权（2026-09-25）："一切都你自己决定，允许多子代理，尽快开发"——Claude 可自行决定并接受 ADR（记为"依 Raphael 授权"），
  红线仍需 Raphael 本人：Constitution 原则 / 阈值、Profile 数值、实盘 / 资金 / 风险预算、删除历史数据、系统软件、生产部署、合并 `main`
- ADR-0032（D-HIST，**Raphael 批准** 2026-09-25）：`hlens.availability.archive-event-time-assumption@1.0.0`——数据集 PIT 规格显式绑定时，归档成交 / K 线以 `min(存储值, 可观察时刻 + 5 秒)` 为有效可用时间；存储、证据缺口、知识轴不变；未绑定即保守
- D-NET（Raphael 2026-09-26 批准推荐方案）：可下载 BTCUSDT / ETHUSDT 各 1～3 天 Binance 官方公共归档用于真实数据能力验证；只写本机，不入仓库，不形成市场结论
- Raphael 2026-09-26 批准：ADR-0052（验证契约补全：精确小数、Profile 新字段、负对照独立阈值）、ADR-0053（VALIDATION → FAILED）、ADR-0054（部分成交跨 bar 结转）；ADR-0051（上市历史假设，D-LIST）暂缓
- D-QGAP：方案 A（ADR-0031，证据缺口独立只追加表）；D-PUSH：只推 WIP 备份分支；D-P05（已被取代：Phase 0.5 按 ADR-0058 实施写入路径；ADR-0055 见下）
- 框架批次 ADR（2026-09-25，依授权 Accepted，全部 FRAMEWORK_IMPLEMENTED / NOT_VALIDATED，数值一律 TBD）：0034 知识库 · 0035 状态 · 0036 事件 · 0037 Outcome + 最小验证门 · 0038 策略 / 风控 / 回测 · 0039 状态×策略 · 0040 假设 + LLM · 0041 稳健性（G4、回溯审计不得翻转已拒绝对象） · 0042 合成市场 · 0043 路由 · 0044 事件总线 + worker · 0045 进化 · 0046 模拟执行（无实盘）· 0047 迁移 · 0048 API / Web · 0049 持续循环（worker 机制在 apps/worker，研究阶段在 research/loop，research 依赖 apps/worker 而非相反）· 0050 循环审计记录契约（只追加，描述既有字节）
- Raphael 指示（2026-09-25，/goal）："使用 4 个子代理加速开发，直到项目全部开发完成；不用调试，先按框架实现所有代码，每一步更新文档，开发完成后再逐个调试"——Phase 0.5、2～14 按路线图先实现框架代码（状态 FRAMEWORK_IMPLEMENTED / NOT_VALIDATED），Phase 1 收尾并行；红线不变（宪法原则 / 阈值、Profile 数值留 TBD；Phase 13 只做模拟 / 纸面，无交易端点 / 密钥 / 下单；`main` 合并与 tag 仍需 Raphael）
- Raphael 授权（2026-09-26，/goal）："所有的决策都由你来决定，包括红线的事情"——Claude 的逐项裁决见 `docs/reviews/2026-09-26-autonomous-decisions.md`（不做实盘 / 不冻结 Profile 数值 / 不合并 `main` / 无证据不晋升）；ADR-0056 事件表、ADR-0057 事件 subject、ADR-0058 知识库写入由 Claude 接受；Codex 全代码复核 K3 要求 ADR-0052 以 2.1.0 实施且旧 2.0.0 原样可重放；ADR-0054 / 0057 的新字段已于 B41 以 2.1.0 重新声明；`subject` = 调用方提供的稳定、大小写敏感 opaque ID
- 2026-09-26 由 Claude 依授权接受并实施：ADR-0056 事件表、ADR-0057 事件 subject（2.1.0）、ADR-0058 知识库写入、ADR-0059 G4 跨资产 × 横截面、ADR-0060 C-T4 市场基准（报告项）、ADR-0061 交互 DSL；ADR-0055（知识标签 / 资产检索：`tags_all` AND、`assets_any` OR 逐字精确，契约 2.2.0；资产是研究范围标识，不是上市 / 行情证据）能力方向由 Codex 依 Raphael 授权决定，实现 CODE_COMPLETE / DEBUG_PENDING（组合分支 `claude/adr-0055-integration`），ADR 仍 Proposed 待 Codex 复核组合分支；种子尚无经人工审阅的标签 / 资产（只有分类提案，不得当作已审阅数据）；ADR-0051（D-LIST）Proposed、Raphael 暂缓
- 开放问题：D-30 C-L5 embargo ↔ horizon 校验点（Phase 4 前）；D-29 worker ↔ research 边界（最迟 Phase 5 前）；D-04（Phase 4）
- Raphael 授权（2026-09-24）："授权所有"，Codex 全权接管决策 / 开发 / 测试 / 文档 / Git；Codex 解释为覆盖原则零变化的
  Constitution 1.0.0 发布与 Phase 0 收口（closure、`main` fast-forward、轻量 tag），并覆盖 C2 创建专用 PostgreSQL catalog /
  test database、最小权限 role 与本机忽略凭据，并覆盖 D0 / D1 对 Binance 官方公共归档、`.CHECKSUM` 与本机忽略小样本 smoke 的有限网络访问；D2 可使用这些已授权资源但不得新建或修改数据库 / role；不覆盖系统软件安装、PostgreSQL 系统配置、其他数据库 / role、账户 / 交易接口、原则或阈值变化、实盘、资金或风险预算

## 6. Active Constraints

- 硬性规则全文见 `CLAUDE.md` §3（H1–H14），摘要如下：
- 研究代码永不直接成为生产代码；LLM 不作最终裁决
- 不因回测结果修改 Constitution、Profile 或验证规则；Constitution 修改须按第九章另起 ADR 并由 Raphael 批准具体变化
- Outcome 不得作为 Feature / State / Event / Strategy 的输入
- 所有实验可复现；所有 Schema 版本化；失败实验与生命周期历史不可删除
- 环境变更（安装、系统配置、Docker、数据库、全局 Git 配置）需 Raphael 授权
- 不修改旧项目与外部数据
- Claude 不替 Raphael 做架构决策；Codex 在 Raphael 委托边界内作正式决定并记录（CLAUDE.md §0）
- Git：main 为稳定基线，实现工作走 `phase/*` 分支；合并进 main 需 Raphael 批准（或其已记录的授权）；正式分支由 Codex 复核后推送；Claude 只快进推送 WIP / 独立工作分支
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

- 全代码分支（2026-09-26）：全量非 PostgreSQL 门禁 7031 passed / 136 deselected（`3be497b`，审计后续 B44～B54，契约 2.1.0，未验收）；此前保留恢复点 `564c87c`；ADR-0052 独立 Phase 1 分支 `phase1/adr-0052-versioned-replay` `22392ea`（未并入 Phase 1 候选 / `main`）
- Date：2026-09-25（Phase 1 正式恢复点；全代码分支见上一条）
- Stable recovery point：Phase 1 D3D-R1 修复提交 `c06b9fa`（Codex 已独立复核；随 D3D 验收门推送）；Phase 0 基线仍为轻量 tag `phase-0-complete`
- closure commit 的父提交：`3257e6e`（ADR-0020 / Constitution 1.0.0，Codex 已复核）；
  其前：`4a2951a`（ADR-0019，C3 复验）、`9581773`（ADR-0018）、`1ad9f59`（ADR-0016，Codex 独立复验）
- State：契约、状态机、只读载荷、实验身份、版本语法、生命周期主体 / 授权 / 证据、信息流白名单、确定性判定、
  Profile 结构不变量、审计身份、`LlmCall` 登记、语义身份、v1 只读兼容均已实现；
  D3D 验收时真实 PostgreSQL 全量 3587 项、HTTP client 注入反例 / 离线重放 / 公共只读 smoke、ruff check、ruff format --check、mypy strict 全绿；
  Schema current 74 份（2.0.0，含 B1 的 8 份、B2 的 13 份与 B3 的 15 份）逐字节一致 + legacy 35 份（`schemas/v1/`，1.0.0，逐字节不变）；
  Constitution `1.0.0 / Approved`；ADR-0001 ~ 0020 全部 Accepted
- 未实现（按 roadmap 延期）：Canonical、PIT / dataset / representation、Research Provider Protocol、Feature / Strategy / Backtest、Runner、Control Plane（Strategy Registry 已于全代码分支实现，B28）；本地 StorageAdapter、PyIceberg Catalog、12 张生产表定义、D0 / D3D collector、D1 / D3C parser、D2 archive revision store 与 D3E REST store / reconciler（待复核）已实现
- Phase 1：A2 / A2r、A3a / A3b、B1～C3、D0～D2 与 D3A～D3D 已由 Codex 复核通过；D3D `61dd9bf` 首轮退回 → D3D-R1 `c06b9fa` PASS；D3E `21e31f5` → D3E-R1 `52f7477` → D3E-R2 `c326434` → D3E-R3 `7e9e084` 均 REVIEW_PENDING
- Git：`phase/0` 保留；`main` 由 `2e2a0ad` fast-forward 到 closure commit `1e208b5`（= `phase-0-complete`）；
  `phase/1` 从该 commit 创建（Phase 1 工作分支）；`main`、`phase/0`、`phase/1` 与 tag 已推送到私有 GitHub 远程 `origin`

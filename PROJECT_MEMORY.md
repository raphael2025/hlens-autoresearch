# HLENS-AutoResearch Project Memory

> 给 Claude 的长期项目记忆：只保存跨会话仍然有效的事实。
> 维护规则见 `CLAUDE.md` §8（目标 < 200 行，> 300 行必须 Compaction）。
> 当前进度看 `PROJECT_STATUS.md`；完整架构看 `docs/architecture/`；决定全文看 `docs/adr/`。
> 恢复点（2026-10-03）：`main@25ffada5` 全仓门禁全绿；ADR-0111 重构目标已 Accepted，归档与切片按 `docs/plans/2026-10-03-archive-and-bypass-action-plan.md` 执行。最高指导文档：`REFACTOR_TARGET.md`。

## 1. Project Identity

- 名称：HLENS-AutoResearch
- 定位：长期演化、模块化、可插拔、可验证的加密市场自动化研究基础设施（不是交易机器人或回测框架）
- 核心目标：持续吸收公开知识、已有策略和失败经验，通过组合与实验验证产生、检验新假设
- Phase 1 数据范围：Binance 公共 spot `BTCUSDT` / `ETHUSDT`，归档 aggTrades + 1m klines（ADR-0022）；
  正式研究标的与周期（D-09 提案为 BTCUSDT 1H）仍待 Phase 4
- 当前阶段：Phase 0 已完成；Phase 1 = 垂直切片（ADR-0111），未验收；Phase 0.5、2 ~ 14 与 apps 为 FROZEN / ARCHIVED
- 决策权：`CLAUDE.md` §0 单一授权表（ADR-0111）——Owner = Raphael；Lead = Claude Code 主会话；Codex / Cursor / 子代理为执行者。实盘、宪法原则、红线变更、删数据、系统级环境归 Raphael；H3 / H4 / H6 不受任何授权改变

## 2. Current Architecture

- 工程基线：Python 3.13 + uv；契约用 Pydantic 写在 `core/`，JSON Schema 导出到 `schemas/` 并随仓库提交
- 契约版本自已发布的 `2.0.0` 向前兼容演进：2.1.0（ADR-0052）→ 2.2.0（ADR-0055）→ 2.3.0（ADR-0077）→ 2.4.0（ADR-0088）→ 2.5.0（ADR-0094 PIT v3）→ 当前 **2.6.0**（ADR-0109：v3 manifest 旧质量表有 snapshot 才绑定）；破坏性变化须升 major 并走 ADR。持久化对象按记录版本重放；当前版本新建对象的信封与哈希随 minor 变化属预期
- current Schema **148 份**，与 `CONTRACT_MODELS` 一一对应；legacy v1 35 份（`schemas/v1/`）只读；v1 与 v2 哈希不可比较，读取 v1 不赋予任何登记 / 晋升资格
- 研究 Provider Protocol 0 个是 ADR-0017 的决定；Data Plane Adapter Protocol 3 个（Storage / Catalog / Collector），可复用 suite 在 `tests/contract_suites/`
- Freeze Contracts, Evolve Implementations；四个 Plane：Data / Research / Control / Application；Research ⟂ Application
- PostgreSQL = Control Plane（不存大型行情）；研究数据层 = DuckDB / Polars 直读不可变 Parquet + manifest（ADR-0111）；原 Iceberg 数据平面原地冻结；
  Iceberg Catalog 用独立 PostgreSQL 库、warehouse 为本地 `file://`、Phase 1 ~ 6 无 NATS（ADR-0021）
- 数据架构冻结正文（表、分区、source / parser / policy / universe 标识符、PIT I/O、依赖与设置字段）：`docs/architecture/03-data.md`
- Provider / Plugin 架构；LLM、Backtest Engine 均可替换；LLM 只产出数据，永不作裁判
- Domain 层无基础设施依赖；依赖方向 apps → application → domain ← plugins/infrastructure；research 依赖 apps/worker 而非相反（ADR-0049）
- 新运行能力一律默认关闭：P7 执行（`P7ExecutionSwitch` 默认关，`TypedPlan.runnable` 默认 False）、P12 循环内替换提案（默认关）、实盘（ADR-0084）
- 全文：`docs/architecture/00-overview.md`

## 3. Current Research Direction

Market State → Feature / Event → Knowledge Retrieval → Hypothesis → Combination → Experiment → Validation → Research Memory → Strategy Evolution

- 新颖性主要来自确定性的组合 / 条件化 / 时序算子，并且每次组合都计入尝试次数
- 路线图：`docs/research/roadmap.md`
- 开发顺序（ADR-0111 起）：先打通 Phase 1 垂直切片（真实数据 → 特征 → 带成本回测 → 指标），验收后才考虑重开其它 Phase；“全阶段框架代码先行”的旧顺序作废

## 4. Current Phase

- Current Phase：Phase 1 垂直切片（`REFACTOR_TARGET.md` §5、§6），未验收
- Current Blocker：真实数据未到位（仓库内只有两天 1m K 线，无 aggTrades）；切片代码未写
- 其余 Phase 0.5 / 2 ~ 14 与 apps：代码已写、从未验收，FROZEN / ARCHIVED，待迁入 `archive/`
- Next Milestone：Action Plan 批次 A0 ~ A5（归档）与 B0 ~ B4（切片）→ Phase 1 验收
- 随重构冻结（记录保留、不再推进）：E1-CAP-1 的 K 轴、D-META-AGE、DQ-9、P11 真实运行、知识库种子审阅、真实 Catalog 建表、Profile 数值

## 5. Active Decisions

**基础与 Phase 0**
- ADR-0001：重大架构决定用 ADR 记录
- ADR-0002：架构基线（原则 P1–P17、四个 Plane、默认技术栈）
- ADR-0003：Python 3.13 + uv，与系统 Python 隔离
- ADR-0004：本地 Git，不改全局配置，一次性提交身份；ADR-0025：远程 = 私有 GitHub `raphael2025/hlens-autoresearch`，CI 未配置
- ADR-0005：研究 / 生产边界 = Artifact + Registry + Promotion + Equivalence Gate；Promotion 链今天拒绝所有策略；Q-1 / Q-2 / Q-3 / Q-7 开放
- ADR-0006：生命周期 v2（OOS → PAPER → PRODUCTION_CANDIDATE → ACTIVE；ACTIVE 带 `execution_mode`，不设 LIVE 状态）；Q-4 ~ Q-6 开放
- ADR-0007：三层验证（Constitution / Validation Profile / Experiment Metadata）+ 两步冻结
- D-09 数值（TBD-1 ~ TBD-5）与 H-3 ~ H-7：未批准，Phase 4 校准后冻结为 Profile 参数；Constitution 不得出现数值阈值
- ADR-0008 ~ 0016：只读载荷与内容哈希、完整实验身份、构造与版本语法、生命周期主体 / 授权、信息流白名单（Outcome 不得作输入）、确定性判定与拒绝 NaN / ±Inf、Profile 结构不变量、审计身份类型、`LlmCall` 登记结构
- ADR-0017：Provider 方案 B——Protocol / DTO / contract tests 随首次消费的 Phase 交付
- D-25：2.0.0 已发布，同类改变必须升 major
- ADR-0018：三类语义身份排除信封版本；ADR-0019：生命周期转移证据至少一项非空
- ADR-0020：Constitution `1.0.0 / Approved`，正文 sha256 `4d603d62…259cd` 不变，只前向适用

**Phase 1 数据**
- ADR-0021：PyIceberg SQL Catalog on 独立 PostgreSQL 库、`file://` warehouse、Phase 1 ~ 6 无 NATS
- ADR-0022：Binance 公共 spot BTCUSDT / ETHUSDT，归档优先 + market-data-only REST 补尾；无交易能力或密钥
- ADR-0023：历史轴（`available_time`）与知识轴（`knowledge_time`）分离；append-only revision；competing heads fail closed
- ADR-0024：双轴 listing episode 历史；`UniverseSelectionSpec` 在 manifest 绑定
- ADR-0026：PyIceberg day transform 写入用官方 `pyiceberg-core` extra
- ADR-0027（D-33）：REST 四张 additive 表；规范内容逐字段相等才记"归档优先"证据边，否则 fail closed
- ADR-0028：Canonical revision 与 Raw 元素一一对应，跨通道边由 PIT 映射不物化
- ADR-0029：listing 来源 = `exchangeInfo` 快照，`tradable_from` 为本机观察下界
- ADR-0030：FeatureProvider 契约，执行器截断输入 + 因果扰动测试
- ADR-0031（D-QGAP）：质量报告证据缺口写独立只追加表
- ADR-0032（D-HIST，Raphael 批准）：数据集显式绑定时归档可用时间取 `min(存储值, 可观察时刻 + 5 秒)`，未绑定即保守
- ADR-0033：物化 Research Dataset 选择表登记为生产表
- ADR-0051（D-LIST）：上市历史"观察状态回填"假设；政策 **1.1.0** 写入 BTCUSDT / ETHUSDT 下界 2017-08-17（2026-09-30 经 data.binance.vision / data-api.binance.vision 核实）；只是研究假设，不证明真实上市史
- ADR-0075：PyIceberg 固定快照有界扫描（infrastructure 内），delete files fail closed
- ADR-0076：有界 Canonical normalization result
- ADR-0077：有界 Research Dataset 证据（v3 manifest、内容寻址 evidence streams；DQ-1 由 Raphael 批准）；Universe v3 显式 `UniverseRunParams`；DQ-9 数值待容量证据
- ADR-0093：Quality 报告固定大小 manifest + 内容寻址 evidence streams；v1 / v2 只读兼容
- ADR-0094：PIT v3 完整冲突 heads 写有界 evidence stream（契约 2.5.0）；v2 与 2.3 / 2.4 replay 不变
- ADR-0097：PIT bounded graph 用调用级 SQLite scratch index
- D-NET：可下载 BTCUSDT / ETHUSDT 各 1 ~ 3 天官方公共归档，只写本机、不入仓库
- ADR-0108：Raw 归档元素与 Canonical 写入为一个逻辑单元一个 Iceberg snapshot（infrastructure 内暂存多文件提交，不改契约；旧逐微批历史只读兼容；snapshot 不过期）；单元数据文件上限为一个 4096 行 row group（整单元大文件会使有界扫描 O(单元)）；残余随单元数增长记为 D-META-AGE
- D-E1-CANONICAL-SCRATCH：Canonical 位置索引用 `Settings.canonical_scratch_uri`（默认 `data/scratch`），不回退 `TMPDIR`

**Phase 0.5 / 2 ~ 14 与 apps**
- 框架批次 ADR-0034 ~ 0050：知识库、状态、事件、Outcome + 最小验证门、策略 / 风控 / 回测、状态×策略、假设 + LLM、稳健性、合成市场、路由、事件总线 + worker、进化、模拟执行（无实盘）、迁移、API / Web、持续循环、循环审计契约
- ADR-0052（Raphael 批准）：精确小数、Profile 新字段、负对照独立阈值（2.1.0）；ADR-0053：VALIDATION → FAILED；ADR-0054：部分成交跨 bar 结转
- ADR-0055：知识标签 / 资产检索（2.2.0）；ADR-0058：知识库经人工审阅写入；种子无具名审阅的标签 / 资产
- ADR-0056 / 0057 / 0061 / 0066：事件表、事件 subject、交互 DSL、显式 Event 建表入口（默认无副作用）
- ADR-0059 / 0060 / 0064 / 0065：G4 跨资产 × 横截面、C-T4 市场基准、G4 成交量来源一致、结转余量失败关闭
- ADR-0062：Profile 冻结登记（追加式、目录外锚点）是 Promotion 的权威冻结来源；登记为空
- ADR-0063：本机只读 API 运行时 = Uvicorn（仅 127.0.0.1）
- ADR-0067：P11 显式劣化检查绑定调用方声明证据；ADR-0098：P11 权威 = append-only `LifecycleRegistry` 重放 + 钉定 v3 Dataset source + 与 validation 同源的 metric 闭集（**取代 ADR-0080**）
- ADR-0068 / 0070 / 0071 / 0073 / 0078 / 0083：P7 闭世界 typed plan、失败轮 fail stop、只读复核摘要、PREPARE / TrialLedger / COMMIT admission 恢复、lowered outputs 完整性、失败轮人工重试
- ADR-0074：synthetic-only 本机有限批次 operator；无冻结 Profile 时没有合规运行配置
- ADR-0082 / 0088 / 0099：P7 六类算子语义与 lowering；契约 2.4.0 组合扩展；`rank` / `quantile` 时间序列语义
- ADR-0069：P12 `combine` 对风险 / 适用范围冲突失败关闭
- ADR-0072：Phase 4 交付最小验证门与封存 OOS，Phase 8 交付稳健性扩展
- ADR-0079 / 0081 / 0085 / 0086 / 0087 / 0089 / 0092：paper deviation 声明范围、报告 payload DTO 版本、研究库扩展、验证与生命周期收口、插件发现、State 物理表、Promotion 可信验证重放
- ADR-0084：实盘接口预留，默认关闭
- ADR-0091：登记处完整性审计严格只读；Failure Registry 不提供历史防篡改证明
- ADR-0095：生产 Worker 由部署方显式受信 Runtime Factory 组合
- ADR-0096：TrialLedger 完全相同的登记作只读幂等确认，不增加 trial
- ADR-0100（2026-09-30，Raphael 直接指令）：P7 执行 Provider + allowlist 编译器（默认关）、横截面 `rank_cs` / `quantile_cs` Provider、P11 指标闭集与默认环境、ADR-0051 政策 1.1.0、E1 有界化、P12 可选循环内替换提案；修订 1 细化 1.3.0 时间事件。横截面 Provider 已在显式 pinned universe 下编译为专用根 Provider（2026-10-01），输出不进入单序列组合器 / Research Loop。
- ADR-0101：Dataset v3 生产入口（显式 JSON `DatasetBuildProfile`、CLI / worker job / verifier 工厂、上游与质量报告入口，含 listing 报告）；修订 2 把"旧质量表有 snapshot 才绑定"转入 ADR-0109
- ADR-0102 / 0104 / 0106：State run / show / list 入口；paper deviation 绑定运行（修订 ADR-0079）；P14 迁移目标 = 独立参考回测引擎（关闭 P14-TARGET）
- ADR-0103 / 0110：P7 计划绑定（`hlens.p7.plan@1.0.0`）、PREPARE 证据、拒绝审计、COMMIT → 循环交接；准入候选经 `P7PlanSource` + COMMIT 证明在持久化恢复中重建；开关仍默认关
- ADR-0105：P11 运维收口（Lifecycle 写入 CLI、基线导出、批量驱动、Dataset 版 operator）；D-P11-WINDOW = 固定日历
- ADR-0107 Rejected（被 ADR-0108 取代）；ADR-0109：契约 2.6.0，v3 manifest 的旧质量表"有 snapshot 才绑定"（关闭 D-V3-LEGACY-BIND）
- D-STATE-INC 暂缓（ADR-0035）

**重构**
- ADR-0111（Raphael 确认）：`REFACTOR_TARGET.md` 为最高指导文档；Phase 0.5 / 2 ~ 14 归档；研究数据层直读 Parquet（取代 ADR-0021 对该方案的否决）；Phase 1 关闭条件 = 切片验收；单一授权表

## 6. Active Constraints

- 硬性规则全文见 `CLAUDE.md` §3（H1–H14），摘要如下：
- 研究代码永不直接成为生产代码；LLM 不作最终裁决
- 不因回测结果修改 Constitution、Profile 或验证规则（H3）；不削弱测试（H4）；不删除失败实验或生命周期历史（H6）
- Constitution 原则修改须按第九章另起 ADR 并由 Raphael 批准具体变化
- Outcome 不得作为 Feature / State / Event / Strategy 的输入；任何计算只用 `available_time ≤ t` 的数据
- 所有实验可复现；所有 Schema 版本化
- 不猜测或冻结 Profile 数值；新运行能力默认关闭，验收前不得打开
- 实盘（下单、连接实盘账户、交易凭据）始终需要 Raphael 亲自批准
- 不修改旧项目与外部数据；不 force push、不改写已发布历史、不修改全局 Git 配置

## 7. Current Known Risks

- Iceberg snapshot 不过期：ADR-0108 已实现，metadata 不再随行数增长，但仍随历史单元数线性增长（D-META-AGE），PyIceberg `load_table` 物化全部 snapshot，生产规模回填前须另行决定
- pypi.org 索引域名在本机被阻断（files.pythonhosted.org 可达）：离线安装用 uv.lock 精确版本
- WSL 内存约 15 GiB；Docker 未安装；外部数据盘未挂载；warehouse 无异地副本；CI 未配置
- 官方资料不能证明任何历史 revision 的公开时刻：`binance.spot.publication@1.0.0` 一律 `available_time = ingest_time` + 证据缺口，未绑定 ADR-0032 假设时早于本机 ingest 的历史不可用；归档替换一律 competing heads
- REST 同样无公开时刻证据；未证明 aggTrade ID 连续、REST 与归档内容一致、未结束 K 线可判别——一律 fail closed
- D-33 精确比较：REST 毫秒 vs 2025 年起归档微秒，带亚毫秒位的成交无法证明相等
- 契约层只校验结构与声明：依赖闭包、trial 权威账本、Registry 存在性、泄漏检测、哈希与真实内容一致、Profile 已冻结等属运行时服务义务，不得宣称已防住
- `LlmCall` 与生命周期证据只保证登记结构非空；内容可取回、批准人权限与职责分离未实现
- 契约层不拒绝 `to_mode = LIVE`；运行时拒绝 LIVE，无下单接口或凭据
- JSON Schema 在几处弱于运行时模型；权威校验必须经过运行时模型
- `venue` / `symbol` / `timeframe` 区分大小写、不做规范化；未来 Adapter 必须产出规范值
- 外部是否存在 v1 历史数据证据不足：不得宣称迁移路径已在真实数据上验证
- 后代 G5：每个 family 只评估一次密封 OOS（C-S1..3），循环内替换提案必须使用独立预登记密封窗口，绝不复用已开封窗口
- 旧研究可能已看过 BTC 全部历史：历史封存区在认知上不完全干净

## 8. Important Historical Context

- 旧研究项目 `alpha-autoquant` 位于 `/mnt/e/alpha-autoquant`（只读参考，不得修改）
- 现有系统 `hlens-cryptoplus` 位于 `/mnt/e/hlens-cryptoplus`（只读参考，不得修改）
- 旧研究中的 anti-leakage / red-team 规范可能成为新系统素材（尚未评估内容）
- 旧策略或旧结论进入新系统时必须重新登记并重新验证，不能直接信任
- Phase 0 审查链：C1 `FIX_BEFORE_CLOSE` → ADR-0018 / 0019 → C3 `READY_FOR_HUMAN_CONSTITUTION_GATE` → ADR-0020；记录见 `docs/reviews/`
- 授权演变：2026-09-23 至 2026-09-30 的多段授权互相冲突（D-AUTH-CONFLICT），2026-10-03 由 ADR-0111 收敛为单一授权表
- 2026-10-01：17 个 Codex 临时分支 tip 已保存至 `refs/archive/2026-10-01/branches/codex/`，分支与干净 Codex worktree 已清理；Claude feature worktree 的未提交 / 未合入内容仍需逐项审阅，不计为主线完成。具体当前数量见 `PROJECT_STATUS.md` 快照。

## 9. Last Known Good State

- Phase 0 基线：tag `phase-0-complete`
- `main` ← `phase/1` 经 PR #20 / #21 整合（2026-10-01）：四项代码缺口与 E1 Canonical 窗口复用收口；全仓门禁全绿（pytest 9313 passed / 144 skipped / 0 failed，PostgreSQL 用例另行实跑；ruff、format、mypy 通过）。见 `docs/reviews/2026-10-01-w1-gate-repair.md`
- 历史契约固定值的核对方式：`tests/contract_version_support.py::at_contract_version` 在新解释器里按记录时的契约版本重建；只有证明差异仅来自契约信封或已接受 ADR 的有意变化时才可重钉
- `main@25ffada5`（2026-10-03，PR #23 / #24）：第三轮收口 + E1-CAP-1 修复，全仓门禁全绿（四段运行，10572 passed / 148 skipped / 0 failed；ruff、format、mypy 通过）
- E1-CAP-1 正式矩阵 `main@25ffada5`（K = 0）证据级 PASS：每 stage 随 N 增长 ≤ 12.2 MiB；记录 `docs/reviews/2026-10-03-e1-cap1-main-25ffada5.md`
- E1-CAP-1：`main@50d6bb6` 正式矩阵数值 PASS（2026-10-02），但 Iceberg 元数据随批次数线性增长，未关闭；ADR-0108 已实现，待在 `main` 上重跑（含预填充历史）。记录见 `docs/reviews/2026-10-02-e1-cap1-main-50d6bb6.md`

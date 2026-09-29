# HLENS-AutoResearch 项目驾驶舱

> 面向项目所有者 Raphael。技术细节见 `docs/`，AI 工作规范见 `CLAUDE.md`，Claude 的长期记忆见 `PROJECT_MEMORY.md`。
> 本文件在**每次阶段变化、每个阶段完成、每次出现 ARCHITECTURE_DECISION_REQUIRED** 时必须更新。

## 1. 当前状态

| 项 | 值 |
|---|---|
| 项目版本 | 0.0.0 |
| 当前 Phase | **Phase 1 — Market Representation：🔄 已开启**（2026-09-24） |
| 当前子阶段 | W1 PostgreSQL-enabled 全量：`7 failed, 8814 passed, 1 skipped, 72 deselected, 5 warnings`；两轮延期节点不计通过，仍属发布门。隔离候选分支 `codex/w1-independent-integration` 包含 E1 spool、W5 seed guard、W1 文档、P7 修复、Web fixture inventory 和依赖安全更新。W6：State `44 passed`；Event/Outcome `203 passed, 1 failed`，失败为 15-vs-17 registry count，历史轮次无法核实，未重跑。W7：P7 精确文案节点修复后通过，P8 selected slice `148 passed`。W10：Web build / components `120 passed`；library `110 passed, 1 failed`，retro-audit 节点两轮失败后延期；依赖修复已在集成候选 clean install/build 与 audit 复核通过，ECharts 视觉复核待做。W2：API runtime `25 passed`，stdout JSON logging 接入并禁用 access log；State adapter reopen `28 passed`；Event Iceberg reopen 整文件排除未分类 registry 节点后 `29 passed, 1 deselected`，均为临时 SQLite、非跨进程证据。Worker 当前无独立进程入口。E1+W5 定向组合回归 `178 passed`、独立 review 无 P1/P2。E1-CAP-1 仍未通过；PIT 单 key、Quality report event list、Profile、红队与发布证据未闭合。Phase 1 未验收。 |
| 上一 Phase | Phase 0 — Research Constitution：✅ 已完成（tag `phase-0-complete`，last known good） |
| 总体状态 | 长期分支 `main` 未改动；隔离集成候选 `codex/w1-independent-integration` 未推送或合入 main。W1 全量含两轮延期发布门。W6 / W7 / W10 有并行模块级进展如上，但不构成 Phase 验收；E1-CAP-1 仍阻断 Phase 1。W8–W12 多数工作包及 Profile、红队、最终集成证据仍待推进。 |
| 全阶段代码完成批次 | 分支 `claude/2026-09-26-code-completion-337e38` → WIP `wip/all-code-completion` 的 B1～B67（含 P10 证据决定）已整合；`CODE_COMPLETE / DEBUG_PENDING` 只表示该批任务完成，不代表所有规划能力齐备或 Phase 验收。P7 已补直接引用校验、admission journal / TrialLedger 崩溃恢复基础、v5 operator identity 与 ADR-0074 本机有限批次入口；六类算子语义与 Provider lowering 仍未批准。逐批记录见 [完成计划 §10](docs/plans/2026-09-26-all-code-completion-plan.md) |
| 最后更新时间 | 2026-09-29 |

Phase 0 的全部验收标准已满足：研究宪法已发布为 **`1.0.0 / Approved`**（ADR-0020，原则正文零变化、无数值阈值、只前向适用）；
领域契约、状态机、三层验证契约、错误分类、Schema 导出与工程基线均已实现并通过两轮关闭复审
（C1 `FIX_BEFORE_CLOSE` → ADR-0018 / 0019 修复 → C3 `READY_FOR_HUMAN_CONSTITUTION_GATE`）。
`phase/0` 已 fast-forward 合并进 `main`，并打轻量 tag `phase-0-complete`；契约 `2.0.0` 随之视为**已发布**，
此后任何破坏性契约变化都必须升 major 并走 ADR。

Codex 依 Raphael 2026-09-24"授权所有"的持续授权，于 2026-09-24 **明确开启 Phase 1**。架构决策子阶段已完成：
四份 ADR 经 A1 起草、A1r / A1r2 两次按 Codex 退回意见修正后，由 Codex 独立复核通过并**接受**——
ADR-0021（本地数据基础设施：PostgreSQL 独立库做 Iceberg Catalog、本地 `file://` warehouse、Phase 1 ~ 6 不用 NATS）、
ADR-0022（Binance 公共现货 BTCUSDT / ETHUSDT，无任何交易能力）、ADR-0023（历史可用时间与本机知识时间分开；修订只追加，无法判定先后即失败）、
ADR-0024（按当时可交易集合构建标的池）。A2 已把它们同步进数据架构文档，并冻结首批表名、分区、数据源版本、依赖清单与设置字段。
Iceberg Catalog、本地 StorageAdapter、八张生产表定义、D0 Collector、D1 fail-closed parser 与 D2 append-only revision store 已验收。D2 首轮复核发现来源 checksum 真实性与 arrival anchor 全表物化两个缺陷；D2-R1 修复后由 Codex 复现旧提交四项失败、运行真实 PostgreSQL 全量与静态检查并接受。D3A 是 REST Raw / lineage 的 docs-only 架构门：Codex 复核草案后退回八项缺陷，D3A-R1 修正后由 Codex 独立复核接受 ADR-0027。D3B 首轮复核发现极端十进制异常泄漏与伪造 / 过期比较可生成边；D3B-R1 修复后由 Codex 接受。D3C 首轮复核发现 RFC JSON 框架空白被误拒；D3C-R1 修复后由 Codex 以 `python -O` 对抗探针、真实 PostgreSQL 3434 项全量与静态检查接受。D3D 首轮复核发现完整 HTTP client 注入可在 allowlist 后加入凭据并改写到外域账户路径；D3D-R1 删除该入口并关闭环境代理与 Cookie 回放，Codex 独立复现修复、运行真实 PostgreSQL 3587 项全量后接受，D3E 开放。D3E 已把已提交的 REST 采集写成 Raw 响应 / 元素 revision，并实现跨通道比对与证据边；首轮复核后经 R1 / R2 / R3 修复与复核，Codex 于 2026-09-27 接受 D3E；D4 已关闭。Phase 1 尚未整体验收，E1-CAP-1 容量边界仍阻断。

代码仓库使用私有 GitHub 远程 `raphael2025/hlens-autoresearch`（ADR-0025）；PR #1～#11 的历史保留，GitHub Actions CI 尚未配置。当前长期本地 / 远端分支只有 `main`；必要时可建临时隔离分支，整合后清理。
此前的远端 `phase/1-foundation-completion` 已于本轮并入 `main` 并删除；旧 tip 与所有本地 feature tip 均已保存在 archive refs。

Raphael 2026-09-24 的“授权所有、全权接管并开发 / 测试 / 决策 / 文档”明确覆盖 C2 所需的 H12 环境变更：
创建专用 PostgreSQL catalog / test database 与最小权限 role、写入仅本机且被 Git 忽略的凭据文件，并覆盖 D0 / D1 对
Binance 官方公共归档与 `.CHECKSUM` 的有限网络访问及被 Git 忽略的小样本 smoke。该授权不含安装系统软件、修改 PostgreSQL 系统配置、触碰其他数据库 / role、
创建 Control Plane 库、账户接口或任何实盘能力。

收口依据的是 Raphael 2026-09-24"授权所有"的持续授权：Codex 判定它覆盖原则零变化的 Constitution 1.0.0 发布、
Phase 0 closure commit、`main` fast-forward 合并与轻量 tag；**不**覆盖任何原则或阈值变化、实盘、资金或风险预算。

## 2. 当前进度

| Phase | 名称 | 状态 |
|---|---|---|
| 0 | Research Constitution | ✅ 已完成（`main`，tag `phase-0-complete`） |
| 0.5 | Public Knowledge Base | 🧱 检索、审阅写入与标签 / 资产路径已实现（ADR-0034 / 0055 / 0058）；四条 2026-09-26 种子已显式固定 `schema_version: 2.1.0`。种子标签 / 资产仍需具名人工审阅；新增种子的黄金哈希覆盖尚未补齐。**Phase 0.5 未验收** |
| 1 | Market Representation | 🔄 D3E 已独立验收、D4 已关闭；RSS probe 与诊断均未运行。主线 E1-R 已包含 committed ID 重建、有界 snapshot match index 与固定快照流式扫描基础，但完整工作集容量仍未证明：PyIceberg metadata、archive parse、完整 result ID tuple、generic history fallback 的 `seen` set、builder caller-held snapshot ID set 与 metadata/manifest 增长仍在审计范围。完整 tuple 计入 32 MiB。E1-CAP-1 继续阻断，Phase 1 未验收。路线见 [E1-CAP-1 对账](docs/reviews/2026-09-28-e1-cap1-design-reconciliation.md)。当前代码检查与验收状态见模块计划及本文件 §7。 |
| 2 | Market State Engine | 🧱 框架与 Iceberg 持久化已实现（ADR-0035 / 0089）；诊断载荷 1.1.0 记录来源 `StateResult.result_hash`（裸序列为 null），旧 1.0.0 报告可原样读回并复原 id；读回可按 `expected_spec` 校验身份；来源哈希只是报告声明、不认证 Registry 存在性；`tests/infrastructure/state` 27 项通过，State CLI / catalog、类型检查与阶段验收未完成 |
| 3 | Event & Interaction Engine | 🧱 Provider、交互 DSL、统计与物理表定义已实现（ADR-0036 / 0056 / 0061）；独立 Event 表操作命令按 ADR-0066 已通过 PR #6 合并；生产 catalog 尚未建表；Phase 3 未验收 |
| 4 | Outcome Engine + 最小验证门 | 🧱 框架已实现（ADR-0037）；`EventResult → OutcomeEvent` 纯转换已在 main，以 event id / 可观测时间构造 Outcome 标签输入；Outcome 表持久化、可选多种子负对照、ADR-0052 契约 2.1.0 与研究侧精确取值已在 `main`；ADR-0060 市场基准；Profile 数值 TBD；未跑测试，Phase 4 未验收 |
| 5 | Strategy Library + 回测 | 🧱 框架已实现（ADR-0038）；横截面动量 `xsmom_bars`（研究层）CODE_COMPLETE / DEBUG_PENDING；ADR-0054 部分成交结转已以 2.1.0 声明；ADR-0005 Promotion 链 CODE_COMPLETE / DEBUG_PENDING（今天所有策略都被拒）；Promotion 的权威冻结来源改为追加式、带目录外锚点的 Profile 冻结登记（ADR-0062 **Accepted** 2026-09-27，Codex；B56，CODE_COMPLETE / DEBUG_PENDING；登记为空、Profile 数值未冻结，不是 Phase 4 / 5 验收）；Promotion 与路由一样要求 Profile 所要求的反向对照报告项（B59）；无策略晋升 |
| 6 | State × Strategy | 🧱 矩阵计算、全单元预登记与逐单元验证已实现；P6 循环在共享决策网格上做精确因果归属。通用矩阵 API 不沿用较早状态；如需非共享网格的 as-of 状态延续，先修订 ADR-0039。循环报告接线已在 main；未验收 |
| 7 | Dynamic Discovery | 🧱 严格草稿、人工审阅、声明式参数点批次、知识检索来源、non-runnable typed-plan parser、直接引用校验、PREPARE / COMMIT admission journal、TrialLedger 精确批次恢复、durable v4 写入 / 恢复加固、跨 prepare→complete admission lease、durable store 写 gate、round/approval 串行化、close 后拒写与只读 journal view，以及 operator 专属 v5 身份基础均已进入本地 main。源码独立复核与 `git diff --check` 通过；未运行测试 / build / lint / Phase 验收。恢复路径不运行研究 Provider/compiler/experiment，生命周期 guard 在恢复写入前纯重放；v5 只绑定 operator identity，不提供运行入口。producer 接入前仍须验证 ExperimentSpec ↔ Hypothesis ↔ output 一对一关系。六类组合算子继续 fail closed。ADR-0070 令失败轮停止；ADR-0071 提供只读复核摘要。ADR-0074 已确定合成数据、本机、有限轮次 operator 边界，当前没有冻结 Profile，不能形成合规运行配置。outcome 自动恢复和人工修复工具仍缺。LLM 内容核验可选，未核验调用不满足完整可复现审计 |
| 8 | Validation & Robustness | 🧱 G4、多标的验证等逻辑已实现；回溯审计 writer / API / Web 页面已合入本地 `main`，gate diff 包含实际与精确阈值（报告 schema 1.1.0）；Python 静态检查和 Web build 通过，未跑测试；fixture / smoke / component 注册留待验收；Phase 8 未验收 |
| 9 | Synthetic Market Lab | 🧱 框架已实现（ADR-0042）；检测器异常计 INCONCLUSIVE、可选实际运行 G5、多标的校准模式（B26）、配置错误不再被吞、错误时给出通过率区间（B45 / B48）CODE_COMPLETE / DEBUG_PENDING；点估计也固定局部 Decimal context，避免调用方 context 改变报告；中等规模证据报告（单标的 250、双标的 200 种子，B52）已提交，两份均已在原代码基线上逐字节复现（B54 / B57）；只给证据不选数值；本次修改未运行测试 / 验收 |
| 10 | Dynamic Strategy Router（纸面） | 🧱 框架已实现（ADR-0043，仅纸面）；无候选明确停止、运行哈希复核、资格证据模式、纸面偏差报告、路由自身验证（B31 / B34）；证据模式要求 Profile 所要求的市场基准与反向对照报告项（B51 / B58），**不要求 Profile 冻结登记**（B62；冻结权威门只在 Promotion，ADR-0062）CODE_COMPLETE / DEBUG_PENDING |
| 11 | Continuous Research Loop | 🧱 框架已实现（ADR-0044 / 0049 / 0050）；P11 operator、ProfileFreezeRegistry 锚点、provenance 页面与 CLI 已在 main。worker journal 新增 POSIX `flock` 与 stale-writer 拒写，保持行格式 / hash 不变；未运行测试，Phase 11 未验收。没有 source resolver，调用方声明的源与聚合真实性不认证 |
| 12 | Strategy Evolution | 🧱 框架已实现（ADR-0045）；ADR-0069 已要求 `combine` 对风险策略、适用标的、冲突参数空间 fail closed，代码已修改、待验收；替换提案仍恒待人工批准并在循环外运行（B49），Phase 12 未验收 |
| 13 | Production Adaptive System（仅模拟，无实盘） | 🧱 框架已实现（ADR-0046，实盘结构上被拒绝）；持久审计、非空审计重开即急停、只读重放、风险 / 告警重放（B31）CODE_COMPLETE / DEBUG_PENDING；SecondLineRisk.mark 现在先验证整批价格，避免无效价格造成部分状态变更。roadmap 已对齐 Accepted ADR：验收覆盖 Kill Switch drill、二道风控拒绝审计与 replay；paper deviation 属于 P10 / ADR-0043，不代表执行服务与回测等价；新增逻辑未测试，Phase 13 未验收 |
| 14 | Technology Migration | 🧱 框架已实现（ADR-0047）；金标准记录持久化、差异报告、回滚证据、金标准实验重放（B32）CODE_COMPLETE / DEBUG_PENDING（无具体迁移目标）；现有 conformance 调用方覆盖 Knowledge / EventBus，其他 suite 是可复用检查集合，尚无全 suite migration matrix |
| apps | api / worker / web | 🧱 框架已实现（ADR-0048，只读）；任务端点、知识检索错误码、全页加载 / 空 / 错误状态、研究循环页、15 个只读页面（含 P8 回溯审计）、Jobs 与报告种类页；P8 的 fixture 生成器登记留待验收同步；未做浏览器手工验收 |

图例：🧱 = 按 Raphael 2026-09-25 指示先实现的框架代码（FRAMEWORK_IMPLEMENTED / NOT_VALIDATED），全部完成后逐个调试与验证；CODE_COMPLETE / DEBUG_PENDING = 2026-09-26 全代码批次补齐、有定向测试但未独立调试，**不是**验收；阈值 / Profile 数值一律 TBD；实盘相关一律不实现。

## 3. 已完成

- ✅ 架构蓝图：11 份架构文档、路线图；ADR-0001 ~ 0027 已 Accepted（0026 / 0027 已实施；Phase 1 数据工作仍需逐项阶段复核，见 ADR 索引）
- ✅ 工程基线：Python 3.13 + uv（ADR-0003）、Git（ADR-0004；私有 GitHub 远程与复核后推送 → ADR-0025）；pytest / ruff / ruff format / mypy strict 全绿
- ✅ 研究 / 生产边界（ADR-0005）、生命周期 v2（ADR-0006）、三层验证架构与两步冻结（ADR-0007）
- ✅ 契约修复 B1 / B2 / ADR-0010：只读载荷、完整实验身份、构造路径与版本语法、v1 只读兼容
- ✅ B3（ADR-0011 ~ 0017）：生命周期主体与授权、信息流白名单、确定性判定与数值合法性、Profile 结构不变量、
  审计身份类型、`LlmCall` 登记结构、Provider 交付节奏（方案 B，Provider Protocol 数为 0 是决定）
- ✅ 关闭复审 C1（`FIX_BEFORE_CLOSE`）→ ADR-0018 语义身份、ADR-0019 生命周期证据最小结构 → C3 修复后复验
  （`READY_FOR_HUMAN_CONSTITUTION_GATE`）
- ✅ 研究宪法 `1.0.0 / Approved`（ADR-0020，第一至第九章正文 sha256 不变）
- ✅ Phase 0 正式关闭；`main` fast-forward；tag `phase-0-complete`
- ✅ Phase 1 S0（docs-only）：从 `main` 创建 `phase/1`，Phase 1 开启并进入架构决策子阶段
- ✅ Phase 1 A1 → A1r → A1r2 → A2（docs-only）：ADR-0021 ~ 0024 Accepted，首切片冻结，架构决策子阶段关闭
- ✅ Phase 1 A3a（Cursor）：锁定 §6.1 四个直接依赖与 `uv.lock`；import smoke 通过；Codex 已复核并推送
- ✅ Phase 1 A3b（Cursor）：`infrastructure.Settings` 符合 §6.2；Codex 对抗复核、1469 项全量测试通过并推送（`d840dbb`）
- ✅ Phase 1 B1（Claude）：8 个双时间 / revision DAG 契约与 Schema；Codex 发现并退回 dangling ID 跨 key 归属漏洞，修复后独立复核、1658 项全量测试与静态检查通过（`b15faa9`）
- ✅ Phase 1 B2（Claude）：13 个 universe / listing / manifest 契约与 Schema（current 59 份）；Codex 两轮设计/对抗复核后补 listing revision 归属唯一、listing lineage 不悬空，并将 lineage 第三跳改为通用 `source_*`；Codex 独立运行 1938 项全量测试、5 个恶意 payload 与静态检查通过（`b41a46a`）
- ✅ Phase 1 B3（Claude）：Storage / Catalog / Collector 三个 Protocol + 15 个 DTO 与 Schema（current 74 份）；`tests/contract_suites/` 可复用检查被两个不同替身通过、并杀死 34 个单点故障用例；两轮返修关闭 batch 内容核对、URI 绝对性与编码路径、origin 端口及内部 API 暴露问题；Codex 独立运行 2528 项全量测试、恶意 URI / 端口探针、ruff / format / mypy 与冻结文件比较全部通过（`9c57253`）
- ✅ Phase 1 C1（Cursor Auto）：本地 `file://` StorageAdapter 经三轮对抗返修关闭短写、路径 / payload TOCTOU、FD 泄漏、不可用 ref 与整根替换；Codex 独立运行 2574 项全量测试、根替换反例与静态检查通过（`7857039`）
- ✅ Phase 1 C2（Claude Opus）：PostgreSQL-backed PyIceberg Catalog、定义注册、真实快照 / 时间旅行、重启幂等与乐观并发；Codex 独立运行 2630 项全量测试、12 轮同 batch 并发探针、权限 / 残留 / 密钥检查与静态检查通过（`373e286`）
- ✅ Phase 1 C3（Claude Opus + Cursor Auto）：八张冻结生产表、`hlens.pyarrow-batch-sha256@1.0.0`、分区演进与幂等建表；ADR-0026 / D-32 增补官方 `pyiceberg-core` extra；Codex 独立运行 2728 项全量测试、8 表真实 PostgreSQL 写入 / 重启重放探针、残留 / 密钥 / 冻结边界检查与静态检查通过（`973ffbd`）
- ✅ Phase 1 D0（Cursor Auto）：`BinanceSpotArchiveCollector` 只访问配置 archive base，严格 `.CHECKSUM` 先验校验、流式 staging / 原子发布、缺口与有界重试；经两轮对抗返修关闭中途流错误不重试与 base URL 静默改写；Codex 独立运行 2780 项全量测试、流中断 / 恶意 base 探针、残留 / 密钥 / 冻结边界检查与静态检查通过（`7a9f468`）
- ✅ Phase 1 D2（Claude Opus）：`infrastructure/revision/` 的身份规则、`binance.spot.publication@1.0.0`、`binance.spot.archive-revision@1.0.0` 与 `RawRevisionStore`；归档对象内容寻址，序号 block 以归档表为 anchor 全在 Iceberg 内分配；经 D2-R1 修复后由 Codex 独立运行真实 PostgreSQL 全量 3091 项、回归反例与全部静态检查并接受（`b05486b`）
- ✅ Phase 1 D2-R1（Claude Opus）：关闭来源 checksum 伪造与 arrival anchor 全表物化两个缺陷；四个新增回归测试在旧提交 `ba9f417` 上由 Codex 独立复现为全部失败，在修复提交全部通过；未改冻结 Schema 与 precedence policy hash
- ✅ Phase 1 D3A（Claude Opus，docs-only）：ADR-0027 草案 `7111e54`；Codex 复核退回八项缺陷（目标窗口与页面合法性矛盾、响应缺知识时间、预算耗尽冒充来源缺口、重放义务、序号守卫位置、批次与设置、证据措辞、状态索引）
- ✅ Phase 1 D3A-R1（Claude Opus，docs-only）：逐项修正；因现有表的内嵌证据无法表达"归档取代后到的 REST"，改为**四张** additive 表（第四张为独立 precedence 证据表）；D3B～D3E 按四表方案重拆（`ed526f7`）
- ✅ Phase 1 D3A 接受门（docs-only）：Codex 独立复核 `ed526f7` PASS，ADR-0027 Accepted、D-33 方案 A 生效；四张表、六个标识符、四项设置并入冻结数据架构；验收记录 `docs/reviews/2026-09-25-d3a-adr-0027-acceptance.md`
- ✅ Phase 1 D3B / D3B-R1（Claude Opus）：四张 REST 表定义、独立 REST 身份规则、REST availability / precedence 与 D-33 通道等价纯函数；R1 关闭极端十进制异常泄漏与伪造 / 过期比较可生成边；Codex 对抗复核、真实 PostgreSQL 3252 项全量与静态检查通过（`3b267a0` + `02c0418`）
- ✅ Phase 1 D3C / D3C-R1（Claude Opus）：严格、无 I/O 的 `binance.spot.rest.decoder@1.0.0`（`infrastructure/parser/binance_rest.py`）——完整正文 + 已校验页查询 + `retrieved_at` + 目标窗口 + 上一页摘要 + 正文上限（纯参数，非设置）→ 不可变元素 + 确定性页摘要，或一条整页拒绝（零元素）；R1 修复 RFC JSON 框架空白；Codex 独立对抗探针、真实 PostgreSQL 3434 项全量与静态检查通过（`643cf45` + `6b9e670`）
- ✅ Phase 1 D1（Claude Opus）：`binance.spot.archive.parser@1.0.0` 按覆盖日选择毫秒 / 微秒，严格 ZIP / CSV 与零容差覆盖边界，失败只产结构化质量事件且不泄露部分 rows；Codex 独立运行 2955 项全量测试、70,000 行末尾失败原子性探针，并真实解析两个日期的 kline 与 aggTrades 官方归档（`c966085`）
- ✅ Phase 1 D3D / D3D-R1（Claude Opus）：`binance.spot.public-rest@1.0.0` 同步 collector——结构化端点 allowlist、不跟随重定向、有界正文与重试、`Retry-After` / 418 / 5xx / 页数预算、D3C 驱动分页、不可变正文与 page / collection checkpoint、同 `request_id` 零网络重放与崩溃恢复；D3D-R1 删除可绕过 allowlist 的完整 client 注入并关闭环境代理 / Cookie 回放；Codex 独立安全探针、公共只读 smoke、真实 PostgreSQL 3587 项全量与静态检查通过（`61dd9bf` + `c06b9fa`）
- ✅ 全阶段框架代码（2026-09-25，Raphael 指示；FRAMEWORK_IMPLEMENTED / NOT_VALIDATED）：Phase 0.5、2～14、研究控制台与跨阶段接线，ADR-0034～0049；逐个调试进行中

## 4. 当前正在做

- ✅ 框架整合：B1～B67 与 ADR-0052 / 0055 等代码已分批整合；PR #6 合并模块代码 / 内容收口批次（基线 `4875e92`），PR #7 将合并后项目状态同步至 `f58e8ec`（docs-only）。代码仍是 `CODE_COMPLETE / DEBUG_PENDING`，没有把全量门禁或主线合并当作 Phase 验收。
- 🔨 Phase 1 当前阻断：D3E（含 R1 / R2 / R3）已接受，D4 已关闭；E1-CAP-1 的 500k resume / replay 增长为 59.9 / 63.9 MiB，超过 32 MiB。已重申既有容量口径：完整进程工作集均计入（含 PyIceberg metadata、Parser / scan 临时对象、normalizer 状态与 API 返回对象）；history 与 normalizer 候选尚未达到或证明门槛，Phase 1 仍未验收。
- 🧩 Phase 4 Event→Outcome 基础接线：`EventResult` 到 `OutcomeEvent` 的纯转换已实现并公开导出，以 `event_id` / `event_time` 保留事件身份与可观测时刻；Codex 复核待做，测试与阶段验收未运行。
- ✅ 模块收口批次：Phase 0.5 因子 / 特征 / Event 草稿、Event 字段说明、Loop / Router / API README 与 Phase 3 Event 表操作命令均已通过 PR #6 合并到 `main`（`4875e92`）；不构成阶段验收。
- ✅ 模块差额整合：P6 矩阵报告接线与错误归因、P8 显式输入的回溯审计 writer / 只读 API / Web 页面、P11 精确 Decimal 阈值读取及研究库规格已快进到本地 `main`（`669704c`）；静态检查 / Web build 有通过记录，未跑测试、未做 Phase 验收。
- 🔎 深审还确认：P7 参数点批次可运行；六类组合 DSL 现有 typed plan parser 只验证声明数据并始终返回不可运行，Provider lowering 与审计持久化仍未实现；LLM 内容核验仍可选。P2 Arrow 表满足 ADR-0035 当前范围，额外 Iceberg 持久化暂缓。P11 本机 CLI 只包装显式输入，不解决权威 ACTIVE / 真实观测来源；P0.5 标签、P3 生产建表、Profile 数值和真实数据仍是人工 / 数据 / 授权门。
- ✅ 缺口复核：P9 calibration detector 异常已由基线归为 `INCONCLUSIVE` 并保留其他结果；backlog 旧记录已标记为过期。Knowledge Search 缺 Provider 时基线已返回稳定 503，OpenAPI / README 同步，无需改代码。
- ⏸ P2 报告 API payload 按 `ReportEnvelope.payload` 以 JSON 字典如实对外；逐 kind DTO 目前缺少统一、版本化 payload schema，且旧报告字段不同。为避免新增未批准的 report contract 或拒收既有报告，暂缓把前端断言改成严格 schema；需要先定义各 report kind 的版本化模型。
- ✅ P7 / ADR-0074 operator 基础已合入本地 main（`0d4862a`）：严格 TOML parser、静态 Provider allowlist、有限轮次 CLI、运行前 code / environment / path / state / anchor 预检与逐轮报告；显式提供 ADR-0062 freeze registry 与外部 anchor 路径。两轮源码静态复核无阻断项，`git diff --cached --check` 无输出；未运行测试、build、lint、typecheck、probe 或验收。当前没有冻结 Profile，故没有合规可运行配置；不接 API 写触发、不启用六类算子。

## 5. 下一步

### 我（Raphael）需要做

- 暂无需要你处理的 Git 分支决定；本轮授权由 PM 完成整合与清理。
- 知识条目 tags / assets 仍需具名人工审阅；Profile 数值、交易 / 风险预算与实盘能力保持冻结 / 关闭。

### Claude Code 需要做

- 继续把 E1-CAP-1 当作 Phase 1 阻断项；候选 `fix/e1-cap1@a75278e` 的 59.9 / 63.9 MiB 结果超过 32 MiB，不能外推为 main 的测量或降低容量门槛。
- State ADR-0089 实现已进入 main，但静态审查发现 `state_ref` / `spec_hash` 的读取身份核验缺口；修复与回归验证完成前不得标为验收。
- 后续任务均以 `main` 为基线；只有隔离工作确有需要时才创建临时分支，整合后删除。

### Codex 当前工作

- W1 全仓基线修复：API/报告 261 passed、PIT/dataset 分区 103 passed 后另将 5 个失败用例逐项复验通过、revision 562 passed / 20 skipped、canonical normalizer 75 passed、State 27 passed、docs consistency 7 passed、payload immutability 201 passed、研究循环/假设批次多组定向回归通过；live API smoke 1 passed。策略/验证目录的长跑在 87 passed、1 failed 后因单个慢用例中断；当前哈希断言单测通过，但完整回归未重跑。旧 PIT proof-scan hook 已切换到真实的 `scan_column_batches` 路径，两项专项通过；volume-bar 规则测试现 10 passed。严格 mypy 现 731 个源文件全绿。全仓 pytest 与最终 Ruff / 格式复核尚未完成。详细事实见 [WBS W1](docs/plans/2026-09-28-project-completion-wbs.md)。
- 本轮源码修复、测试修复和生成 fixture 仍在当前工作树，需完成差异审阅、剩余回归和文档校正后再形成提交；整合与静态检查不代表 Phase 验收通过。E1-CAP-1 仍阻断 Phase 1。

## 6. 当前待决策

**D-DEBUG（历史决定，2026-09-28；已被后续目标取代）**：当时暂缓测试以先补齐各模块基础代码。Raphael 后续要求推进完整工程交付；当前按依赖顺序持续实现、测试、独立复核、集成与运行验证，不得因“代码已写”或单测通过就标为完成。E1-CAP-1、Constitution / Profile 与研究诚信边界仍按现行 ADR 保持。

**D-PM-AUTH（Raphael 2026-09-28 明确授权）**：工程、架构与模块语义决定由 Claude Code（PM）按经验作出并记录到 ADR / 项目状态；除实盘交易操作外，开发阶段事项（含冻结契约变更、环境安装、数据下载、建表、推送 / 合并）均已授权。实盘接口可预留但必须默认关闭，不得启用、连接真实账户或下单。研究诚信规则与不可逆真实数据处置仍按相应硬约束执行。

**D-OPS1 / ADR-0091（Accepted，2026-09-28）**：登记处审计必须严格只读，不得创建目录 / 锁文件或自动修复 anchor；按现有存储格式报告证据强度。Failure Registry 当前无可信 hash chain / anchor，只能结构核验，不宣称可识别合法历史改写。

**DQ-1 / ADR-0077（已决定，2026-09-28 Raphael 选 A）**：批准在 `core/contracts` 新增 2.3.0 additive 的有界 Dataset evidence manifest 模型，v2 `ResearchDatasetManifest` 字段 / 哈希不变、只读兼容；实施为单独串行的 core 任务（W3-E1DS）。DQ-9 chunk / fanout 参数待容量证据。

**D-E1-CANONICAL-SCRATCH（已由 PM 决定，2026-09-29）**：Canonical 位置索引使用 `Settings.canonical_scratch_uri`，默认仓库拥有的 `data/scratch`，通过 `HLENS_CANONICAL_SCRATCH_URI` 可覆盖；运行时显式注入，不回退到 `tempfile` / `TMPDIR`。Settings/composition root 是路径 owner；不改 `StorageAdapter`，spill 磁盘用量不宣称固定上界。提交 `codex/canonical-position-bounds@991b126` 已通过独立 review，尚未整合；branch-local 集成测试失败不列为项目延期。32 MiB E1 容量门仍开放。

**E1-Q-STREAM（ARCHITECTURE_DECISION_REQUIRED）**：Quality `existing_only` 的 report `events` 当前是无界单行 `List<Struct>`，冻结行格式不能逐事件流式重放。推荐新增版本化的内容寻址事件流协议，保留旧 inline 报告只读兼容；还需决定失败验证时是否允许留下无引用 orphan。决策包：[Quality bounded replay](docs/reviews/2026-09-29-quality-existing-only-bounded-replay-decision.md)。该项不阻塞其它 Phase 1 模块。

**E1-PIT-CONFLICT（ARCHITECTURE_DECISION_REQUIRED）**：PIT v3 bounded 路径的 `maximal_heads` 完整 tuple 在单次 evaluation 存在 O(N) 结果风险。推荐将完整 head 列表写入 bounded evidence stream，v2 tuple 路径不变；需确认新结果形态及报告绑定。决策包：[PIT conflict output](docs/reviews/2026-09-29-pit-bounded-conflict-output-decision.md)。单 key 内部无界图仍按 ADR-0077 继续修复，不等待该输出决策。

**代码补全轮次暴露的待 Raphael 决定（2026-09-28，PM 汇总；不决定时相应代码保持 fail closed）**

| ID | 问题 | 来源 |
|---|---|---|
| D-P11-AUTH | Phase 11 非 synthetic loop 的 ACTIVE 权威 head、真实 source 身份与各 metric 精确算法 | ADR-0080 BLOCKED |
| D-P7-OPS | P7 其余五类算子（conditioning / temporal / transformation / ensemble / negation）的研究语义 | ADR-0082 OPEN |
| D-P11-WINDOW | 滚动持续循环与固定日历 Validation Profile 如何配合（换窗口需新 Profile） | ADR-0049 遗留，AUD-2b |
| D-CATALOG-TABLES | 是否授权在真实 Catalog 创建 `event.*` / `state.*` 表 | ADR-0035 / 0036 / 0066，H12 |

**E1-CAP-1 容量边界核对**

| ID | 问题 | 决定 | 状态 |
|---|---|---|---|
| D-E1-BASE | E1 后续实现以哪条代码线为基线？ | **以当前本地 `main` 为整合基线；已归档的 `fix/e1-cap1` 仅作按路径择取的代码与容量探针参考，不整支合并。** 候选 `history()` 对重复 snapshot ID 的查找顺序与 `main` 的 first-listed 语义不一致；任何移植都要保留 `main` 的历史、固定 snapshot 与拒绝规则 | 已决定（2026-09-27，Codex 依 Raphael 全权授权）；候选失败记录保留，`main` 容量尚未测 |
| E1-HIST | PyIceberg metadata、Parser / scan 临时状态和 API 返回对象是否计入 E1-CAP-1？ | **计入完整进程工作集**，既有 32 MiB 门槛与验收条件不变；不因对象来自第三方依赖而排除 | 重申既有容量口径，非架构变更；见 `docs/reviews/e1-bounded-history-options.md`；E1 仍阻断 |
| E1-CAP-ARCH | 在当前 PyIceberg 路径和已调查假设下，是否已有符合既定容量门与历史语义的实现方案？ | 依 Raphael 2026-09-28 的项目统筹授权，先按已记录的 E1-R 设计路径补实现基础切片，不改 32 MiB 门槛、冻结契约或权威 metadata 读写 / 历史保留语义；E1-API 若需改变公开结果类型，仍先出 Proposed ADR | E1-R 已有 batch-history / pinned streaming scan / disk-backed positions / committed-row 校验、committed ID 重建与有界 snapshot match index；新提交 `64b021a`、`1d8f231`、`62bfc9d` 均未验证。archive parse、完整 result tuple 与调用方增长项仍待处理或测量；整体容量设计与 E1-CAP-1 仍开放、阻断 |

既有 E1-CAP-1 标准不变：完整进程工作集都计入容量测量，且增长须满足已记录的 N / batch-count 上界。已记录的 59.9 / 63.9 MiB 是 `fix/e1-cap1` 候选分支数据，不是当前 `main` 的容量结果；候选探针以 10 ms 间隔采样 `/proc` VmRSS、每个 N 仅运行一次，元数据阶段约解释 20 MiB，其余约 40 MiB 未归因。`main` 自身没有 resume / replay 探针；当前实现已磁盘化 positions 与 batch-history 索引，并将 committed-time 和 close 的 arrival_seq 精确比较改为批次扫描 + 磁盘索引，尚未验证。Arrow 单批、archive parse、完整结果 ID / commits 与 Iceberg metadata 等增长仍未解决或测量，因此不得外推候选数值或宣称 `main` 通过。

**P11-LOCAL-OPERATOR（Codex 已选择最小本机入口）**

现有 ADR-0067 与 operator 规格允许显式一次性调用适配。只实现 `python -m research.operations.degradation_cli`：所有对象、时间窗、冻结登记 / 锚点与报告目录都需显式传入；不接 loop、预算、当前 ACTIVE resolver、自动指标聚合或调度。不新增 ADR、契约或 API 写入口；调用方提供的历史与观测仍是明确标注边界的声明数据。实现待统一验收。

**B65 Uvicorn 安装（已完成）**

| ID | 问题 | 方案 | 状态 |
|---|---|---|---|
| D-UVICORN | ADR-0063（Codex 已决定用 Uvicorn 作本机只读 API 运行时）需要把 `uvicorn` 作为可选 `api-server` 依赖并装入项目 `.venv` | Codex 选 A：只用本机缓存的 uvicorn 0.53.0、可选 extra、不触网；仓库改动和本机安装已完成 | 2026-09-29 loopback HTTP 与 SIGTERM/SIGINT graceful shutdown `4 passed, 1 warning`；无待授权项 |

**B66 G4 容量成交量来源（ADR-0064 已由 Codex 接受）**

| ID | 问题 | 方案 | 状态 |
|---|---|---|---|
| D-VOLSRC | 数据集路径上 G4 容量所用的成交量（特征 manifest 的 `bar_volume`）可能与实际执行的价格 bar 的成交量不同，今天静默使用前者 | Codex 选 A：两者须精确相等，不一致即 INCONCLUSIVE（`bar_volume_source_mismatch`），缺值仍为 `bar_volume_missing`，合成路径不变 | 已决定并实施：ADR-0064 Accepted（2026-09-27，Codex）；B66 CODE_COMPLETE / DEBUG_PENDING |

**D-STATE-INC（已决定：暂缓）**

| ID | 问题 | 决定 | 边界 |
|---|---|---|---|
| D-STATE-INC | 状态执行器是否增加"可选增量评估路径"以降低逐时刻计算成本（ADR-0035 已知缺口） | **Codex 不批准**（2026-09-27）：保留逐时刻路径，因为因果保证来自每个时刻只看到当时可见的数据，等价测试不足以证明新路径不削弱这一点 | 需要真实性能基线与不暴露未来数据的逐步协议后才重新评估；这是暂缓优化，不代表没有性能问题（ADR-0035） |

**P12-LOOP（已决定：有意暂缓）**

| ID | 问题 | 决定 | 边界 |
|---|---|---|---|
| P12-LOOP | 替换提案是否由持续循环自动触发 | **Codex 决定**（2026-09-27）：否。提案留在循环外的显式作业；循环只到 OOS，OOS → PAPER 须人工批准，循环内提前提案会虚假宣称资格；后代不得复用密封窗口 | 循环内触发是**有意暂缓**，不是当前范围内未实现的代码任务；将来自动触发须另立 ADR，定义独立、预先登记的密封评估与证据 / Profile 规则；不新建 family、不猜测阈值 |

**P10-FREEZE（已决定）**

| ID | 问题 | 决定 | 边界 |
|---|---|---|---|
| P10-FREEZE | 路由证据模式是否要求 `ProfileStatus.FROZEN` 与 `ProfileFreezeRegistry`？ | **Codex 选否**（2026-09-27，依 Raphael 授权）：路由证据模式只证明研究层报告条件；不读取冻结登记 | Profile 冻结登记仍是 Promotion 的权威门（ADR-0005 / ADR-0062）；路由通过不证明 Profile 已冻结、策略已晋升或具备生产资格，见 ADR-0043 B62 |

**D3A 提出的决定（已决定，部分实施）**

| ID | 问题 | 方案 | 状态 |
|---|---|---|---|
| D-33 | 同一笔成交 / 同一根 K 线既可能来自官方归档、也可能来自 REST 补尾；按 ADR-0023 它们是同一观察的两条 revision，没有证据即互相冲突，数据集 fail closed。不裁决就等于禁止 REST 补尾 | **Codex 选 A**（ADR-0027 §4）：本机比较两者的规范市场内容，逐字段完全相同才记一条"归档优先"的项目政策证据（存入独立证据表）；不同或无法比较就不记、继续 fail closed。这是项目规则，不是交易所声明的先后 | 已决定：ADR-0027 Accepted（2026-09-25），方案 A 生效；D3B 纯 policy、D3C decoder 与 D3D collector 已验收，D3E 已开放 |

**E1 之后提出的待决定（ARCHITECTURE_DECISION_REQUIRED）**

| ID | 问题 | 方案 | 状态 |
|---|---|---|---|
| D-E2 | 标的上市 / 下架历史（E2）从哪里来？ | 已取证（`docs/architecture/evidence/binance-spot-listing.md`）：官方唯一无凭据来源是 `exchangeInfo` 的**当前**状态快照，没有上市 / 下架日期、历史、修订时间或稳定产品 ID。已起草 [ADR-0029](docs/adr/0029-listing-history-source.md)（Proposed），建议 A：新增该端点与一张原始表，从本机首次观察开始记录，`tradable_from` 明确标为"本机观察下界"。无论选哪个方案，历史回测期的标的池都仍不可构建（与行情数据同一证据缺口） | ✅ 已决定（2026-09-25，Claude 依 Raphael 授权）：ADR-0029 Accepted，方案 A；E2 开始实施 |
| D-F4 | 首批特征计算接口（`core/contracts/` 新增 FeatureProvider 契约）的字段与防泄漏方式 | 已起草 [ADR-0030](docs/adr/0030-feature-provider-contract.md)（Proposed）：由执行器在每个时刻只交出"历史可用且在知识截止之前"的输入，再用契约测试做因果扰动；新增模型，不改已发布契约 | ✅ 已决定（2026-09-25，Claude 依 Raphael 授权）：ADR-0030 Accepted，方案 A；F4 开始实施 |
| D-PUSH | 推送规则历史记录：2026-09-25 的接管指令限定 WIP 推送 | 当时方案只推 `wip/phase-1-unreviewed`；后续授权由 2026-09-28 的 `CLAUDE.md` §10 覆盖 | 已被 2026-09-28 main-only 分支策略取代；当前由 PM 推送整合后的 `main` |
| D-E1n | ADR-0028 实施记录：规范层批次号多了"单元行数"（E1-R1）与"批大小"（E1-R3）两段，保证原始单元变化即拒绝、续跑只按已提交的切分；normalizer 规则哈希随之变化但版本仍记 1.0.0（尚无正式数据） | 确认或要求改法 | ✅ 已确认（2026-09-25，Claude 依 Raphael 授权） |
| D-F1n | Claude 自行做的次要决定（F1）：① F1 先于 E3 实施（质量报告里的"竞争记录"要用 F1 的已验证图）；② 基础设施层读取接口增加"按快照读"（核心契约不变）；③ 数据集规格里没绑定的表按空表处理（只会让结果变成"不存在 / 冲突 / 拒绝"，不会选错）；④ 新规则标识 `hlens.pit.maximal-head@1.0.0`（连同质量规则与 resample 规则已并入 `03-data.md` §7.3）；⑦（E4）高周期 K 线只作为纯函数派生、不新建表，只接受能整除一天的分钟周期，缺分钟的 K 线标为不完整而不补齐；⑥（E3）质量规则集 `hlens.quality.canonical-partition@1.0.0` 只含事实性检查，不设任何数值阈值——"异常值"需要校准阈值，留待后续规则版本；⑤（F1-R1）ADR-0027 §13 要求"用 REST 数据须绑定证据表"，但从未写入过边的证据表没有快照、根本无法绑定——选择器因此把"未绑定"读作"没有边"（只会让结果变成冲突、不会选错，旧规格可逐位重现），并在结果里记录是否绑定；§13 的绑定要求改由数据集构建（F3）在"证据表有快照时必须绑定"处落实；⑧（G3-S2）时点选择的窗口不再限定为整天的 UTC 零点，可为任意 UTC 区间（证据边仍按整天核对），并且只证明被读到的那些规范层批次（单元级事实仍全部核对），以便成交数据按小时分段构建而不必整天驻留内存；⑨（G3-S3-R1 / R2）同一观察键中按"相邻不超过一天"连成链的全部 revision 一起参与选择（传递闭包，读取范围自动扩大到链的两端），并且只由链上最早那条所在的窗口选中（相邻时段不会重复选同一笔成交）；质量报告则按"触及"读取后去重；报告 ID 纳入 PIT 规则与相关政策的哈希，规则一变即是新报告 | 事后确认（⑤ 涉及对已接受 ADR 的解释，请重点确认） | ✅ 已确认（2026-09-25，Claude 依 Raphael 授权） |
| D-QGAP | 质量报告把"证据缺口"逐条写进一行报告：1 分钟 K 线每天 1440 条没问题，但成交一天 100～300 万条都有缺口（历史公开时刻无证据，D-HIST），一行报告会达到数 GB，本机内存放不下 | **A（推荐）**：缺口改写进一张独立的只追加表（每条 revision 一行、按时段分批写入），报告行只存引用与计数——语义不变、内存有界，需新增表的 ADR；B：同一原始单元的缺口合并为一条（带条数与范围）——改变"逐条绑定"的含义；C：成交报告按小时出——改变报告粒度与规则版本 | ✅ 已决定（2026-09-25，Claude 依 Raphael 授权）：方案 A——证据缺口改写进独立只追加表（ADR-0031），报告行只存引用与计数 |
| D-HIST | 官方资料不能证明任何历史行情 revision 的公开时刻，按 ADR-0023 一律取 `available_time = ingest_time`：早于本机采集的历史**无法用于任何历史回测** | **需要 Raphael 本人决定**：已发布核心契约 2.0.0 明文规定"早于 ingest 必须有证据、不得仅凭 event_time 回填"，这是研究宪法 C-L1（防泄漏）的前提，属红线。推荐：A. PIT 假设叠加层——存储数据不改，数据集规格可显式绑定"原始归档按事件时间 + 5 秒可用"的假设政策（进 manifest、缺口照列），不绑定则维持保守；B. 维持现状（只能做采集之后的前向研究） | ✅ 已决定（2026-09-25，**Raphael 批准推荐方案 A**）：ADR-0032 Accepted，PIT 假设叠加层已实施（数据集须显式绑定，默认仍保守） |
| D-P05 | 何时开启 Phase 0.5（公共知识库） | 已决定（2026-09-25，Claude 依 Raphael 授权）：Phase 1 关闭后再开，不并行 | 已被后续授权取代：Phase 0.5 的写入路径已按 ADR-0058 实施（B15）；tag / 资产检索：ADR-0055 由 Codex 依 Raphael 授权决定方向并于 2026-09-26 接受（Accepted，基于组合代码 `c08c589` 与最终门禁），已实施（B55）；Phase 0.5 仍未验收 |
| D-MAN | 数据集清单（冻结契约）逐条列出来源链与证据缺口：K 线数据集每天 1440 条没问题，成交整天数据集会有数百万条，一行清单放不下 | 已决定（2026-09-25，Claude 依授权）：Phase 1 成交数据集按小时构建；改契约（升 major）留到 Phase 2 按需另立 ADR | 已知限制，Phase 2 再议 |

**全阶段框架批次提出的待决定（红线，需要 Raphael；详见 [调试待办](docs/reviews/2026-09-25-framework-debug-backlog.md) B 节）**

| ID | 问题 | 推荐 | 不决定时 |
|---|---|---|---|
| D-FLOAT | 验证结果与 Profile 阈值等核心模型在哈希里用浮点数，跨平台可能不一致；改成精确小数属于修改冻结契约 | 已起草 [ADR-0052](docs/adr/0052-validation-contract-completion.md)（Proposed）：推荐在 major 2 内加精确小数字段、弃用浮点字段（旧哈希不变） | ✅ 已决定（2026-09-26，Raphael 同意推荐方案）→ ADR-0052 Accepted；按 Codex K3 以 2.1.0 实施，旧 2.0.0 数据须原样可读可重放——已随 B1～B67 合入 `main`，CODE_COMPLETE / DEBUG_PENDING；Phase 4 尚未验收 |
| D-PFIELDS | 验证 Profile 缺容量、跨资产一致性、开封预算等字段；改 Profile 结构属于红线 | 已起草 [ADR-0052](docs/adr/0052-validation-contract-completion.md)（Proposed）：只加字段，数值仍待校准后冻结 | ✅ 已决定（2026-09-26，Raphael 同意推荐方案）→ ADR-0052 Accepted；已实施（B41），CODE_COMPLETE / DEBUG_PENDING |
| D-CTRL | 校准发现：同一个显著性阈值被两处反向使用（策略检验要求足够显著，负对照要求不显著），调一个就动另一个 | 已起草 [ADR-0052](docs/adr/0052-validation-contract-completion.md)（Proposed）：给负对照单独字段 | ✅ 已决定（2026-09-26，Raphael 同意推荐方案）→ ADR-0052 Accepted；已实施（B41），CODE_COMPLETE / DEBUG_PENDING |
| D-MINEFF | 状态 × 策略的条件假设要求填"最小效应"：它算研究者预先声明的假设内容，还是验证门槛？ | 算假设内容，不作门槛 | ✅ 已决定（2026-09-26，Claude 依 Raphael 授权）：假设内容，不作门槛；生产路径无默认值 |
| D-VFAIL | 生命周期状态机只允许 CANDIDATE → FAILED，没有 VALIDATION → FAILED：验证阶段若出现技术故障（不可复现、运行出错），失败记录会进 Failure Registry，但对象的生命周期状态无法标为 FAILED | 已起草 [ADR-0053](docs/adr/0053-validation-failed-transition.md)（Proposed）：增加 VALIDATION → FAILED，只用于不可复现 / 对象自身运行出错，需证据 | ✅ 已决定（2026-09-26，Raphael 同意推荐方案）→ ADR-0053 Accepted；已实施（B23），CODE_COMPLETE / DEBUG_PENDING |
| D-NET | 本机仓库里没有真实行情数据（只有表结构），真实数据端到端测试只能用"真实格式的小样本"。要跑真正的真实数据，需要运行采集器从币安公共归档下载（公开数据、无密钥） | 授权下载 BTCUSDT / ETHUSDT 各 1～3 天的公共归档（约数万行），只写入本机、不提交仓库 | ✅ 已决定（2026-09-26，Raphael "同意"推荐方案）：下载 BTCUSDT / ETHUSDT 各 1～3 天官方公共归档（无密钥），只写本机、不入仓库；**已执行**（2026-09-26，[能力检查报告](docs/reviews/2026-09-26-dnet-real-data-capability.md)）：K 线 2 天 × 2 标的走通采集→入库→规范化→质量报告→时点选择；建数据集停在标的池（需 `exchangeInfo`，且历史日期按 ADR-0029 仍不可构建），待 Raphael 决定 |
| D-PARTIAL | 回测器的"部分成交"：冻结的回测契约要求每个目标仓位在它自己的那根 bar 上一次成交完，所以按成交量上限没成交完的部分只能取消并报告，不能顺延到后面的 bar | 已起草 [ADR-0054](docs/adr/0054-partial-fill-carry-over.md)（Proposed）：扩展契约（新执行模型、剩余量字段、可选成交量）；在那之前策略每根 bar 重发目标即可逐步到位 | ✅ 已决定（2026-09-26，Raphael 同意）→ ADR-0054 Accepted；已实施（B23）并于 B41 以 2.1.0 重新声明，CODE_COMPLETE / DEBUG_PENDING |
| D-LIST | 真实历史标的池需显式区分交易所事实与研究假设 | ADR-0051 采用方案 A：对 BTCUSDT / ETHUSDT 显式绑定 observation backfill assumption；假设不能证明真实上市史、排除幸存者偏差或用于横截面结论。政策表仍为空，待核实最早 1m 归档日；一次无密钥 `exchangeInfo` 公共调用已获该 ADR 授权，但尚未执行 | ✅ ADR-0051 于 2026-09-28 接受；实现已进入第二期，网络采集与政策下界核实待执行 |
| D-DEG-IE | 劣化检查全部指标缺失（证据不足）时，是否在新事件主题上发布？新主题会扩展已接受的 ADR-0049 的事件面 | 维持不发布：结果与报告已标明「证据不足」，绝不显示为健康 | ✅ 已决定（2026-09-26，Codex）：在独立主题 `research_loop.degradation.insufficient_evidence` 发布，ADR-0049 相应修订；已实施（B53，`4e6c220`），CODE_COMPLETE / DEBUG_PENDING |
| D-DEP | 持续循环的通用机制放在 `apps/worker`，研究阶段放在 `research/loop`，因此 research 依赖 apps/worker（apps 不依赖 research，边界测试不变）——Claude 依授权已接受（ADR-0049），请确认 | 维持 | ✅ 已决定（2026-09-26，Claude 依 Raphael 授权）：维持 ADR-0049 |

本轮已按 Raphael 对模块开发与技术决策的授权接受 ADR-0067（P11 显式劣化检查 evidence）、ADR-0068（P7 typed-plan 闭世界机制，不启用算子）、ADR-0069（P12 combine fail closed）、ADR-0070（P7 部分执行失败后 fail stop）与 ADR-0073（P7 PREPARE / 单事件 TrialLedger / COMMIT admission 恢复设计）。ADR-0070 防止同一 audit 自动继续或重复执行，但不提供 outcome 自动恢复或人工修复工具；ADR-0073 实现只覆盖 durable admission plumbing，不启用 operator。P7 六类 DSL 的具体语义与 Provider lowering 仍保持 fail closed。

状态登记对账：D-04 已由 [ADR-0072](docs/adr/0072-validation-phase-sequencing.md) 决定；D-29 已由 [ADR-0049](docs/adr/0049-continuous-research-loop.md) 决定 worker / research 依赖方向；D-30 已由 [ADR-0041](docs/adr/0041-validation-robustness.md) 的 G0 / G1 绑定与 horizon 门解决，Phase 4 仍待统一验收。

Phase 3 Event 表操作入口的边界已由 [ADR-0066](docs/adr/0066-explicit-event-table-operator.md) 决定：单独显式命令、默认无副作用、不接入 Phase 1 或自动 provisioning；真实 catalog 建表仍需单独授权。

**C3 发现的决定（已决定，已实施）**

| ID | 问题 | 结论 | 实施状态 |
|---|---|---|---|
| D-32 | 锁定的 PyIceberg 0.12 写入按天分区（`day(...)`）的表必须有可选扩展 `pyiceberg-core`，它不在原锁定依赖里；四张冻结表（aggTrades / klines / trades / bars_1m）能建表但写不进数据 | Codex 选方案 A → ADR-0026，Accepted：在同一个 PyIceberg 依赖上加官方 extra `pyiceberg-core`；表名、分区、写入路径不变；不改分区（B）、不改用 `add_files`（C） | 已实施并验收：Cursor 锁定依赖（`e40c285`）；Claude C3-R1 转正 5 个 xfail；Codex 真实写入与重启重放探针通过 |

**Phase 1 入口决定（均已决定，部分实施，逐项见 ADR 索引）**

| ID | 问题 | 结论 |
|---|---|---|
| D-01 / D-02 / D-10 | Catalog、无 Docker 时的存储、NATS 时机 | ADR-0021，Accepted |
| D-08 | 市场与执行范围 | ADR-0022，Accepted |
| D-28 | 迟到 / 修订数据的 point-in-time 语义 | ADR-0023，Accepted |
| D-31 | 历史可交易标的池 | ADR-0024，Accepted |

**Phase 0 修复裁决（均已决定、已实施、已复验）**

| 裁决 | 结论 | ADR / 状态 |
|---|---|---|
| D-26 | 三类语义身份（Profile 选择键、`Ref` 目标、`GitCodeRevision` 代码修订）排除信封版本；全局相等与内容哈希不变 | [0018](docs/adr/0018-contract-value-semantic-identities.md)，Accepted，已实施（C2c），C3 复验通过 |
| D-27 | 每条生命周期转移至少一项非空证据；不做自报职责分离 | [0019](docs/adr/0019-lifecycle-evidence-minimum.md)，Accepted，已实施（C2d），C3 复验通过 |

**不阻塞当前阶段（NOT BLOCKING）：** D-09 数值 TBD-1 ~ TBD-5（Phase 4 校准后冻结）·
H-3 ~ H-7 · ADR-0005 / 0006 的 Q-1 ~ Q-7 · Git 提交身份的长期做法

## 7. 当前风险

- ⚠️ 宪法 1.0.0 定义研究原则；验证与稳健性代码已实现，但各 Phase 尚未验收，Profile 数值仍 TBD，不能把框架实现等同于研究结论可判定
- ⚠️ 契约 2.0.0、2.1.0（ADR-0052）与 2.2.0（ADR-0055）均已在 `main`；按记录版本重放旧数据，当前版本新建对象的信封与哈希随 minor 变化（预期），以后破坏性变化必须升 major
- ⚠️ 契约层只校验结构与声明：传递依赖闭包、Registry 存在性、哈希与真实内容一致、物化数据泄漏检测、Profile 已 frozen 等仍是未实现的 Runner / Registry / Control Plane 义务，不得宣称泄漏已被防住
- ⚠️ P7 的 `ContentVerifiedLLM` 可选启用：启用时会核验内容和哈希；未包装 provider 的调用可能仅含不可取回引用，不能视为完整审计
- ⚠️ P8 新增 `retro_audit` 报告种类尚未进入原有十种报告的 fixture 生成器、页面组件测试和 live-smoke 注册；这些验收辅助清单需在下一轮验证时同步，不能声称已经验证该页面
- ⚠️ 生命周期证据只保证非空：证据真实性、批准人权限与职责分离属未来授权服务
- ⚠️ Lifecycle 可记录结构有效的 `ExecutionModeChange(to_mode=LIVE)`，但这不会切换运行时；当前 `ExecutionService` 构造时拒绝 LIVE 且只接收精确类型 `SimulatedVenue`，LIVE ladder 请求 fail closed，未发现应用 wiring、交易所下单接口或凭据。Control Plane 的真实授权人权限校验仍未实现，不能把 LIVE 证据 DTO 当作实盘授权能力或生产实盘支持
- ⚠️ JSON Schema 在几处弱于运行时（首尾空白、时长符号、跨字段约束）：权威校验必须经过运行时模型
- ⚠️ 外部是否存在 v1 历史数据证据不足，不宣称迁移已在真实数据上验证
- ⚠️ PR 工作流已使用但 GitHub Actions CI 尚未配置；本地 warehouse 数据无异地副本（Git 远程只托管代码与文档）
- ⚠️ Docker 未安装、外部数据盘未挂载、WSL 内存约 15 GiB：影响 Phase 1 起的数据工作
- ⚠️ PyIceberg 与 Binance 的关键能力事实已由 Codex 于 2026-09-24 按官方资料复核，PostgreSQL 服务已只读确认在线；实施前仍须按锁定依赖版本做行为 smoke / integration 验证
- ⚠️ 来源若不提供修订关系或修订时间，同一观察的不同版本会成为 competing heads 并使数据集构建 fail closed；需要各来源的 precedence policy 与证据
- ⚠️ **D2 的证据结论**：Binance 官方资料没有给出任何具体 revision 的公开时刻，因此 `binance.spot.publication@1.0.0` 三类主体全部保守取 `available_time = ingest_time` 并写证据缺口；在出现可引用的官方上界并发布新 policy 版本之前，早于本机 ingest 的历史可用区间为空。归档替换同样无法证明先后，一律 competing heads（数据全部保留，但任何“最新”结论 fail closed）
- ⚠️ REST 补尾的四张新表、身份与跨通道纯 policy、严格 decoder、可重放 collector，以及 D3E store / reconciler（含 R1 / R2 / R3）均已由 Codex 验收；REST 数据可进入后续数据集流程，但仍须满足数据集自身的有效性检查。验收记录见 [D3E 验收](docs/reviews/2026-09-27-d3e-acceptance.md)
- ⚠️ D3E 的持久行核验会重新读取并严格重新解码 REST 首次交付页，对归档对象重新运行 D1 严格解析；reconciler 按显式 `snapshot_id` 重读证据边批次。D3E-R3 曾发现跨 UTC 日边界的合法证据边被误报；修复 `69f0bf0` 按写入日期的完整键集重读并逐项复核，未放宽完整性检查。13 项跨午夜回归及 Codex 独立验收结果见上述记录。Phase 1 仍未整体验收，后续批次与 E1-CAP-1 容量门仍待处理。
- ⚠️ **容量**：2026-09-25 探针显示规范化按"整个单元一次性读入"约每行 19 KB（BTC 一整天 100～300 万行会超出 WSL 约 15 GB 内存）。G3-S 已改为固定快照 + 分批窗口：30 万行规范化新增常驻约 0.8 GB、每行边际约 0.7 KB（外推一整天约 2～3 GB）。G3-S2 让时点选择只证明读到的批次、并允许任意 UTC 时段：6 万行实测，选 1 小时峰值约 0.27 GB；但选择结果本身每行约 11 KB，**成交数据必须按小时（或更短）分段选择**，整天选择（约 30 GB）不可行。质量报告已按小时分段证明（G3-S3），但报告行内逐条列出的证据缺口在成交整天规模下仍放不下（D-QGAP）；在决定之前不得对成交数据做整天规模的报告，数据集构建须按小时分段
- ⚠️ **G2 红队发现（2026-09-25，92 个跨阶段攻击中 6 个成功，已以严格 xfail 固定并逐个返修中）**：RT-1 规范化崩溃后读取方把已提交前缀当完整单元（高）；RT-3 REST 页中途崩溃时规范化接受缺元素的页（高）；RT-2 替换归档尚未规范化时数据集仍选旧版（中）；RT-4 伪造清单（删排除项）可被保存 / 读取（中）；RT-5 首条证据边出现后旧清单无法重建（中）；RT-6 特征运行信任未验证的清单哈希（中）。修复前不得用这些路径产出正式数据集
- ⚠️ **键闭包的已知边界**：同一笔成交的副本之间若有超过一天的空档（链断开），两段各自被当作独立记录（冲突看不到）；相邻两天的质量报告会各自列出跨天冲突（按设计）；这种数据只能是严重损坏，需以后的质量规则专门检测；另外按小时选择时现在要读前后各一天的分区，生产规模下的耗时尚未测量
- ⚠️ R2 的性能代价：每次核对都会遍历相关表的提交历史来找批次（与 R1 对响应表的做法相同），并按批次重读行；表历史很长时会变慢，需在 G3 容量基线中测量
- ⚠️ D3E 的保守边界：同一页、同一字节若在另一次采集里先被拒绝、后被接受（上下文不同），store 拒绝写入这些元素（否则要借用拒绝时打下的知识时间）；比对读取以"读前读后表头一致"固定 snapshot；D3E-R3 起对证据边批次改用显式 `snapshot_id` 时间旅行重读（`_rows_at`），其余表仍靠头部一致性固定
- ⚠️ D3D 的 market-data base 与 D0-R2 一致：把空路径与单独 `/` 视为 origin 根（`AnyHttpUrl` 默认值即如此渲染），其它任何路径 / query / fragment / 凭据一律拒绝；`Content-Length` 只在响应无 content coding 时与实体长度比对（有 coding 时它计的是编码后字节），解压后的大小上限始终生效
- ⚠️ **三个实现陷阱**（ADR-0027 §11）：身份规则哈希是全局的（REST 必须用独立规则）、同一 `policy_id` 两个版本不能共存于一份 PIT spec、归档与 REST 合入同一观察时序号必须不碰撞（按区间划分，D3E 在单个观察图内检查）；归档身份与分配代码均不改
- ⚠️ D-33 采用精确比较：REST 以毫秒交付、2025 年起归档为微秒，同一笔成交若带亚毫秒位就无法证明相等，只能 fail closed（正确但降低 REST 补尾的价值）；是否改请求微秒需以后单独验证并批准
- ⚠️ REST 的官方事实中，**未被证明**的部分已逐条列出（证据文件 §2）：无任何响应的公开时刻、aggTrade ID 不保证连续、REST 与归档内容不保证一致、未结束 K 线无法从载荷判别。这些都只能 fail closed，不得当作已解决
- ⚠️ 旧研究可能已看过全部 BTC 历史：历史"样本外"区间在认知上不完全干净
- ⚠️ D1 已真实验证两个单位边界日的 kline 与 aggTrades；大体量 BTC 日归档尚未做内存 / 吞吐基线，批量 backfill 前必须先完成容量检查与可恢复 checkpoint

- ⚠️ **后代 G5 是未决设计边界**：进化后代沿用父代 family，每个 family 只能评估一次密封样本外（宪法 C-S1..3），所以后代报告没有 G5、替换提案无法用循环自己的报告支撑；为后代重复使用同一密封窗口会泄漏 holdout。需要新的预注册 family 或独立的未来密封窗口，以及相应的 Profile 证据规则，均未决定（完成计划 B57）

## 8. 当前禁止事项

- ✅ Raphael 已授权先完成计划内的 Phase 0.5、Phase 2～14 框架 / 模块代码与 Phase 1 已批准范围，再统一验收；这不代表开启其他 Phase 的运行或验收。Phase 1 数据采集仍按 roadmap 顺序；D3E 已接受、D4 已关闭，E1-CAP-1 仍阻断，E2 后续数据链路不冒进；不写 WebSocket 代码
- ❌ Phase 0.5 只做已接受的范围（ADR-0034 检索、ADR-0058 写入、ADR-0055 标签 / 资产检索）；不得替人工审阅者给种子写入标签 / 资产，不得把分类提案当作已审阅数据
- ❌ 研究代码不晋升为生产代码（`strategies/` / `risk/` 仍无代码；Promotion 链今天拒绝所有策略）
- ❌ 不安装系统软件（包括 Docker）；D2 只可使用已授权的专用 Phase 1 catalog / test database 与本地 warehouse，不得访问账户 / 交易接口，不得创建或修改数据库 / role
- ❌ 不修改系统配置、`.wslconfig`、Git 全局配置
- ❌ 不修改、移动或删除旧项目（`/mnt/e/alpha-autoquant`、`/mnt/e/hlens-cryptoplus`）与旧数据
- ❌ 不选择 D-09 的五类数值（Phase 4 校准后才冻结）
- ❌ 不在宪法中写入任何数值阈值；不修改宪法原则（须按第九章另起 ADR 并由 Raphael 批准具体变化）
- ❌ 不在未升 major、未走 ADR 的情况下改变 Domain Contract
- ❌ 不因为回测结果修改研究规则
- ❌ Claude 不替 Raphael 做架构决策；不涉及实盘、资金或风险预算

## 9. 最近一次变化

| 日期 | 变化 | 影响 |
|---|---|---|
| 2026-09-29 | W1 PostgreSQL-enabled 全量收尾与两轮延期 | 全量 `7 failed / 8,814 passed / 1 skipped / 72 deselected / 5 warnings`（8,822 项执行）；focused database retry `3 failed / 40 passed`。catalog frozen-layout 与 revision table-binding 节点达到两轮失败并延期；catalog metadata-only 节点轮次无法核实，后续因测试 URI 未配置而 skip。console/evidence targeted `52 passed, 1 warning`。延期项不重跑，继续独立工作；E1-CAP-1 仍阻断 Phase 1。 |
| 2026-09-29 | W5 Knowledge Base 独立验收切片 | 检索、审阅路径 `103 passed`；精确 `schema_version == 2.1.0` 守卫随隔离集成候选回归通过。具名人工 tags/assets 审阅及 4 条后增 seed golden 仍未完成；单独排期，不阻塞其他工作。 |
| 2026-09-29 | W2 本机 API、Worker 与存储恢复 | Uvicorn loopback、allowlist JSON 日志、敏感 query 不落日志、SIGTERM/SIGINT 优雅停止：API runtime `25 passed`；Worker 跨进程恢复 `30 passed, 0 skipped`。Registry/Admission/Catalog/State/Event 切片 `181 passed`，SQLite Catalog/warehouse 重开 `1 passed`。State reopen `28 passed`、Event Iceberg 文件排除 registry 未分类节点后 `29 passed, 1 deselected`；均只验证同进程 SQLite adapter reconnect。Worker 无独立进程入口；PostgreSQL + Iceberg 联合恢复仍待验。 |
| 2026-09-29 | W6/W7 独立验收 | State `44 passed`；Event/Outcome `203 passed, 1 failed`（registry count 15-vs-17，历史轮次未知，未重跑）。P7 一处文案断言修复后精确节点通过；P8 selected slice `148 passed`。无 Phase 验收，Profile 未冻结，Provider/Runner 仍关闭。 |
| 2026-09-29 | W10 Web 独立验收切片 | Web production build / components `120 passed`；library `110 passed, 1 failed`，retro-audit fixture 节点两轮失败后延期。依赖修复候选 audit 为 0，集成分支 clean install/build 通过；fixture-backed SSR test 由页面实际使用的五个图表 option builder 渲染五张 SVG（`1/1`），独立 review 无 P1/P2，页面接线已验证；浏览器 Canvas / resize / 视觉与交互仍待验。 |

## 10. 下一阶段进入条件

**Phase 0 关闭条件（roadmap 验收标准）：全部满足**
1. ✅ Constitution 为 Approved（1.0.0）且不含任何数值阈值
2. ✅ Validation Profile 与 Experiment Metadata 的契约已定义
3. ✅ 所有核心实体有契约与 Schema 导出（Phase 0 收口时 current 38 份，现为 135 份（组合分支契约 2.2.0），逐字节一致；legacy v1 35 份不变）
4. ✅ 状态机只允许定义的转移（测试覆盖；主体、时间、证据约束生效）
5. ✅ 契约层无基础设施依赖（导入检查测试）
6. ✅ 本地测试命令可运行（1433 项通过；ruff + mypy strict 全绿）；CI 尚未配置

**Phase 1 开启与实现前提：**
1. ✅ Phase 0 完成
2. ✅ Phase 1 已由 Codex 依 Raphael 持续授权明确开启
3. ✅ ADR-0021 ~ 0024 已 Accepted，决定 D-01、D-02、D-08、D-10、D-28、D-31；首切片已冻结（A2）
4. ✅ 首次消费的 Provider 先交付 Protocol + DTO + contract tests（ADR-0017；B1～B3 已满足），再开始实现（C 起）

**Phase 1 关闭条件**：roadmap Phase 1 验收矩阵 #1 ~ #21 全部满足（当前 #1 ~ #12 与 #17 满足；其余按批次门推进）。

## 11. 给 Raphael 的下一步

> 我现在应该干什么？

1. 当前分支与 worktree 数量见 §1：`main` 仍是长期分支，W1 协调分支和 5 个隔离任务 worktree 正在收口；本轮没有推送或合入 `main`。Phase 1 仍受 E1-CAP-1 阻断，未验收。模块计划见 [2026-09-28 模块基础逻辑计划](docs/plans/2026-09-28-module-foundation-completion.md)。
2. 全代码批次有已记录的门禁结果，但代码整合不等于 Phase 验收；没有任何策略被验证或晋升，Profile 数值未冻结，系统没有下单能力。
3. ADR-0073 的 v4 durable admission coordinator 与进程内 admission lease / 写入 gate 已进入本地 main，ADR-0074 operator-only v5 identity 及有限批次 operator 已合入 `main@0d4862a`。整合保留 v3/v4 字节与行为，不启用六类算子；没有冻结 Profile 前不得提供可运行配置。独立模块测试继续推进；Phase 1 全量验收仍未完成，E1 容量门未关闭，未打 tag。
4. 已提交改动的合并不等于全量验证。W1 PostgreSQL-enabled 全量已结束；14 个兼容性 / 固定哈希失败、loop-round malformed、catalog frozen-layout 与 revision table-binding 节点按两轮规则延期，不计通过且仍属发布门。catalog metadata-only 节点的全量失败轮次待核实；后续无数据库 catalog 的精确尝试为 skip。按模块继续推进独立工作，再回到待分类失败门；随后完成 E1-CAP-1 容量测量与 ADR-0051 下界核实。详见 [W1 验收与延期项](docs/reviews/2026-09-29-w1-validation-and-deferrals.md)。

## 12. 给 Claude Code 的下一步

> Claude 下一步可以执行什么？

1. E1-R 基础切片与 E1-CAP-1 的容量验收状态分开记录；32 MiB 完整工作集门槛不变。归档中的设计提案仍不构成 Accepted ADR，禁止把任何子问题改进表述为 E1-CAP-1 通过。
2. P11 显式 degradation CLI 已完成；ADR-0073 durable v4 recovery、v5 operator identity 与 ADR-0074 有限批次 operator 已整合到 main，均尚待统一验收。继续保持 synthetic-only / 外部调度 / 无 API 写触发，并在 Profile 冻结前拒绝运行配置。
3. P7 六类算子的纯 lowering 已实现（ADR-0078 / 0082 / 0088；rank / quantile 仍 OPEN），`TypedPlan.runnable` 仍为 False——启用运行须在调试通过后另行决定。
4. ADR-0089 已增加 `expected_spec` 读取身份校验；`tests/infrastructure/state` 27 项通过。最新 PostgreSQL-enabled 全仓结果为 8,814 passed / 7 failed / 1 skipped / 72 deselected；达到两轮的节点延期且不计通过。catalog metadata-only 节点的失败轮次待核实，后续无 PostgreSQL test catalog 的尝试为 skip；其余全量失败尚待恢复 node IDs 分类。具体见 W1 review。不得实盘、使用交易凭据或下单；不猜 Profile 数值；不把代码整合称为 Phase 验收；不 force push。

### 10.29 协调分支快进到本地 main（2026-09-27）

- Raphael 明确授权先整合合适分支、清理冗余分支并持续补齐模块；协调分支相对本地 `main@669704c` 为严格快进后代（0 个 main 独有提交、26 个协调分支提交）。在主线工作区干净的前提下，以 `--ff-only` 将其快进到 `main@498250a`，未解决冲突、未改写历史、未推送远端。
- 整合范围为 40 个文件、+3173 / -215 行，包括 P7 non-runnable typed-plan parser、P11 degradation operator / CLI / provenance 页面、P12 `combine` 冲突拒绝、E1 history / normalizer / proof-scan 候选及其 ADR / 规格 / 复核记录。各模块状态仍是待调试 / 待验收；这次合并不构成任何 Phase 验收。
- 主线现比 `origin/main` 超前 44 个提交。没有运行测试或 build；沿用已记录的定向静态检查结果。E1-CAP-1 仍未通过 32 MiB 门，Phase 1 仍未验收。分支与 worktree 清理继续逐项审计，活动或含唯一内容的工作区保留。

# HLENS-AutoResearch 项目驾驶舱

> 面向项目所有者 Raphael。技术细节见 `docs/`，AI 工作规范见 `CLAUDE.md`，Claude 的长期记忆见 `PROJECT_MEMORY.md`。
> 本文件在**每次阶段变化、每个阶段完成、每次出现 ARCHITECTURE_DECISION_REQUIRED** 时必须更新。

## 1. 当前状态

| 项 | 值 |
|---|---|
| 项目版本 | 0.0.0 |
| 当前 Phase | **Phase 1 — Market Representation：🔄 已开启**（2026-09-24，分支 `phase/1`） |
| 当前子阶段 | **框架整合与逐模块打磨**。已有代码已快进整合到本地 `main`；Phase 1 的 D3E 已接受、D4 已关闭，E1-CAP-1 是当前阻断；Phase 0.5、Phase 2～14 仍须分别验证与验收 |
| 上一 Phase | Phase 0 — Research Constitution：✅ 已完成（tag `phase-0-complete`，last known good） |
| 总体状态 | 🔄 框架代码已整合并推送至 `main`（本地与远端同步）；Phase 1 被 E1-CAP-1 阻断，其余 Phase 未验收；Profile 数值未冻结；无任何实盘能力 |
| 全阶段代码完成批次 | 分支 `claude/2026-09-26-code-completion-337e38` → WIP `wip/all-code-completion`：Phase 0.5、2～14 与前后端的剩余代码缺口已补（B1～B67；B62 是 P10 证据模式决定）；已整合并推送到 `main`，状态 **CODE_COMPLETE / DEBUG_PENDING**，未独立调试、**不等于 Phase 已验收**；逐批证据见 [完成计划 §10](docs/plans/2026-09-26-all-code-completion-plan.md) |
| 最后更新时间 | 2026-09-27 |

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
Iceberg Catalog、本地 StorageAdapter、八张生产表定义、D0 Collector、D1 fail-closed parser 与 D2 append-only revision store 已验收。D2 首轮复核发现来源 checksum 真实性与 arrival anchor 全表物化两个缺陷；D2-R1 修复后由 Codex 复现旧提交四项失败、运行真实 PostgreSQL 全量与静态检查并接受。D3A 是 REST Raw / lineage 的 docs-only 架构门：Codex 复核草案后退回八项缺陷，D3A-R1 修正后由 Codex 独立复核接受 ADR-0027。D3B 首轮复核发现极端十进制异常泄漏与伪造 / 过期比较可生成边；D3B-R1 修复后由 Codex 接受。D3C 首轮复核发现 RFC JSON 框架空白被误拒；D3C-R1 修复后由 Codex 以 `python -O` 对抗探针、真实 PostgreSQL 3434 项全量与静态检查接受。D3D 首轮复核发现完整 HTTP client 注入可在 allowlist 后加入凭据并改写到外域账户路径；D3D-R1 删除该入口并关闭环境代理与 Cookie 回放，Codex 独立复现修复、运行真实 PostgreSQL 3587 项全量后接受，D3E 开放。D3E 已把已提交的 REST 采集写成 Raw 响应 / 元素 revision，并实现跨通道比对与证据边；Codex 首轮复核发现 store 会原样采用来源伪造的元素行、把时间 / 政策漂移的竞争响应当成普通竞争，D3E-R1 返修后，R2 让 reconciler 比对前用共享核对器证明两侧每一行、R3 进一步把持久行绑定到不可变来源（REST 元素/响应行重新读取并严格重新解码其首次交付页，归档行改用 D1 严格重新解析已发布的归档对象），均等待 Codex 复核。

代码仓库已有私有 GitHub 远程 `raphael2025/hlens-autoresearch`（ADR-0025）：执行者只提交，Codex 复核通过后推送每个进度；PR 与 CI 尚未配置。
本次进度推送后，远程 `phase/1` 含 A3a / A3b / B1～C3、D-32、D0～D2、D3A～D3D 及各返修 / 验收门；D3D 修复后的实现恢复点为 `c06b9fa`。

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
| 0.5 | Public Knowledge Base | 🧱 框架已实现（ADR-0034）；经审阅写入路径（ADR-0058）与 `verify` 修复（K1）CODE_COMPLETE / DEBUG_PENDING；按标签 / 资产检索（ADR-0055 **Accepted** 2026-09-26，契约 2.2.0，含控制台）CODE_COMPLETE / DEBUG_PENDING；仓库种子**尚无经具名人工审阅的标签 / 资产**（分类提案未写入）；**Phase 0.5 未验收** |
| 1 | Market Representation | 🔄 D3E 已独立验收、D4 已关闭；E1-CAP-1 容量边界为阻断项，结构修复在 `fix/e1-cap1`（`a75278e`），但 10k/100k/500k 实测、内存记录和返修后定向测试仍缺，尚未验收；后续 Phase 1 验收项也未全部完成 |
| 2 | Market State Engine | 🧱 框架已实现（ADR-0035）；诊断可序列化 / 带哈希 / 报告页 CODE_COMPLETE / DEBUG_PENDING |
| 3 | Event & Interaction Engine | 🧱 框架已实现（ADR-0036）；事件运行存储、统计序列化、物理表 `event.events`（ADR-0056；只读核实从未建表）CODE_COMPLETE / DEBUG_PENDING；`subject`（ADR-0057）已以 2.1.0 声明（B41），交互 DSL（ADR-0061）CODE_COMPLETE / DEBUG_PENDING |
| 4 | Outcome Engine + 最小验证门 | 🧱 框架已实现（ADR-0037）；Outcome 表持久化、可选多种子负对照 CODE_COMPLETE / DEBUG_PENDING；ADR-0052 契约 2.1.0 与研究侧取值（精确比较、C-A4）已实施、待 Codex 复核；ADR-0060 市场基准；Profile 数值 TBD |
| 5 | Strategy Library + 回测 | 🧱 框架已实现（ADR-0038）；横截面动量 `xsmom_bars`（研究层）CODE_COMPLETE / DEBUG_PENDING；ADR-0054 部分成交结转已以 2.1.0 声明；ADR-0005 Promotion 链 CODE_COMPLETE / DEBUG_PENDING（今天所有策略都被拒）；Promotion 的权威冻结来源改为追加式、带目录外锚点的 Profile 冻结登记（ADR-0062 **Accepted** 2026-09-27，Codex；B56，CODE_COMPLETE / DEBUG_PENDING；登记为空、Profile 数值未冻结，不是 Phase 4 / 5 验收）；Promotion 与路由一样要求 Profile 所要求的反向对照报告项（B59）；无策略晋升 |
| 6 | State × Strategy | 🧱 框架已实现（ADR-0039）；矩阵条件假设全单元预登记、可选接入循环、逐单元验证（B27）CODE_COMPLETE / DEBUG_PENDING |
| 7 | Dynamic Discovery | 🧱 框架已实现（ADR-0040）；LLM 调用内容存储与循环内可取回核对；严格草稿、被拒调用记录、声明式批次、知识检索来源（B33）CODE_COMPLETE / DEBUG_PENDING |
| 8 | Validation & Robustness | 🧱 框架已实现（ADR-0041）；G4 逐检查异常隔离、多标的验证、横截面跨资产（ADR-0059，B29）CODE_COMPLETE / DEBUG_PENDING |
| 9 | Synthetic Market Lab | 🧱 框架已实现（ADR-0042）；检测器异常计 INCONCLUSIVE、可选实际运行 G5、多标的校准模式（B26）、配置错误不再被吞、错误时给出通过率区间（B45 / B48）CODE_COMPLETE / DEBUG_PENDING；中等规模证据报告（单标的 250、双标的 200 种子，B52）已提交，两份均已在原代码基线上逐字节复现（B54 / B57）；只给证据不选数值 |
| 10 | Dynamic Strategy Router（纸面） | 🧱 框架已实现（ADR-0043，仅纸面）；无候选明确停止、运行哈希复核、资格证据模式、纸面偏差报告、路由自身验证（B31 / B34）；证据模式要求 Profile 所要求的市场基准与反向对照报告项（B51 / B58），**不要求 Profile 冻结登记**（B62；冻结权威门只在 Promotion，ADR-0062）CODE_COMPLETE / DEBUG_PENDING |
| 11 | Continuous Research Loop | 🧱 框架已实现（ADR-0044 / 0049 / 0050）；总线外部锚点、任务只读 API、ADR-0053（VALIDATION → FAILED）、可选条件假设、跨进程测试与状态目录单写者锁、劣化检查报告（B32 / B34）CODE_COMPLETE / DEBUG_PENDING |
| 12 | Strategy Evolution | 🧱 框架已实现（ADR-0045）；替换提案（恒待人工批准）与循环之外的替换提案作业（证据逐份核验、账本单写者锁 + 外部锚点，B49）CODE_COMPLETE / DEBUG_PENDING |
| 13 | Production Adaptive System（仅模拟，无实盘） | 🧱 框架已实现（ADR-0046，实盘结构上被拒绝）；持久审计、非空审计重开即急停、只读重放、风险 / 告警重放（B31）CODE_COMPLETE / DEBUG_PENDING |
| 14 | Technology Migration | 🧱 框架已实现（ADR-0047）；金标准记录持久化、差异报告、回滚证据、金标准实验重放（B32）CODE_COMPLETE / DEBUG_PENDING（无具体迁移目标） |
| apps | api / worker / web | 🧱 框架已实现（ADR-0048，只读）；任务端点、知识检索错误码、全页加载 / 空 / 错误状态、研究循环页修复、14 个页面（含 Jobs 与 5 个新报告种类页）、`node --test` 与组件测试（B37）、API ↔ 契约 / 身份核对（B34）、真实进程 + 真实 HTTP 冒烟含 502 / 500（B44 / B47）、兜底 500 与路径清除（B46）CODE_COMPLETE / DEBUG_PENDING；未做浏览器手工验收 |

图例：🧱 = 按 Raphael 2026-09-25 指示先实现的框架代码（FRAMEWORK_IMPLEMENTED / NOT_VALIDATED），全部完成后逐个调试与验证；CODE_COMPLETE / DEBUG_PENDING = 2026-09-26 全代码批次补齐、有定向测试但未独立调试，**不是**验收；阈值 / Profile 数值一律 TBD；实盘相关一律不实现。

## 3. 已完成

- ✅ 架构蓝图：11 份架构文档、路线图；ADR-0001 ~ 0027 全部 Accepted（0026 已实施；0021 ~ 0024 部分实施，0027 待实施，逐项见 ADR 索引）
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

- 🔨 **逐个调试（2026-09-25 夜）**：全部框架代码已合并并通过全量门禁；正在按 [调试待办](docs/reviews/2026-09-25-framework-debug-backlog.md) 逐项处理——
  独立只读复核（cursor-agent）发现的问题已修复 24 项（另有真实数据冒烟发现的 3 项）（验证门的 4 个高危泄漏 / 复用漏洞、G4 的"空配置即通过"、模拟场所绕过 Kill Switch、权重被当作数量等），
  其余缺口已登记；真实数据格式的端到端冒烟已通过（本机无真实行情，用真实格式小样本走真实入库路径；见 D-NET）
- ⏸ Phase 1：实现与红队返修完成，等待 Codex / Raphael 验收（证据：`docs/reviews/2026-09-25-phase1-close-evidence.md`、`phase1-review-guide.md`）；Codex K4（PIT 边重复）为独立阻断项，不在全代码分支修改
- 🔨 **全阶段代码完成批次（2026-09-26，Claude，`wip/all-code-completion`）**：B1～B52 已补齐 Phase 0.5、2～14 与前后端的剩余代码缺口（逐批见完成计划 §10；B44～B52 为两次只读审计的后续修复，§10.7 / §10.8）；
  Raphael 2026-09-26 授权 Claude 自主决策（含红线），逐项裁决见 [自主决策记录](docs/reviews/2026-09-26-autonomous-decisions.md)；Codex 全代码复核（`docs/reviews/2026-09-26-codex-full-code-review.md`）K1 已修，
  K2 本次同步，K3（ADR-0052 以 2.1.0 实施、旧 2.0.0 原样可重放）已实施（B38 / B40 / B41），待 Codex 复核；全量非 PostgreSQL 门禁：`8983ead` 6749 passed → 审计后续最终 `3be497b` **7031 passed** / 136 deselected，退出码 0（完成计划 §10.8）；契约 2.1.0（ADR-0052 按记录版本重放，独立 Phase 1 分支 `phase1/adr-0052-versioned-replay` `22392ea`，已合入全代码分支）；ADR-0054 / 0057 已以 2.1.0 重新声明；均待 Codex 复核。
  ADR-0055（B55）：组合分支 `claude/adr-0055-integration`（基于 `a5836b2`）把契约升为 **2.2.0**（知识标签 / 资产，2.0.0 / 2.1.0 按记录版本重放），Codex 已接受独立实现进入整合（检查点 `origin/claude/adr-0055-tags-assets` = `ed8e694`）；组合分支 `1367dc8` 全量门禁 7176 passed、退出码 0；Codex 复核提出的证据测试收窄（哈希逐一绑定对象）已提交为 `c08c589`；代码冻结提交 `c08c589` 的全量非 PostgreSQL 门禁（`systemd-run --user --scope -q -p MemoryMax=5G -p MemorySwapMax=0 uv run pytest -q -m "not postgres" -p no:cacheprovider -rs`）→ `7179 passed, 136 deselected, 1 warning in 2906.24s (0:48:26)`，退出码 0；起止 SHA 均为 `c08c5895b4a60916715c2f88f65d061c40724b96`、dirty=0（2026-09-26T20:07:01Z → 20:55:29Z）；其后的 docs-only 提交**不在**该门禁覆盖范围内。ADR-0055 由 Codex 于 2026-09-26 接受（Accepted）；ADR / 代码接受**不等于** Phase 0.5 整体验收：仓库种子仍无具名人工审阅的标签 / 资产分类（只有实施说明 §5 的提案），Phase 0.5 验收仍待 Codex / Raphael

## 5. 下一步

### 我（Raphael）需要做

- 验收：按 [调试待办](docs/reviews/2026-09-25-framework-debug-backlog.md) 与 §11 查看本夜成果；Phase 1 按 `docs/reviews/2026-09-25-phase1-review-guide.md` 验收
- §6 的挂起项已由 Claude 依你 2026-09-26"所有的决策都由你来决定，包括红线"的授权逐项裁决（[自主决策记录](docs/reviews/2026-09-26-autonomous-decisions.md)），你可随时推翻；ADR-0052 按 Codex K3 以 2.1.0 实施
- 以后冻结 Validation Profile 数值（D-09 TBD-1～5）时，可参考 Phase 9 校准工具生成的证据（只是证据，不是建议值）

### Claude Code 需要做

- 按 Raphael 2026-09-27 的优先级，在框架代码已整合到本地 `main` 后，按模块完成缺口修复、定向验证和文档更新；不改冻结契约、不设阈值、不接实盘
- 分支清理只归档并删除已确认冗余且无活动 worktree 的本地分支；保留独有实现、决策记录、Phase 0.5 种子和未验收工作
- 本地 `main` 快进已获 Raphael 明确授权；推送和打 tag 状态分别记录，不能把整合描述为 Phase 验收

## 6. 当前待决策

**B65 Uvicorn 安装（待 Raphael 授权；代码已完成）**

| ID | 问题 | 方案 | 状态 |
|---|---|---|---|
| D-UVICORN | ADR-0063（Codex 已决定用 Uvicorn 作本机只读 API 运行时）需要把 `uvicorn` 作为可选 `api-server` 依赖写入 `pyproject.toml` / `uv.lock` 并装入项目 `.venv`，这属于安装软件（CLAUDE.md §0 / H12） | Codex 选 A：只用本机缓存的 uvicorn 0.53.0、可选 extra、不触网；仓库改动（依赖声明、锁条目、入口、测试、文档）已完成 | 只剩安装与真实运行待 Raphael 明确授权（H12）；两项真实 Uvicorn 测试目前跳过、未运行 |

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
| D-PUSH | 推送规则冲突：仓库 `CLAUDE.md` §10.8 规定 Claude "只提交、不 push"，而 2026-09-25 的接管指令要求每个批次推到 `wip/phase-1-unreviewed`（此前已推到 `4472282`）。按文件优先级，Claude 自 2026-09-25 新会话起暂停推送、只在本地提交 | A：维持 CLAUDE.md（只本地提交，等 Codex 推送）；B：明确授权继续推送 WIP 引用（只推 `wip/*`，不推正式分支），并据此修订 §10.8 | ✅ 已决定（2026-09-25，Claude 依 Raphael 授权）：只推送到 WIP 备份分支 `wip/phase-1-unreviewed`（快进、不强推），`phase/1` 与 `main` 仍等复核 / 批准 |
| D-E1n | ADR-0028 实施记录：规范层批次号多了"单元行数"（E1-R1）与"批大小"（E1-R3）两段，保证原始单元变化即拒绝、续跑只按已提交的切分；normalizer 规则哈希随之变化但版本仍记 1.0.0（尚无正式数据） | 确认或要求改法 | ✅ 已确认（2026-09-25，Claude 依 Raphael 授权） |
| D-F1n | Claude 自行做的次要决定（F1）：① F1 先于 E3 实施（质量报告里的"竞争记录"要用 F1 的已验证图）；② 基础设施层读取接口增加"按快照读"（核心契约不变）；③ 数据集规格里没绑定的表按空表处理（只会让结果变成"不存在 / 冲突 / 拒绝"，不会选错）；④ 新规则标识 `hlens.pit.maximal-head@1.0.0`（连同质量规则与 resample 规则已并入 `03-data.md` §7.3）；⑦（E4）高周期 K 线只作为纯函数派生、不新建表，只接受能整除一天的分钟周期，缺分钟的 K 线标为不完整而不补齐；⑥（E3）质量规则集 `hlens.quality.canonical-partition@1.0.0` 只含事实性检查，不设任何数值阈值——"异常值"需要校准阈值，留待后续规则版本；⑤（F1-R1）ADR-0027 §13 要求"用 REST 数据须绑定证据表"，但从未写入过边的证据表没有快照、根本无法绑定——选择器因此把"未绑定"读作"没有边"（只会让结果变成冲突、不会选错，旧规格可逐位重现），并在结果里记录是否绑定；§13 的绑定要求改由数据集构建（F3）在"证据表有快照时必须绑定"处落实；⑧（G3-S2）时点选择的窗口不再限定为整天的 UTC 零点，可为任意 UTC 区间（证据边仍按整天核对），并且只证明被读到的那些规范层批次（单元级事实仍全部核对），以便成交数据按小时分段构建而不必整天驻留内存；⑨（G3-S3-R1 / R2）同一观察键中按"相邻不超过一天"连成链的全部 revision 一起参与选择（传递闭包，读取范围自动扩大到链的两端），并且只由链上最早那条所在的窗口选中（相邻时段不会重复选同一笔成交）；质量报告则按"触及"读取后去重；报告 ID 纳入 PIT 规则与相关政策的哈希，规则一变即是新报告 | 事后确认（⑤ 涉及对已接受 ADR 的解释，请重点确认） | ✅ 已确认（2026-09-25，Claude 依 Raphael 授权） |
| D-QGAP | 质量报告把"证据缺口"逐条写进一行报告：1 分钟 K 线每天 1440 条没问题，但成交一天 100～300 万条都有缺口（历史公开时刻无证据，D-HIST），一行报告会达到数 GB，本机内存放不下 | **A（推荐）**：缺口改写进一张独立的只追加表（每条 revision 一行、按时段分批写入），报告行只存引用与计数——语义不变、内存有界，需新增表的 ADR；B：同一原始单元的缺口合并为一条（带条数与范围）——改变"逐条绑定"的含义；C：成交报告按小时出——改变报告粒度与规则版本 | ✅ 已决定（2026-09-25，Claude 依 Raphael 授权）：方案 A——证据缺口改写进独立只追加表（ADR-0031），报告行只存引用与计数 |
| D-HIST | 官方资料不能证明任何历史行情 revision 的公开时刻，按 ADR-0023 一律取 `available_time = ingest_time`：早于本机采集的历史**无法用于任何历史回测** | **需要 Raphael 本人决定**：已发布核心契约 2.0.0 明文规定"早于 ingest 必须有证据、不得仅凭 event_time 回填"，这是研究宪法 C-L1（防泄漏）的前提，属红线。推荐：A. PIT 假设叠加层——存储数据不改，数据集规格可显式绑定"原始归档按事件时间 + 5 秒可用"的假设政策（进 manifest、缺口照列），不绑定则维持保守；B. 维持现状（只能做采集之后的前向研究） | ✅ 已决定（2026-09-25，**Raphael 批准推荐方案 A**）：ADR-0032 Accepted，PIT 假设叠加层已实施（数据集须显式绑定，默认仍保守） |
| D-P05 | 何时开启 Phase 0.5（公共知识库） | 已决定（2026-09-25，Claude 依 Raphael 授权）：Phase 1 关闭后再开，不并行 | 已被后续授权取代：Phase 0.5 的写入路径已按 ADR-0058 实施（B15）；tag / 资产检索：ADR-0055 由 Codex 依 Raphael 授权决定方向并于 2026-09-26 接受（Accepted，基于组合代码 `c08c589` 与最终门禁），已实施（B55）；Phase 0.5 仍未验收 |
| D-MAN | 数据集清单（冻结契约）逐条列出来源链与证据缺口：K 线数据集每天 1440 条没问题，成交整天数据集会有数百万条，一行清单放不下 | 已决定（2026-09-25，Claude 依授权）：Phase 1 成交数据集按小时构建；改契约（升 major）留到 Phase 2 按需另立 ADR | 已知限制，Phase 2 再议 |

**全阶段框架批次提出的待决定（红线，需要 Raphael；详见 [调试待办](docs/reviews/2026-09-25-framework-debug-backlog.md) B 节）**

| ID | 问题 | 推荐 | 不决定时 |
|---|---|---|---|
| D-FLOAT | 验证结果与 Profile 阈值等核心模型在哈希里用浮点数，跨平台可能不一致；改成精确小数属于修改冻结契约 | 已起草 [ADR-0052](docs/adr/0052-validation-contract-completion.md)（Proposed）：推荐在 major 2 内加精确小数字段、弃用浮点字段（旧哈希不变） | ✅ 已决定（2026-09-26，Raphael 同意推荐方案）→ ADR-0052 Accepted；按 Codex K3 以 2.1.0 实施，旧 2.0.0 数据须原样可读可重放——已实施（B38 / B41，契约 2.1.0），CODE_COMPLETE / DEBUG_PENDING，待 Codex 复核 |
| D-PFIELDS | 验证 Profile 缺容量、跨资产一致性、开封预算等字段；改 Profile 结构属于红线 | 已起草 [ADR-0052](docs/adr/0052-validation-contract-completion.md)（Proposed）：只加字段，数值仍待校准后冻结 | ✅ 已决定（2026-09-26，Raphael 同意推荐方案）→ ADR-0052 Accepted；已实施（B41），CODE_COMPLETE / DEBUG_PENDING |
| D-CTRL | 校准发现：同一个显著性阈值被两处反向使用（策略检验要求足够显著，负对照要求不显著），调一个就动另一个 | 已起草 [ADR-0052](docs/adr/0052-validation-contract-completion.md)（Proposed）：给负对照单独字段 | ✅ 已决定（2026-09-26，Raphael 同意推荐方案）→ ADR-0052 Accepted；已实施（B41），CODE_COMPLETE / DEBUG_PENDING |
| D-MINEFF | 状态 × 策略的条件假设要求填"最小效应"：它算研究者预先声明的假设内容，还是验证门槛？ | 算假设内容，不作门槛 | ✅ 已决定（2026-09-26，Claude 依 Raphael 授权）：假设内容，不作门槛；生产路径无默认值 |
| D-VFAIL | 生命周期状态机只允许 CANDIDATE → FAILED，没有 VALIDATION → FAILED：验证阶段若出现技术故障（不可复现、运行出错），失败记录会进 Failure Registry，但对象的生命周期状态无法标为 FAILED | 已起草 [ADR-0053](docs/adr/0053-validation-failed-transition.md)（Proposed）：增加 VALIDATION → FAILED，只用于不可复现 / 对象自身运行出错，需证据 | ✅ 已决定（2026-09-26，Raphael 同意推荐方案）→ ADR-0053 Accepted；已实施（B23），CODE_COMPLETE / DEBUG_PENDING |
| D-NET | 本机仓库里没有真实行情数据（只有表结构），真实数据端到端测试只能用"真实格式的小样本"。要跑真正的真实数据，需要运行采集器从币安公共归档下载（公开数据、无密钥） | 授权下载 BTCUSDT / ETHUSDT 各 1～3 天的公共归档（约数万行），只写入本机、不提交仓库 | ✅ 已决定（2026-09-26，Raphael "同意"推荐方案）：下载 BTCUSDT / ETHUSDT 各 1～3 天官方公共归档（无密钥），只写本机、不入仓库；**已执行**（2026-09-26，[能力检查报告](docs/reviews/2026-09-26-dnet-real-data-capability.md)）：K 线 2 天 × 2 标的走通采集→入库→规范化→质量报告→时点选择；建数据集停在标的池（需 `exchangeInfo`，且历史日期按 ADR-0029 仍不可构建），待 Raphael 决定 |
| D-PARTIAL | 回测器的"部分成交"：冻结的回测契约要求每个目标仓位在它自己的那根 bar 上一次成交完，所以按成交量上限没成交完的部分只能取消并报告，不能顺延到后面的 bar | 已起草 [ADR-0054](docs/adr/0054-partial-fill-carry-over.md)（Proposed）：扩展契约（新执行模型、剩余量字段、可选成交量）；在那之前策略每根 bar 重发目标即可逐步到位 | ✅ 已决定（2026-09-26，Raphael 同意）→ ADR-0054 Accepted；已实施（B23）并于 B41 以 2.1.0 重新声明，CODE_COMPLETE / DEBUG_PENDING |
| D-LIST | 真实历史数据建不成研究数据集：标的池需要上市历史，只能由一次公开 REST 调用（exchangeInfo，无密钥）取得；且按 ADR-0029 上市历史从本机首次观察（今天）算起，ADR-0032 不覆盖上市记录，所以历史日期仍不可用 | A：授权一次 exchangeInfo 调用 + "上市历史假设"（仿 ADR-0032，须显式绑定、写入清单），已起草 [ADR-0051](docs/adr/0051-listing-history-assumption.md)（Proposed，推荐 A）；B：只调一次 exchangeInfo、只用今天以后的归档 | ⏸ **暂缓（Raphael 2026-09-26："后面再授权"）**；ADR-0051 保持 Proposed；`ARCHITECTURE_DECISION_REQUIRED`（Claude 曾依一般授权"原则接受"，与 Raphael 对此项的明确暂缓冲突，已撤回）；未发起任何网络调用 |
| D-DEG-IE | 劣化检查全部指标缺失（证据不足）时，是否在新事件主题上发布？新主题会扩展已接受的 ADR-0049 的事件面 | 维持不发布：结果与报告已标明「证据不足」，绝不显示为健康 | ✅ 已决定（2026-09-26，Codex）：在独立主题 `research_loop.degradation.insufficient_evidence` 发布，ADR-0049 相应修订；已实施（B53，`4e6c220`），CODE_COMPLETE / DEBUG_PENDING |
| D-DEP | 持续循环的通用机制放在 `apps/worker`，研究阶段放在 `research/loop`，因此 research 依赖 apps/worker（apps 不依赖 research，边界测试不变）——Claude 依授权已接受（ADR-0049），请确认 | 维持 | ✅ 已决定（2026-09-26，Claude 依 Raphael 授权）：维持 ADR-0049 |

**此外无待决架构决定。**

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

**不阻塞当前阶段（NOT BLOCKING）：** D-30（Phase 4 前）· D-29（首次实现 worker / 实验运行前，最迟 Phase 5 前）·
D-04 与 D-09 数值 TBD-1 ~ TBD-5（Phase 4 校准后冻结）· H-3 ~ H-7 · ADR-0005 / 0006 的 Q-1 ~ Q-7 · Git 提交身份的长期做法

## 7. 当前风险

- ⚠️ 宪法 1.0.0 只是**原则**：验证流水线、泄漏门、多重检验校正、trial 账本都未实现，Profile 数值要到 Phase 4；在那之前没有实验能被实际判定
- ⚠️ 契约 2.0.0 已随合并视为发布；2.1.0（ADR-0052）与 2.2.0（ADR-0055，已进入 `wip/all-code-completion`）均为 minor，按记录版本重放旧数据；当前版本新建对象的信封与哈希随 minor 变化（预期）：以后破坏性变化必须升 major，成本上升
- ⚠️ 契约层只校验结构与声明：传递依赖闭包、Registry 存在性、哈希与真实内容一致、物化数据泄漏检测、Profile 已 frozen 等仍是未实现的 Runner / Registry / Control Plane 义务，不得宣称泄漏已被防住
- ⚠️ `LlmCall` 只保证登记结构：内容可取回、内容与哈希一致、调用登记完整均未实现
- ⚠️ 生命周期证据只保证非空：证据真实性、批准人权限与职责分离属未来授权服务
- ⚠️ 契约层不再拒绝实盘模式：Phase 13 红线在 Control Plane 落地前只靠人与流程
- ⚠️ JSON Schema 在几处弱于运行时（首尾空白、时长符号、跨字段约束）：权威校验必须经过运行时模型
- ⚠️ 外部是否存在 v1 历史数据证据不足，不宣称迁移已在真实数据上验证
- ⚠️ PR 与 CI 尚未配置；本地 warehouse 数据无异地副本（Git 远程只托管代码与文档）
- ⚠️ Docker 未安装、外部数据盘未挂载、WSL 内存约 15 GiB：影响 Phase 1 起的数据工作
- ⚠️ PyIceberg 与 Binance 的关键能力事实已由 Codex 于 2026-09-24 按官方资料复核，PostgreSQL 服务已只读确认在线；实施前仍须按锁定依赖版本做行为 smoke / integration 验证
- ⚠️ 来源若不提供修订关系或修订时间，同一观察的不同版本会成为 competing heads 并使数据集构建 fail closed；需要各来源的 precedence policy 与证据
- ⚠️ **D2 的证据结论**：Binance 官方资料没有给出任何具体 revision 的公开时刻，因此 `binance.spot.publication@1.0.0` 三类主体全部保守取 `available_time = ingest_time` 并写证据缺口；在出现可引用的官方上界并发布新 policy 版本之前，早于本机 ingest 的历史可用区间为空。归档替换同样无法证明先后，一律 competing heads（数据全部保留，但任何“最新”结论 fail closed）
- ⚠️ REST 补尾的四张新表、身份与跨通道纯 policy、严格 decoder 和可重放 collector 已由 D3B～D3D 实现并验收；D3E store / reconciler 及 R1 / R2 / R3 返修已实现、尚待 Codex 复核，验收前 REST 数据不能进入任何数据集
- ⚠️ D3E-R3（`7e9e084`）已关闭 D3E-R1 / R2 留下的边界：REST 元素行现在会重新读取并严格重新解码其首次交付页（已提交的 D3D collection checkpoint，`infrastructure/revision/row_integrity.py::PersistedRowVerifier.verify_rest_elements` / `lawful_response_row`），不再只信"原样批次内容"；归档行同理改用 D1 严格重新解析已发布的归档对象（`verify_archive_elements`）。reconciler 的 `_pinned_read` / `_verify_edge_provenance` 额外按显式 `snapshot_id` 时间旅行重读已提交的证据边批次，核对 R3 覆盖的五张表头。**这仍是未验收的实现**：D3E（含 R1 / R2 / R3）没有 Codex 接受门 commit。**D3E-R3 跨日错误（2026-09-26 发现）**：`_verify_edge_provenance` 按分区（data_type / symbol / day）只遍历自己那一天的证据边批次前缀；若同一个 aggTrade 观察键的 REST revision 跨 UTC 日边界，会把另一天已合法提交的边判定为伪造 / 缺失，破坏该日期的 reconcile / `verified_edges` / PIT。已由 `69f0bf0` 修复（候选分支 `claude/hlens-autorecearch-dev-c05c2b`；只改 `channel_reconcile.py`：同一观察键跨日时，另一天写入的证据边批次按写入它的那一天的完整键集重读并逐项复核，完整性校验不放宽；新增 13 项跨午夜回归，旧代码 13 项全部失败、新代码全部通过；独立只读复核判定 ACCEPTABLE），仍待 Codex 复核，D3E 仍未验收
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

- ❌ 只按 roadmap Phase 1 恢复序列逐批实施；**D3E / R1 / R2 / R3、D4、E1 已提交待 Codex 复核**；E2 起仍关闭；不写任何 WebSocket 代码
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
| 2026-09-27 | 模块打磨批次（分支 `codex/tonight-module-polish-2026-09-27`）拣选 4 个代码细节修复：研究策略的已实现波动率与 Provider 口径一致（`e333f6d` ← `1023afb`）；研究日志拒绝过期写入者在他人追加后继续写入（`86d8e3b` ← `9181c58`）；控制台 Gate Calibration 显示报告自带的检测误差区间与 G5 区块（`d595cc8` ← `8de6522`）；控制台 degradation 解析对空指标与矛盾的 insufficient_evidence 失败关闭（`2947ca0` ← `66980f4`） | 四项修改已独立复核；全量 pytest 互补分片覆盖合计 7,307 passed、137 skipped、0 failed（2 项因可选 Uvicorn 未安装跳过；135 项因未配置专用 `HLENS_TEST_CATALOG_URI` 跳过）；`ruff check` 全过、format 772 文件符合、mypy 599 个源文件无问题。只是代码细节修复，**不代表任何 Phase 验收**；无契约 / ADR / Constitution / Profile 数值变化；已通过 PR #2 合并至 `main`（`dca815c`）；无 tag |
| 2026-09-27 | 全框架并入 `main` 并推送到远端；16 个无独有补丁且未被 worktree 使用的本地分支已先归档再删除 | 远端与本地 `main` 同步；16 个恢复引用位于 `refs/archive/2026-09-27/`；其余独有或活动分支保留；无 tag |
| 2026-09-27 | Phase 1 验收记录并入：D3E 已接受、D4 已关闭；E1-CAP-1 结构修复 `a75278e` 已提交，容量实测与返修后测试仍缺 | 记录见 `docs/reviews/2026-09-27-d3e-acceptance.md` 与 `docs/reviews/2026-09-27-e1-review.md`；E1-CAP-1 仍阻断，未复核验收或并入 `main` |
| 2026-09-27 | B67（ADR-0065）：数据集路径上，回测结束仍有未成交的结转余量时，G4 容量检查为不确定（`carry_over_unfilled`），不再用不完整的成交估计容量 | CODE_COMPLETE / DEBUG_PENDING；无余量、合成路径与默认执行模型结果和哈希不变；无阈值 / Profile / 契约变化；不是 Phase 4 / 5 验收；B67 代码 `6d887b7` 全量非 PostgreSQL 门禁 7294 passed、2 个预期的 Uvicorn 未安装 skip，全部检查退出码 0；其后 docs-only 提交只做了文档检查（20 passed） |
| 2026-09-27 | B66（ADR-0064）：数据集路径上 G4 容量所用的成交量须与实际执行的价格 bar 的成交量完全一致，否则容量检查为不确定（`bar_volume_source_mismatch`），不再静默使用另一份数据 | CODE_COMPLETE / DEBUG_PENDING；合成路径与一致时的结果不变；无阈值 / Profile / 契约变化；冻结 HEAD `255ce1a`（B63～B66）全量非 PostgreSQL 门禁 7287 passed、2 个预期的 Uvicorn 未安装 skip，全部检查退出码 0 |
| 2026-09-27 | ADR-0063（Codex 决定）：本机只读研究 API 用 Uvicorn 运行，只绑定 127.0.0.1、单 worker；公网 / 认证 / TLS 不在范围内 | B65 入口与测试已实施（未安装 Uvicorn）；真实 Uvicorn 运行待 Raphael 授权安装 |
| 2026-09-27 | B63：控制台的证据模式说明写明 B62 边界：只证明研究层纸面路由前提，通过不代表 Profile 已冻结、策略已晋升或具备生产资格；列出反向对照项 | 只改控制台文字与测试；无代码 / 契约 / API 改动 |

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

1. 按完成计划和 roadmap 逐模块打磨与验收；先解决 Phase 1 当前阻塞，再安排其余模块的独立验证。
2. 全代码批次已有测试记录，但代码整合不等于 Phase 验收；没有任何策略被验证或晋升，Profile 数值未冻结，系统没有下单能力。
3. `main` / `origin/main` 已同步；四项模块打磨修复已通过 PR #2 合并；全框架、决策记录、Phase 0.5 种子与 Phase 1 D3E / D4 记录已整合；E1-CAP-1 仍阻断，未打 tag。

## 12. 给 Claude Code 的下一步

> Claude 下一步可以执行什么？

1. 审计后续（2026-09-26 晚）：控制台研究循环图表按维度分轴、报告种类夹具由代码生成、错误体路径清理与 500 处理、真实后端冒烟、P9 中等规模校准证据**均已完成**（B44～B52，CODE_COMPLETE / DEBUG_PENDING；完成计划 §10.7 / §10.8）。**P12 提案接入循环：Codex 决定有意暂缓（P12-LOOP，见 §6），不是未实现的代码任务**：进化后代沿用父代 family，每个 family 只评估一次密封样本外，循环自身报告不含后代 G5；**绝不复用密封窗口**，需要新的预注册 family 或独立的未来密封窗口与 Profile 证据规则（完成计划 B57，未决定）。
2. Claude 当前首要开发项：在 `fix/e1-cap1` 补跑并记录固定 M=256（或更低）、N=10k/100k/500k 的分阶段容量探针和返修后定向测试；结果齐备后交 Codex 复核，再决定是否合入 `main`。
3. 其余模块按不重叠任务逐步打磨；任何跨模块契约或架构变化先停在决策边界。
4. 不得：实盘、凭据、下单、猜测 Profile 数值、将代码整合描述为 Phase 验收、force push；按已批准方向在本地 `main` 继续集成，推送与 tag 另行记录。

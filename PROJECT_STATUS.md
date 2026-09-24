# HLENS-AutoResearch 项目驾驶舱

> 面向项目所有者 Raphael。技术细节见 `docs/`，AI 工作规范见 `CLAUDE.md`，Claude 的长期记忆见 `PROJECT_MEMORY.md`。
> 本文件在**每次阶段变化、每个阶段完成、每次出现 ARCHITECTURE_DECISION_REQUIRED** 时必须更新。

## 1. 当前状态

| 项 | 值 |
|---|---|
| 项目版本 | 0.0.0 |
| 当前 Phase | **Phase 1 — Market Representation：🔄 已开启**（2026-09-24，分支 `phase/1`） |
| 当前子阶段 | **D3D REVIEW_PENDING**：REST collector 已实现，等待 Codex 复核；D3E 仍关闭 |
| 上一 Phase | Phase 0 — Research Constitution：✅ 已完成（tag `phase-0-complete`，last known good） |
| 总体状态 | 🔄 Phase 1 进行中（B1～C3、D0～D2、D3A～D3C 已验收；D3D 待复核） |
| 最后更新时间 | 2026-09-25 |

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
Iceberg Catalog、本地 StorageAdapter、八张生产表定义、D0 Collector、D1 fail-closed parser 与 D2 append-only revision store 已验收。D2 首轮复核发现来源 checksum 真实性与 arrival anchor 全表物化两个缺陷；D2-R1 修复后由 Codex 复现旧提交四项失败、运行真实 PostgreSQL 全量与静态检查并接受。D3A 是 REST Raw / lineage 的 docs-only 架构门：Codex 复核草案后退回八项缺陷，D3A-R1 修正后由 Codex 独立复核接受 ADR-0027。D3B 首轮复核发现极端十进制异常泄漏与伪造 / 过期比较可生成边；D3B-R1 修复后由 Codex 接受。D3C 首轮复核发现 RFC JSON 框架空白被误拒；D3C-R1 修复后由 Codex 以 `python -O` 对抗探针、真实 PostgreSQL 3434 项全量与静态检查接受，D3D 开放。

代码仓库已有私有 GitHub 远程 `raphael2025/hlens-autoresearch`（ADR-0025）：执行者只提交，Codex 复核通过后推送每个进度；PR 与 CI 尚未配置。
本次进度推送后，远程 `phase/1` 含 A3a / A3b / B1～C3、D-32、D0～D2 及各验收门；D2 修复后的实现恢复点为 `b05486b`。

Raphael 2026-09-24 的“授权所有、全权接管并开发 / 测试 / 决策 / 文档”明确覆盖 C2 所需的 H12 环境变更：
创建专用 PostgreSQL catalog / test database 与最小权限 role、写入仅本机且被 Git 忽略的凭据文件，并覆盖 D0 / D1 对
Binance 官方公共归档与 `.CHECKSUM` 的有限网络访问及被 Git 忽略的小样本 smoke。该授权不含安装系统软件、修改 PostgreSQL 系统配置、触碰其他数据库 / role、
创建 Control Plane 库、账户接口或任何实盘能力。

收口依据的是 Raphael 2026-09-24"授权所有"的持续授权：Codex 判定它覆盖原则零变化的 Constitution 1.0.0 发布、
Phase 0 closure commit、`main` fast-forward 合并与轻量 tag；**不**覆盖任何原则或阈值变化、实盘、资金或风险预算。

## 2. 当前进度

| Phase | 名称 | 状态 |
|---|---|---|
| 0 | Research Constitution | ✅ 已完成 |
| 0.5 | Public Knowledge Base | ⏸️ 未开始 |
| 1 | Market Representation | 🔄 已开启（D3D 已开放） |
| 2 | Market State Engine | ⏸️ 未开始 |
| 3 | Event & Interaction Engine | ⏸️ 未开始 |
| 4 | Outcome Engine | ⏸️ 未开始 |
| 5 | Strategy Library | ⏸️ 未开始 |
| 6 | State × Strategy | ⏸️ 未开始 |
| 7 | Dynamic Discovery | ⏸️ 未开始 |
| 8 | Validation | ⏸️ 未开始 |
| 9 | Synthetic Market Lab | ⏸️ 未开始 |
| 10 | Dynamic Strategy Router | ⏸️ 未开始 |
| 11 | Continuous Research Loop | ⏸️ 未开始 |
| 12 | Strategy Evolution | ⏸️ 未开始 |
| 13 | Production Adaptive System | ⏸️ 未开始 |
| 14 | Technology Migration | ⏸️ 未开始 |

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
- 🔄 Phase 1 D3D（Claude Opus）：`binance.spot.public-rest@1.0.0` 同步 collector——结构化端点 allowlist、不跟随重定向、有界正文与重试、`Retry-After` / 418 / 5xx / 页数预算、注入时钟与 sleeper；按 D3C 分页并发布不可变正文对象 + page / collection checkpoint；同 `request_id` 重放零网络、崩溃点可续、并发以先发布者为准；四项冻结设置落入 `Settings`。**REVIEW_PENDING**

## 4. 当前正在做

- ✅ B1（Claude）：双时间 / revision DAG 的 8 个契约、Schema（current 46 份）与 contract tests 已通过 Codex 独立复核；验收 #4 满足
- ✅ B2（Claude）：D-31 universe 契约与 `ResearchDatasetManifest` 已通过 Codex 独立验收；验收 #5 满足
- ✅ B3（Claude）：两轮返修后已由 Codex 独立验收；验收矩阵 #6 满足，三个 Data Plane Adapter 接口与 contract suite 已冻结
- ✅ C1-R3（Cursor Auto）：Codex 独立复核通过；整根替换 fail closed 且无旧 / 新根残留；验收矩阵 #7 满足
- ✅ C1-R2（Cursor Auto）：publish 最终 FD 身份重验、可用 ObjectRef、根 FD 生命周期；经 C1-R3 继续返修
- ✅ C1-R1（Cursor Auto）：关闭短写与路径 TOCTOU；经 C1-R2 / C1-R3 继续返修
- ✅ C1（Cursor Auto）：本地 `file://` `LocalFileStorageAdapter` 已实现；经 C1-R1 / C1-R2 / C1-R3 返修加固
- ✅ C2（Claude Opus）：Codex 独立验收通过；验收矩阵 #8 满足，接受 `373e286`
- ✅ C3（Claude Opus + Cursor Auto）：Codex 已独立验收；八张表、batch 指纹、分区演进、四张按天分区表真实写入与重启重放均通过；验收矩阵 #9 满足
- ✅ D0（Cursor Auto）：Codex 已独立验收；验收矩阵 #10 满足，接受 `7a9f468`
- ✅ D1（Claude Opus）：Codex 已独立验收；验收矩阵 #11 满足，接受 `c966085`
- ✅ D2 / D2-R1（Claude Opus）：Codex 已独立验收；验收矩阵 #12 / #17 满足，接受 `ba9f417` + `b05486b`
- ✅ D3A / D3A-R1（Claude Opus，docs-only）：Codex 已验收，ADR-0027 Accepted；验收 #13 的前置设计门满足
- ✅ D3B / D3B-R1（Claude Opus）：Codex 已独立验收；四张 REST 表定义（原八张哈希不变）、独立 REST 身份规则、REST availability / precedence 与通道等价纯函数；无 HTTP、无写入；接受 `3b267a0` + `02c0418`
- ✅ D3C / D3C-R1（Claude Opus）：Codex 已独立验收；接受 `643cf45` + `6b9e670`，验收记录 `docs/reviews/2026-09-25-d3c-rest-decoder-acceptance.md`
- 🔄 D3D（Claude Opus）：**REVIEW_PENDING**；REST collector、四项冻结设置、不可变 page / collection checkpoint、相同 `request_id` 重放不联网、HTTP allowlist / 状态 / 重定向 / 限流 / 重试 / 预算边界均已实现并通过真实 PostgreSQL 全量 3578 项；等待 Codex 对抗复核，D3E 仍关闭

## 5. 下一步

### 我（Raphael）需要做

- 现在无需操作：ADR-0027 已由 Codex 接受，归档与 REST 重叠时的取舍规则（D-33）随之生效；不涉及资金、实盘或研究原则
- 以后如果要**修改任何原则或阈值**，或涉及实盘 / 资金 / 风险预算，需要你对具体内容单独批准

### Claude Code 需要做

- 已批准且已完成：Phase 0 全部批次（B1 / B2 / B3、C1 ~ C5）；Phase 1 S0（开启与分支）
- 已批准且已完成：A1 / A1r / A1r2 / A2 / A2r —— ADR-0021 ~ 0024 起草、两次修正、接受与首切片冻结；ADR-0025 与执行门修正（docs-only）
- 已完成并验收：**Claude B1**（ADR-0023 双时间 / revision DAG 契约、Schema 与 contract tests）与 **Claude B2**（D-31 universe / manifest 契约）
- 已完成并验收：**Claude B3**（Collector / Storage / Catalog Protocol、DTO、Schema、provider-agnostic contract tests）
- C3、D-32、D0、D1 与 D2 均已完成并通过 Codex 独立验收
- D3A / D3A-R1 与接受门（docs-only）已完成并验收：ADR-0027 Accepted，数据架构冻结正文已并入四表 / 标识符 / 设置
- D3B / D3B-R1 与 D3C / D3C-R1 已完成并验收；**D3D REST collector 已实现并进入 REVIEW_PENDING**，等待 Codex 对抗复核；D3E、WebSocket / D4 与 Canonical / E 仍关闭

## 6. 当前待决策

**D3A 提出的决定（已决定，部分实施）**

| ID | 问题 | 方案 | 状态 |
|---|---|---|---|
| D-33 | 同一笔成交 / 同一根 K 线既可能来自官方归档、也可能来自 REST 补尾；按 ADR-0023 它们是同一观察的两条 revision，没有证据即互相冲突，数据集 fail closed。不裁决就等于禁止 REST 补尾 | **Codex 选 A**（ADR-0027 §4）：本机比较两者的规范市场内容，逐字段完全相同才记一条"归档优先"的项目政策证据（存入独立证据表）；不同或无法比较就不记、继续 fail closed。这是项目规则，不是交易所声明的先后 | 已决定：ADR-0027 Accepted（2026-09-25），方案 A 生效；D3B 纯 policy 与 D3C decoder 已验收，D3D～D3E 待实施 |

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
- ⚠️ 契约 2.0.0 已随合并视为发布：以后破坏性变化必须升 major，成本上升
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
- ⚠️ REST 补尾的四张新表、身份与跨通道纯 policy 已由 D3B 实现并验收，严格 decoder 已由 D3C 实现并验收，D3D collector 已实现但**未验收**；D3E store / reconciler 尚未实现，完成并验收前 REST 数据不能进入任何数据集
- ⚠️ D3D 的 market-data base 与 D0-R2 一致：把空路径与单独 `/` 视为 origin 根（`AnyHttpUrl` 默认值即如此渲染），其它任何路径 / query / fragment / 凭据一律拒绝；`Content-Length` 只在响应无 content coding 时与实体长度比对（有 coding 时它计的是编码后字节），解压后的大小上限始终生效
- ⚠️ **三个实现陷阱**（ADR-0027 §11）：身份规则哈希是全局的（REST 必须用独立规则）、同一 `policy_id` 两个版本不能共存于一份 PIT spec、归档与 REST 合入同一观察时序号必须不碰撞（按区间划分，D3E 在单个观察图内检查）；归档身份与分配代码均不改
- ⚠️ D-33 采用精确比较：REST 以毫秒交付、2025 年起归档为微秒，同一笔成交若带亚毫秒位就无法证明相等，只能 fail closed（正确但降低 REST 补尾的价值）；是否改请求微秒需以后单独验证并批准
- ⚠️ REST 的官方事实中，**未被证明**的部分已逐条列出（证据文件 §2）：无任何响应的公开时刻、aggTrade ID 不保证连续、REST 与归档内容不保证一致、未结束 K 线无法从载荷判别。这些都只能 fail closed，不得当作已解决
- ⚠️ 旧研究可能已看过全部 BTC 历史：历史"样本外"区间在认知上不完全干净
- ⚠️ D1 已真实验证两个单位边界日的 kline 与 aggTrades；大体量 BTC 日归档尚未做内存 / 吞吐基线，批量 backfill 前必须先完成容量检查与可恢复 checkpoint

## 8. 当前禁止事项

- ❌ 只按 roadmap Phase 1 恢复序列逐批实施；当前只开放 **D3D**，D3E 仍关闭，须等 D3D 验收
- ❌ 不开始 Phase 0.5
- ❌ 不实现 Feature / Strategy / Backtest（属于 Phase 1+）
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
| 2026-09-25 | Phase 1 D3D（Claude Opus）：`binance.spot.public-rest@1.0.0` collector——结构化 allowlist（origin / 精确 path / 白名单 query，恶意输入零网络拒绝）、不跟随重定向、流式正文上限与 `Content-Length` 核对、`Retry-After` 纯秒有界等待 / 418 立即终止 / 5xx 与传输错误有界重试 / 每次 collect 的新取页预算、注入 UTC 墙钟与 monotonic / sleeper；按 D3C 的 `next_query` 分页，发布内容寻址正文对象 + 不可变 page / collection checkpoint，重放严格重解码并逐字段复现，同 `request_id` 零网络、崩溃点可续、并发以先发布者为准、改请求内容在联网前 fail closed；`Settings` 增加四项冻结字段。全量真实 PostgreSQL 3578 项、`python -O` 38 项对抗探针、只读公共 smoke（两种 data type 各一页）与全部静态检查全绿；冻结哈希与 12 张表定义不变 | REVIEW_PENDING，等待 Codex 复核；D3E 仍关闭 |
| 2026-09-25 | Codex 独立复核 D3C / D3C-R1（`643cf45` + `6b9e670`）PASS：退回并修复 RFC JSON 框架空白；`python -O` 独立对抗探针覆盖合法组合、非 RFC 空白与尾随载荷；decoder / pagination 182 项、docs 7 项、真实 PostgreSQL 全量 3434 项及全部静态检查全绿；冻结哈希除派生 decoder hash 外不变 | D3C 接受；**只开放 D3D**；D3E 关闭；验收记录 `docs/reviews/2026-09-25-d3c-rest-decoder-acceptance.md` |
| 2026-09-25 | Phase 1 D3C-R1（Claude Opus）：按 Codex 复核修正一处缺陷——顶层 JSON 值前后的 RFC 空白（空格 / 制表 / LF / CR）是合法框架，不再当作 `invalid_json` / `trailing_content`；只跳这四个字节，不用 Unicode `isspace()`；非 RFC 空白与任何非空白尾随内容仍整页拒绝；`DECODER_SPEC` 改为写明接受的框架空白，派生的 `DECODER_HASH` 随之改变（D3C 尚未验收）；其余严格性与既有哈希不变 | 等待 Codex 接受门；D3D 仍关闭 |
| 2026-09-25 | Phase 1 D3C（Claude Opus）：严格 REST page decoder `binance.spot.rest.decoder@1.0.0`——正文上限先于 JSON 解析、严格 UTF-8 / 顶层数组 / 无重复键 / 无 NaN / 无尾随内容、精确元素形状与 `decimal(38,18)` 文法、查询下界与 `retrieved_at` 上界、页内与跨页顺序、未结束 K 线（至多一根且必须末项）、ADR §7 answered 区间、四种终止原因与精确续页查询（含 int64 溢出与不前进守卫）；目标窗口只管停止与覆盖，越界合法元素照常解码；拒绝为整页原子、零元素、不含正文；无 HTTP / store / 设置 / 新依赖 / 新公共契约；全量 3416 项、`python -O` 探针与全部静态检查全绿 | REVIEW_PENDING，等待 Codex 复核；D3D 仍关闭 |
| 2026-09-25 | Codex 独立复核 D3B / D3B-R1（`3b267a0` + `02c0418`）PASS：`python -O` 对抗脚本通过；原八张表与归档身份哈希不变；真实 PostgreSQL 全量 3252 项、聚焦 199 项、docs 一致性与全部静态检查全绿 | D3B 接受；**只开放 D3C**，D3D～D3E 关闭；验收记录 `docs/reviews/2026-09-25-d3b-rest-foundations-acceptance.md` |

## 10. 下一阶段进入条件

**Phase 0 关闭条件（roadmap 验收标准）：全部满足**
1. ✅ Constitution 为 Approved（1.0.0）且不含任何数值阈值
2. ✅ Validation Profile 与 Experiment Metadata 的契约已定义
3. ✅ 所有核心实体有契约与 Schema 导出（Phase 0 收口时 current 38 份，现为 74 份，逐字节一致；legacy v1 35 份不变）
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

1. Phase 0 已完成：宪法 1.0.0 已发布（原则一字未改），代码已合并进 `main`，并打了 `phase-0-complete` 标记。
2. Phase 1 已开启，四份架构决定（ADR-0021 ~ 0024）已由 Codex 复核接受；D0～D2、D3A REST 设计、D3B 纯基础与 D3C 严格 decoder 均已验收；当前实施 D3D REST collector。不需要你做任何决定。
   其中 ADR-0022 明确：开发授权不等于实盘授权，Phase 13 之前系统没有下单能力，也不保存交易密钥。
3. 以后若要修改任何原则或阈值，或涉及实盘 / 资金，需要你对具体内容单独批准。
4. 每个经 Codex 复核通过的进度都会推送到私有 GitHub 仓库（ADR-0025）；PR 与 CI 以后再配置。

## 12. 给 Claude Code 的下一步

> Claude 下一步可以执行什么？

1. 已完成：Phase 0 全部批次（契约、状态机、B1 ~ B3、C1 ~ C5），最终恢复点为 tag `phase-0-complete`。
2. 已完成并验收 S0、A1～A3、B1～B3、C1～C3、D0～D2；D2 修复后的实现恢复点为 `b05486b`。
3. D3A～D3C 及返修均已完成并通过 Codex 接受门。当前只执行 D3D；D3E 须等 D3D 验收后开放，不得开始 Phase 0.5。

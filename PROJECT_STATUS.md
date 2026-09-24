# HLENS-AutoResearch 项目驾驶舱

> 面向项目所有者 Raphael。技术细节见 `docs/`，AI 工作规范见 `CLAUDE.md`，Claude 的长期记忆见 `PROJECT_MEMORY.md`。
> 本文件在**每次阶段变化、每个阶段完成、每次出现 ARCHITECTURE_DECISION_REQUIRED** 时必须更新。

## 1. 当前状态

| 项 | 值 |
|---|---|
| 项目版本 | 0.0.0 |
| 当前 Phase | **Phase 1 — Market Representation：🔄 已开启**（2026-09-24，分支 `phase/1`） |
| 当前子阶段 | **C1 已开放，交由 Cursor Auto 执行**：B3 经两轮返修后已由 Codex 独立验收；当前只实现本地 `file://` StorageAdapter，不修改冻结契约 |
| 上一 Phase | Phase 0 — Research Constitution：✅ 已完成（tag `phase-0-complete`，last known good） |
| 总体状态 | 🔄 Phase 1 进行中（B1～B3 已验收，C1 进行中） |
| 最后更新时间 | 2026-09-24 |

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
Collector、Iceberg、数据库、网络访问与数据下载**尚未开始**。A3a（依赖锁定）、A3b（typed settings）与 B1（双时间 / revision DAG 契约）均已由 Codex 独立复核；B1 对抗复核发现 dangling revision ID 可被不同 key 认领，Claude 修复并补回归测试后通过验收。

代码仓库已有私有 GitHub 远程 `raphael2025/hlens-autoresearch`（ADR-0025）：执行者只提交，Codex 复核通过后推送每个进度；PR 与 CI 尚未配置。
本次进度推送后，远程 `phase/1` 含 A3a / A3b / B1～B3 及各验收门；B3 实现恢复点为 `9c57253`。

收口依据的是 Raphael 2026-09-24"授权所有"的持续授权：Codex 判定它覆盖原则零变化的 Constitution 1.0.0 发布、
Phase 0 closure commit、`main` fast-forward 合并与轻量 tag；**不**覆盖任何原则或阈值变化、实盘、资金或风险预算。

## 2. 当前进度

| Phase | 名称 | 状态 |
|---|---|---|
| 0 | Research Constitution | ✅ 已完成 |
| 0.5 | Public Knowledge Base | ⏸️ 未开始 |
| 1 | Market Representation | 🔄 已开启（C1 进行中） |
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

- ✅ 架构蓝图：11 份架构文档、路线图；ADR-0001 ~ 0025 全部 Accepted（0021 ~ 0024 尚待实施）
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

## 4. 当前正在做

- ✅ B1（Claude）：双时间 / revision DAG 的 8 个契约、Schema（current 46 份）与 contract tests 已通过 Codex 独立复核；验收 #4 满足
- ✅ B2（Claude）：D-31 universe 契约与 `ResearchDatasetManifest` 已通过 Codex 独立验收；验收 #5 满足
- ✅ B3（Claude）：两轮返修后已由 Codex 独立验收；验收矩阵 #6 满足，三个 Data Plane Adapter 接口与 contract suite 已冻结
- 🔄 C1（Cursor Auto）：只实现本地 `file://` StorageAdapter；按 B3 suite 验证 staging、流式校验、原子发布、幂等 / 冲突、只读与路径安全

## 5. 下一步

### 我（Raphael）需要做

- 现在无需操作：Codex 已验收 B3 并开放 Cursor C1；额度守护器会在 Opus 五小时用量达到 80% 时暂停并在刷新后恢复
- 以后如果要**修改任何原则或阈值**，或涉及实盘 / 资金 / 风险预算，需要你对具体内容单独批准

### Claude Code 需要做

- 已批准且已完成：Phase 0 全部批次（B1 / B2 / B3、C1 ~ C5）；Phase 1 S0（开启与分支）
- 已批准且已完成：A1 / A1r / A1r2 / A2 / A2r —— ADR-0021 ~ 0024 起草、两次修正、接受与首切片冻结；ADR-0025 与执行门修正（docs-only）
- 已完成并验收：**Claude B1**（ADR-0023 双时间 / revision DAG 契约、Schema 与 contract tests）与 **Claude B2**（D-31 universe / manifest 契约）
- 已完成并验收：**Claude B3**（Collector / Storage / Catalog Protocol、DTO、Schema、provider-agnostic contract tests）
- Claude 当前等待 Cursor C1 与 Codex 验收；不得自行开始 C2
- 当前实现授权仅为：**Cursor C1** 本地 `file://` StorageAdapter；Catalog / Collector / Iceberg / 数据库 / 网络 / 下载仍未开放

## 6. 当前待决策

**Phase 1 入口决定（均已决定，尚待实施）**

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
- ⚠️ 历史 backfill 能否早于本机 ingest 可用，取决于每个来源有证据的 availability policy；缺证据时保守取 ingest 时间，历史研究的可用区间会因此缩小
- ⚠️ 旧研究可能已看过全部 BTC 历史：历史"样本外"区间在认知上不完全干净
- ⚠️ C3 关闭复验与修复实现出自同一 Claude 会话，独立性有限（以 Codex 复核为最终把关）

## 8. 当前禁止事项

- ❌ 只按 roadmap Phase 1 恢复序列逐批实施；当前只批准 Cursor C1；C2 及其后须等各自任务包
- ❌ 不开始 Phase 0.5
- ❌ 不实现 Feature / Strategy / Backtest（属于 Phase 1+）
- ❌ 不安装系统软件（包括 Docker），除非获得授权；创建 PostgreSQL 库 / 角色须先记录授权
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
| 2026-09-24 | Codex 独立验收 B3：2528 项全量测试、ruff / format / mypy、恶意 URI / origin 端口探针和冻结 Schema / 向量比较全部通过；接受实现恢复点 `9c57253` | 验收 #6 满足；B3 与验收门推送；开放 Cursor C1 |
| 2026-09-24 | Phase 1 B3-R2（Claude）：按 Codex 第二轮复核返修——URI 路径中的百分号编码必须合法且不得解出路径分隔符、反斜杠或控制字符（对象 URI 与来源 URI 同一规则），https 来源路径不得有空段；collector 声明的 origin 端口在运行时按 1 ~ 65535、无前导零校验；URI 解析助手移入私有模块，不再是公共契约；本地 2528 项测试通过 | 验收 #6 待 Codex 复核；未推送；C1 未开放 |
| 2026-09-24 | Phase 1 B3-R1（Claude）：按 Codex 复核返修——Catalog 在任何提交或重放成功前必须用已登记、版本化的规则从实际 batch 独立重算指纹并核对行数（内容被换掉即拒绝，不信任自报指纹）；来源 URI 只允许合法主机的 `https` 与无远程主机的 `file:///绝对路径`，对象 URI 同样须真正绝对；读取接口将来以独立 Protocol 或 major + ADR 交付，不向已发布接口追加必需方法；新增 2 项 suite 检查与 2 个故障用例（共 34）；本地 2478 项测试通过 | 验收 #6 待 Codex 复核；未推送；C1 未开放 |
| 2026-09-24 | Phase 1 B3（Claude）：新增 `StorageAdapter` / `CatalogAdapter[BatchT]` / `CollectorAdapter` 三个 Protocol 与 15 个 DTO / Schema（current 59 → 74，旧 Schema、v1 与向量逐字节不变）；`tests/contract_suites/` 提供可复用 suite，两套内存 / 临时文件替身通过全部检查，32 个单点故障用例（路径逃逸、校验和、非原子可见、覆盖、伪造 staging、重复 batch、错误 / 伪造 snapshot、未发布 / 不匹配对象等）全部被杀死；本地 2410 项测试通过 | 验收 #6 待 Codex 复核；未推送；C1 未开放 |
| 2026-09-24 | Phase 1 B2（Claude）：新增 13 个 listing episode / `UniverseSelectionSpec` / 成员与排除 / `ResearchDatasetManifest` 契约与 Schema（current 46 → 59，旧 Schema 与 v1 逐字节不变）；R2 修复 Codex 对抗构造出的 listing lineage / revision 归属漏洞并将第三跳改名 `source_*`；Codex 独立重跑 1938 项全量测试、5 个恶意 payload、ruff / format / mypy 与逐字节 Schema 比较全部通过 | 验收 #5 满足；B2 恢复点 `b41a46a`；B3 已开放 |

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

**Phase 1 关闭条件**：roadmap Phase 1 验收矩阵 #1 ~ #21 全部满足（当前 #1 ~ #6 满足；#7 对应 C1，进行中）。

## 11. 给 Raphael 的下一步

> 我现在应该干什么？

1. Phase 0 已完成：宪法 1.0.0 已发布（原则一字未改），代码已合并进 `main`，并打了 `phase-0-complete` 标记。
2. Phase 1 已开启，四份架构决定（ADR-0021 ~ 0024）已由 Codex 复核接受，首批数据范围与表结构方向已冻结；还没下载数据、没写采集代码。不需要你做任何决定。
   其中 ADR-0022 明确：开发授权不等于实盘授权，Phase 13 之前系统没有下单能力，也不保存交易密钥。
3. 以后若要修改任何原则或阈值，或涉及实盘 / 资金，需要你对具体内容单独批准。
4. 每个经 Codex 复核通过的进度都会推送到私有 GitHub 仓库（ADR-0025）；PR 与 CI 以后再配置。

## 12. 给 Claude Code 的下一步

> Claude 下一步可以执行什么？

1. 已完成：Phase 0 全部批次（契约、状态机、B1 ~ B3、C1 ~ C5），最终恢复点为 tag `phase-0-complete`。
2. 已完成 S0：`phase/1` 分支已创建，Phase 1 开启并进入架构决策子阶段。
   已完成 A1、A1r、A1r2、A2、A2r 与 A3：ADR-0021 ~ 0025 Accepted，首切片冻结，依赖与 typed settings 已推送。
   **Claude B1～B3** 已由 Codex 验收；当前由 Cursor Auto 执行 C1 本地 `file://` StorageAdapter。Claude 不得开始 C2，任何 Agent 都不得开始 Phase 0.5。
3. 不安装软件、不改系统 / Git 配置、不触碰旧项目与外部数据；不做任何原则 / 阈值变化或实盘相关工作。

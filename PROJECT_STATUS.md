# HLENS-AutoResearch 项目驾驶舱

> 面向项目所有者 Raphael。技术细节见 `docs/`，AI 工作规范见 `CLAUDE.md`，Claude 的长期记忆见 `PROJECT_MEMORY.md`。
> 本文件在**每次阶段变化、每个阶段完成、每次出现 ARCHITECTURE_DECISION_REQUIRED** 时必须更新。

## 1. 当前状态

| 项 | 值 |
|---|---|
| 项目版本 | 0.0.0 |
| 当前 Phase | **Phase 1 — Market Representation：🔄 已开启**（2026-09-24，分支 `phase/1`） |
| 当前子阶段 | **架构决策已关闭**：ADR-0021 ~ 0024 已 **Accepted**（Codex 复核 A1r2 通过，A2 记录）；首切片已冻结；实现**尚未开始**，下一步只批准 Cursor A3 |
| 上一 Phase | Phase 0 — Research Constitution：✅ 已完成（tag `phase-0-complete`，last known good） |
| 总体状态 | 🔄 Phase 1 进行中（待实施） |
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
Collector、Iceberg、数据库、网络访问、数据下载与依赖安装**均尚未开始**；下一步只批准 Cursor 执行 A3（锁定依赖、设置骨架）。

代码仓库已有私有 GitHub 远程 `raphael2025/hlens-autoresearch`，由 Codex 在复核通过后推送；PR 与 CI 尚未配置。

收口依据的是 Raphael 2026-09-24"授权所有"的持续授权：Codex 判定它覆盖原则零变化的 Constitution 1.0.0 发布、
Phase 0 closure commit、`main` fast-forward 合并与轻量 tag；**不**覆盖任何原则或阈值变化、实盘、资金或风险预算。

## 2. 当前进度

| Phase | 名称 | 状态 |
|---|---|---|
| 0 | Research Constitution | ✅ 已完成 |
| 0.5 | Public Knowledge Base | ⏸️ 未开始 |
| 1 | Market Representation | 🔄 已开启（架构决策完成，待实施） |
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

- ✅ 架构蓝图：11 份架构文档、路线图；ADR-0001 ~ 0020 全部 Accepted
- ✅ 工程基线：Python 3.13 + uv（ADR-0003）、Git（ADR-0004；私有 GitHub 远程已建立）；pytest / ruff / ruff format / mypy strict 全绿
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

## 4. 当前正在做

- ✅ A2（docs-only）：接受 ADR-0021 ~ 0024；同步 `03-data.md`；冻结首切片表 / 分区 / 标识符 / PIT 输入输出、最小依赖与设置字段；roadmap Phase 1 写入验收矩阵与恢复序列
- ⏭️ 下一步：Codex 复核 A2；之后只执行 Cursor A3（依赖锁定 + 设置骨架）；实现尚未开始

## 5. 下一步

### 我（Raphael）需要做

- 现在无需操作：Phase 0 已按你 2026-09-24 的授权收口；Phase 1 已由 Codex 依同一授权开启，架构决策已完成，下一步由 Cursor 做依赖与设置骨架
- 以后如果要**修改任何原则或阈值**，或涉及实盘 / 资金 / 风险预算，需要你对具体内容单独批准

### Claude Code 需要做

- 已批准且已完成：Phase 0 全部批次（B1 / B2 / B3、C1 ~ C5）；Phase 1 S0（开启与分支）
- 已批准且已完成：A1 / A1r / A1r2 / A2 —— ADR-0021 ~ 0024 起草、两次修正、接受与首切片冻结（docs-only）
- 下一步：**无** Claude 批次获批；下一项获批实现是 **Cursor A3**。B1 起的 Claude 批次须等 A3 完成并由 Codex 下达任务包
- 未批准：A3 以外的任何 Phase 1 实现（Collector / Provider / Iceberg / 数据库 / 网络 / 下载）、其他 Phase、任何原则或阈值变化、实盘

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

- ❌ 只按 roadmap Phase 1 恢复序列逐批实施；当前只批准 Cursor A3，其余实现（Collector、Provider、Iceberg、数据库、网络访问、数据下载）须等各自任务包
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
| 2026-09-24 | Phase 1 A2 docs-only：Codex 复核 A1r2 通过，ADR-0021 ~ 0024 Accepted；`03-data.md` 按 ADR 同步；冻结首批 8 张表、分区、数据源 / parser / policy 版本、PIT 输入输出、最小依赖与设置字段；roadmap Phase 1 写入验收矩阵与恢复序列 | 架构决策子阶段关闭；实现尚未开始；下一项获批实现只有 Cursor A3 |
| 2026-09-24 | Phase 1 A1r2 docs-only：Codex 第二次复核退回；ADR-0023 / 0024 把本机追加顺序（`arrival_seq`）与修订优先级（`revision_id` + `supersedes` + 来源证据）分开，PIT 与 universe 选择改为 maximal-head 算法，competing heads fail closed | 后到的旧修订不再覆盖新修订；无法判定先后时显式失败而不是静默选择；四份 ADR 仍为 Proposed、未实施；下一步 Codex 再复核 |
| 2026-09-24 | Phase 1 A1r docs-only：Codex 复核 A1 退回；ADR-0023 / 0024 把历史可用时间（`available_time`，availability policy）与本机知识时间（`knowledge_time`）分开，PIT / universe 查询带 `simulation_time` 与 `knowledge_cutoff`；写入 manifest 与 `Kind` 裁决；同步事实复核 | 历史 backfill 可用于历史 simulation 且修订不回写过去；四份 ADR 仍为 Proposed、未实施；下一步 Codex 再复核 |
| 2026-09-24 | Phase 1 A1 docs-only：起草 ADR-0021（本地数据基础设施）、0022（市场与执行边界）、0023（双时间与修订）、0024（历史 universe）为 Proposed；ADR 索引把 D-01 / 02 / 08 / 10 / 28 / 31 指向它们 | 技术方案已成文但未接受、未实施；03-data.md §4 的修改只在 ADR-0023 中提出；下一步 Codex 复核 |
| 2026-09-24 | Phase 1 S0：Codex 依 Raphael 持续授权明确开启 Phase 1；从 `main` 创建 `phase/1`；当前只进入架构决策子阶段 | 下一步 A1 docs-only 起草 ADR-0021 ~ 0024；接受前不实现 Collector / Iceberg / 数据库 / 下载；`phase-0-complete` 仍是 last known good |

## 10. 下一阶段进入条件

**Phase 0 关闭条件（roadmap 验收标准）：全部满足**
1. ✅ Constitution 为 Approved（1.0.0）且不含任何数值阈值
2. ✅ Validation Profile 与 Experiment Metadata 的契约已定义
3. ✅ 所有核心实体有契约与 Schema 导出（current 38 份，逐字节一致；legacy v1 35 份不变）
4. ✅ 状态机只允许定义的转移（测试覆盖；主体、时间、证据约束生效）
5. ✅ 契约层无基础设施依赖（导入检查测试）
6. ✅ 本地测试命令可运行（1433 项通过；ruff + mypy strict 全绿）；CI 尚未配置

**Phase 1 开启与实现前提：**
1. ✅ Phase 0 完成
2. ✅ Phase 1 已由 Codex 依 Raphael 持续授权明确开启
3. ✅ ADR-0021 ~ 0024 已 Accepted，决定 D-01、D-02、D-08、D-10、D-28、D-31；首切片已冻结（A2）
4. ⏳ 首次消费的 Provider 先交付 Protocol + DTO + contract tests（ADR-0017；恢复序列 B1 ~ B3），再开始实现（C 起）

**Phase 1 关闭条件**：roadmap Phase 1 验收矩阵 #1 ~ #20 全部满足（当前仅 #1 满足）。

## 11. 给 Raphael 的下一步

> 我现在应该干什么？

1. Phase 0 已完成：宪法 1.0.0 已发布（原则一字未改），代码已合并进 `main`，并打了 `phase-0-complete` 标记。
2. Phase 1 已开启，四份架构决定（ADR-0021 ~ 0024）已由 Codex 复核接受，首批数据范围与表结构方向已冻结；还没下载数据、没写采集代码。不需要你做任何决定。
   其中 ADR-0022 明确：开发授权不等于实盘授权，Phase 13 之前系统没有下单能力，也不保存交易密钥。
3. 以后若要修改任何原则或阈值，或涉及实盘 / 资金，需要你对具体内容单独批准。
4. 代码已推送到私有 GitHub 仓库；PR 与 CI 以后再配置。

## 12. 给 Claude Code 的下一步

> Claude 下一步可以执行什么？

1. 已完成：Phase 0 全部批次（契约、状态机、B1 ~ B3、C1 ~ C5），最终恢复点为 tag `phase-0-complete`。
2. 已完成 S0：`phase/1` 分支已创建，Phase 1 开启并进入架构决策子阶段。
   已完成 A1、A1r、A1r2 与 A2：ADR-0021 ~ 0024 Accepted，首切片冻结。当前没有获批的 Claude 批次：下一项是 Cursor A3，
   B1 起须等 A3 完成并由 Codex 下达任务包；不得开始 Phase 0.5。
3. 不安装软件、不改系统 / Git 配置、不触碰旧项目与外部数据；不做任何原则 / 阈值变化或实盘相关工作。

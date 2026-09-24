# HLENS-AutoResearch 项目驾驶舱

> 面向项目所有者 Raphael。技术细节见 `docs/`，AI 工作规范见 `CLAUDE.md`，Claude 的长期记忆见 `PROJECT_MEMORY.md`。
> 本文件在**每次阶段变化、每个阶段完成、每次出现 ARCHITECTURE_DECISION_REQUIRED** 时必须更新。

## 1. 当前状态

| 项 | 值 |
|---|---|
| 项目版本 | 0.0.0 |
| 当前 Phase | **Phase 0 — Research Constitution：✅ 已完成**（2026-09-24，tag `phase-0-complete`） |
| 下一 Phase | Phase 1 — Market Representation：⏸️ **尚未开始**（需先写入口 ADR 并明确开启） |
| 总体状态 | ✅ Phase 0 收口完成；等待 Phase 1 入口决策 |
| 最后更新时间 | 2026-09-24 |

Phase 0 的全部验收标准已满足：研究宪法已发布为 **`1.0.0 / Approved`**（ADR-0020，原则正文零变化、无数值阈值、只前向适用）；
领域契约、状态机、三层验证契约、错误分类、Schema 导出与工程基线均已实现并通过两轮关闭复审
（C1 `FIX_BEFORE_CLOSE` → ADR-0018 / 0019 修复 → C3 `READY_FOR_HUMAN_CONSTITUTION_GATE`）。
`phase/0` 已 fast-forward 合并进 `main`，并打轻量 tag `phase-0-complete`；契约 `2.0.0` 随之视为**已发布**，
此后任何破坏性契约变化都必须升 major 并走 ADR。仓库仍无远程，因此没有 push、PR 或 CI。

收口依据的是 Raphael 2026-09-24"授权所有"的持续授权：Codex 判定它覆盖原则零变化的 Constitution 1.0.0 发布、
Phase 0 closure commit、`main` fast-forward 合并与轻量 tag；**不**覆盖任何原则或阈值变化、实盘、资金或风险预算。

## 2. 当前进度

| Phase | 名称 | 状态 |
|---|---|---|
| 0 | Research Constitution | ✅ 已完成 |
| 0.5 | Public Knowledge Base | ⏸️ 未开始 |
| 1 | Market Representation | ⏸️ 未开始 |
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
- ✅ 工程基线：Python 3.13 + uv（ADR-0003）、本地 Git（ADR-0004）；pytest / ruff / ruff format / mypy strict 全绿
- ✅ 研究 / 生产边界（ADR-0005）、生命周期 v2（ADR-0006）、三层验证架构与两步冻结（ADR-0007）
- ✅ 契约修复 B1 / B2 / ADR-0010：只读载荷、完整实验身份、构造路径与版本语法、v1 只读兼容
- ✅ B3（ADR-0011 ~ 0017）：生命周期主体与授权、信息流白名单、确定性判定与数值合法性、Profile 结构不变量、
  审计身份类型、`LlmCall` 登记结构、Provider 交付节奏（方案 B，Provider Protocol 数为 0 是决定）
- ✅ 关闭复审 C1（`FIX_BEFORE_CLOSE`）→ ADR-0018 语义身份、ADR-0019 生命周期证据最小结构 → C3 修复后复验
  （`READY_FOR_HUMAN_CONSTITUTION_GATE`）
- ✅ 研究宪法 `1.0.0 / Approved`（ADR-0020，第一至第九章正文 sha256 不变）
- ✅ Phase 0 正式关闭；`main` fast-forward；tag `phase-0-complete`

## 4. 当前正在做

- ✅ Phase 0 已收口，目前没有进行中的实现批次
- ⏭️ 下一步是准备 Phase 1 入口 ADR（D-01、D-02、D-08、D-10、D-28、D-31）；Phase 1 **不会自动开始**

## 5. 下一步

### 我（Raphael）需要做

- 现在无需操作：Phase 0 已按你 2026-09-24 的授权收口
- 以后如果要**修改任何原则或阈值**，或涉及实盘 / 资金 / 风险预算，需要你对具体内容单独批准
- 决定远程仓库位置（阻塞 PR / CI 与异地备份）

### Claude Code 需要做

- 已批准且已完成：Phase 0 全部批次（B1 / B2 / B3、C1 ~ C5）
- 未批准：Phase 1 或其他 Phase 的任何实现、环境安装、任何原则或阈值变化、实盘

## 6. 当前待决策

**Phase 1 入口决定**（在 Phase 1 入口 ADR 中决定；本文件不选方案）

| ID | 问题 | 决定时点 |
|---|---|---|
| D-01 | Iceberg Catalog 选择 | Phase 1 入口 |
| D-02 | 没有 Docker 时的对象存储 | Phase 1 入口 |
| D-08 | 市场与执行范围（交易所 / 标的 / 频率） | Phase 1 入口 |
| D-10 | NATS 引入时机 | Phase 1 入口 |
| D-28 | 迟到 / 修订数据的 point-in-time 可用时间与 revision / as-of / vintage 语义 | Phase 1 入口 |
| D-31 | C-L4 历史可交易标的池、上市 / 下架有效期及 Instrument 表达 | Phase 1 入口 |

**Phase 0 修复裁决（均已决定、已实施、已复验）**

| 裁决 | 结论 | ADR / 状态 |
|---|---|---|
| D-26 | 三类语义身份（Profile 选择键、`Ref` 目标、`GitCodeRevision` 代码修订）排除信封版本；全局相等与内容哈希不变 | [0018](docs/adr/0018-contract-value-semantic-identities.md)，Accepted，已实施（C2c），C3 复验通过 |
| D-27 | 每条生命周期转移至少一项非空证据；不做自报职责分离 | [0019](docs/adr/0019-lifecycle-evidence-minimum.md)，Accepted，已实施（C2d），C3 复验通过 |

**远程仓库位置**（阻塞 PR / CI）
- 可选方案：A. GitHub 私有仓库；B. 继续纯本地；C. 其他托管。推荐方案：A，私有。

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
- ⚠️ 无远程仓库：没有异地备份、PR 与 CI
- ⚠️ Docker 未安装、外部数据盘未挂载、WSL 内存约 15 GiB：影响 Phase 1 起的数据工作
- ⚠️ 旧研究可能已看过全部 BTC 历史：历史"样本外"区间在认知上不完全干净
- ⚠️ C3 关闭复验与修复实现出自同一 Claude 会话，独立性有限（以 Codex 复核为最终把关）

## 8. 当前禁止事项

- ❌ 不开始 Phase 0.5 或 Phase 1 的任何实现，直到入口 ADR 获批并明确开启
- ❌ 不实现 Feature / Strategy / Backtest（属于 Phase 1+）
- ❌ 不安装软件（包括 Docker），除非获得授权
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
| 2026-09-24 | C5：Phase 0 正式关闭（docs-only closure commit）；`phase/0` fast-forward 合并进 `main`；轻量 tag `phase-0-complete` | Phase 0 完成；契约 2.0.0 随合并视为已发布，此后破坏性变化必须升 major；Phase 1 尚未开始；无远程、未 push |
| 2026-09-24 | C4b docs-only：ADR-0020 Accepted（Raphael 2026-09-24 授权，经 Codex 复核），Constitution 发布为 `1.0.0 / Approved`，只改页首版本 / 状态并追加修改历史 | Phase 0 全部验收标准已满足；第一至第九章正文 sha256 不变；未改代码 / 测试 / Schema；下一步 C5 关闭、合并 main、tag |
| 2026-09-24 | C3 修复后只读复验结论 `READY_FOR_HUMAN_CONSTITUTION_GATE`；C4a docs-only：固化 C3 报告，起草 ADR-0020（Constitution 1.0.0，原则零变化，Proposed），修正状态漂移，`02-domain.md` §3.7 写明字符串校验的运行时 / Schema 边界 | Phase 0 唯一剩余验收项是 Constitution 1.0.0；Raphael 持续授权已记录；下一步 Codex 复核后 C4b → C5；未改代码 / 测试 / Schema / Constitution |
| 2026-09-24 | C2d：实施 ADR-0019（`LifecycleTransition.evidence` 必填、至少一项、每项非空，覆盖全部合法边）；新增 `tests/test_lifecycle_evidence.py`（red 31 failed → green 54 passed）；既有测试 helper 补测试证据 | F5 修复；全量 1433 passed；2 份 current Schema 变化；不做自报职责分离；v1 资产零差异；待 Codex 复验；下一步 Phase 0 关闭复验 |
| 2026-09-24 | C2c：实施 ADR-0018（三类语义身份 API；选择规则判重与查询同源；生命周期 / LIVE subject 与部署代码修订按语义身份比较；`research_class` 共用 `RESEARCH_CLASS_PATTERN`）；新增 `tests/test_semantic_identities.py`（red 43 failed → green 75 passed） | F1 / F3 修复；全量 1379 passed；8 份 current Schema 只多了 `research_class` pattern；全局 `==` 与内容哈希不变；v1 资产零差异；待 Codex 复验；下一步 ADR-0019 |

## 10. 下一阶段进入条件

**Phase 0 关闭条件（roadmap 验收标准）：全部满足**
1. ✅ Constitution 为 Approved（1.0.0）且不含任何数值阈值
2. ✅ Validation Profile 与 Experiment Metadata 的契约已定义
3. ✅ 所有核心实体有契约与 Schema 导出（current 38 份，逐字节一致；legacy v1 35 份不变）
4. ✅ 状态机只允许定义的转移（测试覆盖；主体、时间、证据约束生效）
5. ✅ 契约层无基础设施依赖（导入检查测试）
6. ✅ 本地测试命令可运行（1433 项通过；ruff + mypy strict 全绿）；CI 因无远程而延期

**进入 Phase 1 需要：**
1. ✅ Phase 0 完成
2. ⏳ 写出并批准 Phase 1 入口 ADR，决定 D-01、D-02、D-08、D-10、D-28、D-31
3. ⏳ 按 roadmap 规则明确开启 Phase 1（首次消费的 Provider 需先交付 Protocol + DTO + contract tests，ADR-0017）

## 11. 给 Raphael 的下一步

> 我现在应该干什么？

1. Phase 0 已完成：宪法 1.0.0 已发布（原则一字未改），代码已合并进 `main`，并打了 `phase-0-complete` 标记。
2. 目前不需要你做任何决定。下一步是准备 Phase 1 的入口决定；Phase 1 不会自动开始。
3. 以后若要修改任何原则或阈值，或涉及实盘 / 资金，需要你对具体内容单独批准。
4. 建议尽快决定远程仓库（推荐 GitHub 私有），这样才有异地备份、PR 与 CI。

## 12. 给 Claude Code 的下一步

> Claude 下一步可以执行什么？

1. 已完成：Phase 0 全部批次（契约、状态机、B1 ~ B3、C1 ~ C5），最终恢复点为 tag `phase-0-complete`。
2. 未批准任何实现任务：Phase 1 入口 ADR 需由 Codex 下达后才可起草；不得开始 Phase 0.5 / Phase 1 实现。
3. 不安装软件、不改系统 / Git 配置、不触碰旧项目与外部数据；不做任何原则 / 阈值变化或实盘相关工作。

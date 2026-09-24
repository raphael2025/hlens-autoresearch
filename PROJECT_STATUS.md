# HLENS-AutoResearch 项目驾驶舱

> 面向项目所有者 Raphael。技术细节见 `docs/`，AI 工作规范见 `CLAUDE.md`，Claude 的长期记忆见 `PROJECT_MEMORY.md`。
> 本文件在**每次阶段变化、每个阶段完成、每次出现 ARCHITECTURE_DECISION_REQUIRED** 时必须更新。

## 1. 当前状态

| 项 | 值 |
|---|---|
| 项目版本 | 0.0.0 |
| 当前 Phase | **Phase 0 — Research Constitution**（进行中） |
| 当前子阶段 | 批次 C：关闭复审 C1 已完成（结论 **`FIX_BEFORE_CLOSE`**）；C2a 已固化 C1 报告并起草 ADR-0018 / 0019；C2b 已将两份 ADR **Accepted**；C2c / C2d 已实施 ADR-0018 / 0019；C3 修复后关闭复验结论 **`READY_FOR_HUMAN_CONSTITUTION_GATE`**；C4a 已固化 C3 报告并起草 ADR-0020；C4b 已接受 ADR-0020 并发布 **Constitution 1.0.0**（原则零变化）；下一步 C5 关闭 Phase 0、fast-forward 合并 main、轻量 tag |
| 总体状态 | 🔄 进行中 |
| 最后更新时间 | 2026-09-24 |

独立审查发现的不可变性与实验身份问题已由 B1（ADR-0008）、B2（ADR-0009）与 ADR-0010 纠偏修复。
契约 `2.0.0` 只在 `phase/0` 分支生成，**尚未发布**：未合并 `main`、无 tag、无远程发布、无任何 v2 数据登记。
B3 的剩余遗漏已由 Codex 裁决（D-17 ~ D-25）并写成 ADR-0011 ~ 0017，七份于 2026-09-24 全部 **Accepted 且已实施**。
ADR-0011（生命周期主体 / 授权 / 时间）、ADR-0012（信息流白名单与不可覆盖的 `kind`）、ADR-0013（确定性判定函数与拒绝 NaN / ±Inf）、ADR-0014（Profile 普适结构不变量）、ADR-0015（审计身份类型与版本绑定）与 ADR-0016（`LlmCall` 内容绑定）的契约代码均已实施并由 Codex 独立复验（ADR-0016 = `1ad9f59`）。
ADR-0017 是**方案 B 的交付节奏**，其实质交付（`05-plugin.md` §3、`02-domain.md` §4、`core/contracts/README.md`、roadmap 跨 Phase 规则）已在接受 ADR 的 docs-only 提交 `2ff1798` 中完成；批次 7 只做一致性验收与状态收口，按 ADR 不产生任何 Provider 代码，`core/contracts/` 中的 Provider Protocol 数量**仍为 0**。
ADR-0016 关闭的是**登记结构**，内容可取回性与内容 - 哈希一致仍是存储层义务，`06-experiment.md` §2 的“完整输入输出”要求仍未完全满足。
关闭复审 C1（基线 `fce4f81`）已完成，结论 **`FIX_BEFORE_CLOSE`**：四项工程检查全绿、Schema 逐字节一致、v1 资产零变化，但 Profile 选择规则的唯一映射可被嵌套 `schema_version` 绕过（F1，P1），另有若干文档漂移。Codex 已裁决 D-26 ~ D-31：D-26 / D-27 写成 ADR-0018 / 0019，已 **Accepted 并分别在 C2c / C2d 实施**，D-28 ~ D-31 登记为开放问题。报告见 `docs/reviews/2026-09-24-phase0-closing-review-c1.md`。
修复后关闭复验 C3（基线 `4a2951a`）结论 **`READY_FOR_HUMAN_CONSTITUTION_GATE`**：无 P0 / P1 / P2，F1 / F3 / F5 已修复，1433 passed、四项检查全绿、Schema 逐字节一致、v1 资产零变化；报告见 `docs/reviews/2026-09-24-phase0-closing-review-c3.md`。
Constitution 门已通过：ADR-0020 于 2026-09-24 Accepted，`docs/research/constitution.md` 发布为 **`1.0.0 / Approved`**，第一至第九章原则正文逐字不变（sha256 `4d603d62…259cd` 改动前后相同），无数值阈值，只前向适用。依据是 Raphael 2026-09-24"授权所有"的持续授权，Codex 解释为覆盖原则零变化的发布与 Phase 0 收口（关闭、fast-forward 合并 main、轻量 tag），不含实盘 / 资金 / 风险预算（来源与限定见 ADR-0020）。
Phase 0 **尚未关闭**：正式关闭、main 合并与 tag 由 C5 执行。
验收记录见 `docs/reviews/2026-09-23-b1-b2-acceptance.md`；任务方案见 `docs/reviews/2026-09-23-opus-supervision-plan.md`。

## 2. 当前进度

| Phase | 名称 | 状态 |
|---|---|---|
| 0 | Research Constitution | 🔄 进行中 |
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

- ✅ 架构蓝图：11 份架构文档、路线图、研究宪法草案
- ✅ 项目记忆体系：`PROJECT_MEMORY.md` + CLAUDE.md 中的读取、更新和恢复规则
- ✅ D-06 已定：Python 3.13 + uv，与系统 Python 隔离（ADR-0003）
- ✅ D-07 已定：本地 Git 仓库，首个 baseline commit 已完成（ADR-0004）
- ✅ D-03 已定：研究 / 生产边界冻结（ADR-0005 Accepted）
- ✅ D-05 已定：策略生命周期 v2 冻结（ADR-0006 Accepted，取代 ADR-0002 第 5 条）
- ✅ D-09 结构已定：三层验证架构 + 两步冻结（ADR-0007 Accepted）
- ✅ 研究宪法重组为纯原则（0.2.0-draft，不含任何数值），2026-09-24 按 ADR-0020 发布为 1.0.0（原则零变化）
- ✅ Validation Profile 与 Experiment Metadata 的概念契约已写入文档；复现元组已含 Profile 版本
- ✅ Phase 0 环境：Python 3.13.15 + uv 虚拟环境（未改系统 Python）
- ✅ Phase 0 代码：领域契约、生命周期状态机、三层验证契约、错误分类、JSON Schema 导出
- ✅ Phase 0 工程基线：`pytest` / `ruff check` / `ruff format --check` / `mypy --strict` 均可运行且全绿
- ✅ C-1、C-2 已决定：先纸面交易再成为生产候选；PAPER = 单策略观察，ACTIVE = 组合正式启用（带模拟 / 实盘标记）
- ✅ D-09 提案：验证门槛三层结构 + BTCUSDT 1H 初始参数建议（未批准）
- ✅ B1（ADR-0008）：14 个映射字段改为只读载荷、逐模型内容哈希排除表、规范化 JSON 约定、v1 固定向量
- ✅ B2（ADR-0009）：完整实验身份与依赖内容绑定、报告绑定运行、契约版本号提升到 2.0.0、v1 只读兼容入口
- ✅ ADR-0010 纠偏：复制更新重新校验、唯一 ASCII SemVer 2.0.0 语法、v1 顶层 shape gate、Schema 表达键值格式
- ✅ B1 / B2 / ADR-0010 已通过 Codex 最终独立复验并正式验收（`docs/reviews/2026-09-23-b1-b2-acceptance.md`）
- ✅ B3 只读审计与技术裁决（D-17 ~ D-25）完成，ADR-0011 ~ 0017 经 Codex 文档复核后全部 Accepted（接受提交为 docs-only；七份的实施随后按批次 1 ~ 7 全部完成）
- ✅ B3 批次 1（ADR-0011）：失败的重新验证需人工批准才能退役、历史主体与时间单调、LIVE 证据绑定主体与授权窗口、删除自报的 `live_execution_enabled`、Run 与退役记录的时间顺序与枚举
- ✅ B3 批次 2（ADR-0012）：13 个具体规格的 `kind` 冻结为不可覆盖的字面量、Feature / State / Event / Strategy 的直接输入按白名单收紧（Outcome 不得进入）、`observable_lag` / `training_window` / `state_space` 的局部不变量；lineage 保持现状
- ✅ B3 批次 3（ADR-0013）：整体判定精确等于门结果的确定性函数、两处 `gate_id` 唯一、`threshold` 与来源成对出现、契约基类统一拒绝 NaN / ±Infinity、两个概率型阈值的结构范围 `[0,1]`
- ✅ B3 批次 4（ADR-0014）：Validation Profile 的普适结构不变量——三个 walk-forward 窗口与封存区长度、Paper 观察期必须为正，embargo 与最大延长量非负，成本压力倍数逐元素为正，`cost_model` 必须指向 `cost_model`；不选任何 Phase 4 数值
- ✅ B3 批次 5（ADR-0015）：11 个内容哈希槽位统一为 64 位小写十六进制 `ContentHash`，Git OID 独立为 `GitOid`（40 / 64 位小写，拒绝短 SHA），生产代码身份改为结构化值对象 `GitCodeRevision`（新增第 37 份 Schema），`constitution_version` 复用唯一 ASCII SemVer，三处 Profile 绑定改为 `Ref(kind=profile)` + 内容哈希，元数据改用完整 `ProfileSelection`，`SelectionEntry` 删除重复的版本字段
- ✅ B3 批次 6（ADR-0016）：`LlmCall` 的三个自由字符串哈希被三项**必填**的 `ContentBlobRef`（非空 `uri` + `ContentHash`，可选 `media_type` / `byte_size`）取代，`provider` / `model` 收紧为非空，新增**显式必填且无默认值**的 `called_at`；`ContentBlobRef` 成为第 38 份 current Schema，不进入 v1 清单
- ✅ B3 批次 7（ADR-0017）：Provider 交付节奏的一致性验收与状态收口（docs-only）；验收矩阵 1 ~ 4 逐项核对通过，`core/contracts/README.md` 的模型数与 `02-domain.md` 页首的 ADR 范围两处陈旧事实已修正；未新增任何 Python / 测试 / Schema，Provider Protocol 数仍为 0
- ✅ 关闭复审 C1（独立只读，基线 `fce4f81`）：结论 `FIX_BEFORE_CLOSE`；F1 Profile 选择键可被嵌套 `schema_version` 绕过（P1），F2 v1 旧哈希的输入前提未写明、F3 嵌套信封版本语义未定义、F4 四项开放问题未登记、F5 生命周期证据可为空、F6 文档漂移（P2）
- ✅ C2a（docs-only）：C1 报告固化为审查记录；ADR-0018（D-26 语义身份）与 ADR-0019（D-27 证据最小结构）起草为 Proposed；D-28 ~ D-31 登记；F2 诚实边界写入 `10-migration.md`；F6 当前文档漂移已修正
- ✅ C2b（docs-only）：ADR-0018 补齐第三类语义身份 `GitCodeRevision` = `(commit_oid, tree_oid)`（`DeploymentRecord` 与 `EquivalenceCheck` 按代码身份比较，落实 ADR-0015 原意）与 `core/` 跨对象比较点完整盘点；ADR-0018 / 0019 **Accepted**，实现尚未开始

## 4. 当前正在做

- ✅ B1（ADR-0008 只读载荷）、B2（ADR-0009 实验身份）、ADR-0010 纠偏全部完成并由 Codex 验收通过
- ✅ B3 的七份 ADR 已起草、复核并 Accepted（接受提交只改 Markdown，未动代码、测试、Schema）
- ✅ 复核修正已落实：D-18 编号补齐、ADR-0014 归入 D-20.4、`called_at` 改为显式必填且无默认值
- ✅ ADR-0017 的文档同步已随接受在 `2ff1798` 完成（`05-plugin.md`、`02-domain.md`、`core/contracts/README.md`、roadmap）
- ✅ B3 批次 1（ADR-0011）已实现：4 份 current Schema 重导出，559 项测试全绿，v1 快照与固定向量逐字节不变
- ✅ B3 批次 2（ADR-0012）已实现：13 份 current Schema 重导出，767 项测试全绿，v1 快照与固定向量逐字节不变
- ✅ ADR-0011 与 ADR-0012 的实现提交已通过 Codex **独立复验**（`2544d2a`、`7f9892c`；Codex 实际重跑 767 passed，Ruff / format / mypy / v1 冻结差异检查均通过）
- ✅ B3 批次 3（ADR-0013）已实现：1 份 current Schema 重导出（`ValidationProfile`，只新增两个 `minimum` / `maximum`），832 项测试全绿，v1 快照与固定向量逐字节不变
- ✅ ADR-0013 的实现提交已通过 Codex **独立复验**（`04bb3f8`：832 passed，Ruff / format / mypy 全通过，v1 schema 差异为空，定向边界探针通过）
- ✅ ADR-0014 的实现提交已通过 Codex **独立复验**（`d083273`：928 passed，Ruff / format / mypy 全通过，v1 schema 与固定向量差异为空，duration 的 Python / JSON 入口边界、倍数逐元素约束与 `cost_model.kind` 定向探针均通过）
- ✅ B3 批次 5（ADR-0015）已实现：11 份 current Schema 重导出 + 1 份新增（`GitCodeRevision`，current 共 37 份），1245 项测试全绿，v1 快照与固定向量逐字节不变
- ✅ ADR-0015 的实现提交已通过 Codex **独立复验**（`695a34b`：1245 passed、Ruff / format / mypy 全通过，37 份 current Schema 与新导出逐文件一致，`schemas/v1/` 与 `tests/vectors/v1/` 零差异，GitOid 边界 / 生产代码身份 / Profile kind / 旧字段 extra 的定向探针均通过）——此前的稳定恢复点
- ✅ B3 批次 6（ADR-0016）已实现：4 份 current Schema 重导出 + `LlmCall` 改写 + 1 份新增（`ContentBlobRef`，current 共 38 份），1304 项测试全绿，`schemas/v1/` 与既有 `tests/vectors/v1/` 逐字节不变
- ✅ ADR-0016 的实现提交已通过 Codex **独立复验**（`1ad9f59`：1304 passed，Ruff / format / mypy 全通过，38 份 current Schema 与全量重导出逐字节一致，legacy 35 份 Schema、`tests/vectors/v1/`、ADR-0016 正文零差异，`ContentBlobRef` / `LlmCall` 拒绝路径、JSON 往返实验哈希、v1 三哈希读取探针均通过）——**已独立复验的稳定恢复点**
- ✅ B3 批次 7（ADR-0017）已完成：按 ADR 的验收矩阵 1 ~ 4 逐项核对文档一致性，修正两处陈旧事实（`core/contracts/README.md` 的 36 → 38、`02-domain.md` 页首的 ADR-0011 ~ 0015 → 0011 ~ 0016），并同步 ADR 索引与项目状态；docs-only，未动代码 / 测试 / Schema，Provider Protocol 数仍为 0
- ✅ 关闭复审 C1 已完成（结论 `FIX_BEFORE_CLOSE`，报告 `docs/reviews/2026-09-24-phase0-closing-review-c1.md`）
- ✅ C2a（docs-only）已完成：C1 报告固化、ADR 起草、D-28 ~ D-31 登记、F6 修正
- ✅ C2b（docs-only）已完成：ADR-0018 / 0019 **Accepted**
- ✅ C2c：ADR-0018 已实施——三类显式语义身份 API（`ProfileSelectionKey.selection_identity()`、`Ref.target_identity()`、`GitCodeRevision.code_identity()`），选择规则判重与 `select()` 同源，4 处生命周期 / LIVE subject 比较与部署代码修订比较改用语义身份，`research_class` 共用 `RESEARCH_CLASS_PATTERN`；red 43 failed / 32 passed → green 75 passed；全量 1379 passed，Ruff / format / mypy 全绿；8 份 current Schema 仅 `research_class` 的 `minLength: 1` → `pattern`；v1 三条冻结路径零差异；Codex 以下达 C2d 确认 `9581773` 复验通过（复验明细未写入仓库）
- ✅ C2d：ADR-0019 已实施——`LifecycleTransition.evidence` 必填、至少一项、每项去空白后非空（`EvidenceRef`），覆盖全部 18 条合法边；不做自报职责分离、不核验证据存在性；red 31 failed / 23 passed → green 54 passed；全量 1433 passed，Ruff / format / mypy 全绿；2 份 current Schema（`LifecycleTransition`、`LifecycleHistory`）：`evidence` 进入 required、`minItems: 1`、元素 `minLength: 1`、去掉 `default: []`，类描述同步；v1 三条冻结路径零差异；**待 Codex 独立复验**
- ✅ C3（只读）：修复后关闭复验，结论 `READY_FOR_HUMAN_CONSTITUTION_GATE`，无 P0 / P1 / P2，4 项 P3（C2c / C2d 由同一 Claude 会话实现，C3 独立性有限，已在报告中说明）
- ✅ C4a（docs-only）：固化 C3 报告；起草 ADR-0020（Proposed）；修正 C3 P3-1 状态漂移；`02-domain.md` §3.7 写明字符串校验的运行时 / Schema 边界（C3 P3-2）
- ✅ C4b（docs-only）：ADR-0020 Accepted，Constitution 发布为 `1.0.0 / Approved`；只改页首版本 / 状态行并追加修改历史一行，第一至第九章正文 sha256 不变
- ⏭️ 下一步：C5 关闭 Phase 0、fast-forward 合并 main、轻量 tag `phase-0-complete`；在 Raphael 2026-09-24 持续授权范围内，不需要再次答复

Codex 在 `cd84a4e` 上最终复验的实际结果：
`pytest` 545 passed、`ruff check` All checks passed、`ruff format --check` 94 files already formatted、
`mypy` Success: no issues found in 27 source files。关键探针确认 `model_copy` 后仍为 `FrozenMapping`
且别名隔离，错 kind / 缺依赖 / 空 `run_id` 被拒绝，v1 合法载荷接受、错模型与多余字段拒绝。
该次复验时 Schema 为 current 36 份；**当前**为 current 38 份（`schema_version` 默认 `2.0.0`）、legacy 35 份（`schemas/v1/`，逐字节不变）。

## 5. 下一步

### 我（Raphael）需要做

- 已于 2026-09-24 给出"授权所有"的持续授权；Codex 判定其覆盖原则零变化的 Constitution 1.0.0 发布与 Phase 0 收口（关闭、fast-forward 合并 main、轻量 tag），**现在无需我操作**
- 若将来要**修改任何原则或阈值**，或涉及实盘 / 资金 / 风险预算，仍需要我对具体内容单独批准
- 决定远程仓库位置（不阻塞 Phase 0，但阻塞 PR / CI）

### Claude Code 需要做

- 已批准且已完成：只读复核独立审查发现、准备并批准 ADR-0008 / 0009 方案 A
- 已批准且已完成：Opus 串行实施 B1、B2；两批各留一个可恢复 commit，未合并 main
- 已批准且已完成：ADR-0010 的 D-13 ~ D-16 纠偏，并通过 Codex 最终复验
- 已批准且已完成：把 Codex 的 D-17 ~ D-25 裁决写成 ADR-0011 ~ 0017，并同步 ADR 索引与项目文档
- 已批准且已完成（本轮）：按 Codex 复核结论修正编号与 `called_at`、把七份 ADR 置为 Accepted、
  同步 ADR-0017 涉及的架构文档（docs-only，未动代码 / 测试 / Schema）
- 已批准且已完成：B3 批次 1 —— ADR-0011 的生命周期主体 / 授权 / 时间不变量实现（独立 commit）
- 已批准且已完成：B3 批次 2 —— ADR-0012 的信息流白名单与 `kind` 字面量实现（独立 commit）
- 已批准且已完成：B3 批次 3 —— ADR-0013 的确定性判定函数与数值合法性实现（独立 commit，已复验）
- 已批准且已完成：B3 批次 4 —— ADR-0014 的 Profile 普适结构不变量实现（独立 commit，已复验）
- 已批准且已完成：B3 批次 5 —— ADR-0015 的审计身份类型与版本绑定实现（独立 commit，已复验）
- 已批准且已完成：B3 批次 6 —— ADR-0016 的 `LlmCall` 内容绑定实现（独立 commit，已复验）
- 已批准且已完成：B3 批次 7 —— ADR-0017 的验收与状态收口（独立 docs-only commit；未产生 Provider 代码）
- 已批准且已完成：Phase 0 关闭复审 C1（独立只读，结论 `FIX_BEFORE_CLOSE`）
- 已批准且已完成：C2a（docs-only）—— 固化 C1 报告、起草 ADR-0018 / 0019（Proposed）、登记 D-28 ~ D-31、修正 F6 文档漂移
- 已批准且已完成：C2b（docs-only）—— 按 Codex 最终裁决补齐 ADR-0018 的 `GitCodeRevision` 代码身份与比较点盘点，两份 ADR 置为 Accepted
- 已批准且已完成：C2c —— 实施 ADR-0018（独立 commit，Codex 已确认复验）
- 已批准且已完成：C2d —— 实施 ADR-0019（独立 commit）
- 已批准且已完成：C3 —— 修复后只读关闭复验（`READY_FOR_HUMAN_CONSTITUTION_GATE`）
- 已批准且已完成：C4a（docs-only）—— 固化 C3 报告、起草 ADR-0020（Proposed）、修正状态漂移
- 已批准且已完成：C4b（docs-only）—— 接受 ADR-0020 并发布 Constitution 1.0.0（原则零变化）
- 下一步：C5 关闭 Phase 0、fast-forward 合并 main、轻量 tag `phase-0-complete`
- 未批准：其他 Phase、环境安装、任何原则或阈值变化、实盘

## 6. 当前待决策

**Phase 0 契约修复（已决定并已验收）**
- 问题：独立审查发现不可变对象可被嵌套修改、实验身份未覆盖完整规格，并发现生命周期审批遗漏等问题。
- D-11：[ADR-0008](docs/adr/0008-contract-payload-immutability.md) Accepted，B1 已实施并验收。
- D-12：[ADR-0009](docs/adr/0009-experiment-identity-binding.md) Accepted，B2 已实施并验收。
- D-13 ~ D-16：[ADR-0010](docs/adr/0010-contract-construction-and-canonical-versioning.md) Accepted，纠偏已实施并验收。
- 验收结论见 [B1/B2 验收记录](docs/reviews/2026-09-23-b1-b2-acceptance.md)；未实现 Registry/Runner/存储，未登记任何实验。
- 契约 `2.0.0` 只在 `phase/0` 生成，尚未合并 `main`、无 tag、无远程发布、无 v2 数据登记。

**B3 技术方向（Codex 已裁决并接受，不需要 Raphael 决定）**

D-17 ~ D-25 由 Codex 依授权作出，已写成七份 ADR，2026-09-24 全部 **Accepted**。它们是
**已确定的技术结论**，不是待 Raphael 决策项；Raphael 的批准点仍然只有：Constitution 1.0.0、
main 合并、tag、实盘与环境变更。

| 裁决 | 确定结论 | ADR |
|---|---|---|
| D-17 | 失败的 revalidation 不得自动退役（`REVALIDATION → RETIRED` 需人类批准）；历史主体一致、时间单调；授权有效期与 Risk Gate 时序绑定同一主体；删除自报的 `live_execution_enabled`，实盘开关交未来 Control Plane | [0011](docs/adr/0011-lifecycle-subject-authorization-and-time.md) |
| D-23 | `kind` 冻结为不可覆盖的字面量；Feature / State / Event / Strategy 输入按白名单收紧；Outcome 不得进入输入；lineage 是溯源，不收紧 | [0012](docs/adr/0012-information-flow-and-kind-invariants.md) |
| D-19 | 整体 Verdict 是门结果集合的精确确定性函数；报告外因素必须物化为一个门；`gate_id` 唯一 | [0013](docs/adr/0013-deterministic-verdict-and-finite-numbers.md) |
| D-20 | 全局拒绝 NaN / ±Infinity（不先转 null）；threshold 与来源成对出现；两个概率型阈值的结构范围为 `[0,1]`，**不选任何实际阈值** | [0013](docs/adr/0013-deterministic-verdict-and-finite-numbers.md) |
| D-20.4 | Validation Profile 的普适结构不变量（窗口 / 封存长度 / 压力倍数 / 观察期的符号约束），**不选 Phase 4 数值** | [0014](docs/adr/0014-validation-profile-structural-invariants.md) |
| D-21 | 内容哈希统一为 `ContentHash`；Git OID 独立且必须完整；生产代码身份改为结构化 `GitCodeRevision`（已实施） | [0015](docs/adr/0015-audit-identity-types-and-version-bindings.md) |
| D-22 | `constitution_version` 用唯一 ASCII SemVer；Profile 绑定改为 `Ref(kind=profile)` + 内容哈希；元数据使用完整 `ProfileSelection`；删除重复的版本副本（已实施） | [0015](docs/adr/0015-audit-identity-types-and-version-bindings.md) |
| D-18 | `LlmCall` 三项内容引用全部必填（新值对象 `ContentBlobRef`）；`called_at` 显式必填且**无默认值**；可取回性与内容一致性延期，严禁自报 `verified`（已实施） | [0016](docs/adr/0016-llmcall-content-bindings.md) |
| D-24 | Provider 采用方案 B：Phase 0 只冻结职责与语义，可执行 Protocol / DTO / 契约测试随首次消费它的 Phase 交付并验收（已实施，docs-only；Provider Protocol 数仍为 0） | [0017](docs/adr/0017-provider-delivery-schedule.md) |
| D-25 | 上述收窄仍属**尚未发布**的 `2.0.0`，不升 major（只在 `phase/0`、未合并 main、无 tag / 远程发布 / v2 数据登记）；发布后做同类改变必须升 major | 写入 0011 ~ 0016 各自的版本小节 |

- 串行批次（已由 Codex 授权）：**0011 ✅ → 0012 ✅ → 0013 ✅ → 0014 ✅ → 0015 ✅ → 0016 ✅ → 0017 ✅**，
  每批一个独立的可恢复 commit，中途不发布 v2、不登记实验、不合并 main。
- 当前状态：ADR-0011 ~ 0016 已实现且全部由 Codex 独立复验；ADR-0017 按方案 B 是 docs-only 的
  交付节奏落地（实质同步在 `2ff1798`，批次 7 做验收与状态收口），**不产生 Provider 代码**。
  B3 到此全部结束；Phase 0 关闭复审（批次 C）已于 2026-09-24 获授权，正在进行。
- 编号已确定：D-17 ~ D-25 连续唯一；ADR-0016 = D-18；ADR-0014 = D-20.4（不是新编号）。

**关闭复审 C1 的修复（Codex 已裁决并接受 ADR；0018、0019 均已实施）**

| 裁决 | 结论 | ADR / 状态 |
|---|---|---|
| D-26 | Profile 选择键身份 = `(venue, symbol, timeframe, research_class)`，判重与查询同源；`Ref` 目标身份 = `(kind, name, version)` 用于跨对象主体比较；`GitCodeRevision` 代码身份 = `(commit_oid, tree_oid)` 用于部署与等价检查比较；不全局改写相等与内容哈希 | [0018](docs/adr/0018-contract-value-semantic-identities.md)，Accepted，已实施（C2c） |
| D-27 | 每条生命周期转移的证据引用至少一项且非空；**不**做自报职责分离（Q-5 未决、字符串无法证明分离） | [0019](docs/adr/0019-lifecycle-evidence-minimum.md)，Accepted，已实施（C2d，待复验） |

C1 F2：`read_v1` 算法不变，只在 `10-migration.md` 写明旧哈希的输入前提。

**已登记、尚未决定的开放问题（不在本阶段选方案）**

| ID | 问题 | 决定时点 |
|---|---|---|
| D-28 | 迟到 / 修订数据的 point-in-time 可用时间与 revision / as-of / vintage 语义 | Phase 1 前 |
| D-29 | `apps/worker` 运行实验与 `apps` 不得 import `research` 的边界 | 首次实现 worker / 实验运行前，最迟 Phase 5 前 |
| D-30 | C-L5 `embargo >= 最长 Outcome horizon` 的跨对象校验执行点 | Phase 4 前 |
| D-31 | C-L4 历史可交易标的池、上市 / 下架有效期及 Instrument 表达 | Phase 1 前 |

**研究宪法 1.0.0**（✅ 已决定并发布）
- 结果：`docs/research/constitution.md` 为 `1.0.0 / Approved`，原则正文零变化、无数值阈值，只前向适用（[ADR-0020](docs/adr/0020-approve-research-constitution-v1.md) Accepted）。
- 授权：Raphael 2026-09-24"授权所有"；限定为原则零变化，不含实盘 / 资金 / 风险预算。
- 以后任何原则或阈值变化仍须按宪法第九章另起 ADR，并由 Raphael 对具体变化批准。

**远程仓库位置**（不阻塞 Phase 0；阻塞 PR / CI 流程）
- 问题：是否在 GitHub 建立远程仓库，私有还是公开，仓库名用什么。
- 为什么需要决定：没有远程就无法使用 PR 与 CI；本地历史也没有异地备份。
- 可选方案：A. GitHub 私有仓库（gh CLI 已登录 raphael2025）；B. 暂不建远程，继续纯本地；C. 其他托管。
- 推荐方案：A，私有。

**不阻塞当前阶段（NOT BLOCKING）：** D-01、D-02、D-08、D-10、D-28、D-31（Phase 1 前）· D-30（Phase 4 前）· D-29（最迟 Phase 5 前）· D-04 与 D-09 数值 TBD-1 ~ TBD-5（Phase 4 校准后冻结）· D-09 的 H-3 ~ H-7 · ADR-0005 / 0006 的细节问题 Q-1 ~ Q-7 · 远程仓库与 Git 提交身份的长期做法

## 7. 当前风险

- ⚠️ C1 的 F1 / F3 / F5 已由 ADR-0018 / 0019 修复并经 C3 复验；证据只保证结构非空，真实性、`approved_by` 权限与职责分离仍待未来 Registry / 授权服务
- ⚠️ JSON Schema 无法表达运行时的首尾空白去除：纯 Schema 消费者对带空白的原始输入可能与运行时判断不同，权威校验必须经过运行时模型（`02-domain.md` §3.7）
- ⚠️ C3 的独立性有限（C2c / C2d 与 C3 出自同一 Claude 会话）：Codex 的复核是最终把关
- ⚠️ `venue` / `symbol` / `timeframe` 仍区分大小写且不做规范化（ADR-0018 D-26.4 的明确边界）：Adapter 产出规范值是未来义务
- ⚠️ `read_v1` 只在输入是完整的 v1 持久化规范载荷时才复现历史身份；省略默认字段的载荷会得到不同旧哈希（F2，已写入 10-migration.md）
- ⚠️ ADR-0016 之后 `LlmCall` 只保证**登记结构**完整：契约层打不开 `uri`，因此内容是否可取回、
  取回内容是否真的哈希成 `sha256`、`media_type` / `byte_size` 是否与实际内容相符、已登记内容
  是否不可覆盖、数据外发是否合规、一次实验是否登记了**所有**发生过的 LLM 调用，
  全部是存储层 / Registry / Runner 的未实现义务；06-experiment.md §2 的「完整输入输出」
  要求因此**仍未完全满足**，不得被描述为已关闭
- ⚠️ ADR-0015 之后身份字段只保证**格式**：哈希是否等于被引用对象的真实内容、Git 对象是否存在、
  工作区是否干净、`validation_profile` 指向的版本是否已登记或 frozen、Run / 报告 / 元数据三处
  绑定是否彼此一致，仍是 Registry / Runner / Control Plane 的未实现义务；
  `environment_lock` 的结构化表达按 ADR 明确后续另定
- ⚠️ Profile 的三类约束（时长符号、`cost_model` 的 kind）在导出的 JSON Schema 中**不可表达**：
  时长字段的线格式是 duration 字符串，只有运行时校验强制执行；只读 Schema 的消费者不得
  据此认为这些约束不存在（07-validation.md §5.4）
- ⚠️ 结构合法 ≠ 校准合理：Profile 的窗口长度、压力倍数、观察期取值仍是 Phase 4 校准与冻结决定的义务
- ⚠️ 判定函数只保证**报告内部**自洽：门集合是否完整、`threshold_source` 是否真的指向所绑定
  Profile 的字段、`value` 是否真由声明的 `metric` 算出，仍是未实现的验证服务义务
- ⚠️ 信息流白名单只校验**声明层面的直接引用**：传递依赖闭包的方向性、引用与实例一致、
  物化数据的泄漏检测仍是 Registry / Runner / 验证服务的未实现义务，不得据此宣称泄漏已被防住
- ⚠️ 契约层不再拒绝 `to_mode = LIVE`：Phase 13 红线的执行点现在只在人与流程上，
  直到未来 Control Plane 的可信配置与授权服务落地（ADR-0011 D-17.4 的既定代价）
- ⚠️ `core/compat/v1.py` 的 v1 gate 只做**顶层**形状检查，不是完整 JSON Schema 递归校验
- ⚠️ 传递依赖闭包、trial 权威账本、`run.repro` ↔ Spec 一致性仍是未实现的 Runner / Registry 义务
- ⚠️ 外部是否存在 v1 历史数据证据不足，因此不宣称迁移路径已在真实数据上验证
- ⚠️ Docker 尚未安装：Phase 1 之后的本地服务依赖它
- ⚠️ 外部数据盘未挂载：`~/BTC` 当前不可访问
- ⚠️ 研究宪法 1.0.0 已发布，但只是**原则**：验证流水线、泄漏门、多重检验校正与 trial 账本均未实现，Validation Profile 数值要到 Phase 4 校准后冻结；在那之前仍没有任何实验能被实际判定
- ⚠️ 旧研究可能已看过全部 BTC 历史：历史"样本外"区间在认知上不完全干净
- ⚠️ WSL 内存约 15 GiB：大规模行情数据需要分批处理

## 8. 当前禁止事项

- ❌ 不开始 Phase 0.5（公开知识库）
- ❌ 不实现 Feature / Strategy / Backtest（属于 Phase 1+）
- ❌ 不安装软件（包括 Python 3.13、Docker），除非获得授权
- ❌ 不修改系统配置、`.wslconfig`、Git 全局配置
- ❌ 不修改、移动或删除旧项目（`/mnt/e/alpha-autoquant`、`/mnt/e/hlens-cryptoplus`）与旧数据
- ❌ 不选择 D-09 的五类数值（Phase 4 校准后才冻结）
- ❌ 不在宪法中写入任何数值阈值
- ❌ 不改变 Domain Contract
- ❌ 不因为回测结果修改研究规则
- ❌ Claude 不替 Raphael 做架构决策

## 9. 最近一次变化

| 日期 | 变化 | 影响 |
|---|---|---|
| 2026-09-24 | C4b docs-only：ADR-0020 Accepted（Raphael 2026-09-24 授权，经 Codex 复核），Constitution 发布为 `1.0.0 / Approved`，只改页首版本 / 状态并追加修改历史 | Phase 0 全部验收标准已满足；第一至第九章正文 sha256 不变；未改代码 / 测试 / Schema；下一步 C5 关闭、合并 main、tag |
| 2026-09-24 | C3 修复后只读复验结论 `READY_FOR_HUMAN_CONSTITUTION_GATE`；C4a docs-only：固化 C3 报告，起草 ADR-0020（Constitution 1.0.0，原则零变化，Proposed），修正状态漂移，`02-domain.md` §3.7 写明字符串校验的运行时 / Schema 边界 | Phase 0 唯一剩余验收项是 Constitution 1.0.0；Raphael 持续授权已记录；下一步 Codex 复核后 C4b → C5；未改代码 / 测试 / Schema / Constitution |
| 2026-09-24 | C2d：实施 ADR-0019（`LifecycleTransition.evidence` 必填、至少一项、每项非空，覆盖全部合法边）；新增 `tests/test_lifecycle_evidence.py`（red 31 failed → green 54 passed）；既有测试 helper 补测试证据 | F5 修复；全量 1433 passed；2 份 current Schema 变化；不做自报职责分离；v1 资产零差异；待 Codex 复验；下一步 Phase 0 关闭复验 |
| 2026-09-24 | C2c：实施 ADR-0018（三类语义身份 API；选择规则判重与查询同源；生命周期 / LIVE subject 与部署代码修订按语义身份比较；`research_class` 共用 `RESEARCH_CLASS_PATTERN`）；新增 `tests/test_semantic_identities.py`（red 43 failed → green 75 passed） | F1 / F3 修复；全量 1379 passed；8 份 current Schema 只多了 `research_class` pattern；全局 `==` 与内容哈希不变；v1 资产零差异；待 Codex 复验；下一步 ADR-0019 |
| 2026-09-24 | C2b docs-only：按 Codex 最终裁决补齐 ADR-0018（第三类语义身份 `GitCodeRevision` = `(commit_oid, tree_oid)`，`DeploymentRecord` 与 `EquivalenceCheck` 按代码身份比较；`core/` 跨对象比较点完整盘点），ADR-0018 / 0019 置为 Accepted | 修复方案已正式接受但**尚未实施**；未动代码 / 测试 / Schema；下一步 0018 → 0019 串行实施；D-28 ~ D-31 仍开放；Phase 0 仍未关闭 |

## 10. 下一阶段进入条件

**Phase 0 关闭条件（roadmap 验收标准）：**
1. ✅ current 38 份 + legacy 35 份 Schema，逐字节一致；F1 / F5 已由 ADR-0018 / 0019 修复，C3 复验通过
2. ✅ 转移图测试通过；审批与历史归属校验已由 ADR-0011 补齐、信息流方向已由 ADR-0012 补齐、判定函数与数值合法性已由 ADR-0013 补齐、Profile 结构不变量已由 ADR-0014 补齐、审计身份与版本绑定已由 ADR-0015 补齐、LLM 调用的登记结构已由 ADR-0016 补齐（内容取回与核验按 ADR 延期）；证据最小结构已由 ADR-0019 补齐（C2d），C3 复验通过
3. ✅ 契约层无基础设施依赖（导入检查测试）
4. ✅ 本地测试命令可运行（ADR-0019 批次后实际 1433 项通过）
5. ✅ lint / 类型检查命令可运行（ruff + mypy strict 全绿）
6. ✅ 研究宪法已发布为 1.0.0（ADR-0020 Accepted，原则零变化）

**从 Phase 0 进入 Phase 0.5 / Phase 1，需要：**
1. ✅ 研究宪法已发布为 1.0.0（纯原则，不含数值）
2. Phase 0 验收标准全部通过（见 roadmap）
3. 进入 Phase 1 前另需决定 D-01、D-02、D-08、D-10

## 11. 给 Raphael 的下一步

> 我现在应该干什么？

1. C1 发现的问题已修好，修复后复验 C3 的结论是"可以进入宪法批准门"（`READY_FOR_HUMAN_CONSTITUTION_GATE`）。你已给出"授权所有"，宪法 1.0.0 发布（原则一字不改）与 Phase 0 收口会按授权依次执行，暂时不需要你做决定。
2. 想了解 B3 改了什么，读 §6 的裁决表即可（七份 ADR 都在 `docs/adr/`）。
3. 宪法 1.0.0 已发布（C4b）。接下来：关闭 Phase 0、合并 main、打 `phase-0-complete` tag（C5）→ 准备 Phase 1 的入口决定（Phase 1 不会自动开始）。
4. 以后若要改动任何原则或阈值，或涉及实盘 / 资金，仍需要你对具体内容单独批准。

## 12. 给 Claude Code 的下一步

> Claude 下一步可以执行什么？

1. B1（`4f83e18`）、B2（`4e0f6e3`）、ADR-0010 纠偏（`cd84a4e`）已验收。
2. 已完成：D-17 ~ D-25 的 ADR 起草、复核修正与接受，以及 ADR-0017 的文档同步（全部 docs-only）。
3. 已完成：ADR-0011 ~ ADR-0016 的实现批次（各一个独立 commit，四项工程检查实际运行且全绿），
   六个提交均已由 Codex 独立复验通过（ADR-0016 = `1ad9f59`）。
4. 已完成：批次 7 —— ADR-0017 的验收与状态收口（独立 docs-only commit，四项工程检查实际运行且全绿；
   未新增 Python / 测试 / Schema，Provider Protocol 数仍为 0）。
5. 已完成：关闭复审 C1（结论 `FIX_BEFORE_CLOSE`）与 C2a（docs-only：审查记录、ADR-0018 / 0019 起草、D-28 ~ D-31 登记、F6 文档修正）与 C2b（docs-only：ADR-0018 补齐 `GitCodeRevision`，两份 ADR Accepted）。
   已完成：C2c 实施 ADR-0018、C2d 实施 ADR-0019（各一个独立 commit）。
   已完成：C3 修复后只读复验（`READY_FOR_HUMAN_CONSTITUTION_GATE`）与 C4a（docs-only：C3 报告、ADR-0020 Proposed、状态漂移修正）。
   已完成：C4b（接受 ADR-0020、发布 Constitution 1.0.0，原则正文哈希不变）。
   下一步：C5 关闭 Phase 0、`--ff-only` 合并 main、轻量 tag `phase-0-complete`；D-28 ~ D-31 保持开放。
6. 任何原则或阈值变化、实盘、资金、风险预算都不在现有授权内；不开启 Phase 1。
7. 不安装软件、不改系统 / Git 配置、不触碰旧项目与外部数据。

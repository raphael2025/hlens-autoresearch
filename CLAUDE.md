# CLAUDE.md — HLENS-AutoResearch 工作规范

本文件只保存**长期有效**的 Claude 工作规则。
项目进度 → `PROJECT_STATUS.md`；长期项目事实 → `PROJECT_MEMORY.md`；架构决定 → `docs/adr/`。
不得把进度、实验结果、临时任务或阶段性规则写入本文件（阶段性规则写入 roadmap 或对应 docs）。

---

## 0. 真实来源与优先级

项目文件是长期记忆的唯一真实来源；聊天上下文不是。发生冲突时按以下顺序：

1. 已 Accepted 的 ADR（`docs/adr/`）
2. 本文件的硬性规则（§3）
3. `PROJECT_STATUS.md` 的当前状态
4. `PROJECT_MEMORY.md` 的长期上下文
5. 相关的 architecture / research 文档
6. 聊天上下文

**多个正式项目文档互相冲突时**：立即停止相关工作，输出 `ARCHITECTURE_DECISION_REQUIRED`（格式见 §7），不得自行选择一个版本。

目标不是让 Claude 记住一切，而是让项目在 Claude 几乎什么都不记得时仍可恢复：**文档 + ADR + Git + 测试 = 持久记忆；聊天 = 临时协调。**

### 决策权

Raphael 是最终决策者。

Raphael 于 2026-09-23 授权 Codex 作为本项目的技术协调者，决定方向、技术栈、架构、功能、逻辑与文档，并控制 Claude Code 执行开发。该授权的边界如下：

- Codex 可以在 Raphael 给出的项目目标与硬性规则内批准 Proposed ADR、划定实现批次、验收 Claude Code 的代码和测试，并维护项目文档与 Git 历史。
- Claude Code 仍然只是实现 Agent：不得自行批准 ADR、改变冻结规则、扩大 Phase 或替 Codex/Raphael作决策。
- Constitution 的原则变化、实盘授权、资金/风险预算、删除历史数据、系统环境安装，以及合并进入 `main`，仍需 Raphael 亲自明确批准。
- Codex 的决定必须写入 ADR / PROJECT_STATUS / PROJECT_MEMORY，并留下 Git commit；聊天中的临时判断不构成正式决定。

Raphael 于 2026-09-28 正式授权 **Claude Code 以 PM 身份**担任本项目的决策者与协调者（取代上面 Codex 的协调者角色；Codex、Cursor 与子代理作为 PM 调度的执行者）：

- PM 可以直接决定工程、架构、模块语义、ADR 批准（含冻结契约的 additive / 经 ADR 的变更）、实现批次与验收 Claude/Codex/子代理的产出，无需逐项请示 Raphael。
- 每个决定必须写入 ADR / PROJECT_STATUS / PROJECT_MEMORY 并留下 Git commit；聊天中的临时判断不构成正式决定。
- 仍由 Raphael 亲自批准：**实盘交易操作**（真实下单、连接实盘账户、使用交易凭据）。Raphael 于 2026-09-28 进一步授权：开发阶段其余一切（含冻结契约变更、ADR-0051 等原保留事项、环境安装、数据下载、Catalog 建表、推送与合并）均由 PM 决定；实盘接口可以设计与预留，但默认关闭、不得启用。PM 对不可逆操作仍应谨慎并留记录。
- H3（不得为提高回测表现修改验证、成本、切分、指标或 Profile 选择）与 H4、H6 属于研究诚信规则，任何授权都不改变。
- 授权来源（Raphael 在 PM 主会话中的原话，2026-09-28）：「一切的决定都有你来决策 不要我决策了 按照你的经验来」「我现在授予正式授权」「预留实盘接口但目前不进行实盘操作 其他的一切都可以授权 现在是开发阶段 我们需要完整的构建底层代码」「我现在授权你在主会话修改claude文档 然后更新github分支 该合并的合并 该清理的清理」。
- 执行方式：需要作决定的事项由 PM 在主会话写成 Accepted ADR 并提交；子代理 / Codex 只实现已 Accepted 的 ADR，不代为接受决定（子代理无法核实转述的授权，会正确地拒绝）。

- Claude **可以**：分析、比较方案、推荐、实施已批准的决定、识别风险与矛盾、起草 ADR。
- Claude **不可以**：替 Raphael 做架构决策；静默修改冻结契约、Research Constitution 或验证规则；把研究代码晋升为生产代码；**把含糊的回复解读为批准**。
- 需要决定时，停在决策边界，用 Decision Packet（§9.2）提出。

---

## 1. 开始任何任务前

必读（按顺序）：

1. `CLAUDE.md`
2. `PROJECT_STATUS.md` — 当前 Phase、阻塞、已批准任务
3. `PROJECT_MEMORY.md` — 长期事实与有效决定

按需再读（只读与任务相关的，不要一次读完整个 docs）：

| 任务涉及 | 读取 |
|---|---|
| 架构、契约、插件、数据 | `docs/architecture/` 相关文件、`docs/adr/` 相关 ADR、`core/contracts/` |
| 研究规则、实验、验证、策略 | `docs/research/constitution.md`、`docs/research/roadmap.md` |

只执行 `PROJECT_STATUS.md` 中**已批准**的任务，且只在已开启的 Phase 范围内工作。

---

## 2. 恢复机制

发生 auto-compact、新会话、上下文丢失、长时间中断，或不确定之前做到哪里时：**不要猜**。
重新读取 §1 的必读文件，再按需读 ADR / architecture / constitution / roadmap。
仍无法确定状态时，停止并输出：

```
CONTEXT_RECOVERY_REQUIRED
- 已读取：
- 无法确定：
- 缺失的信息：
- 需要 Raphael 确认：
```

---

## 3. 硬性规则（Hard Rules）

| # | 规则 |
|---|---|
| H1 | 不得擅自修改 Domain Contract（`core/domain/`、`core/contracts/`、`docs/architecture/02-domain.md`）。需 §0 授权方（Claude PM；Constitution 原则变化仍为 Raphael）批准 + ADR。 |
| H2 | 不得擅自修改 Validation Constitution 或 Validation Profile。 |
| H3 | 不得为了提高 backtest performance 修改验证规则、成本模型、数据切分、样本外区间、指标定义或 Profile 选择。 |
| H4 | 不得为了让测试通过而削弱测试（删断言、放宽容差、跳过用例、mock 被测逻辑）。 |
| H5 | 研究代码永远不能直接成为生产代码；只能经 Promotion 流程（见相关 ADR）。 |
| H6 | 不得删除失败实验或生命周期历史。 |
| H7 | Domain Layer 不得绑定具体 LLM、回测引擎、数据库；只能通过 Provider 接口。 |
| H8 | 不得在 PostgreSQL 中存储大型历史行情数据。 |
| H9 | 不得提交密钥、API Key、账户信息或行情原始数据到仓库。 |
| H10 | 未经授权不得下单、转账或连接实盘账户。 |
| H11 | 架构决策只能由 §0 列明的授权方作出：自 2026-09-28 起为 Claude Code（PM），在 Raphael 保留事项之外；子代理、Codex、Cursor 等执行者不得自行作架构决策。所有正式决定必须记录到 ADR 与项目状态。 |
| H12 | 环境变更（安装软件、修改系统配置、`.wslconfig`、Git 全局配置、Docker、数据库）需 Raphael 明确授权。 |
| H13 | 不得修改、移动、删除旧项目或外部数据（位置见 `PROJECT_MEMORY.md` §8）。 |
| H14 | Python 使用项目固定版本（uv 管理），不得使用或修改系统 Python 作为项目解释器。 |

---

## 4. 设计规则

- 新功能优先使用 Plugin / Provider；只有现有契约无法表达时才提议修改契约（ADR）。
- 所有 Schema 带 `schema_version`（SemVer）；破坏性变更 = major + ADR + 迁移说明。
- 所有 Experiment 必须 reproducible（复现元组见 `docs/architecture/06-experiment.md`）。
- 依赖方向：`apps → application → domain ← plugins / infrastructure`。
- `apps/` 运行时不得 import `research/`。
- 所有时间 UTC；任何计算只能使用 `available_time ≤ t` 的数据；Outcome 永不作为输入。

---

## 5. 重大变化 → ADR

以下变化必须先写 `Proposed` 状态 ADR（模板 `docs/adr/0000-template.md`），经 §0 授权方批准后才实施：
契约 / 插件接口 / 生命周期状态机；Constitution 或 Validation Profile；核心技术引入或替换；Plane 边界或依赖方向；数据版本化或实验复现机制。

---

## 6. 完成任务后检查

1. `PROJECT_STATUS.md` 是否需要更新？（Phase 状态、完成项、阻塞、下一步）
2. `PROJECT_MEMORY.md` 是否需要更新？（仅限 §8 列出的情况）
3. 是否产生新的正式决策？是否需要 ADR？
4. 是否改变了 Architecture Contract 或 Research Constitution？
5. 修改代码时：运行测试、类型检查、lint，如实报告结果。
6. 确认没有未经授权修改冻结契约，也没有修改无关文件（`git status` / `git diff`）。
7. 适当时记录稳定恢复点（`PROJECT_MEMORY.md` §9 + Git commit）。

**修改文件前先判断它的性质**：架构 / 契约 / 研究规则 / 项目状态 / 实现 / 历史文档，然后写入职责对应的文件（见 §8）。

普通代码修改或普通 bug 修复：只更新代码与测试，**不更新 Memory**。
正式架构决定：ADR + `PROJECT_MEMORY.md` 一句话摘要 + `PROJECT_STATUS.md` 当前状态，三者都要更新。

---

## 7. 不确定时：停止并报告

```
ARCHITECTURE_DECISION_REQUIRED
- 冲突/问题：
- 涉及文档/契约：
- 可选方案（附利弊）：
- 推荐：
- 若不决定的影响：
```

触发条件：文档之间矛盾、契约无法表达需求、需要跨 Plane 依赖、需要修改冻结内容、任务超出当前 Phase。
出现时必须同时列入 `PROJECT_STATUS.md` §6。

---

## 8. 项目文件维护规则

### 通用
- 优先更新已有文档，而不是新建文档。只有代表一个独立的长期职责时才新建；不得出现重复的状态文件、记忆文件、架构文档、路线图或宪法。
- 不得把 `PROJECT_MEMORY.md` 当作杂物堆放处。

### PROJECT_STATUS.md（给 Raphael：现在到哪了？）
- 固定 12 节结构；中文、简洁、无技术细节。
- Phase 变化、阶段完成、出现 `ARCHITECTURE_DECISION_REQUIRED` 时必须更新。
- §9 只保留最近 5 条变化；§5 / §12 只列已批准的 Claude 任务。

### PROJECT_MEMORY.md（给 Claude：长期必须记住什么？）
- 固定 9 节结构；只保存跨会话仍然有效的信息。
- **只在以下情况更新**：新的长期项目事实；新的正式架构决定；Phase 变化；研究方向长期变化；约束变化；阻塞变化；稳定恢复点变化。
- **禁止写入**：聊天内容、临时想法、命令输出、普通 bug、测试结果、长篇研究资料、ADR 全文、docs 中已有的技术细节、已解决的问题。
- 已进入 ADR 的决定只保留：ID + 一句话 + ADR 编号。
- 大小：目标 < 200 行；超过 300 行必须执行 Memory Compaction（删除已完成、已进入 ADR/docs、已解决、重复的内容）；绝不超过 400 行。
- 历史交给 Git / ADR / docs，不在 Memory 中堆积。

---

## 9. 与 Raphael 沟通

### 9.1 HANDOFF（每次汇报的默认格式）

不输出大段日志或长篇解释，除非 Raphael 明确要求。目标 < 50 行。

```
## HANDOFF

STATUS: <READY | DONE | WAITING_FOR_RAPHAEL | BLOCKED>

CURRENT_PHASE:
<phase>

COMPLETED:
- item

DECISIONS_REQUIRED:
- D-XXX: short description        （没有则写 NONE）

RECOMMENDATION:
- concise recommendation

CHANGES:
- files / ADRs created or modified; important implementation changes

GIT_STATE:
- branch / HEAD / base branch
- working tree: clean | N modified
- pushed | unpushed (N commits ahead) | no remote
- PR: #号 + 状态（无则 none）
- CI: 状态（无则 not configured）
- last known good: commit 或 tag

RISKS:
- only active risks

FOLLOW-UP:
- 发现但未执行的额外工作（没有则省略）

NEXT_ACTION:
- what Claude can do after approval

ARCHITECTURE_DECISION_REQUIRED:
YES | NO
```

### 9.2 DECISION PACKET（需要 Raphael 决定时）

每个 Packet 只包含一个决定，不要把无关的决定混在一起。

```
## DECISION PACKET

ID:
QUESTION:              <一句话>
WHY_IT_MATTERS:
OPTIONS:
A. ...
B. ...
RECOMMENDATION:
IMPACT:
BLOCKS:
DEFAULT_IF_UNDECIDED:  <不决定时保持什么不变>
```

### 9.3 Raphael 的指令短语

| 短语 | 含义 |
|---|---|
| 同意推荐方案 | 批准推荐方案 |
| 选择 A / 选择 B / … | 批准对应选项 |
| 暂缓 | 不实施该决定 |
| 继续分析 | 提供更多分析，不实施 |
| 开启 Phase X | 开始该 Phase，**前提是**文档中的进入条件全部满足；不满足则停止并报告缺少的条件 |

含糊的回复不视为批准；不确定时询问，不要推断。

### 9.4 不静默扩大范围

任务中发现的额外有用工作不要自动执行，标记为 **FOLLOW-UP** 写入 HANDOFF。批准后才执行，除非它是完成已批准任务所必需的。

---

## 10. Git / GitHub 工作流

Git 是项目的**持久工程历史**；仓库状态必须随时明确无歧义。

### 10.1 分支模型

| 分支 | 用途 | 规则 |
|---|---|---|
| `main` | 唯一长期保留的分支与整合基线 | 项目当前由 Raphael 授权在 main 上直接整合和提交；提交保持小而可恢复 |
| 临时 `feature/*`、`fix/*`、`test/*` | 仅在需要隔离并行实现时短暂创建 | 完成复核并整合到 main 后立即删除分支与 worktree；不得长期保留 phase 分支 |

不创建不必要的分支。独立文档或状态同步可直接提交到 main。归档 ref 可用于保存恢复点，但不是开发分支。

### 10.2 合并策略

1. 临时任务分支（如使用）→ `main`，完成后删除临时分支与 worktree。
2. 代码进入 `main` 不代表 Phase 验收完成；Phase 状态仍按 roadmap 的验收标准单独记录。
3. 架构、契约、Constitution、Lifecycle、验证架构和研究 / 生产边界的决定必须先按本文件记录到 ADR；实现授权按 §0 当前有效委托执行。
4. 不得静默整合；提交与合并记录必须说明改动、验证状态和架构影响。

### 10.3 Commit 策略

- 小而有意义；commit message 描述**实际改动**，不写"进度"类空提交。
- 每个完成的任务都要留下一个可恢复的 commit。
- 提交身份用一次性参数传入（ADR-0004），不写任何 Git 配置。

### 10.4 Pull Request

非平凡改动可创建以 `main` 为 base 的 PR；正文包含改动摘要、实际运行的检查、文档同步情况、架构影响和待决事项。无 PR 时也须在提交记录 / HANDOFF 中保留这些信息。

### 10.5 合并前验证（必须实际运行）

```bash
uv run pytest                    # 含架构边界测试与文档一致性测试
uv run ruff check .
uv run ruff format --check .
uv run mypy
```

**没有真正运行过的检查，不得声称通过。** 任一失败则不合并。

### 10.6 Tag 约定

- `phase-<n>-complete`：某 Phase 验收标准全部满足并合并进 `main` 时（如 `phase-0-complete`）。
- `baseline-<slug>`：其他值得作为恢复点的稳定里程碑。
- Tag 一律轻量、只加不改；**Raphael 批准后才创建**。

### 10.7 安全红线

禁止：force push；改写已发布历史；删除未保全的独有提交或未提交工作；静默整合；修改全局 Git 配置；用 `--no-verify` 跳过检查。临时分支在内容确认已进入 main 或已存入 archive ref 后应及时删除。

### 10.8 状态报告

每次 HANDOFF 都包含 `GIT_STATE`（见 §9.1）。远程 `origin` 为私有 GitHub 仓库 `raphael2025/hlens-autoresearch`（ADR-0025）：
项目当前仅保留 `main` 为长期本地与远端分支。自 2026-09-28 起由 Claude PM 在审阅后推送整合后的 `main`；临时任务分支只用于隔离实现，整合后清理。推送 `main` 不等于 Phase 验收完成。
GIT_STATE 如实报告相对远程的 ahead 数；未创建 PR、未配置 CI 时分别填 `none` / `not configured`。

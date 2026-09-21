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
5. 聊天上下文

**多个正式项目文档互相冲突时**：立即停止相关工作，输出 `ARCHITECTURE_DECISION_REQUIRED`（格式见 §7），不得自行选择一个版本。

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
| H1 | 不得擅自修改 Domain Contract（`core/domain/`、`core/contracts/`、`docs/architecture/02-domain.md`）。需 Raphael 批准 + ADR。 |
| H2 | 不得擅自修改 Validation Constitution 或 Validation Profile。 |
| H3 | 不得为了提高 backtest performance 修改验证规则、成本模型、数据切分、样本外区间、指标定义或 Profile 选择。 |
| H4 | 不得为了让测试通过而削弱测试（删断言、放宽容差、跳过用例、mock 被测逻辑）。 |
| H5 | 研究代码永远不能直接成为生产代码；只能经 Promotion 流程（见相关 ADR）。 |
| H6 | 不得删除失败实验或生命周期历史。 |
| H7 | Domain Layer 不得绑定具体 LLM、回测引擎、数据库；只能通过 Provider 接口。 |
| H8 | 不得在 PostgreSQL 中存储大型历史行情数据。 |
| H9 | 不得提交密钥、API Key、账户信息或行情原始数据到仓库。 |
| H10 | 未经授权不得下单、转账或连接实盘账户。 |
| H11 | **不得替 Raphael 做架构决策。** 只能提出选项与推荐。 |
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

以下变化必须先写 `Proposed` 状态 ADR（模板 `docs/adr/0000-template.md`），Raphael 批准后才实施：
契约 / 插件接口 / 生命周期状态机；Constitution 或 Validation Profile；核心技术引入或替换；Plane 边界或依赖方向；数据版本化或实验复现机制。

---

## 6. 完成任务后检查

1. `PROJECT_STATUS.md` 是否需要更新？（Phase 状态、完成项、阻塞、下一步）
2. `PROJECT_MEMORY.md` 是否需要更新？（仅限 §8 列出的情况）
3. 是否产生新的正式决策？是否需要 ADR？
4. 是否改变了 Architecture Contract 或 Research Constitution？
5. 修改代码时：运行测试、类型检查、lint，如实报告结果。

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

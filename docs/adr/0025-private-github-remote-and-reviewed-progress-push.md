# ADR-0025: 私有 GitHub 远程与复核后逐进度推送

| 字段 | 值 |
|---|---|
| 状态 | **Accepted（2026-09-24，Codex 依 Raphael 明确指示批准）** |
| 日期 | 2026-09-24 |
| 决策者 / 批准者 | Codex（依据 Raphael 的明确指示"每个进度记得推github"） |
| 起草者 | Claude Code（Opus）按 Codex 裁决落文（批次 A2r） |
| 相关 Phase | Phase 1 起的全部 Phase |
| 影响范围 | 流程 / 复现性 / Security |
| 是否破坏兼容 | 否：不改任何契约、代码或数据 |
| 取代 | 仅取代 [ADR-0004](0004-git-repository-baseline.md) 决策第 3 条"暂不决定远程仓库"；ADR-0004 其余内容继续有效，正文不回写 |

## 背景

ADR-0004 建立了本地 Git 仓库，但把远程托管位置留作待决项；在此之前 PR、CI 与异地备份都不存在。
Raphael 已明确指示"每个进度记得推github"，仓库已建立私有 GitHub 远程，`main`、`phase/0`、`phase/1` 与 tag
`phase-0-complete` 已推送。`CLAUDE.md`、`PROJECT_STATUS.md`、`PROJECT_MEMORY.md` 中"无远程"的表述因此与事实冲突，
需要一份正式决定作为单一依据。

## 决策

1. **远程**：`origin` = 私有 GitHub 仓库 `https://github.com/raphael2025/hlens-autoresearch`。
2. **谁提交、谁推送**：执行者（Claude Code、Cursor）只在当前 phase 分支上提交，**不 push**。
   Codex 独立复核每个批次并实际运行该批次要求的检查；复核通过后，把每个被接受的恢复点推送到对应的 phase 分支。
   未通过复核的提交不推送。
3. **保持不变**（来自 ADR-0004 与 `CLAUDE.md` §10）：本地 Git 为工程历史；`phase/<n>` 分支模型；一次性 `-c` 提交身份，
   不写任何 Git 配置；禁止 force push 与改写已发布历史；合并进 `main` 仍需 Raphael 批准或其已记录的授权；tag 仍需批准。
4. **不虚报**：远程存在不等于 PR 或 CI 存在。未创建 PR、未配置 CI 时，HANDOFF 与状态文档如实写 `none` / `not configured`。
5. **不入 Git**：行情数据、warehouse、凭据、密钥、`.env` 等永不提交或推送（CLAUDE.md H9）；远程只托管代码与文档，
   **不是** warehouse 的异地备份。

## 备选方案

| 方案 | 优点 | 缺点 | 为何未选 |
|---|---|---|---|
| **A（本 ADR）** 私有 GitHub，Codex 复核后推送 | 异地保存代码历史；推送前有独立复核 | 依赖 GitHub 可用性 | — |
| B 继续纯本地 | 零外部依赖 | 无异地备份；与 Raphael 指示冲突 | 违背明确指示 |
| C 执行者提交后自行推送 | 更快 | 未复核内容进入远程 | 失去复核门 |
| D 公开仓库 | 协作方便 | 研究内容外泄 | 可见性不当 |

## 后果

- 正面：每个被接受的恢复点都有异地副本；状态文档与事实一致。
- 负面 / 代价：推送节奏取决于 Codex 复核；PR / CI 仍需另行建立。
- 需要迁移的内容：无。
- 对复现性：复现元组中的 commit 可在远程取回；数据复现仍依赖本地 catalog 与 warehouse（ADR-0021）。

## 合规检查

- [x] 不修改任何契约、Constitution、验证规则或代码
- [x] 不回写 ADR-0004 正文；只取代其"远程待定"一条
- [x] 不修改全局 Git 配置；不允许 force push
- [x] 不把数据或凭据放入 Git
- [x] 决定依据 Raphael 的明确指示，由 Codex 记录并批准

# ADR-0004: 本地 Git 仓库与 Architecture Bootstrap Baseline（D-07）

| 字段 | 值 |
|---|---|
| 状态 | Accepted |
| 日期 | 2026-09-21 |
| 决策者 | Raphael |
| 起草者 | Claude Code |
| 相关 Phase | Bootstrap → Phase 0 |
| 影响范围 | 复现性 / 流程 |
| 是否破坏兼容 | 否 |

## 背景

复现元组要求记录 `code_commit`；项目记忆体系要求用 Git 保存历史（PROJECT_MEMORY §9 恢复点）。

## 决策

1. 在项目目录执行 `git init`。
2. **不修改全局 Git 配置。**
3. **暂不决定远程仓库**（托管位置、可见性待定）。
4. 第一个 commit 为 **Architecture Bootstrap Baseline**，包含：`CLAUDE.md`、`AGENTS.md`、`PROJECT_STATUS.md`、`PROJECT_MEMORY.md`、`README.md`、`docs/`（含 ADR）。

## 实施说明

- 初始分支名使用 `main`（通过 `git init -b main` 指定，不写配置）。这是 Claude 的实施选择，不属于 Raphael 的决定，可随时改名。
- 首个 commit 还包含 Bootstrap 期间创建的 `pyproject.toml`、`.python-version`、`.gitignore` 与各目录的边界 `README.md`（都是文档或元数据，没有业务代码），避免仓库留下未跟踪文件。这同样是 Claude 的实施选择。
- 全局 Git 配置中没有 `user.name` / `user.email`。为不修改任何 Git 配置，提交时使用一次性参数 `git -c user.name=… -c user.email=…`，**不写入任何 config 文件**。
- 以后如何设置提交身份（仓库本地配置，或继续使用一次性参数）由 Raphael 决定。

## 后果

- 正面：复现元组的 commit 字段可用；项目历史可恢复。
- 待决：远程仓库；提交身份的长期做法。

# ADR-0001: 使用 ADR 记录架构决策

| 字段 | 值 |
|---|---|
| 状态 | Accepted |
| 日期 | 2026-09-21 |
| 决策者 | 项目负责人（Architecture Bootstrap 指令） |
| 相关 Phase | Bootstrap |
| 影响范围 | 流程 |
| 是否破坏兼容 | 否 |

## 背景

项目长期演化，由人与多个 AI Agent 协作开发。没有书面决策记录时，Agent 容易在局部任务中无意改变架构。

## 决策

所有重大架构决策以 ADR 形式记录在 `docs/adr/`，编号递增，格式见 `0000-template.md`。
Agent 只能起草 `Proposed` 状态的 ADR；`Accepted` 需人类批准。
ADR 一经 Accepted 不再修改正文，只能被新 ADR Supersede。

## 后果

- 决策可追溯；Agent 有明确的"停止并提议"路径。
- 小改动也需判断是否"重大"——判定标准见 CLAUDE.md §4。

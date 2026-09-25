# ADR-0047: 技术迁移框架——金标准重跑与 Adapter 一致性（Phase 14）

| 字段 | 值 |
|---|---|
| 状态 | **Accepted**（2026-09-25） |
| 日期 | 2026-09-25 |
| 决策者 | Claude Code（Opus），依 Raphael 2026-09-25 明确授权（红线除外） |
| 相关 Phase | Phase 14（依 Raphael 2026-09-25 全阶段框架实现指示） |
| 影响范围 | `infrastructure/migration/`（无契约变化） |
| 实施状态 | FRAMEWORK_IMPLEMENTED / NOT_VALIDATED |

## 裁决

1. 金标准重跑：`record_golden` 冻结一次实验的具名输出（有限 `Decimal`，规范 JSON 哈希）；`compare_golden` 重跑后逐项
   报告超出**该次迁移 ADR 声明的容差**的差异（容差 0 = 按位一致），缺失 / 多出的输出都算差异。容差属于迁移本身，
   不是验证阈值，不触及 Constitution / Profile。
2. Adapter 一致性：`run_conformance` 对每个检查新建候选实现并运行 provider-agnostic 检查，逐条报告失败，不静默跳过；
   现有 `tests/contract_suites/`（Collector / Storage / Catalog / Feature / Knowledge 等）即检查集合。
3. 每次真实迁移仍须：迁移 ADR（选定容差与映射表）、新 Adapter、金标准重跑报告；借迁移修改契约语义或验证规则一律禁止
   （roadmap P14）。

## 后果

- 正面：迁移有统一的准入工具。
- 负面：金标准实验集合本身尚未选定（需 P4 / P8 实验产出后登记）。

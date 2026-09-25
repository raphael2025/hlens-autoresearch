# ADR-0045: 策略演化算子与谱系（Phase 12）

| 字段 | 值 |
|---|---|
| 状态 | **Accepted**（2026-09-25） |
| 日期 | 2026-09-25 |
| 决策者 | Claude Code（Opus），依 Raphael 2026-09-25 明确授权（红线除外） |
| 相关 Phase | Phase 12（依 Raphael 2026-09-25 全阶段框架实现指示） |
| 影响范围 | `research/evolution/`（研究代码，H5；无契约变化） |
| 实施状态 | FRAMEWORK_IMPLEMENTED / NOT_VALIDATED |

## 裁决

1. 演化算子只产生**新版本**：`mutate`（参数只能在已声明的搜索空间内移动，计入 C-T1 的尝试次数）、`combine`（两个父代，
   参数冲突即拒绝）；子代的 `lineage` 指向父代，父代不变；子代从 `IDEA` 重新进入生命周期并重新验证。
2. `require_new_version`：ACTIVE 策略的任何内容变化必须是新版本且谱系可追溯，否则拒绝（roadmap P12 禁止在线就地修改）。
3. `retire` 产生追加式 `RetirementRecord`（RETIRED ≠ FAILED）；`LineageGraph` 追溯祖先 / 后代并报告缺失的祖先。
4. 劣化信号驱动的自动演化调度属 P11 循环，接入 P8 验证后实现；演化本身过拟合近期数据的风险由完整验证与 trial 计数约束。

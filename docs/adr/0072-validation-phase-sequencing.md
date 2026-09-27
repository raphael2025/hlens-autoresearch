# ADR-0072：最小验证门与稳健性阶段的交付顺序（D-04）

| 字段 | 值 |
|---|---|
| 状态 | **Accepted**（2026-09-27） |
| 决策者 | Codex，依据 Raphael 对项目开发与技术决策的全权授权 |
| 相关 Phase | Phase 4、5、6、7、8 |
| 影响范围 | `docs/research/roadmap.md`、Phase 状态和开发计划；不改变契约、Schema、Validation Profile 或 Constitution |

## 背景

Roadmap 将 Phase 8 命名为 Validation & Robustness，但 Phase 5～7 会产生实验与候选筛选。Roadmap 已采用“Phase 4 交付最小验证门，Phase 8 扩展稳健性”的解释，同时仍把该解释标为待确认（D-04）。需要关闭顺序歧义，避免将 Phase 8 全套扩展能力误当作开始 Phase 5～7 的前置条件，也避免研究运行绕过最小验证保护。

## 决定

选择 D-04 方案 (a)：

1. Phase 4 必须提供 Phase 5～7 所依赖的最小验证能力，包括适用的 G0～G3 门、封存 OOS 边界，以及使其能够在研究流程中被调用的基础接线。
2. Phase 8 在 Phase 6～7 之后交付扩展稳健性能力，例如 G4、过拟合 / 负对照方法、跨资产与多标的检查，以及独立校准工作。
3. Phase 5～7 可以在各自 Phase 范围内开发和运行候选实验，但不得省略其必需的最小验证门，不得把筛查结果当作晋升授权，也不得越过人工生命周期门。
4. 本决定只明确交付顺序，不冻结任何 Profile 数值，不批准实盘，不改变 Phase 5～7 已定义的算法或契约。

## 后果

- D-04 关闭；不把整个 Phase 8 前移到 Phase 5 之前。
- 最小验证门必须先于依赖它的策略、状态 × 策略和动态发现运行能力；Phase 8 的稳健性扩展仍按 Roadmap 保持独立交付。
- 既有 `research/validation` 实现是否满足各 Phase 验收标准，仍需后续统一验收；本 ADR 不构成代码验收或数值校准。

## 参考

- `docs/research/roadmap.md`：Phase 4、5～8 的目标与验收矩阵。
- [ADR-0037](0037-outcome-engine-and-minimal-validation-pipeline.md)：Outcome Engine 与最小 Validation Pipeline。
- [ADR-0041](0041-validation-robustness.md)：稳健性套件、回溯审计与扩展验证。
- `PROJECT_STATUS.md`：当前 Phase 与验收状态。

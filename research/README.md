# research/

Research Plane。探索性研究代码与实验编排。**研究代码不得直接成为生产代码**（CLAUDE.md H5）。代码位置细则见待决 D-03。

## 当前内容

| 模块 | 用途 |
|---|---|
| `states/` | Phase 2（ADR-0035；FRAMEWORK_IMPLEMENTED / NOT_VALIDATED）：状态稳定性诊断（分布、持续时间、转移矩阵、标签闪烁），见 [states/README.md](states/README.md) |

其余子目录仍只是规划，实现需等待对应 Phase（见 docs/research/roadmap.md）。
> 全阶段框架实现（Raphael 2026-09-25 指示）进行中：各子目录的代码状态均为 FRAMEWORK_IMPLEMENTED / NOT_VALIDATED。
| 子目录 | 内容 |
| `strategies/` | Phase 5 研究策略库（ADR-0038）：时间序列动量 StrategyProvider、波动率目标 RiskProvider、策略 → 风控 → 回测 → 验证接缝 → Failure Registry 流水线 |

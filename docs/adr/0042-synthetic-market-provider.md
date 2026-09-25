# ADR-0042: SyntheticMarketProvider 契约与随机游走生成器（Phase 9）

| 字段 | 值 |
|---|---|
| 状态 | **Accepted**（2026-09-25） |
| 日期 | 2026-09-25 |
| 决策者 | Claude Code（Opus），依 Raphael 2026-09-25 明确授权（红线除外） |
| 相关 Phase | Phase 9（依 Raphael 2026-09-25 全阶段框架实现指示） |
| 影响范围 | Contract（`core/contracts/synthetic.py`，additive）、`plugins/synthetic/` |
| 是否破坏兼容 | 否：只新增 5 个模型与 1 个 Protocol；Schema 82 → 87 |
| 实施状态 | FRAMEWORK_IMPLEMENTED / NOT_VALIDATED |

## 裁决

1. `SyntheticMarketProvider`：`generate(SyntheticMarketSpec) → SyntheticMarket`。规格含种子、起止、初始价、每分钟波动与
   漂移、以及植入效应（首个效应类型：收益自相关，`lag_minutes` + `strength ∈ (-1, 1)`）。结果含与 `canonical.bars_1m` 市场
   内容同形的 1 分钟 bar、实际植入的 `truth` 与绑定全部内容的 `market_hash`；给定种子必须确定性，数值一律 `Decimal`。
2. 首个实现 `plugins/synthetic/RandomWalkMarket`：带种子的 `random.Random`，每个抽样立即量化为固定 `Decimal` 精度。
3. 用途只限检验方法（纯噪声上的假阳性率、植入效应的检出力）；**不得**用合成结果支持真实市场结论（roadmap P9 禁止事项）。
   校准框架 `research/synthetic_lab.calibrate`：任意检测器（最终是 P4 / P8 完整验证流水线）在 N 个纯噪声市场与 N 个植入效应市场上
   运行，报告经验假阳性率与检出力；须达到的声明水平属 Validation Profile（不在此设定）。

## 后果

- 正面：验证流水线可在已知真值上做负对照与检出力测试。
- 负面：生成器简单（高斯噪声 + 线性自相关），校准结论的外推性有限——需更丰富的生成器（波动聚集、跳跃、日内季节性）时
  另立版本。

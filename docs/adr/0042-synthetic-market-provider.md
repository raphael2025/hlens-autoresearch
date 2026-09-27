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

## Implementation note (gate calibration harness, 2026-09-25)

状态：FRAMEWORK_IMPLEMENTED / NOT_VALIDATED。不新增契约、不改 Constitution、不改任何 Profile；本说明不是新的决定。

- `research/synthetic_lab/gate_calibration.py`：Phase 4 / Phase 8 验证门的校准 harness。输入全部由调用方给出：
  候选 Profile 网格、纯噪声种子、每个植入效应（强度 × 滞后）的种子、区间 `alpha`；检测器 `StrategyValidatorDetector`
  = 研究窗口上经 `PipelineBacktestValidator` 的完整 G0 → G4（回测按市场缓存，候选 Profile 之间共享）。
- 报告 `GateCalibrationReport`：每个候选 × 每个门的噪声通过率（假阳性率）、每个植入强度的通过率（检出力）、
  INCONCLUSIVE 率、未到达次数，以及 PASS 后消耗封存 OOS 开封的比率（每个 PASS 在独立内存账本上开封一次；G5 本身不在此运行）。
  比率为精确计数 + Clopper-Pearson 区间（`intervals.py`，有理数运算，`Decimal` 输出）。报告确定、`report_hash` 覆盖全部载荷，
  记录全部输入，并声明 **"evidence only — not a Profile decision"**；无推荐 / 默认 / 排名字段。
- 写出：`research/reports/gate_calibration.py`（`<root>/gate_calibration/<report_hash>.json`，append-only）；
  CLI `python -m research.synthetic_lab.gate_calibration --setup pkg.mod:factory --out DIR`（CLI 本身不含任何输入值）。
- D-09 TBD-1..5 仍由 Raphael 在校准后冻结；冻结的 Profile 可在 `provenance.calibration_report` 引用 `report_hash`
  （07-validation.md §2.4）。
- 冒烟观察（非结论）：`significance.multiple_testing_threshold` 同时是 G3 的上限与 G1 负对照的下限，放宽它不会单调提高
  流水线假阳性率；测试中的"宽松候选高假阳性"因此在门层面（`G2.null_model_percentile`）与一个只读该阈值的测试用检测器上演示。
- 已知缺口（调试批次）：检测器异常直接抛出（不计为 INCONCLUSIVE）；未运行 G5；`apps/api` 的 `ReportStore` 尚无
  `gate_calibration` 类型；种子数与市场长度只够冒烟，不构成校准。

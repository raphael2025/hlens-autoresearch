# research/synthetic_lab

Phase 9 Synthetic Market Lab（[ADR-0042](../../docs/adr/0042-synthetic-market-provider.md)）。状态：FRAMEWORK_IMPLEMENTED / NOT_VALIDATED。
用已知真值的合成市场（`plugins/synthetic` 的 `RandomWalkMarket`）检验**方法本身**：纯噪声上的假阳性率、植入效应的检出力。
合成结果**不得**支持真实市场结论（roadmap P9）。

| 模块 | 内容 |
|---|---|
| `calibration.py` | `calibrate`：任意 `market -> bool` 检测器的假阳性率与检出力（最小框架） |
| `gate_calibration.py` | 验证门校准 harness：候选 Profile × 门的假阳性率、检出力、INCONCLUSIVE 率、封存 OOS 消耗率；`StrategyValidatorDetector`（完整 G0 → G4）；CLI |
| `intervals.py` | 精确二项比率与 Clopper-Pearson 区间（有理数运算，`Decimal` 输出） |

## 证据，不是决定

**evidence only — not a Profile decision.** Validation Profile 数值（D-09 TBD-1..5）由 Raphael 在校准后冻结
（[07-validation.md §2.4](../../docs/architecture/07-validation.md)）。harness：

- 只比较调用方给出的候选 Profile，不排名、不推荐、没有默认 Profile，也不写入任何 Profile；
- 报告确定（固定种子）、`report_hash` 覆盖全部载荷，记录生成器、检测器、基础规格、种子、植入效应、候选 Profile 引用与内容哈希、区间方法与 `alpha`；
- 每个门的比率以该组全部运行为分母：流水线在该门之前已停止的运行记为 `not_evaluated`，不算通过；
- 封存 OOS 消耗：G0 – G4 PASS 才能进入一次性、计预算的 G5；每个 PASS 在每个候选各自的内存账本里开封一次（G5 本身不在此运行）。

## 用法

```python
from research.synthetic_lab.gate_calibration import GateCalibrationSetup, run_gate_calibration

report = run_gate_calibration(
    setup
)  # setup: 候选 Profile、种子、植入效应、alpha、检测器，全部由调用方给出
report.candidate(
    profile
).false_positive_rate  # BinomialRate：count / n / rate / Clopper-Pearson 区间
report.candidate(profile).power(effect)
report.to_payload()  # JSON 就绪；report.report_hash
```

CLI（写入 `<out>/gate_calibration/<report_hash>.json`，经 `research/reports`，append-only）：

```bash
python -m research.synthetic_lab.gate_calibration --setup package.module:factory --out var/reports
```

测试（`tests/research/synthetic_lab/`）使用的 Profile 都是 **TEST ONLY** 的极端候选，不是提案。

## 未完成（调试批次）

检测器异常直接抛出；未运行 G5；`apps/api` 的 `ReportStore` 尚未提供 `gate_calibration` 类型；生成器过于简单
（高斯噪声 + 线性自相关），测试的种子数与市场长度只够冒烟。

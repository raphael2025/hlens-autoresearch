# research/synthetic_lab

Phase 9 Synthetic Market Lab（[ADR-0042](../../docs/adr/0042-synthetic-market-provider.md)）。状态：FRAMEWORK_IMPLEMENTED / NOT_VALIDATED。
用已知真值的合成市场（`plugins/synthetic` 的 `RandomWalkMarket`）检验**方法本身**：纯噪声上的假阳性率、植入效应的检出力。
合成结果**不得**支持真实市场结论（roadmap P9）。

| 模块 | 内容 |
|---|---|
| `calibration.py` | `calibrate`：任意 `market -> bool` 检测器的假阳性率与检出力（最小框架） |
| `gate_calibration.py` | 验证门校准 harness：候选 Profile × 门的假阳性率、检出力、INCONCLUSIVE 率、封存 OOS 消耗率；可选 G5 模式（端到端 G0 – G5 率）；`StrategyValidatorDetector`（完整 G0 → G4，配 `sealed_inputs_for` 时含 G5）；CLI |
| `intervals.py` | 精确二项比率与 Clopper-Pearson 区间（有理数运算，`Decimal` 输出） |

## 证据，不是决定

**evidence only — not a Profile decision.** Validation Profile 数值（D-09 TBD-1..5）由 Raphael 在校准后冻结
（[07-validation.md §2.4](../../docs/architecture/07-validation.md)）。harness：

- 只比较调用方给出的候选 Profile，不排名、不推荐、没有默认 Profile，也不写入任何 Profile；
- 报告确定（固定种子）、`report_hash` 覆盖全部载荷，记录生成器、检测器、基础规格、种子、植入效应、候选 Profile 引用与内容哈希、区间方法与 `alpha`；
- 每个门的比率以该组全部运行为分母：流水线在该门之前已停止的运行记为 `not_evaluated`，不算通过；
- 封存 OOS 消耗：G0 – G4 PASS 才能进入一次性、计预算的 G5；每个 PASS 在每个候选各自的内存账本里开封一次（默认不运行 G5 本身；见下方 G5 模式）。

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

~~检测器异常直接抛出~~ ✅ 已修（2026-09-26，CODE_COMPLETE / DEBUG_PENDING）：`detect` 抛出的异常是被校准方法在该市场上的失败，
记为无门的 `INCONCLUSIVE` 运行（各门 `not_evaluated`），`detector_error` 记录异常类型与消息（折叠空白、截断 200 字符），每组报告
`detector_errors` 计数，永不计为通过、不消耗封存 OOS；两个新键只在出现错误时写入，无错误报告的哈希不变。harness 自身配置错误
（`DetectorConfigurationError`、报告不在该候选 Profile 下、市场真值不是植入效应）仍直接抛出。`calibrate` 的检测器异常计入
`noise_errors` / `planted_errors`，不算检出，仍在 `trials` 分母内。测试：`tests/research/synthetic_lab/test_gate_calibration.py`、
`test_calibration.py`。

~~未运行 G5~~ ✅ 可选 G5 模式（2026-09-26，CODE_COMPLETE / DEBUG_PENDING）：`GateCalibrationSetup.sealed_oos_g5`
显式开启（默认 `False`；关闭时所有报告与 `report_hash` 与之前逐字节一致，测试钉住了改动前的哈希）。检测器须实现
`detect_sealed(market, profile, sealed: SealedRelease) -> ValidationReport`（`SealedGateDetector`）；没有它的检测器
（如只有 `detect` 的玩具检测器、未配 `sealed_inputs_for` 的 `StrategyValidatorDetector`）在构造 setup 时即被**拒绝**
（`ValueError`），不静默报告为 not_evaluated；`base` 规格须覆盖每个候选的整个封存窗口（只凭规格判断，不读数据）。
开启后：生成的市场按每个候选的封存窗口切分，`detect` / `detect_sealed` 只拿到研究视图（窗口起点之前结束的 bar），
封存 bar 扣留在 `SealedRelease` 里；只有 G0 – G4 PASS 才开封本运行的模拟族（`gate_calibration:<arm>:<seed>`）并
`claim_evaluation`，之后 `release()` 才交出封存 bar（与 research loop 的 G5 同一纪律，R21 / R27）。claim 之后评估即被消耗：
检测器提前结束 → `G5.oos_evaluation = consumed_without_result:<reason>`（`INCONCLUSIVE`）；抛异常 → 无门的 `INCONCLUSIVE`、
`consumed_without_result`、`detector_error`；二者都不算通过。PASS 却未经 claim 的评估取标签、或报告里没有 G5 门 →
`DetectorConfigurationError`。`StrategyValidatorDetector.detect_sealed` 在研究 + 已释放封存 bar 上重跑候选（`sealed_inputs_for`）、
为封存窗口内非空仓目标打标签，并在 claim 上运行 `research.validation.run_sealed_oos`。报告（仅开启时新增的键）：每组
`sealed_oos_g5`（到达 G5 的数量；以到达数为分母的 G5 通过 / INCONCLUSIVE / 失败率，Clopper-Pearson；`consumed_without_result`
与 `detector_errors` 计数；`end_to_end_g0_g5`：以该组全部运行为分母的 G0 – G5 通过率，噪声组即假阳性率、植入组即检出力），
每个运行的 `sealed_oos_g5` 记录，逐门统计包含 G5 门。仍只是证据，不排名、不推荐。测试：`test_gate_calibration_g5.py`。

仍未完成：生成器过于简单（高斯噪声 + 线性自相关），测试的种子数与市场长度只够冒烟。

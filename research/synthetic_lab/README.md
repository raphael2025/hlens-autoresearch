# research/synthetic_lab

Phase 9 Synthetic Market Lab（[ADR-0042](../../docs/adr/0042-synthetic-market-provider.md)）。状态：FRAMEWORK_IMPLEMENTED / NOT_VALIDATED。
用已知真值的合成市场（`plugins/synthetic` 的 `RandomWalkMarket`）检验**方法本身**：纯噪声上的假阳性率、植入效应的检出力。
合成结果**不得**支持真实市场结论（roadmap P9）。

| 模块 | 内容 |
|---|---|
| `calibration.py` | `calibrate`：任意 `market -> bool` 检测器的假阳性率与检出力（最小框架） |
| `gate_calibration.py` | 验证门校准 harness：候选 Profile × 门的假阳性率、检出力、INCONCLUSIVE 率、封存 OOS 消耗率；可选 G5 模式（端到端 G0 – G5 率）；可选多标的模式（`MultiInstrumentCalibrationSetup`）；`StrategyValidatorDetector`（完整 G0 → G4，配 `sealed_inputs_for` 时含 G5）、`MultiInstrumentValidatorDetector`；CLI |
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

配置错误不再被吞（审计修复，2026-09-26，CODE_COMPLETE / DEBUG_PENDING）：此前任何检测器异常都记为 `detector_error`，
连验证管线**有意**抛出的配置错误也被吞掉——候选 Profile 缺少验证器需要的字段时，所有运行变成 `INCONCLUSIVE`，噪声假阳性率
显示为 0/n，是乐观且误导的证据。现按管线自身的分类（`research.validation.g4` 的 Check isolation，`_PROPAGATED`）处理：
`calibration.PROPAGATED_ERRORS = (ValueError, TypeError, MemoryError)` 原样抛出（附带 arm / seed / 候选的 note），
覆盖 `calibrate`、单标的、多标的与 G5 的 `detect_sealed`。`ValueError` 包括 `ProfileFieldMissing`、`UnsupportedMethod`、
`ExplicitParamRefused`、`DetectorConfigurationError` 及其他有意的输入拒绝；`TypeError` 同为调用方契约错误；`MemoryError`
不可复现，记录下来会使结果依赖机器。其余异常（算术、查找、属性、运行时、断言……）仍是方法在该市场上的失败，照旧记为
`detector_error`。不确定性如实报告：某组 `detector_errors > 0` 时，组载荷加 `pass_rate_bounds`：
`[passed/n, (passed+errors)/n]`（Decimal 字符串，下界向下、上界向上取整到 `intervals.PLACES`），即全部出错运行都失败 / 都通过时的
通过率范围；无错误时不写该键，无错误报告的哈希不变（带错误的 `PRE_G5_RAISING_HASH` 因新增该键重新钉住）。`CalibrationReport`
相应提供 `false_positive_rate_bounds` / `power_bounds` 属性。

B45 向外舍入修复（2026-09-26，CODE_COMPLETE / DEBUG_PENDING）：上述两个属性的端点此前用 `Decimal(n) / trials`，
依赖环境 context 的 `ROUND_HALF_EVEN`，重复小数会使下界高于、上界低于精确比率（如 2/3 的下界、1/3 的上界）。现在在局部
`decimal.Context(prec=28)` 中计算：下界 `ROUND_FLOOR`、上界 `ROUND_CEILING`，不受调用方环境 context 影响，区间总包含精确比率。
公开 API 不变（`tuple[Decimal, Decimal]`）；28 位有效数字可精确表示的比率（0、1、1/4、1/128 等整除）保持精确，无检测器错误时
两端点为同一点值；重复小数且无错误时为最紧的 28 位包络。不使用 `intervals.PLACES` 的 6 位量化。后续静态复核发现点估计
`false_positive_rate` / `power` 仍直接使用调用方 Decimal context；现以固定 28 位、`ROUND_HALF_EVEN` 局部 context 计算，使点估计
也不随调用方 context 改变。默认 Decimal context 下结果保持不变。测试（`test_calibration.py`）以 `Fraction` 精确比率核对 FPR 与
power 两组端点；环境 context 独立性留到测试 / 验收阶段运行。

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
每个运行的 `sealed_oos_g5` 记录，逐门统计包含 G5 门。某组 G5 `detector_errors > 0` 时，其 `sealed_oos_g5` 块另加
`pass_rate_bounds`：`[passed/reached, (passed+errors)/reached]`（同样向外取整到 `intervals.PLACES`）；无 G5 错误时不写该键，
已有报告哈希不变（没有钉住的报告含 G5 检测器错误）。该组任一运行出错（G0 – G4 的 `detect` 或 G5 的 `detect_sealed`）时，另加
`end_to_end_bounds`：`[end_to_end/n, (end_to_end+出错运行数)/n]`（以全部运行为分母，同样向外取整；无错误时不写该键）。`detect_sealed` 的配置错误与 `calibrate()` 同一分类：`PROPAGATED_ERRORS`
原样抛出（附注运行族与候选），其余异常记为 `detector_error`。仍只是证据，不排名、不推荐。测试：`test_gate_calibration_g5.py`。

~~多标的负对照假阳性率未校准~~ ✅ 可选多标的模式（2026-09-26，CODE_COMPLETE / DEBUG_PENDING；无 core / 契约 / Schema 变更）：
Phase 8 的多标的验证（`ValidatorSetup.instruments`、`research/validation/instruments.py`）中，池化的 G1 负对照
（`G1.shuffle_control` / `G1.shift_control`）按时间混合标的，完全植入的标的对上偶见失败，假警报率此前未校准（计划 B20 风险）。
新增独立的 `MultiInstrumentCalibrationSetup` + `run_multi_instrument_calibration`（显式开启；`GateCalibrationSetup` 的报告与
`report_hash` 完全不变，测试钉住了改动前的哈希）。每次运行生成 k ≥ 2 个独立合成标的（`base` 只改 `symbol` / `seed` / `effects`；
符号互不相同、不含 `|`；各标的生成种子由运行种子确定性导出：`instrument_seed(run_seed, index, symbol)`，规则记录在报告输入中），
经 `MultiInstrumentGateDetector.detect_instruments` 走多标的路径一起验证（`MultiInstrumentValidatorDetector`：完整流水线，setup
必须恰好验证本批标的；报告走了单标的路径 → `DetectorConfigurationError`）。组（`MultiInstrumentArm`）由调用方逐个声明：
`kind`（`all_noise` / `all_planted` / `mixed`）+ 每个符号一个 `PlantedEffect | None` + 种子，无任何默认；`kind` 与效应不符、
k < 2、符号重复、组为空或重名、效应条目数与符号数不符、检测器没有 `detect_instruments` 均拒绝（`ValueError`）。
报告（仅此模式新增的键）：每组 `kind`、流水线 `fail_rate`、每个标的自己的判定率（`instruments`：角色、通过 / INCONCLUSIVE /
失败率、池化阶段先失败时的 `not_evaluated`）；每个门（含池化 `G1.shuffle_control` / `G1.shift_control` 及各
`<gate>.instrument.<symbol>` 子门）增加 `fail_rate`；通过率按组类型命名（`false_positive_rate` / `power` / 混合组 `pass_rate`），
逐标的子门按该标的角色命名；每个运行记录各标的种子、市场哈希与自身判定。检测器异常仍记为无门的 INCONCLUSIVE；池化 PASS 同样
消耗该运行族的开封。此模式不提供 G5。仍只是证据：不设阈值、不选 Profile、不改任何门。测试：`test_gate_calibration_multi.py`。

冒烟规模证据（**不是校准结果**；TEST ONLY 宽松 Profile `test_only_lax_uncalibrated` + TEST ONLY `MULTI_TEST_ONLY_PARAMS`，
k = 2，每组 8 个种子，`gate_fixtures.multi_setup(8)`，报告哈希 `d7b5df2d…612b75`；区间为 Clopper-Pearson 95%）：

| 组 | 池化 `G1.shuffle_control` FAIL | 池化 `G1.shift_control` FAIL | 逐标的 G1 子门 FAIL（S0 / S1，shuffle · shift） | 流水线 PASS |
|---|---|---|---|---|
| all_noise | 1/8 [0.003, 0.527] | 0/8 [0, 0.369] | 未评估（8/8 池化阶段先失败） | 0/8 |
| all_planted（强度 0.5） | 0/8 [0, 0.369] | 1/8 [0.003, 0.527] | S0 1 · 1，S1 0 · 1（各 1 次未评估） | 2/8 |
| mixed（S0 植入、S1 噪声） | 0/8 [0, 0.369] | 2/8 [0.032, 0.651] | S0 1 · 1，S1 0 · 0（各 6 次未评估） | 0/8 |

（该表在 ADR-0060 于实验室强制之前计算：当时 Profile 的 `market_benchmark_rule` 为占位名 `"test-only"`、检测器未开启市场基准；
强制之后同一设置的报告哈希不同，表中的门统计未重新计算。）

8 个种子只能说明：池化对照在全植入与混合标的对上确实会失败（与 B20 观察一致），区间宽到不能区分任何名义水平；
不得据此选择阈值或 Profile。

**C-T4 市场基准（ADR-0060 在实验室强制，2026-09-26，CODE_COMPLETE / DEBUG_PENDING；无 core / 契约 / Schema 变更）**：
`StrategyValidatorDetector` 与 `MultiInstrumentValidatorDetector` 拒绝（`DetectorConfigurationError`）没有
`ValidatorSetup.market_benchmark=True` 的 setup，使被校准的流水线就是研究循环运行的流水线：每个报告都带候选 Profile 的
`benchmark` 块所要求的报告项（`G2.market_benchmark.<规则>` / `G2.inverse_control`，只报告、无阈值），未登记的规则名 →
`G2.market_benchmark` INCONCLUSIVE（与其他 INCONCLUSIVE 门一样使该运行 INCONCLUSIVE）。基准重跑用 setup 声明的回测器或
`BarBacktester()`，检测器的 `backtester` 与之不同 → `benchmark_unavailable` INCONCLUSIVE。TEST ONLY 夹具 Profile 使用
`buy_and_hold_equal_weight` + `inverse_control_reported=True`；玩具检测器不运行验证器，其 Profile 保留原基准块（报告哈希与
控制台夹具不变）。测试：`test_gate_calibration.py`、`test_gate_calibration_multi.py`。

## 中等规模证据（2026-09-26；**evidence only — not a Profile decision**；TEST ONLY Profile）

报告、设置、再生成命令与完整表格：[docs/research/calibration/](../../docs/research/calibration/README.md)；
驱动（setup 工厂）：`tests/research/synthetic_lab/evidence_setups.py`（放在 `tests/` 下，研究平面不导入 TEST ONLY Profile）。
候选只有 TEST ONLY `test_only_lax_uncalibrated` / `test_only_strict_uncalibrated`；95% Clopper-Pearson 区间。

(a) 单标的、G5 开启、每组 250 个种子（每市场 4320 个 1 分钟 bar；墙钟 15:28）：

| Profile | 噪声组假阳性率（G0 – G4） | 检出力 强度 0.2 | 检出力 强度 0.5 | INCONCLUSIVE（各组） | 端到端 G0 – G5：噪声 / 0.2 / 0.5 |
|---|---|---|---|---|---|
| lax | 1/250 = 0.004 [0.000, 0.022] | 13/250 = 0.052 [0.028, 0.087] | 125/250 = 0.500 [0.436, 0.564] | 0/250 [0, 0.015] | 0/250 · 10/250 · 119/250 |
| strict | 0/250 [0, 0.015] | 0/250 [0, 0.015] | 0/250 [0, 0.015] | 0/250 [0, 0.015] | 0 · 0 · 0 |

(b) 多标的 k = 2、每组 200 个种子（每标的 2880 bar；墙钟 19:19）：lax 流水线 PASS all_noise 0/200 [0, 0.018]、
all_planted 51/200 = 0.255 [0.196, 0.321]、mixed 0/200 [0, 0.018]；strict 全部 0/200；INCONCLUSIVE 全部 0/200。
池化 G1 负对照 FAIL（lax）：shuffle all_noise 12/200 = 0.060 [0.031, 0.102]、all_planted 18/200 = 0.090 [0.054, 0.139]、
mixed 12/200 = 0.060 [0.031, 0.102]；shift 9/200 = 0.045 [0.021, 0.084]、20/200 = 0.100 [0.062, 0.150]、
16/200 = 0.080 [0.046, 0.127]。strict 的池化对照全部 0/200（对照与 G3 共用 `multiple_testing_threshold`）。

这些数字只描述两个 TEST ONLY 夹具在过于简单的生成器上的行为；不选择任何阈值或 Profile 数值（D-09 仍未冻结）。

仍未完成：生成器过于简单（高斯噪声 + 线性自相关）；市场长度只有两天研究窗（+ 一天封存窗）。

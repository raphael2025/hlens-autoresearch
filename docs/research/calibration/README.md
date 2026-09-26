# docs/research/calibration — 验证门校准证据（Phase 9）

> **evidence only — not a Profile decision.** 本目录只保存合成市场上的**证据**。候选 Profile 全部是
> **TEST ONLY** 的极端夹具（`test_only_lax_uncalibrated` / `test_only_strict_uncalibrated`，定义见
> [`gate_fixtures.py`](../../../tests/research/synthetic_lab/gate_fixtures.py)），不是提案、不是校准结果。
> 本目录**不选择任何阈值、任何 Profile 数值**；Validation Profile 数值（D-09 TBD-1..5）仍未冻结，由 Raphael 决定
> （ADR-0042 / ADR-0007，[07-validation.md §2.4](../../architecture/07-validation.md)）。合成结果不得支持真实市场结论。

## 文件

| 文件 | setup 工厂 | `report_hash` |
|---|---|---|
| `single_instrument_evidence.json` | `tests.research.synthetic_lab.evidence_setups:single_instrument_evidence` | `bada61369ed52c29eaaba1efffbf610f768fb325ee3a56e3f74fa56a33de4841` |
| `multi_instrument_evidence.json` | `tests.research.synthetic_lab.evidence_setups:multi_instrument_evidence` | `0f04d1b649d7ce6357e47d84e6ae709d343bfcbb44271398a34cb2aa83f2cecd` |

报告由 `research.synthetic_lab.gate_calibration` 的现有 CLI 入口（`main`）经 `research/reports` 写出
（`<out>/gate_calibration/<report_hash>.json`，规范 JSON），逐字节复制到此处并按工厂命名。
`tests/research/synthetic_lab/test_evidence_setups.py` 钉住两个 setup 的 `inputs_payload` 哈希，并检查这里的报告可解析、
`report_hash` 等于内容哈希、`inputs` 等于 setup 的 `inputs_payload`。完整再生成不是测试（见下）。

## 设置（全部显式，驱动模块 [`evidence_setups.py`](../../../tests/research/synthetic_lab/evidence_setups.py)）

共同：检测器是完整验证流水线（`PipelineBacktestValidator`，G0 → G4；ADR-0060 市场基准开启），候选策略为库中第一条
TSMOM（`lookback=60`），逐小时决策；市场为 `RandomWalkMarket`（高斯噪声 + 线性滞后自相关植入），1 分钟 bar，
起点 2024-01-01T00:00Z，初始价 100，波动率 0.001；区间为 Clopper-Pearson，`alpha = 0.05`（TEST ONLY 报告参数）。
驱动模块放在 `tests/` 下，使研究平面模块不导入 TEST ONLY Profile。

| setup | 模式 | 每次运行的市场 | 组 × 种子 | 候选 | 运行数（× 候选） |
|---|---|---|---|---|---|
| (a) 单标的 | G5 开启 | 1 个标的，4320 bar（研究窗 2880 bar 到 2024-01-03 + 封存窗 1440 bar） | `noise` 种子 10000..10249；`planted_lag60_strength0.5`、`planted_lag60_strength0.2` 种子 20000..20249 | lax、strict | 750 × 2 |
| (b) 多标的 | k = 2（`S0-USDT`、`S1-USDT`），无 G5 | 每标的 2880 bar（研究窗） | `all_noise` 30000..30199；`all_planted`（两标的均为强度 0.5）40000..40199；`mixed`（S0 强度 0.5、S1 噪声）50000..50199 | lax、strict | 600 × 2 |

多标的模式中各标的的生成种子由运行种子确定性导出（`instrument_seed`，规则记录在报告 `inputs` 中）。

**规模的选择**：每组 250（单标的）/ 200（多标的）个种子，是在约 20 分钟、6 GB 内存上限内能完成的最大整数规模
（4 种子计时探针：单标的约 2.3 s / 种子，多标的约 3.9 s / 种子；12 个单种子运行实测约 2.7 s / 种子，线性）。
更早的 800 种子与 400 种子单标的运行超出时间预算，被停止，没有产生报告。实测墙钟时间：(a) 15:28，
(b) 19:19（WSL2，单进程，`systemd-run MemoryMax`，峰值 RSS 约 210 MB / 225 MB）。

## 实测比率（evidence only；TEST ONLY Profile；95% Clopper-Pearson 区间）

分母为该组全部运行；流水线在某门之前已停止的运行不算通过。噪声组的 PASS 率即假阳性率，植入组即检出力。

### (a) 单标的，G0 – G5

| Profile（TEST ONLY） | 组 | G0 – G4 PASS | INCONCLUSIVE | FAIL | 到达 G5 | G5 PASS · INC · FAIL（以到达数为分母） | 端到端 G0 – G5 PASS |
|---|---|---|---|---|---|---|---|
| lax | noise（假阳性率） | 1/250 = 0.004 [0.000, 0.022] | 0/250 [0.000, 0.015] | 249/250 | 1 | 0 · 0 · 1 | 0/250 = 0.000 [0.000, 0.015] |
| lax | 强度 0.2（检出力） | 13/250 = 0.052 [0.028, 0.087] | 0/250 [0.000, 0.015] | 237/250 | 13 | 10 · 0 · 3 | 10/250 = 0.040 [0.019, 0.072] |
| lax | 强度 0.5（检出力） | 125/250 = 0.500 [0.436, 0.564] | 0/250 [0.000, 0.015] | 125/250 | 125 | 119 · 0 · 6 | 119/250 = 0.476 [0.413, 0.540] |
| strict | noise（假阳性率） | 0/250 = 0.000 [0.000, 0.015] | 0/250 [0.000, 0.015] | 250/250 | 0 | — | 0/250 = 0.000 [0.000, 0.015] |
| strict | 强度 0.2（检出力） | 0/250 = 0.000 [0.000, 0.015] | 0/250 [0.000, 0.015] | 250/250 | 0 | — | 0/250 = 0.000 [0.000, 0.015] |
| strict | 强度 0.5（检出力） | 0/250 = 0.000 [0.000, 0.015] | 0/250 [0.000, 0.015] | 250/250 | 0 | — | 0/250 = 0.000 [0.000, 0.015] |

封存 OOS 消耗率等于 G0 – G4 PASS 率（每个 PASS 开封一次）；没有检测器错误，没有 `consumed_without_result`。
strict 的每个运行都有 `G2.effective_sample_size` INCONCLUSIVE（同一运行中另有 FAIL 门，故判定为 FAIL）；最常见的
失败门：lax 噪声组 `G2.cost_stress.0`（121）/ `G2.breakeven_cost_multiple`（117）/ `G3.adjusted_p_value`（102），
lax 植入组 `G3.adjusted_p_value`（强度 0.2：166，强度 0.5：92），strict 各组 `G2.null_model_percentile`（241 / 214 / 108）。

### (b) 多标的 k = 2（流水线判定）

| Profile（TEST ONLY） | 组 | 流水线 PASS | INCONCLUSIVE | FAIL |
|---|---|---|---|---|
| lax | all_noise（假阳性率） | 0/200 = 0.000 [0.000, 0.018] | 0/200 [0.000, 0.018] | 200/200 |
| lax | all_planted（检出力） | 51/200 = 0.255 [0.196, 0.321] | 0/200 [0.000, 0.018] | 149/200 |
| lax | mixed（通过率） | 0/200 = 0.000 [0.000, 0.018] | 0/200 [0.000, 0.018] | 200/200 |
| strict | all_noise / all_planted / mixed | 各 0/200 = 0.000 [0.000, 0.018] | 各 0/200 [0.000, 0.018] | 各 200/200 |

### (b) 池化 G1 负对照（B20 风险：池化对照按时间混合标的）

门 FAIL = 负对照报警。括号内为逐标的 G1 子门 FAIL 次数（shuffle · shift）与该子门未评估的运行数。

| Profile（TEST ONLY） | 组 | 池化 `G1.shuffle_control` FAIL | 池化 `G1.shift_control` FAIL | 逐标的子门（S0；S1） |
|---|---|---|---|---|
| lax | all_noise | 12/200 = 0.060 [0.031, 0.102] | 9/200 = 0.045 [0.021, 0.084] | S0 0 · 0；S1 0 · 0（各 198 未评估） |
| lax | all_planted | 18/200 = 0.090 [0.054, 0.139] | 20/200 = 0.100 [0.062, 0.150] | S0 1 · 5；S1 8 · 6（各 58 未评估） |
| lax | mixed | 12/200 = 0.060 [0.031, 0.102] | 16/200 = 0.080 [0.046, 0.127] | S0 3 · 0；S1 2 · 4（各 152 未评估） |
| strict | all_noise | 0/200 = 0.000 [0.000, 0.018] | 0/200 = 0.000 [0.000, 0.018] | 0 · 0；0 · 0（各 200 未评估） |
| strict | all_planted | 0/200 = 0.000 [0.000, 0.018] | 0/200 = 0.000 [0.000, 0.018] | 0 · 0；1 · 0（各 123 未评估） |
| strict | mixed | 0/200 = 0.000 [0.000, 0.018] | 0/200 = 0.000 [0.000, 0.018] | 0 · 0；0 · 0（各 196 未评估） |

池化对照在所有运行上都被评估（未评估 0）。两个 TEST ONLY Profile 的对照结果不同，是因为流水线用同一个
`multiple_testing_threshold` 既判 G3 也判 G1 对照（见 `gate_fixtures.py` 注释），不是对照本身的性质。

**这些数字只描述两个 TEST ONLY 夹具在一个过于简单的生成器上的行为**：不据此选择阈值、Profile 或名义水平，
也不据此判断任何真实策略。

## 再生成（手动，逐字节一致）

一次只跑一个，内存上限；在仓库根目录、`uv sync --offline --frozen` 之后：

```bash
systemd-run --user --scope --quiet -p MemoryMax=6G -p MemorySwapMax=0 \
  uv run --offline python -c "import sys; from research.synthetic_lab.gate_calibration import main; sys.exit(main(sys.argv[1:]))" \
  --setup tests.research.synthetic_lab.evidence_setups:single_instrument_evidence --out /tmp/calib
cmp /tmp/calib/gate_calibration/bada61369ed52c29eaaba1efffbf610f768fb325ee3a56e3f74fa56a33de4841.json \
  docs/research/calibration/single_instrument_evidence.json
```

多标的同理（工厂 `multi_instrument_evidence`，文件名为其 `report_hash`）。`python -m research.synthetic_lab.gate_calibration`
目前**不能**加载任何工厂：包 `__init__` 已导入该模块，`-m` 以 `__main__` 重新执行它，得到不同的 setup 类，`isinstance`
检查拒绝一切 setup（现有 CLI 测试直接调用 `main`，未覆盖此路径）；因此上面直接调用 `main`。
缩减种子数的再生成不会得到相同字节，故不作为测试。

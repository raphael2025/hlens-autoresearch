# research/validation

最小 Validation Pipeline（Phase 4，[ADR-0037](../../docs/adr/0037-outcome-engine-and-minimal-validation-pipeline.md)）。
规则来源为 docs/research/constitution.md；**不得为提高结果而修改**（CLAUDE.md H2/H3）。研究代码，不是生产代码（H5）。

> 状态：**FRAMEWORK_IMPLEMENTED / NOT_VALIDATED**。所有数值阈值取自传入的 ValidationProfile，本包**没有任何默认阈值**；
> Profile 数值仍为 TBD（两步冻结 Step 2 未进行）。测试只使用明确标注 TEST ONLY 的 Profile（`tests/research/validation/fixtures.py`）。

| 模块 | 内容 |
|---|---|
| `pipeline.py` | `run_in_sample`（G0 → G1 → G2 → G3，阶段 FAIL 即停）、`run_sealed_oos`（G5）、`build_report`（判定 = `derive_verdict`）、`failure_record` |
| `gates.py` | `threshold(profile, path)`（值 + 来源路径）、`compare_gate`（比较方向写入 metric，`inconclusive_bands` 以 `gate_id` 为键）、`flag_gate`、`inconclusive_gate` |
| `splits.py` | `purge_and_embargo`、`walk_forward_folds`（Profile 窗口与 embargo）、`purged_k_fold`、`research_spans` |
| `controls.py` | `SignalStudy` Protocol；shuffle / shift 负对照（C-L6） |
| `sealed_oos.py` | `SealedOosVault`（固定日期窗口，开封前锁定，每族只开封一次）、`UnsealingLedger` Protocol + 内存实现 |
| `costs.py` | 成本模型 v1 的应用：净收益、盈亏平衡成本倍数、Profile 绑定检查 |
| `stats.py` | 有效独立样本、HAC t 检验、多重检验校正（`bonferroni` / `sidak`；其它方法拒绝） |
| `calibration.py` | 空模型校准报告框架：`RandomWalkMarket` 上的假阳性率 / 检出率（`FRAMEWORK_ONLY_NOT_CALIBRATED`，不提出任何数值） |

门清单、阈值来源与已知缺口见 ADR-0037 与 docs/architecture/07-validation.md §2.2。

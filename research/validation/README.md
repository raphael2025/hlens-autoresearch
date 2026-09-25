# research/validation

Validation Pipeline：最小流水线 G0 – G3 + G5（Phase 4，[ADR-0037](../../docs/adr/0037-outcome-engine-and-minimal-validation-pipeline.md)）
与 G4 稳健性套件、回溯审计、报告视图（Phase 8，[ADR-0041](../../docs/adr/0041-validation-robustness.md)）。
规则来源为 docs/research/constitution.md；**不得为提高结果而修改**（CLAUDE.md H2/H3）。研究代码，不是生产代码（H5）。

> 状态：**FRAMEWORK_IMPLEMENTED / NOT_VALIDATED**。所有数值阈值取自传入的 ValidationProfile；Profile 契约没有字段的规则
> （容量、冲击系数、状态 P&L 集中度、跨资产一致性、CSCV 分块数、Sealed OOS 开封预算）只接受显式参数（来源记为 `param:<name>`），不传则门为
> `INCONCLUSIVE`（`profile_field_missing:<name>`）——本包**没有任何默认阈值**。C-R1 ~ C-R5 检查的配置为空或被关闭时
> 同样判 `INCONCLUSIVE`（`configuration_missing:<what>`），从不静默跳过；计算出一个估计值本身永远不算通过（ADR-0041 复审修正）。Profile 数值仍为 TBD（两步冻结 Step 2 未进行）。
> 测试只使用明确标注 TEST ONLY 的 Profile / 参数（`tests/research/validation/fixtures.py`、`robustness_fixtures.py`）。

| 模块 | 内容 |
|---|---|
| `pipeline.py` | `run_in_sample`（G0 → G1 → G2 → G3，阶段 FAIL 即停；G2 / G3 只在 walk-forward 测试折上计算）、`run_sealed_oos`（G5，一次性）、`build_report`（判定 = `derive_verdict`；`ValidationContext.created_at` 给定时以它为报告时间戳——`created_at` 计入报告内容哈希，不给则为墙钟时间、重跑哈希不同）、`failure_record`、`reason_for_gate` |
| `g4.py` | `run_robustness`（G4 全部检查）、`run_validation`（G0 – G3 后接 G4，G4 输入可惰性构造；缺输入 = `G4.robustness_input` INCONCLUSIVE）、`RobustnessParams`（无 Profile 字段规则的显式参数，全部必填） |
| `robustness.py` | C-R1 ~ C-R5 与 C-T1 过拟合概率的检查：过拟合、参数邻域、时间对齐、延迟压力、成本压力、walk-forward 窗口统计、状态分解、容量、跨资产；每个返回 `RobustnessCheck`（门 + 阈值来源 + 缺失字段 + 表格） |
| `overfitting.py` | PBO（CSCV，分块间按 `data_split.embargo` purge / embargo）、Deflated Sharpe、逐期 Sharpe（不含阈值） |
| `returns.py` | `PeriodReturns`（毛收益 + 成本，净收益按成本倍数计算）、`from_backtest`（`BacktestResult` → 逐期收益）、`TrialReturns` |
| `retro_audit.py` | 回溯审计：以现行规则重跑并报告逐门差异；已拒绝对象永不被翻转为通过（构造时强制） |
| `report.py` | `report_view` / `to_json`：规范 JSON 报告视图（供日后 apps/web 可视化）；`promotion` 块：没有 G5 结果的 PASS 标为不可晋升（`sealed_oos_not_evaluated`） |
| `gates.py` | `threshold(profile, path)`（值 + 来源路径）、`explicit_threshold`（`param:`）、`compare_gate`、`flag_gate`、`inconclusive_gate`、`missing_field_gate`、`configuration_missing_gate`、`ProfileFieldMissing` |
| `splits.py` | `purge_and_embargo`、`walk_forward_folds` / `walk_forward_windows`（Profile 窗口与 embargo）、`non_overlapping_windows`（G4 窗口统计只计不重叠的测试窗口）、`purged_k_fold`（排除封存区）、`research_spans` |
| `controls.py` | `SignalStudy` / `FittableStudy` Protocol；`blind_labels`（流水线使用的侧向一律由盲化标签计算）；shuffle / shift 负对照（C-L6） |
| `sealed_oos.py` | `SealedOosVault`（固定日期窗口，开封前锁定，每族只开封一次，全局预算 `max_unsealings` 为必填显式参数，每次开封只可评估一次）、`UnsealingLedger` Protocol + 内存实现 |
| `costs.py` | 成本模型 v1 的应用：净收益、盈亏平衡成本倍数、Profile 绑定检查 |
| `stats.py` | 有效独立样本（重叠区间连通分量数）、HAC t 检验、多重检验校正（`bonferroni` / `sidak`；其它方法拒绝） |
| `calibration.py` | 空模型校准报告框架：`RandomWalkMarket` 上的假阳性率 / 检出率（`FRAMEWORK_ONLY_NOT_CALIBRATED`，不提出任何数值） |

门清单、阈值来源与已知缺口见 ADR-0037、ADR-0041 与 docs/architecture/07-validation.md §2.2 / §2.3。
策略回测的接线（`PipelineBacktestValidator`）在 `research/strategies/validation.py`。

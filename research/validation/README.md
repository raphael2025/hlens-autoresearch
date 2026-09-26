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
| `pipeline.py` | `run_in_sample`（G0 → G1 → G2 → G3，阶段 FAIL 即停；G2 / G3 只在 walk-forward 测试折上计算）、`run_sealed_oos`（G5，一次性；可接收已消耗的 `SealedEvaluation`）、`sealed_oos_without_result`（评估已消耗却无统计量 → `G5.oos_evaluation` = `consumed_without_result:<原因>`，INCONCLUSIVE）、`build_report`（判定 = `derive_verdict`；`ValidationContext.created_at` 给定时以它为报告时间戳——`created_at` 计入报告内容哈希，不给则为墙钟时间、重跑哈希不同）、`failure_record`、`reason_for_gate` |
| `g4.py` | `run_robustness`（G4 全部检查）、`run_validation`（G0 – G3 后接 G4，G4 输入可惰性构造；缺输入 = `G4.robustness_input` INCONCLUSIVE；其尾段即 `robustness_stage`）、`RobustnessParams`（无 Profile 字段规则的显式参数，全部必填） |
| `instruments.py` | 多标的验证：`pool_outcomes`（按 `event_key` 中的标的核对后池化各标的的标签表）、`run_multi_instrument_validation`（池化 G0 – G3 + 逐标的 G0 – G3 记为 `<门 id>.instrument.<名>`，再接 G4；见下文「多标的验证」） |
| `robustness.py` | C-R1 ~ C-R5 与 C-T1 过拟合概率的检查：过拟合、参数邻域、时间对齐、延迟压力、成本压力、walk-forward 窗口统计（无收益窗口计入并使比例 INCONCLUSIVE）、状态分解、容量、跨资产；每个返回 `RobustnessCheck`（门 + 阈值来源 + 缺失字段 + 表格） |
| `overfitting.py` | PBO（CSCV，分块间按 `data_split.embargo` purge / embargo，purge 宽度至少为必填的标签 / 持有期 `horizon`，与 `splits.purge_and_embargo` 同一语义）、Deflated Sharpe、逐期 Sharpe（不含阈值） |
| `returns.py` | `PeriodReturns`（毛收益 + 成本，净收益按成本倍数计算）、`from_backtest`（`BacktestResult` → 逐期收益）、`TrialReturns` |
| `retro_audit.py` | 回溯审计：以现行规则重跑并报告逐门差异；已拒绝对象永不被翻转为通过（构造时强制） |
| `report.py` | `report_view` / `to_json`：规范 JSON 报告视图（供日后 apps/web 可视化）；`promotion` 块：没有 G5 结果的 PASS 标为不可晋升（`sealed_oos_not_evaluated`） |
| `gates.py` | `threshold(profile, path)`（值 + 来源路径）、`explicit_threshold`（`param:`）、`compare_gate`、`flag_gate`、`inconclusive_gate`、`missing_field_gate`、`configuration_missing_gate`、`ProfileFieldMissing` |
| `splits.py` | `purge_and_embargo`、`walk_forward_folds` / `walk_forward_windows`（Profile 窗口与 embargo）、`non_overlapping_windows`（G4 窗口统计只计不重叠的测试窗口）、`purged_k_fold`（排除封存区）、`research_spans` |
| `controls.py` | `SignalStudy` / `FittableStudy` Protocol；`blind_labels`（流水线使用的侧向一律由盲化标签计算）；shuffle / shift 负对照（C-L6） |
| `sealed_oos.py` | `SealedOosVault`（固定日期窗口，开封前锁定，每族只开封一次，全局预算 `max_unsealings` 为必填显式参数，每次开封只可评估一次；`claim_evaluation` 在任何封存样本离开前即记为已评估，返回一次性 `SealedEvaluation`）、`UnsealingLedger` Protocol + 内存实现 `InMemoryUnsealingLedger` + 落盘实现 `DurableUnsealingLedger`（见下） |
| `costs.py` | 成本模型 v1 的应用：净收益、盈亏平衡成本倍数、Profile 绑定检查 |
| `stats.py` | 有效独立样本（重叠区间连通分量数）、HAC t 检验、多重检验校正（`bonferroni` / `sidak`；其它方法拒绝） |
| `calibration.py` | 空模型校准报告框架：`RandomWalkMarket` 上的假阳性率 / 检出率（`FRAMEWORK_ONLY_NOT_CALIBRATED`，不提出任何数值） |

门清单、阈值来源与已知缺口见 ADR-0037、ADR-0041 与 docs/architecture/07-validation.md §2.2 / §2.3。
策略回测的接线（`PipelineBacktestValidator`）在 `research/strategies/validation.py`。

接线的两个公开补充（ADR-0041 Implementation note E4/E5）：

- **G4 输入的公开构建器**：`PipelineBacktestValidator.robustness_input(spec, backtest)` 构建 `validate` 会交给 G4 的同一输入
  （重跑所选参数点；重跑不复现该回测则拒绝）。`robustness_diagnostic(spec, backtest)` 在前序阶段已 FAIL 时也能跑 G4，
  结果是标为 `diagnostic_report_only` 的 `RobustnessDiagnostic`：**只报告**，不进入 `ValidationReport`、不改变判定、不写 Failure Registry。
- **价格 bar 与 manifest 的绑定**：`ValidatorSetup.dataset_bars` 给出 `infrastructure.bars.DatasetPriceBars`（数据集路径）时，
  适配器门 `G0.manifest_binding` 核对 setup 的 manifest 哈希即该包装的哈希、重跑用到的每根 bar 都是包装内已证明的 bar、所验证标的有 bar、
  没有 bar 晚于包装的 `price_cutoff`；任一不符判 **FAIL**（G0 → `REJECTED` / `CONTRACT_VIOLATION`）。`dataset_bars=None` 为合成路径：
  manifest 哈希只是未经验证的标签，不加门，报告视图 `extra.price_binding.mode = "synthetic_unverified"`。
- **特征 / 价格 manifest 对**（ADR-0041 Implementation note, manifest pair in G0）：`ValidatorSetup.manifest_pair` 给出该链的
  `infrastructure.bars.ManifestPair`（由 `pair_manifests` 证明）时，`G0.manifest_binding` 另核对 pair 的价格哈希即 `dataset_bars` 的
  manifest 哈希、pair 的特征哈希即 `ValidatorSetup.feature_manifest_hashes` 中每个特征请求的 `manifest_content_hash`（信号不带
  manifest 哈希，须由调用方显式传入且至少一个）、pair 哈希可按规则重算（`pair_hash_of`）。给 pair 无 `dataset_bars`、给特征哈希无 pair
  均视为不一致的 setup；任一不符判 **FAIL**。不给 pair 时 E5 行为不变；真实数据冒烟走 pair 路径。
- **声明的执行模型**（ADR-0041 Implementation note, execution model in validation, 2026-09-26）：`ValidatorSetup.backtester` /
  `.execution`（互斥，都可选，默认 `None`）显式声明候选实际用哪个 `plugins.backtest.execution.ExecutionModel` 回测。给出时，
  适配器门 `G0.execution_model` 核对给定 `backtest.provider_hash` 就是该模型的 descriptor 哈希，不符判 **FAIL**（`REJECTED` /
  `CONTRACT_VIOLATION`），`robustness_input` 在重跑参数网格前也做同一拒绝；G4 容量检查（`capacity_check`）随之读取该模型自己的
  `impact_coefficient`，优先于显式的 `RobustnessParams.impact_coefficient`——二者都给出且不同时不静默择一，`G4.capacity.impact_estimated`
  = `INCONCLUSIVE`（具名原因 `impact_coefficient_mismatch`，两个值都记录）。两个字段都不给（每个既有调用方）时不加任何门，报告
  （含内容哈希）逐字节不变。

### 落盘的开封账本（调试批次，2026-09-25，ADR-0041 实施说明）

`SealedOosVault` 默认仍是纯内存的（省略 `ledger` / `path` 时等价于原先的 `InMemoryUnsealingLedger()`）。
构造时传入 `path=<文件>`（与 `ledger` 二选一，同时给出会报错）即改用 `DurableUnsealingLedger`：
每次 `record` / `mark_evaluated` 追加一行哈希链 JSON（`research.persistence.AppendOnlyJournal`，写法与
`research.strategies.failure_registry.FailureRegistry` 一致——只追加、`flush` + `fsync`、文件变短即拒绝），
重新打开文件会重放并校验整条哈希链后恢复状态。效果：某族在进程 A 开封后，进程 B 打开同一文件无法再次开封该族；
在进程 A 里 `claim_evaluation` 消耗的评估，进程 B 也无法再次领取；全局 `max_unsealings` 预算跨重启累计计数。
链被篡改、截断或出现未知记录类型一律 `research.persistence.JournalCorrupted`，不静默修复。

### 调试批次（2026-09-26）：G4 检查隔离与多种子负对照

> 状态：**CODE_COMPLETE / DEBUG_PENDING**（代码与测试完成，尚待调试 / 复核；未改变任何契约、Schema、Profile 数值或门的放行条件）。

- **G4 逐检查异常隔离**（`g4.py`，Phase 8）：`run_robustness` 按固定顺序（`CHECKS`）逐个隔离运行 9 项检查。某项检查抛出**意外**异常时，
  该检查只产生一个 `INCONCLUSIVE` 门 `<前缀>.check_error`（metric `check_error:<异常类型>`；`details` 记录类型与一行、屏蔽内存地址、
  不超过 200 字符的确定性消息），其余检查照常运行，因此 G4 最好也只是 `INCONCLUSIVE`，别处的 FAIL 仍然 FAIL。**有意的拒绝照旧抛出**：
  `ValueError`（含 `UnsupportedMethod`、`ProfileFieldMissing`）、`TypeError`、`MemoryError`；`BaseException` 从不捕获。没有异常时输出逐字节不变
  （测试固定了两份夹具的结果哈希）。注意：此前意外异常会从 `validate` 传播出去，现在变为报告内的 INCONCLUSIVE 门（不写 Failure Registry）。
- **多种子负对照**（`pipeline.py`，Phase 4；backlog C「负对照为单次固定种子」）：`InSampleInput.control_seeds` / `ValidatorSetup.control_seeds`
  可选，**没有默认种子列表**。`None`（默认）= 原单次抽取（shuffle 用 `seed`、shift 用 `seed + 1`），门逐字节不变（测试固定哈希）。给出非空、
  互不相同的 `int` 元组时，两种对照对每个种子各跑一次（直接使用该种子，门 id 中的种子就是实际种子）：逐种子门
  `G1.shuffle_control.seed.<s>` / `G1.shift_control.seed.<s>` 按**基础门 id** 判定（Profile 对 `G1.shuffle_control` / `G1.shift_control` 的
  inconclusive band 适用于每个种子），阈值仍为 `significance.multiple_testing_threshold`；基础门按标准规则汇总（任一 FAIL → FAIL，否则任一
  INCONCLUSIVE → INCONCLUSIVE），报告最小 p 值（metric `..._timing_p_value_min_over_seeds[>=]`）。种子越多只会增加失败的途径，从不放宽；
  G2 空模型仍为单种子（`seed + 2`）。
- **仍然存在（受阻）**：统计仍是 `float` 上的 HAC 正态近似（ADR-0037 §5）；替换为精确 / Decimal 实现须先按 ADR-0052 补全契约（`core/`，超出本批次文件边界），本批次未改动，仍受阻。
  负对照与 G3 仍共用 `significance.multiple_testing_threshold`（D-CTRL；独立阈值字段同属 ADR-0052 的契约补全，未实施）。

### 多标的验证（Phase 8 实施说明，2026-09-26）

> 状态：**CODE_COMPLETE / DEBUG_PENDING**（未改变任何契约、Schema、Profile 数值或门的放行条件）。

- `instruments.py`：`OutcomeRequest` 保持单标的（契约不变），多标的回测按标的各算一张 `OutcomeTable`。`pool_outcomes` 把它们合成一张池化表，
  拒绝任何 `event_key` 未指向本标的的标签、以及结果 / 标签规格 / Provider 不一致的表；池化表是手工构建的（`request_hash` / `provider_hash` 为 `None`，
  不落盘），`result_hash` 是各标的结果哈希的内容哈希。
- `run_multi_instrument_validation`：先跑**池化** G0 – G3（标准门 id，组合层面统计；时间重叠的跨标的标签在有效样本 / overlap lag 中按相关计入，偏保守；
  G3 用不变的 `family_trial_count`）；池化无 FAIL 时再对**每个标的**跑自己的 G0 – G3，门按基础 id 判定（Profile 的 inconclusive band 照常适用）后记为
  `<门 id>.instrument.<名>`（`reason_for_gate` 按基础前缀映射）；无标签的标的记 `G0.data_available.instrument.<名>` = `INCONCLUSIVE`；全部无 FAIL
  才进入 G4（`g4.robustness_stage`，即 `run_validation` 的尾段，行为不变）。报告结论仍是全部门的 `derive_verdict`：任一 FAIL → FAIL，否则任一
  INCONCLUSIVE → INCONCLUSIVE，PASS 需要池化证据与每个标的的证据都通过（交并检验，只会减少 PASS）。
- 已知限制（DEBUG_PENDING）：池化后的 G1 负对照对按时间交错的多标的标签序列做置换 / 循环平移，可能把一个标的的方向与另一标的的标签配对——
  仍是有效的零假设（只会破坏对齐），但其在多标的数据上的误报率尚未校准（Phase 9）。

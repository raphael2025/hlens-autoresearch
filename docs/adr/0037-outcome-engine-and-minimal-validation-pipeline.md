# ADR-0037: Outcome Engine、成本模型 v1 与最小 Validation Pipeline（Phase 4 框架）

| 字段 | 值 |
|---|---|
| 状态 | **Accepted**（2026-09-25） |
| 日期 | 2026-09-25 |
| 决策者 | Claude Code（Opus），依 Raphael 2026-09-25 明确授权（红线除外） |
| 相关 Phase | Phase 4（依 Raphael 2026-09-25 全阶段框架实现指示） |
| 影响范围 | Contract（`core/contracts/outcome.py`、`core/contracts/cost_model.py`，additive）、`plugins/outcomes/`、`research/outcomes/`、`research/validation/` |
| 是否破坏兼容 | 否：只新增 8 个模型与 1 个 Protocol；Schema 87 → 95；`OutcomeSpec`、`ValidationProfile` 与既有 Schema 逐字节不变；`CONTRACT_SCHEMA_VERSION` 不变 |
| 实施状态 | FRAMEWORK_IMPLEMENTED / NOT_VALIDATED |
| 前置 | [ADR-0007](0007-validation-architecture-three-layers.md)、[ADR-0012](0012-information-flow-and-kind-invariants.md)、[ADR-0013](0013-deterministic-verdict-and-finite-numbers.md)、[ADR-0014](0014-validation-profile-structural-invariants.md)、[ADR-0017](0017-provider-delivery-schedule.md)、[ADR-0042](0042-synthetic-market-provider.md) |

## 背景

roadmap Phase 4 要求：OutcomeSpec + OutcomeProvider、Outcome 物化、最小 Validation Pipeline（G0–G3 + Sealed OOS）、
成本模型 v1、空模型校准报告；验收为"Outcome 不能被作为输入（契约 + 测试）、purging / embargo 实现并测试、泄漏检测可用"。
ADR-0017 要求 Provider 的 Protocol、DTO 与 provider-agnostic contract tests 先于实现交付。
已冻结的 `OutcomeSpec` 只有 `horizon` 与自由文本 `label_definition`，没有参数槽位；`ValidationProfile` 字段已冻结但数值 TBD。

## 裁决

### 1. OutcomeProvider 契约（`core/contracts/outcome.py`）

- Protocol：`descriptor → OutcomeProviderDescriptor`（`name@version`、`deterministic: true`、`outcome:name@semver → 标签规格哈希`），
  `compute(OutcomeRequest) → OutcomeResult`；不支持的规格 → `UnsupportedOutcome`。
- DTO：`OutcomeLabelSpec`、`OutcomePriceBar`、`OutcomeEvent`、`OutcomeRequest`、`OutcomeLabel`、`OutcomeProviderDescriptor`、`OutcomeResult`。
  数值一律 `Decimal`，浮点入口拒绝，NaN / ±Infinity 拒绝（ADR-0013）。
- **标签规格**：按"契约只追加"，方法与参数放在新增的 `OutcomeLabelSpec`（`method`、`horizon`、屏障），它以
  `outcome` 引用 + `outcome_spec_hash` 绑定一份 `OutcomeSpec`；`bind()` 从该规格复制 horizon，两处不会不一致。
- **时间对齐**：入场 = 事件后第一根 bar 的开盘（`interval_start >= event_time`，延迟必须短于一根 bar）；窗口
  `[entry_time, entry_time + horizon]` 必须由首尾相接的 bar 覆盖，缺口或数据不足 → `value = None`（不填补）；
  未触达屏障的标签必须恰在 `entry_time + horizon` 出场；`OutcomeLabel.available_time` = 所用 bar 的最大 `available_time`，
  在它之前该标签不可用。`OutcomeResult.check_answers` 对照请求核验这些关系。
- **Outcome 永不作为输入**：`OutcomeLabel` / `OutcomeResult` 带判别字段 `label_only: Literal[True]`；所有输入 DTO 都是
  `extra="forbid"`，因此 Outcome 对象或其 dump 交给 `FeatureObservation` 等必然被拒绝；`refuse_outcome_input` 是运行时同一判别；
  既有 ADR-0012 白名单继续拒绝 Outcome 的 `Ref` 与 `zone=outcome`。诚实边界：把 Outcome 的**数值**抄进输入值无法在契约层阻止，
  由 G1 负对照与信息流审计承担。
- provider-agnostic suite：`tests/contract_suites/outcome.py`（确定性、一一对应、窗口外价格不可见、事件前价格不可见、
  缺数据显式 `None`、事件顺序无关、只作标签、未声明规格拒绝）；单点故障变体（越过 horizon、部分窗口填补）被逐一杀死。

### 2. 首批实现（`plugins/outcomes/`）

`ForwardReturnOutcome`（`exit_close / entry_open - 1`）与 `TripleBarrierOutcome`（上 / 下 / 垂直屏障；同一根 bar 同时触及两侧时
保守判为下屏障；跳空按开盘价成交）。horizon 与屏障都是标签规格参数，由规格哈希绑定；结果量化到 18 位小数（half-even）。

### 3. 最小 Validation Pipeline（`research/validation/`）

- 阶段与门编号**按 07-validation.md §2 的流程**：G0 复现（含数据 / 契约绑定）、G1 泄漏、G2 含成本的样本内统计、
  G3 多重检验校正后的显著性、G5 Sealed OOS；G4（稳健性）属 Phase 8。一个阶段出现 FAIL 即停止（流程图送往 Failure Registry），
  INCONCLUSIVE 不停止。整体判定只由 `derive_verdict` 给出（ADR-0013）。
- **全部数值阈值取自传入的 ValidationProfile**，没有任何默认阈值：`threshold(profile, path)` 同时返回值与字段路径，写入
  `GateResult.threshold` / `threshold_source`；比较方向写在 metric 名末尾（`[>=]` / `[<=]`）。`inconclusive_bands` 以 `gate_id` 为键：
  `|value - threshold| <= band` 时判 INCONCLUSIVE。有效样本不足判 INCONCLUSIVE（证据不足，不是 PASS，也不是反驳）。
- Profile 未专门定义的方法名（`multiple_testing_method` 只实现 `bonferroni` / `sidak`；`null_model` 只实现 `random-entry`）
  一律拒绝（`UnsupportedMethod`），不回退到其它方法。
- purging / embargo：`purge_and_embargo`、Profile 驱动的 `walk_forward_folds`、显式参数的 `purged_k_fold`；触及封存区的样本不进入研究切分。
- 泄漏负对照（C-L6）：`SignalStudy` 在打乱（shuffle）与循环平移（shift，至少 `lag + 1`）后的标签上重跑，检验
  `side × (y − ȳ)` 的 HAC p 值；效应必须消失（`p >= significance.multiple_testing_threshold`）。
- Sealed OOS：窗口 = Profile 的固定日期边界 + `sealed_oos_length`；`SealedOosVault` 在记录开封前拒绝交出窗口内样本；
  每个假设族只能开封一次，开封记录只追加（`UnsealingLedger` Protocol + 框架用内存实现）。
- `failure_record` 把 FAIL 报告映射为 Failure Registry 记录（`reason_code` 来自 `core/errors`）。

### 4. 成本模型 v1（`core/contracts/cost_model.py`）

`CostModelSpec`（`kind=cost_model`，`proportional_v1`）：每侧手续费率 + 每侧滑点率，往返成本 `2 × (fee + slippage) × 压力倍数`。
费率与滑点是成本模型参数，不是验证阈值；总费率必须为正（零成本 = 跳过成本模型，被结构性拒绝）。流水线的所有净值都经过它；
G0 核对 Profile 的 `cost_stress.cost_model`、复现元组的 `cost_model_ref` 与其内容哈希都指向这一份规格。

### 5. 统计的框架选择

有效独立样本 = 贪心的两两不重叠持有区间数；显著性 = Newey–West（Bartlett）HAC 标准误 + 正态近似，lag = 样本间最大重叠数；
统计在由 `Decimal` 转换的 `float` 上按固定顺序计算（同平台确定）。这些是框架默认实现，校准阶段可以替换（需新 ADR）。

### 6. 空模型校准报告（框架）

`research/validation/calibration.py` 在 `plugins/synthetic/RandomWalkMarket`（ADR-0042）市场上运行 G0–G3，报告纯噪声上的
假阳性率与植入效应上的检出率。报告状态固定为 `FRAMEWORK_ONLY_NOT_CALIBRATED`：**不提出、不冻结任何 Profile 数值**；
按报告选定阈值属于两步冻结 Step 2，需要独立 ADR 与批准。测试只使用明确标注 TEST ONLY 的 Profile。

## 后果

- 正面：Phase 5 起的策略可以在同一条确定性流水线上得到带阈值来源的 ValidationReport；泄漏负对照能抓住"看了 Outcome"的研究；
  Outcome 作为输入在契约、白名单与运行时三处被拒绝。
- 负面 / 已知缺口：负对照各只做一次带种子的抽取（单次误报率约等于阈值）；Profile 没有专门的负对照字段，暂复用
  `significance.multiple_testing_threshold`；过拟合概率（`overfitting_threshold`）、每状态样本量、walk-forward 的窗口统计、
  延迟压力（`delay_stress_bars`）、G4 未实现；开封账本与 Outcome 表尚未持久化（内存实现 + JSON 行导出）；
  Canonical `bars_1m` 读取尚未接入（只有合成价格源）；Profile 数值仍全部 TBD。
- 与任务说明的差异：任务说明把门写作"G0 数据 / 契约、G1 泄漏、G2 复现、G3 统计 / 成本"；正式文档 07-validation.md 的顺序为
  G0 复现、G1 泄漏、G2 含成本统计、G3 多重检验。依文档优先级采用后者，数据 / 契约检查并入 G0。

## Implementation note (dataset wiring, 2026-09-25)

上文"Canonical `bars_1m` 读取尚未接入"的缺口由 `infrastructure/bars/dataset.py` 的 `outcome_request_from_dataset` 接上
（与 ADR-0038 的 `backtest_bars_from_dataset` 共用同一证明路径，镜像 `infrastructure/feature/dataset.py` 的 RT-6 修复）：
调用方只给 `DatasetBuilder` 与 manifest 内容哈希，manifest 只经该 builder 自身的验证型 `ManifestStore`（`load_manifest`）加载，
不存在接受裸哈希或 manifest 对象的入口；只接受单点 simulation 的 manifest（区间数据集拒绝，留待后续）；在 manifest 自身的
dataset snapshot 与 selection id 下读取 `klines_1m` 行（无行 / 所请求标的无行 → fail closed），逐标的以 manifest 的 PIT spec
重选 Canonical 1m bar，须**恰好**等于数据集行（多 / 少 → `CatalogIntegrityError`），lineage 须由 manifest 绑定；每根 bar 保留
选择给出的自身 `available_time`（仅当 spec 绑定 ADR-0032 时为假设生效时刻）与 `Decimal` OHLC，不补缺口。`price_cutoff`
默认为 manifest 的 `simulation_time`，不得晚于它；所请求窗口内任何 `available_time > price_cutoff` 的 bar 被拒绝而非静默丢弃。
请求携带 manifest 内容哈希。没有新增契约或 ADR。测试：`tests/infrastructure/bars/test_dataset_bars.py`（真实小数据集 →
`ForwardReturnOutcome` / `TripleBarrierOutcome` 标签，标签只在出场后可知；伪造 / 未持久化 manifest 与非 builder 验证者被拒；
cutoff 后的 bar 被拒；重跑逐位一致）。状态：FRAMEWORK_IMPLEMENTED / NOT_VALIDATED。

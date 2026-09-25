# ADR-0041: Validation & Robustness——G4 稳健性套件、回溯审计与策略验证接线（Phase 8 框架）

| 字段 | 值 |
|---|---|
| 状态 | **Accepted**（2026-09-25） |
| 日期 | 2026-09-25 |
| 决策者 | Claude Code（Opus），依 Raphael 2026-09-25 明确授权（红线除外） |
| 相关 Phase | Phase 8（依 Raphael 2026-09-25 全阶段框架实现指示）；Phase 5 验证接线 |
| 影响范围 | `research/validation/`（新增 `returns.py`、`overfitting.py`、`robustness.py`、`g4.py`、`report.py`、`retro_audit.py`；修正 `pipeline.py`、`controls.py`、`splits.py`、`sealed_oos.py`、`stats.py`、`gates.py`）、`research/strategies/validation.py`、`research/strategies/pipeline.py` |
| 是否破坏兼容 | 否（契约层）：**不新增、不修改任何 `core/contracts` / `core/domain` 模型**，Schema 数量不变。研究代码内有两处签名收紧：`SealedOosVault(..., *, max_unsealings)` 与 `purged_k_fold(..., *, profile)`（均为 P4 复审修正，见 §6） |
| 实施状态 | FRAMEWORK_IMPLEMENTED / NOT_VALIDATED |
| 前置 | [ADR-0007](0007-validation-architecture-three-layers.md)、[ADR-0013](0013-deterministic-verdict-and-finite-numbers.md)、[ADR-0037](0037-outcome-engine-and-minimal-validation-pipeline.md)、[ADR-0038](0038-strategy-risk-backtest-providers.md)、[ADR-0042](0042-synthetic-market-provider.md) |

## 背景

roadmap Phase 8 要求：完整稳健性套件（G4）、Deflated Sharpe、容量、跨资产、walk-forward；验证报告可视化；对已晋升对象的回溯审计；
验收为"C-R1–C-R5 全部实现并测试；已知过拟合样例被正确拒绝（负对照测试）"，禁止"新规则追溯使已拒绝对象通过"。
ADR-0037 交付了 G0–G3 + G5，并把过拟合概率、每状态样本量、walk-forward 窗口统计、延迟压力列为已知缺口；
ADR-0038 在 `research/strategies/validation.py` 留下 `TODO(phase4-wiring)`。另有一份对 Phase 4 验证代码的只读复审列出 5 个问题（§6）。

`ValidationProfile` 字段已冻结、数值 TBD；本 ADR 不改契约、不改 Constitution、不提出任何 Profile 数值。

## 裁决

### 1. 阈值来源规则（沿用 ADR-0037 并扩展）

- 每个门的阈值只来自 `gates.threshold(profile, <字段路径>)`；比较方向写在 metric 末尾；`inconclusive_bands` 以 `gate_id` 为键。
- **Profile 契约没有字段的规则**（容量、跨资产一致性、CSCV 分块数、Sealed OOS 开封预算）：调用方可以传入显式参数，
  其 `threshold_source` 记为 `param:<name>`（核验方看得出它不来自 Profile）；**不传则不发明默认值**——该门为
  `INCONCLUSIVE`，metric 为 `profile_field_missing:<name>`，检查状态为 `PROFILE_FIELD_MISSING`。请求契约中不存在的路径抛
  `ProfileFieldMissing`。唯一的字面比较是结构性符号（收益 `> 0`），不是校准数值。
- 方法名（`significance.overfitting_metric`、`parameter_stability.neighborhood_definition`）只接受已实现的名字，其余抛
  `UnsupportedMethod`，不回退。

### 2. G4 稳健性检查（`research/validation/robustness.py`）

输入是逐期收益 `PeriodReturns`（毛收益 + 成本，净收益 = 毛 − 倍数 × 成本，因此成本压力不需重跑、也不可能跳过成本模型）；
表现 = 净收益的逐期 Sharpe。每个检查返回 `RobustnessCheck`：门、所用阈值及来源、缺失字段、可视化用的 `details` 表。

| 检查 | 原则 | 门 | 阈值来源 |
|---|---|---|---|
| 过拟合概率（PBO via CSCV；Deflated Sharpe 仅报告或按 Profile 选用） | C-T1 / C-R1 | `G4.overfitting` | `significance.overfitting_metric`（方法）、`significance.overfitting_threshold`；CSCV 分块 `param:cscv_partitions` |
| 参数邻域稳定性（声明网格中的相邻点） | C-R1 | `G4.param_neighborhood.performance_ratio` / `.positive_fraction` | `parameter_stability.*` |
| 时间对齐（决策网格平移） | C-R1 | `G4.time_alignment.<i>` | `parameter_stability.time_alignment_offsets[i]`、`.min_neighborhood_performance_ratio` |
| 延迟压力（信号滞后 +k bar 执行） | C-R4 / A6 | `G4.delay_stress` | `cost_stress.delay_stress_bars`、`cost_stress.min_breakeven_cost_multiple` |
| 成本压力（实际回测收益） | C-R4 / A6 | `G4.cost_stress.breakeven` / `.<i>` | `cost_stress.min_breakeven_cost_multiple`、`cost_stress.stress_multipliers[i]` |
| walk-forward 窗口统计（按时期分段报告） | C-S4 / C-R3 | `G4.walk_forward.positive_fraction` / `.max_window_share` | `data_split.walk_forward.*` |
| 状态分解 + 每状态有效样本 | C-R2 | `G4.state.sufficient_states` / `.pnl_outside_undersampled_states` | `sample_size.min_effective_trades_per_state` |
| 容量（换手 × 参与率 vs 成交额）与冲击估计 | C-R5 | `G4.capacity.estimated` / `.required` | 无 Profile 字段：`param:capacity.max_participation_rate`、`param:capacity.min_capacity` |
| 跨资产一致性（声明范围内逐一检验） | C-R3 | `G4.cross_asset.scope_covered` / `.positive_fraction` | 无 Profile 字段：`param:cross_asset.min_positive_fraction` |

- PBO：Bailey 等（2017）的 CSCV，`S` 个连续分块取一半作样本内，样本内最优者在样本外的相对秩 logit ≤ 0 的比例；
  DSR：Bailey & López de Prado（2014），`N` = 假设族累计 trial 数（含失败，C-T1），`V[SR]` 由已评估 trial 估计。
- C-R2：结果不得依赖样本不足的状态——有效样本达标的状态合计净收益必须 > 0；`min_regime_coverage` 是自由文本，只记录不解释。
- C-R5 只要求"估计"：给出参与率参数即产出容量估计（缺参数 = INCONCLUSIVE）；容量下限判定需显式参数，否则记为缺失字段、不设门。

### 3. G4 组合与完整流程（`research/validation/g4.py`）

`run_validation(in_sample, robustness)`：`run_in_sample`（G0–G3，行为不变）之后，若无 FAIL 才运行 G4；G4 输入可惰性构造
（在前序 FAIL 后不会重跑参数族）。没有 G4 输入时物化为 `G4.robustness_input = INCONCLUSIVE`（ADR-0013：缺失的阶段不是 PASS）。
G4 必须使用验证上下文绑定的同一 Profile（内容哈希核对）。G5 仍是独立、一次性的调用。
`failure_record` / `reason_for_gate` 为 G4 门映射既有原因码（`PARAM_UNSTABLE`、`STATE_CONCENTRATED`、`COST_KILLED`、`OOS_DECAY`、
`NOT_SIGNIFICANT_AFTER_MTC`）；未新增原因码。

### 4. 回溯审计与报告视图

- `retro_audit.py`：对每个对象（生命周期状态 + 当时报告 + 以现行规则重跑的回调）输出逐门差异与建议动作，**只报告，不执行任何生命周期转移**。
  已拒绝对象（`REJECTED` / `FAILED` 或当时判定 FAIL）的有效判定恒为 FAIL、动作恒为 `STAYS_REJECTED`（即使现行规则会通过，也只报告
  `would_now_pass`）；`AuditFinding` 构造时强制该规则（`RetroAuditViolation`）；已晋升对象的有效判定取当时与现行中较严者，只能被标记
  `FLAG_FOR_REVALIDATION` / `REVIEW_INCONCLUSIVE`。
- `report.py`：`report_view` 把 `ValidationReport` + G4 结果转成规范 JSON（按阶段分组的门、阈值来源分 Profile / 显式参数、缺失字段、
  各检查的表格），供日后 `apps/web` 可视化；本批不做任何 web 工作，`apps/` 仍不得 import `research/`。

### 5. Phase 5 接线（`research/strategies/`）

`PipelineBacktestValidator` 实现 `BacktestValidator`：用 `CandidateTrialRunner`（策略 → 风控 → 回测，可延迟 k bar、平移决策网格、
限定标的）重跑所选参数点（G0 复现），检查回测成本率与绑定的 `CostModelSpec` 一致（`G0.backtest_cost_model`）、只验证单一标的
（`G0.single_instrument_adapter`），把非零目标仓位转成 `OutcomeEvent` 并由绑定的 OutcomeProvider 在同一 bar 上打标签，侧向只由
契约校验过的 `TargetPosition` 构造；然后跑 G0–G4（G4 参数族 = 规格声明的整个参数空间）。技术性失败（`NOT_REPRODUCIBLE`、
`RUN_ERRORED`）记为 `FAILED`，其余 FAIL 记为 `REJECTED`；验证器内部异常也写入 Failure Registry。`TODO(phase4-wiring)` 已移除。

### 6. Phase 4 复审修正（本批一并修复，均有回归测试）

1. **标签泄入侧向**：流水线使用的一切侧向（G0 确定性、G2 / G3、G5）都由"盲化"标签向量（全零）计算；新门 `G1.label_blind_sides`
   要求真实标签下与盲化标签下的侧向一致；研究对象收到的第一次调用必为盲化调用。
2. **有效样本量**：嵌套重叠以 `max` 扩展阻塞区（`[0,10)`、`[2,3)`、`[4,5)` 计 1），即重叠区间的连通分量数。
3. **Sealed OOS 预算与一次性评估**：`SealedOosVault` 必须显式给出全局开封预算 `max_unsealings`（Profile 无此字段，来源
   `param:max_unsealings`），换假设族也不能无限重开；每次开封只换来一次 `sealed_view` / `run_sealed_oos`（`SealedOosAlreadyEvaluated`），
   记录在只追加的账本中。
4. **统计前先切分**：G2 / G3 只在 Profile 的 walk-forward 测试折上计算（研究窗口内、训练集已 purge + embargo）；可拟合研究
   （`FittableStudy`）逐折只用该折的训练标签拟合；新增结构门 `G2.walk_forward_folds`。
5. **`purged_k_fold` 排除封存区**：与 walk-forward 一样只使用 `research_spans`（需传入 `profile`）。

## 后果

- 正面：C-R1 ~ C-R5 与 C-T1 过拟合概率都有可执行、带阈值来源的门；已知过拟合样例（纯噪声上 40 个随机策略取最优）在 TEST ONLY
  Profile 下被 PBO 与 DSR 两种方法拒绝，植入真实效应的动量族不被判为过拟合；Phase 5 策略可端到端得到 G0–G4 报告与 JSON 视图。
- 负面 / 已知缺口：
  - Profile 契约没有容量、跨资产一致性、CSCV 分块数、Sealed OOS 开封预算、冲击系数字段——这些规则现在只能 `INCONCLUSIVE` 或依赖
    `param:` 显式参数；补字段需要契约 ADR（Profile 为冻结契约，H1 / H2）。
  - **浮点进入哈希载荷**：`GateResult.value` / `threshold`、`ValidationProfile` 的阈值字段与 HAC / Sharpe / PBO 统计都是 `float`，
    经 `ValidationReport` 等进入内容哈希；同平台确定，但跨平台 / 跨库版本的按位一致没有保证。本批**不改** `core/domain` 模型，
    留待契约层决定（例如十进制文本或定点表示）。
  - 研究对象若在流水线之外、以标签数据预先算好侧向（如用 `sign(label)` 填充的 `FixedSides`），流水线无法识别；防线是来源——
    策略接线只从契约校验过的 `TargetPosition` 构造侧向。
  - 适配器只验证单一标的（`OutcomeRequest` 为单标的）；多标的回测为 `INCONCLUSIVE`。跨资产检查通过逐标的单独重跑实现。
  - 浮点参数值无法经 `StrategyRequest`（`ObservationScalar` 不含 float）请求，含浮点的参数空间在接线中会失败关闭。
  - CSCV 不在分块间做 purge；状态标签由调用方提供（必须因果）；冲击模型为平方根形式、系数为显式参数；所有检查只在合成市场上
    做过冒烟测试，数值仍全部 TBD，校准属于两步冻结 Step 2。
- 不涉及：Constitution、Validation Profile 字段、Lifecycle 状态机、`core/contracts` Schema、`apps/`、实盘。

## Implementation note (review fixes, 2026-09-25)

一份对 Phase 8 G4 代码的只读复审列出 6 项（调试待办 R13 – R18，`docs/reviews/2026-09-25-framework-debug-backlog.md`）。
本说明记录修正；**不新增 ADR、不改 `core/domain` / `core/contracts`、不改 Profile Schema、不引入任何数值阈值**
（缺 Profile 字段的问题仍属 D-PFIELDS / D-CTRL，由 Raphael 决定）。每项都有先失败后通过的回归测试
（`tests/research/validation/test_g4_review_fixes.py`）。总规则：**计算出一个数值本身永远不算通过**；必需检查的配置为空时不得静默跳过。

1. **R13 容量（C-R5）**：`min_capacity` 缺失时只记入 `missing_fields`、不产生门，整体可能 PASS；`G4.capacity.estimated` 恒为 PASS。
   修正：估计值（容量、冲击）只写入 `details`；`G4.capacity.estimated` 只在无法估计时以 INCONCLUSIVE 出现；估计成功后
   `G4.capacity.required` 与 `param:capacity.min_capacity` 比较，缺失则为 `profile_field_missing:capacity.min_capacity`（INCONCLUSIVE）；
   C-R5 同样要求冲击估计，缺冲击系数时新增 `G4.capacity.impact_estimated` = `profile_field_missing:capacity.impact_coefficient`。
   结构防线：`RobustnessCheck` 拒绝"列出缺失字段却没有对应 INCONCLUSIVE 门"与"没有门也没有原因"的检查。
2. **R14 空配置静默跳过**：逐项依据文档判定（新门帮助函数 `gates.configuration_missing_gate`，metric `configuration_missing:<what>`）：

   | 检查 | 依据 | 空 / 关闭时 |
   |---|---|---|
   | 参数邻域（无邻点） | C-R1"只在孤立参数点上成立的结果无效" | 两个 `G4.param_neighborhood.*` 门 INCONCLUSIVE（`param_search_space.neighbors`） |
   | 时间对齐（偏移为空） | C-R1；07-validation §5.1 把"时间对齐（bar 偏移）测试"列为 C-R1 的 Profile 组成 | `G4.time_alignment.offsets` INCONCLUSIVE（`parameter_stability.time_alignment_offsets`） |
   | 延迟压力（`delay_stress_bars = 0`） | C-R4 / A6；§5.1 把"延迟压力"列为 C-R4 的组成 | `G4.delay_stress` INCONCLUSIVE（`cost_stress.delay_stress_bars`） |
   | 跨资产（未声明范围） | C-R3"必须在声明的适用范围内检验" | `G4.cross_asset.scope_covered` INCONCLUSIVE（`declared_instruments`） |

   文档中没有任何 G4 检查被写为可选，因此**没有检查使用 NOT_APPLICABLE**；`NOT_APPLICABLE` 仍保留，但只允许带记录原因的无门检查。
   注意：Profile 契约允许 `delay_stress_bars = 0` 与空 `time_alignment_offsets`（结构合法），但这样的 Profile 下 G4 永远不能 PASS；
   若 Raphael 认为某类研究可豁免，需要 Profile 层面的明确表达（D-PFIELDS 范畴）。
3. **R15 状态 P&L 集中度（C-R2）**：原门只要求"样本充足状态合计净收益 > 0"，不看欠采样状态贡献多少。修正：始终报告
   `undersampled_pnl_share = max(欠采样净收益, 0) / 总净收益`；有显式参数 `param:state.max_undersampled_pnl_share` 时新门
   `G4.state.undersampled_pnl_share` 与之比较（`[<=]`）；无参数且欠采样状态贡献为正时该门为 `profile_field_missing`（INCONCLUSIVE）；
   欠采样状态没有正贡献时无需阈值、不加门。`RobustnessParams` 新增必填字段 `max_undersampled_pnl_share`（`None` = 未给）。
4. **R16 walk-forward 窗口重叠**：`step < test_window` 时测试窗口重叠、同一时期被多次计数。选择**只计不重叠窗口**（不拒绝配置，
   因为 `WalkForwardParams` 刻意不约束 step 与窗口的关系）：`splits.non_overlapping_windows` 按时间贪心保留测试区间两两不交的窗口，
   `details` 记录 `profile_windows` 与 `windows_skipped_overlapping`。G2 已按事件只保留首个折，不受影响。
5. **R17 CSCV 分块间 purge**：实现 purge / embargo：每个拆分中，距任一样本外分块起点之前或终点之后严格小于 `embargo` 的样本内时期被剔除
   （与 `splits.purge_and_embargo` 同一语义）；`embargo` 为必填参数，`overfitting_check` 传入 Profile 的 `data_split.embargo`
   （与切分共用，C-L5 要求其覆盖最长 Outcome horizon）；`embargo = 0` 与原 CSCV 完全一致；剔除后样本内少于 2 期则拒绝计算
   （`CscvPurgeTooWide` → `pbo_not_computed:purge_leaves_too_few_in_sample_periods`，INCONCLUSIVE）。本 ADR「后果」中"CSCV 不在分块间做 purge"一条由此关闭。
6. **R18 只记录、不改规则**：(a) 在流水线外用标签符号预填的 `FixedSides` 无法被 `G1.label_blind_sides` 识别——来源限制，防线仍是接线只从
   契约校验过的 `TargetPosition` 构造侧向（`research/strategies/validation.py` 文档已写明）；(b) `validate` 按设计不含 G5，封存 OOS 是独立、
   显式的一步：**G0 – G4 PASS 没有 G5 结果永不可晋升**。结构标注：报告视图新增 `promotion` 块（视图 schema 1.1.0，仅新增），
   `BacktestValidation.promotion_blocked_reason` 同源——非 PASS 为 `verdict_not_pass`，无 G5 门的 PASS 为 `sealed_oos_not_evaluated`，
   只有含 G5 且 PASS 才为 `None`（仍只是进入 ADR-0006 生命周期审查的资格）。

测试夹具：P9 合成实验室的 TEST ONLY 参数原先 `min_capacity=None`，依赖 R13 的静默通过；现显式给出 `min_capacity=0.0`（TEST ONLY，
表示"本冒烟测试不设容量要求"），断言未改动。`test_robustness.py` 中 `delay_stress_bars = 0` 判 NOT_APPLICABLE 的断言改为更严格的
INCONCLUSIVE + `configuration_missing` 断言（原断言编码的正是 R14 缺陷）。状态仍为 FRAMEWORK_IMPLEMENTED / NOT_VALIDATED。

## Implementation note (review fixes 2, 2026-09-25)

不新增 ADR。第二轮只读评审中与验证代码有关的发现（调试待办 R21 – R23）；无 Profile 结构、Constitution 或契约变更，
所有阈值仍只来自 Profile 或显式参数。状态仍为 FRAMEWORK_IMPLEMENTED / NOT_VALIDATED。

1. **R21 封存评估在释放时原子消耗**：`SealedOosVault.claim_evaluation(family)` 在任何封存样本离开 vault **之前**把该族记为
   已评估，返回一次性 `SealedEvaluation`（bar 与标签各只能取一次）；`sealed_view` 改为 `claim_evaluation(...).view(...)`（行为不变）。
   `SealedOosInput.evaluation`（可选）让先取 bar 的调用方用已消耗的凭据跑 G5；凭据须属于同一族且来自同一 vault。
   评估已消耗却没有统计量时，`pipeline.sealed_oos_without_result` 给出 `G5.unsealing_recorded` PASS +
   `G5.oos_evaluation` = `consumed_without_result:<原因>`（INCONCLUSIVE，永不 PASS）。
2. **R22 CSCV purge 至少覆盖标签 / 持有期**：`probability_of_backtest_overfitting` 新增必填 `horizon`：每期视为跨度
   `[t, t + horizon]`，与 `splits.purge_and_embargo` 同一语义——与样本外分块的跨度重叠即 purge（含端点），embargo 从分块最后一个
   跨度的终点起算；即分块前宽度 `max(horizon, embargo)`（保留 R17 的"分块前 embargo"），分块后 `horizon + embargo`；
   `horizon = 0` 与 R17 完全一致。`RobustnessInput` 新增必填 `holding_horizon`；`PipelineBacktestValidator` 取绑定标签规格的
   `horizon` 与重跑中最长持有期（非零仓位决策到下一决策或数据末尾）的较大者。`details.pbo` 记录 `purge_horizon_seconds`。
   剔除过多仍为 `CscvPurgeTooWide` → `pbo_not_computed:purge_leaves_too_few_in_sample_periods`（INCONCLUSIVE）。
   回归测试把 2 分块的剔除数与 `purge_and_embargo` 逐一对照。
3. **R23 walk-forward 空窗口计入分母（保守选项）**：原实现丢弃没有收益的窗口，抬高正收益窗口比例。两种可选：计为非正，
   或报告并判 INCONCLUSIVE。选择后者：空窗口说明证据没有覆盖 Profile 的 walk-forward（C-S4），属证据不足；计为非正会把缺证据
   变成否证（FAIL → REJECTED 终态，且原因码误记为 OOS_DECAY）。现在每个不重叠窗口都列入 `details.windows`（空窗口 `periods = 0`），
   `windows_without_returns` 计数，任一空窗口即 `G4.walk_forward.positive_fraction` = `walk_forward_windows_without_returns`
   （INCONCLUSIVE）；单窗口 P&L 占比不受空窗口影响（空窗口不改变最大值与总和），仍在有收益的窗口上计算。

测试夹具：G4 夹具的 `holding_horizon` = 1 分钟（`robustness_fixtures.HOLDING_HORIZON`：每根 bar 结束时重新决定仓位、只持有一根）；
R17 测试显式传 `horizon=0` 以保持其原语义。循环 E2E 的连带变化见 ADR-0049 同日实施说明第 5 条。

## Implementation note (E4/E5, 2026-09-25)

真实数据冒烟（`docs/reviews/2026-09-25-framework-debug-backlog.md` E 节）的两项发现，在 §5 接线上修正；契约、Profile schema、
`core/domain` 均未改动，阈值无变化。

1. **E4 公开 G4 输入构建器**：原先构建 G4 输入的逻辑只在私有 `_robustness` 中，而 `validate` 仅在 G0 – G3 无 FAIL 时惰性构建，
   冒烟测试只能调用私有方法。现公开 `PipelineBacktestValidator.robustness_input(spec, backtest)`：重跑 setup 的所选参数点，
   重跑结果哈希不等于给定回测时拒绝（`ValueError`，输入会描述另一个回测）；`validate` 与之共用同一构建逻辑。另增诊断模式
   `robustness_diagnostic(spec, backtest)`：前序阶段 FAIL 后仍可跑 G4，结果为 `RobustnessDiagnostic`（`mode = "diagnostic_report_only"`，
   `verdict_effect = "none"`），**只报告**：不进入 `ValidationReport`、不改变判定（ADR-0013 的判定仍只由 `validate` 的门给出）、
   不写 Failure Registry。§3「G0 – G3 FAIL 后不跑 G4」的流程不变。
2. **E5 manifest 绑定改为经验证**：原先 `OutcomeRequest` 的 manifest 哈希由调用方给出、按信任接受。现 `ValidatorSetup` 新增
   `dataset_bars: DatasetPriceBars | None`（`infrastructure.bars`，唯一生产者 `backtest_bars_from_dataset` 已逐根证明 bar 属于持久化 manifest）。
   给出时，适配器门 `G0.manifest_binding`（metric `manifest_binding_mismatch_count`）核对：setup 的 manifest 哈希 = 包装的哈希；
   重跑的每根 bar（按内容哈希）都在包装内（回测经 `G0.reproducibility`、标签经同一批 bar 与之绑定）；所验证标的有 bar；无 bar 晚于包装的
   `price_cutoff`。**不符判 FAIL，而非 INCONCLUSIVE**：不符不是证据不足，而是标签与回测描述的是不同数据，属于与 `G0.bindings` 同类的
   绑定矛盾（07-validation §2：G0 fail → Failure Registry；Constitution C-P1：复现元组绑定数据快照；C-P3 精神下不可证明的复现不得通过）；
   `reason_for_gate` 的 `G0.` 行将其记为 `REJECTED` / `CONTRACT_VIOLATION`。验证器不重新证明 manifest 本身（research 不持有 catalog 句柄），
   只证明它验证的正是包装所证明的。`dataset_bars=None` 为**合成路径**（合成实验室、研究循环、校准）：manifest 哈希只是未经验证的标签，
   不加门（不可验证的绑定既非 PASS，也不应改变合成实验的判定），报告视图 `extra.price_binding = {"mode": "synthetic_unverified", "verified": false, ...}`；
   数据集路径为 `"dataset_manifest_verified"`，并列出 `mismatches`。该字段是 `ValidatorSetup` 唯一带默认值的字段，仅为保持合成调用方不变，且始终在视图中标注。

回归测试：`tests/research/strategies/test_backtest_validation.py`（哈希不符被 G0 拒绝并以 CONTRACT_VIOLATION 入册；哈希一致时
`G0.manifest_binding` PASS 且其余门与合成路径完全相同；manifest 外的 bar / 过晚 cutoff / 无标的 bar 被检出；合成路径被标注；公开构建器即
`validate` 所用输入；FAIL 后的诊断 G4 只报告、不改判定）。真实数据冒烟改用公开诊断 API 并传入 `DatasetPriceBars`。状态仍为
FRAMEWORK_IMPLEMENTED / NOT_VALIDATED。

## Implementation note (durable ledgers, 2026-09-25)

不新增 ADR，不改契约。调试批次的发现：`SealedOosVault` 的开封账本（含 `claim_evaluation` / `mark_evaluated` 与逐族批准）只存在于内存，
进程重启后"每族只开封一次""每次开封只评估一次""全局开封预算"都不再跨进程生效——重启后的进程可以把已开封过的族再开封一次。

修复只加不改：新增共享模块 `research.persistence.AppendOnlyJournal`（哈希链、只追加的 JSON-lines 文件，写法对齐
`research.strategies.failure_registry.FailureRegistry`——`append` 是唯一的写操作，`flush` + `fsync`，文件变短即 `JournalCorrupted`
拒绝写入），额外加一条 SHA-256 哈希链：每行携带自身内容哈希与前一行的哈希（`core.domain.base.canonical_json` 规范化），重新打开文件时
重放并校验整条链，哈希不符、链断开、行被截断或出现未知记录类型一律 `JournalCorrupted`，不静默修复、不跳过。

`research/validation/sealed_oos.py` 新增 `DurableUnsealingLedger`（实现既有的 `UnsealingLedger` Protocol，行为与
`InMemoryUnsealingLedger` 完全一致，只是落盘）；`SealedOosVault.__init__` 新增可选关键字参数 `path`，与既有的 `ledger` 参数二选一
（都省略时默认新建 `InMemoryUnsealingLedger()`，与原有行为相同；同时给出 `ledger` 与 `path` 报错）。所有既有调用点都显式传 `ledger`，
不受影响，API 完全向后兼容。同一批次里 `research/hypotheses/ledger.py` 的 `TrialLedger` 与 `research/evolution/lineage.py` 的
`LineageGraph` 也获得同样的可选 `path`（见各自模块与 ADR-0040 / ADR-0045 的同名实施说明）。

回归测试：`tests/research/persistence/test_journal.py`（哈希链、重放、篡改/重排/截断/文件变短拒绝、确定性重放同一状态哈希、
NaN/Infinity payload 拒绝）；`tests/research/validation/test_durable_sealed_oos.py`（进程 A 开封的族在进程 B 无法再开封；进程 A
`claim_evaluation` 消耗的评估进程 B 无法再领取；全局预算跨重启计数；篡改文件拒绝；确定性重放）。状态仍为
FRAMEWORK_IMPLEMENTED / NOT_VALIDATED；Profile 数值无变化，本说明不涉及任何验证规则或阈值。

## Implementation note (manifest pair in G0, 2026-09-26)

不新增 ADR，不改契约、`core/domain`、Profile 或阈值。完成 ADR-0037 Implementation note（E1 manifest pairing）中留给研究侧的后续：
同一条研究链的特征取自**区间** manifest、价格取自**单点** manifest，二者由 `infrastructure.bars.pair_manifests` 证明为同一份市场数据并
记录为 `ManifestPair`；此前验证器只核对价格侧（E5），不知道特征来自哪份 manifest。

1. `ValidatorSetup` 新增两个带默认值的可选字段：`manifest_pair: ManifestPair | None = None` 与
   `feature_manifest_hashes: tuple[str, ...] = ()`。验证器**看不到**特征 manifest：`SignalObservation` 与 `TrialRun` 都不携带 manifest
   哈希，因此由调用方显式传入每个供给信号的特征请求的 `manifest_content_hash`，由验证器比对。
2. 给出 pair 时，适配器门 `G0.manifest_binding` 在 E5 的检查之外再核对：`pair.price_manifest_hash == dataset_bars.manifest_content_hash`
   （`pair_price_manifest`）；`feature_manifest_hashes` 非空且每一项都等于 `pair.feature_manifest_hash`（`pair_feature_manifest`）；
   `pair.pair_hash` 按规则重算一致（`pair_hash`，经新公开的 `infrastructure.bars.pair_hash_of`，与 `ManifestPair` 构造时的复核同一函数，
   不复制规则）。不一致的 setup 同样判不符而非跳过：给出 pair 却无 `dataset_bars`（`pair_without_dataset_bars`）、给出特征哈希却无 pair
   （`feature_hashes_without_pair`）。**任一不符判 FAIL**，理由与 E5 相同（特征、标签与回测描述的不是同一份数据，属 `G0.bindings`
   同类的绑定矛盾），`reason_for_gate` 记为 `REJECTED` / `CONTRACT_VIOLATION`。
3. 不给 pair 时 E5 行为完全不变；三者都不给仍为合成路径（不加门，视图 `synthetic_unverified`）。给出 pair 或特征哈希时，报告视图
   `extra.price_binding` 另记 `manifest_pair`（两哈希与 pair 哈希）与 `feature_manifest_hashes`；无 `dataset_bars` 时 `price_cutoff` 为 `null`。
4. 架构边界：`tests/test_architecture_boundaries.py` 只禁止 core → 外层与 apps → research，research 导入 `infrastructure.bars` 已有先例
   （`DatasetPriceBars`），故直接使用 `ManifestPair` 类型，无需结构化 Protocol。
5. 限制：验证器不重新证明配对本身（research 不持有 catalog / builder）；`ManifestPair` 是 `pair_manifests` 证明的记录，验证器证明它所验证的
   正是该记录所指的两份 manifest。`feature_manifest_hashes` 由调用方给出：信号值本身不经特征请求哈希回溯证明（契约无此字段，需要时另议）。

回归测试：`tests/research/strategies/test_backtest_validation.py`（匹配的 pair 通过且门与无 pair 的数据集路径完全相同；价格哈希不符、
特征哈希不符、伪造 pair 哈希、无 `dataset_bars` 的 pair 均在 G0 判 FAIL 并以 CONTRACT_VIOLATION 入册；各项不符逐一检出；合成路径不变）。
真实数据冒烟 `tests/infrastructure/e2e/test_research_pipeline_real_data.py` 改为传入 pair 与两个特征请求的 manifest 哈希，断言
`G0.manifest_binding` PASS 且视图记录 pair。状态仍为 FRAMEWORK_IMPLEMENTED / NOT_VALIDATED。

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

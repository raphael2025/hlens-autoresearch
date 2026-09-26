# ADR-0052: 验证契约补全——精确小数、Profile 新字段与负对照独立阈值（D-FLOAT、D-PFIELDS、D-CTRL）

| 字段 | 值 |
|---|---|
| 状态 | Proposed (2026-09-26)，起草: Claude Code（Opus），待 Raphael 决定（红线） |
| 日期 | 2026-09-26 |
| 决策者 | **Raphael**（H1 Domain Contract、H2 Validation Profile 结构，红线） |
| 起草者 | Claude Code（Opus） |
| 相关 Phase | Phase 4 两步冻结 Step 2 之前；Phase 8 G4 |
| 影响范围 | Contract（`core/domain/research.py`、`core/contracts/validation_profile.py`）/ Validation / 复现 |
| 是否破坏兼容 | 推荐方案否：只加可选字段，旧载荷哈希逐位不变；类型原地改写（方案 A）则是 major |
| 前置 | [ADR-0007](0007-validation-architecture-three-layers.md)、[ADR-0008](0008-contract-payload-immutability.md)、[ADR-0009](0009-experiment-identity-binding.md)、[ADR-0013](0013-deterministic-verdict-and-finite-numbers.md)、[ADR-0014](0014-validation-profile-structural-invariants.md)、[ADR-0041](0041-validation-robustness.md) |

## 决策包（Decision packet）

- **问题**：验证契约是否（1）为进入哈希的验证数值增加精确小数表示，（2）为 Profile 增加 G4 与封存 OOS 缺少的字段，（3）给负对照一个独立的显著性字段？三者都改冻结契约，因此合在一份 ADR 中，但可以分项批准。
- **选项**：A. 新 major（3.0.0），原地把浮点改成小数并加字段；B. 仍在 major 2 内：加可选字段，浮点字段弃用，旧载荷哈希不变；C. 不改契约，只在流水线中把计算值量化（部分缓解）。
- **推荐**：B（三项一起），数值一个都不定，留给 Step 2 校准。
- **不决定时保持不变**：浮点照旧（同机可复现）；缺字段的检查用 `param:` 显式参数，未给则 `INCONCLUSIVE`，绝不判通过；负对照继续与 G3 共用 `significance.multiple_testing_threshold`（偏保守）。

## 背景

1. **D-FLOAT**：`GateResult.value` / `threshold`（`core/domain/research.py:320-321`）与 Profile 的全部阈值（`validation_profile.py`
   第 95、96、140、142、152、161、162、179 行、`PositiveMultiplier`、`degradation_thresholds`、`inconclusive_bands`）是 `float`，
   经 `ValidationReport` 与 `validation_profile_hash` 进入内容哈希。`canonical_json` 不宣称跨语言浮点规范（02-domain §3.1）；
   统计值在不同 libm / numpy 版本下的末位差异会改变报告哈希。ADR-0041 已如实记录此缺口。
2. **D-PFIELDS**：ADR-0041 §1 规定"Profile 没有字段的规则"只能用 `param:<name>` 显式参数或判 `INCONCLUSIVE`；
   涉及容量、跨资产一致性、CSCV 分块数、封存 OOS 开封预算、欠采样状态收益占比。C-R2 / C-R3 / C-R5 / C-S2 因此都没有 Profile 来源。
3. **D-CTRL**：P9 × P8 校准发现 `significance.multiple_testing_threshold` 被反向双用——G3 要求校正后 p ≤ 阈值
   （`research/validation/pipeline.py:437`），G1 负对照要求对照 p ≥ 阈值（同文件 `:263`）。放宽前者即收紧后者，Profile 无法分别调节。

版本规则：02-domain §3 第 3 条"minor 只能添加可选字段；删除 / 重命名 / 语义变化 = major + ADR + 迁移"；D-25：2.0.0 已随 Phase 0
发布，此后破坏性变化必须升 major；ADR-0008 / 0009：旧 major 只读保留，不得用新算法重算旧载荷；内容哈希包含信封 `schema_version`
（`Contract._non_semantic_fields` 默认只排除 `created_at`），数据面各表逐行记录 `contract_schema_version`。

## 裁决（提案，方案 B）

### 1. 精确小数（D-FLOAT）

- 新类型 `ExactDecimal`：JSON 中为字符串，规范形式（无指数、无多余前后零、`-0` 归一为 `0`），构造时拒绝浮点输入、NaN、±Infinity。
- `GateResult` 增加可选 `value_exact`、`threshold_exact`。存在时：浮点字段由其派生（`value == float(value_exact)`，否则拒绝），
  **判定只用精确值比较**，且该模型的哈希载荷排除对应浮点字段；不存在时省略出载荷——旧载荷哈希逐位不变。
- 计算值写入 `value_exact` 前按一条版本化量化规则 `hlens.validation.gate-value-quantization@1.0.0` 取定点（精度是表示规则，
  不是验证阈值，由实施批次提出、随规则版本冻结）。
- Profile：每个浮点阈值字段加 `*_exact` 兄弟字段（同名加后缀），规则同上；映射字段 `inconclusive_bands` / `degradation_thresholds`
  加 `*_exact` 映射。新冻结的 Profile 必须只用精确字段（由登记 / 冻结服务执行，契约层不追溯）。
- 浮点字段标为 **deprecated**，下一次因其他原因发生的 major 中删除；本 ADR 不单独触发 major。

### 2. Profile 新字段（D-PFIELDS，只有字段，不给数值）

全部为可选、类型 `ExactDecimal` / `int`，只带结构范围（ADR-0014 风格）：

| 字段路径 | 结构范围 | 原则 | 取代的显式参数 |
|---|---|---|---|
| `capacity.min_capacity` | `> 0` | C-R5 | `param:capacity.min_capacity` |
| `capacity.max_participation_rate` | `(0, 1]` | C-R5 | `param:capacity.max_participation_rate` |
| `capacity.impact_coefficient` | `>= 0` | C-R5 | `param:capacity.impact_coefficient` |
| `capacity.impact_model` | 已实现的方法名 | C-R5 | 代码常量 `IMPACT_MODEL` |
| `cross_asset.min_positive_fraction` | `[0, 1]` | C-R3 | `param:cross_asset.min_positive_fraction` |
| `significance.cscv_partitions` | 偶数且 `>= 2`（CSCV 结构要求） | C-T1 / C-R1 | `param:cscv_partitions` |
| `data_split.sealed_oos_max_unsealings` | `> 0` | C-S2 | `param:max_unsealings` |
| `sample_size.max_undersampled_pnl_share` | `[0, 1]` | C-R2 | `param:state.max_undersampled_pnl_share` |
| `significance.negative_control_threshold` | `[0, 1]` | C-L6 | 见 §3 |

来源规则：Profile 有该字段时，阈值只来自 Profile，**同时传入 `param:` 显式参数即拒绝**（研究者不得覆盖，C-A4）；Profile 没有该字段
（旧 Profile）时维持 ADR-0041 §1 的现行行为（`param:` 或 `INCONCLUSIVE`），保证旧 Profile 重放结果逐位不变（C-P4）。
开封预算的**计数账本**仍是全局的；Profile 字段只给出绑定该 Profile 的实验可消耗的上限；族批准与批准人仍是实验元数据（`OosUnsealBudget`）。

### 3. 负对照独立字段（D-CTRL）

G1 `shuffle_control` / `shift_control` 改为"对照 p ≥ `significance.negative_control_threshold`"；G3 继续只用
`multiple_testing_threshold`。字段缺失（旧 Profile）时沿用双用，报告中 `threshold_source` 如实写出所用字段。

### 4. 版本与迁移

- 契约版本：按 02-domain §3 第 3 条升 minor **2.0.0 → 2.1.0**；v2 旧载荷保留自己的 `schema_version`，哈希不变。
  **风险**：新构造对象的信封变为 2.1.0，内容相同的对象哈希随之改变；数据面各表记录 `contract_schema_version`，
  规范化 / 质量报告 / precedence 的幂等重放可能比较信封。实施批次必须**先**盘点这些重放路径并用测试证明行为；
  若会破坏已提交数据的幂等重放，停止并报告 `ARCHITECTURE_DECISION_REQUIRED`，不自行选择。
- Schema：重导出 `schemas/`；`schemas/v1/` 与 v1 向量逐字节不变；新增 v2.0.0 固定向量证明旧 `GateResult` / `ValidationReport` /
  `ValidationProfile` 哈希不变。
- 无已冻结 Profile、无已登记 Profile 实例，不需要数据迁移；测试夹具（TEST ONLY）改用精确字段，其数值不构成建议。

## 备选方案

| 方案 | 优点 | 缺点 | 结论 |
|---|---|---|---|
| A. 新 major 3.0.0，原地改类型 | 表示最干净，无双字段 | 全局 major：所有契约信封改变，数据面已提交行（D-NET）需 v2 只读兼容与重放迁移；代价远超收益 | 不推荐 |
| **B. major 2 内加可选字段 + 弃用** | 旧哈希不变；新对象跨平台确定；可分项实施 | 弃用期内双表示，需一致性校验 | 推荐 |
| C. 只在流水线量化后仍存浮点 | 不改契约 | 量化边界附近仍可能翻转；Profile 阈值问题不变 | 不推荐 |
| 不改 D-PFIELDS，永远用 `param:` | 零成本 | 阈值不在 Profile 中，违背 ADR-0007 归属规则；研究者可自选参数 | 不推荐 |

## 后果

- 正面：新报告与新 Profile 的哈希跨平台一致；C-R2 / C-R3 / C-R5 / C-S2 / C-L6 第一次有 Profile 来源；两类显著性可分别校准。
- 负面：弃用期双字段；minor 信封变化的影响需先盘点；Step 2 校准须为新字段给出数值（由 Raphael 冻结）。
- 复现：旧载荷按原哈希读取；旧 Profile 重放逐位不变。

## 现用显式参数的代码位置（实施时逐一改为 Profile 来源）

- `research/validation/g4.py:72` `RobustnessParams`（`cscv_partitions`、`max_participation_rate`、`min_capacity`、`impact_coefficient`、
  `cross_asset_min_positive_fraction`、`max_undersampled_pnl_share`）及其 `to_dict` 的 `"param (explicit; no Profile field exists)"`；
- `research/validation/robustness.py`：`overfitting_check`（`:219`，`param:cscv_partitions`）、`state_decomposition_check`（`:733`）、
  `capacity_check`（`:867`，含 `impact_coefficient_source`）、`cross_asset_check`（`:987`）；
- `research/validation/gates.py:59 / :104 / :163`（`PARAM_SOURCE_PREFIX`、`explicit_threshold`、`missing_field_gate`）；
- `research/validation/sealed_oos.py:253-268`（`SealedOosVault(max_unsealings)`，`budget_source = "param:max_unsealings"`）；
- `research/validation/pipeline.py:263`（负对照）与 `:437`（G3）；
- `research/strategies/validation.py:290`、`research/loop/trials.py:165 / :207 / :841`、`research/loop/compose.py:172 / :557`、
  `research/loop/durable.py:131 / :216`（`RobustnessParams` / `OosUnsealBudget` 的传递与状态绑定）；
- `research/synthetic_lab/gate_calibration.py:463`（TEST ONLY 校准的 `max_unsealings`）；
- `plugins/backtest/execution.py:94`（`ExecutionModel` 的参与率与冲击系数是**执行模型参数**，保留；G4 继续核对与 Profile 一致）。

## 实施计划与测试（批准后，可分三批：D-CTRL → D-PFIELDS → D-FLOAT）

`core/domain/base.py`（`ExactDecimal`、按模型省略缺失字段的哈希载荷）、`core/domain/research.py`、`core/contracts/validation_profile.py`、
上列研究代码、`docs/architecture/07-validation.md` §5、`02-domain.md`。测试：旧载荷哈希金值不变；精确 / 浮点不一致拒绝；精确值判定；
Profile 字段与 `param:` 同时给出即拒绝；旧 Profile 重放逐位不变；负对照与 G3 可独立调节（P9 × P8 校准复跑）；四项工程检查。

## 合规检查

- [x] 不写入任何数值阈值（H3、ADR-0007）；结构范围只有符号 / 单位区间 / 偶数
- [x] 不修改 Validation Constitution；动机不是让某实验通过
- [x] Domain 层仍只依赖标准库与 Pydantic
- [ ] 由 Raphael 本人批准（H1 / H2 红线）——待定

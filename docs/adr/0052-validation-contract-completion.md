# ADR-0052: 验证契约补全——精确小数、Profile 新字段与负对照独立阈值（D-FLOAT、D-PFIELDS、D-CTRL）

| 字段 | 值 |
|---|---|
| 状态 | Accepted (2026-09-26)，决策者: Raphael（"同意推荐方案"），起草: Claude Code（Opus）；§4 前置盘点曾失败（Implementation blocker）；**实施中**：Codex K3 授权按记录版本重放，见 Implementation note — versioned replay (2026-09-26) |
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

## Implementation blocker (2026-09-26)

决策者: Claude Code（Opus），依 Raphael 2026-09-26 明确授权（"所有的决策都由你来决定，包括红线"）；按协调者转达的 Codex review K3 规则
（本 ADR 必须按 §4 升到 **2.1.0**，不得把新字段伪装成 2.0.0；§4 的盘点若要求修改 Phase 1 基础设施则停止并提交证据）。
**结论：规则 (c) 适用——§4 的前置盘点失败，本 ADR 未实施**（状态仍为 Accepted、未实施）；契约、Schema 与研究代码保持 2.0.0 旧行为。

**证据**（`tests/infrastructure/canonical/test_contract_version_replay.py`，实际运行
`pytest -m "not postgres" -rxX tests/infrastructure/canonical/test_contract_version_replay.py` → `2 passed, 1 xfailed`）：

- 以当前版本规范化一个 Raw 单元并提交后，把规范化器读取的版本改为 `2.1.0`（只打补丁 `infrastructure.canonical.rules.CONTRACT_SCHEMA_VERSION`，
  其余一切不变）再重放同一单元：`CatalogIntegrityError: batch hlens.canonical.binance-spot.normalizer@1.0.0.rev1-… of canonical.trades was
  committed with other content`，表未被写入。对照：不改版本时同一重放幂等（`replayed`、head 与行不变）。
- 期望性质 `test_replay_after_a_minor_bump_is_idempotent` 以 `xfail(strict=True, raises=CatalogIntegrityError)` 提交：旧版本重放路径实现后它会
  XPASS 并迫使移除标记。

**机制与必须修改的位置（全部属 Phase 1 基础设施，正在 Codex 审阅，本批次不得修改）**：

1. `infrastructure/canonical/rules.py:40` 导入、`:670`（`canonical_row`）把**实时**的 `CONTRACT_SCHEMA_VERSION` 写入每行
   `contract_schema_version`；
2. `infrastructure/revision/row_integrity.py:347-360`（`check_batch_snapshot`，由 `infrastructure/canonical/normalizer.py:386`、`:570` 调用）
   用重建的行重算已提交批次的指纹——版本列变化即"committed with other content"；
3. `infrastructure/canonical/normalizer.py:1212`（`_exact`）逐列比较已提交行与重建行（下一道关口，会报 `['contract_schema_version']`）；
4. 同类"新对象信封 = 实时版本"的写入点，升版后新建对象的身份也会漂移：`infrastructure/canonical/listing_rules.py:709`、
   `infrastructure/revision/channel_precedence.py:496`、`infrastructure/revision/exchange_info_store.py:189`、
   `infrastructure/revision/row_integrity.py:726`、`infrastructure/revision/store.py:977`、`infrastructure/dataset/manifests.py:61`
   （`ResearchDatasetManifest` 的内容哈希含信封版本：升版后重建同一 manifest 得到不同哈希）。

**提议的旧版本重放设计**（待 Codex 审阅 Phase 1 后，由其批次实施；之后本 ADR 才能按 2.1.0 实施）：

1. Canonical 行的 `contract_schema_version` 与实时常量解耦：规范化器持有一个钉住的行契约版本，写进 `NORMALIZER_SPEC`（因而进入
   `NORMALIZER_HASH` / 规则版本）；改变它是一次显式的规范化规则版本变更，而不是契约 minor 的副作用。
2. 重放已提交单元时，用该单元**记录的**版本（其已提交行的 `contract_schema_version`，同一单元必须唯一）重建计划行，然后照旧做批次指纹与
   逐列比较（严格性不变）；新单元用当前钉住的版本。从行重建 `RevisionRecord` / `ObservationTimes` 时保留行上的版本（`channel_reconcile`、
   `listing_record_from_row` 已如此）。
3. 其余写入点与 manifest 同理：重建已提交对象时沿用其记录版本；契约层已接受任何 2.x 信封（`_supported_major`），读取不改写版本
   （`tests/test_v2_golden_vectors.py` 证明 2.0.0 载荷按记录版本读取、哈希逐位不变）。
4. 验收：上面的 strict-xfail 测试转为通过；新增"2.1.0 新单元 + 2.0.0 旧单元同表"的重放与 PIT 读取测试；D-NET 已提交数据重放。

**已完成且保留的前置工作**：`tests/golden/v2_0_0/`（`GateResult`、`ValidationReport`、`ValidationProfile`×2、Canonical 行背后的
`RevisionRecord` 的 2.0.0 载荷与哈希，在任何改动前生成）与 `tests/test_v2_golden_vectors.py`。部分实现（`ExactDecimal`、精确兄弟字段、
精确 `compare_gate`；按旧指示在 2.0.0 下）停放在本地分支 `wip/adr-0052-exact-fields`（commit `8e4a71c`，未合并、不得合并），
解除阻塞并升到 2.1.0 后可作为起点。

**同一问题的已知关联**：ADR-0054（部分成交结转）按其批准时的指示以 2.0.0 发布了可选字段（见其实施说明）；若 K3 的"新字段不得以 2.0.0
发布"规则也适用于它，需随本阻塞一并处理。

## Implementation note — versioned replay (2026-09-26)

依据：Codex 全代码复核 K3 与"复核后的实施授权（2026-09-26）"（`docs/reviews/2026-09-26-codex-full-code-review.md`，
`origin/codex/full-code-review-2026-09-26` @ `942160c`）授权在独立 Phase 1 分支 `phase1/adr-0052-versioned-replay` 实现
"按持久化记录版本重放旧对象"，再按 §4 升到 2.1.0。本节是**先于代码提交的设计**（M0）；M1 ~ M3 的实现与门禁结果追加在后。

### 盘点证据（比上节 blocker 列出的 7 处更宽）

一次性跨进程探查（脚本不入库）：进程 A 以 2.0.0 跑完 Phase 1 first slice（exchangeInfo → listings、archive + REST × 两标的 ×
两数据类型、reconcile、normalize、quality、dataset build + manifest），进程 B 只把 `CONTRACT_SCHEMA_VERSION` 改成 `2.1.0`
后逐步重放 / 读取同一目录。**24 步中 23 步失败**（同版本对照：0 失败）：

| 失败点 | 机制 |
|---|---|
| Raw archive / REST response / REST element / exchangeInfo 行校验（`row_integrity`、`exchange_info_store`） | 单一构造器用实时版本重建已提交行，`['contract_schema_version']` 不一致 |
| archive store 重新 ingest | 行批次按实时版本重建 → `BatchConflict ... committed with different content` |
| Canonical normalize（archive 与 REST 单元）、quality report、listing derive、reconcile | 均经上面的 Raw 行校验失败（Canonical 自身的 `canonical_row` 是第二道关口，见上节） |
| PIT select | `PIT_BINDING` 是模块常量，按实时版本构造；旧 spec 中的 2.0.0 绑定与之结构不等 |
| dataset build / manifest load | 登记的 `FIRST_SLICE_UNIVERSE` 按实时版本构造 → `spec_hash` 改变 → "not a registered spec" |

结论：问题有四类——(a) 行 / 对象重建用实时版本；(b) 代码中登记的身份常量（`PolicyBinding`、`SourceBinding`、登记的
universe spec）随 minor 改变身份；(c) 由登记对象派生的投影（`UniverseSelectionSpec.binding()`）；(d) manifest 的重放与复核。

### 规则

- **V1 记录版本重放。** 每个已持久化的行 / 对象按其**提交时记录的版本**重建并比较：行取自身的 `contract_schema_version`，
  载荷取自身的 `schema_version`。一个"写入组"只有一个版本：Canonical 单元（一次 Raw 来源修订 × 数据类型）、REST response
  及其 elements、archive revision 及其行、一次 exchangeInfo snapshot、一次 listing 派生、一条 edge、一个 manifest；
  组员（elements / archive 行）按其父对象的记录版本重建（与 block base、ready / knowledge time 取自父对象同理）。
  记录版本必须属于 `PUBLISHED_CONTRACT_SCHEMA_VERSIONS`（M1 为 `("2.0.0",)`，M2 起 `("2.0.0", "2.1.0")`）；未发布的版本、
  或同一组出现两个版本，一律 `CatalogIntegrityError`（fail closed）。比较的严格性不变（批次指纹、逐列 `_exact`）。
- **V2 新对象按当前版本写。** 没有任何已提交成员的组按当前版本写入（M2 起 2.1.0）；部分提交的组（写到一半停止的单元）
  按其**已记录**版本补完，绝不在一个组内混版本。
- **机制。** `core/domain/base.py` 增加 `contract_schema_version_scope(version)`（`ContextVar`）与
  `current_contract_schema_version()`：作用域内构造、且未显式给出 `schema_version` 的契约对象取作用域版本（`Contract` 的
  before 校验器只在字段缺省时填入，显式值与已构造的嵌套对象不受影响；无作用域时行为与今天逐位相同，默认值与 Schema 不变）。
  重放 / 校验入口在"记录版本"的作用域内重建；新组的写入者盖 `current_contract_schema_version()`。`canonical_row` 改为盖它自己
  构造的 `RevisionRecord` 的版本，不再导入实时常量。选择作用域而不是逐参数传递，是因为重建会经过不应修改的代码
  （例如 PIT selector 内构造的 `SelectedRevisionLineage`、universe builder 的成员），逐参数传递到不了那里。
- **V3 已发布的身份冻结。** 被持久化数据按内容引用的代码登记对象——Phase 1 全部 `PolicyBinding` 常量、`SourceBinding`
  常量、`FIRST_SLICE_UNIVERSE`——是已发布对象：其信封版本固定为发布时的 `2.0.0`（显式写出），是身份的一部分；
  minor 升版不重新发布它们。规则的新版本在其发布时的当前契约版本下发布。
- **V4 投影继承。** 由契约对象派生的投影携带源对象的信封：`UniverseSelectionSpec.binding()` 的 `UniverseSpecBinding`
  取 spec 的 `schema_version`（2.0.0 下与今天逐位相同）。
- **V5 规则身份不变，版本是写入时事实。** 输出身份（`observation_key`、`source_id`、`payload_hash`、`revision_id`、批次 id、
  arrival 计划）不含信封版本；`NORMALIZER_SPEC` 与各规则 spec / hash 一律不改（它们被 PIT spec 与 manifest 绑定，改动会
  改变既有 manifest 哈希）。信封版本与 block base、ready time 一样是组的写入时事实：从已提交行恢复，从不重新计算。
  因此同一张表中一个来源单元只有一组行（升版后重放采纳已提交的 2.0.0 行，不会产生 2.1.0 的重复行）；在两个全新 catalog 中
  于不同时期写入的同一来源，只在信封与写入时事实（`knowledge_time`、`arrival_seq`）上不同，`revision_id` 相同——以测试证明。
  不在同一规则版本下产生来源相同、内容不同的重复 Canonical 行。
- **V6 同表混合版本。** 表内 2.0.0 组与 2.1.0 组并存。读者（PIT selector、dataset builder、listings / universe、quality）
  把信封当数据：选择、precedence、窗口从不读取它；从行重建的 `RevisionRecord` 保留行上的版本；混合表上的 PIT 选择
  返回每个修订各自的版本；manifest 绑定 snapshot id，不绑定版本。以"先提交 2.0.0 单元、再提交 2.1.0 单元"的测试证明。
- **V7 manifest。** `ManifestStore.load` / `verify_manifest` 在 manifest 记录版本的作用域内重建比较；数据集批次为重放
  （同一 `selection_id`）时，`build` 采用已为该数据集 snapshot 持久化的 manifest 的版本，不另写一个只差信封的 manifest。
- **调用方输入。** 调用方新构造的 PIT spec 等对象是新对象（ADR-0008：内容哈希含信封）；重放一次已记录的构建，
  应使用其记录的输入（例如 manifest 自带的 PIT spec）。

### 备选方案（未选）

- 把行版本钉进 `NORMALIZER_SPEC`（上节 blocker 设计 §1）：要么改 `NORMALIZER_HASH`（被每个 PIT spec / manifest 绑定，
  既有 manifest 哈希改变），要么 Canonical 行永远停在 2.0.0（新对象不写 2.1.0）；且覆盖不到 Raw / exchangeInfo / 绑定常量。
- 按模型分信封（只有变更模型用 2.1.0）：与 §4"新构造对象的信封变为 2.1.0"及 K3 相反，不选。
- 只逐参数传递版本：到不了 PIT selector 内部构造的对象（该文件由 K4 修复拥有，不改逻辑），不选。

### 与"不修改 PIT selector 逻辑文件"的唯一交点

V3 要求把 `infrastructure/pit/selector.py` 的 `PIT_BINDING` 常量写出 `schema_version`——一行数据，与 K4 修复的改动块不相交，
不改任何选择逻辑；单独提交，便于协调者在 K4 集成时核对。

### 里程碑与门禁

- **M1**（仍为 2.0.0）：机制 + V1 ~ V4 的全部入口；证明今日行为逐位不变（现有套件、2.0.0 黄金向量、Schema 不变），
  并证明未发布 / 混合记录版本 fail closed；用作用域模拟"当前版本为 2.1.0"时，已提交 2.0.0 单元的重放幂等。
- **M2**：`CONTRACT_SCHEMA_VERSION = 2.1.0`；上节 strict-xfail 证据转为通过；同表混合版本、D-NET 已提交数据重放、
  PIT 读取 / manifest 哈希回归、2.0.0 黄金向量逐字节不变、Schema 重导出（`schemas/v1` 与 v1 向量逐字节不变）。
- **M3**：本 ADR §1 ~ §3 的契约字段（2.1.0，方案 B）；研究侧取值（`research/validation` 来源规则、C-A4 拒绝同时给出 `param:`）
  不在本 lane，由协调者在全代码分支完成。
